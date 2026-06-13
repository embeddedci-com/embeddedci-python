#!/usr/bin/env python3
"""BenchPod serial-link stress test — localize byte loss without OpenOCD.

Drives the firmware's `echo-test` console modes over the USB-serial console to
answer, in seconds instead of 20-minute flash runs:

  1. sink:  host->pod only.  Pod counts every byte it receives; compare with
            what we sent.  Pod-side `rx-stats` says WHERE a loss happened:
            hw_overrun>0  -> the PL011 32-byte FIFO overran (RX IRQ starved)
            ring_dropped>0-> the 8KB software ring overflowed (main loop stuck)
            both zero but rx_total short -> bytes never reached the pod's UART
            (CH340 / macOS driver / cable lost them).
  2. src:   pod->host only.  Pod transmits a deterministic LCG pattern; we
            verify every byte and report the first divergence offset.
  3. echo:  full duplex.  Every byte echoed back; verified byte-for-byte with
            a bounded in-flight window (mimics remote_bitbang round-trips).

Usage:
  python scripts/serial_link_stress.py --port /dev/ttyUSB0
      [--bytes 262144] [--chunk 4096] [--tests sink,src,echo]
"""

from __future__ import annotations

import argparse
import sys
import time

import serial  # type: ignore

LCG_SEED = 0x12345678


def lcg_bytes(n: int, seed: int = LCG_SEED) -> bytes:
    out = bytearray(n)
    s = seed
    for i in range(n):
        s = (s * 1664525 + 1013904223) & 0xFFFFFFFF
        out[i] = (s >> 24) & 0xFF
    return bytes(out)


def payload_bytes(n: int) -> bytes:
    """LCG pattern with 0x51 ('Q', the exit byte) remapped to 0x50."""
    return lcg_bytes(n).replace(b"Q", b"P")


