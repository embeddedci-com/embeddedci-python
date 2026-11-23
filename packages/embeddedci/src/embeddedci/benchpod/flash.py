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
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import IO, Any, Dict, List, Optional, Sequence

from .errors import FlashError, TargetUnreachableError
from .transport.base import RawLink, Transport

TARGET_POWER_SETTLE = 0.25  # seconds for the target to boot after power-on
# After switching the target's rail on, the pod also waits (up to this long) for the target to
# answer SWD before handing over: an STM32 NUCLEO needs ~2.1 s (its ST-LINK boots first), which a
# fixed settle cannot cover for every board.  Firmware that predates it ignores the field.
TARGET_ANSWER_WAIT_MS = 5000

# OpenOCD stderr substrings that mean the probe worked but no target answered.
_SWD_CONNECT_MARKERS = (b"cannot read IDR", b"Error connecting DP",
                        # JTAG: an empty or floating chain
                        b"scan chain interrogation failed")

# --- CMSIS-DAP-over-TCP framing (must match the firmware's PROTO_DAP frame and
#     OpenOCD's cmsis_dap_tcp backend; mirrors the Go CLI's internal/openocd/dap.go).
_DAP_TCP_SIG = b"\x44\x41\x50\x00"  # "DAP\0" little-endian u32 packet signature
_DAP_TCP_HDR = 8                    # signature(4) + len(2) + type(1) + reserved(1)
_DAP_PKT_RESPONSE = 0x02            # packet_type OpenOCD expects on device->host frames
# CMSIS-DAP packets the pod is asked to advertise (dap.h DAP_PACKET_SIZE / DAP_PACKET_COUNT_MAX).
# Each packet is a host + network round trip, so bigger packets and several in flight cut the
# waiting between them; firmware that predates the fields keeps 256 x 1.
_DAP_MAX_PACKET = 1024
_DAP_PACKET_COUNT = 4

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
    #: The uploaded build that was flashed (``job_id``, ``name``, ``repo``, ``ref``, ``sha``, …) when
    #: the image came from embeddedci.com (``flash(build=...)``); None for a local file.
    artifact: Optional[Dict[str, Any]] = None


