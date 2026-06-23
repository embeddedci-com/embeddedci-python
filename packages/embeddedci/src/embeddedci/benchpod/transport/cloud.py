"""Cloud transport: drive a named device through embeddedci.com.

The device sits behind NAT and connects out to the embeddedci server over a WebSocket. The server
bridges a *raw byte tunnel* between this client and the device, and the firmware feeds those bytes
through the same protocol state machine a local TCP client would hit. So the full protocol — JSON
commands AND the raw SWD/UART modes used for flashing and captures — works unchanged.

Implementation: :class:`CloudTransport` reuses every protocol method of :class:`TcpTransport` and
only overrides :meth:`_dial` to hand back a WebSocket-backed object that quacks like a socket
(``recv``/``sendall``/``settimeout``/``close``). Each "dial" opens a fresh tunnel WS, mirroring the
TCP transport's one-connection-per-command model.
"""

from __future__ import annotations

from urllib.parse import quote

from ..cloud_auth import DEFAULT_API_BASE, DEFAULT_AUDIENCE, get_session_token
from ..errors import TransportError
from .tcp import DEFAULT_DIAL_TIMEOUT, TcpTransport


class _WsTunnelSocket:
    """Adapts a WebSocket tunnel to the subset of the socket API the TCP transport uses.

    The server carries device→client bytes as binary WS frames; ``recv`` buffers one frame and
    serves it out in ``n``-byte slices. ``recv`` returns ``b""`` on close/EOF so the line readers
    treat a dropped tunnel the same as a closed socket.
    """

    def __init__(self, url: str, timeout: float) -> None:
        try:
            import websocket  # websocket-client (optional extra: embeddedci[cloud])
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise TransportError(
                "the 'embeddedci' destination needs the cloud extra: pip install 'embeddedci[cloud]'"
            ) from exc
        try:
            self._ws = websocket.create_connection(url, timeout=timeout, enable_multithread=True)
        except Exception as exc:
            raise TransportError(f"could not open cloud tunnel: {exc}") from exc
        self._buf = bytearray()
        self._closed = False

    def settimeout(self, t) -> None:  # noqa: ANN001 - mirrors socket.settimeout
        self._ws.settimeout(t)

    def setsockopt(self, *_args) -> None:
        # TCP_NODELAY etc. — irrelevant over a WS; the TCP transport's _dial sets these but we
        # override _dial, so this exists only for defensive parity.
        return None

    def recv(self, n: int) -> bytes:
        if not self._buf:
            try:
                msg = self._ws.recv()
            except Exception:
                return b""
            if not msg:
                return b""
            if isinstance(msg, str):
                msg = msg.encode("utf-8")
            self._buf.extend(msg)
            if not self._buf:
                return b""
        take = bytes(self._buf[:n])
        del self._buf[:n]
        return take

    def sendall(self, data: bytes) -> None:
        self._ws.send_binary(bytes(data))

    def shutdown(self, _how) -> None:
        return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._ws.close()
        except Exception:
            pass


class CloudTransport(TcpTransport):
    """Drive a named device through the embeddedci cloud (full protocol over a WS tunnel)."""

    def __init__(
        self,
        device_name: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        token: "str | None" = None,
        audience: str = DEFAULT_AUDIENCE,
        timeout: float = 30.0,
    ) -> None:
        if not device_name:
            raise TransportError("the embeddedci destination requires a device name")
        # Intentionally do NOT call TcpTransport.__init__ (it requires a host:port). Set the fields
        # the inherited protocol methods read.
        self.device_name = device_name
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self.audience = audience
        self.timeout = timeout
        self.addr = ""  # unused; the inherited _split_addr is never called
        self.dial_timeout = DEFAULT_DIAL_TIMEOUT
        self._token = token

    def _session_token(self) -> str:
        if not self._token:
            self._token = get_session_token(self.api_base, self.audience)
        return self._token

    def _ws_url(self) -> str:
        base = self.api_base
        if base.startswith("https://"):
            ws_base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            ws_base = "ws://" + base[len("http://"):]
        else:
            ws_base = base
        token = self._session_token()
        return (
            f"{ws_base}/api/cloud/devices/ws"
            f"?device={quote(self.device_name, safe='')}&token={quote(token, safe='')}"
        )

    def _dial(self) -> _WsTunnelSocket:  # type: ignore[override]
        sock = _WsTunnelSocket(self._ws_url(), self.timeout)
        sock.settimeout(self.timeout)
        return sock

    def close(self) -> None:
        # Each command/raw session owns its own tunnel; nothing persistent to release.
        return None
