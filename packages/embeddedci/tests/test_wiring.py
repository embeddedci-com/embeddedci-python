"""Wiring profiles: schema validation, lookups, files, and how BenchPod falls back to them."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import BenchPod, Signal, Wiring


def test_defaults_are_the_web_ui_defaults():
    w = Wiring()
    assert (w.uart_rx, w.uart_tx, w.uart_baud, w.i2c_sda, w.i2c_scl) == (5, 4, 115200, 1, 2)
    assert (w.swd_swclk, w.swd_swdio, w.efuse, w.la_voltage, w.i2c_address) == (11, 12, 1, 3.3, 0x76)
    assert w.source == "defaults" and w.warnings() == []


def test_lookups_by_role_and_signal():
    w = Wiring(uart_rx=3, signals=[Signal("TRIGGER", 9, "output"), Signal("Ready", 10)])
    assert w.la("uart_rx") == 3 and w.la("trigger") == 9 and w.la("READY") == 10
    assert w.signal("ready").direction == "input"
    assert w.pins()[9] == "TRIGGER" and 6 not in w.pins()
    with pytest.raises(ValueError, match="not a signal"):
        w.la("MISSING")
    with pytest.raises(ValueError, match="not wired"):
        Wiring(spi_cs=None).la("spi_cs")
    assert "LA9   TRIGGER" in w.describe()


def test_every_problem_is_reported_at_once():
    with pytest.raises(ValueError) as ei:
        Wiring(la_mv=5000, efuse=3, uart_baud=10, uart_rx=13, i2c_addr="0x80",
               signals=[Signal("1bad", 20), Signal("uart_tx", 6, "sideways")])
    msg = str(ei.value)
    for part in ("la_mv", "efuse", "uart_baud", "uart_rx", "i2c_addr", "signals[0].name",
                 "signals[0].la", "signals[1].name", "signals[1].direction"):
        assert part in msg, part


def test_one_channel_one_user():
    with pytest.raises(ValueError, match="LA4 is used by both uart_tx and i2c_sda"):
        Wiring(i2c_sda=4)
    with pytest.raises(ValueError, match="LA9 is used by both A and B"):
        Wiring(signals=[Signal("A", 9), Signal("B", 9)])
    with pytest.raises(ValueError, match="already used"):
        Wiring(signals=[Signal("A", 9), Signal("a", 10)])
    Wiring(uart_tx=None, i2c_sda=4)  # not wired frees the channel


def test_warnings_for_risky_wiring():
    w = Wiring(i2c_sda=9, i2c_scl=7, uart_rx=8, uart_tx=None, swd_swclk=None, swd_swdio=None)
    text = " | ".join(w.warnings())
    assert "no pod pull-up" in text and "i2c_scl is on LA7" in text and "uart_rx is on LA8" in text


def test_dict_round_trip_and_unknown_keys():
    w = Wiring.from_dict({"uart_rx": 3, "spi_cs": 6, "loop_input_unit": "mA",
                          "signals": [{"name": "T", "la": 9, "direction": "output"}]})
    assert w.extra == {"loop_input_unit": "mA"} and w.source == "dict"
    assert Wiring.from_dict(w.to_dict()) == w
    with pytest.raises(ValueError, match="unknown key.*colour"):
        Wiring.from_dict({"colour": "red"})
    with pytest.raises(ValueError, match="signals\\[0\\].pin"):
        Wiring.from_dict({"signals": [{"name": "T", "la": 9, "pin": 9}]})
    assert Wiring.from_dict({"colour": "red"}, strict=False).extra == {"colour": "red"}
    assert Wiring.from_dict({"uart_rx": None}).uart_rx is None  # null = not wired


def test_load_json_and_toml(tmp_path):
    p = tmp_path / "wiring.json"
    p.write_text(json.dumps({"uart_rx": 3, "signals": [{"name": "T", "la": 9}]}))
    w = Wiring.load(p)
    assert w.uart_rx == 3 and w.source == "file" and Wiring.coerce(str(p)) == w
    if sys.version_info >= (3, 11):
        t = tmp_path / "wiring.toml"
        t.write_text('uart_rx = 3\n\n[[signals]]\nname = "T"\nla = 9\n')
        assert Wiring.load(t) == w
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with pytest.raises(ValueError, match="not valid JSON"):
        Wiring.load(bad)
    with pytest.raises(ValueError, match="must be a Wiring"):
        Wiring.coerce(42)  # type: ignore[arg-type]


# -- BenchPod falls back to the profile -------------------------------------------------

class WiredFake:
    """Records commands, UART proxy starts and power calls."""

    def __init__(self) -> None:
        self.commands: List[dict] = []
        self.uart: List[tuple] = []
        self.power: List[tuple] = []

    def status(self) -> Dict[str, Any]:
        return {"board": "stm32h563", "adc_bits": 16}

    def ping(self) -> Any:
        return "pong"

    def command(self, req: dict) -> Any:
        self.commands.append(req)
        return {}

    def uart_proxy_start(self, rx: int, tx: int, baud: int):
        self.uart.append((rx, tx, baud))
        return SimpleNamespace(read=lambda n: b"", write=lambda d: len(d), close=lambda: None)

    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        self.power.append((efuse, on, delay_ms))

    def close(self) -> None:
        pass


def test_benchpod_uses_the_profile_for_omitted_channels(monkeypatch):
    monkeypatch.delenv("BENCHPOD_WIRING", raising=False)
    t = WiredFake()
    bp = BenchPod(transport=t, lease=False,
                  wiring={"uart_rx": 3, "uart_tx": 6, "uart_baud": 921600, "efuse": 2,
                          "i2c_sda": 5, "i2c_scl": 4, "i2c_addr": "0x77", "signals": [{"name": "T", "la": 9}]})
    bp.open_uart().close()
    bp.capture_uart(duration=0.01)
    bp.open_uart(rx="T", tx=4, baud=9600).close()
    assert t.uart == [(3, 6, 921600), (3, 6, 921600), (9, 4, 9600)]
    bp.power_on()
    bp.power_off(1)
    assert t.power == [(2, True, 0), (1, False, 0)]
    bp.enable_i2c_sensor()
    start = next(c for c in t.commands if c["cmd"] == "sensor_start")
    assert 5 in start.values() and 4 in start.values()
    assert bp.signal("t").la == 9


def test_missing_channel_names_the_profile_key(monkeypatch):
    monkeypatch.delenv("BENCHPOD_WIRING", raising=False)
    bp = BenchPod(transport=WiredFake(), lease=False, wiring=Wiring(uart_rx=None))
    with pytest.raises(ValueError, match="uart_rx"):
        bp.open_uart()


def test_wiring_resolution_order(monkeypatch, tmp_path):
    p = tmp_path / "bench.json"
    p.write_text(json.dumps({"uart_rx": 7}))
    monkeypatch.setenv("BENCHPOD_WIRING", str(p))
    assert BenchPod(transport=WiredFake(), lease=False).wiring.uart_rx == 7
    assert BenchPod(transport=WiredFake(), lease=False, wiring={"uart_rx": 3}).wiring.uart_rx == 3
    monkeypatch.delenv("BENCHPOD_WIRING")
    bp = BenchPod(transport=WiredFake(), lease=False)
    assert bp.wiring == Wiring()
    bp.wiring = {"uart_rx": 6}
    assert bp.wiring.uart_rx == 6
    with pytest.raises(ValueError, match="set a role that isn't wired to None"):
        bp.wiring = {"uart_rx": 2}  # LA2 is i2c_scl's default


def test_cloud_device_loads_and_saves_the_server_profile(monkeypatch):
    monkeypatch.delenv("BENCHPOD_WIRING", raising=False)

    class Server:
        def __init__(self):
            self.saved = None

        def resolve_device_id(self, name):
            return "dev-1"

        def wiring_profile(self, device_id):
            return {"profile": dict(Wiring(uart_rx=3).to_dict(), future_key=1)}

        def put_wiring(self, device_id, wiring):
            self.saved = (device_id, wiring)
            return wiring

        def device_parameters(self, name):
            return {}

    server = Server()
    bp = BenchPod(transport=WiredFake(), lease=False)
    bp._device_name = "bench-pod"
    monkeypatch.setattr(bp, "_try_server_api", lambda: server)
    monkeypatch.setattr(bp, "_require_server_api", lambda: server)
    assert bp.wiring.uart_rx == 3 and bp.wiring.source == "server"
    assert bp.wiring.extra["future_key"] == 1           # a newer server's keys are kept
    saved = bp.save_wiring(Wiring(uart_rx=6))
    assert server.saved[0] == "dev-1" and server.saved[1]["uart_rx"] == 6 and saved.uart_rx == 6


def test_save_wiring_needs_a_cloud_device(monkeypatch):
    monkeypatch.delenv("BENCHPOD_WIRING", raising=False)
    with pytest.raises(Exception, match="cloud device"):
        BenchPod(transport=WiredFake(), lease=False).save_wiring()


def test_pytest_fixture_supplies_wiring_and_la_voltage(pytester):
    pytester.makeconftest("""
