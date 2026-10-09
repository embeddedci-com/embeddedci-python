"""One wiring profile and one saved connection across the web UI, the CLI, the SDK and MCP.

* A LAN/USB BenchPod reads the profile stored on embeddedci.com for the device the pod says it is
  registered as, when credentials exist; every failure falls back to the defaults.
* ``BenchPod()`` without a connection uses the connection ``benchpod-cli`` saved; the pytest
  plugin only does so when asked with ``saved``.
"""

from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import BenchPod, Wiring
from embeddedci.benchpod import cli_config
from embeddedci.benchpod.connection import resolve_connection
from embeddedci.benchpod.errors import ConnectionConfigError, FirmwareError

DEVICE_ID = "8f1c0d2e-0000-4000-8000-000000000001"
PROFILE = {"uart_rx": 3, "uart_tx": 6, "efuse": 2, "signals": [{"name": "READY", "la": 10}]}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for var in ("BENCHPOD_CONNECTION", "BENCHPOD_WIRING", "BENCHPOD_WIRING_SOURCE",
                "BENCHPOD_API_KEY", "BENCHPOD_API_BASE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def _cli_dir(tmp_path):
    d = tmp_path / "xdg" / "benchpod-cli"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_config(tmp_path, data: Any) -> None:
    text = data if isinstance(data, str) else json.dumps(data)
    (_cli_dir(tmp_path) / "config.json").write_text(text)


def _login(tmp_path, access: str = "login-access") -> None:
    now = datetime.now(timezone.utc)
    (_cli_dir(tmp_path) / "token.json").write_text(json.dumps({
        "access_token": access, "refresh_token": "r",
        "access_expires_at": (now + timedelta(hours=1)).isoformat(),
        "refresh_expires_at": (now + timedelta(days=7)).isoformat(),
    }))


# -- the saved connection -------------------------------------------------------------------

def test_saved_connection_reads_the_cli_config(tmp_path):
    assert cli_config.saved_connection() is None                      # no file
    _save_config(tmp_path, {"connection": " 192.168.1.220 ", "last_serial": "/dev/x"})
    assert cli_config.saved_connection() == "192.168.1.220"
    assert cli_config.config_path() == tmp_path / "xdg" / "benchpod-cli" / "config.json"
    _save_config(tmp_path, {"bench_pod_addr": "10.0.0.5:8080"})        # the legacy field
    assert cli_config.saved_connection() == "10.0.0.5:8080"
    _save_config(tmp_path, {"connection": "usb", "bench_pod_addr": "10.0.0.5"})
    assert cli_config.saved_connection() == "usb"


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"connection": 5}', "{}",
                                     '{"connection": "saved"}'])
def test_a_bad_or_empty_cli_config_means_nothing_saved(tmp_path, content):
    _save_config(tmp_path, content)
    assert cli_config.saved_connection() is None


def test_config_path_without_xdg_is_under_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli_config.config_path() == tmp_path / ".config" / "benchpod-cli" / "config.json"


def test_connection_precedence_argument_env_saved(monkeypatch, tmp_path):
    _save_config(tmp_path, {"connection": "10.0.0.3"})
    assert resolve_connection().addr == "10.0.0.3:8080"                 # saved
    monkeypatch.setenv("BENCHPOD_CONNECTION", "10.0.0.2")
    assert resolve_connection().addr == "10.0.0.2:8080"                 # env beats saved
    assert resolve_connection("10.0.0.1").addr == "10.0.0.1:8080"       # argument beats both
    assert resolve_connection("saved").addr == "10.0.0.3:8080"          # explicit keyword


def test_no_connection_anywhere_says_how_to_save_one():
    with pytest.raises(ConnectionConfigError, match="benchpod set-connection"):
        resolve_connection()
    with pytest.raises(ConnectionConfigError, match="holds none"):
        resolve_connection("saved")


def test_benchpod_without_a_connection_opens_the_saved_one(monkeypatch, tmp_path):
    import embeddedci.benchpod.client as client_mod

    _save_config(tmp_path, {"connection": "usb"})
    opened: List[Any] = []

    def fake_open(spec, **kwargs):
        opened.append(spec)
        return Pod()

    monkeypatch.setattr(client_mod, "open_transport", fake_open)
    BenchPod(lease=False).close()
    assert opened[0].kind == "serial" and opened[0].device == ""


# -- the wiring profile of a LAN/USB pod ----------------------------------------------------

class Pod:
    """A LAN pod that answers cloud_status."""

    def __init__(self, cloud: Any = None) -> None:
        self.cloud = {"configured": True, "state": "connected", "device_id": DEVICE_ID} \
            if cloud is None else cloud
        self.commands: List[dict] = []
        self.timeout = 30.0
        self.dial_timeout = 10.0
        self.seen_timeouts: List[tuple] = []

    def status(self) -> Dict[str, Any]:
        return {"board": "stm32h563"}

    def ping(self) -> Any:
        return "pong"

    def command(self, req: dict) -> Any:
        self.commands.append(req)
        if req["cmd"] == "cloud_status":
            self.seen_timeouts.append((self.timeout, self.dial_timeout))
            if isinstance(self.cloud, Exception):
                raise self.cloud
            return self.cloud
        return {}

    def close(self) -> None:
        pass


