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

import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional
from urllib.parse import quote

from ..cloud_auth import DEFAULT_API_BASE, DEFAULT_AUDIENCE, USER_AGENT, mint_session_token
from ..errors import (
    ConnectionClosedError,
    ConnectionConfigError,
    TransportError,
    TransportTimeout,
    firmware_error,
)
from .base import request_timeout
from .tcp import DEFAULT_DIAL_TIMEOUT, TcpTransport

_RETRY_DELAYS = (0.5, 1.5)
_EDGE_ERRORS = {502, 503, 504, 520, 521, 522, 523, 524}
#: The server's cap on how long one command-channel request may wait for the pod
#: (benchpodCommandMaxTimeout).
_SERVER_COMMAND_MAX_TIMEOUT = 120.0
#: Renew a minted session token this many seconds before it expires.
_RENEW_MARGIN = 120.0

#: Largest raw payload of one client->pod WebSocket frame. The server forwards each client frame
#: as one ``tunnel.data`` message (base64url plus a ~70-byte JSON envelope) and the pod copies that
#: message into a 2048-byte buffer (firmware BP_CLOUD_RX_MAX): a frame over about 1.5 KB raw is
#: dropped without an error. 1024 raw bytes is 1366 base64 characters, well inside the limit.
TUNNEL_FRAME_MAX = 1024
#: Rate cap for one large client upload (``load_bin`` for replay/SPI staging), the same backstop the
#: server's own DAC upload uses (benchpodUploadBytesPerSec): the pod acknowledges TCP eagerly, so
#: without pacing a fast burst overruns its 16 KB receive buffer or the server's per-pod inbox
#: and the tunnel ends.
TUNNEL_UPLOAD_BYTES_PER_SEC = 128 * 1024


def _server_error(detail: str) -> str:
    """The ``error`` field of a server JSON error body, else the raw text."""
    try:
        return str(json.loads(detail).get("error") or detail)
    except (ValueError, AttributeError):
        return detail


def _device_problem(device: str, code: int, detail: str) -> Optional[Exception]:
    """A clear error for the device-level failures the server reports, or None."""
    msg = _server_error(detail)
    if code == 503 and "offline" in msg:
        return TransportError(
            f"BenchPod {device!r} is offline: it is not connected to embeddedci.com right now. "
            "Check that it is powered and on the network (its 'online' flag in the device list).")
    if code == 404 and "not found" in msg:
        return ConnectionConfigError(
            f"no cloud BenchPod named {device!r} in this organization ({msg}); list the devices "
            "to see the names")
    return None


def _not_forwarded(code: int, detail: str) -> bool:
    """The server refused before forwarding the command to the pod (another instance holds its
    connection), so a retry cannot run it twice."""
    return code == 503 and "retry shortly" in detail


def _edge_failure(code: int, detail: str) -> bool:
    """The Cloudflare edge failed to reach or hear back from the server. The command may or may
    not have reached the pod."""
    # The server answers offline/timeout with a JSON error, which a retry would only repeat.
    # A non-JSON body is the edge failing to reach the server.
    if code not in _EDGE_ERRORS:
        return False
    try:
        json.loads(detail)
    except ValueError:
        return True
    return False


