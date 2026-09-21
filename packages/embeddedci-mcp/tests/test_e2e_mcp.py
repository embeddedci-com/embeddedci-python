"""End-to-end: the MCP server's tools against a real BenchPod (in-process, through FastMCP).

    pytest packages/embeddedci-mcp/tests/test_e2e_mcp.py --benchpod-connection=<pod host> \
        [--benchpod-firmware=<scenario-sensors.elf>]

Skips without a connection. The DUT part expects the examples/scenario-sensors-stm32 app with its
UART on BENCHPOD_E2E_UART_RX/TX (default LA3/LA4) and SWD on LA11/LA12.
"""

from __future__ import annotations

import os
import time

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from conftest import call

pytestmark = pytest.mark.hardware

LA_VOLTAGE = 3.3  # the bench board's I/O voltage — change to 1.8 for a 1V8 board


def _pin(name: str, default: int) -> int:
    return int(os.environ.get(f"BENCHPOD_E2E_{name}", default))


@pytest.fixture
def connected(benchpod_connection):
    status = call("connect", connection=benchpod_connection, la_voltage=LA_VOLTAGE)
    assert status["connected"] and status["la_voltage"] == LA_VOLTAGE, status
    yield status
    for tool, args in (("dac_stop", {}), ("analog_path", {"path": "off"}), ("can_close", {}),
                       ("disable_i2c_sensor", {}), ("disconnect", {})):
        try:
            call(tool, **args)
        except ToolError:
            pass


def test_status_and_typed_state(connected):
    assert connected["capabilities"]["board"]
    assert connected["warnings"] == []
    assert "internal" in call("power_status")
    channels = call("pull_status")["channels"]
    assert [c["direction"] for c in channels] == ["up"] * 6 + ["down"] * 2


def test_analog_round_trip(connected):
    out = call("dac_output", path="5v", volts=2.5)
    assert out["path"] == "5v" and abs(out["voltage"] - 2.5) < 0.05
    reading = call("adc_read", source="cal1")
    assert abs(reading["voltage"] - 2.5) < 0.1, reading


def test_generate_capture_summary_and_replay(connected):
    pytest.importorskip("numpy")
    call("analog_path", path="cal1")
    call("generate", waveform="sine", freq_hz=200, amplitude=0.8, offset=1.5, route=False)
    time.sleep(0.3)
    summary = call("capture_adc", samples=2000, sample_rate_hz=20_000, points=50)
    assert abs(summary["dominant_frequency_hz"] - 200) < 10 and len(summary["envelope_min"]) == 50
    call("dac_stop")
    replay = call("replay", from_last_capture=True, dac_path="5v", route=False)
    assert replay["samples"] == 2000 and not replay["deep"]


def test_logic_capture_and_decode(connected):
    la = call("capture_la", samples=8192, sample_rate_hz=1_000_000)
    assert la["samples"] == 8192 and len(la["channels"]) == 12
    assert call("decode_la", protocol="uart", rx=5, baud=115200)["protocol"] == "uart"


def test_i2c_sensor_and_can(connected):
    call("enable_i2c_sensor", sda=_pin("I2C_SDA", 2), scl=_pin("I2C_SCL", 1))
    assert call("i2c_sensor_regs", start=0xD0, length=1)["bytes"] == [0x58]
    call("disable_i2c_sensor")
    if connected["capabilities"]["board"] == "stm32h563":
        call("can_open", mode="internal")
        call("can_write", can_id=0x321, data=[7, 8])
        frames = call("can_read", can_id=0x321, timeout=1.0)
        assert frames["matched"] and frames["frames"][0]["data"] == [7, 8]
        call("can_close")


def test_firmware_refusals_are_tool_errors(connected):
    if connected["firmware"].get("nrst_pin"):
        pytest.skip("rev3 pod: the reset pin exists")
    with pytest.raises(ToolError, match="FirmwareError"):
        call("reset_target")
    with pytest.raises(ToolError, match="FirmwareError"):
        call("set_la_voltage", voltage=1.8)


def test_rev3_tools(connected):
    """The reset pin, the 1.8 V bank and USB-C CC through the tools an agent would call."""
    expected = os.environ.get("BENCHPOD_E2E_BOARD_REV", "")
    if expected:
        assert connected["firmware"].get("board_rev") == expected, connected["firmware"]
    if not connected["firmware"].get("nrst_pin"):
        pytest.skip("not a rev3 pod")
    caps = connected["capabilities"]
    assert caps["board_rev"] == "v3" and caps["nrst_pin"] and caps["usb_cc"], caps
    try:
        assert call("set_la_voltage", voltage=1.8) == {"voltage": 1.8, "readback": 1.8}
    finally:
        assert call("set_la_voltage", voltage=3.3)["readback"] == 3.3
    try:
        assert call("reset_target", action="hold")["asserted"] is True
        assert call("reset_target", action="status")["asserted"] is True
    finally:
        assert call("reset_target", action="release")["asserted"] is False
    assert call("reset_target", action="pulse", pulse=0.05)["asserted"] is False
    with pytest.raises(ToolError):
        call("reset_target", pulse=5)                   # longer than the pod can hold
    cc = call("command", request={"cmd": "usb_cc"})
    assert cc["supported"] is True and cc["orientation"] in ("none", "cc1", "cc2"), cc


def test_tools_switch_the_gateware_image(connected):
    if not (connected["capabilities"]["dac_control_loop"] or connected["capabilities"]["dac_deep_replay"]):
        pytest.skip("the pod has a single gateware image")
    try:
        call("fpga_image", image="deep_replay")
        armed = call("control_loop", curve=[0, 30000], source="fixed", input_code=0)
        assert armed["switched_image"] == "loop"
        call("dac_stop")
        with pytest.raises(ToolError, match="switch_image"):
            call("replay", volts=[1.0] * 4096, switch_image=False)
        res = call("replay", volts=[1.0] * 4096, route=False)
        assert res["deep"] is True and res["switched_image"] == "deep_replay"
    finally:
        call("dac_stop")
        call("fpga_image", image="loop")


def test_dut_flash_boot_and_console(connected, pytestconfig):
    firmware = pytestconfig.getoption("benchpod_firmware")
    if not firmware:
        pytest.skip("pass --benchpod-firmware to run the DUT part")
    rx, tx = _pin("UART_RX", 3), _pin("UART_TX", 4)
    flash = call("flash", swclk=_pin("SWCLK", 11), swdio=_pin("SWDIO", 12),
                 target="target/stm32f4x.cfg", file=os.path.abspath(firmware), target_power=1)
    assert flash["ok"], flash
    try:
        boot = call("power_cycle_and_capture", rx=rx, tx=tx, delay=0.5, duration=25, until_regex="APP_OK")
        assert boot["matched"], boot["text"][-1000:]
        call("power_off")
        call("uart_open", rx=rx, tx=tx)
        call("power_on")
        assert call("uart_read", until_regex="APP_OK", timeout=25)["matched"]
        call("uart_read", until_regex="> ", timeout=5)
        call("uart_write", text="help", line_ending="none")
        call("uart_write", text="\r", line_ending="none")
        assert call("uart_read", until_regex="commands:", timeout=5)["matched"]
        call("uart_close")
    finally:
        call("power_off")
