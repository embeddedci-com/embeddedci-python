"""Turn SDK results into something an agent can reason about without the raw sample arrays.

An ADC capture becomes statistics, the dominant frequency and a min/max envelope (a faithful
thumbnail of the waveform in a few hundred numbers); an LA capture becomes per-channel activity; a
power profile becomes its statistics plus a short current/voltage trace. A wiring profile becomes a
12-row pin table.
"""

from typing import Any, List, Optional, Sequence, Tuple

from embeddedci.benchpod import Capture, LaCapture, PowerProfile, Trigger, Wiring
from embeddedci.benchpod.constants import PULL_OHMS, PULLDOWN_CHANNELS

from . import models as m

_V_DIGITS = 5
_A_DIGITS = 6


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


def trigger_text(trigger: Optional[Trigger]) -> Optional[str]:
    """A capture's trigger as ``"LA9 rising"``, or ``None`` when it was free-running."""
    if trigger is None:
        return None
    la = trigger.la
    return f"LA{la} {trigger.edge}" if isinstance(la, int) else f"{la} {trigger.edge}"


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
        trigger=trigger_text(cap.trigger),
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
                             channels=channels, trigger=trigger_text(la.trigger))


# -- wiring, pins and power ------------------------------------------------------------

def _bias(la: int) -> Optional[str]:
    ohms = PULL_OHMS.get(la)
    return None if not ohms else f"{ohms} {'down' if la in PULLDOWN_CHANNELS else 'up'}"


def wiring_summary(wiring: Wiring, *, saved: bool = False) -> m.WiringResult:
    """The effective profile, its 12-row pin table and anything risky about it."""
    pins = wiring.pins()
    return m.WiringResult(
        source=wiring.source, version=wiring.version, la_voltage=wiring.la_voltage,
        efuse=wiring.efuse, uart_baud=wiring.uart_baud, i2c_address=wiring.i2c_address,
        swd_nreset=wiring.swd_nreset, swd_target=wiring.swd_target,
        pins=[m.WiringPin(la=la, wired_to=pins.get(la), pull=_bias(la)) for la in range(1, 13)],
        signals=[m.WiringSignal(name=s.name, la=s.la, direction=s.direction,
                                active_low=s.active_low, description=s.description)
                 for s in wiring.signals],
        warnings=wiring.warnings(), profile=wiring.to_dict(), saved=saved,
    )


def pin_state(pin: Any) -> m.LaPinResult:
    """One :class:`~embeddedci.benchpod.LaPinState` as a tool result row."""
    return m.LaPinResult(la=pin.la, function=pin.function, gpio=pin.gpio, level=pin.level,
                         pull=pin.pull, pull_ohms=pin.pull_ohms, pull_on=pin.pull_on,
                         in_use=pin.in_use)


def power_summary(profile: PowerProfile, points: int) -> m.PowerProfileResult:
    """A power profile's statistics plus its trace averaged down to at most ``points`` points."""
    current, voltage, step = _trace(profile, points)
    return m.PowerProfileResult(
        efuse=profile.efuse, rate_hz=profile.rate_hz, adc_rate_hz=profile.adc_rate_hz,
        n=profile.n, duration=profile.duration,
        avg_current=round(profile.avg_current, _A_DIGITS),
        min_current=round(profile.min_current, _A_DIGITS),
        peak_current=round(profile.peak_current, _A_DIGITS),
        avg_voltage=round(profile.avg_voltage, _V_DIGITS),
        min_voltage=round(profile.min_voltage, _V_DIGITS),
        max_voltage=round(profile.max_voltage, _V_DIGITS),
        energy=round(profile.energy, _A_DIGITS), charge=round(profile.charge, _A_DIGITS),
        avg_power=round(profile.avg_power, _A_DIGITS), fault=profile.fault,
        truncated=profile.truncated, trace_step=step, trace_current=current, trace_voltage=voltage,
    )


def _trace(profile: PowerProfile, points: int) -> Tuple[List[float], List[float], float]:
    """Bucket-average the kept samples down to at most ``points`` (current, voltage, seconds/point)."""
    samples = profile.samples
    if not samples or points <= 0:
        return [], [], 0.0
    size = -(-len(samples) // min(points, len(samples)))
    current: List[float] = []
    voltage: List[float] = []
    for i in range(0, len(samples), size):
        chunk = samples[i:i + size]
        current.append(round(sum(s[1] for s in chunk) / len(chunk), _A_DIGITS))
        voltage.append(round(sum(s[2] for s in chunk) / len(chunk), _V_DIGITS))
    step = profile.duration / len(current) if (profile.duration > 0 and current) else 0.0
    return current, voltage, step
