import pytest

from embeddedci.benchpod import constants
from embeddedci.benchpod.connection import ENV_VAR, parse_connection, resolve_connection
from embeddedci.benchpod.errors import BenchPodError, ConnectionConfigError


@pytest.mark.parametrize(
    "raw, kind, addr, device",
    [
        ("192.168.1.213", "tcp", "192.168.1.213:8080", ""),
        ("192.168.1.213:9000", "tcp", "192.168.1.213:9000", ""),
        ("host.local", "tcp", "host.local:8080", ""),
        ("/dev/ttyACM0", "serial", "", "/dev/ttyACM0"),
        ("COM3", "serial", "", "COM3"),
        ("serial", "serial", "", ""),
        ("USB", "serial", "", ""),
    ],
)
def test_parse_connection(raw, kind, addr, device):
    spec = parse_connection(raw)
    assert spec.kind == kind
    assert spec.addr == addr
    assert spec.device == device


def test_resolve_prefers_explicit_over_env(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "/dev/ttyACM0")
    spec = resolve_connection("192.168.1.5")
    assert spec.is_wifi()
    assert spec.addr == "192.168.1.5:8080"


def test_resolve_falls_back_to_env(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "serial")
    spec = resolve_connection()
    assert spec.is_serial()


def test_resolve_errors_when_unset(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    with pytest.raises(ConnectionConfigError):
        resolve_connection()


@pytest.mark.parametrize("keyword", ["discover", "mdns", "auto", "AUTO"])
def test_discover_single_pod(keyword, monkeypatch):
    from embeddedci.benchpod import connection as conn
    from embeddedci.benchpod.discovery import DiscoveredPod

    pod = DiscoveredPod(
        name="BenchPod a1b2c3",
        hostname="benchpod-a1b2c3.local",
        addresses=["192.168.1.7"],
        port=8080,
        pod_id="abc",
    )
    monkeypatch.setattr(conn, "_discover_one", conn._discover_one)
    monkeypatch.setattr("embeddedci.benchpod.discovery.discover", lambda timeout=3.0: [pod])

    spec = parse_connection(keyword)
    assert spec.kind == "tcp"
    assert spec.addr == "192.168.1.7:8080"


def test_discover_none_found(monkeypatch):
    monkeypatch.setattr("embeddedci.benchpod.discovery.discover", lambda timeout=3.0: [])
    with pytest.raises(ConnectionConfigError, match="no BenchPod found"):
        parse_connection("discover")


def test_discover_multiple_is_ambiguous(monkeypatch):
    from embeddedci.benchpod.discovery import DiscoveredPod

    pods = [
        DiscoveredPod(name="BenchPod a1", hostname="benchpod-a1.local",
                      addresses=["192.168.1.7"], port=8080, pod_id="aaaaaaaaaaaa1"),
        DiscoveredPod(name="BenchPod b2", hostname="benchpod-b2.local",
                      addresses=["192.168.1.8"], port=8080, pod_id="bbbbbbbbbbbb2"),
    ]
    monkeypatch.setattr("embeddedci.benchpod.discovery.discover", lambda timeout=3.0: pods)
    with pytest.raises(ConnectionConfigError, match="2 BenchPods found"):
        parse_connection("discover")


def test_constant_coercion():
    assert constants.coerce_efuse(constants.INTERNAL) == 1
    assert constants.coerce_efuse(2) == 2
    assert constants.coerce_pin(constants.PIN12) == 12
    assert constants.coerce_pin(1, "swclk") == 1
    with pytest.raises(BenchPodError):
        constants.coerce_efuse(3)
    with pytest.raises(BenchPodError):
        constants.coerce_pin(13, "swdio")