def read_until(port: serial.Serial, token: bytes, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        chunk = port.read(256)
        if chunk:
            buf.extend(chunk)
            if token in buf:
                return bytes(buf)
    raise TimeoutError(f"never saw {token!r}; got {bytes(buf)!r}")


def command(port: serial.Serial, line: str, sentinel: bytes, timeout: float = 5.0) -> bytes:
    port.write(b"\r")
    port.flush()
    time.sleep(0.05)
    port.reset_input_buffer()
    port.write(line.encode() + b"\n")
    port.flush()
    return read_until(port, sentinel, timeout)


def parse_stats(text: str) -> dict:
    # rx_total=N ring_dropped=N hw_overrun=N hw_err=N tx_raw=N
    stats = {}
    for tok in text.split():
        if "=" in tok:
            k, _, v = tok.partition("=")
            try:
                stats[k] = int(v)
            except ValueError:
                pass
    return stats


def get_stats(port: serial.Serial) -> dict:
    out = command(port, "rx-stats", b"tx_raw=")
    out += port.read(64)  # finish the line
    line = [l for l in out.decode(errors="replace").splitlines() if "rx_total=" in l][-1]
    return parse_stats(line)


def first_divergence(expected: bytes, got: bytes) -> str:
    n = min(len(expected), len(got))
    for i in range(n):
        if expected[i] != got[i]:
            # was it a dropped byte (stream shifted) or corruption?
            shifted = expected[i + 1 : i + 17] == got[i : i + 16]
            kind = "DROPPED byte (stream shifted)" if shifted else "CORRUPTED byte"
            return (f"first divergence at offset {i}: expected "
                    f"0x{expected[i]:02x} got 0x{got[i]:02x} -> {kind}")
    if len(got) < len(expected):
        return f"clean prefix, but stream ended short at {len(got)}/{len(expected)}"
    return "no divergence"


def test_sink(port: serial.Serial, n: int, chunk: int) -> bool:
    print(f"\n--- sink test: host->pod, {n} bytes, chunk={chunk} ---")
    command(port, "rx-stats reset", b"rx-stats reset")
    command(port, "echo-test sink", b"sink ready")
    data = payload_bytes(n)
    t0 = time.monotonic()
    for off in range(0, n, chunk):
        port.write(data[off : off + chunk])
    port.flush()
    # 'Q' exits and the pod reports its count
    time.sleep(0.3)
    port.write(b"Q")
    port.flush()
    out = read_until(port, b"sink done", timeout=max(10.0, n / 11000 + 10))
    out += port.read(128)
    dt = time.monotonic() - t0
    line = [l for l in out.decode(errors="replace").splitlines() if "sink done" in l][-1]
    rx = int(line.split("rx=")[1].split()[0])
    stats = get_stats(port)
    ok = rx == n
    print(f"sent={n} pod_received={rx} ({dt:.1f}s, {n/dt:.0f} B/s) -> "
          f"{'OK' if ok else f'LOST {n - rx} BYTES'}")
    print(f"pod counters: {stats}")
    if not ok:
        if stats.get("hw_overrun", 0) > 0:
            print(">>> CAUSE: PL011 hardware FIFO overrun on the pod (RX IRQ starved)")
        elif stats.get("ring_dropped", 0) > 0:
            print(">>> CAUSE: pod software ring overflow (main loop stalled)")
        else:
            print(">>> CAUSE: bytes vanished BEFORE the pod's UART "
                  "(CH340 adapter / macOS driver / wiring)")
    return ok


def test_src(port: serial.Serial, n: int) -> bool:
    print(f"\n--- src test: pod->host, {n} bytes ---")
    command(port, "rx-stats reset", b"rx-stats reset")
    expected = lcg_bytes(n)
    # Send the command and accumulate EVERYTHING from here — do not use the
    # read-until helper, which over-reads past the sentinel and would discard
    # the first payload bytes (making a clean stream look corrupt at offset 0).
    marker = f"src ready {n}".encode()
    port.write(b"\r"); port.flush(); time.sleep(0.05)
    port.reset_input_buffer()
    port.write(f"echo-test src {n}".encode() + b"\n"); port.flush()
    got = bytearray()
    deadline = time.monotonic() + n / 9000 + 20
    t0 = time.monotonic()
    while time.monotonic() < deadline:
        chunk = port.read(4096)
        if chunk:
            got.extend(chunk)
            if b"\nsrc done" in got[-80:] or b"\nidle done" in got[-80:]:
                break
    dt = time.monotonic() - t0
    # Align on the marker: payload is everything between the marker line's
    # newline and the trailing "\n<how> done ..." report.
    mpos = got.find(marker)
    if mpos < 0:
        print(f"ERROR: never saw {marker!r}; raw head={bytes(got[:80])!r}")
        return False
    nl = got.find(b"\n", mpos)
    payload_start = nl + 1 if nl >= 0 else mpos + len(marker)
    end = got.find(b"\nsrc done", payload_start)
    if end < 0:
        end = got.find(b"\nidle done", payload_start)
    payload = bytes(got[payload_start:end]) if end >= 0 else bytes(got[payload_start:])
    ok = payload == expected
    print(f"expected={n} received={len(payload)} ({dt:.1f}s) -> "
          f"{'OK' if ok else 'MISMATCH'}")
    if not ok:
        print(">>> " + first_divergence(expected, payload))
        print(">>> CAUSE: pod->host loss (CH340 RX / macOS driver) or pod TX path")
    read_until(port, b">", 10.0)  # let the prompt come back
    return ok


def test_echo(port: serial.Serial, n: int, window: int = 2048) -> bool:
    print(f"\n--- echo test: full duplex, {n} bytes, window={window} ---")
    command(port, "rx-stats reset", b"rx-stats reset")
    command(port, "echo-test", b"echo ready")
    data = payload_bytes(n)
    got = bytearray()
    sent = 0
    t0 = time.monotonic()
    stall_at = time.monotonic()
    while len(got) < n:
        if sent < n and sent - len(got) < window:
            burst = min(1024, n - sent, window - (sent - len(got)))
            port.write(data[sent : sent + burst])
            sent += burst
        chunk = port.read(4096)
        if chunk:
            got.extend(chunk)
            stall_at = time.monotonic()
        elif time.monotonic() - stall_at > 5.0:
            print(f"STALL: no echo for 5s at sent={sent} echoed={len(got)}")
            break
    dt = time.monotonic() - t0
    port.write(b"Q")
    port.flush()
    time.sleep(0.5)
    port.read(8192)
    stats = get_stats(port)
    ok = bytes(got[:n]) == data and len(got) >= n
    print(f"sent={sent} echoed={len(got)} ({dt:.1f}s, {len(got)/max(dt,0.01):.0f} B/s) -> "
          f"{'OK' if ok else 'FAIL'}")
    print(f"pod counters: {stats}")
    if not ok:
        print(">>> " + first_divergence(data, bytes(got)))
        if stats.get("hw_overrun", 0) > 0:
            print(">>> pod-side hardware FIFO overrun detected")
        elif stats.get("rx_total", -1) == sent:
            print(">>> pod received every byte we sent -> loss is pod->host")
        elif stats.get("rx_total", -1) >= 0:
            print(f">>> pod only received {stats['rx_total']}/{sent} -> loss is host->pod")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", required=True,
                    help="serial device of the pod, e.g. /dev/ttyUSB0")
    ap.add_argument("--bytes", type=int, default=262144)
    ap.add_argument("--chunk", type=int, default=4096)
    ap.add_argument("--tests", default="sink,src,echo")
    args = ap.parse_args()

    port = serial.Serial(args.port, 115200, timeout=0.1)
    try:
        # sync to a prompt
        port.write(b"\r")
        port.flush()
        time.sleep(0.2)
        port.reset_input_buffer()

        results = {}
        for t in args.tests.split(","):
            t = t.strip()
            if t == "sink":
                results[t] = test_sink(port, args.bytes, args.chunk)
            elif t == "src":
                results[t] = test_src(port, args.bytes)
            elif t == "echo":
                results[t] = test_echo(port, args.bytes)
        print("\n=== summary ===")
        for t, ok in results.items():
            print(f"  {t:5s}: {'PASS' if ok else 'FAIL'}")
        return 0 if all(results.values()) else 1
    finally:
        port.close()


if __name__ == "__main__":
    sys.exit(main())
