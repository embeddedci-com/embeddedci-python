"""DAC arbitrary-waveform replay: fault/segment helpers and the armed-replay handle.

A replay loops out of the DAC until it is stopped. On a v2 pod a *deep* replay streams from
PSRAM and — with gateware v18 — an ADC/LA capture can run **at the same time** (the DAC reader
and the capture writer are time-multiplexed on the quad bus). :class:`ReplayHandle` models the
looping replay so a test can express "replay this while I capture" as a context manager::

    with bp.replay(recording, dac_path="5v", loop=True):
        la = bp.capture_la(samples=8192, sample_rate_mhz=1)   # runs concurrently
    # DAC stopped on exit

The actual arming (route the DAC path, ``load_bin`` + ``replay`` over the tunnel, or the server's
``/dac/replay/start``) lives on :class:`~embeddedci.benchpod.client.BenchPod`; this module holds
the transport-independent pieces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


@dataclass
class Fault:
    """A fault to splice into a replayed waveform (mirrors the server's ``applyWaveformFault``).

    ``type`` is ``"flatline"``, ``"spike"`` or ``"stuck"``; ``start``/``width`` are sample
    indices into the (downsampled) replay; ``level`` is an optional raw DAC code (defaults per
    type). Injected server-side for a library replay, or client-side (:func:`benchpod.dsp.apply_fault`)
    for a direct replay.
    """

    type: str
    start: int = 0
    width: int = 0
    level: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"type": self.type, "start": int(self.start), "width": int(self.width)}
        if self.level is not None:
            d["level"] = int(self.level)
        return d


@dataclass
class Segment:
    """One piece of a segmented waveform (``ramp``/``hold``/``step``)."""

    shape: str
    duration_ms: float
    v_start: float
    v_end: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "shape": self.shape,
            "duration_ms": float(self.duration_ms),
            "v_start": float(self.v_start),
            "v_end": float(self.v_end if self.v_end is not None else self.v_start),
        }


class ReplayHandle:
    """A currently-armed (looping) DAC replay. Use as a context manager or call :meth:`stop`."""

    def __init__(self, *, stop: Callable[[], Any], samples: int = 0, sample_rate_hz: float = 0.0,
                 dac_path: str = "", deep: bool = False, data: Optional[dict] = None) -> None:
        self._stop = stop
        self.samples = samples
        self.sample_rate_hz = sample_rate_hz
        self.dac_path = dac_path
        self.deep = deep
        self.data = data or {}
        self._stopped = False

    def stop(self) -> Any:
        """Stop the looping replay (idempotent)."""
        if self._stopped:
            return None
        self._stopped = True
        return self._stop()

    def __enter__(self) -> "ReplayHandle":
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (f"ReplayHandle(samples={self.samples}, rate_hz={self.sample_rate_hz:.0f}, "
                f"dac_path={self.dac_path!r}, deep={self.deep})")


def normalize_fault(fault: "Fault | dict | None") -> Optional[Dict[str, Any]]:
    """Coerce a :class:`Fault` or plain dict (or None) to the wire dict."""
    if fault is None:
        return None
    if isinstance(fault, Fault):
        return fault.to_dict()
    return dict(fault)


def normalize_segments(segments) -> list:
    """Coerce a list of :class:`Segment`/dicts to wire dicts."""
    out = []
    for s in segments or []:
        out.append(s.to_dict() if isinstance(s, Segment) else dict(s))
    return out
