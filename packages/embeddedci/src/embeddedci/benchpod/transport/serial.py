"""USB serial-console transport.

Mirrors the Go ``serialconsole``: the firmware exposes a line-oriented text
console over USB CDC-ACM. Commands are echoed and each reply ends with a
``"> "`` prompt. ``dap-start`` switches the port to a raw length-framed
CMSIS-DAP stream until the quit byte is sent.

``pyserial`` is imported lazily so the TCP-only path has no hard dependency on
it at import time.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Iterator, List, Optional

from ..errors import FirmwareError, TransportError
from ..protocol import encode_request, parse_reply, raise_for_status
from .base import RawLink, Transport

# USB vendor ids a bench-pod console enumerates under: 0x2E8A (Raspberry Pi,
# the RP2350 pod) and 0x0483 (STMicroelectronics, the STM32H563 pod, which uses
# ST's stock CDC VID/PID 0483:5740 so the host binds its in-box driver).
#
# Neither id is exclusive to a bench pod — 0x2E8A is shared with the CMSIS-DAP
# probe's CDC and 0483:5740 with every ST Virtual COM Port on the bench — so
# these only ORDER the ports to probe. Identification is by probing for
# BENCHPOD_MARKER, never by vendor id alone.
POD_VIDS = (0x2E8A, 0x0483)
RP_VID = 0x2E8A  # deprecated alias, kept for callers that imported it

# The substring the firmware's ``status`` prints to identify itself
# ("device : benchpod").
BENCHPOD_MARKER = "benchpod"

# Per-port probe budget. A real pod answers ``status`` in well under a second;
# only ports that are not pods run this out.
PROBE_TIMEOUT = 2.0
BAUD = 115200
PROMPT = "> "
DAP_READY = "dap ready"          # console prints "dap ready" then carries framed CMSIS-DAP
DAP_LEAVE = b"\x00\x00"          # zero-length frame — leaves the console DAP passthrough
UART_READY = "uart ready"        # console prints "uart ready (press Ctrl-] to exit)"
CTRL_RBRACKET = b"\x1d"          # Ctrl-] — leaves the console UART proxy
_CLEAR_LINE = b"\x08" * 128  # backspaces to clear any partial input line
_RAW_READ_TIMEOUT = 0.1  # poll interval while bridging raw CMSIS-DAP bytes
JSON_PROBE_TIMEOUT = 3.0  # how long to wait for the console to answer the `json` mode switch
UNKNOWN_COMMAND = "unknown command"

#: JSON commands the text console can answer itself when the firmware has no JSON mode on USB
#: (the STM32 pod's console is a text shell: status, ping, la-voltage, power, diagnostics).
TEXT_CONSOLE_COMMANDS = ("status", "ping", "la_voltage")


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
    return out


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
    """USB serial ports to probe, most likely to be a pod first.

    Ordering, not filtering: a pod is identified by probing (see
    :func:`_is_benchpod`), so an adapter this ranking puts last is still tried.
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
    return [p.device for p in ports]


