"""JSON-over-TCP transport (wifi/network), port 8080 by default.

Mirrors the Go ``tcpclient``: each command dials a fresh connection (the
firmware serves one client at a time), disables Nagle, sends one JSON line and
reads the reply. ``swd_start`` is special — after its ack the same socket
switches to raw remote_bitbang, so the ack must be read one byte at a time so no
bitbang bytes are swallowed.
"""

from __future__ import annotations

import socket
import time
from typing import Any, List, Optional

from ..errors import TransportError
from ..protocol import encode_request, parse_reply, raise_for_status
from .base import RawLink, Transport

DEFAULT_DIAL_TIMEOUT = 10.0  # total budget to establish a connection
_DIAL_ATTEMPT_TIMEOUT = 0.5
_DIAL_RETRY_BACKOFF = 0.1


class _SocketRawLink:
    """Adapts a connected socket to :class:`RawLink` for the flash bridge."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    def read(self, n: int) -> bytes:
        try:
            return self._sock.recv(n)
        except OSError:
            return b""

    def write(self, data: bytes) -> int:
        self._sock.sendall(data)
        return len(data)

    def close(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


class TcpTransport(Transport):
    """Talks to the pod over JSON/TCP."""

    def __init__(self, addr: str, *, timeout: float = 30.0,
                 dial_timeout: float = DEFAULT_DIAL_TIMEOUT) -> None:
        if not addr:
            raise TransportError("TCP transport requires a host:port address")
        self.addr = addr
        self.timeout = timeout
        self.dial_timeout = dial_timeout

    # -- connection helpers -------------------------------------------------

    def _split_addr(self) -> "tuple[str, int]":
        host, sep, port = self.addr.rpartition(":")
        if not sep:
            raise TransportError(f"invalid address {self.addr!r}; expected host:port")
        host = host.strip("[]")  # tolerate bracketed IPv6
        try:
            return host, int(port)
        except ValueError:
            raise TransportError(f"invalid port in address {self.addr!r}") from None

    def _dial(self) -> socket.socket:
        host, port = self._split_addr()
        deadline = time.monotonic() + self.dial_timeout
        last: Optional[Exception] = None
        while True:
            try:
                sock = socket.create_connection((host, port), timeout=_DIAL_ATTEMPT_TIMEOUT)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(self.timeout)
                return sock
            except OSError as exc:
                last = exc
                if time.monotonic() >= deadline:
                    break
                time.sleep(_DIAL_RETRY_BACKOFF)
        raise TransportError(f"could not connect to {self.addr}: {last}")

    @staticmethod
    def _recv_line(sock: socket.socket, buf: bytearray) -> bytes:
        """Read one newline-terminated line, buffering any overshoot in ``buf``."""
        while True:
            nl = buf.find(b"\n")
            if nl >= 0:
                line = bytes(buf[:nl])
                del buf[: nl + 1]
                return line
            chunk = sock.recv(4096)
            if not chunk:
                if buf:
                    line = bytes(buf)
                    buf.clear()
                    return line
                raise TransportError("connection closed before a full reply line")
            buf.extend(chunk)

    @staticmethod
    def _recv_line_exact(sock: socket.socket) -> bytes:
        """Read one line a byte at a time, leaving everything after ``\\n``.

        Used for the ``swd_start`` ack: the bytes after the newline are the raw
        remote_bitbang stream and must not be consumed here.
        """
        out = bytearray()
        while True:
            b = sock.recv(1)
            if not b:
                raise TransportError("connection closed before swd_start ack")
            if b == b"\n":
                return bytes(out)
            out.extend(b)

    # -- Transport API ------------------------------------------------------

    def command(self, req: dict) -> Any:
        """Send one JSON command and return its ``data`` (raises on error)."""
        sock = self._dial()
        try:
            sock.sendall(encode_request(req))
            reply = parse_reply(self._recv_line(sock, bytearray()))
            raise_for_status(reply, cmd=req.get("cmd"))
            return reply.data
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def samples(self, req: dict) -> List[int]:
        """Send a command whose reply is a chunked sample array."""
        sock = self._dial()
        buf = bytearray()
        out: List[int] = []
        try:
            sock.sendall(encode_request(req))
            while True:
                reply = parse_reply(self._recv_line(sock, buf))
                raise_for_status(reply, cmd=req.get("cmd"))
                if isinstance(reply.data, list):
                    out.extend(reply.data)
                if not reply.more:
                    break
            return out
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def status(self) -> Any:
        return self.command({"cmd": "status"})

    def ping(self) -> Any:
        return self.command({"cmd": "ping"})

    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        req: dict = {"cmd": "target_power", "efuse": efuse, "state": 1 if on else 0}
        if delay_ms:
            req["delay_ms"] = int(delay_ms)
        self.command(req)

    def _raw_handshake(self, req: dict, cmd: str) -> RawLink:
        """Send a mode-switch command and hand back the raw byte link.

        Shared by ``swd_start`` and ``uart_proxy_start``: both ack one JSON line
        and then the same socket carries raw bytes, so the ack must be read one
        byte at a time (``_recv_line_exact``) to not swallow what follows.
        """
        sock = self._dial()
        # The session can outlast the per-command timeout — clear it so the
        # caller owns the lifetime, mirroring the Go client.
        sock.settimeout(None)
        try:
            sock.sendall(encode_request(req))
            reply = parse_reply(self._recv_line_exact(sock))
            raise_for_status(reply, cmd=cmd)
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise
        return _SocketRawLink(sock)

    def swd_start(self, swclk: int, swdio: int, nreset: Optional[int]) -> RawLink:
        req: dict = {"cmd": "swd_start", "swclk": swclk, "swdio": swdio}
        if nreset is not None:
            req["nreset"] = nreset
        return self._raw_handshake(req, "swd_start")

    def uart_proxy_start(self, rx: int, tx: int, baud: int) -> RawLink:
        return self._raw_handshake(
            {"cmd": "uart_proxy_start", "rx": rx, "tx": tx, "baud": baud},
            "uart_proxy_start",
        )

    def close(self) -> None:
        # Nothing persistent is held; connections are per-command.
        return None
