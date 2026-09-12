"""Exercise every MCP tool through FastMCP, without hardware (fake transport injected)."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from embeddedci.benchpod.flash import FlashResult

from embeddedci_mcp.guide import INSTRUCTIONS
from embeddedci_mcp.server import mcp
from embeddedci_mcp.session import SESSION

from conftest import call

EXPECTED_TOOLS = {
    "connect", "disconnect", "status", "set_la_voltage",
    "wiring", "set_wiring",
    "power_on", "power_off", "power_status", "reset_target",
    "measure_power", "power_profile_start", "power_profile_stop",
    "la_pins", "gpio_mode", "gpio_write", "gpio_read", "gpio_wait", "gpio_pulse", "gpio_release",
    "flash",
    "capture_uart", "power_cycle_and_capture", "uart_open", "uart_write", "uart_read", "uart_close",
    "enable_i2c_sensor", "set_i2c_sensor", "disable_i2c_sensor", "i2c_sensor_status",
    "i2c_sensor_regs", "i2c_sensor_capture",
    "set_pull", "pull_status",
    "analog_path", "dac_output", "adc_read",
    "capture_adc", "capture_la", "capture_correlated", "decode_la", "la_timing",
    "generate", "dac_stop", "replay", "list_waveforms", "replay_waveform",
    "save_capture_as_recording",
    "control_loop", "loop_input", "loop_probe", "fpga_image",
    "can_open", "can_write", "can_read", "can_respond", "can_status", "can_close",
    "la_step", "command",
}


# -- registration + metadata ---------------------------------------------------------

def test_tool_set():
    import anyio

    names = {t.name for t in anyio.run(mcp.list_tools)}
    assert names == EXPECTED_TOOLS


def test_every_tool_is_described_annotated_and_structured():
    import anyio

    for tool in anyio.run(mcp.list_tools):
        assert tool.description and len(tool.description) > 20, tool.name
        assert tool.annotations is not None and tool.annotations.title, tool.name
        assert tool.outputSchema is not None, tool.name
        if not tool.annotations.readOnlyHint:
            assert tool.annotations.destructiveHint is not None, tool.name


def test_instructions_carry_the_session_start():
    assert mcp.instructions == INSTRUCTIONS
    for needle in ("connect", "set_la_voltage", "LA7/LA8", "flash runs OpenOCD",
                   "Call `wiring` first", "gpio_release", "pin conflict", "trigger_la",
                   "measure_power"):
        assert needle in INSTRUCTIONS


def test_enums_reach_the_schema():
    import anyio

    tools = {t.name: t for t in anyio.run(mcp.list_tools)}
    assert tools["analog_path"].inputSchema["properties"]["path"]["enum"][0] == "off"
    assert tools["set_la_voltage"].inputSchema["properties"]["voltage"]["enum"] == [1.8, 3.3]


# -- errors + connection ---------------------------------------------------------------

def test_tool_before_connect_is_a_tool_error():
    with pytest.raises(ToolError, match="NotConnectedError"):
        call("power_on")


def test_status_when_not_connected_explains_how():
    result = call("status")
    assert result["connected"] is False and "connect" in result["warnings"][0]


def test_connect_reports_status_and_la_voltage(fake_open, fake_transport):
    result = call("connect", connection="192.168.1.50", la_voltage=3.3)
    assert result["connected"] is True and result["kind"] == "tcp"
    assert result["la_voltage"] == 3.3 and result["warnings"] == []
    assert result["capabilities"]["board"] == "stm32h563"
    assert fake_open["la_voltage"] == 3.3 and fake_open["lease_wait"] == 30.0


def test_connect_without_la_voltage_warns(fake_open):
    result = call("connect", connection="usb")
    assert result["kind"] == "serial" and result["la_voltage"] is None
    assert any("set_la_voltage" in w for w in result["warnings"])


def test_connect_rolls_back_when_the_pod_does_not_answer(fake_open, fake_transport, monkeypatch):
    from embeddedci.benchpod.errors import TransportError

    def unreachable():
        raise TransportError("could not connect to 10.0.0.9:8080")

    monkeypatch.setattr(fake_transport, "status", unreachable)
    with pytest.raises(ToolError, match="TransportError"):
        call("connect", connection="10.0.0.9")
    assert not SESSION.connected and call("status")["connected"] is False


def test_connect_uses_the_server_default(fake_open):
    SESSION.default_connection = "embeddedci:bench-1"
    SESSION.default_la_voltage = 1.8
    call("connect")
    assert fake_open["connection"] == "embeddedci:bench-1" and fake_open["la_voltage"] == 1.8


def test_schema_rejects_bad_arguments(connected):
    with pytest.raises(ToolError):
        call("set_la_voltage", voltage=5.0)
    with pytest.raises(ToolError):
        call("power_on", efuse=3)


def test_sdk_value_errors_become_tool_errors(connected):
    with pytest.raises(ToolError, match="invalid argument"):
        call("generate", waveform="sine", freq_hz=10, amplitude=0.0001, dac_path="3v3")


def test_disconnect(connected):
    assert call("disconnect")["connected"] is False
    assert not SESSION.connected
    assert ("close",) in connected.calls


# -- power + flash ---------------------------------------------------------------------

def test_power_tools(connected):
    assert call("power_on", efuse=1, delay=0.5) == {"efuse": 1, "on": True, "delay": 0.5}
    assert ("target_power", 1, True, 500) in connected.calls
    status = call("power_status")
    assert status["internal"]["enabled"] is True and status["internal"]["bus_voltage"] == 5.01
    assert status["internal"]["current"] == pytest.approx(0.042)
    assert status["external"]["fault"] is True
    call("power_off")
    assert connected.power[1] is False


def test_reset_target(connected):
    assert call("reset_target")["asserted"] is False
    assert connected.requests[-1] == {"cmd": "nrst", "pulse_ms": 100}
    assert call("reset_target", action="hold")["asserted"] is True
    assert call("reset_target", action="status")["asserted"] is True
    assert call("reset_target", action="release")["asserted"] is False


def test_flash_returns_outcome_not_error(connected, monkeypatch):
    def fake_flash(**kwargs):
        assert kwargs["check"] is False and kwargs["nreset"] is True
        return FlashResult(ok=False, returncode=1, stdout="", stderr="x" * 10_000,
                           target_unreachable=True)

    monkeypatch.setattr(SESSION.require(), "flash", fake_flash)
    result = call("flash", swclk=11, swdio=12, nreset=True, target="target/stm32f4x.cfg", file="a.elf")
    assert result["ok"] is False and result["target_unreachable"] is True
    assert len(result["stderr_tail"]) <= 4000


# -- UART ------------------------------------------------------------------------------

def test_capture_uart(connected):
    result = call("capture_uart", rx=5, tx=4, duration=1.0, until_regex="APP_OK")
    assert result["matched"] is True and "boot" in result["text"] and result["truncated"] is False


def test_bad_regex_is_a_tool_error(connected):
    with pytest.raises(ToolError, match="until_regex"):
        call("capture_uart", rx=5, tx=4, duration=1.0, until_regex="(")


def test_power_cycle_and_capture(connected):
    result = call("power_cycle_and_capture", rx=5, tx=4, delay=0.2, duration=1.0, off_settle=0)
    assert "APP_OK" in result["text"]
    assert [c for c in connected.calls if c[0] == "target_power"] == [
        ("target_power", 1, False, 0), ("target_power", 1, True, 200)]


def test_uart_session_read_write(connected):
    with pytest.raises(ToolError, match="uart_open"):
        call("uart_read")
    assert call("uart_open", rx=5, tx=4)["open"] is True
    first = call("uart_read", until_regex="APP_OK", timeout=2)
    assert first["matched"] is True and "boot" in first["text"]
    assert call("uart_read", timeout=0)["text"] == ""  # nothing new since the last read
    assert call("uart_write", text="help")["written"] == 5
    assert bytes(connected.uart_links[-1].written) == b"help\n"
    assert call("status")["session"]["uart_open"] is True
    assert call("uart_close")["open"] is False


# -- I2C sensor + pulls ----------------------------------------------------------------

def test_i2c_sensor_tools(connected):
    call("set_pull", las=[1, 2], enabled=True)
    assert call("enable_i2c_sensor", sda=2, scl=1, temperature_c=22.5)["reply"]["type"] == "bmp280"
    assert call("i2c_sensor_status")["reply"]["active"] is True
    assert call("i2c_sensor_regs", start=0, length=4)["bytes"] == [0, 1, 2, 3]
    cap = call("i2c_sensor_capture", address=0x76, register=0xD0)
    assert cap["transactions"] == 0 and cap["addressed"] is False and cap["register_value"] is None
    assert call("disable_i2c_sensor") == {"stopped": True}


def test_pulls(connected):
    res = call("set_pull", las=[7], enabled=True)
    assert res["channels"][0]["direction"] == "down"
    status = call("pull_status")
    assert [c["la"] for c in status["channels"]] == list(range(1, 9))
    with pytest.raises(ToolError):
        call("set_pull", las=[9], enabled=True)


# -- analog + captures -----------------------------------------------------------------

def test_analog_tools(connected):
    assert call("analog_path", path="cal1") == {"path": "cal1"}
    out = call("dac_output", path="5v", volts=2.5)
    assert out == {"path": "5v", "voltage": 2.5, "code": 128}
    assert call("dac_output", path="off")["voltage"] is None
    assert call("adc_read", source="ext")["voltage"] == pytest.approx(3.301)


def test_capture_adc_summary_is_agent_sized(connected):
    result = call("capture_adc", samples=2000, sample_rate_hz=100_000, points=50)
    assert result["samples"] == 2000 and result["sample_rate_hz"] == 100_000
    assert result["mean"] == pytest.approx(2.5, abs=0.05)
    assert result["peak_to_peak"] == pytest.approx(2.0, abs=0.05)
    import importlib.util

    if importlib.util.find_spec("numpy"):  # a dependency via embeddedci[analysis]
        assert result["dominant_frequency_hz"] == pytest.approx(1000, rel=0.02)
    assert len(result["envelope_min"]) == 50 and result["envelope_step"] == pytest.approx(40 / 100_000)
    assert SESSION.last_adc is not None and len(SESSION.last_adc) == 2000


def test_capture_la_and_decode_reuses_the_capture(connected):
    result = call("capture_la", samples=1000, sample_rate_hz=1e6)
    la1 = result["channels"][0]
    assert la1["edges"] == 19 and la1["est_frequency_hz"] == pytest.approx(9500, rel=0.01)
    assert result["channels"][2]["high_fraction"] == 1.0
    before = len(connected.requests)
    decoded = call("decode_la", protocol="uart", rx=1, baud=9600)
    assert decoded["protocol"] == "uart" and len(connected.requests) == before  # no new capture
    with pytest.raises(ToolError, match="sda and scl"):
        call("decode_la", protocol="i2c", sda=2)


def test_capture_correlated(connected):
    result = call("capture_correlated", adc_samples=500, la_samples=100, points=10)
    assert result["adc"]["samples"] == 500 and result["la"]["samples"] == 100
    assert SESSION.last_la is not None and SESSION.last_adc is not None


# -- DAC -------------------------------------------------------------------------------

def test_generate_and_stop(connected):
    result = call("generate", waveform="sine", freq_hz=1000, amplitude=1.0, on_capture=True)
    assert result == {"waveform": "sine", "freq_hz": 1000.0, "dac_path": "5v", "cotrig": True}
    assert connected.requests[-2] == {"cmd": "dac_out", "path": "5v"}
    assert connected.requests[-1]["amplitude"] == 51
    assert call("dac_stop") == {"stopped": True}
    # route=false keeps a loopback set up with analog_path: no dac_out before the generator.
    call("analog_path", path="cal1")
    call("generate", waveform="square", freq_hz=100, amplitude=1.0, route=False)
    assert [r["cmd"] for r in connected.requests[-2:]] == ["analog_path", "generate"]


def test_replay_sources(connected):
    with pytest.raises(ToolError, match="exactly one"):
        call("replay")
    with pytest.raises(ToolError, match="capture_adc"):
        call("replay", from_last_capture=True)
    res = call("replay", volts=[0.0, 2.5, 5.0], fault={"type": "flatline", "start": 0, "width": 1})
    assert res["samples"] == 3 and res["deep"] is False
    call("capture_adc", samples=64, sample_rate_hz=100_000)
    res = call("replay", from_last_capture=True, on_capture=True)
    assert res["samples"] == 64 and res["cotrig"] is True and res["sample_rate_hz"] == 100_000


def test_save_recording_needs_a_capture(connected):
    with pytest.raises(ToolError, match="capture_adc"):
        call("save_capture_as_recording", name="x")


# -- control loop + gateware -------------------------------------------------------------

def test_control_loop_tools(connected):
    armed = call("control_loop", curve=[0, 30000, 60000], source="fixed", input_code=100,
                 input_map={"mv_per_unit": 2.0, "range_min": 0, "range_max": 500})
    assert armed["armed"] is True and armed["source"] == "fixed"
    arm_req = [r for r in connected.requests if r["cmd"] == "dac_control_loop"][-1]
    assert arm_req["in_mv_per_unit"] == 2.0 and "curve" in arm_req
    assert call("loop_input", input_code=500)["output_code"] == 1234
    probe = call("loop_probe")
    assert probe["loop_input"] == 32768 and probe["i"] == 64000
    img = call("fpga_image", image="loop")
    assert img == {"image": "loop", "version": 30, "features": 1}


# -- CAN ------------------------------------------------------------------------------

def test_can_tools(connected):
    with pytest.raises(ToolError, match="can_open"):
        call("can_write", can_id=0x123, data=[1])
    assert call("can_open", mode="internal")["open"] is True
    call("can_write", can_id=0x123, data=[0xDE, 0xAD])
    frames = call("can_read", can_id=0x123, timeout=0.5)
    assert frames["matched"] is True and frames["frames"][0]["data"] == [0xDE, 0xAD]
    assert frames["frames"][0]["id_hex"] == "0x123"
    assert call("can_respond", match_id=0x7DF, reply_id=0x7E8, reply_data=[0x50])["rules"] == 1
    assert call("can_respond", clear=True)["cleared"] is True
    with pytest.raises(ToolError, match="match_id"):
        call("can_respond")
    assert call("can_status")["reply"]["mode"] == "internal"
    assert call("can_close")["open"] is False
    with pytest.raises(ToolError):
        call("can_write", can_id=1, data=list(range(9)))


# -- stepper + escape hatch --------------------------------------------------------------

def test_la_step_and_command(connected):
    assert call("la_step", la=3, steps=100, delay=0.001) == {"la": 3, "steps": 100, "delay": 0.001,
                                                             "status": "started"}
    assert connected.requests[-1]["delay_us"] == 1000
    assert call("command", request={"cmd": "adc_read", "source": "cal1"})["reply"]["source"] == "cal1"
    with pytest.raises(ToolError):
        call("command", request={"samples": 1})
