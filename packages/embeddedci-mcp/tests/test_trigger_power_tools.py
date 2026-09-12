"""Triggered captures (trigger_la / trigger_edge / trigger_timeout) and the power-profile tools."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from embeddedci_mcp.session import SESSION

from conftest import call, without_caps


# -- triggers -----------------------------------------------------------------------------

def test_capture_la_sends_the_trigger_and_reports_it(connected):
    result = call("capture_la", samples=500, sample_rate_hz=1e6, trigger_la=9,
                  trigger_edge="falling", trigger_timeout=2.5)
    req = [r for r in connected.requests if r["cmd"] == "la_capture"][-1]
    assert req["trigger"] == {"la": 9, "edge": "falling"} and req["trigger_timeout_ms"] == 2500
    assert result["trigger"] == "LA9 falling"


def test_capture_adc_triggers_through_the_streaming_path(connected):
    result = call("capture_adc", samples=200, sample_rate_hz=100_000, trigger_la=2,
                  trigger_edge="high", points=10)
    req = connected.requests[-1]
    assert req["cmd"] == "capture_dual" and req["la_samples"] == 0
    assert req["trigger"] == {"la": 2, "edge": "high"}
    assert result["trigger"] == "LA2 high" and result["samples"] == 200


def test_capture_correlated_triggers_both_streams(connected):
    result = call("capture_correlated", adc_samples=100, la_samples=100, trigger_la=4, points=10)
    assert connected.requests[-1]["trigger"] == {"la": 4, "edge": "rising"}
    assert result["adc"]["trigger"] == "LA4 rising" and result["la"]["trigger"] == "LA4 rising"


def test_captures_without_a_trigger_are_unchanged(connected):
    result = call("capture_la", samples=100, sample_rate_hz=1e6)
    assert result["trigger"] is None
    assert "trigger" not in [r for r in connected.requests if r["cmd"] == "la_capture"][-1]


def test_a_trigger_that_never_fires_is_a_tool_error(connected):
    connected.error = "trigger timeout: no rising edge on LA9 within 10000 ms"
    with pytest.raises(ToolError, match="TriggerTimeout: .*no rising edge on LA9"):
        call("capture_la", samples=100, sample_rate_hz=1e6, trigger_la=9)


def test_triggers_need_the_firmware_capability(connected):
    without_caps(connected, "capture_trigger")
    with pytest.raises(ToolError, match="capture_trigger"):
        call("capture_la", samples=100, trigger_la=9)


def test_the_schema_bounds_the_trigger_arguments(connected):
    with pytest.raises(ToolError):
        call("capture_la", samples=100, trigger_la=13)
    with pytest.raises(ToolError):
        call("capture_la", samples=100, trigger_la=9, trigger_edge="sideways")
    with pytest.raises(ToolError):
        call("capture_la", samples=100, trigger_la=9, trigger_timeout=601)


# -- power profiles -------------------------------------------------------------------------

def test_measure_power_reports_statistics_in_si_units(connected):
    result = call("measure_power", duration=1.0)
    assert connected.requests[-1] == {"cmd": "power_profile", "efuse": 1, "rate_hz": 500,
                                      "keep_samples": 0, "duration_ms": 1000}
    assert result["efuse"] == 1 and result["n"] == 950
    # the delivered rate, with the sensor's configured conversion rate beside it
    assert result["rate_hz"] == 364.0 and result["adc_rate_hz"] == 950.0
    assert result["duration"] == 1.0
    assert result["avg_current"] == pytest.approx(0.052)
    assert result["peak_current"] == pytest.approx(0.18)
    assert result["avg_voltage"] == pytest.approx(5.01)
    assert result["energy"] == pytest.approx(0.2605) and result["charge"] == pytest.approx(0.052)
    assert result["avg_power"] == pytest.approx(0.2605)
    assert result["fault"] is False and result["truncated"] is False
    assert result["trace_current"] == [] and result["trace_step"] == 0.0


def test_measure_power_returns_a_trace_when_points_are_asked_for(connected):
    result = call("measure_power", duration=1.0, points=4, efuse=2, rate_hz=500)
    assert connected.requests[-1]["keep_samples"] == 4 and connected.requests[-1]["efuse"] == 2
    assert connected.requests[-1]["rate_hz"] == 500.0
    assert result["trace_current"] == [0.04, 0.18, 0.05, 0.048]
    assert result["trace_voltage"] == [5.01, 4.99, 5.0, 5.005]
    assert result["trace_step"] == pytest.approx(0.25)
    assert result["efuse"] == 2


def test_measure_power_downsamples_a_long_trace(connected):
    result = call("measure_power", duration=1.0, points=2)
    assert result["trace_current"] == [0.11, 0.049]  # two bins, averaged
    assert result["trace_step"] == pytest.approx(0.5)


def test_power_profile_session_brackets_other_calls(connected):
    started = call("power_profile_start", rate_hz=500, max_duration=5)
    assert started == {"running": True, "efuse": 1, "rate_hz": 500.0, "max_duration": 5.0}
    assert connected.requests[-1] == {"cmd": "power_profile", "efuse": 1, "rate_hz": 500.0,
                                      "keep_samples": 4096, "max_duration_ms": 5000,
                                      "action": "start"}
    assert call("status")["session"]["power_profile_running"] is True
    call("power_on")
    result = call("power_profile_stop", points=4)
    assert connected.requests[-1] == {"cmd": "power_profile", "action": "stop"}
    assert result["peak_current"] == pytest.approx(0.18) and len(result["trace_current"]) == 4
    assert SESSION.power_profile is None
    assert call("status")["session"]["power_profile_running"] is False


def test_stopping_without_a_running_profile_says_so(connected):
    with pytest.raises(ToolError, match="power_profile_start"):
        call("power_profile_stop")


def test_disconnect_forgets_the_running_profile(connected):
    call("power_profile_start")
    call("disconnect")
    assert SESSION.power_profile is None


def test_power_profiles_need_the_firmware_capability(connected):
    without_caps(connected, "power_profile")
    with pytest.raises(ToolError, match="power_profile"):
        call("measure_power", duration=1.0)
    with pytest.raises(ToolError, match="power_profile"):
        call("power_profile_start")


def test_the_schema_bounds_the_power_arguments(connected):
    with pytest.raises(ToolError):
        call("measure_power", duration=1.0, rate_hz=5000)
    with pytest.raises(ToolError):
        call("measure_power", duration=0)
    with pytest.raises(ToolError):
        call("measure_power", duration=1.0, points=501)
    with pytest.raises(ToolError):
        call("power_profile_start", max_duration=601)
