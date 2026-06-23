"""Flash an SWD target by driving host OpenOCD over the pod's CMSIS-DAP probe.

Pure-Python port of the Go ``runFlash``/``openDAP``/``BridgeDAP`` flow: arm the
pod's CMSIS-DAP processor (``dap_start``), spawn ``openocd`` with its ``cmsis-dap``
TCP backend pointed at a loopback bridge, and translate the per-packet framing
both ways (OpenOCD's 8-byte ``cmsis_dap_tcp`` header <-> the pod's 2-byte length
frame). OpenOCD runs the whole CMSIS-DAP host stack (batched DAP_Transfer /
DAP_TransferBlock, posted reads, WAIT retries, flash loaders); the pod executes
each transfer locally on the SWD wire. The pod holds no flash intelligence —
**OpenOCD's exit code is the verdict** (0 = success).

The board's flash algorithm comes from OpenOCD's ``target=`` config (e.g.
``target/stm32f4x.cfg``), so every OpenOCD-supported MCU works unchanged.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import IO, List, Optional, Sequence

from .errors import FlashError, TargetUnreachableError
from .transport.base import RawLink, Transport

TARGET_POWER_SETTLE = 0.25  # seconds for the target to boot after power-on

# OpenOCD stderr substrings that mean the probe worked but no target answered.
_SWD_CONNECT_MARKERS = (b"cannot read IDR", b"Error connecting DP")

# --- CMSIS-DAP-over-TCP framing (must match the firmware's PROTO_DAP frame and
#     OpenOCD's cmsis_dap_tcp backend; mirrors the Go CLI's internal/openocd/dap.go).
_DAP_TCP_SIG = b"\x44\x41\x50\x00"  # "DAP\0" little-endian u32 packet signature
_DAP_TCP_HDR = 8                    # signature(4) + len(2) + type(1) + reserved(1)
_DAP_PKT_RESPONSE = 0x02            # packet_type OpenOCD expects on device->host frames
_DAP_MAX_PACKET = 256               # mirrors DAP_PACKET_SIZE in the firmware (dap.h)

# Empty the target cfg's clock-boost reset events. The just-after-reset PLL/clock
# writes (`adapter speed`, reset-init) race the still-coming-up core over the SWD
# link; cleared, `program` does a clean reset-halt at the default reset clock.
# Copied verbatim from the Go CLI.
_CLEAR_RESET_EVENTS_TCL = """foreach __t [target names] {
    foreach __ev {reset-start reset-init} {
        if {[catch {$__t cget -event $__ev}]} { continue }
        $__t configure -event $__ev {}
    }
}"""


@dataclass
class FlashResult:
    """Outcome of a flash run."""

    ok: bool
    returncode: int
    stdout: str
    stderr: str
    target_unreachable: bool = False
    stalled: bool = False  # bridge saw no frame traffic for stall_timeout


def find_openocd(explicit: Optional[str] = None) -> str:
    """Locate the OpenOCD binary, raising an actionable error if missing."""
    if explicit:
        return explicit
    found = shutil.which("openocd")
    if not found:
        raise FlashError(
            "openocd not found in PATH; install it first "
            "(e.g. `brew install open-ocd` on macOS, `apt install openocd` on Debian/Ubuntu)"
        )
    return found


def supports_cmsis_dap_tcp(openocd_bin: str) -> bool:
    """Report whether ``openocd_bin`` has the ``cmsis_dap_tcp`` backend (added
    post-0.12.0). Runs a config-stage probe that shuts down before init, so no
    hardware is touched: a clean exit means the backend exists."""
    try:
        proc = subprocess.run(
            [openocd_bin,
             "-c", "adapter driver cmsis-dap",
             "-c", "cmsis-dap backend tcp",
             "-c", "cmsis-dap tcp port 4441",
             "-c", "shutdown"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
        return proc.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def build_openocd_args(
    target: str,
    file: str,
    load_address: str,
    *,
    verify: bool,
    reset: bool,
    connect_under_reset: bool,
    clear_reset_events: bool,
    extra_configs: Sequence[str],
    extra_args: Sequence[str],
) -> List[str]:
    """Assemble OpenOCD args (excluding the cmsis-dap adapter prefix, which
    :func:`_run_bridge` prepends)."""
    target = target.strip()
    file = file.strip()
    if not target and not extra_configs and not extra_args:
        raise FlashError(
            "nothing to flash; pass target= (with file=) or extra config/args"
        )

    args: List[str] = []
    if target:
        args += ["-f", target]
    if connect_under_reset:
        args += ["-c", "reset_config srst_only srst_nogate connect_assert_srst"]
    if target and clear_reset_events:
        args += ["-c", _CLEAR_RESET_EVENTS_TCL]
    if file:
        prog = ["program", file]
        if load_address.strip():
            prog.append(load_address.strip())
        if verify:
            prog.append("verify")
        if reset:
            prog.append("reset")
        prog.append("exit")
        args += ["-c", " ".join(prog)]
    args += list(extra_args)
    for c in extra_configs:
        args += ["-c", c]
    return args


def flash(
    transport: Transport,
    *,
    swclk: int,
    swdio: int,
    nreset: Optional[int] = None,
    target: str = "",
    file: str = "",
    load_address: str = "",
    target_power: Optional[int] = None,
    verify: bool = True,
    reset: bool = True,
    connect_under_reset: Optional[bool] = None,
    clear_reset_events: bool = True,
    openocd_bin: Optional[str] = None,
    extra_configs: Sequence[str] = (),
    extra_args: Sequence[str] = (),
    timeout: float = 300.0,
    connect_attempts: int = 5,
) -> FlashResult:
    """Arm the CMSIS-DAP probe and run OpenOCD. Returns a :class:`FlashResult`.

    Pins are LA channels 1-12 (already coerced by the caller). ``target_power``
    of 1/2 enables that eFuse first; ``None`` leaves power untouched.
    ``connect_under_reset`` defaults to True when ``nreset`` is wired.

    The SWD link can intermittently fail the initial debug-port read (``cannot
    read IDR``); a connect failure fails fast, so ``connect_attempts`` retries the
    whole arm+OpenOCD run on a target-unreachable result.

    Needs an OpenOCD build with the ``cmsis_dap_tcp`` backend (post-0.12.0; e.g.
    ``brew install --HEAD open-ocd``).
    """
    if file and not target:
        raise FlashError("file= requires target=")
    if connect_under_reset is None:
        connect_under_reset = nreset is not None
    connect_under_reset = bool(connect_under_reset) and nreset is not None

    bin_path = find_openocd(openocd_bin)
    if not supports_cmsis_dap_tcp(bin_path):
        raise FlashError(
            f"this OpenOCD ({bin_path}) lacks the cmsis-dap TCP backend; update "
            "OpenOCD (e.g. `brew install --HEAD open-ocd`, or build a recent "
            "version with the cmsis_dap_tcp backend)"
        )
    args = build_openocd_args(
        target, file, load_address,
        verify=verify, reset=reset,
        connect_under_reset=connect_under_reset,
        clear_reset_events=clear_reset_events,
        extra_configs=extra_configs, extra_args=extra_args,
    )

    if target_power is not None:
        transport.target_power(target_power, True)
        time.sleep(TARGET_POWER_SETTLE)

    attempts = max(1, connect_attempts)
    result: Optional[FlashResult] = None
    for attempt in range(attempts):
        pod_link = transport.dap_start(swclk, swdio, nreset)
        try:
            result = _run_bridge(bin_path, args, pod_link, timeout=timeout)
        finally:
            pod_link.close()
        # Retry the transient, transport-level failures (no target on the wire,
        # or a wedged link) — both are cleared by a fresh arm. A flash that
        # genuinely ran and errored is not retried.
        transient = result.target_unreachable or result.stalled
        if result.ok or not transient or attempt == attempts - 1:
            return result
        time.sleep(0.5)
    return result  # unreachable; for type-checkers


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    """Read exactly ``n`` bytes from a socket, or None on EOF/close."""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _read_exact(link: RawLink, n: int) -> Optional[bytes]:
    """Read exactly ``n`` bytes from a pod RawLink, or None on EOF/close."""
    buf = bytearray()
    while len(buf) < n:
        chunk = link.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _run_bridge(
    openocd_bin: str,
    args: Sequence[str],
    pod_link: RawLink,
    *,
    timeout: float,
    stall_timeout: float = 60.0,
) -> FlashResult:
    """Stand up a loopback listener, run OpenOCD's cmsis-dap TCP backend against
    it, and translate the per-packet framing both ways until OpenOCD exits."""
    env_stall = os.environ.get("BENCHPOD_STALL_TIMEOUT")
    if env_stall:
        stall_timeout = float(env_stall)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    full_args = [
        openocd_bin,
        "-c", "adapter driver cmsis-dap",
        "-c", "cmsis-dap backend tcp",
        "-c", "cmsis-dap tcp host 127.0.0.1",
        "-c", f"cmsis-dap tcp port {port}",
        "-c", "transport select swd",
        *args,
    ]

    proc = subprocess.Popen(
        full_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    stdout_chunks: List[bytes] = []
    stderr_chunks: List[bytes] = []
    target_unreachable = threading.Event()

    def drain(stream: IO[bytes], sink: List[bytes], scan: bool) -> None:
        for chunk in iter(lambda: stream.read(4096), b""):
            sink.append(chunk)
            if scan and any(m in chunk for m in _SWD_CONNECT_MARKERS):
                target_unreachable.set()

    t_out = threading.Thread(target=drain, args=(proc.stdout, stdout_chunks, False), daemon=True)
    t_err = threading.Thread(target=drain, args=(proc.stderr, stderr_chunks, True), daemon=True)
    t_out.start()
    t_err.start()

    # Accept OpenOCD's single connection, but bail out if it exits first.
    listener.settimeout(0.2)
    oc_conn: Optional[socket.socket] = None
    while True:
        try:
            oc_conn, _ = listener.accept()
            break
        except socket.timeout:
            if proc.poll() is not None:
                break
    listener.close()

    if oc_conn is None:
        rc = proc.wait()
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        return FlashResult(
            ok=False, returncode=rc if rc is not None else -1,
            stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
            stderr=stderr + "\nopenocd exited before connecting to the bridge",
            target_unreachable=target_unreachable.is_set(),
        )

    oc_conn.settimeout(None)
    # Disable Nagle on the loopback socket: each DAP command is a request/response
    # round-trip, so Nagle + delayed-ACK would add latency to every one.
    try:
        oc_conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    openocd_exited = threading.Event()
    pod_dropped = threading.Event()
    stalled = threading.Event()

    diag = {"oc2pod": 0, "pod2oc": 0, "last_activity": time.monotonic()}

    def oc_to_pod() -> None:
        # OpenOCD cmsis_dap_tcp request frames -> the pod's 2-byte length frames.
        try:
            while True:
                hdr = _recv_exact(oc_conn, _DAP_TCP_HDR)
                if hdr is None:
                    break
                if hdr[0:4] != _DAP_TCP_SIG:
                    break  # bad signature; treat as a fatal stream error
                n = hdr[4] | (hdr[5] << 8)
                payload = _recv_exact(oc_conn, n) if n else b""
                if payload is None:
                    break
                pod_link.write(bytes((n & 0xFF, (n >> 8) & 0xFF)) + payload)
                diag["oc2pod"] += n
                diag["last_activity"] = time.monotonic()
        except OSError:
            pass

    def pod_to_oc() -> None:
        # The pod's 2-byte length frames -> OpenOCD cmsis_dap_tcp response frames.
        try:
            while True:
                lenhdr = _read_exact(pod_link, 2)
                if lenhdr is None:
                    break
                n = lenhdr[0] | (lenhdr[1] << 8)
                payload = _read_exact(pod_link, n) if n else b""
                if payload is None:
                    break
                out = (_DAP_TCP_SIG
                       + bytes((n & 0xFF, (n >> 8) & 0xFF, _DAP_PKT_RESPONSE, 0x00))
                       + payload)
                oc_conn.sendall(out)
                diag["pod2oc"] += n
                diag["last_activity"] = time.monotonic()
        except OSError:
            pass
        # Pod link ended. If OpenOCD is still running the link dropped first —
        # kill OpenOCD rather than let it spin on a dead socket.
        if not openocd_exited.is_set():
            pod_dropped.set()
            try:
                proc.terminate()
            except OSError:
                pass

    def stall_monitor() -> None:
        # If no frame moves in either direction for stall_timeout, the SWD link
        # has wedged; kill OpenOCD so the flash fails fast and the caller can
        # retry instead of hanging out the full `timeout`.
        while not openocd_exited.wait(1.0):
            if time.monotonic() - diag["last_activity"] > stall_timeout:
                stalled.set()
                try:
                    proc.kill()
                except OSError:
                    pass
                return

    p1 = threading.Thread(target=oc_to_pod, daemon=True)
    p2 = threading.Thread(target=pod_to_oc, daemon=True)
    pm = threading.Thread(target=stall_monitor, daemon=True)
    p1.start()
    p2.start()
    pm.start()

    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = proc.wait()
    openocd_exited.set()
    pm.join(timeout=2)

    # Unblock the pump threads.
    try:
        oc_conn.close()
    except OSError:
        pass
    pod_link.close()
    p1.join(timeout=2)
    p2.join(timeout=2)
    t_out.join(timeout=2)
    t_err.join(timeout=2)

    stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    stderr += ("\n[bridge] oc->pod=%d bytes  pod->oc=%d bytes"
               % (diag["oc2pod"], diag["pod2oc"]))
    if pod_dropped.is_set():
        stderr += "\npod connection closed mid-flash: the SWD link dropped before OpenOCD finished"
    if stalled.is_set():
        stderr += ("\nSWD link stalled (no frames for %.0fs); retrying usually clears it"
                   % stall_timeout)
    return FlashResult(
        ok=(rc == 0),
        returncode=rc,
        stdout=stdout,
        stderr=stderr,
        target_unreachable=target_unreachable.is_set(),
        stalled=stalled.is_set(),
    )


def raise_for_result(result: FlashResult, *, hint: str = "") -> None:
    """Raise the right error for a failed flash, or return on success."""
    if result.ok:
        return
    detail = (result.stderr or result.stdout).strip()
    suffix = f"\n{hint}" if hint else ""
    if result.target_unreachable:
        raise TargetUnreachableError(
            "SWD target did not respond (could not read the debug port IDCODE). "
            "Check power, wiring (SWCLK/SWDIO not swapped, common ground), and "
            "whether the running firmware disables SWD (wire NRST + pass nreset)."
            + suffix + ("\n" + detail if detail else "")
        )
    raise FlashError(
        f"flash failed (openocd exit {result.returncode})" + suffix
        + ("\n" + detail if detail else "")
    )
