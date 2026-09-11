"""DAC output handles plus the fault/segment helpers for arbitrary-waveform replay.

Both the parametric generator and a replay keep driving the DAC until they are stopped. On a v2
pod a *deep* replay streams from PSRAM and — with gateware v18 — an ADC/LA capture can run **at
the same time**. :class:`DacHandle` / :class:`ReplayHandle` model a running output so a test can
express "drive this while I capture" as a context manager::

    with bp.replay(recording, dac_path="5v"):
        la = bp.capture_la(8192, sample_rate_hz=1e6)   # runs concurrently
    # DAC stopped on exit

The arming itself (route the DAC path, ``load_bin`` + ``replay`` over the tunnel, or the server's
``/dac/replay/start``) lives on :class:`~embeddedci.benchpod.client.BenchPod`; this module holds
the transport-independent pieces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from .constants import FAULT_TYPES, check_choice

if TYPE_CHECKING:  # pragma: no cover
    from .state import FpgaImageInfo


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
        check_choice(self.type, FAULT_TYPES, "fault type")
        d: Dict[str, Any] = {"type": self.type, "start": int(self.start), "width": int(self.width)}
        if self.level is not None:
            d["level"] = int(self.level)
        return d


@dataclass
class Segment:
    """One piece of a segmented waveform: ``ramp`` (``v_start`` → ``v_end``), ``hold`` or ``step``.

    ``duration`` is in seconds; voltages in volts.
    """

    shape: str
    duration: float
    v_start: float
    v_end: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        if self.duration <= 0:
            raise ValueError(f"segment duration must be > 0 seconds, got {self.duration!r}")
        return {
            "shape": self.shape,
            "duration_ms": float(self.duration) * 1000.0,
            "v_start": float(self.v_start),
            "v_end": float(self.v_end if self.v_end is not None else self.v_start),
        }


class DacHandle:
    """A running DAC output (generator or replay). Use as a context manager or call :meth:`stop`."""

    def __init__(self, *, stop: Callable[[], Any], dac_path: str = "", cotrig: bool = False,
                 data: Optional[dict] = None) -> None:
        self._stop = stop
        self.dac_path = dac_path
        #: True when the DAC was ARMED to start on the next capture's hardware t0 (``on_capture``)
        #: rather than started immediately. When False the output is already driving.
        self.cotrig = cotrig
        #: The device/server reply that armed it.
        self.data = data or {}
        self._stopped = False

    def stop(self) -> None:
        """Stop the output (idempotent)."""
        if self._stopped:
            return
        self._stopped = True
        self._stop()

    def __enter__(self) -> "DacHandle":
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"{type(self).__name__}(dac_path={self.dac_path!r}, cotrig={self.cotrig})"


class ReplayHandle(DacHandle):
    """A looping DAC replay: a :class:`DacHandle` that also knows what it is replaying."""

    def __init__(self, *, stop: Callable[[], Any], samples: int = 0, sample_rate_hz: float = 0.0,
                 dac_path: str = "", deep: bool = False, data: Optional[dict] = None,
                 cotrig: bool = False, switched_image: Optional["FpgaImageInfo"] = None) -> None:
        super().__init__(stop=stop, dac_path=dac_path, cotrig=cotrig, data=data)
        self.samples = samples
        #: The replay rate, or 0.0 when the device picked it.
        self.sample_rate_hz = sample_rate_hz
        #: The replay streams from PSRAM (deeper than the DAC's block RAM).
        self.deep = deep
        #: The gateware image switch made for this replay (``None`` when none was needed). A switch
        #: resets the FPGA, stopping anything else that ran in it.
        self.switched_image = switched_image

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (f"ReplayHandle(samples={self.samples}, rate_hz={self.sample_rate_hz:.0f}, "
                f"dac_path={self.dac_path!r}, deep={self.deep}, cotrig={self.cotrig})")


def normalize_fault(fault: "Fault | dict | None") -> Optional[Dict[str, Any]]:
    """Coerce a :class:`Fault` or plain dict (or None) to the wire dict."""
    if fault is None:
        return None
    if isinstance(fault, Fault):
        return fault.to_dict()
    d = dict(fault)
    check_choice(str(d.get("type", "")), FAULT_TYPES, "fault type")
    return d


def normalize_segments(segments) -> list:
    """Coerce a list of :class:`Segment`/dicts to wire dicts."""
    out = []
    for s in segments or []:
        out.append(s.to_dict() if isinstance(s, Segment) else dict(s))
    return out
