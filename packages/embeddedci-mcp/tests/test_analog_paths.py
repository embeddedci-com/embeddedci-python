"""Tests for the intuitive analog-path MCP tools + SDK methods.

These exercise the whole stack down to the JSON command the pod would receive:
MCP tool -> BenchPod SDK method -> FakeTransport.command(req). We assert both the
returned value and the exact request emitted, so a wiring/naming drift between
the layers is caught here rather than on hardware.
"""

from __future__ import annotations

import embeddedci_mcp.server as server


def _last_analog(tx) -> dict:
    assert tx.analog_reqs, "no analog command was sent"
    return tx.analog_reqs[-1]


# -- analog_path ------------------------------------------------------------

def test_analog_path_routes_named_path(connected):
    out = server.analog_path("cal1")
    assert _last_analog(connected) == {"cmd": "analog_path", "path": "cal1"}
    assert out["path"] == "cal1"
    assert "u55" in out and "u58" in out


def test_analog_path_accepts_alias(connected):
    server.analog_path("12v")
    assert _last_analog(connected)["path"] == "12v"


# -- dac_output -------------------------------------------------------------

def test_dac_output_with_volts_is_calibrated(connected):
    out = server.dac_output("5v", 2.5)
    assert _last_analog(connected) == {"cmd": "dac_out", "path": "5v", "volts": 2.5}
    assert out["mv"] == 2500
    assert out["code"] == 128


def test_dac_output_route_only_omits_volts(connected):
    out = server.dac_output("3v3")
    req = _last_analog(connected)
    assert req == {"cmd": "dac_out", "path": "3v3"}   # no volts key
    assert out["code"] == -1


# -- adc_read ---------------------------------------------------------------

def test_adc_read_default_source_is_ext(connected):
    out = server.adc_read()
    assert _last_analog(connected) == {"cmd": "adc_read", "source": "ext"}
    assert out["source"] == "ext"
    assert out["mv"] == 12034          # ÷12 divider applied on the pod
    assert "count" in out


def test_adc_read_named_source(connected):
    out = server.adc_read("cal1")
    assert _last_analog(connected)["source"] == "cal1"
    assert out["mv"] == 2502           # cal node volts, no divider


# -- SDK convenience (measure_volts) ----------------------------------------

def test_measure_volts_returns_float(connected):
    from embeddedci.benchpod import BenchPod
    pod: BenchPod = server.SESSION.require()
    assert pod.measure_volts("ext") == 12.034


def test_route_dac_to_adc_uses_single_source_path(connected):
    # The old convenience must now route via analog_path (no hand-flipped mux),
    # so cal1 can never disagree with the firmware's routing.
    server.route_dac_to_adc("5v")
    assert _last_analog(connected) == {"cmd": "analog_path", "path": "cal1"}
    server.route_dac_to_adc("12v")
    assert _last_analog(connected) == {"cmd": "analog_path", "path": "cal2"}