class Http:
    """Stands in for urllib: answers GET .../wiring/profile and records each request."""

    def __init__(self, status: int = 200, body: Any = None, error: Exception = None) -> None:
        self.status = status
        self.body = {"profile": PROFILE, "stored": True} if body is None else body
        self.error = error
        self.requests: List[tuple] = []

    def urlopen(self, req, timeout=None):
        self.requests.append((req.get_method(), req.full_url, req.get_header("Authorization"), timeout))
        if self.error is not None:
            raise self.error
        if self.status >= 400:
            raise urllib.error.HTTPError(req.full_url, self.status, "x", {},
                                         io.BytesIO(json.dumps({"error": "nope"}).encode()))
        return _Resp(json.dumps(self.body).encode(), self.status)


class _Resp:
    def __init__(self, body: bytes, status: int) -> None:
        self._body, self.status = body, status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        pass


@pytest.fixture
def http(monkeypatch):
    h = Http()
    monkeypatch.setattr(urllib.request, "urlopen", h.urlopen)
    return h


def test_lan_pod_uses_the_stored_profile_with_an_api_key(monkeypatch, http):
    monkeypatch.setenv("BENCHPOD_API_KEY", "eci_test")
    monkeypatch.setenv("BENCHPOD_API_BASE", "https://staging.example")
    pod = Pod()
    bp = BenchPod(transport=pod, lease=False)
    assert bp.wiring.source == "server"
    assert bp.wiring.uart_rx == 3 and bp.wiring.efuse == 2 and bp.signal("READY").la == 10
    method, url, auth, timeout = http.requests[0]
    assert (method, url, auth) == (
        "GET", f"https://staging.example/api/benchpod/devices/{DEVICE_ID}/wiring/profile", "ApiKey eci_test")
    assert 0 < timeout <= 3.0
    # cloud_status went out with the short budget, and the transport's timeouts came back.
    assert pod.seen_timeouts and all(t <= 3.0 and d <= 3.0 for t, d in pod.seen_timeouts)
    assert (pod.timeout, pod.dial_timeout) == (30.0, 10.0)
    bp.wiring  # resolved once
    assert len(http.requests) == 1


def test_lan_pod_uses_the_benchpod_login_session(tmp_path, http):
    _login(tmp_path, access="tok-123")
    bp = BenchPod(transport=Pod(), lease=False)
    assert bp.wiring.source == "server"
    assert http.requests[0][1].startswith("https://www.embeddedci.com/api/benchpod/devices/")
    assert http.requests[0][2] == "Bearer tok-123"


def test_a_callers_user_token_is_used_for_the_lookup(http):
    bp = BenchPod(transport=Pod(), lease=False, cloud_user_token=lambda: "mcp-token")
    assert bp.wiring.source == "server" and http.requests[0][2] == "Bearer mcp-token"


def test_no_credentials_means_defaults_without_asking_anyone(http, caplog):
    pod = Pod()
    with caplog.at_level(logging.INFO, logger="embeddedci.benchpod"):
        w = BenchPod(transport=pod, lease=False).wiring
    assert w == Wiring() and w.source == "defaults"
    assert pod.commands == [] and http.requests == []
    assert "no embeddedci.com credentials" in caplog.text


@pytest.mark.parametrize("cloud", [{"configured": False, "state": "unconfigured"},
                                   {"configured": True, "device_id": ""}, {}])
def test_an_unregistered_pod_gets_the_defaults(monkeypatch, http, cloud):
    monkeypatch.setenv("BENCHPOD_API_KEY", "eci_test")
    assert BenchPod(transport=Pod(cloud), lease=False).wiring.source == "defaults"
    assert http.requests == []


def test_a_pod_that_refuses_cloud_status_gets_the_defaults(monkeypatch, http, caplog):
    monkeypatch.setenv("BENCHPOD_API_KEY", "eci_test")
    pod = Pod(FirmwareError("cloud_status: unknown command"))
    with caplog.at_level(logging.WARNING, logger="embeddedci.benchpod"):
        assert BenchPod(transport=pod, lease=False).wiring.source == "defaults"
    assert http.requests == [] and "could not ask the pod" in caplog.text
    assert (pod.timeout, pod.dial_timeout) == (30.0, 10.0)


@pytest.mark.parametrize("status, phrase", [(404, "not on this account"), (403, "not on this account"),
                                            (500, "HTTP 500")])
def test_server_errors_fall_back_to_the_defaults(monkeypatch, http, caplog, status, phrase):
    monkeypatch.setenv("BENCHPOD_API_KEY", "eci_test")
    http.status = status
    with caplog.at_level(logging.WARNING, logger="embeddedci.benchpod"):
        assert BenchPod(transport=Pod(), lease=False).wiring.source == "defaults"
    assert phrase in caplog.text


