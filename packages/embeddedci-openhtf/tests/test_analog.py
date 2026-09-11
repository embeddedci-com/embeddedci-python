"""Analog helpers and phase factories, run through the real OpenHTF executor against the fake
transport. Units are volts/seconds/hertz; the SDK converts to firmware codes."""

import json
import math

import openhtf as htf
import pytest
from openhtf.core import test_record as tr

from embeddedci.benchpod import AdcReading, DacHandle, DacOutput, FpgaImage, FpgaImageInfo

from embeddedci_openhtf import (
    adc_capture,
    adc_capture_phase,
    adc_read,
    adc_read_phase,
    benchpod_plug,
    control_loop,
    control_loop_phase,
    dac_output,
    dac_output_phase,
    fpga_image,
    loopback_measure_phase,
    signal_generate,
    signal_generate_phase,
    signal_stop,
)
from _fake import FakeTransport

TRIANGLE = [0, 64, 128, 192, 255, 192, 128, 64]
# The fake status has no ADC calibration, so the SDK scales linearly: volts = count * 3.3 / 255.
LSB = 3.3 / 255
TRI_V = [c * LSB for c in TRIANGLE]


def _run(*phases):
    records = []
    test = htf.Test(*phases)
    test.add_output_callbacks(records.append)
    test.execute(test_start=lambda: "SN-TEST")
    return records[0]


def _phase(record, name):
    return next(p for p in record.phases if p.name == name)


def _value(phase, name):
    return phase.measurements[name].measured_value.value


def _cmds(tx):
    return [c.get("cmd") for c in tx.commands]


# -- signal generation ---------------------------------------------------------

def test_signal_generate_phase_converts_volts_through_generate():
    tx = FakeTransport()
    rec = _run(signal_generate_phase(benchpod_plug(transport=tx), waveform="sine",
                                     freq_hz=1000, amplitude=1.0, duration=0.5))
    assert rec.outcome == tr.Outcome.PASS
    i = _cmds(tx).index("generate")
    # generate routes the DAC path first, then sends 8-bit generator codes:
    # 1.0 V / (5 V / 255) = 51, offset defaults to mid-range 2.5 V -> 128.
    assert tx.commands[i - 1] == {"cmd": "dac_out", "path": "5v"}
    assert tx.commands[i] == {"cmd": "generate", "waveform": "sine", "freq": 1000.0,
                              "amplitude": 51, "offset": 128, "duration_ms": 500}


def test_signal_generate_helper_path_offset_and_rate():
    tx = FakeTransport()
    plug = benchpod_plug(transport=tx)()
    handle = signal_generate(plug, waveform="square", freq_hz=50, amplitude=1.1, offset=0.66,
                             dac_path="3v3", sample_rate_hz=250_000)
    assert isinstance(handle, DacHandle) and handle.dac_path == "3v3"
    assert tx.commands[-2:] == [
        {"cmd": "dac_out", "path": "3v3"},
        {"cmd": "generate", "waveform": "square", "freq": 50.0, "amplitude": 85, "offset": 51,
         "sample_rate_mhz": 0.25},
    ]
    handle.stop()
    assert tx.commands[-1] == {"cmd": "dac_stop"}


def test_signal_generate_rejects_out_of_range_volts_before_sending():
    tx = FakeTransport()
    plug = benchpod_plug(transport=tx)()
    with pytest.raises(ValueError):
        signal_generate(plug, waveform="sine", freq_hz=100, amplitude=6.0)  # > 5 V path
    assert "generate" not in _cmds(tx) and "dac_out" not in _cmds(tx)


def test_signal_stop_helper():
    tx = FakeTransport()
    assert signal_stop(benchpod_plug(transport=tx)()) is None
    assert tx.commands[-1] == {"cmd": "dac_stop"}


# -- ADC capture -----------------------------------------------------------------

