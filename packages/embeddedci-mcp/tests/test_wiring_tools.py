"""The wiring profile: the `wiring` / `set_wiring` tools, the resource, and the tools whose
channel, baud, rail and SWD arguments now default to the profile."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from embeddedci.benchpod import Signal, Wiring

from embeddedci_mcp.server import mcp, wiring_resource
from embeddedci_mcp.session import SESSION

from conftest import call

BENCH = {"efuse": 2, "uart_rx": 7, "uart_tx": 8, "uart_baud": 9600, "i2c_sda": 3, "i2c_scl": 4,
         "i2c_addr": "0x77", "swd_swclk": 9, "swd_swdio": 10, "swd_nreset": True,
         "swd_target": "target/stm32f4x.cfg",
         "signals": [{"name": "READY", "la": 6, "direction": "input", "active_low": True,
                      "description": "high when the DUT finished"}]}


def _set(profile=None):
    """Give the connected pod a wiring profile without going through the server API."""
    SESSION.require().wiring = Wiring.from_dict(BENCH if profile is None else profile)


# -- the wiring tool ------------------------------------------------------------------

def test_wiring_reports_the_effective_profile_and_pin_table(connected):
    _set()
    result = call("wiring")
    assert result["efuse"] == 2 and result["uart_baud"] == 9600 and result["i2c_address"] == 0x77
    assert result["swd_nreset"] is True and result["swd_target"] == "target/stm32f4x.cfg"
    assert [p["la"] for p in result["pins"]] == list(range(1, 13))
    by_la = {p["la"]: p for p in result["pins"]}
    assert by_la[7]["wired_to"] == "uart_rx" and by_la[7]["pull"] == "10k down"
    assert by_la[6]["wired_to"] == "READY" and by_la[12]["wired_to"] is None
    assert by_la[12]["pull"] is None  # LA9-LA12 have no bias resistor
    assert result["signals"][0]["name"] == "READY" and result["signals"][0]["active_low"] is True
    assert result["profile"]["uart_rx"] == 7 and result["saved"] is False


def test_wiring_reports_risky_wiring_as_warnings(connected):
    _set()
    # uart_rx on LA7 (a pull-DOWN channel) and i2c on LA3/LA4 are allowed but worth saying.
    assert any("uart_rx is on LA7" in w for w in call("wiring")["warnings"])


def test_wiring_defaults_when_the_bench_has_no_profile(connected):
    result = call("wiring")
    assert result["source"] == "defaults" and result["efuse"] == 1
    assert {p["la"]: p["wired_to"] for p in result["pins"]}[5] == "uart_rx"


# -- set_wiring -----------------------------------------------------------------------

def test_set_wiring_applies_the_profile_to_the_session(connected):
    result = call("set_wiring", profile=BENCH)
    assert result["efuse"] == 2 and result["saved"] is False
    assert SESSION.require().wiring.uart_rx == 7
    call("power_on")
    assert ("target_power", 2, True, 0) in connected.calls  # the new rail is in force


def test_set_wiring_rejects_an_invalid_profile(connected):
    with pytest.raises(ToolError, match="invalid argument"):
        call("set_wiring", profile={"uart_rx": 4, "uart_tx": 4})  # both roles on LA4
    with pytest.raises(ToolError, match="unknown key"):
        call("set_wiring", profile={"uart_rxx": 4})
    assert SESSION.require().wiring.source == "defaults"  # nothing changed


def test_set_wiring_save_needs_a_cloud_device(connected):
    with pytest.raises(ToolError, match="cloud device"):
        call("set_wiring", profile=BENCH, save=True)


def test_set_wiring_save_stores_it_on_the_server(connected, monkeypatch):
    saved = {}
    monkeypatch.setattr(SESSION.require(), "save_wiring", lambda *a: saved.setdefault("hit", True))
    assert call("set_wiring", profile=BENCH, save=True)["saved"] is True
    assert saved == {"hit": True}


# -- the benchpod://wiring resource ------------------------------------------------------

def test_wiring_resource_is_static_without_a_connection():
    text = wiring_resource()
    assert "BenchPod wiring reference" in text and "this bench" not in text.lower()


def test_wiring_resource_shows_the_connected_profile_first(connected):
    _set()
    text = wiring_resource()
    assert text.index("This bench's wiring profile") < text.index("BenchPod wiring reference")
    assert "LA7   uart_rx" in text and "eFuse 2" in text


def test_wiring_resource_is_registered():
    import anyio

    uris = {str(r.uri) for r in anyio.run(mcp.list_resources)}
    assert "benchpod://wiring" in uris


# -- arguments that now default to the profile ---------------------------------------------

def test_uart_tools_take_channels_and_baud_from_the_profile(connected):
    _set()
    result = call("uart_open")
    assert result == {"open": True, "rx": 7, "tx": 8, "baud": 9600}
    call("uart_close")
    call("capture_uart", duration=0.2)
    assert connected.uart_links[-1] is not None
    call("power_cycle_and_capture", delay=0.1, duration=0.3, off_settle=0)
    assert [c for c in connected.calls if c[0] == "target_power"][-2:] == [
        ("target_power", 2, False, 0), ("target_power", 2, True, 100)]  # the profile's rail


def test_channel_arguments_accept_wiring_names(connected):
    _set()
    assert call("uart_open", rx="READY", tx=8)["rx"] == 6
    with pytest.raises(ToolError, match="not a signal or role"):
        call("uart_open", rx="NOPE", tx=8)


def test_power_tools_report_the_resolved_rail(connected):
    _set()
    assert call("power_on")["efuse"] == 2
    assert call("power_off", efuse=1)["efuse"] == 1  # an explicit rail still wins


def test_i2c_sensor_takes_the_profile_channels_and_address(connected):
    _set()
    call("enable_i2c_sensor")
    start = [r for r in connected.requests if r["cmd"] == "sensor_start"][-1]
    assert start["sda"] == 3 and start["scl"] == 4 and start["addr"] == "0x77"


def test_flash_takes_the_profile_pins_target_and_reset(connected, monkeypatch):
    from embeddedci.benchpod.flash import FlashResult

    seen = {}

    def fake_flash(**kwargs):
        seen.update(kwargs)
        return FlashResult(ok=True, returncode=0, stdout="", stderr="")

    _set()
    monkeypatch.setattr(SESSION.require(), "flash", fake_flash)
    assert call("flash", file="a.elf")["ok"] is True
    assert seen["swclk"] == 9 and seen["swdio"] == 10 and seen["nreset"] is True
    assert seen["target"] == "target/stm32f4x.cfg"


def test_an_unwired_role_says_so(connected):
    _set({"uart_rx": None, "uart_tx": 4})
    with pytest.raises(ToolError, match="the wiring profile has no uart_rx"):
        call("uart_open")


def test_signals_reach_gpio_and_trigger_arguments(connected):
    SESSION.require().wiring = Wiring(signals=[Signal("TRIGGER", 9, direction="output")])
    assert call("gpio_mode", la=["TRIGGER"])["pins"][0]["la"] == 9
    assert call("capture_la", samples=64, sample_rate_hz=1e6,
                trigger_la="TRIGGER")["trigger"] == "LA9 rising"