def find_openocd(explicit: Optional[str] = None) -> str:
    """Locate the OpenOCD binary, raising an actionable error if missing."""
    if explicit:
        return explicit
    found = shutil.which("openocd")
    if not found:
        raise FlashError(
            "openocd not found in PATH; install an OpenOCD with the cmsis_dap_tcp backend (newer "
            "than 0.12.0 — e.g. `brew install --HEAD open-ocd`, the xPack build, or from source)"
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


_LOAD_ADDR_RE = re.compile(r"^(0[xX][0-9a-fA-F]{1,8}|[0-9]{1,10})$")


def _tcl_word(path: str) -> str:
    """``path`` as one Tcl word with no substitution: in braces, which a path cannot break out of
    unless it holds a brace (refused). Backslashes (Windows) become forward slashes, which OpenOCD
    takes on Windows too, so none can escape a brace."""
    path = path.replace("\\", "/")
    if any(c in path for c in "{}\n\r"):
        raise ValueError(f"the firmware path has characters OpenOCD would read as commands: {path!r}")
    return "{" + path + "}"


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
        raise ValueError(
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
        # The program command is Tcl: OpenOCD would run "[...]" in a file name or address as a
        # command. The path goes in braces (no substitution inside) and the address must be a number.
        prog = ["program", _tcl_word(file)]
        if load_address.strip():
            addr = load_address.strip()
            if not _LOAD_ADDR_RE.match(addr):
                raise ValueError(f"load_address must be a number like 0x08000000, not {load_address!r}")
            prog.append(addr)
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


#: OpenOCD targets whose debug port is JTAG only (no SWD): the flash path picks JTAG for them
#: whenever the wiring has JTAG pins. One place; a name is the target config's file name without
#: ``target/`` and ``.cfg`` (``target/esp32.cfg`` -> ``esp32``). Any name starting with
#: :data:`JTAG_ONLY_PREFIXES` counts too.
JTAG_ONLY_TARGETS = frozenset({"esp32", "esp32s2", "gd32vf103", "fe310", "k210"})
JTAG_ONLY_PREFIXES = ("riscv",)


def target_is_jtag_only(target: str) -> bool:
    """Whether the OpenOCD ``target`` config (``target/esp32.cfg``, ``esp32s2``) has JTAG only."""
    name = str(target).strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(".cfg"):
        name = name[:-4]
    return name in JTAG_ONLY_TARGETS or name.startswith(JTAG_ONLY_PREFIXES)


def select_port(*, swd: bool, jtag: bool, target: str = "") -> str:
    """``"jtag"`` or ``"swd"`` for a flash. JTAG when all four JTAG pins are available (``jtag``)
    and either no SWD pins are (``swd`` False) or the target is JTAG only; otherwise SWD. With
    neither, raises :class:`ValueError`."""
    if not swd and not jtag:
        raise ValueError("flash needs SWD pins (swclk, swdio) or JTAG pins (tck, tms, tdi, tdo): "
                         "pass them or set them in the wiring profile")
    return "jtag" if jtag and (not swd or target_is_jtag_only(target)) else "swd"


def flash(
    transport: Transport,
    *,
    swclk: Optional[int] = None,
    swdio: Optional[int] = None,
    tck: Optional[int] = None,
    tms: Optional[int] = None,
    tdi: Optional[int] = None,
    tdo: Optional[int] = None,
    trst: Optional[int] = None,
    port: str = "swd",
    nreset: bool = False,
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

    ``swclk``/``swdio`` are LA channels 1-14 (already coerced by the caller). ``port="jtag"``
    arms the probe with ``tck``/``tms``/``tdi``/``tdo`` (and ``trst``) and has OpenOCD use JTAG
    (``transport select jtag``); the SWD pins may be given too. The caller chooses the port
    (:func:`select_port`).
    ``target_power`` of 1/2 enables that eFuse first; ``None`` leaves power
    untouched.

    ``nreset`` says whether the target's reset line is wired to the pod's reset
    pin (DUT header J1 pin 22). It is a flag, not a channel: pods have driven
    NRST from that dedicated pin since rev3, so nothing has to be routed to an LA
    channel any more. ``connect_under_reset`` defaults to True when ``nreset`` is
    set, and is forced off when it is not — holding a reset the pod cannot drive
    would only stall the connect.

    The SWD link can intermittently fail the initial debug-port read (``cannot
    read IDR``); a connect failure fails fast, so ``connect_attempts`` retries the
    whole arm+OpenOCD run on a target-unreachable result.

    Needs an OpenOCD build with the ``cmsis_dap_tcp`` backend (post-0.12.0; e.g.
    ``brew install --HEAD open-ocd``).
    """
    if file and not target:
        raise ValueError("file= requires target=")
    if port not in ("swd", "jtag"):
        raise ValueError(f"port must be 'swd' or 'jtag', not {port!r}")
    if port == "jtag" and None in (tck, tms, tdi, tdo):
        raise ValueError("JTAG needs tck, tms, tdi and tdo")
    if port == "swd" and (swclk is None or swdio is None):
        raise ValueError("SWD needs swclk and swdio")
    nreset = bool(nreset)
    if connect_under_reset is None:
        connect_under_reset = nreset
    connect_under_reset = bool(connect_under_reset) and nreset

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
        pins: dict = {}
        if port == "jtag":
            pins = {"tck": tck, "tms": tms, "tdi": tdi, "tdo": tdo, "port": "jtag"}
            if trst is not None:
                pins["trst"] = trst
        pod_link = transport.dap_start(
            swclk, swdio, packet_size=_DAP_MAX_PACKET, packet_count=_DAP_PACKET_COUNT, **pins,
            # Every attempt, not just the first: a retry after a target that answered too early
            # (a NUCLEO's ST-LINK still holding it) must wait for it to settle as well.
            wait_ms=TARGET_ANSWER_WAIT_MS if target_power is not None else None)
        try:
            result = _run_bridge(bin_path, args, pod_link, timeout=timeout, transport=port)
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
    transport: str = "swd",
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
        "-c", f"transport select {transport}",
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
            "whether the running firmware disables SWD (wire the target's NRST to "
            "the pod's reset pin, DUT header J1 pin 22, and pass nreset=True)."
            + suffix + ("\n" + detail if detail else "")
        )
    raise FlashError(
        f"flash failed (openocd exit {result.returncode})" + suffix
        + ("\n" + detail if detail else "")
    )
