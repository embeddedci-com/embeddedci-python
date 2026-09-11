"""Small helpers shared by the end-to-end tests."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, List, Optional, Sequence, Tuple

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


def events_during(capture: Callable[[], Any], fire: Callable[[int], Any], *, count: int,
                  interval: float, lead: float = 0.4) -> Tuple[Any, List[float]]:
    """Run ``capture()`` in a thread and call ``fire(k)`` every ``interval`` host seconds while it
    runs (the pod takes commands during a capture over TCP). Returns the capture and the host
    time each event was sent."""
    result: dict = {}

    def run() -> None:
        try:
            result["capture"] = capture()
        except BaseException as exc:  # re-raised on the test's thread
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(lead)
    stamps: List[float] = []
    t0 = time.monotonic()
    try:
        for k in range(count):
            while time.monotonic() < t0 + k * interval:
                time.sleep(0.0005)
            stamps.append(time.monotonic())
            fire(k)
    finally:
        # even when fire() raises: an abandoned capture keeps the pod's single capture slot and
        # the next test's capture is refused as "busy"
        thread.join()
    if "error" in result:
        raise result["error"]
    return result["capture"], stamps


def host_clock_rate(event_samples: Sequence[int], stamps: Sequence[float]) -> float:
    """Real samples per second: fit the sample index of each event against the host time it was
    sent. The host fires at a fixed interval, so events are paired in order; events after the
    capture ended are ignored and outliers (a command that sat in a queue) are dropped."""
    import numpy as np

    n = min(len(event_samples), len(stamps))
    if n < 8:
        raise AssertionError(f"too few events in the capture: {len(event_samples)} seen, "
                             f"{len(stamps)} sent")
    x = np.asarray(stamps[:n]) - stamps[0]
    y = np.asarray(event_samples[:n], dtype=float)
    slope, icpt = np.polyfit(x, y, 1)
    resid = np.abs(y - (slope * x + icpt))
    keep = resid < max(3 * np.median(resid), 0.002 * slope)   # 2 ms floor
    if keep.sum() >= 8:
        slope, _ = np.polyfit(x[keep], y[keep], 1)
    return float(slope)
