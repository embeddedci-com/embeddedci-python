"""Capture result objects returned by the ADC / logic-analyzer / correlated captures.

These wrap the raw device data with the scaling already applied (ADC counts → volts via the
device :class:`~embeddedci.benchpod.capabilities.Capabilities`) and add the summary helpers a
test typically asserts on (mean / rms / peak-to-peak / min / max, and an optional FFT), plus
timing helpers: edge and threshold-crossing timestamps, pulse widths, frequency and the delay
between two channels. No hard dependency on numpy — the reductions are plain Python;
:meth:`Capture.fft` uses numpy when it is installed and raises a clear error otherwise.

Timestamps are seconds from the capture's first sample, so an ADC and an LA timestamp from one
:class:`CorrelatedCapture` can be subtracted directly. Their resolution is one sample
(``1 / sample_rate_hz``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Union

from .constants import TRIGGER_EDGES, Edge, TriggerEdge

_EDGES = ("rising", "falling", "both")


@dataclass(frozen=True)
class Trigger:
    """Start a capture when LA ``la`` sees ``edge``: ``rising`` or ``falling``, or is ``high``/``low``.

    ``la`` is 1-12 or a wiring-profile name (``Trigger("READY")``). With a trigger, t = 0 of the capture
    is the trigger moment, and a DAC co-trigger or ``stop_dac_after`` counts from it::

        la = bp.capture_la(100_000, sample_rate_hz=1_000_000, trigger=Trigger(9, "rising"))
    """

    la: Union[int, str]
    edge: TriggerEdge = "rising"

    def __post_init__(self) -> None:
        if self.edge not in TRIGGER_EDGES:
            raise ValueError(f"trigger edge must be one of {', '.join(TRIGGER_EDGES)}, got {self.edge!r}")
        if isinstance(self.la, bool) or not (
                isinstance(self.la, str) or (isinstance(self.la, int) and 1 <= self.la <= 14)):
            raise ValueError(f"trigger la must be an LA channel 1-14 or a wiring name, got {self.la!r}")


def _check_edge(edge: str, name: str = "edge") -> None:
    if edge not in _EDGES:
        raise ValueError(f"{name} must be 'rising', 'falling' or 'both', got {edge!r}")


def _seconds(index: int, sample_rate_hz: float) -> float:
    if sample_rate_hz <= 0:
        raise ValueError("the capture has no sample rate, so it has no timebase")
    return index / sample_rate_hz


def _transitions(bits: Sequence[int], edge: str) -> List[int]:
    """Indices where the level changes — the first sample at the new level."""
    out: List[int] = []
    if not bits:
        return out
    prev = bits[0]
    for i in range(1, len(bits)):
        b = bits[i]
        if b != prev:
            if edge == "both" or (edge == "rising") == (b == 1):
                out.append(i)
            prev = b
    return out


@dataclass
class Capture:
    """An ADC capture: raw counts plus calibrated volts and timing.

    ``volts`` is the calibrated probe voltage per sample; ``counts`` the raw ADC codes.
    ``sample_rate_hz`` is the achieved rate (the firmware floors requested rates, so this may be
    below what was asked). Index ``i`` corresponds to ``t = i / sample_rate_hz`` seconds.
    ``source`` is the ADC source the capture routed (``""`` when it left the routing alone).
    """

    counts: List[int] = field(default_factory=list)
    volts: List[float] = field(default_factory=list)
    sample_rate_hz: float = 0.0
    source: str = ""
    #: The trigger that started the capture (t = 0 is its moment), or ``None``.
    trigger: Optional[Trigger] = None

    def __len__(self) -> int:
        return len(self.volts)

    # -- reductions (volts) -------------------------------------------------

    def mean(self) -> float:
        return sum(self.volts) / len(self.volts) if self.volts else 0.0

    def min(self) -> float:
        return min(self.volts) if self.volts else 0.0

    def max(self) -> float:
        return max(self.volts) if self.volts else 0.0

    def peak_to_peak(self) -> float:
        return (max(self.volts) - min(self.volts)) if self.volts else 0.0

    def rms(self) -> float:
        if not self.volts:
            return 0.0
        return math.sqrt(sum(v * v for v in self.volts) / len(self.volts))

    def rms_ac(self) -> float:
        """RMS about the mean (the AC component) — a clean amplitude for a centred waveform."""
        if not self.volts:
            return 0.0
        m = self.mean()
        return math.sqrt(sum((v - m) ** 2 for v in self.volts) / len(self.volts))

    @property
    def duration(self) -> float:
        """Capture length in seconds."""
        return len(self.volts) / self.sample_rate_hz if self.sample_rate_hz > 0 else 0.0

    def times(self) -> List[float]:
        """Per-sample timestamps in seconds (``i / sample_rate_hz``)."""
        if self.sample_rate_hz <= 0:
            return [0.0] * len(self.volts)
        dt = 1.0 / self.sample_rate_hz
        return [i * dt for i in range(len(self.volts))]

    # -- timing (volts) -----------------------------------------------------

    def crossing_times(self, threshold: float, edge: Edge = "rising", *,
                       hysteresis: float = 0.0) -> List[float]:
        """Seconds at which the calibrated volts cross ``threshold``.

        A rising crossing is the first sample at or above ``threshold + hysteresis / 2`` after the
        signal was below ``threshold - hysteresis / 2`` (a falling one the reverse), so noise on a
        slow edge counts once when ``hysteresis`` spans it. The first sample only sets the starting
        side; it is never a crossing itself.
        """
        _check_edge(edge)
        if hysteresis < 0:
            raise ValueError(f"hysteresis must be >= 0 volts, got {hysteresis!r}")
        high, low = threshold + hysteresis / 2, threshold - hysteresis / 2
        out: List[float] = []
        state: Optional[int] = None
        for i, v in enumerate(self.volts):
            if v >= high:
                level = 1
            elif v < low:
                level = 0
            else:
                continue  # inside the hysteresis band: keep the previous side
            if state is not None and level != state and (
                    edge == "both" or (edge == "rising") == (level == 1)):
                out.append(_seconds(i, self.sample_rate_hz))
            state = level
        return out

    def first_crossing(self, threshold: float, edge: Edge = "rising", *, after: float = 0.0,
                       hysteresis: float = 0.0) -> Optional[float]:
        """The first :meth:`crossing_times` entry at or after ``after`` seconds, or ``None``."""
        for t in self.crossing_times(threshold, edge, hysteresis=hysteresis):
            if t >= after:
                return t
        return None

    def fft(self):
        """Return ``(freqs_hz, magnitude)`` of the AC-coupled signal (needs numpy).

        Removes the DC mean, applies a Hann window, and returns the one-sided magnitude
        spectrum. Raises :class:`RuntimeError` if numpy is not installed.
        """
        try:
            import numpy as np
        except ImportError as exc:  # pragma: no cover - trivial
            raise RuntimeError(
                "Capture.fft needs numpy: pip install 'embeddedci[analysis]' (or numpy)"
            ) from exc
        if len(self.volts) < 2 or self.sample_rate_hz <= 0:
            return [], []
        x = np.asarray(self.volts, dtype=float)
        x = x - x.mean()
        win = np.hanning(len(x))
        spec = np.fft.rfft(x * win)
        freqs = np.fft.rfftfreq(len(x), d=1.0 / self.sample_rate_hz)
        mag = (2.0 / np.sum(win)) * np.abs(spec)
        return freqs.tolist(), mag.tolist()

    def dominant_frequency(self) -> float:
        """The frequency (Hz) of the largest AC spectral bin (0.0 if indeterminate). Needs numpy."""
        freqs, mag = self.fft()
        if not freqs:
            return 0.0
        peak = max(range(len(mag)), key=lambda i: mag[i])
        return float(freqs[peak])


@dataclass
class LaCapture:
    """A raw multi-channel logic-analyzer capture (14-bit words, LA1..LA14).

    ``words[i]`` packs all channels for sample ``i`` (bit ``n`` = channel ``LA{n+1}``). Use
    :meth:`channel` to pull one channel out as 0/1s, or :meth:`decode` for protocol decoding.
    """

    words: List[int] = field(default_factory=list)
    sample_rate_hz: float = 0.0
    channels: int = 14
    #: The trigger that started the capture (t = 0 is its moment), or ``None``.
    trigger: Optional[Trigger] = None

    def __len__(self) -> int:
        return len(self.words)

    @property
    def duration(self) -> float:
        """Capture length in seconds."""
        return len(self.words) / self.sample_rate_hz if self.sample_rate_hz > 0 else 0.0

    def channel(self, la: int) -> List[int]:
        """Extract one 1-based LA channel as a list of 0/1 samples."""
        if not 1 <= int(la) <= self.channels:
            raise ValueError(f"la must be an LA channel 1..{self.channels}, got {la!r}")
        bit = int(la) - 1
        return [(w >> bit) & 1 for w in self.words]

    def edges(self, la: int) -> int:
        """Number of level transitions on LA channel ``la``."""
        bits = self.channel(la)
        return sum(1 for a, b in zip(bits, bits[1:]) if a != b)

    # -- timing ------------------------------------------------------------

    def edge_times(self, la: int, edge: Edge = "both") -> List[float]:
        """Seconds at which LA ``la`` changes level (``rising``, ``falling`` or ``both``).

        Each timestamp is the first sample at the new level, so it can be up to one sample late.
        """
        _check_edge(edge)
        return [_seconds(i, self.sample_rate_hz) for i in _transitions(self.channel(la), edge)]

    def first_edge(self, la: int, edge: Edge = "both", *, after: float = 0.0) -> Optional[float]:
        """The first edge on LA ``la`` at or after ``after`` seconds, or ``None``."""
        for t in self.edge_times(la, edge):
            if t >= after:
                return t
        return None

    def level_at(self, la: int, t: float) -> int:
        """The level (0/1) of LA ``la`` at ``t`` seconds: the sample at or just before ``t``."""
        if t < 0 or not self.words:
            raise ValueError(f"t must be within the capture (0..{self.duration:g} s), got {t!r}")
        i = min(len(self.words) - 1, int(t * self.sample_rate_hz + 1e-9))
        _seconds(i, self.sample_rate_hz)  # a capture without a timebase has no "time t"
        return self.channel(la)[i]

    def pulse_widths(self, la: int, level: int = 1) -> List[float]:
        """Durations (s) of the complete pulses at ``level`` on LA ``la``.

        A pulse counts only when both of its edges are inside the capture, so a line that starts or
        ends the capture at ``level`` contributes no partial width.
        """
        if level not in (0, 1):
            raise ValueError(f"level must be 0 or 1, got {level!r}")
        start = "rising" if level == 1 else "falling"
        bits = self.channel(la)
        changes = _transitions(bits, "both")
        return [_seconds(b - a, self.sample_rate_hz) for a, b in zip(changes, changes[1:])
                if bits[a] == level and _transitions(bits[a - 1:a + 1], start)]

    def frequency(self, la: int) -> Optional[float]:
        """Mean frequency (Hz) from the rising edges on LA ``la``; ``None`` with fewer than two."""
        rises = self.edge_times(la, "rising")
        if len(rises) < 2:
            return None
        return (len(rises) - 1) / (rises[-1] - rises[0])

    def duty_cycle(self, la: int) -> float:
        """Fraction of the capture that LA ``la`` spends high (0.0 for an empty capture)."""
        bits = self.channel(la)
        return sum(bits) / len(bits) if bits else 0.0

    def delay(self, from_la: int, to_la: int, *, from_edge: Edge = "rising",
              to_edge: Edge = "rising", after: float = 0.0) -> Optional[float]:
        """Seconds from the first ``from_edge`` on ``from_la`` (at or after ``after``) to the next
        ``to_edge`` on ``to_la`` at or after it — e.g. a trigger pin to a "result ready" pin.

        ``None`` when either edge is not in the capture. Resolution is one sample.
        """
        _check_edge(from_edge, "from_edge")
        _check_edge(to_edge, "to_edge")
        start = self.first_edge(from_la, from_edge, after=after)
        if start is None:
            return None
        end = self.first_edge(to_la, to_edge, after=start)
        return None if end is None else end - start

    def decode(self, protocol: str = "i2c", **channels):
        """Decode a protocol from this capture. See :func:`benchpod.decode.decode`.

        Examples: ``cap.decode("i2c", sda=2, scl=1)``,
        ``cap.decode("uart", rx=5, baud=115200)``,
        ``cap.decode("spi", sclk=1, mosi=2, miso=3, cs=4)``.
        """
        from .decode import decode as _decode

        return _decode(self.words, protocol, sample_rate_hz=self.sample_rate_hz, **channels)


@dataclass
class CorrelatedCapture:
    """An ADC and an LA capture taken from ONE hardware trigger, so their timebases align."""

    adc: Capture
    la: LaCapture

    def __len__(self) -> int:
        return max(len(self.adc), len(self.la))
