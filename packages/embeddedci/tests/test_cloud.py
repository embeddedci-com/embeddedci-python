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
    assert url.startswith("wss://example.test/api/cloud/devices/ws?")
    assert "device=dev-a" in url
    assert "token=abc" in url


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


def test_cloud_transport_command_roundtrip(monkeypatch):
    t = CloudTransport("dev-a", api_base="https://example.test", token="x")
    fake = _FakeSock(b'{"status":"ok","data":"pong"}\n')
    monkeypatch.setattr(t, "_dial", lambda: fake)

    assert t.command({"cmd": "ping"}) == "pong"
    # The request was framed as a newline-terminated JSON line.
    assert fake.sent.endswith(b"\n")
    assert b'"cmd":"ping"' in bytes(fake.sent)


def test_cloud_transport_swd_handshake_returns_raw_link(monkeypatch):
    # swd_start acks one JSON line, then the same tunnel carries raw bytes.
    t = CloudTransport("dev-a", api_base="https://example.test", token="x")
    fake = _FakeSock(b'{"status":"ok"}\nRAWBYTES')
    monkeypatch.setattr(t, "_dial", lambda: fake)

    link = t.swd_start(swclk=11, swdio=12, nreset=3)
    # Bytes after the ack newline are the raw remote_bitbang stream and must survive.
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
