"""Tests for the embeddedci cloud destination: connection parsing, OIDC minting errors, and the
WS-tunnel transport reusing the TCP protocol logic. No network or websocket-client required — the
WS is faked."""

from __future__ import annotations

import pytest

from embeddedci.benchpod import cloud_auth
from embeddedci.benchpod.connection import parse_connection, resolve_connection
from embeddedci.benchpod.errors import CloudAuthError, ConnectionConfigError
from embeddedci.benchpod.transport import open_transport
from embeddedci.benchpod.transport.cloud import CloudTransport, _WsTunnelSocket


# -- connection parsing -----------------------------------------------------

def test_parse_embeddedci_connection():
    spec = parse_connection("embeddedci:my-bench-01")
    assert spec.is_cloud()
    assert spec.device_name == "my-bench-01"
    assert spec.kind == "embeddedci"


def test_parse_embeddedci_requires_name():
    with pytest.raises(ConnectionConfigError):
        parse_connection("embeddedci:")


def test_resolve_and_open_cloud_transport():
    spec = resolve_connection("embeddedci:dev-a")
    t = open_transport(spec, api_base="https://example.test", token="sess-tok")
    assert isinstance(t, CloudTransport)
    assert t.device_name == "dev-a"
    assert t.api_base == "https://example.test"


def test_cloud_ws_url():
    t = CloudTransport("dev-a", api_base="https://example.test", token="abc")
    url = t._ws_url()
    assert url == "wss://example.test/api/cloud/devices/ws?device=dev-a"


def _fake_websocket_module(monkeypatch, fail=None):
    """Install a fake ``websocket`` module; returns the list of create_connection calls."""
    import sys
    import types

    calls = []

    def create_connection(url, **kwargs):
        calls.append((url, kwargs))
        if fail is not None:
            raise fail
        return _FakeWS([])

    monkeypatch.setitem(sys.modules, "websocket",
                        types.SimpleNamespace(create_connection=create_connection))
    return calls


def test_tunnel_sends_the_token_as_a_bearer_header_not_in_the_url(monkeypatch):
    calls = _fake_websocket_module(monkeypatch)
    t = CloudTransport("dev-a", api_base="https://example.test", token="sekrit-tok")
    t.lease_id = "lease-1"
    t._dial()
    (url, kwargs), = calls
    assert url == "wss://example.test/api/cloud/devices/ws?device=dev-a"
    assert "sekrit-tok" not in url and "lease" not in url
    assert "Authorization: Bearer sekrit-tok" in kwargs["header"]
    assert "X-Benchpod-Lease: lease-1" in kwargs["header"]
    assert any(h.startswith("User-Agent: ") for h in kwargs["header"])


def test_tunnel_error_never_shows_the_token(monkeypatch):
    from embeddedci.benchpod.errors import TransportError

    # An exception text that echoes the request (as a proxy or a debug build might).
    _fake_websocket_module(monkeypatch, fail=OSError(
        "handshake failed: Authorization: Bearer sekrit/tok+1 url=?token=sekrit%2Ftok%2B1"))
    t = CloudTransport("dev-a", api_base="https://example.test", token="sekrit/tok+1")
    with pytest.raises(TransportError) as info:
        t._dial()
    assert "sekrit" not in str(info.value)
    assert "[redacted]" in str(info.value)
    assert info.value.__cause__ is None  # the unredacted original is not chained


# -- OIDC minting: the three distinct error reasons -------------------------

