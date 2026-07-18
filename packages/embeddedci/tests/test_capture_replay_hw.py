"""Hardware/cloud integration for the capture + DAC-replay + waveform-library features.

Mirrors the Go ``hwe2e`` cloud suite (TestCloud_SaveRecordingAndReplay, TestCloud_UnifiedCapture,
concurrent replay+capture) from the Python side. Every test skips cleanly without a configured
device (``--benchpod-connection`` / ``BENCHPOD_CONNECTION``); the library tests additionally need
an API key (``--benchpod-api-key`` / ``BENCHPOD_API_KEY``).

    BENCHPOD_CONNECTION=embeddedci:benchpod-v2.0.0 \
    BENCHPOD_API_KEY=eci_… BENCHPOD_LA_VOLTAGE=3.3 \
    pytest packages/embeddedci/tests/test_capture_replay_hw.py -v
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.hardware


def test_scope_capture_returns_volts(benchpod):
    cap = benchpod.scope_capture(256, sample_rate_mhz=1)
    assert len(cap) > 0
    assert len(cap.volts) == len(cap.counts)
    # calibrated volts should be a sane, finite number
    assert -50.0 < cap.mean() < 50.0


@pytest.mark.benchpod_capability("analyzer")
def test_raw_la_capture(benchpod):
    la = benchpod.capture_la(1024, sample_rate_mhz=1)
    assert len(la) > 0
    assert all(0 <= w < (1 << 12) for w in la.words[:32])


def test_correlated_adc_la_capture(benchpod):
    ac = benchpod.capture_analog(adc_samples=256, adc_rate_mhz=0.4, la_samples=256, la_rate_mhz=1)
    assert len(ac.adc) > 0 and len(ac.la) > 0


@pytest.mark.benchpod_capability("dac_replay")
def test_save_recording_and_replay(benchpod, benchpod_waveforms):
    """Capture → save to library → replay from the library → stop (the headline flow)."""
    cap = benchpod.scope_capture(2048, sample_rate_mhz=0.4)
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
    with bp.replay(codes, dac_path="5v", are_codes=True, sample_rate_mhz=0.4):
        la = bp.capture_la(8192, sample_rate_mhz=1)   # must complete alongside the replay
        assert len(la) == 8192
