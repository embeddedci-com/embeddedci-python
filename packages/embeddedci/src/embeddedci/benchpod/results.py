"""Capture result objects returned by the scope / logic-analyzer / correlated captures.

These wrap the raw device data with the scaling already applied (ADC counts → volts via the
device :class:`~embeddedci.benchpod.capabilities.Capabilities`) and add the summary helpers a
test typically asserts on (mean / rms / peak-to-peak / min / max, and an optional FFT). No
hard dependency on numpy — the reductions are plain Python; :meth:`Capture.fft` uses numpy when
it is installed and raises a clear error otherwise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from . import i2c as _i2c


@dataclass
class Capture:
    """An ADC (scope) capture: raw counts plus calibrated volts and timing.

    ``volts`` is the calibrated probe voltage per sample; ``counts`` the raw ADC codes.
    ``sample_rate_hz`` is the achieved rate (the firmware floors requested rates, so this may be
    below what was asked). Index ``i`` corresponds to ``t = i / sample_rate_hz`` seconds.
    """

    counts: List[int] = field(default_factory=list)
    volts: List[float] = field(default_factory=list)
    sample_rate_hz: float = 0.0
    source: str = ""

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
    def duration_s(self) -> float:
        return len(self.volts) / self.sample_rate_hz if self.sample_rate_hz > 0 else 0.0

    def times(self) -> List[float]:
        """Per-sample timestamps in seconds (``i / sample_rate_hz``)."""
        if self.sample_rate_hz <= 0:
            return [0.0] * len(self.volts)
        dt = 1.0 / self.sample_rate_hz
        return [i * dt for i in range(len(self.volts))]

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
        """The frequency (Hz) of the largest AC spectral bin (0.0 if indeterminate)."""
        freqs, mag = self.fft()
        if not freqs:
            return 0.0
        peak = max(range(len(mag)), key=lambda i: mag[i])
        return float(freqs[peak])


@dataclass
class LaCapture:
    """A raw multi-channel logic-analyzer capture (12-bit words, LA1..LA12).

    ``words[i]`` packs all channels for sample ``i`` (bit ``n`` = channel ``LA{n+1}``). Use
    :meth:`channel` to pull one channel out as 0/1s, or :meth:`decode` for protocol decoding.
    """

    words: List[int] = field(default_factory=list)
    sample_rate_hz: float = 0.0
    channels: int = 12

    def __len__(self) -> int:
        return len(self.words)

    @property
    def duration_s(self) -> float:
        return len(self.words) / self.sample_rate_hz if self.sample_rate_hz > 0 else 0.0

    def channel(self, la: int) -> List[int]:
        """Extract one 1-based LA channel as a list of 0/1 samples."""
        bit = la - 1
        return [(w >> bit) & 1 for w in self.words]

    def decode(self, protocol: str = "i2c", **channels):
        """Decode a protocol from this capture. See :func:`benchpod.decode.decode`.

        Examples: ``cap.decode("i2c", sda=2, scl=1)``,
        ``cap.decode("uart", rx=5, baud=115200)``,
        ``cap.decode("spi", sclk=1, mosi=2, miso=3, cs=4)``.
        """
        from .decode import decode as _decode

        return _decode(self.words, protocol, sample_rate_hz=self.sample_rate_hz, **channels)

    def decode_i2c(self, *, sda: int, scl: int):
        """Convenience: decode I2C transactions from this capture."""
        return _i2c.decode_from_la(self.words, sda_ch=sda, scl_ch=scl)


@dataclass
class AnalogCapture:
    """A correlated ADC + LA capture from a single hardware trigger (aligned timebases)."""

    adc: Capture
    la: LaCapture

    def __len__(self) -> int:
        return max(len(self.adc), len(self.la))
