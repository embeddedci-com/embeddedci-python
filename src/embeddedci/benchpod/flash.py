"""Flash an SWD target by driving host OpenOCD over the pod's remote_bitbang.

Pure-Python port of the Go ``runFlash``/``openSWD``/``Bridge`` flow: arm the
pod's SWD probe (``swd_start``), spawn ``openocd`` pointed at a loopback bridge,
and pipe remote_bitbang bytes both ways. The pod holds no flash intelligence —
**OpenOCD's exit code is the verdict** (0 = success).
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

# Empty the target cfg's clock-boost reset events. Over the slow bit-banged link
# the cfg's `adapter speed`/PLL writes glitch flashing; cleared, `program` does a
# clean reset-halt at the default reset clock. Copied verbatim from the Go CLI.
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
    stalled: bool = False  # bridge saw no byte traffic for stall_timeout


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
    """Assemble OpenOCD args (excluding the remote_bitbang adapter prefix)."""
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
    """Arm the probe and run OpenOCD. Returns a :class:`FlashResult`.

    Pins are LA channels 1-12 (already coerced by the caller). ``target_power``
    of 1/2 enables that eFuse first; ``None`` leaves power untouched.
    ``connect_under_reset`` defaults to True when ``nreset`` is wired.

    The bit-banged SWD link can intermittently fail the initial debug-port read
    (``cannot read IDR``); a connect failure fails fast, so ``connect_attempts``
    retries the whole arm+OpenOCD run on a target-unreachable result.
    """
    if file and not target:
        raise FlashError("file= requires target=")
    if connect_under_reset is None:
        connect_under_reset = nreset is not None
    connect_under_reset = bool(connect_under_reset) and nreset is not None

    bin_path = find_openocd(openocd_bin)
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
        pod_link = transport.swd_start(swclk, swdio, nreset)
        try:
            result = _run_bridge(bin_path, args, pod_link, timeout=timeout)
        finally:
            pod_link.close()
        # Retry the transient, transport-level failures (no target on the wire,
        # or a wedged link from a dropped serial byte) — both are cleared by a
        # fresh arm. A flash that genuinely ran and errored is not retried.
        transient = result.target_unreachable or result.stalled
        if result.ok or not transient or attempt == attempts - 1:
            return result
        time.sleep(0.5)
    return result  # unreachable; for type-checkers


def _run_bridge(
    openocd_bin: str,
    args: Sequence[str],
    pod_link: RawLink,
    *,
    timeout: float,
    stall_timeout: float = 60.0,
) -> FlashResult:
    """Stand up a loopback listener, run OpenOCD against it, pump bytes both ways."""
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
        "-c", "adapter driver remote_bitbang",
        "-c", "remote_bitbang host 127.0.0.1",
        "-c", f"remote_bitbang port {port}",
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
    # CRITICAL for speed: disable Nagle on the loopback socket to OpenOCD. SWD
    # remote_bitbang is request/response one byte at a time, so without
    # TCP_NODELAY every sample round-trip pays ~40ms of Nagle + delayed-ACK on
    # the loopback — turning a sub-minute flash into 20+ minutes.
    try:
        oc_conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    openocd_exited = threading.Event()
    pod_dropped = threading.Event()
    stalled = threading.Event()

    # Off by default: stripping a "[...]" run that lacks a trailing newline can
    # get stuck dropping real reply bytes. With the firmware fixes (USB stdout
    # non-blocking, watchdog gated during console SWD) the pod no longer injects
    # logs into the raw stream, so the filter is unnecessary. Opt in for debug.
    filter_enabled = os.environ.get("BENCHPOD_LOG_FILTER") == "1"
    diag = {"oc2pod": 0, "pod_in": 0, "pod_out": 0, "last_pod": b"", "last_oc": b"",
            "last_activity": time.monotonic()}

    def oc_to_pod() -> None:
        try:
            for data in iter(lambda: oc_conn.recv(4096), b""):
                diag["oc2pod"] += len(data)
                diag["last_oc"] = data[-32:]
                diag["last_activity"] = time.monotonic()
                pod_link.write(data)
        except OSError:
            pass

    progress_path = os.environ.get("BENCHPOD_BRIDGE_PROGRESS")
    progress_start = time.monotonic()
    progress_last = {"oc2pod": 0, "pod_in": 0, "t": progress_start}

    def stall_monitor() -> None:
        # If no bytes move in either direction for stall_timeout, the SWD link
        # has wedged (e.g. an occasional dropped reply byte on a flaky serial
        # link leaves OpenOCD blocked). Kill OpenOCD so the flash fails fast and
        # the caller can retry, instead of hanging out the full `timeout`.
        while not openocd_exited.wait(1.0):
            if progress_path:
                now = time.monotonic()
                if now - progress_last["t"] >= 2.0:
                    d_oc = diag["oc2pod"] - progress_last["oc2pod"]
                    d_pod = diag["pod_in"] - progress_last["pod_in"]
                    quiet = now - diag["last_activity"]
                    try:
                        with open(progress_path, "a") as _pf:
                            _pf.write("t=%5.1f  oc->pod=%8d (+%5d)  pod->oc=%7d (+%4d)  quiet=%4.1fs\n"
                                      % (now - progress_start, diag["oc2pod"], d_oc,
                                         diag["pod_in"], d_pod, quiet))
                    except OSError:
                        pass
                    progress_last.update(oc2pod=diag["oc2pod"], pod_in=diag["pod_in"], t=now)
            if time.monotonic() - diag["last_activity"] > stall_timeout:
                stalled.set()
                # SIGKILL: OpenOCD is blocked reading a reply that will never
                # come; don't wait on SIGTERM.
                try:
                    proc.kill()
                except OSError:
                    pass
                return

    # The pod's remote_bitbang reply stream is ONLY '0'/'1' sample bytes. Over
    # the serial transport the pod also multiplexes firmware debug logs onto the
    # same link, and a stray "[...]\n" line injected mid-flash corrupts OpenOCD's
    # reads ("invalid read response"). Strip any '['-to-newline run from the
    # pod->OpenOCD direction; bitbang replies never contain '[' or '\n', and over
    # TCP (debug on a separate channel) this never matches, so it is a safe no-op.
    _filter_state = {"dropping": False}
    stripped_logs = bytearray()  # captured "[...]" debug lines for diagnosis

    def _strip_logs(data: bytes) -> bytes:
        if not filter_enabled:
            return data
        out = bytearray()
        dropping = _filter_state["dropping"]
        for b in data:
            if dropping:
                stripped_logs.append(b)
                if b == 0x0A:  # '\n' ends the debug line
                    dropping = False
            elif b == 0x5B:  # '[' starts a "[subsystem] ..." log line
                dropping = True
                stripped_logs.append(b)
            else:
                out.append(b)
        _filter_state["dropping"] = dropping
        return bytes(out)

    def pod_to_oc() -> None:
        try:
            while True:
                data = pod_link.read(4096)
                if not data:
                    break
                diag["pod_in"] += len(data)
                diag["last_pod"] = data[-32:]
                diag["last_activity"] = time.monotonic()
                data = _strip_logs(data)
                diag["pod_out"] += len(data)
                if data:
                    oc_conn.sendall(data)
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
    stderr += ("\n[bridge] oc->pod=%d pod->oc in=%d out=%d filter=%s drop_stuck=%s"
               "\n[bridge] last pod->oc bytes: %r\n[bridge] last oc->pod bytes: %r"
               % (diag["oc2pod"], diag["pod_in"], diag["pod_out"],
                  filter_enabled, _filter_state["dropping"],
                  bytes(diag["last_pod"]), bytes(diag["last_oc"])))
    if stripped_logs:
        stderr += ("\n[bridge] stripped pod debug-log bytes injected into the SWD "
                   "stream (these are the printf()s to silence during swd mode):\n"
                   + stripped_logs.decode("utf-8", errors="replace"))
    if pod_dropped.is_set():
        stderr += "\npod connection closed mid-flash: the SWD link dropped before OpenOCD finished"
    if stalled.is_set():
        stderr += ("\nSWD link stalled (no bytes for %.0fs) — likely a dropped byte on "
                   "the serial link; retrying usually clears it" % stall_timeout)
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