def test_adc_capture_phase_records_volts():
    tx = FakeTransport(adc_samples=TRIANGLE)
    rec = _run(adc_capture_phase(benchpod_plug(transport=tx), samples=8,
                                 mean_range=(1.5, 1.8), pp_range=(3.2, 3.4)))
    assert rec.outcome == tr.Outcome.PASS
    assert tx.commands[-2:] == [{"cmd": "analog_path", "path": "adc_ext"},
                                {"cmd": "capture", "samples": 8}]
    p = _phase(rec, "adc_capture")
    assert _value(p, "adc_mean_v") == pytest.approx(sum(TRI_V) / 8)
    assert _value(p, "adc_pp_v") == pytest.approx(3.3)
    assert _value(p, "adc_rms_v") == pytest.approx(math.sqrt(sum(v * v for v in TRI_V) / 8))
    assert _value(p, "adc_min_v") == pytest.approx(0.0)
    assert _value(p, "adc_max_v") == pytest.approx(3.3)
    att = json.loads(p.attachments["adc.json"].data)
    assert att["counts"] == TRIANGLE and att["source"] == "ext"
    assert att["volts"] == pytest.approx(TRI_V)


def test_adc_capture_phase_source_none_leaves_routing_and_passes_rate():
    tx = FakeTransport(adc_samples=TRIANGLE)
    rec = _run(adc_capture_phase(benchpod_plug(transport=tx), samples=8, source=None,
                                 sample_rate_hz=100_000, prefix="rail", attachment=None))
    assert rec.outcome == tr.Outcome.PASS
    assert "analog_path" not in _cmds(tx)
    assert tx.commands[-1] == {"cmd": "capture", "samples": 8, "sample_rate_mhz": 0.1}
    p = _phase(rec, "adc_capture")
    assert _value(p, "rail_pp_v") == pytest.approx(3.3)
    assert "adc.json" not in p.attachments


def test_adc_capture_phase_fails_out_of_range():
    tx = FakeTransport(adc_samples=TRIANGLE)
    rec = _run(adc_capture_phase(benchpod_plug(transport=tx), samples=8, pp_range=(0, 1.0)))
    assert rec.outcome == tr.Outcome.FAIL
    assert _value(_phase(rec, "adc_capture"), "adc_pp_v") == pytest.approx(3.3)


def test_adc_capture_helper_returns_capture():
    tx = FakeTransport(adc_samples=TRIANGLE)
    cap = adc_capture(benchpod_plug(transport=tx)(), 8, source="cal1")
    assert cap.counts == TRIANGLE and cap.source == "cal1"
    assert tx.commands[-2] == {"cmd": "analog_path", "path": "cal1"}


def test_phase_factories_validate_arguments():
    bench = benchpod_plug(transport=FakeTransport())
    with pytest.raises(ValueError):
        adc_capture_phase(bench, source="sma")
    with pytest.raises(ValueError):
        adc_capture_phase(bench, mean_range=(2.0, 1.0))
    with pytest.raises(ValueError):
        loopback_measure_phase(bench, freq_hz=100, amplitude=1.0, settle=-1)
    with pytest.raises(ValueError):
        adc_read_phase(bench, source="5v")


# -- loopback ----------------------------------------------------------------------

def test_loopback_measure_phase_generates_captures_then_stops():
    tx = FakeTransport(adc_samples=TRIANGLE)
    rec = _run(loopback_measure_phase(benchpod_plug(transport=tx), waveform="sine", freq_hz=100,
                                      amplitude=1.0, samples=8, sample_rate_hz=50_000, settle=0,
                                      pp_range=(3.0, 3.5), mean_range=(1.5, 1.8)))
    assert rec.outcome == tr.Outcome.PASS
    # DAC first (its path re-applies and would open a loopback relay), then route the ADC.
    assert _cmds(tx) == ["dac_out", "generate", "analog_path", "capture", "dac_stop"]
    assert tx.commands[1] == {"cmd": "generate", "waveform": "sine", "freq": 100.0,
                              "amplitude": 51, "offset": 128}
    assert tx.commands[2] == {"cmd": "analog_path", "path": "adc_ext"}
    assert tx.commands[3] == {"cmd": "capture", "samples": 8, "sample_rate_mhz": 0.05}
    p = _phase(rec, "loopback_measure")
    assert _value(p, "adc_pp_v") == pytest.approx(3.3)
    assert _value(p, "adc_mean_v") == pytest.approx(sum(TRI_V) / 8)


