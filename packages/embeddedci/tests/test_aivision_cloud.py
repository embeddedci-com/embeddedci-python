"""embeddedci.aivision over embeddedci.com (``embeddedci:<name>``) against a fake server.

The fake answers the server's routes with the shapes embeddedci-server returns: the token
exchange, the device lease, the AI Vision proxy and registration.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from embeddedci.aivision import AIVision, AuthError, BusyError, TransportError, resolve_connection
from embeddedci.aivision import cloud

from test_aivision import FakeBoard

STATUS = {"device": "ai-vision-2", "version": "1.2.3", "public_key": "k" * 43}


class FakeServer(FakeBoard):
    """FakeBoard with full paths: the server's routes, the board's under /api/cloud/aivision/<name>."""

    def __init__(self) -> None:
        super().__init__()
        self.tokens = 0
        self.lease_holder = ""

        def token(req: Dict[str, Any]):
            self.tokens += 1
            return 200, {"token": f"sess-{self.tokens}", "expires_in": 3600}

        def lease(req: Dict[str, Any]):
            if self.lease_holder:
                return 409, {"busy": True, "holder": self.lease_holder, "expires_at": "2026-11-06T20:00:00Z",
                             "error": "pod is in use by " + self.lease_holder, "code": "lease_held"}
            return 200, {"lease_id": req["json"]["lease_id"], "ttl_seconds": 120}

        self.raw("POST /api/auth/token", fn=token)
        self.raw("POST /api/cloud/devices/lease", fn=lease)
        self.raw("POST /api/cloud/devices/lease/release", body={"released": True})
        self.raw("POST /api/cloud/devices/lease/renew", body={"renewed": True})

    @property
    def base(self) -> str:
        return "http://" + self.addr

    def raw(self, route: str, status: int = 200, body: Any = None, fn=None) -> None:
        self.routes[route] = fn or (lambda req: (status, body))

    def board(self, route: str, status: int = 200, body: Any = None, fn=None) -> None:
        method, path = route.split(" ", 1)
        self.raw(f"{method} /api/cloud/aivision/bench-1/api/v1{path}", status, body, fn)

    def last_raw(self, path: str) -> Dict[str, Any]:
        return [r for r in self.requests if r["path"] == path][-1]

    def paths(self, prefix: str = ""):
        return [r["path"] for r in self.requests if r["path"].startswith(prefix)]


@pytest.fixture
def server(monkeypatch):
    monkeypatch.delenv("BENCHPOD_API_KEY", raising=False)
    monkeypatch.delenv("BENCHPOD_API_BASE", raising=False)
    s = FakeServer()
    yield s
    s.close()


def test_cloud_name():
    assert cloud.cloud_name("embeddedci:bench-1") == "bench-1"
    assert cloud.cloud_name("  EmbeddedCI:bench-1 ") == "bench-1"
    assert cloud.cloud_name("embeddedci:") is None
    assert cloud.cloud_name("10.0.0.5") is None
    with pytest.raises(TransportError):
        resolve_connection("embeddedci:bench-1")


def test_pairing_server():
    assert cloud.pairing_server("https://www.embeddedci.com") == "api.embeddedci.com"
    assert cloud.pairing_server("http://127.0.0.1:18191") == "127.0.0.1:18191"


def test_calls_go_through_the_server_with_a_session_token(server):
    server.board("GET /status", body=STATUS)
    v = AIVision("embeddedci:bench-1", api_base=server.base, api_key="eci_test")
    assert repr(v) == "AIVision('embeddedci:bench-1')"
    assert v.status()["version"] == "1.2.3"
    exchange = server.last_raw("/api/auth/token")
    assert exchange["headers"]["Authorization"] == "ApiKey eci_test"
    call = server.last_raw("/api/cloud/aivision/bench-1/api/v1/status")
    assert call["headers"]["Authorization"] == "Bearer sess-1"
    assert call["headers"]["User-Agent"].startswith("embeddedci-python")
    v.status()
    assert server.tokens == 1  # minted once, reused


def test_a_refused_token_is_minted_again_once(server):
    calls = {"n": 0}

    def status(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return 401, {"error": "token expired"}
        return 200, STATUS

    server.board("GET /status", fn=status)
    v = AIVision("embeddedci:bench-1", api_base=server.base, api_key="eci_test")
    assert v.status()["device"] == "ai-vision-2"
    assert server.tokens == 2
    assert server.last_raw("/api/cloud/aivision/bench-1/api/v1/status")["headers"]["Authorization"] == "Bearer sess-2"


def test_lease_is_the_servers_device_lease(server):
    server.board("POST /tap", body={"tapped_at": [10, 20]})
    with AIVision("embeddedci:bench-1", api_base=server.base, api_key="eci_test", lease="ci-run") as v:
        lease_req = server.last_raw("/api/cloud/devices/lease")
        assert lease_req["query"] == "device=bench-1"
        assert lease_req["json"]["run_label"] == "ci-run"
        v.tap(10, 20)
        tap = server.last_raw("/api/cloud/aivision/bench-1/api/v1/tap")
        assert tap["headers"]["X-Benchpod-Lease"] == v.lease.id
        assert "X-Lease-Id" not in tap["headers"]
    assert "/api/cloud/devices/lease/release" in server.paths()
    assert not any(p.endswith("/api/v1/lease") for p in server.paths())


def test_busy_board(server):
    server.lease_holder = "github:repo/run-7"
    with pytest.raises(BusyError) as e:
        AIVision("embeddedci:bench-1", api_base=server.base, api_key="eci_test", lease="me")
    assert "AI Vision board 'bench-1' is in use by github:repo/run-7" in str(e.value)


def test_server_answers_map_to_errors(server):
    server.board("POST /tap", 409, {"busy": True, "holder": "web:ann", "expires_at": "2026-11-06T20:00:00Z",
                                    "error": "pod is in use by web:ann", "code": "lease_held"})
    server.board("GET /status", 503, {"error": "bench-1 is offline"})
    v = AIVision("embeddedci:bench-1", api_base=server.base, api_key="eci_test")
    with pytest.raises(BusyError) as busy:
        v.tap(1, 2)
    assert busy.value.detail["who"] == "web:ann"
    with pytest.raises(TransportError, match="offline"):
        v.status()


def test_no_credential_is_an_auth_error(server, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud, "login_token", lambda base: None)
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    server.board("GET /status", body=STATUS)
    v = AIVision("embeddedci:bench-1", api_base=server.base)
    with pytest.raises(AuthError, match="embeddedci.com sign-in"):
        v.status()


def test_register_cloud_registers_and_pairs(server):
    lan = FakeBoard()
    try:
        lan.on("GET /status", body=STATUS)
        lan.on("PUT /cloud", body={"paired": True})
        server.raw("POST /api/aivision/devices", 201, {"id": "dev-1", "name": "bench-1", "connection": "embeddedci:bench-1"})
        dev = AIVision(lan.addr).register_cloud("bench-1", api_base=server.base, api_key="eci_test")
        assert dev["connection"] == "embeddedci:bench-1"
        reg = server.last_raw("/api/aivision/devices")
        assert reg["headers"]["Authorization"] == "ApiKey eci_test"
        assert reg["json"] == {"name": "bench-1", "public_key": STATUS["public_key"]}
        assert lan.last("/cloud")["json"] == {"server": server.addr, "device_id": "dev-1"}
    finally:
        lan.close()


def test_register_cloud_conflict_is_reported(server):
    lan = FakeBoard()
    try:
        lan.on("GET /status", body=STATUS)
        server.raw("POST /api/aivision/devices", 409, {"error": "another device in this organization is already named \"bench-1\""})
        with pytest.raises(Exception, match="already named"):
            AIVision(lan.addr).register_cloud("bench-1", api_base=server.base, api_key="eci_test")
    finally:
        lan.close()

