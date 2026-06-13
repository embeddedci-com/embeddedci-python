"""Exercise the MCP tools without hardware (fake transport injected)."""

from __future__ import annotations

import asyncio

from embeddedci.benchpod import BenchPod
from embeddedci.benchpod.connection import ConnSpec
from embeddedci.benchpod.flash import FlashResult

import embeddedci.benchpod.connection as conn_mod
import embeddedci_mcp.session as session_mod
from embeddedci_mcp import server
from embeddedci_mcp.session import SESSION


# -- not connected ----------------------------------------------------------

def test_tool_before_connect_returns_structured_error():
    result = server.ping()
    assert result["ok"] is False
    assert result["error_type"] == "NotConnectedError"
    assert "connect" in result["error"].lower()


# -- connect ----------------------------------------------------------------

def test_connect_reports_status(monkeypatch, fake_transport):
    monkeypatch.setattr(
        session_mod, "BenchPod",
        lambda conn, timeout=30.0: BenchPod(transport=fake_transport),
    )
    monkeypatch.setattr(
        conn_mod, "resolve_connection",
        lambda c: ConnSpec(kind="tcp", addr="1.2.3.4:8080"),
    )
    result = server.connect("1.2.3.4")
    assert result["connected"] is True
    assert result["kind"] == "tcp"
    assert result["target"] == "1.2.3.4:8080"
    assert result["status"]["version"] == "fake-1.0"
    assert SESSION.connected


def test_disconnect(connected):
    assert SESSION.connected
    assert server.disconnect() == {"connected": False}
    assert not SESSION.connected
    # idempotent: disconnecting again is safe
    assert server.disconnect() == {"connected": False}


# -- status / ping ----------------------------------------------------------

def test_ping(connected):
    assert server.ping() == {"ping": "pong"}


def test_status(connected):
    assert server.status()["version"] == "fake-1.0"


# -- power ------------------------------------------------------------------

def test_power_on_off(connected):
    on = server.power_on(efuse=1)
    assert on["ok"] is True and on["on"] is True
    assert connected.power[1] is True

    off = server.power_off(efuse=1)
    assert off["on"] is False
    assert connected.power[1] is False


def test_target_power_explicit(connected):
    assert server.target_power(efuse=2, on=True, delay=0.5)["efuse"] == 2
    assert connected.power[2] is True


def test_target_status(connected):
    assert server.target_status()["efuse1"]["enabled"] == 1


# -- flash ------------------------------------------------------------------

def test_flash_serializes_result(connected, monkeypatch):
    monkeypatch.setattr(
        SESSION._pod, "flash",
        lambda **kw: FlashResult(ok=True, returncode=0,
                                 stdout="Programming Finished", stderr=""),
    )
    result = server.flash(swclk=11, swdio=12, nreset=3,
                          target="target/stm32f4x.cfg", file="fw.elf")
    assert result["ok"] is True
    assert result["returncode"] == 0
    assert "Programming Finished" in result["stdout_tail"]
    assert result["target_unreachable"] is False


# -- UART -------------------------------------------------------------------

def test_capture_uart_matches(connected):
    result = server.capture_uart(rx=5, tx=4, duration=1.0, until_regex="APP_OK")
    assert result["matched"] is True
    assert "APP_OK" in result["text"]
    assert "APP_OK" in result["lines"]


def test_power_cycle_and_capture(connected):
    result = server.power_cycle_and_capture(
        rx=5, tx=4, efuse=1, delay=0.0, duration=1.0, until_regex="APP_OK",
        off_settle=0.0,
    )
    assert result["matched"] is True
    # power was cycled off then scheduled back on
    assert ("target_power", 1, False, 0) in connected.calls


# -- I2C sensor -------------------------------------------------------------

def test_i2c_sensor_lifecycle(connected):
    started = server.enable_i2c_sensor(sda=2, scl=1, temperature_c=22.5,
                                       pressure_pa=101000)
    assert started["type"] == "bmp280"
    status = server.i2c_sensor_status()
    assert status["active"] is True
    assert status["transactions"] == 3
    assert server.disable_i2c_sensor() == {"ok": True}


def test_i2c_sensor_regs_summary(connected):
    regs = server.i2c_sensor_regs(start=0, length=8)
    assert regs["length"] == 8
    assert regs["bytes"] == list(range(8))


# -- pull-ups ---------------------------------------------------------------

def test_pullups(connected):
    assert server.enable_pullup([1, 2])["la_pullup_mask"] == 3
    assert server.disable_pullup([1, 2])["la_pullup_mask"] == 3
    assert server.pullup_status()["la_pullup_mask"] == 3


# -- low-level --------------------------------------------------------------

def test_command_escape_hatch(connected):
    assert server.command({"cmd": "status"})["version"] == "fake-1.0"


def test_capture_adc_summary(connected):
    summary = server.capture_adc(samples=4)
    assert summary["count"] == 4
    assert summary["head"] == [0, 1, 2, 3]
    assert summary["min"] == 0 and summary["max"] == 3


def test_measure_summary(connected):
    summary = server.measure(waveform="sine", freq=1000, amplitude=1.0, samples=4)
    assert summary["count"] == 4
    assert summary["max"] == 40


# -- tool / resource registration ------------------------------------------

def test_all_tools_registered():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    expected = {
        "connect", "disconnect", "ping", "status",
        "power_on", "power_off", "target_power", "target_status",
        "flash", "capture_uart", "power_cycle_and_capture",
        "enable_i2c_sensor", "set_i2c_sensor", "disable_i2c_sensor",
        "i2c_sensor_status", "i2c_sensor_regs", "i2c_sensor_la_decoded",
        "i2c_read_register",
        "enable_pullup", "disable_pullup", "pullup_status",
        "command", "gpio_set", "capture_adc", "signal_generate", "measure",
    }
    assert expected <= names

    # the flash tool's schema exposes its key parameters
    flash = next(t for t in tools if t.name == "flash")
    props = flash.inputSchema["properties"]
    for param in ("swclk", "swdio", "nreset", "target", "file", "verify"):
        assert param in props


def test_resources_registered():
    resources = asyncio.run(server.mcp.list_resources())
    uris = {str(r.uri) for r in resources}
    assert "benchpod://wiring" in uris
    assert "benchpod://help" in uris
