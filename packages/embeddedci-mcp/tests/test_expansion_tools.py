"""The expansion tool and EXT pins in the gpio_* tools, against the fake pod's TCA9555 at 0x20."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from conftest import call


def test_status_lists_chips_when_nothing_is_attached(connected):
    result = call("expansion")
    assert result["attached"] is False and "tca9555" in result["chips"] and "74hc595" in result["chips"]


def test_attach_drive_read_and_detach(connected):
    result = call("expansion", chip="tca9555", addr=0x20, sda=1, scl=2,
                  names={"EXT1": "LED1"})
    assert result["attached"] and result["la"] == {"sda": 1, "scl": 2} and len(result["pins"]) == 16
    assert result["pins"][0]["name"] == "LED1" and result["pins"][0]["chip_pin"] == "P00"
    pins = call("gpio_mode", la=["LED1", "EXT2", 3], mode="output")["pins"]
    assert [p.get("pin") or p.get("la") for p in pins] == ["EXT1", "EXT2", 3]
    assert call("gpio_write", la=["EXT1", "EXT2"], level=[1, 0]) == {"la": ["EXT1", "EXT2"], "level": [1, 0]}
    assert connected.tca[2] & 0b11 == 0b01
    levels = call("gpio_read", la=["EXT1", 3])["levels"]
    assert levels == [{"la": "EXT1", "level": 1}, {"la": 3, "level": 0}]
    everything = call("gpio_read")["levels"]
    assert len(everything) == 14 + 16 and everything[14] == {"la": "EXT1", "level": 1}
    assert call("gpio_wait", la="EXT1", level=1, timeout=0.1)["reached"] is True
    assert call("gpio_release", la=["EXT1"])["released"] == ["EXT1"]
    assert call("expansion", detach=True)["attached"] is False
    assert connected.tca[6:8] == [0xFF, 0xFF]


def test_refusals(connected):
    with pytest.raises(ToolError, match="no expander is attached"):
        call("gpio_write", la=["EXT1"], level=1)
    with pytest.raises(ToolError, match="the SDK drives"):
        call("expansion", chip="ds2413")
    with pytest.raises(ToolError, match="0x21"):
        call("expansion", chip="tca9555", addr=0x21)
    call("expansion", chip="tca9555")
    with pytest.raises(ToolError, match="pulses are timed by the FPGA"):
        call("gpio_pulse", la="EXT1", width=0.001)
    with pytest.raises(ToolError, match="not both"):
        call("expansion", chip="tca9555", detach=True)
