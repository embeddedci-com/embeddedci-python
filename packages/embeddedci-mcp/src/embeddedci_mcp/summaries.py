"""Turn captures into something an agent can reason about without the raw sample arrays.

An ADC capture becomes statistics, the dominant frequency and a min/max envelope (a faithful
thumbnail of the waveform in a few hundred numbers); an LA capture becomes per-channel activity.
"""

from typing import List, Sequence, Tuple

from embeddedci.benchpod import Capture, LaCapture

from . import models as m

_V_DIGITS = 5


def clip(text: str, limit: int) -> str:
    """Keep ``text`` under ``limit`` characters, preserving its start and its end."""
    if len(text) <= limit:
        return text
    head = limit // 4
    return text[:head] + "\n…(truncated)…\n" + text[-(limit - head):]


def envelope(values: Sequence[float], points: int) -> Tuple[List[float], List[float], int]:
    """Min/max per bucket over at most ``points`` buckets; returns (mins, maxs, bucket_size)."""
    n = len(values)
    if n == 0:
        return [], [], 0
    size = -(-n // max(1, min(points, n)))
    mins: List[float] = []
    maxs: List[float] = []
    for i in range(0, n, size):
        chunk = values[i:i + size]
        mins.append(round(min(chunk), _V_DIGITS))
        maxs.append(round(max(chunk), _V_DIGITS))
    return mins, maxs, size


def adc_summary(cap: Capture, points: int) -> m.AdcCaptureResult:
    dominant = None
    if len(cap) >= 16 and cap.sample_rate_hz > 0:
        try:
            dominant = round(cap.dominant_frequency(), 3)
        except RuntimeError:  # numpy missing
            dominant = None
    mins, maxs, size = envelope(cap.volts, points)
    step = size / cap.sample_rate_hz if cap.sample_rate_hz > 0 else 0.0
    r = lambda v: round(v, _V_DIGITS)  # noqa: E731
    return m.AdcCaptureResult(
        samples=len(cap), sample_rate_hz=cap.sample_rate_hz, duration=cap.duration,
        source=cap.source or None, mean=r(cap.mean()), min=r(cap.min()), max=r(cap.max()),
        peak_to_peak=r(cap.peak_to_peak()), rms=r(cap.rms()), rms_ac=r(cap.rms_ac()),
        dominant_frequency_hz=dominant, envelope_step=step, envelope_min=mins, envelope_max=maxs,
    )


def la_summary(la: LaCapture) -> m.LaCaptureResult:
    channels: List[m.LaChannelSummary] = []
    rate = la.sample_rate_hz
    for ch in range(1, 13):
        bits = la.channel(ch)
        if not bits:
            continue
        first = None
        edges = 0
        for i in range(1, len(bits)):
            if bits[i] != bits[i - 1]:
                edges += 1
                if first is None:
                    first = i
        channels.append(m.LaChannelSummary(
            la=ch, initial=bits[0], final=bits[-1], edges=edges,
            high_fraction=round(sum(bits) / len(bits), 4),
            first_edge=(first / rate) if (first is not None and rate > 0) else None,
            est_frequency_hz=round(edges / 2 / la.duration, 3) if (edges >= 2 and la.duration > 0) else None,
        ))
    return m.LaCaptureResult(samples=len(la), sample_rate_hz=rate, duration=la.duration,
                             channels=channels)