import pytest
from embeddedci.benchpod import pytest_plugin

made = {}

class FakePod:
    def __init__(self, connection, **kwargs):
        made.update(kwargs, connection=connection)
        self.capabilities = type("C", (), {"la_pins": False})()
        self.wiring = kwargs["wiring"]
    def close(self):
        pass

# The benchpod fixture is session-scoped, so swap the class before any fixture runs.
pytest_plugin.BenchPod = FakePod

@pytest.fixture(scope="session")
def benchpod_wiring():
    return {"la_mv": 1800, "uart_rx": 3}
""")
    pytester.makepyfile("""
import conftest

def test_session(benchpod):
    assert benchpod.wiring.uart_rx == 3
    assert conftest.made["la_voltage"] == 1.8
""")
    result = pytester.runpytest("-p", "no:cacheprovider", "--benchpod-connection=10.0.0.9")
    result.assert_outcomes(passed=1)


TARGET_CONFTEST = """
import pytest
from embeddedci.benchpod import Wiring, pytest_plugin

powered = []

class FakePod:
    def __init__(self, connection, **kwargs):
        self.capabilities = type("C", (), {"la_pins": False})()
        self.wiring = Wiring(efuse=2)
    def power_on(self, efuse):
        powered.append(("on", efuse))
    def power_off(self, efuse):
        powered.append(("off", efuse))
    def close(self):
        pass

pytest_plugin.BenchPod = FakePod
"""


@pytest.mark.parametrize("flag, rail", [(None, 2), ("--benchpod-efuse=1", 1)])
def test_benchpod_target_powers_the_wiring_profiles_rail(pytester, flag, rail):
    pytester.makeconftest(TARGET_CONFTEST)
    pytester.makepyfile(f"""
import conftest

def test_target(benchpod_target):
    assert conftest.powered == [("on", {rail})]

def test_after():
    assert conftest.powered == [("on", {rail}), ("off", {rail})]
""")
    args = ["-p", "no:cacheprovider", "--benchpod-connection=10.0.0.9"] + ([flag] if flag else [])
    pytester.runpytest(*args).assert_outcomes(passed=2)
