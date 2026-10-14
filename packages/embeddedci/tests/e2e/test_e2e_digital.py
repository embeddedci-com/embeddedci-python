"""Tier "digital": the digital-only board (digital-v3, digital-v3r2), the pod without the analog
front end.

    BENCHPOD_EXPECT_BOARD=digital pytest packages/embeddedci/tests/e2e --benchpod-connection=<pod host>

These tests run only on a pod that reports ``analog: false`` and skip on the analog board; the
analog tests elsewhere skip here. The firmware finds the analog section at boot by its two I2C
expanders and refuses every analog command on a board without them (cmd_gate.c): the SDK raises
:class:`UnsupportedFeatureError` for those, and everything digital keeps working.
"""

from __future__ import annotations

import time

import pytest

from embeddedci.benchpod import UnsupportedFeatureError

pytestmark = pytest.mark.hardware

#: The pod's refusal on the digital board (cmd_gate.c).
NO_ANALOG = "no analog front end"

#: Capabilities only the analog board announces (cloud_caps_hw.c, cloud_caps.c).
ANALOG_ONLY_CAPS = ("scope", "dac_replay", "dac_deep_replay", "dac_control_loop", "dac_cotrig",
                    "dac_loop_sources", "dac_loop_input_map", "calibrate", "current_out")


@pytest.fixture
def digital(pod):
    if pod.can_analog:
        pytest.skip("analog board: the digital-only checks do not apply")
    return pod


def test_status_says_digital_and_drops_the_analog_capabilities(digital):
    assert digital.status().get("analog") is False
    caps = digital.refresh_capabilities()
    assert caps.analog is False and not digital.can_analog
    assert [c for c in ANALOG_ONLY_CAPS if getattr(caps, c, False)] == []
    assert caps.analyzer  # the logic analyzer is on the digital board


@pytest.mark.parametrize("call", [
    lambda p: p.dac_output("5v", volts=1.0),
    lambda p: p.adc_read("cal1"),
    lambda p: p.analog_path("cal1"),
    lambda p: p.current_out(0.012),
    lambda p: p.capture_adc(1024, sample_rate_hz=100_000),
    lambda p: p.capture_correlated(adc_samples=1024, adc_sample_rate_hz=100_000,
                                   la_samples=1024, la_sample_rate_hz=1_000_000),
    lambda p: p.generate("sine", freq_hz=1000, amplitude=0.5, dac_path="5v"),
    lambda p: p.replay([0.5, 1.0, 1.5], dac_path="5v", sample_rate_hz=1000),
], ids=["dac_output", "adc_read", "analog_path", "current_out", "capture_adc",
        "capture_correlated", "generate", "replay"])
def test_analog_calls_are_refused_fast(digital, call):
    t0 = time.monotonic()
    with pytest.raises(UnsupportedFeatureError) as err:
        call(digital)
    assert time.monotonic() - t0 < 5.0, "the refusal must not wait for a timeout"
    # From the pod (cmd_gate.c) or, for a capability-gated call, from the SDK before sending:
    # either way it names the missing front end, never a firmware update.
    assert err.value.feature == "analog", err.value
    assert "newer" not in str(err.value)


def test_refused_raw_commands_name_the_missing_front_end(digital):
    for cmd in ({"cmd": "dac_out", "path": "5v", "mv": 1000}, {"cmd": "adc_read", "source": "cal1"}):
        with pytest.raises(UnsupportedFeatureError, match=NO_ANALOG):
            digital.command(cmd)


def test_dac_stop_still_works(digital):
    """dac_stop is the cleanup every test and tool runs: it must not fail on the digital board."""
    digital.dac_stop()


def test_pod_answers_after_the_refusals(digital):
    for call in (lambda: digital.adc_read("cal1"), lambda: digital.capture_adc(1024)):
        with pytest.raises(UnsupportedFeatureError):
            call()
    assert digital.status().get("board")
    assert digital.power_status().internal.ok
    la = digital.capture_la(8192, sample_rate_hz=1_000_000)
    assert len(la) == 8192 and la.sample_rate_hz == pytest.approx(1e6, rel=0.05)
