"""Hardware/cloud integration for the capture + DAC-replay + waveform-library features.

Mirrors the Go ``hwe2e`` cloud suite (TestCloud_SaveRecordingAndReplay, TestCloud_UnifiedCapture,
concurrent replay+capture) from the Python side. Every test skips cleanly without a configured
device (``--benchpod-connection`` / ``BENCHPOD_CONNECTION``); the library tests additionally need
an API key (``--benchpod-api-key`` / ``BENCHPOD_API_KEY``). The board's LA voltage comes from
``tests/conftest.py``.

    BENCHPOD_CONNECTION=embeddedci:benchpod-v2.0.0 BENCHPOD_API_KEY=eci_… \
    pytest packages/embeddedci/tests/test_capture_replay_hw.py -v
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.hardware


def test_capture_adc_returns_volts(benchpod):
    cap = benchpod.capture_adc(256, sample_rate_hz=1e6)
    assert len(cap) == 256, f"asked for 256 samples, got {len(cap)}"
    assert len(cap.volts) == len(cap.counts)
    assert cap.sample_rate_hz == pytest.approx(1e6, rel=0.05), cap.sample_rate_hz
    # The front end spans roughly +/-12 V after the divider; +/-50 V was ~4x wider than the
    # hardware can produce, so a broken calibration scale could not fail it.
    assert -13.0 < cap.mean() < 13.0, f"calibrated mean {cap.mean():.3f} V is off-scale"
    assert all(0 <= c <= 65535 for c in cap.counts), "raw counts outside the 16-bit ADC range"


@pytest.mark.benchpod_capability("analyzer")
def test_raw_la_capture(benchpod):
    la = benchpod.capture_la(1024, sample_rate_hz=1e6)
    assert len(la) == 1024, f"asked for 1024 samples, got {len(la)}"
    assert la.sample_rate_hz == pytest.approx(1e6, rel=0.05), la.sample_rate_hz
    assert all(0 <= w < (1 << 12) for w in la.words), "LA words must be 12-bit"


def test_correlated_adc_la_capture(benchpod):
    ac = benchpod.capture_correlated(adc_samples=256, adc_sample_rate_hz=400_000,
                                     la_samples=256, la_sample_rate_hz=1e6)
    assert len(ac.adc) == 256 and len(ac.la) == 256
    assert ac.adc.sample_rate_hz == pytest.approx(400_000, rel=0.05), ac.adc.sample_rate_hz
    assert ac.la.sample_rate_hz == pytest.approx(1e6, rel=0.05), ac.la.sample_rate_hz


def test_deep_adc_capture_streams_from_psram(benchpod):
    """Above the 32768-sample single-shot buffer, capture_adc switches to the PSRAM stream."""
    cap = benchpod.capture_adc(65536, sample_rate_hz=400_000)
    assert len(cap) == 65536


@pytest.mark.benchpod_capability("dac_replay")
def test_save_recording_and_replay(benchpod, benchpod_waveforms):
    """Capture → save to library → replay from the library → stop (the headline flow)."""
    cap = benchpod.capture_adc(2048, sample_rate_hz=400_000)
    wf = benchpod.save_capture_as_recording(cap, f"pytest-{int(time.time())}")
    assert wf.is_recording and wf.id

    # it appears in the library
    assert any(w.id == wf.id for w in benchpod_waveforms.list())

    # replay it on the 5V path, then stop the looping replay
    handle = benchpod.replay_waveform(wf.id, dac_path="5v", mapping="faithful", target_samples=4096)
    try:
        assert handle.samples >= 1
    finally:
        handle.stop()


@pytest.mark.benchpod_capability("dac_deep_replay")
def test_replay_while_capturing_concurrently(benchpod_dac):
    """gateware v18: a looping deep replay runs WHILE an LA capture streams (shared PSRAM bus)."""
    bp = benchpod_dac
    codes = [0xC000] * 65536  # deep constant waveform (streams from PSRAM, loops)
    with bp.replay(codes, dac_path="5v", are_codes=True, sample_rate_hz=400_000):
        la = bp.capture_la(8192, sample_rate_hz=1e6)   # must complete alongside the replay
        assert len(la) == 8192
