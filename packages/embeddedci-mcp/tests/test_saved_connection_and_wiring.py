"""One saved connection and one wiring profile: `connect` falls back to the connection benchpod-cli
saved, and a LAN pod's `wiring` is the profile stored on embeddedci.com when the user is signed in.

Both need an SDK with ``embeddedci.benchpod.cli_config`` (2.10); CI also runs these tests against
the oldest SDK the server allows, where they skip."""

from __future__ import annotations

import io
import json
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from embeddedci.benchpod import BenchPod

import embeddedci_mcp.session as session_mod
from embeddedci_mcp.session import SESSION, cloud_user_token

from conftest import call

try:
    import embeddedci.benchpod.cli_config  # noqa: F401

    NEW_SDK = True
except ImportError:  # pragma: no cover - the minimum-SDK CI job
    NEW_SDK = False

needs_new_sdk = pytest.mark.skipif(not NEW_SDK, reason="needs embeddedci with cli_config (2.10)")

DEVICE_ID = "dev-1234"


@pytest.fixture
def xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    for var in ("BENCHPOD_API_BASE", "BENCHPOD_WIRING", "BENCHPOD_WIRING_SOURCE"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / "benchpod-cli"
    d.mkdir()
    return d


def _save(xdg, connection):
    (xdg / "config.json").write_text(json.dumps({"connection": connection}))


def _login(xdg):
    now = datetime.now(timezone.utc)
    (xdg / "token.json").write_text(json.dumps({
        "access_token": "login-access", "refresh_token": "r",
        "access_expires_at": (now + timedelta(hours=1)).isoformat(),
        "refresh_expires_at": (now + timedelta(days=7)).isoformat()}))


def _no_mdns(monkeypatch):
    from embeddedci.benchpod import discovery

    def no_browse(timeout=None):
        raise AssertionError("mDNS must not run when a connection is saved")

    monkeypatch.setattr(discovery, "discover", no_browse)


@needs_new_sdk
def test_connect_uses_the_cli_saved_connection(fake_open, monkeypatch, xdg):
    _save(xdg, "192.168.1.220")
    _no_mdns(monkeypatch)
    result = call("connect")
    assert fake_open["connection"] == "192.168.1.220" and result["connected"] is True


@needs_new_sdk
@pytest.mark.parametrize("configured", ["argument", "server", "env"])
def test_the_saved_connection_comes_last(fake_open, monkeypatch, xdg, configured):
    _save(xdg, "192.168.1.220")
    if configured == "argument":
        call("connect", connection="10.0.0.7")
    else:
        if configured == "server":
            SESSION.default_connection = "10.0.0.7"
        else:
            monkeypatch.setenv("BENCHPOD_CONNECTION", "10.0.0.7")
        call("connect")
    assert fake_open["connection"] == "10.0.0.7"


def test_the_login_reaches_a_lan_pod_only_when_there_is_one(monkeypatch, xdg):
    assert cloud_user_token("192.168.1.220") is None          # not signed in
    _login(xdg)
    provider = cloud_user_token("192.168.1.220")
    assert provider is not None and provider() == "login-access"
    assert cloud_user_token("usb")() == "login-access"
    monkeypatch.setenv("BENCHPOD_API_KEY", "eci_x")             # the SDK uses the key itself
    assert cloud_user_token("192.168.1.220") is None


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@needs_new_sdk
def test_wiring_of_a_lan_pod_is_the_stored_profile(monkeypatch, xdg, fake_transport):
    _login(xdg)
    fake_transport._cmd_cloud_status = lambda req: {"configured": True, "device_id": DEVICE_ID}
    requests = []

    def urlopen(req, timeout=None):
        requests.append((req.full_url, req.get_header("Authorization")))
        return _Resp(json.dumps({"profile": {"uart_rx": 3, "uart_tx": 6}, "stored": True}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    def factory(connection, **kwargs):
        return BenchPod(transport=fake_transport, lease=False, la_voltage=kwargs.get("la_voltage"),
                        cloud_user_token=kwargs.get("cloud_user_token"))

    monkeypatch.setattr(session_mod, "BenchPod", factory)
    status = call("connect", connection="192.168.1.220", la_voltage=3.3)
    assert status["wiring_source"] == "server"
    w = call("wiring")
    assert w["source"] == "server" and w["profile"]["uart_rx"] == 3 and w["profile"]["uart_tx"] == 6
    assert requests == [(f"https://www.embeddedci.com/api/benchpod/devices/{DEVICE_ID}/wiring/profile",
                         "Bearer login-access")]


def test_status_reports_the_defaults_when_nothing_is_stored(connected):
    assert call("status")["wiring_source"] == "defaults"
