"""Small helpers shared by the end-to-end tests."""

from __future__ import annotations

import time
from typing import List, Optional, Sequence, Tuple

from embeddedci.benchpod import BenchPod, FirmwareError

#: Seconds for relays and a freshly started DAC output to settle before a capture.
SETTLE = 0.3


def p2p(cap) -> int:
    """Peak-to-peak spread of a capture in raw ADC counts."""
    return max(cap.counts) - min(cap.counts) if cap.counts else 0


def adc_levels(pod: BenchPod, source: str, seconds: float) -> List[float]:
    """Calibrated readings of a slowly changing input for ``seconds``.

    ``adc_read`` refuses a burst that straddles an edge ("input not settled"); those are skipped.
    """
    readings: List[float] = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            readings.append(pod.adc_read(source).voltage)
        except FirmwareError:
            pass
    return readings


def split_levels(readings: Sequence[float], threshold: float) -> Tuple[Optional[float], Optional[float]]:
    """Mean of the readings above and at/below ``threshold`` (``None`` when a side is empty)."""
    hi = [r for r in readings if r > threshold]
    lo = [r for r in readings if r <= threshold]
    return (sum(hi) / len(hi) if hi else None), (sum(lo) / len(lo) if lo else None)
