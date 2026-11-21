"""USB serial-console transport.

Mirrors the Go ``serialconsole``: the firmware exposes a line-oriented text
console over USB CDC-ACM. Commands are echoed and each reply ends with a
``"> "`` prompt. ``dap-start`` switches the port to a raw length-framed
CMSIS-DAP stream until the quit byte is sent.

``pyserial`` is imported lazily so the TCP-only path has no hard dependency on
it at import time.
"""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..connection import normalize_pod_name
from ..errors import (
    BenchPodError,
    ConnectionClosedError,
    ConnectionConfigError,
    FirmwareError,
    TransportError,
    TransportTimeout,
)
from .base import RawLink, Transport

# USB vendor ids a bench-pod console enumerates under: 0x2E8A (Raspberry Pi,
# the RP2350 pod), 0x0483 (STMicroelectronics: the STM32H563 pod, which uses ST's
# stock CDC VID/PID 0483:5740 on every firmware, from 3.9.0 as a composite device
# with the CDC-ACM console plus a CDC-NCM network adapter) and 0x1209 (pre-release
# 3.9.0 builds, under the pid.codes test id 1209:0001).
#
# None of these ids is exclusive to a bench pod (0x2E8A is shared with the
# CMSIS-DAP probe's CDC, 0483:5740 with every ST Virtual COM Port on the bench
# and 0x1209 is every pid.codes device), so they only ORDER the ports to probe.
# Identification is by probing for BENCHPOD_MARKER, never by vendor id alone.
POD_VIDS = (0x2E8A, 0x0483, 0x1209)

# The substring the firmware's ``status`` prints to identify itself
# ("device : benchpod").
BENCHPOD_MARKER = "benchpod"

# Per-port probe budget. A real pod answers ``status`` in well under a second;
# only ports that are not pods run this out.
PROBE_TIMEOUT = 2.0
BAUD = 115200
PROMPT = "> "
# The prompt followed only by complete firmware log lines ("[sig] ...\r\n"): see _read_until_prompt.
_PROMPT_THEN_LOG = re.compile(r"(?:^|\n)>[ \t]*(?P<log>(?:\[[A-Za-z0-9_.:-]+\][^\r\n]*\r?\n)+)\Z")
_CLEAR_LINE = b"\x08" * 128  # backspaces to clear any partial input line
_RAW_READ_TIMEOUT = 0.1  # poll interval while bridging raw CMSIS-DAP bytes
#: JSON commands the text console answers through its text equivalents (it has no JSON mode):
#: firmware 4.0's console keeps status, ping and identity; everything else is on the JSON API
#: (the LAN, the USB network adapter or the cloud).
TEXT_CONSOLE_COMMANDS = ("status", "ping", "lan_clients", "lan_tls")


@contextmanager
def _port_errors(what: str) -> Iterator[None]:
    """Turn a pyserial failure (an ``OSError``: the pod was unplugged or reset) into a
    :class:`ConnectionClosedError`. SDK errors pass through unchanged."""
    try:
        yield
    except BenchPodError:
        raise
    except OSError as exc:
        raise ConnectionClosedError(f"{what}: the USB serial link failed ({exc})") from exc


