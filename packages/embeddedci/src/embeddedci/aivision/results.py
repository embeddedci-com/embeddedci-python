"""Typed results of the AI Vision client.

Each result keeps the board's full answer in ``raw``, so a field the dataclass does not name yet is
still reachable. Boxes are in screen pixels once the screen is calibrated, else in camera pixels
(``Box.space`` says which).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


@dataclass(frozen=True)
class Box:
    """A rectangle ``x, y, w, h`` in ``space`` (``screen``, ``camera`` or ``norm``)."""

    x: float
    y: float
    w: float
    h: float
    space: str = "screen"

    @property
    def center(self) -> Tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["Box"]:
        if not d:
            return None
        return cls(float(d.get("x", 0)), float(d.get("y", 0)), float(d.get("w", 0)),
                   float(d.get("h", 0)), str(d.get("space", "screen")))


@dataclass
class Element:
    """One thing found on the screen: a button, icon or text line (or a live-model object)."""

    kind: str  # the board's "class": "button", "icon", "text", or a live-model class
    label: str
    text: str
    confidence: float
    box: Optional[Box]
    source: str = ""  # "ui", "ocr", "template", "live"
    score: Optional[float] = None
    id: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def center(self) -> Optional[Tuple[float, float]]:
        return self.box.center if self.box else None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Element":
        return cls(kind=str(d.get("class", "")), label=str(d.get("label", "")),
                   text=str(d.get("text", "")), confidence=float(d.get("confidence", 0) or 0),
                   box=Box.from_dict(d.get("box")), source=str(d.get("source", "")),
                   score=d.get("score"), id=str(d.get("id", "")), raw=d)


def elements(items: Optional[List[Dict[str, Any]]]) -> List[Element]:
    return [Element.from_dict(d) for d in items or []]


@dataclass
class TextBlock:
    text: str
    box: Optional[Box]
    confidence: float
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class OCRResult:
    """Text on the screen (or a region), in reading order."""

    text: str
    blocks: List[TextBlock]
    frame_id: int
    ms: int
    cached: bool
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def lines(self) -> List[str]:
        return [b.text for b in self.blocks]

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OCRResult":
        blocks = [TextBlock(str(b.get("text", "")), Box.from_dict(b.get("box")),
                            float(b.get("confidence", 0) or 0), b) for b in d.get("blocks") or []]
        return cls(str(d.get("text", "")), blocks, int(d.get("frame_id", 0) or 0),
                   int(d.get("ms", 0) or 0), bool(d.get("cached", False)), d)


@dataclass
class Health:
    """Screen health over a window of frames. ``ok`` is False when ``problems`` is not empty."""

    ok: bool
    problems: List[str]
    black: bool
    dim: bool
    frozen: bool
    frozen_for_ms: int
    flicker: bool
    flicker_hz: float
    mean_luma: float
    stuck_pixels: List[Any]
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Health":
        return cls(bool(d.get("ok")), list(d.get("problems") or []), bool(d.get("black")),
                   bool(d.get("dim")), bool(d.get("frozen")), int(d.get("frozen_for_ms", 0) or 0),
                   bool(d.get("flicker")), float(d.get("flicker_hz", 0) or 0),
                   float(d.get("mean_luma", 0) or 0), list(d.get("stuck_pixels") or []), d)


@dataclass
class Comparison:
    """How close the screen is to a reference: ``score`` 0..1 (1 = identical)."""

    score: float
    reference: str
    changed_pct: float
    diff_image: str  # board path of the diff image
    snapshot: str
    frame_id: int
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Comparison":
        return cls(float(d.get("score", 0) or 0), str(d.get("reference", "")),
                   float(d.get("changed_pct", 0) or 0), str(d.get("diff_image", "")),
                   str(d.get("snapshot", "")), int(d.get("frame_id", 0) or 0), d)


@dataclass
class Identification:
    """Which reference screen this is (``screen`` empty when none scores ``min_score``)."""

    screen: str
    score: float
    candidates: List[Tuple[str, float]]
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Identification":
        cands = [(str(c.get("name", "")), float(c.get("score", 0) or 0))
                 for c in d.get("candidates") or []]
        return cls(str(d.get("screen", "") or ""), float(d.get("score", 0) or 0), cands, d)


@dataclass
class WaitResult:
    """The outcome of a wait. ``matches`` holds the found elements for find conditions."""

    met: bool
    condition: str
    elapsed_ms: int
    evaluations: int
    frame_id: int
    matches: List[Element]
    evidence: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def match(self) -> Optional[Element]:
        """The best match of a find condition, or None."""
        return self.matches[0] if self.matches else None

    def __bool__(self) -> bool:
        return self.met

    @classmethod
    def from_dict(cls, d: Dict[str, Any], elapsed_ms: Optional[int] = None,
                  evaluations: Optional[int] = None) -> "WaitResult":
        res = d.get("result") if isinstance(d.get("result"), dict) else {}
        return cls(bool(d.get("met")), str(d.get("condition", "")),
                   int(elapsed_ms if elapsed_ms is not None else d.get("elapsed_ms", 0) or 0),
                   int(evaluations if evaluations is not None else d.get("evaluations", 0) or 0),
                   int(d.get("frame_id", 0) or 0), elements(res.get("matches")),
                   str(d.get("evidence", "") or ""), d)


@dataclass
class CheckItem:
    name: str
    passed: bool
    condition: str
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class CheckResult:
    """A list of named checks run once, with one evidence bundle for the report."""

    passed: bool
    results: List[CheckItem]
    evidence: str
    evidence_zip: str
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def failed(self) -> List[str]:
        return [r.name for r in self.results if not r.passed]

    def __bool__(self) -> bool:
        return self.passed

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CheckResult":
        items = [CheckItem(str(r.get("name", "")), bool(r.get("passed")), str(r.get("condition", "")), r)
                 for r in d.get("results") or []]
        return cls(bool(d.get("passed")), items, str(d.get("evidence", "")),
                   str(d.get("evidence_zip", "")), d)


@dataclass
class PressResult:
    """What a press (or tap on a target) found, where it tapped, and the verify outcome."""

    found: Optional[Element]
    tapped_at: Optional[Tuple[float, float]]
    verified: Optional[bool]  # None when no verify condition was given
    verify: Optional[WaitResult]
    evidence: str
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PressResult":
        at = d.get("tapped_at")
        if at is None and isinstance(d.get("action"), dict):
            at = d["action"].get("at")
        point = (float(at["x"]), float(at["y"])) if isinstance(at, dict) else None
        found = Element.from_dict(d["found"]) if isinstance(d.get("found"), dict) else None
        verify = WaitResult.from_dict(d["verify"]) if isinstance(d.get("verify"), dict) else None
        verified = d.get("verified") if "verified" in d else None
        return cls(found, point, verified, verify, str(d.get("evidence", "")), d)


@dataclass
class Calibration:
    """The screen calibration: camera corners (TL, TR, BR, BL) and the screen's own size."""

    corners: List[Tuple[float, float]]
    screen_size: Tuple[int, int]
    method: str
    quality: float
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["Calibration"]:
        if not d:
            return None
        size = d.get("screen_size") or [0, 0]
        return cls([(float(c[0]), float(c[1])) for c in d.get("corners") or []],
                   (int(size[0]), int(size[1])), str(d.get("method", "")),
                   float(d.get("quality", 0) or 0), d)


@dataclass
class Snapshot:
    """A frozen full-resolution frame with its detections, stored on the board."""

    id: str
    label: str
    frame_id: int
    urls: Dict[str, str]
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Snapshot":
        return cls(str(d.get("id", "")), str(d.get("label", "")), int(d.get("frame_id", 0) or 0),
                   dict(d.get("urls") or {}), d)


@dataclass
class Image:
    """An image from the board (a frame, a zoom, a snapshot file)."""

    data: bytes
    content_type: str
    frame_id: Optional[int] = None
    timestamp: str = ""
    space: str = ""
    headers: Dict[str, str] = field(default_factory=dict, repr=False)

    def save(self, path: Union[str, Path]) -> Path:
        p = Path(path)
        p.write_bytes(self.data)
        return p

    def __len__(self) -> int:
        return len(self.data)


@dataclass
class Lease:
    """The board's lease: driving calls from others get :class:`BusyError` while it is held."""

    id: str
    who: str
    expires: str
    ttl_s: int
