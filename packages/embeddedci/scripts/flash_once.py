#!/usr/bin/env python3
"""Flash a target once over serial and report timing + pod RX/TX counters.

Resets the pod's rx-stats before the flash, runs the flash, then (after the
console returns from SWD mode) reads rx-stats so we can see whether any serial
loss (hw_overrun / ring_dropped) occurred during a real OpenOCD flash.

Run from a checkout with the package installed (``pip install -e .``)::

    python scripts/flash_once.py /dev/ttyUSB0 firmware.elf
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time

import serial

from embeddedci import benchpod


def read_stats(port: str, note: str) -> None:
    p = serial.Serial(port, 115200, timeout=0.3)
    time.sleep(0.3)
    p.write(b"\r"); p.flush(); time.sleep(0.2); p.reset_input_buffer()
    p.write(b"rx-stats\n"); p.flush(); time.sleep(0.4)
    out = p.read(400).decode(errors="replace")
    p.close()
    line = [l for l in out.splitlines() if "rx_total=" in l]
    print(f"[{note}] {line[-1] if line else out!r}")


def reset_stats(port: str) -> None:
    p = serial.Serial(port, 115200, timeout=0.3)
    time.sleep(0.3)
    p.write(b"\r"); p.flush(); time.sleep(0.2); p.reset_input_buffer()
    p.write(b"rx-stats reset\n"); p.flush(); time.sleep(0.3)
    p.read(200)
    p.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port", help="serial device of the pod, e.g. /dev/ttyUSB0")
    ap.add_argument("elf", help="firmware image to flash")
    ap.add_argument("--target", default="target/stm32f4x.cfg",
                    help="OpenOCD target config (default: %(default)s)")
    ap.add_argument("--log-file",
                    default=os.path.join(tempfile.gettempdir(), "openocd_full.log"),
                    help="where to write the full OpenOCD stderr (default: %(default)s)")
    args = ap.parse_args()

    print(f"port={args.port}\nelf={args.elf}\ntarget={args.target}")
    reset_stats(args.port)
    read_stats(args.port, "before")
    t0 = time.monotonic()
    with benchpod.BenchPod(args.port) as bp:
        r = bp.flash(
            file=args.elf, target=args.target,
            swclk=11, swdio=12, nreset=3,
            target_power=benchpod.INTERNAL,   # DUT runs off the internal 5V eFuse
            verify=False, connect_attempts=1,
            timeout=300.0,
            check=False,
        )
    dt = time.monotonic() - t0
    print(f"\n=== flash {'OK' if r.ok else 'FAIL'} rc={r.returncode} "
          f"stalled={r.stalled} unreachable={r.target_unreachable} ({dt:.0f}s) ===")
    with open(args.log_file, "w") as f:
        f.write(r.stderr)
    print(f"full openocd stderr -> {args.log_file}")
    print("--- openocd stderr tail (last 25 lines) ---")
    for l in r.stderr.splitlines()[-25:]:
        print(l)
    read_stats(args.port, "after")
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
