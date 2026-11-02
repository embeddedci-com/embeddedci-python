"""capture_adc(oversample=True): conversions at 400 kS/s averaged down to the requested rate."""

from __future__ import annotations

import pytest

from embeddedci.benchpod.capabilities import AffineCal, Capabilities
from embeddedci.benchpod.capture import (ADC_OVERSAMPLE_MAX_CONVERSIONS, average_conversions,
                                         capture_adc_oversampled, oversample_factor)
from embeddedci.benchpod.errors import BenchPodError
from embeddedci.benchpod.results import Capture


def caps() -> Capabilities:
    c = Capabilities()
    c.adc_affine = AffineCal(a=65.708158, b=-0.001002644, unwrap=True)
    return c


def raw(counts) -> Capture:
    return Capture(counts=[c & 0xFFFF for c in counts], volts=[0.0] * len(counts),
                   sample_rate_hz=400_000.0, source="cal2", range="normal")


def test_factor_is_the_nearest_ratio_and_1_when_nothing_to_average():
    assert oversample_factor(10_000) == 40
    assert oversample_factor(30_000) == 13
    assert oversample_factor(400_000) == 1
    assert oversample_factor(None) == 1
    with pytest.raises(ValueError):
        oversample_factor(0)


def test_a_block_straddling_zero_averages_in_the_signed_domain():
    cap = average_conversions(raw([-1, -1, 1, 3]), caps(), 4)
    assert cap.mean_counts == [0.5]  # the raw mean would be about 32768
    assert cap.counts == [1]         # 0.5 rounds up
    assert cap.oversample == 4
    assert cap.sample_rate_hz == 100_000.0
    assert cap.source == "cal2" and cap.range == "normal"


def test_negative_means_round_to_the_raw_two_s_complement_code():
    assert average_conversions(raw([-3, -2]), caps(), 2).counts == [0xFFFE]  # -2.5 -> -2


def test_volts_keep_the_fraction_and_match_the_plain_scaling_on_whole_counts():
    c = caps()
    whole = average_conversions(raw([-5, -5]), c, 2).volts[0]
    assert whole == pytest.approx(c.counts_to_volts(-5 & 0xFFFF))
    pos = average_conversions(raw([5, 5]), c, 2).volts[0]
    assert pos == pytest.approx(c.counts_to_volts(5))
    half = average_conversions(raw([5, 6]), c, 2).volts[0]
    assert half == pytest.approx((c.counts_to_volts(5) + c.counts_to_volts(6)) / 2)


def test_a_clipped_conversion_is_reported_though_the_average_hides_it():
    cap = average_conversions(raw([32767, 0, 0, 0, 1, 1, 1, 1]), caps(), 4)
    assert 0x7FFF not in cap.counts
    assert cap.clipped_samples == 1 and cap.overrange


def test_a_partial_last_block_is_dropped():
    assert len(average_conversions(raw([1, 1, 2, 2, 3]), caps(), 2)) == 2


def test_too_many_conversions_is_a_clear_error_before_touching_the_pod():
    with pytest.raises(BenchPodError, match="at most"):
        capture_adc_oversampled(None, caps(), samples=ADC_OVERSAMPLE_MAX_CONVERSIONS // 40 + 1,
                                sample_rate_hz=10_000)