def parse_text_status(raw: str) -> Dict[str, Any]:
    """Turn the console's ``status`` report (``  key : value`` lines) into a dict.

    Every line lands under its own key; the fields the rest of the SDK reads from a JSON status
    (``board``, ``version``, ``board_rev``, ``adc_bits``, ``adc_fullscale_mv``) are derived too.
    """
    out: Dict[str, Any] = {"console": "text"}
    for line in raw.replace("\r", "\n").split("\n"):
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or not key or " " in key or key.startswith("["):
            continue
        out[key] = value.strip()
    board = str(out.get("board", ""))
    if board:
        out["board"] = board.split()[0]
        if m := re.search(r"\bfw v(\S+)", board):
            out["version"] = m.group(1)
        if m := re.search(r"\brev (\S+)", board):
            out["board_rev"] = m.group(1)
        if m := re.search(r"\bnrst_pin=(\w+)", board):
            out["nrst_pin"] = m.group(1) == "yes"
        if m := re.search(r"\busb_cc=(\w+)", board):
            out["usb_cc"] = m.group(1) == "yes"
    adc = str(out.get("adc", ""))
    if m := re.search(r"(\d+)-bit", adc):
        out["adc_bits"] = int(m.group(1))
    if m := re.search(r"(\d+) mV", adc):
        out["adc_fullscale_mv"] = int(m.group(1))
    if m := re.search(r"gateware v(\d+)", str(out.get("fpga", ""))):
        out["gateware"] = int(m.group(1))
    # Boot health under the JSON status keys: "safe : safe mode: <reason>", "crash : <line>".
    safe = str(out.get("safe", ""))
    out["safe_mode"] = bool(safe)
    out["safe_reason"] = safe[len("safe mode:"):].strip() if safe.startswith("safe mode:") else safe
    if "crash" in out:
        out["last_crash"] = out["crash"]
    # The USB network adapter (firmware 3.9.0+), under the JSON status's "usb" shape:
    # "usb : net on pod 10.237.8.85 host 10.237.8.86 link up rx 2946 (drop 0) tx ..."
    usb = parse_usb_status_line(str(out.get("usb", "")))
    if usb is not None:
        out["usb"] = usb
    return out


_IPV4 = r"(\d{1,3}(?:\.\d{1,3}){3})"


def parse_usb_status_line(value: str) -> Optional[Dict[str, Any]]:
    """Parse the value of the console status's ``usb`` line into the JSON status's ``usb`` dict
    (``{"net": bool, "link": bool, "ip": ..., "host_ip": ...}``), or None when the line does not
    describe the USB network adapter."""
    m = re.search(r"\bnet (on|off)\b", value)
    if not m:
        return None
    usb: Dict[str, Any] = {"net": m.group(1) == "on"}
    link = re.search(r"\blink (up|down)\b", value)
    usb["link"] = bool(link and link.group(1) == "up")
    if ip := re.search(r"\bpod " + _IPV4, value):
        usb["ip"] = ip.group(1)
    if host := re.search(r"\bhost " + _IPV4, value):
        usb["host_ip"] = host.group(1)
    return usb


def _import_serial():
    try:
        import serial  # type: ignore
        import serial.tools.list_ports as list_ports  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only without pyserial
        raise TransportError(
            "the serial transport requires pyserial; install it with "
            "`pip install pyserial` (or `pip install embeddedci[serial]`)"
        ) from exc
    return serial, list_ports


def _candidate_ports() -> List[str]:
    """USB serial ports to probe, most likely to be a pod first (see :func:`_candidate_ports_vid`)."""
    return [device for device, _ in _candidate_ports_vid()]


def _candidate_ports_vid() -> List[Tuple[str, Optional[int]]]:
    """USB serial ports to probe with each one's USB vendor id (None when the enumerator does not
    list it, as for macOS's ``/dev/tty.*`` twin of a ``/dev/cu.*`` port), most likely to be a pod
    first.

    Ordering, not filtering: a pod is identified by probing (see
    :func:`_probe_pod`), so an adapter this ranking puts last is still tried.
    Ranking by vendor id alone is not enough — both pod vendor ids are shared
    with other hardware — so a product/manufacturer string that names the pod
    outranks a bare vendor-id match.
    """
    _, list_ports = _import_serial()

    def rank(p: Any) -> int:
        text = " ".join(str(getattr(p, attr, "") or "") for attr in ("product", "manufacturer", "description"))
        if "bench-pod" in text.lower() or "benchpod" in text.lower() or "embeddedci" in text.lower():
            return 0
        if p.vid in POD_VIDS:
            return 1
        return 2

    ports = [p for p in list_ports.comports() if getattr(p, "device", None)]
    ports.sort(key=lambda p: (rank(p), p.device))
    return [(p.device, getattr(p, "vid", None)) for p in ports]


