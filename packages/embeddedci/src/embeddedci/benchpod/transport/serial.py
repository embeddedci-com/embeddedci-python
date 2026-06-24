"""USB serial-console transport.

Mirrors the Go ``serialconsole``: the firmware exposes a line-oriented text
console over USB CDC-ACM (VID 0x2E8A). Commands are echoed and each reply ends
with a ``"> "`` prompt. ``swd-start`` switches the port to a raw remote_bitbang
stream until the ``Q`` quit byte is sent.

``pyserial`` is imported lazily so the TCP-only path has no hard dependency on
it at import time.
"""

from __future__ import annotations

import time
from typing import Any, List, Optional

from ..errors import TransportError
from ..protocol import encode_request, parse_reply, raise_for_status
from .base import RawLink, Transport

RP_VID = 0x2E8A  # Raspberry Pi (RP2350) USB vendor id
BAUD = 115200
PROMPT = "> "
SWD_READY = "swd ready"
DAP_READY = "dap ready"          # console prints "dap ready" then carries framed CMSIS-DAP
DAP_LEAVE = b"\x00\x00"          # zero-length frame — leaves the console DAP passthrough
UART_READY = "uart ready"        # console prints "uart ready (press Ctrl-] to exit)"
CTRL_RBRACKET = b"\x1d"          # Ctrl-] — leaves the console UART proxy
_CLEAR_LINE = b"\x08" * 128  # backspaces to clear any partial input line
_RAW_READ_TIMEOUT = 0.1  # poll interval while bridging raw bitbang bytes


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


def autodetect_port() -> str:
    """Return the device path of the first RP2350 CDC-ACM port, else raise."""
    _, list_ports = _import_serial()
    matches = [p.device for p in list_ports.comports() if p.vid == RP_VID]
    if not matches:
        raise TransportError(
            f"no BenchPod serial device found (looked for USB VID "
            f"0x{RP_VID:04x}); pass an explicit device path instead"
        )
    return matches[0]


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
        # corrupt the command (e.g. "swd-start 1 2 3" -> "wsr1"), so we flush
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

    def _enter_json(self) -> None:
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
        """Send one JSON command over the console json mode; return its data."""
        self._enter_json()
        deadline = time.monotonic() + self.timeout
        self._port.write(encode_request(req))
        self._port.flush()
        for reply in self._read_json_reply(deadline):
            raise_for_status(reply, cmd=req.get("cmd"))
            return reply.data
        raise TransportError("no JSON reply over serial")

    def samples(self, req: dict) -> List[int]:
        """Send a command whose reply is a chunked sample array (json mode)."""
        self._enter_json()
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
        return self._clean(self._send_command("status"), "status")

    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        state = "on" if on else "off"
        cmd = f"target-power {efuse} {state}"
        if delay_ms:
            cmd += f" {int(delay_ms)}"
        out = self._send_command(cmd)
        for line in out.replace("\r", "\n").split("\n"):
            if line.strip().startswith("ERROR:"):
                raise TransportError(f"firmware rejected target-power: {line.strip()}")

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
            if "ERROR:" in text or "usage:" in text:
                _recover_and_raise(
                    f"{verb} rejected by firmware; pod output:\n{text.strip()}"
                )
        _recover_and_raise(
            f"{verb}: pod never reported {ready!r}; pod output:\n{acc.decode('utf-8', 'replace').strip()}"
        )

    def swd_start(self, swclk: int, swdio: int, nreset: Optional[int]) -> RawLink:
        cmd = f"swd-start {swclk} {swdio}"
        if nreset is not None:
            cmd += f" {nreset}"
        return self._console_raw_handshake(cmd, SWD_READY, quit_byte=b"Q")

    def dap_start(self, swclk: int, swdio: int, nreset: Optional[int]) -> RawLink:
        cmd = f"dap-start {swclk} {swdio}"
        if nreset is not None:
            cmd += f" {nreset}"
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
