"""LA pin ownership and GPIO: la_pins, gpio_mode / gpio_write / gpio_read / gpio_wait /
gpio_pulse / gpio_release, and the conflicts the pod raises."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from conftest import call, without_caps


def _pin(result, la):
    return next(p for p in result["pins"] if p["la"] == la)


# -- la_pins ----------------------------------------------------------------------------

def test_la_pins_reports_every_channel_with_levels(connected):
    connected.inputs = 1 << 10  # the DUT drives LA11 high
    result = call("la_pins")
    assert [p["la"] for p in result["pins"]] == list(range(1, 15))
    assert _pin(result, 1) == {"la": 1, "function": "none", "gpio": None, "level": None,
                               "pull": "up", "pull_ohms": "4.7k", "pull_on": False, "in_use": False}
    assert _pin(result, 7)["pull"] == "down" and _pin(result, 12)["pull"] is None
    assert _pin(result, 14)["pull"] is None
    assert {lv["la"]: lv["level"] for lv in result["levels"]}[11] == 1


def test_la_pins_omits_levels_without_the_gateware_support(connected):
    without_caps(connected, "gpio_read")
    connected.requests.clear()
    assert call("la_pins")["levels"] is None
    assert [r["cmd"] for r in connected.requests] == ["la_pins"]  # no fallback capture


def test_la_pins_shows_who_owns_a_channel(connected):
    call("uart_open", rx=5, tx=4)
    result = call("la_pins")
    assert _pin(result, 5)["function"] == "uart_rx" and _pin(result, 5)["in_use"] is True
    assert _pin(result, 4)["function"] == "uart_tx"


def test_la_pins_needs_the_firmware_capability(connected):
    without_caps(connected, "la_pins")
    with pytest.raises(ToolError, match="la_pins"):
        call("la_pins")


# -- configuring and driving -------------------------------------------------------------

def test_gpio_mode_claims_channels_in_one_command(connected):
    result = call("gpio_mode", la=[3, 4], mode="output", level=1)
    assert [p["la"] for p in result["pins"]] == [3, 4]
    assert result["pins"][0] == {"la": 3, "function": "gpio", "gpio": "output", "level": 1,
                                 "pull": "up", "pull_ohms": "2.2k", "pull_on": False, "in_use": True}
    assert connected.requests[-1] == {"cmd": "gpio", "la": [3, 4], "mode": "output", "level": 1}


def test_gpio_mode_defaults_and_input_rejects_a_level(connected):
    assert call("gpio_mode", la=[2])["pins"][0]["level"] == 0  # output starts low
    assert call("gpio_mode", la=[2], mode="open_drain")["pins"][0]["level"] == 1  # released
    assert call("gpio_mode", la=[2], mode="input")["pins"][0]["level"] is None
    with pytest.raises(ToolError, match="invalid argument"):
        call("gpio_mode", la=[2], mode="input", level=0)


def test_gpio_write_and_read_round_trip(connected):
    call("gpio_mode", la=[3, 4], mode="output")
    assert call("gpio_write", la=[3, 4], level=1) == {"la": [3, 4], "level": 1}
    levels = {lv["la"]: lv["level"] for lv in call("gpio_read", la=[3, 4])["levels"]}
    assert levels == {3: 1, 4: 1}
    call("gpio_write", la=[3], level=0)
    assert call("gpio_read")["levels"][2] == {"la": 3, "level": 0}  # all 14 channels, in order
    assert len(call("gpio_read")["levels"]) == 14


def test_gpio_write_on_an_unconfigured_channel_is_refused(connected):
    with pytest.raises(ToolError, match="is not a gpio output"):
        call("gpio_write", la=[6], level=1)


def test_gpio_release_frees_channels(connected):
    call("gpio_mode", la=[3, 4], mode="output")
    assert call("gpio_release", la=[3]) == {"released": [3]}
    assert call("la_pins")["pins"][2]["function"] == "none"
    assert call("gpio_release") == {"released": []}  # no argument = every GPIO channel
    assert connected.requests[-1] == {"cmd": "gpio", "la": "all", "mode": "off"}
    assert all(p["function"] == "none" for p in call("la_pins")["pins"])


# -- waiting and pulsing -------------------------------------------------------------------

def test_gpio_wait_returns_as_soon_as_the_level_is_there(connected):
    connected.inputs = 1 << 8  # LA9 is already high
    result = call("gpio_wait", la=9, level=1, timeout=1.0)
    assert result["reached"] is True and result["la"] == 9 and result["waited"] < 1.0


def test_gpio_wait_times_out_without_failing(connected):
    result = call("gpio_wait", la=9, level=1, timeout=0.05)
    assert result["reached"] is False and result["waited"] >= 0.05


def test_gpio_pulse_runs_a_step_train(connected):
    assert call("gpio_pulse", la=3, width=0.001, count=5) == {"la": 3, "count": 5, "width": 0.001}
    assert connected.requests[-1] == {"cmd": "la", "la": 3, "steps": 5, "delay_us": 1000}


# -- ownership conflicts reach the agent ------------------------------------------------------

def test_pin_conflict_names_the_owner_and_how_to_free_it(connected):
    call("uart_open", rx=5, tx=4)
    with pytest.raises(ToolError, match="PinConflictError: gpio: pin conflict: LA5 is in use by uart_rx"):
        call("gpio_mode", la=[5])
    call("uart_close")


def test_a_gpio_channel_blocks_the_uart_until_it_is_released(connected):
    call("gpio_mode", la=[5], mode="output")
    with pytest.raises(ToolError, match=r"PinConflictError:.*LA5 is in use by gpio"):
        call("uart_open", rx=5, tx=4)
    call("gpio_release", la=[5])
    assert call("uart_open", rx=5, tx=4)["open"] is True


def test_pull_conflict_reaches_the_agent(connected):
    call("set_pull", las=[7], enabled=True)
    with pytest.raises(ToolError, match="PullConflictError: .*pull-down engaged"):
        call("gpio_mode", la=[7], mode="open_drain")


def test_gpio_needs_the_firmware_capability(connected):
    without_caps(connected, "la_pins")
    with pytest.raises(ToolError, match="la_pins"):
        call("gpio_mode", la=[3])
    with pytest.raises(ToolError, match="la_pins"):
        call("gpio_release")