_IDENTITY_RE = re.compile(r"\bidentity[ \t]+(\S+)")


def parse_identity(text: str) -> str:
    """The pod's identity short id (``"baea06"``) from the console's ``identity`` reply
    (``identity <shortid> <pubkey-base64url>``), or ``""`` when the pod has none
    (``identity unknown <why>``) or the firmware has no ``identity`` command."""
    for line in text.replace("\r", "\n").split("\n"):
        m = _IDENTITY_RE.search(line)
        if m and m.group(1).lower() != "unknown":
            return m.group(1).lower()
    return ""


def _read_to_prompt(port: Any, deadline: float, buf: bytearray) -> bool:
    """Read ``port`` into ``buf`` until the console prompt or ``deadline``; whether it came."""
    while time.monotonic() < deadline:
        if buf.decode("utf-8", errors="replace").rstrip(" \t").endswith(">"):
            return True
        chunk = port.read(256)
        if chunk:
            buf.extend(chunk)
    return buf.decode("utf-8", errors="replace").rstrip(" \t").endswith(">")


def _probe_pod(device: str, timeout: float = PROBE_TIMEOUT) -> Optional[str]:
    """Open ``device``, run ``status`` and then ``identity``, and close it again.

    Returns None when no pod answered, else the pod's identity short id (``""`` when it reports
    none). Anything that goes wrong (the port is held by another process, it is not a console, it
    never prints a prompt) means "not a pod here", so every failure is swallowed and the caller
    moves on to the next candidate. ``status`` and ``identity`` each get ``timeout``.
    """
    serial_mod, _ = _import_serial()
    port = None
    try:
        port = serial_mod.Serial(device, BAUD, timeout=0.25)
        port.write(b"\r")
        time.sleep(0.03)
        port.reset_input_buffer()
        port.write(b"status\n")
        deadline = time.monotonic() + timeout
        buf = bytearray()
        is_pod = False
        while time.monotonic() < deadline:
            chunk = port.read(256)
            if chunk:
                buf.extend(chunk)
                if BENCHPOD_MARKER in buf.decode("utf-8", errors="replace").lower():
                    is_pod = True
                    break
        if not is_pod:
            return None
        try:
            _read_to_prompt(port, deadline, buf)  # let the rest of the status report drain
            port.write(b"\r")
            time.sleep(0.03)
            port.reset_input_buffer()
            port.write(b"identity\n")
            reply = bytearray()
            _read_to_prompt(port, time.monotonic() + timeout, reply)
            return parse_identity(reply.decode("utf-8", errors="replace"))
        except Exception:
            return ""  # a pod, but its name could not be read
    except Exception:
        return None
    finally:
        if port is not None:
            try:
                port.close()
            except Exception:
                pass


