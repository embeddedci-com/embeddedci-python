"""Hardware/cloud integration for the closed-loop DAC control + DAC↔capture co-trigger.

Mirrors the Go ``hwe2e`` cloud suite (TestCloud_V2ControlLoop, the co-trigger / auto-stop DAC
tests) from the Python side. Skips cleanly without a configured device; the control-loop tests
need the loop gateware image (``cap.dac_control_loop``) and the co-trigger test needs
``cap.dac_cotrig``.

    BENCHPOD_CONNECTION=embeddedci:benchpod-v2.0.0 \
    BENCHPOD_API_KEY=eci_… BENCHPOD_LA_VOLTAGE=3.3 \
    pytest packages/embeddedci/tests/test_control_loop_hw.py -v
"""

from __future__ import annotations

import time

import pytest

from embeddedci import benchpod as bp

pytestmark = pytest.mark.hardware


def _ensure_loop_image(device) -> bool:
    """Best-effort: switch to the closed-loop gateware image if the device isn't already on it.
    Returns True when the loop capability is available afterwards."""
    if device.capabilities.dac_control_loop:
        return True
    try:
        out = device.fpga_image(0)  # 0 = closed-loop demo image
    except Exception:
        return False
    time.sleep(1.0)  # let the reconfigured gateware + front-end settle
    return int(out.get("features", 0)) == 1 or device.refresh_capabilities().dac_control_loop


def test_control_loop_settles_on_panel_curve(benchpod):
    if not _ensure_loop_image(benchpod):
        pytest.skip("device does not provide the closed-loop gateware image (cap.dac_control_loop)")
    try:
        voc = 52000
        curve = bp.build_panel_curve(voc_code=voc, sharpness=6)
        with benchpod.control_loop(curve=curve, vmax=voc, tick_div=64) as loop:
            assert loop.armed
            last = None
            for _ in range(6):
                time.sleep(0.1)
                last = loop.probe()
            assert last is not None
            # the loop drove the DAC somewhere inside the clamp window
            assert 0 <= last.v <= voc
            # for a measured current i, the DAC target is curve[i >> 5]; the loop should be
            # heading toward it (allow a wide band for damping + analog settle)
            idx = min(len(curve) - 1, (last.i >> 5) * len(curve) // 2048)
            assert abs(last.v - curve[idx]) < voc  # sane, not railed at the wrong end
    finally:
        benchpod.dac_stop()


@pytest.mark.benchpod_capability("dac_cotrig")
def test_replay_co_triggers_with_capture(benchpod):
    """Arm a replay to fire on the next capture's t0 (phase-locked), then run the capture."""
    cap = benchpod.scope_capture(64, sample_rate_mhz=0.4)
    try:
        handle = benchpod.replay(cap, dac_path="5v", on_capture=True)
        assert handle.cotrig  # device armed the DAC instead of starting it immediately
        # the following capture fires the DAC at t0
        ac = benchpod.capture_analog(adc_samples=512, adc_rate_mhz=0.4, la_samples=0)
        assert len(ac.adc) > 0
    finally:
        benchpod.dac_stop()


def test_dac_auto_stop_during_capture(benchpod):
    """A DAC output cut mid-capture: the captured window brackets the cutoff (no crash / clean run)."""
    benchpod.generate("square", freq=2000, amplitude=1.0)
    try:
        ac = benchpod.capture_analog(adc_samples=2048, adc_rate_mhz=0.4, la_samples=0,
                                     stop_dac_after_us=2000)
        assert len(ac.adc) > 0
    finally:
        benchpod.dac_stop()