def test_mint_oidc_not_in_github_action(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", raising=False)
    with pytest.raises(CloudAuthError, match="not running inside a GitHub Action"):
        cloud_auth.mint_oidc_token()


def test_mint_oidc_missing_id_token_permission(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", raising=False)
    with pytest.raises(CloudAuthError, match="id-token"):
        cloud_auth.mint_oidc_token()


def test_mint_oidc_request_failure(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://example.test/token?foo=1")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "req-tok")

    def boom(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(cloud_auth.urllib.request, "urlopen", boom)
    with pytest.raises(CloudAuthError, match="failed to request a GitHub OIDC token"):
        cloud_auth.mint_oidc_token()


# -- transport reuse over a fake WS tunnel ----------------------------------

class _FakeSock:
    """A socket-like double that returns a canned reply line for one command."""

    def __init__(self, reply: bytes) -> None:
        self._reply = bytearray(reply)
        self.sent = bytearray()

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def recv(self, n: int) -> bytes:
        out = bytes(self._reply[:n])
        del self._reply[:n]
        return out

    def settimeout(self, _t):
        pass

    def setsockopt(self, *_a):
        pass

    def shutdown(self, _h):
        pass

    def close(self):
        pass


def test_cloud_transport_command_uses_command_channel(monkeypatch):
    # Non-streaming commands go over the HTTP command channel (not a byte tunnel),
    # so they work while a streaming session holds a tunnel.
    import urllib.request

    t = CloudTransport("dev-a", api_base="https://example.test", token="x")
    captured = {}

    class _Resp:
        def __init__(self, body):
            self._b = body

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = request.data
        captured["auth"] = request.get_header("Authorization")
        return _Resp(b'{"status":"ok","data":"pong"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert t.command({"cmd": "ping"}) == "pong"
    assert "/api/cloud/devices/command" in captured["url"]
    assert "device=dev-a" in captured["url"]
    assert captured["auth"] == "Bearer x"
    assert b'"ping"' in captured["body"]


def test_cloud_transport_command_error_raises(monkeypatch):
    import urllib.request

    from embeddedci.benchpod.errors import FirmwareError

    t = CloudTransport("dev-a", api_base="https://example.test", token="x")

    class _Resp:
        def read(self):
            return b'{"status":"error","error":"swd busy"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    with pytest.raises(FirmwareError):
        t.command({"cmd": "dap_start"})


def test_cloud_transport_dap_handshake_returns_raw_link(monkeypatch):
    # dap_start acks one JSON line, then the same tunnel carries raw bytes.
    t = CloudTransport("dev-a", api_base="https://example.test", token="x")
    fake = _FakeSock(b'{"status":"ok"}\nRAWBYTES')
    monkeypatch.setattr(t, "_dial", lambda: fake)

    link = t.dap_start(swclk=11, swdio=12)
    # Bytes after the ack newline are the raw CMSIS-DAP stream and must survive.
    assert link.read(8) == b"RAWBYTES"
    link.write(b"ping")
    assert bytes(fake.sent).endswith(b"ping")
    link.close()


# -- WS socket adapter buffering --------------------------------------------

class _FakeWS:
    def __init__(self, frames):
        self._frames = list(frames)

    def recv(self):
        return self._frames.pop(0) if self._frames else b""

    def send_binary(self, data):
        self.last = data
        self.frames = getattr(self, "frames", [])
        self.frames.append(bytes(data))

    def settimeout(self, _t):
        pass

    def close(self):
        pass


def test_ws_tunnel_socket_buffers_and_eofs():
    sock = _WsTunnelSocket.__new__(_WsTunnelSocket)
    sock._ws = _FakeWS([b"hello", b"world"])
    sock._buf = bytearray()
    sock._closed = False

    assert sock.recv(3) == b"hel"
    assert sock.recv(100) == b"lo"  # rest of first frame
    assert sock.recv(5) == b"world"
    assert sock.recv(5) == b""  # EOF


def _sending_socket(monkeypatch):
    """A tunnel socket over a fake WS with a fake clock: sleeps advance it."""
    from embeddedci.benchpod.transport import cloud

    clock = [100.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock[0] += s

    monkeypatch.setattr(cloud._WsTunnelSocket, "_clock", staticmethod(lambda: clock[0]))
    monkeypatch.setattr(cloud._WsTunnelSocket, "_sleep", staticmethod(sleep))
    sock = _WsTunnelSocket.__new__(_WsTunnelSocket)
    sock._ws = _FakeWS([])
    sock._buf = bytearray()
    sock._closed = False
    return sock, sleeps


def test_ws_tunnel_socket_splits_a_large_upload_into_small_frames_in_order(monkeypatch):
    from embeddedci.benchpod.transport import cloud

    sock, sleeps = _sending_socket(monkeypatch)
    payload = bytes(i * 7 % 251 for i in range(10 * 1024))
    sock.sendall(payload)
    frames = sock._ws.frames
    assert [len(f) for f in frames] == [1024] * 10
    assert all(len(f) <= cloud.TUNNEL_FRAME_MAX for f in frames)
    assert b"".join(frames) == payload
    # base64url of one frame plus the server's tunnel.data envelope fits the pod's 2048-byte buffer.
    import base64
    import json
    env = json.dumps({"type": "tunnel.data", "tunnel_id": "x" * 36,
                      "data_b64": base64.urlsafe_b64encode(frames[0]).decode().rstrip("=")})
    assert len(env) < 1600
    # Paced to the upload rate: 10 KiB at 128 KiB/s takes about 70 ms after the first frame.
    assert len(sleeps) == 9
    assert sum(sleeps) == pytest.approx(9 * 1024 / cloud.TUNNEL_UPLOAD_BYTES_PER_SEC)


def test_ws_tunnel_socket_sends_a_small_write_as_one_frame_without_waiting(monkeypatch):
    sock, sleeps = _sending_socket(monkeypatch)
    sock.sendall(b'{"cmd":"ping"}\n')
    sock.sendall(bytearray(b"x" * 1024))
    sock.sendall(b"y" * 1025)
    assert [len(f) for f in sock._ws.frames] == [15, 1024, 1024, 1]
    assert sleeps == [pytest.approx(1024 / (128 * 1024))]


def test_cloud_load_replay_skips_the_tunnel_ack_lines(monkeypatch):
    """Over a cloud tunnel the pod interleaves {"ack":N} progress lines during load_bin; the
    completion line, not the first ack, ends the upload."""
    sock, _ = _sending_socket(monkeypatch)
    sock._ws = _FakeWS([
        b'{"status":"ok","data":{"ready":20000}}\n',
        b'{"ack":8192}\n{"ack":16384}\n',
        b'{"status":"ok","data":{"total":20000}}\n',
        b'{"status":"ok","data":{"replaying":true}}\n',
    ])
    t = CloudTransport("dev-a", api_base="https://example.test", token="abc")
    monkeypatch.setattr(t, "_dial", lambda: sock)
    data = t.load_replay(data=b"\x00" * 20000, replay={"cmd": "replay"}, psram=True)
    assert data == {"replaying": True}
    frames = sock._ws.frames
    import json
    assert json.loads(frames[0])["cmd"] == "load_bin"
    assert json.loads(frames[-1]) == {"cmd": "replay"}
    assert b"".join(frames[1:-1]) == b"\x00" * 20000


def _edge_error(code, body):
    import io
    import urllib.error

    return urllib.error.HTTPError("https://example.test", code, "err", {}, io.BytesIO(body))


def _scripted_urlopen(monkeypatch, outcomes):
    import urllib.request

    from embeddedci.benchpod.transport import cloud

    calls = []

    class _Resp:
        def read(self):
            return b'{"status":"ok","data":"pong"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(request, timeout=None):
        calls.append(request)
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(cloud.time, "sleep", lambda s: None)
    return calls


def test_cloud_command_retries_cloudflare_502(monkeypatch):
    t = CloudTransport("dev-a", api_base="https://example.test", token="x")
    calls = _scripted_urlopen(monkeypatch, [_edge_error(502, b"<html>Bad gateway</html>"), None])
    assert t.command({"cmd": "target_status"}) == "pong"
    assert len(calls) == 2


@pytest.mark.parametrize("req", [
    {"cmd": "la_voltage"},
    {"cmd": "dac_limits"},
    {"cmd": "calibrate"},
    {"cmd": "lan_policy"},
    {"cmd": "cloud_proxy"},
])
def test_cloud_command_retries_the_read_form_of_a_mixed_verb(monkeypatch, req):
    t = CloudTransport("dev-a", api_base="https://example.test", token="x")
    calls = _scripted_urlopen(monkeypatch, [_edge_error(502, b"<html></html>"), None])
    assert t.command(req) == "pong"
    assert len(calls) == 2


def test_cloud_command_retries_a_write_the_server_never_forwarded(monkeypatch):
    t = CloudTransport("dev-a", api_base="https://example.test", token="x")
    body = b'{"error":"device is connected to another server instance; retry shortly"}'
    calls = _scripted_urlopen(monkeypatch, [_edge_error(503, body), None])
    assert t.command({"cmd": "spi_xfer", "tx": "9f"}) == "pong"
    assert len(calls) == 2


def test_cloud_command_does_not_retry_server_timeout(monkeypatch):
    from embeddedci.benchpod.errors import TransportError

    t = CloudTransport("dev-a", api_base="https://example.test", token="x")
    calls = _scripted_urlopen(
        monkeypatch, [_edge_error(504, b'{"error":"device did not respond in time"}')])
    with pytest.raises(TransportError, match="HTTP 504"):
        t.command({"cmd": "status"})
    assert len(calls) == 1


def test_cloud_command_retries_other_instance(monkeypatch):
    t = CloudTransport("dev-a", api_base="https://example.test", token="x")
    body = b'{"error":"device is connected to another server instance; retry shortly"}'
    calls = _scripted_urlopen(monkeypatch, [_edge_error(503, body), None])
    assert t.command({"cmd": "status"}) == "pong"
    assert len(calls) == 2


@pytest.mark.parametrize("req", [
    {"cmd": "can_write", "id": 1, "data": "00"},
    {"cmd": "nrst", "pulse_ms": 10},
    {"cmd": "la", "la": 1, "steps": 5, "delay_us": 100},
    {"cmd": "spi_xfer", "tx": "9f"},
    {"cmd": "generate", "waveform": "sine"},
    {"cmd": "target_power", "efuse": 1, "state": 1, "delay_ms": 500},
    {"cmd": "dac_stop"},
    {"cmd": "la_voltage", "mv": 3300},
    {"cmd": "dac_limits", "path": "dac_out", "min_mv": 0},
    {"cmd": "calibrate", "source": "current_in"},
    {"cmd": "lan_policy", "set": "open"},
    {"cmd": "cloud_ca", "clear": True},
    {"cmd": "can_read"},
    {"cmd": "some_future_command"},
])
def test_cloud_command_never_repeats_target_actions(monkeypatch, req):
    from embeddedci.benchpod.errors import TransportError

    t = CloudTransport("dev-a", api_base="https://example.test", token="x")
    calls = _scripted_urlopen(monkeypatch, [_edge_error(502, b"<html></html>")])
    with pytest.raises(TransportError):
        t.command(req)
    assert len(calls) == 1


def test_cloud_command_gives_up_after_two_retries(monkeypatch):
    from embeddedci.benchpod.errors import TransportError

    t = CloudTransport("dev-a", api_base="https://example.test", token="x")
    calls = _scripted_urlopen(monkeypatch, [_edge_error(502, b"<html></html>")] * 3)
    with pytest.raises(TransportError, match="HTTP 502"):
        t.command({"cmd": "status"})
    assert len(calls) == 3


# -- session-token renewal and device errors -------------------------------------------------

def _minting(monkeypatch, expires_in=3600.0):
    """Replace mint_session_token with a counter that hands out tok1, tok2, …"""
    import time as _time

    from embeddedci.benchpod.transport import cloud

    minted = []

    def fake_mint(api_base, audience, api_key, user_token):
        minted.append(user_token() if user_token else api_key)
        return f"tok{len(minted)}", _time.time() + expires_in

    monkeypatch.setattr(cloud, "mint_session_token", fake_mint)
    return minted


def test_user_token_is_exchanged_as_bearer(monkeypatch):
    import io
    import json
    import urllib.request

    seen = {}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(request, timeout=None):
        seen["auth"] = request.get_header("Authorization")
        seen["url"] = request.full_url
        return _Resp(json.dumps({"token": "cloud", "expires_at": "2030-01-01T00:00:00Z"}).encode())

    monkeypatch.delenv("BENCHPOD_API_KEY", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    token, expires_at = cloud_auth.mint_session_token("https://example.test", user_token=lambda: "user-jwt")
    assert token == "cloud" and seen == {"auth": "Bearer user-jwt", "url": "https://example.test/api/auth/token"}
    assert expires_at == pytest.approx(1893456000.0)


def test_session_token_is_renewed_before_it_expires(monkeypatch):
    minted = _minting(monkeypatch, expires_in=60.0)  # already inside the renewal margin
    t = CloudTransport("dev-a", api_base="https://example.test", user_token=lambda: "u")
    assert t._session_token() == "tok1"
    assert t._session_token() == "tok2"
    assert minted == ["u", "u"]


def test_session_token_is_cached_while_valid(monkeypatch):
    _minting(monkeypatch)
    t = CloudTransport("dev-a", api_base="https://example.test", user_token=lambda: "u")
    assert t._session_token() == t._session_token() == "tok1"


def test_cloud_command_renews_a_rejected_token_once(monkeypatch):
    _minting(monkeypatch)
    t = CloudTransport("dev-a", api_base="https://example.test", user_token=lambda: "u")
    calls = _scripted_urlopen(monkeypatch, [_edge_error(401, b'{"error":"invalid session token"}'), None])
    assert t.command({"cmd": "status"}) == "pong"
    assert [c.get_header("Authorization") for c in calls] == ["Bearer tok1", "Bearer tok2"]


def test_cloud_command_does_not_renew_a_caller_token(monkeypatch):
    from embeddedci.benchpod.errors import TransportError

    t = CloudTransport("dev-a", api_base="https://example.test", token="given")
    calls = _scripted_urlopen(monkeypatch, [_edge_error(401, b'{"error":"invalid session token"}')])
    with pytest.raises(TransportError, match="HTTP 401"):
        t.command({"cmd": "status"})
    assert len(calls) == 1


def test_cloud_command_reports_an_offline_pod(monkeypatch):
    from embeddedci.benchpod.errors import TransportError

    t = CloudTransport("dev-a", api_base="https://example.test", token="x")
    _scripted_urlopen(monkeypatch, [_edge_error(503, b'{"error":"device is not reachable (offline)"}')])
    with pytest.raises(TransportError, match="'dev-a' is offline"):
        t.command({"cmd": "status"})


def test_cloud_command_reports_an_unknown_pod(monkeypatch):
    t = CloudTransport("nope", api_base="https://example.test", token="x")
    _scripted_urlopen(monkeypatch, [_edge_error(404, b'{"error":"device not found: nope"}')])
    with pytest.raises(ConnectionConfigError, match="no cloud BenchPod named 'nope'"):
        t.command({"cmd": "status"})


def test_tunnel_renews_a_rejected_token(monkeypatch):
    from embeddedci.benchpod.errors import TransportError
    from embeddedci.benchpod.transport import cloud

    _minting(monkeypatch)
    urls = []

    class _Sock:
        def settimeout(self, t):
            pass

    def fake_socket(url, timeout, headers=None, secrets=()):
        urls.append(headers[0])
        if len(urls) == 1:
            err = TransportError("could not open cloud tunnel: Handshake status 401")
            err.status = 401
            raise err
        return _Sock()

    monkeypatch.setattr(cloud, "_WsTunnelSocket", fake_socket)
    t = CloudTransport("dev-a", api_base="https://example.test", user_token=lambda: "u")
    t._dial()
    assert urls == ["Authorization: Bearer tok1", "Authorization: Bearer tok2"]


@pytest.mark.parametrize("user_token, expected", [(lambda: "user-jwt", "Bearer user-jwt"), (None, "Bearer sess")])
def test_server_api_prefers_the_user_token(monkeypatch, user_token, expected):
    # The device and waveform routes take a user; a cloud session token is only for driving the pod.
    from embeddedci.benchpod import BenchPod

    monkeypatch.delenv("BENCHPOD_API_KEY", raising=False)
    monkeypatch.delenv("BENCHPOD_LA_VOLTAGE", raising=False)
    t = CloudTransport("dev-a", api_base="https://example.test", token="sess")
    bp = BenchPod(transport=t, cloud_user_token=user_token, lease=False)
    assert bp._try_server_api()._auth_header() == expected