def _is_benchpod(device: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether a pod answers ``status`` on ``device`` (see :func:`_probe_pod`)."""
    return _probe_pod(device, timeout) is not None


def _pod_label(short_id: str) -> str:
    return f"benchpod-{short_id}" if short_id else "a pod with no identity"


def _list_found(found: List[Tuple[str, str]]) -> str:
    if not found:
        return "none"
    return ", ".join(f"{_pod_label(sid)} on {device}" for device, sid in found)


def autodetect_port(name: str = "") -> str:
    """Return the device path of the pod's USB console.

    Every USB serial port is probed (best candidates first) rather than trusting
    a vendor-id match, because the pod's vendor ids are shared with other
    hardware and differ between pod generations — an STM32 pod enumerates under
    ST's generic CDC id, so a vendor-id filter would miss it entirely.

    Several pods may be plugged in. With ``name`` (``"baea06"``, ``"benchpod-baea06"``: connection
    ``usb:<name>``) the port of the pod with that identity is returned. Without, the only pod's;
    once one pod is found, only the remaining ports with a pod's USB vendor id are probed for
    another, and two different pods raise :class:`ConnectionConfigError` listing them. The same
    identity on two ports is one pod (macOS lists each pod as ``/dev/cu.*`` and ``/dev/tty.*``).
    Every probed console is closed again; the caller opens the returned path.
    """
    want = normalize_pod_name(name)
    candidates = _candidate_ports_vid()
    if not candidates:
        raise TransportError(
            "no USB serial ports found — is the BenchPod plugged in, and does the "
            "cable carry data (many USB cables are charge-only)? "
            "Pass an explicit device path to override."
        )
    found: List[Tuple[str, str]] = []  # (device, identity short id)
    for device, vid in candidates:
        # Once one pod is found, only the ports with a pod's USB vendor id can hold another.
        if found and vid not in POD_VIDS:
            continue
        short_id = _probe_pod(device)
        if short_id is None:
            continue
        if want and short_id == want:
            return device
        if short_id and any(short_id == sid for _, sid in found):
            continue  # the same pod on its second device node
        found.append((device, short_id))
    if want:
        raise ConnectionConfigError(
            f"no pod named {want} on USB (found: {_list_found(found)})"
        )
    if len(found) == 1:
        return found[0][0]
    if len(found) > 1:
        example = next((sid for _, sid in found if sid), "a1b2c3")
        raise ConnectionConfigError(
            f"{len(found)} pods on USB ({_list_found(found)}): pick one with connection "
            f"usb:<name>, e.g. usb:benchpod-{example}"
        )
    raise TransportError(
        "no BenchPod found among the USB serial ports probed ("
        + ", ".join(device for device, _ in candidates)
        + "). Is the pod powered? A pod with no firmware never opens a console — "
        "flash it with `benchpod flash-self`. Pass an explicit device path to override."
    )


class SerialTransport(Transport):
    """Talks to the pod over the USB serial console."""

    def __init__(self, device: str = "", *, timeout: float = 30.0,
                 port: Optional[Any] = None, pod: str = "") -> None:
        # ``port`` is a test seam: pass a pyserial-like object to skip opening a
        # real device (and pyserial autodetect). ``pod`` picks the pod by name (its identity
        # short id) when ``device`` is auto-detected and several pods are plugged in.
        self.timeout = timeout
        #: The STM32 pod's USB console is a text shell: it never had a JSON mode (that was the
        #: retired RP2350 firmware). JSON goes over the network, also over USB (firmware 3.9.0+).
        self.json_supported = False
        if port is not None:
            self.device = device
            self._port = port
            return
        serial, _ = _import_serial()
        self._serial_mod = serial
        self.device = device or autodetect_port(pod)
        self._port = serial.Serial(self.device, BAUD, timeout=0.25)

    # -- console plumbing ---------------------------------------------------

    def _write_line(self, line: str) -> None:
        # Terminate any partial line the pod's editor may hold, let it drain,
        # then send the command cleanly. A long backspace clear-prefix can
        # overflow the UART RX FIFO on flow-control-less USB-serial adapters and
        # corrupt the command (e.g. "dap-start 1 2 3" -> "dp1"), so we flush
        # with a single newline instead.
        self._port.write(b"\r")
        self._port.flush()
        time.sleep(0.03)
        self._port.reset_input_buffer()
        self._port.write((line + "\n").encode("utf-8"))
        self._port.flush()

    def _read_until_prompt(self, deadline: float) -> str:
        """Accumulate output until the trailing ``"> "`` prompt or timeout.

        Firmware log lines (``[sig] SPI1 link at ...``) are printed from other tasks and can land
        right after the prompt, so the reply no longer ends with it. A prompt followed only by
        complete ``[tag] ...`` lines ends the reply too; those lines are dropped."""
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self._port.read(256)
            if chunk:
                buf.extend(chunk)
                text = buf.decode("utf-8", errors="replace")
                if text.rstrip(" \t").endswith(">"):
                    return text
                trailing = _PROMPT_THEN_LOG.search(text)
                if trailing:
                    return text[:trailing.start("log")]
        raise TransportTimeout(
            f"timed out waiting for console prompt; got: "
            f"{buf.decode('utf-8', errors='replace')!r}"
        )

    def _send_command(self, line: str) -> str:
        with _port_errors(line.split()[0] if line else "console"):
            deadline = time.monotonic() + self.timeout
            self._write_line(line)
            return self._read_until_prompt(deadline)

    @staticmethod
    def _unsupported(what: str) -> str:
        return (f"{what!r} is not on this pod's USB console, which answers status and ping "
                "only: it is a network command. Connection 'usb' reaches the full API over the "
                "pod's USB network adapter (off while Wi-Fi is configured on the pod); a network "
                "address (host[:port]) or the cloud (embeddedci:<device>) works too")

    def command(self, req: dict) -> Any:
        """Answer a JSON command through its text-console equivalent: the commands in
        :data:`TEXT_CONSOLE_COMMANDS`. Any other command raises a :class:`TransportError` naming
        the network, USB network and cloud alternatives."""
        cmd = req.get("cmd")
        with _port_errors(str(cmd)):
            if cmd not in TEXT_CONSOLE_COMMANDS:
                raise TransportError(self._unsupported(cmd))
            return getattr(self, f"_text_{cmd}")(req)

    def samples(self, req: dict) -> List[int]:
        raise TransportError(self._unsupported(req.get("cmd")))

    def stream_chunks(self, req: dict) -> Iterator[Dict[str, Any]]:
        raise TransportError(self._unsupported(req.get("cmd")))

    @staticmethod
    def _clean(raw: str, cmd: str) -> str:
        s = raw.replace("\r\n", "\n").replace("\r", "\n")
        s = s.rstrip(" \t\n")
        if s.endswith(">"):
            s = s[:-1].rstrip(" \t\n")
        kept = [ln for ln in s.split("\n") if ln.strip() != cmd]
        return "\n".join(kept).rstrip("\n")

    # -- Transport API ------------------------------------------------------

    def status(self) -> Any:
        return self.command({"cmd": "status"})

    def status_without_caps(self) -> Any:
        return self.command({"cmd": "status", "caps": False})

    def ping(self) -> Any:
        return self.command({"cmd": "ping"})

    def identity(self) -> str:
        """The pod's identity short id (``"baea06"``, from the console's ``identity`` command),
        or ``""`` when the pod has none or its firmware has no ``identity`` command."""
        return parse_identity(self._send_command("identity"))

    # -- text-console equivalents ---------------------------------------------

    def _text_status(self, req: dict) -> Dict[str, Any]:
        return parse_text_status(self._send_command("status"))

    def _text_ping(self, req: dict) -> str:
        out = self._send_command("ping")
        if "PING ok" not in out:
            raise TransportError(f"the pod did not answer ping: {out.strip()!r}")
        return "pong"

    def _text_lan_clients(self, req: dict) -> Dict[str, Any]:
        """``lan_clients`` through the console's ``lan-clients`` (firmware 4.0.1+): pairing works
        here also while the USB network adapter is off. The reply has the JSON command's shape."""
        if req.get("add") is not None:
            name, key = str(req["add"]), str(req.get("spki_sha256", ""))
            if not name or any(c in name for c in " \t\"\r\n"):
                raise ValueError(f"lan_clients add: {name!r}: over the USB console a name has no spaces")
            line = f"lan-clients add {name} {key.lower()}"
        elif req.get("remove") is not None:
            line = f"lan-clients remove {req['remove']}"
        elif req.get("clear"):
            line = "lan-clients clear"
        else:
            line = "lan-clients"
        return parse_text_lan_clients(self._send_command(line), line)

    def _text_lan_tls(self, req: dict) -> Dict[str, Any]:
        """``lan_tls`` (a read) through the console's ``lan-tls``: the port, HTTPS-only, SCPI and
        the certificate's SHA-256."""
        if len(req) > 1:
            raise TransportError(self._unsupported("lan_tls (a change)"))
        return parse_text_lan_tls(self._send_command("lan-tls"))

    def _needs_network(self, what: str) -> TransportError:
        """The USB console of firmware 4.0 answers status, ping and identity only."""
        return TransportError(
            f"{what} is not on this pod's USB console {self.device or ''}: it is a network "
            "command. Connect with 'usb', which uses the pod's USB network adapter (Wi-Fi "
            "configured on the pod turns it off; `benchpod clear-wifi` turns it back on), or pass "
            "the pod's network address".replace("  ", " "))

    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        raise self._needs_network("target power")

    def dap_start(self, swclk: int, swdio: int, packet_size: Optional[int] = None,
                  packet_count: Optional[int] = None, wait_ms: Optional[int] = None) -> RawLink:
        raise self._needs_network("SWD (flashing)")

    def uart_proxy_start(self, rx: int, tx: int, baud: int, parity: str = "none",
                         stop_bits: int = 1, half_duplex: bool = False,
                         entry: Any = None, de: Optional[int] = None,
                         de_active_low: bool = False) -> RawLink:
        raise self._needs_network("the UART proxy")

    def close(self) -> None:
        try:
            self._port.close()
        except Exception:
            pass