def _is_benchpod(device: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """Open ``device``, run ``status``, and report whether a pod answered.

    Anything that goes wrong — the port is held by another process, it is not a
    console, it never prints a prompt — means "not a pod here", so every failure
    is swallowed and the caller moves on to the next candidate.
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
        while time.monotonic() < deadline:
            chunk = port.read(256)
            if chunk:
                buf.extend(chunk)
                if BENCHPOD_MARKER in buf.decode("utf-8", errors="replace").lower():
                    return True
        return False
    except Exception:
        return False
    finally:
        if port is not None:
            try:
                port.close()
            except Exception:
                pass


def autodetect_port() -> str:
    """Return the device path of the first port that answers as a BenchPod.

    Every USB serial port is probed (best candidates first) rather than trusting
    a vendor-id match, because the pod's vendor ids are shared with other
    hardware and differ between pod generations — an STM32 pod enumerates under
    ST's generic CDC id, so a vendor-id filter would miss it entirely.
    """
    candidates = _candidate_ports()
    if not candidates:
        raise TransportError(
            "no USB serial ports found — is the BenchPod plugged in, and does the "
            "cable carry data (many USB cables are charge-only)? "
            "Pass an explicit device path to override."
        )
    for device in candidates:
        if _is_benchpod(device):
            return device
    raise TransportError(
        "no BenchPod found among the USB serial ports probed ("
        + ", ".join(candidates)
        + "). Is the pod powered? A pod with no firmware never opens a console — "
        "flash it with `benchpod flash-self`. Pass an explicit device path to override."
    )


class _SerialRawLink:
    """Adapts the open serial port to :class:`RawLink` for a raw console mode.

    ``read`` blocks (polling on a short timeout) until data arrives or the link
    is closed, so a quiet stretch of the stream is not mistaken for EOF.
    ``close`` sends a mode-specific quit byte (``Q`` for SWD, Ctrl-] ``0x1d`` for
    UART proxy) to leave the mode; it does not close the underlying port (the
    transport owns that).
    """

    def __init__(self, port, quit_byte: bytes = b"Q") -> None:
        self._port = port
        self._closed = False
        self._quit = quit_byte
        self._port.timeout = _RAW_READ_TIMEOUT

    def read(self, n: int) -> bytes:
        # Return as soon as ANY byte is available — do NOT wait for the full `n`.
        # pyserial's read(n) only returns early once `n` bytes arrive, so reading
        # `n` here would stall the full per-read timeout on every remote_bitbang
        # sample (the pod replies a byte or two at a time), throttling a flash to
        # a crawl. Block for the first byte, then drain whatever else is buffered.
        while not self._closed:
            first = self._port.read(1)
            if not first:
                continue  # read timeout with no data → poll again (or until closed)
            waiting = getattr(self._port, "in_waiting", 0) or 0
            if waiting and n > 1:
                first += self._port.read(min(n - 1, waiting))
            return first
        return b""

    def write(self, data: bytes) -> int:
        # Do NOT flush() here. flush() is tcdrain — it blocks until the OS has
        # physically transmitted every byte, which on a USB-serial adapter can
        # stall for tens of ms (or longer on a driver hiccup) on every write.
        # In the SWD bridge that blocks the oc->pod pump thread, backs up
        # OpenOCD, and can freeze a whole flash. The kernel transmits the queued
        # bytes asynchronously regardless (serial has no Nagle), so the pod still
        # receives them promptly and replies — we just don't wait on the drain.
        self._port.write(data)
        return len(data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._port.write(self._quit)
            self._port.flush()
        except Exception:
            pass


class SerialTransport(Transport):
    """Talks to the pod over the USB serial console."""

    def __init__(self, device: str = "", *, timeout: float = 30.0,
                 port: Optional[Any] = None) -> None:
        # ``port`` is a test seam: pass a pyserial-like object to skip opening a
        # real device (and pyserial autodetect).
        self.timeout = timeout
        self._json_mode = False  # console "json" mode active?
        #: Whether the firmware's console has a JSON mode at all (None = not probed yet). The
        #: STM32 pod's USB console does not: only the text commands work over USB there.
        self.json_supported: Optional[bool] = None
        if port is not None:
            self.device = device
            self._port = port
            return
        serial, _ = _import_serial()
        self._serial_mod = serial
        self.device = device or autodetect_port()
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
        """Accumulate output until the trailing ``"> "`` prompt or timeout."""
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self._port.read(256)
            if chunk:
                buf.extend(chunk)
                text = buf.decode("utf-8", errors="replace")
                if text.rstrip(" \t").endswith(">"):
                    return text
        raise TransportError(
            f"timed out waiting for console prompt; got: "
            f"{buf.decode('utf-8', errors='replace')!r}"
        )

    def _send_command(self, line: str) -> str:
        self._ensure_text()
        deadline = time.monotonic() + self.timeout
        self._write_line(line)
        return self._read_until_prompt(deadline)

    # -- console "json" mode (TCP-style JSON API over serial) ---------------

    def _read_json_reply(self, deadline: float):
        """Read lines until a JSON object reply; skip echoed/debug log lines.

        In JSON mode the firmware interleaves ``[...]`` debug prints with the
        JSON reply on the same stream, so anything not starting with ``{`` is
        ignored. Yields each decoded ``Reply`` (caller decides when to stop).
        """
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self._port.read(256)
            if chunk:
                buf.extend(chunk)
            while b"\n" in buf:
                raw_line, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                s = raw_line.decode("utf-8", errors="replace").strip()
                if not s or not s.startswith("{"):
                    continue  # echo, prompt, or [subsystem] debug line
                yield parse_reply(s.encode("utf-8"))
        raise TransportError("timed out waiting for a JSON reply over serial")

    def _probe_json(self) -> bool:
        """Ask the console to switch to JSON mode once and remember whether it can.

        A console without the mode answers ``unknown command 'json'`` straight away, so the probe
        costs a round trip, not a timeout.
        """
        if self.json_supported is not None:
            return self.json_supported
        self._write_line("json")
        deadline = time.monotonic() + min(self.timeout, JSON_PROBE_TIMEOUT)
        buf = bytearray()
        supported = False
        while time.monotonic() < deadline:
            chunk = self._port.read(256)
            if chunk:
                buf.extend(chunk)
            if UNKNOWN_COMMAND in buf.decode("utf-8", errors="replace"):
                break
            lines = buf.split(b"\n")[:-1]  # complete lines only
            replies = [ln.strip() for ln in lines if ln.strip().startswith(b"{")]
            if replies:
                raise_for_status(parse_reply(replies[0]), cmd="json")
                supported = True
                self._json_mode = True
                break
        if not supported:
            try:
                self._port.reset_input_buffer()
            except Exception:
                pass
        self.json_supported = supported
        return supported

    @staticmethod
    def _unsupported(what: str) -> str:
        return (f"{what!r} is not available over this pod's USB console, which only answers "
                "text commands (status, ping, LA voltage, target power); connect over the network "
                "(host[:port]) or the cloud (embeddedci:<device>) for it")

    def _enter_json(self, cmd: Any = "json") -> None:
        if not self._probe_json():
            raise TransportError(self._unsupported(cmd))
        if self._json_mode:
            return
        deadline = time.monotonic() + self.timeout
        self._write_line("json")
        for reply in self._read_json_reply(deadline):
            raise_for_status(reply, cmd="json")
            self._json_mode = True
            return

    def _exit_json(self) -> None:
        if not self._json_mode:
            return
        deadline = time.monotonic() + self.timeout
        self._port.write(b'{"cmd":"json_exit"}\n')
        self._port.flush()
        try:
            for _reply in self._read_json_reply(deadline):
                break  # got the exit ack
        except TransportError:
            pass
        self._json_mode = False
        try:
            self._port.reset_input_buffer()
        except Exception:
            pass

    def _ensure_text(self) -> None:
        """Leave JSON mode so a text console command can run."""
        self._exit_json()

    def command(self, req: dict) -> Any:
        """Send one JSON command over the console's JSON mode and return its data.

        When the firmware has no JSON mode on USB, the commands in :data:`TEXT_CONSOLE_COMMANDS`
        are answered through their text-console equivalents; any other command raises a
        :class:`TransportError` naming the network/cloud alternative.
        """
        cmd = req.get("cmd")
        if not self._probe_json():
            if cmd not in TEXT_CONSOLE_COMMANDS:
                raise TransportError(self._unsupported(cmd))
            return getattr(self, f"_text_{cmd}")(req)
        self._enter_json(cmd)
        deadline = time.monotonic() + self.timeout
        self._port.write(encode_request(req))
        self._port.flush()
        for reply in self._read_json_reply(deadline):
            raise_for_status(reply, cmd=req.get("cmd"))
            return reply.data
        raise TransportError("no JSON reply over serial")

    def samples(self, req: dict) -> List[int]:
        """Send a command whose reply is a chunked sample array (json mode)."""
        self._enter_json(req.get("cmd"))
        deadline = time.monotonic() + self.timeout
        self._port.write(encode_request(req))
        self._port.flush()
        out: List[int] = []
        for reply in self._read_json_reply(deadline):
            raise_for_status(reply, cmd=req.get("cmd"))
            if isinstance(reply.data, list):
                out.extend(reply.data)
            if not reply.more:
                return out
        raise TransportError("incomplete chunked JSON reply over serial")

    def _read_json_objects(self, idle_timeout: float) -> Iterator[Dict[str, Any]]:
        """Yield each JSON object line; the timeout restarts whenever bytes arrive, so a long
        streamed capture is bounded by silence rather than by its total length."""
        buf = bytearray()
        deadline = time.monotonic() + idle_timeout
        while time.monotonic() < deadline:
            chunk = self._port.read(256)
            if chunk:
                buf.extend(chunk)
                deadline = time.monotonic() + idle_timeout
            while b"\n" in buf:
                raw_line, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                s = raw_line.decode("utf-8", errors="replace").strip()
                if not s.startswith("{"):
                    continue  # echo, prompt, or [subsystem] debug line
                try:
                    obj = json.loads(s)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
        raise TransportError("timed out waiting for a streamed JSON reply over serial")

    def stream_chunks(self, req: dict) -> Iterator[Dict[str, Any]]:
        """Send a streaming command and yield each raw chunk object until ``more`` is false.

        The serial counterpart of :meth:`TcpTransport.stream_chunks`: callers see every chunk's
        extra fields (achieved rates, the RLE LA frames), which :meth:`samples` flattens away.
        """
        self._enter_json(req.get("cmd"))
        self._port.write(encode_request(req))
        self._port.flush()
        cmd = req.get("cmd")
        for obj in self._read_json_objects(self.timeout):
            if obj.get("status") == "error":
                raise FirmwareError(obj.get("message") or "streamed command failed", cmd=cmd)
            yield obj
            if not bool(obj.get("more", False)):
                return

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

    def ping(self) -> Any:
        return self.command({"cmd": "ping"})

    # -- text-console equivalents (firmware without a JSON mode on USB) ------

    def _text_status(self, req: dict) -> Dict[str, Any]:
        return parse_text_status(self._send_command("status"))

    def _text_ping(self, req: dict) -> str:
        out = self._send_command("ping")
        if "PING ok" not in out:
            raise TransportError(f"the pod did not answer ping: {out.strip()!r}")
        return "pong"

    def _text_la_voltage(self, req: dict) -> Dict[str, Any]:
        line = "la-voltage" + (f" {int(req['mv'])}" if req.get("mv") is not None else "")
        out = self._send_command(line)
        if "needs a v3 pod" in out or "usage:" in out:
            msg = next((ln.strip() for ln in out.replace("\r", "\n").split("\n")
                        if "needs a v3 pod" in ln or "usage:" in ln), out.strip())
            raise FirmwareError(msg, cmd="la_voltage")
        m = re.search(r"LA VCCIO = (?:(\d+) mV|UNSET) \(st=(-?\d+)\)", out)
        if not m:
            raise TransportError(f"unexpected la-voltage reply: {out.strip()!r}")
        return {"mv": int(m.group(1) or 0), "st": int(m.group(2))}

    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        if delay_ms:
            raise TransportError("the pod's USB console cannot schedule a delayed power change; "
                                 "use delay=None, or a network/cloud connection")
        out = self._send_command(f"power {int(efuse)} {'on' if on else 'off'}")
        if UNKNOWN_COMMAND in out or "bad eFuse" in out or "ERROR:" in out:
            raise TransportError(f"firmware rejected power {efuse}: {out.strip()!r}")

    def _console_raw_handshake(
        self, cmd: str, ready: str, quit_byte: bytes
    ) -> RawLink:
        """Send a console command, read until the ``ready`` sentinel, then hand
        back the raw byte link (its ``close()`` sends ``quit_byte`` to exit).

        On any failure we still send ``quit_byte``: by the time the sentinel is
        due the firmware may already have armed and switched to the raw mode, so
        without it the pod stays wedged until its inactivity watchdog and the
        next handshake fails too.
        """
        self._ensure_text()
        # Recover from a previous raw session that didn't cleanly exit (e.g. a
        # stalled flash whose Q got dropped on a flaky link): send the quit byte
        # so the pod leaves raw mode, then this command is parsed by the console
        # rather than swallowed as protocol bytes. Harmless when already in text
        # mode (an unknown char, cleared below).
        try:
            self._port.write(quit_byte)
            self._port.flush()
            time.sleep(0.1)
            self._port.reset_input_buffer()
        except Exception:
            pass
        self._write_line(cmd)
        deadline = time.monotonic() + self.timeout
        verb = cmd.split()[0]
        acc = bytearray()

        def _recover_and_raise(msg: str):
            try:
                self._port.write(quit_byte)
                self._port.flush()
                time.sleep(0.1)
                self._port.reset_input_buffer()
            except Exception:
                pass
            raise TransportError(msg)

        # Match the ``ready`` sentinel as a substring of the accumulated output
        # rather than line-by-line: the firmware can emit a long, newline-less
        # run (backspace echo from the clear-line prefix) before the sentinel,
        # and the command echo never contains the sentinel, so this is robust.
        while time.monotonic() < deadline:
            chunk = self._port.read(256)
            if chunk:
                acc.extend(chunk)
            text = acc.decode("utf-8", errors="replace")
            if ready in text:
                return _SerialRawLink(self._port, quit_byte=quit_byte)
            if UNKNOWN_COMMAND in text:
                _recover_and_raise(
                    f"this pod's USB console has no '{verb}' command — flashing and the UART "
                    "proxy need a network (host[:port]) or cloud (embeddedci:<device>) connection"
                )
            if "ERROR:" in text or "usage:" in text:
                _recover_and_raise(
                    f"{verb} rejected by firmware; pod output:\n{text.strip()}"
                )
        _recover_and_raise(
            f"{verb}: pod never reported {ready!r}; pod output:\n{acc.decode('utf-8', 'replace').strip()}"
        )

    def dap_start(self, swclk: int, swdio: int) -> RawLink:
        cmd = f"dap-start {swclk} {swdio}"
        return self._console_raw_handshake(cmd, DAP_READY, quit_byte=DAP_LEAVE)

    def uart_proxy_start(self, rx: int, tx: int, baud: int) -> RawLink:
        return self._console_raw_handshake(
            f"uart-proxy {rx} {tx} {baud}", UART_READY, quit_byte=CTRL_RBRACKET
        )

    def close(self) -> None:
        try:
            self._exit_json()
        except Exception:
            pass
        try:
            self._port.close()
        except Exception:
            pass
