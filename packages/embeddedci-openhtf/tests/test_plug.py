"""Plug-level tests: construction from an injected transport, attribute proxying,
teardown, config/env resolution and the missing-connection error. No hardware, no
OpenHTF executor."""

from types import MappingProxyType

import openhtf as htf
import pytest

from embeddedci.benchpod import BenchPod
from embeddedci.benchpod.errors import ConnectionConfigError

import embeddedci_openhtf.plug as plug_mod
from embeddedci_openhtf import BenchPodPlug, benchpod_plug
from _fake import FakeTransport


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("BENCHPOD_CONNECTION", raising=False)
    monkeypatch.delenv("BENCHPOD_LA_VOLTAGE", raising=False)


def _la_cmds(tx):
    return [c for c in tx.commands if c.get("cmd") == "la_voltage"]


def test_plug_builds_pod_and_proxies():
    tx = FakeTransport()
    plug = benchpod_plug(transport=tx)()
    assert isinstance(plug.pod, BenchPod)
    # unknown attributes proxy through to the SDK client
    assert plug.status()["fake"] is True
    plug.power_on(1)
    plug.power_off(1)
    assert [c["on"] for c in tx.power_calls] == [True, False]


def test_teardown_closes_connection():
    tx = FakeTransport()
    plug = benchpod_plug(transport=tx)()
    plug.tearDown()
    assert tx.closed is True


def test_internal_attrs_do_not_proxy():
    # dunder / private lookups must not be forwarded to the pod
    plug = benchpod_plug(transport=FakeTransport())()
    with pytest.raises(AttributeError):
        _ = plug._nope


def test_missing_connection_raises():
    with pytest.raises(ConnectionConfigError):
        BenchPodPlug()  # no bound connection, no config, no env, no transport


def test_no_la_voltage_by_default():
    tx = FakeTransport()
    benchpod_plug(transport=tx)()
    assert _la_cmds(tx) == []


def test_conf_la_voltage_is_passed_to_benchpod():
    tx = FakeTransport()

    @htf.conf.save_and_restore(benchpod_la_voltage=3.3)
    def build():
        return benchpod_plug(transport=tx)()

    build()
    assert _la_cmds(tx) == [{"cmd": "la_voltage", "mv": 3300}]


def test_bound_la_voltage_wins_over_conf():
    tx = FakeTransport()

    @htf.conf.save_and_restore(benchpod_la_voltage=3.3)
    def build():
        return benchpod_plug(transport=tx, la_voltage=1.8)()

    build()
    assert _la_cmds(tx) == [{"cmd": "la_voltage", "mv": 1800}]


def test_la_voltage_env_fallback(monkeypatch):
    monkeypatch.setenv("BENCHPOD_LA_VOLTAGE", "1.8")
    tx = FakeTransport()
    benchpod_plug(transport=tx)()
    assert _la_cmds(tx) == [{"cmd": "la_voltage", "mv": 1800}]


def test_conf_connection_timeout_and_la_voltage_reach_benchpod(monkeypatch):
    seen = {}

    class SpyBenchPod:
        def __init__(self, connection=None, **kwargs):
            seen.update(connection=connection, **kwargs)

        def close(self):
            pass

    monkeypatch.setattr(plug_mod, "BenchPod", SpyBenchPod)

    @htf.conf.save_and_restore(benchpod_connection="10.0.0.9:8080", benchpod_timeout=7.5,
                               benchpod_la_voltage="3.3")
    def build():
        return BenchPodPlug()

    build()
    assert seen == {"connection": "10.0.0.9:8080", "timeout": 7.5, "la_voltage": 3.3}


def test_wiring_reaches_benchpod_as_a_dict_and_as_a_file(tmp_path):
    tx = FakeTransport()
    plug = benchpod_plug(transport=tx, wiring={"uart_rx": 7, "uart_tx": 8, "efuse": 2})()
    assert plug.wiring.uart_rx == 7 and plug.wiring.efuse == 2
    assert plug.wiring.la("uart_tx") == 8

    path = tmp_path / "bench.json"
    path.write_text('{"i2c_sda": 3, "i2c_scl": 6, "signals": [{"name": "READY", "la": 7}]}')
    from_file = benchpod_plug(transport=FakeTransport(), wiring=str(path))()
    assert from_file.wiring.source == "file" and from_file.wiring.la("READY") == 7


def test_wiring_supplies_the_defaults_of_proxied_calls():
    tx = FakeTransport()
    plug = benchpod_plug(transport=tx, wiring={"efuse": 2})()
    plug.power_on()  # no rail given: the profile's
    assert tx.power_calls == [{"efuse": 2, "on": True, "delay_ms": 0}]


def test_pod_kwargs_are_immutable_and_not_shared():
    assert isinstance(BenchPodPlug.pod_kwargs, MappingProxyType)
    bound = benchpod_plug(transport=FakeTransport(), timeout=5.0)
    with pytest.raises(TypeError):
        bound.pod_kwargs["timeout"] = 1.0
    assert dict(BenchPodPlug.pod_kwargs) == {}
    other = benchpod_plug(transport=FakeTransport())
    assert "timeout" not in other.pod_kwargs
