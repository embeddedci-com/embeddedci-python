"""Timing helpers on captures: edges, crossings, pulses, frequency and channel-to-channel delay."""

from __future__ import annotations

import pytest

from embeddedci.benchpod.results import Capture, LaCapture


def _la(rate: float = 1000.0, **channels):
    """An LaCapture from per-channel bit lists: ``_la(la1=[...], la2=[...])``."""
    n = max(len(bits) for bits in channels.values())
    words = [0] * n
    for name, bits in channels.items():
        for i, b in enumerate(bits):
            words[i] |= b << (int(name[2:]) - 1)
    return LaCapture(words=words, sample_rate_hz=rate)


LA1 = [0, 0, 1, 1, 1, 0, 0, 1, 1, 0]
LA2 = [0, 0, 0, 0, 1, 1, 0, 0, 0, 1]


def test_edge_times_by_direction():
    la = _la(la1=LA1)
    assert la.edge_times(1, "rising") == pytest.approx([0.002, 0.007])
    assert la.edge_times(1, "falling") == pytest.approx([0.005, 0.009])
    assert la.edge_times(1) == pytest.approx([0.002, 0.005, 0.007, 0.009])


def test_first_edge_after():
    la = _la(la1=LA1)
    assert la.first_edge(1, "rising") == pytest.approx(0.002)
    assert la.first_edge(1, "rising", after=0.003) == pytest.approx(0.007)
    assert la.first_edge(1, "rising", after=0.008) is None


def test_level_at():
    la = _la(la1=LA1)
    assert la.level_at(1, 0.0) == 0
    assert la.level_at(1, 0.002) == 1
    assert la.level_at(1, 0.0049) == 1
    assert la.level_at(1, 0.005) == 0
    assert la.level_at(1, 1.0) == 0          # past the end: the last sample
    with pytest.raises(ValueError):
        la.level_at(1, -0.001)


def test_pulse_widths_count_only_complete_pulses():
    la = _la(la1=LA1)
    assert la.pulse_widths(1) == pytest.approx([0.003, 0.002])
    assert la.pulse_widths(1, level=0) == pytest.approx([0.002])  # the leading low run has no start edge
    with pytest.raises(ValueError):
        la.pulse_widths(1, level=2)


def test_frequency_and_duty_cycle():
    la = _la(la1=LA1)
    assert la.frequency(1) == pytest.approx(200.0)
    assert la.duty_cycle(1) == pytest.approx(0.5)
    assert _la(la1=[0, 1, 1, 1]).frequency(1) is None


def test_delay_between_channels():
    la = _la(la1=LA1, la2=LA2)
    assert la.delay(1, 2) == pytest.approx(0.002)
    assert la.delay(1, 2, after=0.003) == pytest.approx(0.002)
    assert la.delay(1, 2, to_edge="falling") == pytest.approx(0.004)
    assert la.delay(2, 1, from_edge="falling") == pytest.approx(0.001)
    assert la.delay(1, 2, after=0.008) is None


def test_timing_validates_arguments():
    la = _la(la1=LA1)
    with pytest.raises(ValueError, match="edge"):
        la.edge_times(1, "up")
    with pytest.raises(ValueError, match="to_edge"):
        la.delay(1, 2, to_edge="down")
    with pytest.raises(ValueError, match="LA channel"):
        la.edge_times(15)
    with pytest.raises(ValueError, match="timebase"):
        LaCapture(words=[0, 1], sample_rate_hz=0).edge_times(1)


def test_adc_crossing_times():
    cap = Capture(volts=[0, 1, 2, 3, 2, 1, 0, 1, 2, 3], sample_rate_hz=100.0)
    assert cap.crossing_times(1.5) == pytest.approx([0.02, 0.08])
    assert cap.crossing_times(1.5, "falling") == pytest.approx([0.05])
    assert cap.first_crossing(1.5, after=0.03) == pytest.approx(0.08)
    assert cap.first_crossing(5.0) is None


def test_adc_crossing_hysteresis_ignores_noise_on_an_edge():
    noisy = Capture(volts=[0, 1.6, 1.4, 1.6, 1.4, 3, 0], sample_rate_hz=100.0)
    assert noisy.crossing_times(1.5) == pytest.approx([0.01, 0.03, 0.05])
    assert noisy.crossing_times(1.5, hysteresis=1.0) == pytest.approx([0.05])
    assert noisy.crossing_times(1.5, "falling", hysteresis=1.0) == pytest.approx([0.06])
    with pytest.raises(ValueError):
        noisy.crossing_times(1.5, hysteresis=-1)