def test_loopback_measure_phase_internal_loopback_source():
    tx = FakeTransport(adc_samples=TRIANGLE)
    rec = _run(loopback_measure_phase(benchpod_plug(transport=tx), freq_hz=100, amplitude=1.0,
                                      dac_path="12v", samples=8, source="cal2", settle=0))
    assert rec.outcome == tr.Outcome.PASS
    assert tx.commands[0] == {"cmd": "dac_out", "path": "12v"}
    assert {"cmd": "analog_path", "path": "cal2"} in tx.commands


def test_loopback_measure_phase_stops_dac_when_capture_fails():
    tx = FakeTransport(fail_capture=True)
    rec = _run(loopback_measure_phase(benchpod_plug(transport=tx), freq_hz=100, amplitude=1.0,
                                      samples=8, settle=0))
    assert rec.outcome == tr.Outcome.ERROR
    assert _cmds(tx)[-1] == "dac_stop"


# -- control loop / gateware -----------------------------------------------------------

def test_control_loop_phase_arms_and_records_operating_point():
    tx = FakeTransport()
    rec = _run(control_loop_phase(benchpod_plug(transport=tx), voc_code=52000, sharpness=6,
                                  vmax=52000, v_range=(0, 52000)))
    assert rec.outcome == tr.Outcome.PASS
    arm = next(c for c in tx.commands if c.get("cmd") == "dac_control_loop")
    assert arm["vmax"] == 52000 and "curve" in arm
    assert {"cmd": "dac_stop"} in tx.commands  # loop stopped on context exit
    p = _phase(rec, "control_loop")
    assert _value(p, "control_loop_v") == 51000
    assert _value(p, "control_loop_i") == 4096


def test_control_loop_and_fpga_image_helpers_through_plug_proxy():
    tx = FakeTransport()
    plug = benchpod_plug(transport=tx)()
    info = fpga_image(plug, FpgaImage.LOOP)
    assert isinstance(info, FpgaImageInfo)
    assert (info.image, info.version, info.features) == (0, 27, 1)
    assert tx.commands[-1] == {"cmd": "fpga_image", "image": 0}
    loop = control_loop(plug, voc_code=1000)
    assert loop.armed
    assert tx.commands[-1]["cmd"] == "dac_control_loop"
    pt = loop.probe()
    assert (pt.i, pt.v) == (4096, 51000)


# -- DC output / single reading ----------------------------------------------------------

def test_dac_output_phase_routes_and_sets_volts():
    tx = FakeTransport()
    rec = _run(dac_output_phase(benchpod_plug(transport=tx), path="5v", volts=2.5))
    assert rec.outcome == tr.Outcome.PASS
    assert tx.commands[-1] == {"cmd": "dac_out", "path": "5v", "volts": 2.5}


def test_dac_output_helper_returns_typed_result():
    tx = FakeTransport()
    plug = benchpod_plug(transport=tx)()
    out = dac_output(plug, "5v", volts=2.5)
    assert out == DacOutput(path="5v", voltage=2.5, code=128)
    routed = dac_output(plug, "off")
    assert routed.voltage is None and routed.code is None
    assert tx.commands[-1] == {"cmd": "dac_out", "path": "off"}


def test_adc_read_phase_records_volts():
    tx = FakeTransport()
    rec = _run(adc_read_phase(benchpod_plug(transport=tx), source="ext", v_range=(11.0, 13.0)))
    assert rec.outcome == tr.Outcome.PASS
    assert tx.commands[-1] == {"cmd": "adc_read", "source": "ext"}
    p = _phase(rec, "adc_read")
    assert _value(p, "ext_v") == pytest.approx(12.034)
    assert "ext_mv" not in p.measurements


def test_adc_read_phase_fails_out_of_range():
    tx = FakeTransport()
    rec = _run(adc_read_phase(benchpod_plug(transport=tx), source="ext", v_range=(0, 0.1)))
    assert rec.outcome == tr.Outcome.FAIL


def test_adc_read_helper_returns_typed_result():
    tx = FakeTransport()
    r = adc_read(benchpod_plug(transport=tx)(), "cal1")
    assert isinstance(r, AdcReading)
    assert r.source == "cal1" and r.voltage == pytest.approx(2.502) and r.count == 63049
