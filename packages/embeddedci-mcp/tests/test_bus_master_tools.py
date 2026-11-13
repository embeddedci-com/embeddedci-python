"""The bus-master tools: i2c_open / i2c_transfer / i2c_scan / i2c_close, la_dir, SPI modes 1/2 and
capture_la's send, against the fake pod."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from conftest import call


def _sent(fake, cmd):
    return [r for r in fake.requests if r["cmd"] == cmd]


def test_i2c_session_transfer_scan_and_close(connected):
    r = call("i2c_open", hz=400_000)
    assert r == {"open": True, "sda": 1, "scl": 2, "hz": 400_000, "pullups": "pod"}
    t = call("i2c_transfer", addr=0x76, write_hex="d0", read=1)
    assert t == {"addr": 0x76, "ack": True, "rx_hex": "58", "nack_at": None}
    assert _sent(connected, "i2c_xfer")[-1] == {"cmd": "i2c_xfer", "addr": 0x76, "write": "0A", "read": 1}
    nack = call("i2c_transfer", addr=0x50)
    assert nack["ack"] is False and nack["nack_at"] == 0
    assert call("i2c_scan") == {"found": [0x76], "found_hex": ["0x76"]}
    assert call("status")["session"]["i2c_open"] is True
    assert call("i2c_close") == {"open": False, "sda": None, "scl": None, "hz": None, "pullups": None}
    assert connected.i2c is None
    with pytest.raises(ToolError, match="i2c_open"):
        call("i2c_scan")


def test_i2c_open_replaces_the_open_session(connected):
    call("i2c_open")
    call("i2c_open", sda=3, scl=4, pullups="external")
    assert [r["cmd"] for r in connected.requests if r["cmd"].startswith("i2c")][-3:] == [
        "i2c_start", "i2c_stop", "i2c_start"]
    assert connected.i2c["sda"] == 3


def test_i2c_refusals_name_the_tools():
    from embeddedci_mcp.server import _mcp_refusal_text

    assert "i2c_close" in _mcp_refusal_text(
        'pin conflict: LA1 is in use by i2cm_sda; stop the I2C session first ({"cmd":"i2c_stop"})')
    assert _mcp_refusal_text("no I2C session: send i2c_start first").endswith("call i2c_open first")
    text = _mcp_refusal_text('I2C needs pull-ups: switch on LA1\'s with {"cmd":"la","la":1,"pullup":"on"}, '
                             'or pass "pullups":"external"')
    assert "set_pull(las=[1], enabled=true)" in text and 'pullups="external"' in text


def test_la_dir_sets_lists_and_follows_the_wiring(connected):
    r = call("la_dir", la=[9, "uart_rx"], dir="dut_output")
    assert [(p["la"], p["dir"]) for p in r["pins"]] == [(9, "dut_output"), (5, "dut_output")]
    assert len(call("la_dir")["pins"]) == 14
    call("la_dir", from_wiring=True)
    assert connected.dirs[4] == "dut_input" and connected.dirs[1] == "bidir"
    with pytest.raises(ToolError, match="invalid argument"):
        call("la_dir", la=[9])


def test_set_wiring_sends_the_directions(connected):
    from embeddedci_mcp.session import SESSION

    connected.version = "3.10.4"
    SESSION.require().refresh_capabilities()
    r = call("set_wiring", profile={"uart_rx": 7, "uart_tx": 8,
                                    "signals": [{"name": "READY", "la": 9}],
                                    "dut_dirs": {"READY": "bidir"}})
    assert r["directions"] == "sent"
    assert connected.dirs[7] == "dut_output" and connected.dirs[8] == "dut_input"
    assert connected.dirs[9] == "bidir" and connected.dirs[5] == "unknown"
    pins = {p["la"]: p for p in call("wiring")["pins"]}
    assert pins[7]["dut_dir"] == "dut_output" and pins[9]["dut_dir"] == "bidir"
    assert pins[3]["dut_dir"] is None


def test_older_firmware_skips_the_directions(connected):
    r = call("set_wiring", profile={"uart_rx": 7, "uart_tx": 8})
    assert r["directions"] == "not sent: this pod's firmware (2.0.0) has no la_dir (3.10.4+)"
    assert 7 not in connected.dirs
    # Older firmware is not a problem to warn about.
    assert not any("pin directions" in w for w in call("status")["warnings"])


def test_spi_modes_1_and_2(connected):
    connected.caps.append("spi_master")
    from embeddedci_mcp.session import SESSION
    SESSION.require().refresh_capabilities()
    call("spi_transfer", tx_hex="9f00", sck=3, mosi=4, miso=5, cs=6, mode=2)
    assert _sent(connected, "spi_start")[-1]["mode"] == 2


def test_capture_la_i2c_send_auto_triggers_on_sda(connected):
    call("i2c_open")
    r = call("capture_la", samples=1000, send={"bus": "i2c", "addr": 0x76, "write_hex": "d0", "read": 1})
    req = _sent(connected, "la_capture")[-1]
    assert req["send"] == {"bus": "i2c", "addr": 0x76, "write": "0A", "read": 1}
    assert req["trigger"] == {"la": 1, "edge": "falling", "pre_samples": 64}
    assert r["send"] == {"bus": "i2c", "ack": True, "rx_hex": "58", "nack_at": None, "la": None,
                         "level": None, "error": None}


def test_capture_la_i2c_send_opens_a_session_from_the_wiring(connected):
    call("capture_la", samples=1000, send={"bus": "i2c", "addr": 0x76, "read": 1})
    assert [r["cmd"] for r in connected.requests if r["cmd"].startswith("i2c")] == ["i2c_start", "i2c_stop"]
    assert connected.i2c is None


def test_capture_la_spi_and_gpio_sends(connected):
    connected.caps.append("spi_master")
    from embeddedci_mcp.session import SESSION
    SESSION.require().refresh_capabilities()
    call("set_wiring", profile={"spi_sclk": 9, "spi_mosi": 10, "spi_miso": 11, "spi_cs": 12,
                                "swd_swclk": None, "swd_swdio": None})
    r = call("capture_la", samples=1000, send={"bus": "spi", "tx_hex": "9f00", "spi_mode": 1})
    req = _sent(connected, "la_capture")[-1]
    assert req["trigger"]["la"] == 12 and _sent(connected, "spi_start")[-1]["mode"] == 1
    assert r["send"]["bus"] == "spi" and r["send"]["rx_hex"] == "9f00"
    assert connected.spi is None
    r = call("capture_la", samples=1000, trigger_la=6, trigger_edge="rising",
             send={"bus": "gpio", "la": 6, "level": 1})
    req = _sent(connected, "la_capture")[-1]
    assert req["send"] == {"bus": "gpio", "la": 6, "level": 1}
    assert req["trigger"] == {"la": 6, "edge": "rising", "pre_samples": 64}
    assert r["send"]["la"] == 6 and r["send"]["level"] == 1


def test_capture_la_send_needs_its_fields(connected):
    with pytest.raises(ToolError, match="addr"):
        call("capture_la", send={"bus": "i2c"})
    with pytest.raises(ToolError, match="la and level"):
        call("capture_la", send={"bus": "gpio", "la": 6})
