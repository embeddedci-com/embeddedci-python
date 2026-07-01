"""Analog phase factories (signal generate / ADC capture / loopback measure),
run through the real OpenHTF executor against the fake transport."""

import json

import openhtf as htf
from openhtf.core import test_record as tr

from embeddedci_openhtf import (
    adc_capture_phase,
    benchpod_plug,
    loopback_measure_phase,
    signal_generate,
    signal_generate_phase,
)
from _fake import FakeTransport

# default fake waveform: triangle 0..255 -> min 0, max 255, mean 128, pp 255
TRIANGLE = [0, 64, 128, 192, 255, 192, 128, 64]


def _run(*phases):
    records = []
    test = htf.Test(*phases)
    test.add_output_callbacks(records.append)
    test.execute(test_start=lambda: "SN-TEST")
    return records[0]


def _phase(record, name):
    return next(p for p in record.phases if p.name == name)


def test_signal_generate_phase_issues_command():
    tx = FakeTransport()
    bench = benchpod_plug(transport=tx)
    rec = _run(signal_generate_phase(bench, waveform="sine", freq=1000,
                                     amplitude=100, offset=128, duration_ms=500))
    assert rec.outcome == tr.Outcome.PASS
    gen = next(c for c in tx.commands if c.get("cmd") == "generate")
    assert gen == {"cmd": "generate", "waveform": "sine", "freq": 1000,
                   "amplitude": 100, "offset": 128, "duration_ms": 500}


def test_adc_capture_phase_records_stats():
    tx = FakeTransport(adc_samples=TRIANGLE)
    bench = benchpod_plug(transport=tx)
    rec = _run(adc_capture_phase(bench, samples=8, mean_range=(100, 160),
                                 pp_range=(200, 260)))
    assert rec.outcome == tr.Outcome.PASS
    cap = tx.commands[-1]
    assert cap["cmd"] == "capture" and cap["samples"] == 8
    p = _phase(rec, "adc_capture")
    assert p.measurements["adc_mean"].measured_value.value == 127.875  # 1023/8
    assert p.measurements["adc_pp"].measured_value.value == 255
    assert json.loads(p.attachments["adc.json"].data) == TRIANGLE


def test_adc_capture_phase_fails_out_of_range():
    tx = FakeTransport(adc_samples=TRIANGLE)
    bench = benchpod_plug(transport=tx)
    rec = _run(adc_capture_phase(bench, samples=8, pp_range=(0, 50)))  # 255 > 50
    assert rec.outcome == tr.Outcome.FAIL
    p = _phase(rec, "adc_capture")
    assert p.measurements["adc_pp"].measured_value.value == 255


def test_loopback_measure_phase():
    tx = FakeTransport(adc_samples=TRIANGLE)
    bench = benchpod_plug(transport=tx)
    rec = _run(loopback_measure_phase(bench, waveform="sine", freq=2000,
                                      amplitude=120, samples=8,
                                      pp_range=(180, 255)))
    assert rec.outcome == tr.Outcome.PASS
    meas = tx.commands[-1]
    assert meas["cmd"] == "measure" and meas["waveform"] == "sine"
    assert meas["samples"] == 8
    p = _phase(rec, "loopback_measure")
    assert p.measurements["adc_pp"].measured_value.value == 255


def test_signal_generate_helper_through_plug_proxy():
    # the low-level helper accepts the plug directly (attrs proxy to the SDK)
    tx = FakeTransport()
    plug = benchpod_plug(transport=tx)()
    signal_generate(plug, waveform="square", freq=5000, amplitude=64)
    assert tx.commands[-1]["cmd"] == "generate"
    assert tx.commands[-1]["waveform"] == "square"