@pytest.mark.parametrize("error", [urllib.error.URLError("offline"), TimeoutError("timed out"),
                                   OSError("network is unreachable")])
def test_an_unreachable_server_falls_back_to_the_defaults(monkeypatch, http, error):
    monkeypatch.setenv("BENCHPOD_API_KEY", "eci_test")
    http.error = error
    assert BenchPod(transport=Pod(), lease=False).wiring.source == "defaults"


def test_an_expired_login_falls_back_to_the_defaults(tmp_path, http):
    past = datetime.now(timezone.utc) - timedelta(days=1)
    (_cli_dir(tmp_path) / "token.json").write_text(json.dumps({
        "access_token": "a", "refresh_token": "r",
        "access_expires_at": past.isoformat(), "refresh_expires_at": past.isoformat()}))
    assert BenchPod(transport=Pod(), lease=False).wiring.source == "defaults"
    assert http.requests == []


def test_a_malformed_stored_profile_falls_back_to_the_defaults(monkeypatch, http):
    monkeypatch.setenv("BENCHPOD_API_KEY", "eci_test")
    http.body = {"profile": {"uart_rx": 99}}
    assert BenchPod(transport=Pod(), lease=False).wiring.source == "defaults"


def test_explicit_wiring_and_the_wiring_file_still_win(monkeypatch, http, tmp_path):
    monkeypatch.setenv("BENCHPOD_API_KEY", "eci_test")
    pod = Pod()
    assert BenchPod(transport=pod, lease=False, wiring={"uart_rx": 7}).wiring.uart_rx == 7
    p = tmp_path / "bench.json"
    p.write_text(json.dumps({"uart_rx": 8}))
    monkeypatch.setenv("BENCHPOD_WIRING", str(p))
    w = BenchPod(transport=pod, lease=False).wiring
    assert w.uart_rx == 8 and w.source == "file"
    assert pod.commands == [] and http.requests == []


def test_wiring_source_local_skips_the_lookup(monkeypatch, http):
    monkeypatch.setenv("BENCHPOD_API_KEY", "eci_test")
    pod = Pod()
    assert BenchPod(transport=pod, lease=False, wiring_source="local").wiring.source == "defaults"
    monkeypatch.setenv("BENCHPOD_WIRING_SOURCE", "LOCAL")
    assert BenchPod(transport=pod, lease=False).wiring.source == "defaults"
    assert pod.commands == [] and http.requests == []
    # The argument wins over the variable.
    assert BenchPod(transport=pod, lease=False, wiring_source="auto").wiring.source == "server"


def test_wiring_source_local_also_skips_a_cloud_devices_profile(monkeypatch):
    bp = BenchPod(transport=Pod(), lease=False, wiring_source="local")
    bp._device_name = "bench-1"
    monkeypatch.setattr(bp, "_try_server_api", lambda: pytest.fail("asked the server"))
    assert bp.wiring.source == "defaults"


def test_a_bad_wiring_source_is_rejected(monkeypatch):
    with pytest.raises(ValueError, match="wiring_source"):
        BenchPod(transport=Pod(), lease=False, wiring_source="cloud")
    monkeypatch.setenv("BENCHPOD_WIRING_SOURCE", "server-only")
    with pytest.raises(ValueError, match="BENCHPOD_WIRING_SOURCE"):
        BenchPod(transport=Pod(), lease=False)


# -- the pytest plugin never picks up the saved connection by itself ----------------------

PLUGIN_CONFTEST = """
import pytest
from embeddedci.benchpod import pytest_plugin

made = {}

class FakePod:
    def __init__(self, connection, **kwargs):
        made["connection"] = connection
        self.capabilities = type("C", (), {"la_pins": False})()
    def close(self):
        pass

pytest_plugin.BenchPod = FakePod
"""

PLUGIN_TEST = """
import conftest

def test_session(benchpod):
    assert conftest.made["connection"] == "10.9.8.7"
"""


def test_pytest_plugin_ignores_the_saved_connection_unless_asked(pytester, tmp_path):
    _save_config(tmp_path, {"connection": "10.9.8.7"})
    pytester.makeconftest(PLUGIN_CONFTEST)
    pytester.makepyfile(PLUGIN_TEST)
    result = pytester.runpytest("-p", "no:cacheprovider", "-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*no BenchPod connection configured*"])
    result = pytester.runpytest("-p", "no:cacheprovider", "--benchpod-connection=saved")
    result.assert_outcomes(passed=1)


def test_pytest_plugin_saved_without_a_saved_connection_skips(pytester):
    pytester.makeconftest(PLUGIN_CONFTEST)
    pytester.makepyfile(PLUGIN_TEST)
    result = pytester.runpytest("-p", "no:cacheprovider", "-rs", "--benchpod-connection=saved")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*benchpod-cli saved no connection*"])
