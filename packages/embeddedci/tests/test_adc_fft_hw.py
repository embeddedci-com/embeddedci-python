"""Ad-hoc hardware test for the ADC FFT capture tool (scripts/adc_fft_capture.py).

Unlike the rest of this suite (which runs against fakes), this drives a REAL pod
and is skipped unless BENCHPOD_HW_ADDR points at one, e.g.:

    BENCHPOD_HW_ADDR=192.168.1.215 pytest tests/test_adc_fft_hw.py -s

It generates a DAC sine, loops it back through the ADC (analog path cal1), and
checks that the FFT recovers a single dominant tone whose frequency AGREES across
two different ADC sample rates. That agreement validates the capture + FFT chain
independently of the DAC's own frequency accuracy (the DAC8551 sequencer is only
nominally calibrated, so we assert consistency, not the exact DAC frequency).
"""
from __future__ import annotations

import importlib.util
import os
import pathlib

import pytest

HW = os.environ.get("BENCHPOD_HW_ADDR")
pytestmark = pytest.mark.skipif(not HW, reason="set BENCHPOD_HW_ADDR to run against a live pod")

np = pytest.importorskip("numpy")  # the FFT path needs numpy


def _load_tool():
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "adc_fft_capture.py"
    spec = importlib.util.spec_from_file_location("adc_fft_capture", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _capture_peak(tool, rate_mhz):
    args = tool.parse_args(
        ["--addr", HW, "--freq", "1000", "--samples", "2048",
         "--rate-mhz", str(rate_mhz), "--source", "cal1"]
    )
    res = tool.capture(args)
    assert res["counts"], "capture returned no samples"
    volts = tool.counts_to_volts(res["counts"], res["bits"], res["fullscale_mv"])
    # A real looped-back tone must swing well above ADC noise.
    assert np.std(np.asarray(volts) - np.mean(volts)) > 0.005, "no signal on the ADC (loopback missing?)"
    peak = tool.run_fft(volts, res["fs_hz"], res["gen"])
    assert peak is not None and peak > 0
    return peak


def test_adc_fft_loopback_tone_consistent_across_rates():
    tool = _load_tool()
    peaks = {r: _capture_peak(tool, r) for r in (0.1, 0.2)}
    lo, hi = min(peaks.values()), max(peaks.values())
    assert (hi - lo) / hi < 0.10, f"recovered tone differs across ADC rates: {peaks}"


def test_generate_frequency_is_accurate():
    """generate(freq) must play (and the ADC/FFT recover) the requested frequency
    within a few % -- guards the DAC8551 sequencer-overhead correction in the
    firmware (before it, the output frequency saturated far below the request)."""
    tool = _load_tool()
    for freq in (500.0, 1000.0, 2000.0):
        args = tool.parse_args(["--addr", HW, "--freq", str(freq), "--samples", "4096",
                                "--source", "cal1"])
        res = tool.capture(args)
        assert res["counts"], "capture returned no samples"
        volts = tool.counts_to_volts(res["counts"], res["bits"], res["fullscale_mv"])
        peak = tool.fft_peak(volts, res["fs_hz"])
        assert peak is not None
        err = abs(peak - freq) / freq
        assert err < 0.05, f"generate({freq}) recovered {peak:.1f} Hz ({err*100:.1f}% off)"
