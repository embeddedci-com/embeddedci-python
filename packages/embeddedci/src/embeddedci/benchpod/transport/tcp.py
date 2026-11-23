"""JSON-over-TCP transport (wifi/network): TLS on 8443 for a LAN address, plaintext 8080 on the
pod's USB network adapter or when the port is given (see :mod:`.tls`).

Mirrors the Go ``tcpclient``: each command dials a fresh connection (the
firmware serves one client at a time), disables Nagle, sends one JSON line and
reads the reply. ``dap_start`` is special — after its ack the same socket
switches to a raw length-framed CMSIS-DAP stream, so the ack must be read one
byte at a time so no DAP bytes are swallowed.

A host name is resolved once per transport (IPv4 first, see :func:`resolve_host`) and the address
is reused for the following connections; a connect that fails resolves the name again, so a pod
that got a new DHCP lease is still found. TLS still uses the name as given: the certificate pin is
keyed by it, and it is the SNI for a name.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from ..errors import (
    BenchPodError,
    ConnectionClosedError,
    TransportError,
    TransportTimeout,
    firmware_error,
)
from ..protocol import encode_request, parse_reply, raise_for_status
from .base import RawLink, Transport, request_timeout
from . import tls as _tls

DEFAULT_DIAL_TIMEOUT = 10.0  # total budget to establish a connection
_DIAL_ATTEMPT_TIMEOUT = 0.5
_DIAL_RETRY_BACKOFF = 0.1
#: Floor for an upload's timeout (``load_bin``): the pod acknowledges a large upload slowly.
_UPLOAD_TIMEOUT = 60.0
_LINK_LOST = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)


@contextmanager
def _io_errors(what: str, timeout: Optional[float]) -> Iterator[None]:
    """Turn a socket error raised inside the block into a :class:`TransportError`: a timeout
    into :class:`TransportTimeout`, a reset or broken pipe into :class:`ConnectionClosedError`.
    SDK errors pass through unchanged."""
    try:
        yield
    except BenchPodError:
        raise
    except TimeoutError as exc:  # socket.timeout
        after = f" within {timeout:g} s" if timeout else ""
        raise TransportTimeout(f"{what}: the pod did not answer{after}") from exc
    except _LINK_LOST as exc:
        raise ConnectionClosedError(f"{what}: the connection was lost ({exc})") from exc
    except OSError as exc:
        raise TransportError(f"{what}: {exc}") from exc


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def resolve_host(host: str, port: int) -> List[str]:
    """The addresses of ``host``, IPv4 first: an ``AF_INET`` lookup, and an ``AF_UNSPEC`` one only
    when that has no answer. A default lookup of a ``.local`` name waits about 5 s on macOS for an
    AAAA answer an IPv4-only pod never sends; the ``AF_INET`` one takes milliseconds."""
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        infos = []
    if not infos:
        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    out: List[str] = []
    for info in infos:
        ip = str(info[4][0])
        if ip not in out:
            out.append(ip)
    if not out:
        raise socket.gaierror(f"{host}: no address")
    return out


class _SocketRawLink:
    """Adapts a connected socket to :class:`RawLink` for the flash bridge."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._closed = False
        #: The error that ended the link, if it did not end cleanly (see RawLink).
        self.error: Optional[BaseException] = None

    def read(self, n: int) -> bytes:
        try:
            return self._sock.recv(n)
        except (OSError, BenchPodError) as exc:
            # RawLink contract: b"" when the stream ends. Keep why, unless we closed it ourselves.
            if not self._closed and self.error is None:
                self.error = exc
            return b""

    def write(self, data: bytes) -> int:
        self._sock.sendall(data)
        return len(data)

    def close(self) -> None:
        self._closed = True
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

    #: Whether connection ``usb`` resolved to the pod's USB network adapter (firmware 3.9.0+).
    usb_net: bool = False
    #: The USB console's device path when connection ``usb`` found the USB network through the
    #: console; ``""`` for a network connection, or when this computer's own end of the USB link
    #: named the pod's address and the console was never opened.
    usb_console: str = ""
    #: Why connection ``usb`` went to the pod's Ethernet or Wi-Fi address instead (firmware 3.9.0+
    #: with its USB network off or down); ``""`` otherwise.
    usb_lan_fallback: str = ""

    #: Whether the last connection used TLS (port 8443).
    last_tls: bool = False
    #: The pod lease this transport's requests carry (:mod:`~embeddedci.benchpod.pod_lease`), or
    #: None. Set by :class:`~embeddedci.benchpod.BenchPod`.
    pod_lease: Optional[Any] = None

    def __init__(self, addr: str, *, timeout: float = 30.0,
                 dial_timeout: float = DEFAULT_DIAL_TIMEOUT, tls: Optional[str] = None,
                 tls_ca: Optional[str] = None) -> None:
        """``tls``: ``"auto"`` (default, also ``BENCHPOD_TLS``), ``"on"`` or ``"off"``;
        ``tls_ca``: a PEM file of CA certificates to verify the pod's certificate against instead
        of pinning it (also ``BENCHPOD_TLS_CA``). See :mod:`.tls`."""
        if not addr:
            raise TransportError("TCP transport requires a host:port address")
        self.addr = addr
        self.timeout = timeout
        self.dial_timeout = dial_timeout
        self.tls = _tls.tls_mode(tls)
        self.tls_ca = tls_ca if tls_ca is not None else (os.environ.get("BENCHPOD_TLS_CA") or None)
        #: Host name -> its resolved addresses, reused until a connect to them fails.
        self._resolved: Dict[str, List[str]] = {}

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

    def _dial(self, timeout: Optional[float] = None, kind: Optional[str] = None) -> socket.socket:
        """Connect, with ``timeout`` (default: the transport's) on every later read.

        ``kind`` names what the connection is for (``"uart"``, ``"capture"``, ``"dac"``,
        ``"flash"``). The cloud transport passes it to the server so a UART console and a capture
        can share the pod as they do here; a LAN socket needs no such hint and ignores it."""
        host, port = self._split_addr()
        io_timeout = self.timeout if timeout is None else timeout
        p = _tls.plan(host, port, self.tls)
        tls_to = p.tls
        if tls_to is None:
            assert p.plain is not None
            self.last_tls = False
            return self._connect(p.plain, self.dial_timeout, io_timeout)
        raw = self._connect(tls_to, self.dial_timeout, io_timeout)
        try:
            sock = _tls.wrap(raw, tls_to[0], tls_to[1], ca=self.tls_ca)
        except BenchPodError:
            raw.close()
            raise
        except (OSError, ValueError) as exc:  # ssl.SSLError is an OSError
            raw.close()
            raise TransportError(f"TLS handshake with {tls_to[0]}:{tls_to[1]}: {exc}") from exc
        self.last_tls = True
        return sock

    def _connect(self, addr: "tuple[str, int]", budget: float, io_timeout: Optional[float],
                 *, retry: bool = True,
                 attempt_timeout: float = _DIAL_ATTEMPT_TIMEOUT) -> socket.socket:
        """A TCP connection to ``addr``, re-probing within ``budget`` seconds. A host name is
        resolved once (:meth:`_addresses`) and resolved again only after a failed connect."""
        deadline = time.monotonic() + budget
        last: Optional[Exception] = None
        host, port = addr
        while True:
            try:
                sock = self._connect_resolved(host, port, attempt_timeout)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(io_timeout)
                return sock
            except OSError as exc:
                last = exc
                if not retry or time.monotonic() >= deadline:
                    break
                time.sleep(_DIAL_RETRY_BACKOFF)
        hint = ""
        if addr[1] == _tls.PLAIN_PORT and not _tls.is_usb_link(addr[0]):
            hint = ("; firmware 4.0 serves its API on the LAN over TLS only: connect without :8080 "
                    "(TLS on 8443), or over USB")
        raise TransportError(f"could not connect to {addr[0]}:{addr[1]}: {last}{hint}") from last

    def _addresses(self, host: str) -> List[str]:
        """The addresses to connect to for ``host``: the literal itself, else the cached
        resolution, else a new one (IPv4 first, :func:`resolve_host`)."""
        if _is_ip_literal(host):
            return [host]
        ips = self._resolved.get(host)
        if ips is None:
            ips = resolve_host(host, 0)
            self._resolved[host] = ips
        return ips

    def _connect_resolved(self, host: str, port: int, attempt_timeout: float) -> socket.socket:
        """One connect attempt to each address of ``host`` in turn. When none answers the cached
        resolution is dropped, so the next attempt resolves the name again (a new DHCP lease)."""
        last: Optional[OSError] = None
        try:
            for ip in self._addresses(host):
                try:
                    return socket.create_connection((ip, port), timeout=attempt_timeout)
                except OSError as exc:
                    last = exc
        except OSError:  # the lookup itself failed
            self._resolved.pop(host, None)
            raise
        self._resolved.pop(host, None)
        assert last is not None
        raise last

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
                raise ConnectionClosedError("connection closed before a full reply line")
            buf.extend(chunk)

    @classmethod
    def _recv_load_bin_done(cls, sock: socket.socket, buf: bytearray) -> bytes:
        """Read the ``load_bin`` completion line, skipping the ``{"ack":N}`` progress lines the
        pod interleaves on a cloud tunnel (firmware sends them only there, for flow control)."""
        while True:
            line = cls._recv_line(sock, buf)
            try:
                obj = json.loads(line)
            except ValueError:
                return line
            if isinstance(obj, dict) and "ack" in obj and "status" not in obj:
                continue
            return line

    @staticmethod
    def _recv_line_exact(sock: socket.socket) -> bytes:
        """Read one line a byte at a time, leaving everything after ``\\n``.

        Used for the ``dap_start`` ack: the bytes after the newline are the raw
        CMSIS-DAP stream and must not be consumed here.
        """
        out = bytearray()
        while True:
            b = sock.recv(1)
            if not b:
                raise ConnectionClosedError("connection closed before the mode-switch ack")
            if b == b"\n":
                return bytes(out)
            out.extend(b)

    # -- the pod lease -------------------------------------------------------

    def _encode(self, req: dict) -> bytes:
        """``req`` as a protocol line; while this transport holds the pod's lease
        (:attr:`pod_lease`, only set on a pod with the ``lease`` feature) it carries
        ``"lease":"<id>"``, which makes the command an owner's."""
        lease = self.pod_lease
        if lease is not None:
            lost = lease.take_pending_loss()
            if lost is not None:
                raise lost
            if lease.active and "lease" not in req:
                req = {**req, "lease": lease.lease_id}
        return encode_request(req)

    def _lease_error(self, exc: BaseException) -> BaseException:
        """``exc``; a ``BP0026`` (the lease was stopped from the web app) also ends the lease."""
        lease = self.pod_lease
        if lease is not None and getattr(exc, "code", "") == "BP0026":
            lease.lost_by_error(exc)
        return exc

    def _raise_for_status(self, reply: Any, cmd: Optional[str] = None) -> Any:
        try:
            return raise_for_status(reply, cmd=cmd)
        except BaseException as exc:
            raise self._lease_error(exc)

    # -- Transport API ------------------------------------------------------

    def command_sequence(self, reqs: List[dict]) -> List[Any]:
        """Send several JSON commands on ONE connection and return their ``data`` in order
        (raises on the first error). For what the pod ties to a connection, such as an upload
        (``ota_*``)."""
        timeout = max((request_timeout(r, self.timeout) for r in reqs), default=self.timeout)
        sock = self._dial(timeout)
        out: List[Any] = []
        buf = bytearray()
        try:
            for req in reqs:
                with _io_errors(f"{req.get('cmd')}", timeout):
                    sock.sendall(self._encode(req))
                    reply = parse_reply(self._recv_line(sock, buf))
                self._raise_for_status(reply, cmd=req.get("cmd"))
                out.append(reply.data)
            return out
        finally:
            sock.close()

    def command(self, req: dict) -> Any:
        """Send one JSON command and return its ``data`` (raises on error)."""
        timeout = request_timeout(req, self.timeout)
        sock = self._dial(timeout)
        try:
            with _io_errors(f"{req.get('cmd')}", timeout):
                sock.sendall(self._encode(req))
                reply = parse_reply(self._recv_line(sock, bytearray()))
            self._raise_for_status(reply, cmd=req.get("cmd"))
            return reply.data
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def samples(self, req: dict) -> List[int]:
        """Send a command whose reply is a chunked sample array, flattened from its decimal
        ``data`` lists (asks for ``"enc":"dec"`` unless ``req`` names an encoding; captures come
        as base64 otherwise, which :meth:`stream_chunks` yields)."""
        req = {**req}
        req.setdefault("enc", "dec")
        timeout = request_timeout(req, self.timeout)
        sock = self._dial(timeout, kind="capture")
        buf = bytearray()
        out: List[int] = []
        try:
            with _io_errors(f"{req.get('cmd')}", timeout):
                sock.sendall(self._encode(req))
            while True:
                with _io_errors(f"{req.get('cmd')}", timeout):
                    line = self._recv_line(sock, buf)
                reply = parse_reply(line)
                self._raise_for_status(reply, cmd=req.get("cmd"))
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

    def stream_chunks(self, req: dict) -> Iterator[Dict[str, Any]]:
        """Send a streaming command and yield each raw chunk object until ``more`` is false.

        Unlike :meth:`samples` (which flattens ``data`` into one int list) this yields the full
        parsed JSON of every chunk, so callers that need the extra per-chunk fields — achieved
        rates (``adc_rate_hz``/``la_rate_hz``) or the RLE LA frames (``la``/``la_edges``/
        ``la_upto``) of a ``capture_dual`` — can see them. Raises on an error chunk.
        """
        timeout = request_timeout(req, self.timeout)
        sock = self._dial(timeout, kind="capture")
        buf = bytearray()
        cmd = req.get("cmd")
        try:
            with _io_errors(f"{cmd}", timeout):
                sock.sendall(self._encode(req))
            while True:
                with _io_errors(f"{cmd}", timeout):
                    line = self._recv_line(sock, buf)
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError:
                    continue  # skip stray non-JSON fragments, mirroring the server
                if not isinstance(obj, dict):
                    continue
                if obj.get("status") == "error":
                    raise self._lease_error(firmware_error(obj.get("message") or "capture failed", cmd=cmd,
                                         code=str(obj.get("code") or ""),
                                         values=obj.get("args") if isinstance(obj.get("args"), dict) else None))
                yield obj
                if not bool(obj.get("more", False)):
                    return
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def load_replay(self, *, data: bytes, replay: dict, psram: bool = False) -> Any:
        """Stream a raw waveform to the pod (``load_bin``) and arm a ``replay`` on one connection.

        Mirrors the firmware protocol: send ``{"cmd":"load_bin","total":N,"psram":…}``, read the
        ``{"ready":N}`` ack, write ``N`` raw bytes, read the ``{"total":S}`` completion, then send
        ``replay`` on the same stream connection. A deep (``psram``) replay keeps looping out of
        PSRAM after the socket closes — so a concurrent capture can run alongside it (gateware
        v18). Returns the ``replay`` reply data.
        """
        timeout = max(self.timeout, _UPLOAD_TIMEOUT)
        sock = self._dial(timeout, kind="dac")
        buf = bytearray()
        try:
            begin = {"cmd": "load_bin", "total": len(data)}
            if psram:
                begin["psram"] = True
            with _io_errors("load_bin", timeout):
                sock.sendall(self._encode(begin))
                line = self._recv_line(sock, buf)
            self._raise_for_status(parse_reply(line), cmd="load_bin")
            with _io_errors("load_bin", timeout):
                sock.sendall(bytes(data))
                line = self._recv_load_bin_done(sock, buf)
            self._raise_for_status(parse_reply(line), cmd="load_bin")
            with _io_errors(replay.get("cmd", "replay"), timeout):
                sock.sendall(self._encode(replay))
                line = self._recv_line(sock, buf)
            reply = parse_reply(line)
            self._raise_for_status(reply, cmd=replay.get("cmd", "replay"))
            return reply.data
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def stage_psram(self, data: bytes) -> int:
        """Upload raw bytes into the pod's PSRAM staging area (``load_bin`` with ``"psram":true``)
        without arming anything: ``spi_stream`` sends them on. Returns the bytes the pod stored."""
        timeout = max(self.timeout, _UPLOAD_TIMEOUT)
        sock = self._dial(timeout)
        buf = bytearray()
        try:
            with _io_errors("load_bin", timeout):
                sock.sendall(self._encode({"cmd": "load_bin", "total": len(data), "psram": True}))
                line = self._recv_line(sock, buf)
            self._raise_for_status(parse_reply(line), cmd="load_bin")
            with _io_errors("load_bin", timeout):
                sock.sendall(bytes(data))
                line = self._recv_load_bin_done(sock, buf)
            reply = parse_reply(line)
            self._raise_for_status(reply, cmd="load_bin")
            total = reply.data.get("total") if isinstance(reply.data, dict) else None
            return int(total) if total is not None else len(data)
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
        req: dict = {"cmd": "target_power", "efuse": efuse, "enabled": bool(on)}
        if delay_ms:
            req["delay_ms"] = int(delay_ms)
        self.command(req)

    def _raw_handshake(self, req: dict, cmd: str, kind: str) -> RawLink:
        """Send a mode-switch command and hand back the raw byte link.

        Shared by ``dap_start`` and ``uart_proxy_start``: both ack one JSON line
        and then the same socket carries raw bytes, so the ack must be read one
        byte at a time (``_recv_line_exact``) to not swallow what follows.

        The ack is read under the request timeout, so a pod that never answers fails instead of
        hanging; only after it does the timeout come off.
        """
        timeout = request_timeout(req, self.timeout)
        sock = self._dial(timeout, kind=kind)
        try:
            with _io_errors(cmd, timeout):
                sock.sendall(self._encode(req))
                line = self._recv_line_exact(sock)
            reply = parse_reply(line)
            self._raise_for_status(reply, cmd=cmd)
            # The session can outlast the per-command timeout: clear it so the caller owns the
            # lifetime, mirroring the Go client.
            sock.settimeout(None)
        except BaseException:
            try:
                sock.close()
            except OSError:
                pass
            raise
        return _SocketRawLink(sock)

    def dap_start(self, swclk: Optional[int] = None, swdio: Optional[int] = None,
                  packet_size: Optional[int] = None, packet_count: Optional[int] = None,
                  wait_ms: Optional[int] = None, *, tck: Optional[int] = None,
                  tms: Optional[int] = None, tdi: Optional[int] = None, tdo: Optional[int] = None,
                  trst: Optional[int] = None, port: Optional[str] = None) -> RawLink:
        req: dict = {"cmd": "dap_start"}
        if swclk is not None or swdio is not None:
            if swclk is None or swdio is None:
                raise ValueError("dap_start needs both swclk and swdio, or neither")
            req["swclk"] = swclk
            req["swdio"] = swdio
        jtag = {"tck": tck, "tms": tms, "tdi": tdi, "tdo": tdo}
        if any(v is not None for v in jtag.values()) or trst is not None:
            missing = [k for k, v in jtag.items() if v is None]
            if missing:
                raise ValueError("dap_start JTAG needs tck, tms, tdi and tdo: missing " + ", ".join(missing))
            req.update(jtag)
            if trst is not None:
                req["trst"] = trst
        if "swclk" not in req and "tck" not in req:
            raise ValueError("dap_start needs swclk/swdio or tck/tms/tdi/tdo")
        if port is not None:
            if port not in ("swd", "jtag"):
                raise ValueError(f"dap_start port must be 'swd' or 'jtag', not {port!r}")
            if "swclk" in req and "tck" in req:
                req["port"] = port
        if packet_size:
            req["packet_size"] = packet_size
        if packet_count:
            req["packet_count"] = packet_count
        if wait_ms:
            req["wait_ms"] = wait_ms
        return self._raw_handshake(req, "dap_start", "flash")

    def uart_proxy_start(self, rx: Optional[int], tx: Optional[int], baud: int, parity: str = "none",
                         stop_bits: int = 1, half_duplex: bool = False,
                         entry: Any = None, de: Optional[int] = None,
                         de_active_low: bool = False, port: Optional[str] = None,
                         dtr: bool = True, rts: bool = True,
                         data_bits: Optional[int] = None) -> RawLink:
        if port is not None:
            # The board's USB serial port (firmware 4.0.3+): no LA channels, no half-duplex or entry.
            req = {"cmd": "uart_proxy_start", "port": port, "baud": baud, "dtr": bool(dtr),
                   "rts": bool(rts), "parity": parity, "stop_bits": stop_bits}
            if data_bits is not None:
                req["data_bits"] = data_bits
            return self._raw_handshake(req, "uart_proxy_start", "uart")
        req = {"cmd": "uart_proxy_start", "rx": rx, "tx": tx, "baud": baud}
        # Only a non-8N1 frame adds the fields (firmware 3.10.8+ / gateware v56 take them).
        if parity != "none":
            req["parity"] = parity
        if stop_bits != 1:
            req["stop_bits"] = stop_bits
        # One wire and the boot-mode entry (gateware v57), likewise only when asked for.
        if half_duplex:
            req["half_duplex"] = True
        if entry is not None:
            req["entry"] = entry.to_request()
        # RS-485 DE driven by the pod (gateware v58), only when asked for.
        if de is not None:
            req["de"] = de
            if de_active_low:
                req["de_active_low"] = True
        return self._raw_handshake(req, "uart_proxy_start", "uart")

    def close(self) -> None:
        # Nothing persistent is held; connections are per-command.
        return None
