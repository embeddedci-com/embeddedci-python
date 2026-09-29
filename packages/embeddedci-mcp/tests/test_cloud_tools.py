"""cloud_list_devices and the `benchpod login` session it falls back to (no network: urlopen is faked)."""

from __future__ import annotations

import io
import json
import stat
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest
from mcp.server.fastmcp.exceptions import ToolError

import embeddedci.benchpod.server_api as server_api_mod
import embeddedci_mcp.cli_login as cli_login

from conftest import call

DEVICES = {"devices": [
    {"id": "d1", "name": "bench-1", "online": True, "last_active_at": "2026-09-29T10:00:00Z",
     "parameters": {"fw_version": "3.4.0"}},
    {"id": "d2", "name": "bench-2", "online": False, "parameters": {}},
]}


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeServer:
    """Answers the refresh and device-list endpoints, recording each request."""

    def __init__(self, *, refresh_status: int = 200):
        self.requests = []
        self.refresh_status = refresh_status

    def urlopen(self, req, timeout=None):
        body = json.loads(req.data) if req.data else None
        self.requests.append((req.get_method(), req.full_url, req.get_header("Authorization"), body))
        if req.full_url.endswith("/api/auth/guest/refresh"):
            if self.refresh_status != 200:
                raise urllib.error.HTTPError(req.full_url, self.refresh_status, "no", {},
                                             io.BytesIO(b'{"error":"refresh token revoked"}'))
            return _Resp(json.dumps({"access_token": "new-access", "access_expires_in": 3600,
                                     "refresh_token": "new-refresh", "refresh_expires_in": 604800,
                                     "user_id": "u1"}).encode())
        if req.full_url.endswith("/api/benchpod/devices"):
            return _Resp(json.dumps(DEVICES).encode())
        raise AssertionError(f"unexpected request {req.full_url}")


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("BENCHPOD_API_BASE", raising=False)
    fake = FakeServer()
    monkeypatch.setattr(cli_login.urllib.request, "urlopen", fake.urlopen)
    monkeypatch.setattr(server_api_mod.urllib.request, "urlopen", fake.urlopen)
    return fake


def _write_tokens(tmp_path, *, access_in: timedelta, refresh_in: timedelta):
    now = datetime.now(timezone.utc)
    path = tmp_path / "benchpod-cli" / "token.json"
    path.parent.mkdir()
    # Go writes nanoseconds; the reader must cope.
    fmt = lambda t: t.strftime("%Y-%m-%dT%H:%M:%S.123456789+00:00")  # noqa: E731
    path.write_text(json.dumps({
        "access_token": "old-access", "refresh_token": "old-refresh",
        "access_expires_at": fmt(now + access_in), "refresh_expires_at": fmt(now + refresh_in),
        "session_id": "s1", "user_id": "u1",
    }))
    return path


def test_lists_devices_with_the_cli_login(server, tmp_path):
    _write_tokens(tmp_path, access_in=timedelta(hours=1), refresh_in=timedelta(days=7))
    out = call("cloud_list_devices")
    assert out["auth"] == "benchpod_login"
    assert [d["connection"] for d in out["devices"]] == ["embeddedci:bench-1", "embeddedci:bench-2"]
    assert out["devices"][0]["online"] and out["devices"][0]["parameters"] == {"fw_version": "3.4.0"}
    assert server.requests == [("GET", "https://www.embeddedci.com/api/benchpod/devices",
                                "Bearer old-access", None)]


def test_expired_access_token_is_refreshed_and_saved(server, tmp_path):
    path = _write_tokens(tmp_path, access_in=timedelta(hours=-1), refresh_in=timedelta(days=1))
    call("cloud_list_devices")
    refresh, listing = server.requests
    assert refresh[1].endswith("/api/auth/guest/refresh") and refresh[3] == {"refresh_token": "old-refresh"}
    assert listing[2] == "Bearer new-access"
    saved = json.loads(path.read_text())
    assert saved["access_token"] == "new-access" and saved["refresh_token"] == "new-refresh"
    assert saved["session_id"] == "s1"  # kept when the reply omits it
    assert cli_login._parse_time(saved["access_expires_at"]) > datetime.now(timezone.utc)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_expired_login_asks_for_benchpod_login(server, tmp_path):
    _write_tokens(tmp_path, access_in=timedelta(hours=-2), refresh_in=timedelta(hours=-1))
    with pytest.raises(ToolError, match="CloudAuthError: .*benchpod login"):
        call("cloud_list_devices")
    assert server.requests == []


def test_rejected_refresh_asks_for_benchpod_login(server, tmp_path):
    server.refresh_status = 401
    _write_tokens(tmp_path, access_in=timedelta(hours=-1), refresh_in=timedelta(days=1))
    with pytest.raises(ToolError, match="refresh token revoked.*benchpod login"):
        call("cloud_list_devices")


def test_no_login_and_no_key(server):
    with pytest.raises(ToolError, match="not logged in.*benchpod login"):
        call("cloud_list_devices")


def test_api_key_wins_over_the_cli_login(server, tmp_path, monkeypatch):
    _write_tokens(tmp_path, access_in=timedelta(hours=1), refresh_in=timedelta(days=7))
    monkeypatch.setenv("BENCHPOD_API_KEY", "eci_test")
    monkeypatch.setenv("BENCHPOD_API_BASE", "https://staging.example")
    out = call("cloud_list_devices")
    assert out["auth"] == "api_key"
    assert server.requests == [("GET", "https://staging.example/api/benchpod/devices", "ApiKey eci_test", None)]


def test_cloud_connect_uses_the_cli_login(server, tmp_path, monkeypatch):
    import embeddedci_mcp.session as session_mod
    from embeddedci_mcp.session import Session

    _write_tokens(tmp_path, access_in=timedelta(hours=1), refresh_in=timedelta(days=7))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    opened = {}

    class _Pod:
        leased = False

        def close(self):
            pass

    def factory(connection, **kwargs):
        opened.update(kwargs)
        return _Pod()

    monkeypatch.setattr(session_mod, "BenchPod", factory)
    Session().connect("embeddedci:bench-1")
    assert opened["cloud_user_token"]() == "old-access"


@pytest.mark.parametrize("connection, env", [
    ("192.168.1.10", {}),                                   # not a cloud pod
    ("embeddedci:bench-1", {"BENCHPOD_API_KEY": "eci_x"}),  # the API key wins
    ("embeddedci:bench-1", {"GITHUB_ACTIONS": "true"}),     # CI authenticates with OIDC
])
def test_cli_login_is_only_used_when_nothing_else_applies(monkeypatch, connection, env):
    from embeddedci_mcp.session import cloud_user_token

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert cloud_user_token(connection) is None