def _redact(text: str, secrets: "tuple[str, ...]") -> str:
    """``text`` with every secret (a session token, say) replaced, for error messages."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]").replace(quote(secret, safe=""), "[redacted]")
    return text


#: Commands that only read state: repeating one after an edge error cannot change the pod or
#: the DUT. The firmware's T0 tier (cmd_tier.c) without the reads that consume or take the
#: capture hardware (can_read drains the receive queue).
_READ_ONLY_COMMANDS = frozenset({
    "ping", "status", "cloud_status", "wifi_status", "la_pins", "usb_cc", "target_status",
    "power_status", "identity_public", "spi_status", "sensor_status", "can_status", "ota_status",
    "blob_status", "dac_loop_probe", "sensor_regs",
})


def _read_form(req: dict) -> bool:
    """The read form of a verb that also writes (the firmware's mixed verbs, plus la_voltage)."""
    cmd = req.get("cmd")
    if cmd == "la_voltage":
        return req.get("mv") is None
    if cmd == "dac_limits":
        return "path" not in req and req.get("enabled") is not False
    if cmd == "calibrate":
        return "source" not in req and req.get("clear") is not True
    if cmd in ("sig_policy", "lan_policy"):
        return "set" not in req
    if cmd in ("cloud_ca", "cloud_proxy"):
        return "set" not in req and req.get("clear") is not True
    return False


def _repeatable(req: dict) -> bool:
    # An edge error does not prove the pod never ran the command, so only a command that reads
    # is repeated. Anything else (an SPI transfer, a waveform, a delayed power change, a CAN
    # frame, a reset pulse) could happen twice.
    return req.get("cmd") in _READ_ONLY_COMMANDS or _read_form(req)


def _ws_exception(name: str) -> type:
    """A websocket-client exception class, or one nothing raises when the extra is missing."""
    try:
        import websocket
    except ImportError:  # pragma: no cover - exercised only without the extra
        return type(name, (Exception,), {})
    return getattr(websocket, name, type(name, (Exception,), {}))


class _WsTunnelSocket:
    """Adapts a WebSocket tunnel to the subset of the socket API the TCP transport uses.

    The server carries device→client bytes as binary WS frames; ``recv`` buffers one frame and
    serves it out in ``n``-byte slices. ``recv`` returns ``b""`` on close/EOF so the line readers
    treat a dropped tunnel the same as a closed socket, and raises :class:`TimeoutError` when no
    frame came within the timeout, like a socket, so a slow pod is not reported as a closed one.
    """

    #: Clock and sleep used for upload pacing (tests swap them).
    _clock = staticmethod(time.monotonic)
    _sleep = staticmethod(time.sleep)

    def __init__(self, url: str, timeout: float, headers: "list[str] | None" = None,
                 secrets: "tuple[str, ...]" = ()) -> None:
        try:
            import websocket  # websocket-client (optional extra: embeddedci[cloud])
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise TransportError(
                "the 'embeddedci' destination needs the cloud extra: pip install 'embeddedci[cloud]'"
            ) from exc
        try:
            # Send an explicit User-Agent on the WS upgrade too — the edge (Cloudflare) bans the
            # default Python/websocket-client signature (HTTP 403, error 1010).
            self._ws = websocket.create_connection(
                url, timeout=timeout, enable_multithread=True,
                header=[f"User-Agent: {USER_AGENT}", *(headers or [])],
            )
        except Exception as exc:
            text = str(exc)
            safe = _redact(text, secrets)
            err = TransportError(f"could not open cloud tunnel: {safe}")
            # websocket-client's WebSocketBadStatusException carries the HTTP status of the upgrade.
            err.status = getattr(exc, "status_code", None)  # type: ignore[attr-defined]
            err.body = getattr(exc, "resp_body", None)  # type: ignore[attr-defined]
            if safe != text:  # the original would show the token in a traceback
                raise err from None
            raise err from exc
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
            except (TimeoutError, _ws_exception("WebSocketTimeoutException")) as exc:
                raise TimeoutError(f"no data from the cloud tunnel ({exc})") from exc
            except _ws_exception("WebSocketConnectionClosedException"):
                return b""
            except OSError:
                raise
            except Exception as exc:
                raise ConnectionClosedError(f"the cloud tunnel failed: {exc}") from exc
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
        """Send ``data`` as WebSocket binary frames of at most :data:`TUNNEL_FRAME_MAX` bytes, in
        order. A write larger than one frame (a ``load_bin`` upload) is paced to
        :data:`TUNNEL_UPLOAD_BYTES_PER_SEC`; small writes (commands, DAP packets) go out at once."""
        view = memoryview(bytes(data))
        start = self._clock()
        for off in range(0, len(view), TUNNEL_FRAME_MAX):
            if off:
                behind = start + off / TUNNEL_UPLOAD_BYTES_PER_SEC - self._clock()
                if behind > 0:
                    self._sleep(behind)
            try:
                self._ws.send_binary(bytes(view[off:off + TUNNEL_FRAME_MAX]))
            except (TimeoutError, _ws_exception("WebSocketTimeoutException")) as exc:
                raise TimeoutError(f"the cloud tunnel did not take the data ({exc})") from exc
            except _ws_exception("WebSocketConnectionClosedException") as exc:
                raise ConnectionClosedError("the cloud tunnel closed while sending") from exc

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
        api_key: "str | None" = None,
        user_token: "Callable[[], str] | None" = None,
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
        # A token given by the caller cannot be renewed; one this transport minted can.
        self._renewable = token is None
        self._expires_at: Optional[float] = None
        self._token_lock = threading.Lock()  # the lease heartbeat thread reads the token too
        # Credentials for minting a session token outside GitHub Actions: an API key, or a callable
        # returning a logged-in user's access token (e.g. the `benchpod login` session). Without
        # either, the cloud destination can only authenticate via Actions OIDC, i.e. only inside CI.
        self.api_key = api_key
        self.user_token = user_token
        # Set by BenchPod once it holds a device lease; sent on tunnel/command requests so the server
        # confirms this client is the lease holder (and lets concurrent runs serialize). None = none.
        self.lease_id: "str | None" = None

    def _session_token(self) -> str:
        """The cloud session token, minted on first use and renewed shortly before it expires, so
        a connection held for longer than the token's lifetime keeps working."""
        with self._token_lock:
            due = (self._renewable and self._expires_at is not None
                   and time.time() >= self._expires_at - _RENEW_MARGIN)
            if not self._token or due:
                self._token, self._expires_at = mint_session_token(
                    self.api_base, self.audience, self.api_key, self.user_token)
            return self._token

    def _invalidate_token(self) -> bool:
        """Drop a token the server rejected so the next request mints a fresh one. Returns False
        when the token came from the caller and cannot be renewed."""
        if not self._renewable:
            return False
        with self._token_lock:
            self._token = None
            self._expires_at = None
        return True

    def _ws_url(self) -> str:
        """The tunnel URL. It carries no credentials: those go in :meth:`_ws_headers`, so the
        token never reaches an access log or an error message."""
        base = self.api_base
        if base.startswith("https://"):
            ws_base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            ws_base = "ws://" + base[len("http://"):]
        else:
            ws_base = base
        return f"{ws_base}/api/cloud/devices/ws?device={quote(self.device_name, safe='')}"

    def _ws_headers(self, token: str) -> "list[str]":
        """Headers for the tunnel's WebSocket upgrade: the session token as a Bearer header
        (the server's requireGithubActionToken reads it before the ``?token=`` fallback) and the
        lease, if any."""
        headers = [f"Authorization: Bearer {token}"]
        if self.lease_id:
            headers.append(f"X-Benchpod-Lease: {self.lease_id}")
        return headers

    def _open_tunnel(self) -> _WsTunnelSocket:
        token = self._session_token()
        return _WsTunnelSocket(self._ws_url(), self.timeout, headers=self._ws_headers(token),
                               secrets=(token,))

    def _dial(self, timeout: Optional[float] = None) -> _WsTunnelSocket:  # type: ignore[override]
        try:
            sock = self._open_tunnel()
        except TransportError as exc:
            status = getattr(exc, "status", None)
            if status == 401 and self._invalidate_token():
                sock = self._open_tunnel()
            else:
                body = getattr(exc, "body", None) or b""
                if isinstance(body, bytes):
                    body = body.decode("utf-8", "replace")
                problem = _device_problem(self.device_name, status or 0, body)
                if problem is not None:
                    raise problem from exc
                raise
        sock.settimeout(self.timeout if timeout is None else timeout)
        return sock

    def command(self, req: dict) -> Any:  # type: ignore[override]
        """Run one non-streaming command over the cloud *command channel*
        (``POST /api/cloud/devices/command``) instead of dialing a byte tunnel.

        The firmware services the command channel (``command.request``) and the
        byte tunnel as independent connections, so this works **while a streaming
        session holds a tunnel** — e.g. powering the target with
        :meth:`~embeddedci.benchpod.client.BenchPod.power_on` during an
        :meth:`~embeddedci.benchpod.client.BenchPod.open_uart` session. It is also
        faster than dialing a fresh WebSocket per command. Streaming modes
        (``dap_start``/``uart_proxy_start``) still use the tunnel via ``_dial``.
        """
        url = (f"{self.api_base}/api/cloud/devices/command"
               f"?device={quote(self.device_name, safe='')}")
        timeout = min(request_timeout(req, self.timeout), _SERVER_COMMAND_MAX_TIMEOUT)
        body = json.dumps(
            {"command": req, "timeout_ms": int(timeout * 1000)}
        ).encode("utf-8")
        cmd = req.get("cmd")
        attempt = 0
        renewed = False
        while True:
            request = urllib.request.Request(url, data=body, method="POST")
            request.add_header("Content-Type", "application/json")
            request.add_header("Authorization", f"Bearer {self._session_token()}")
            request.add_header("Accept", "application/json")
            request.add_header("User-Agent", USER_AGENT)
            if self.lease_id:
                request.add_header("X-Benchpod-Lease", self.lease_id)
            try:
                with urllib.request.urlopen(request, timeout=timeout + 10) as resp:
                    raw = resp.read()
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                # The server rejects the token before running anything, so a retry is safe.
                if exc.code == 401 and not renewed and self._invalidate_token():
                    renewed = True
                    continue
                if attempt < len(_RETRY_DELAYS) and (
                        _not_forwarded(exc.code, detail)
                        or (_edge_failure(exc.code, detail) and _repeatable(req))):
                    time.sleep(_RETRY_DELAYS[attempt])
                    attempt += 1
                    continue
                problem = _device_problem(self.device_name, exc.code, detail)
                if problem is not None:
                    raise problem from exc
                raise TransportError(
                    f"cloud command {cmd!r} failed (HTTP {exc.code}): {detail}"
                ) from exc
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, TimeoutError):
                    raise TransportTimeout(
                        f"cloud command {cmd!r}: no answer within {timeout + 10:g} s") from exc
                raise TransportError(f"cloud command {cmd!r} failed: {exc}") from exc
            except TimeoutError as exc:  # while reading the response
                raise TransportTimeout(
                    f"cloud command {cmd!r}: no answer within {timeout + 10:g} s") from exc
            except (OSError, http.client.HTTPException) as exc:
                raise ConnectionClosedError(
                    f"cloud command {cmd!r}: the connection to the server was lost ({exc})") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise TransportError(f"cloud command {cmd!r}: malformed reply from the server") from exc
        if not isinstance(payload, dict):
            raise TransportError(f"cloud command {cmd!r}: unexpected reply from the server")
        if payload.get("status") == "error":
            raise firmware_error(payload.get("error") or "device returned an error", cmd=cmd)
        return payload.get("data")

    def close(self) -> None:
        # Each command/raw session owns its own tunnel; nothing persistent to release.
        return None