def _console_reply_lines(raw: str, line: str) -> List[str]:
    out, echo_seen = [], False
    for ln in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        ln = ln.strip().lstrip(">").strip()
        if not ln:
            continue
        if not echo_seen and ln == line:
            echo_seen = True
            continue
        out.append(ln)
    return out


def _console_refusal(cmd: str, raw: str, rest: str) -> None:
    if "unknown command" in raw:
        raise TransportError(f"this firmware has no {cmd} on its USB console (firmware 4.0.1+)")
    if rest == "error" or rest.startswith("error "):
        why = rest[5:].strip() or "refused"
        raise FirmwareError(f"{cmd}: {why}")


def parse_text_lan_clients(raw: str, line: str = "lan-clients") -> Dict[str, Any]:
    """The console's ``lan-clients`` reply as the JSON command's ``data``."""
    data: Dict[str, Any] = {}
    header = False
    for ln in _console_reply_lines(raw, line):
        if ln.startswith("lan-clients "):
            rest = ln[len("lan-clients "):].strip()
            _console_refusal("lan-clients", raw, rest)
            if rest in ("added", "renamed", "same"):
                data["outcome"] = rest
                continue
            f = rest.replace(",", " ").split()
            if len(f) >= 3 and f[1] == "of" and f[0].isdigit() and f[2].isdigit():
                data.update(count=int(f[0]), max=int(f[2]), offset=0, clients=[],
                            pairing=len(f) >= 5 and f[3] == "pairing" and f[4] == "on")
                header = True
            continue
        if header:
            f = ln.split()
            if len(f) == 2 and len(f[1]) == 64:
                data["clients"].append({"name": f[0], "spki_sha256": f[1]})
    if not header:
        _console_refusal("lan-clients", raw, "")
        raise TransportError(f"the pod did not answer lan-clients: {raw.strip()[-200:]!r}")
    return data


def parse_text_lan_tls(raw: str) -> Dict[str, Any]:
    """The console's ``lan-tls`` reply: ``lan-tls port 8443 https-only on sha256 <hex> ... scpi off``."""
    for ln in reversed(_console_reply_lines(raw, "lan-tls")):
        if not ln.startswith("lan-tls "):
            continue
        rest = ln[len("lan-tls "):].strip()
        _console_refusal("lan-tls", raw, rest)
        f = rest.split()
        out: Dict[str, Any] = {"sha256": ""}
        for i in range(len(f) - 1):
            if f[i] == "port" and f[i + 1].isdigit():
                out["port"] = int(f[i + 1])
            elif f[i] == "https-only":
                out["https_only"] = f[i + 1] == "on"
            elif f[i] == "sha256" and len(f[i + 1]) == 64:
                out["sha256"] = f[i + 1].lower()
            elif f[i] == "scpi":
                out["scpi"] = f[i + 1] == "on"
        return out
    _console_refusal("lan-tls", raw, "")
    raise TransportError(f"the pod did not answer lan-tls: {raw.strip()[-200:]!r}")
