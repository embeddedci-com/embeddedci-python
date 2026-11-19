"""The run folder's file formats (schema version 1): ``run.json``, ``junit.xml``, ``parts/<step>.json``.

The records here are plain data. :mod:`~embeddedci.benchpod.runs.active` fills them while tests run
and writes them at the end. Field names are part of the contract with ``benchpod ci`` and the server:
add fields, never rename them.
"""

from __future__ import annotations

import hashlib
import json
import math
import numbers
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

SCHEMA_VERSION = 1

#: Case outcomes, and the order counts are listed in.
CASE_OUTCOMES = ("passed", "failed", "error", "skipped", "xfailed", "xpassed")
#: Run outcomes.
RUN_OUTCOMES = ("passed", "failed", "error", "invalid")
#: Trace keep policies.
KEEP_POLICIES = ("all", "failed", "none")
#: Measurement sources.
MEASUREMENT_SOURCES = ("measure", "native", "record_property", "junit", "openhtf")

#: Caps (section 2 and 3 of the spec).
MESSAGE_MAX_BYTES = 16 * 1024
LOG_MAX_BYTES = 64 * 1024
MEASUREMENTS_PER_CASE = 200
INLINE_JSON_MAX_BYTES = 4 * 1024
TRACE_MAX_BYTES_PER_RUN = 50 * 1024 * 1024
SLUG_MAX_CHARS = 120

REDACTED = "<redacted>"
_REDACT_KEYS = frozenset({"password", "passwd", "psk", "token", "api_key", "apikey", "secret",
                          "authorization", "wifi_pass"})
#: ``key`` is redacted only when its value is a string longer than this (a key id stays readable).
_KEY_MIN_SECRET_LEN = 16

_SLUG_BAD = re.compile(r"[^A-Za-z0-9._-]")


# -- small helpers ---------------------------------------------------------------------------------

def case_slug(case_id: str) -> str:
    """The file name stem for a case id: every character outside ``[A-Za-z0-9._-]`` replaced by
    ``_`` (a leading ``.`` too), at most 120 characters, plus ``-<first 8 hex of sha1(id)>`` when it
    was cut or changed."""
    slug = _SLUG_BAD.sub("_", case_id)
    if slug.startswith("."):  # a file name may not start with a dot (hidden; the server refuses it)
        slug = "_" + slug[1:]
    changed = slug != case_id
    if len(slug) > SLUG_MAX_CHARS:
        slug = slug[:SLUG_MAX_CHARS]
        changed = True
    if not slug:
        changed = True
    if changed:
        slug += "-" + hashlib.sha1(case_id.encode("utf-8")).hexdigest()[:8]
    return slug


def redact(value: Any) -> Any:
    """A copy of ``value`` with secrets replaced by ``"<redacted>"``, at any depth.

    Matched keys (case-insensitive): ``password``, ``passwd``, ``psk``, ``token``, ``api_key``,
    ``apikey``, ``secret``, ``authorization``, ``wifi_pass``, and ``key`` when its value is a string
    longer than 16 characters."""
    if isinstance(value, Mapping):
        out: Dict[Any, Any] = {}
        for k, v in value.items():
            lk = str(k).lower()
            if lk in _REDACT_KEYS or (lk == "key" and isinstance(v, str) and len(v) > _KEY_MIN_SECRET_LEN):
                out[k] = REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def cap_text(text: str, max_bytes: int, *, keep: str = "head") -> str:
    """``text`` cut to at most ``max_bytes`` UTF-8 bytes. ``keep="head"`` keeps the start,
    ``"tail"`` the end; a cut text says how much was left out."""
    if not text:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    cut = len(raw) - max_bytes
    note = f"\n[... {cut} bytes cut ...]\n"
    room = max(0, max_bytes - len(note.encode("utf-8")))
    if keep == "tail":
        body = raw[len(raw) - room:].decode("utf-8", "ignore")
        return note.lstrip("\n") + body
    body = raw[:room].decode("utf-8", "ignore")
    return body + note.rstrip("\n")


def canonical_json(value: Any) -> bytes:
    """Sorted keys, no spaces: the bytes the ``sha256`` fields hash."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str).encode("utf-8")


def sha256_of_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def compact_json(value: Any) -> str:
    """One-line JSON for trace lines (anything not JSON becomes its ``str``)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=_json_default)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return {"bytes": len(obj)}
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:  # noqa: BLE001 - fall back to str
            pass
    return str(obj)


def write_json_atomic(path: Union[str, "os.PathLike[str]"], data: Any) -> None:
    """Write ``data`` as JSON to ``path`` through a temporary file in the same folder and a rename,
    so a reader never sees half a file."""
    write_bytes_atomic(path, (json.dumps(data, indent=2, ensure_ascii=False, default=_json_default)
                              + "\n").encode("utf-8"))


def write_bytes_atomic(path: Union[str, "os.PathLike[str]"], data: bytes) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# -- measurements ----------------------------------------------------------------------------------

def parse_drift(drift: Union[str, float, int, None]) -> Optional[float]:
    """A drift tolerance as a fraction: ``"20%"`` and ``0.2`` both give ``0.2``; ``None`` stays None."""
    if drift is None:
        return None
    if isinstance(drift, bool):
        raise TypeError("drift must be a fraction (0.2) or a percentage string ('20%'), not a bool")
    if isinstance(drift, str):
        text = drift.strip()
        if not text.endswith("%"):
            raise ValueError(f"drift {drift!r}: write a percentage ('20%') or a fraction (0.2)")
        try:
            value = float(text[:-1].strip()) / 100.0
        except ValueError:
            raise ValueError(f"drift {drift!r}: write a percentage ('20%') or a fraction (0.2)") from None
    elif isinstance(drift, numbers.Real):
        value = float(drift)
    else:
        raise TypeError(f"drift must be a fraction (0.2) or a percentage string ('20%'), got {drift!r}")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"drift {drift!r}: must be a finite, non-negative tolerance")
    return value


def check_number(value: Any, what: str) -> Union[int, float]:
    """``value`` when it is a finite int or float (bool, NaN and infinity are refused)."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError(f"{what} must be a number (int or float), got {type(value).__name__} {value!r}")
    if isinstance(value, numbers.Integral):
        return int(value)
    f = float(value)
    if not math.isfinite(f):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    return f


def is_plain_number(value: Any) -> bool:
    """True for a finite int or float that is not a bool (what ``record_property`` values count)."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return False
    return math.isfinite(float(value))


@dataclass
class Measurement:
    """One named value of a case (spec section 2.2)."""

    name: str
    value: Union[int, float]
    unit: str = ""
    min: Optional[Union[int, float]] = None
    max: Optional[Union[int, float]] = None
    drift: Optional[float] = None
    outcome: str = "recorded"
    source: str = "measure"

    @classmethod
    def make(cls, name: str, value: Any, unit: str = "", min: Any = None, max: Any = None,
             drift: Any = None, source: str = "measure") -> "Measurement":
        """Validate and build a measurement; ``outcome`` follows from the limits."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"measurement name must be a non-empty string, got {name!r}")
        if source not in MEASUREMENT_SOURCES:
            raise ValueError(f"measurement source {source!r} is not one of {', '.join(MEASUREMENT_SOURCES)}")
        v = check_number(value, f"measurement {name!r} value")
        lo = check_number(min, f"measurement {name!r} min") if min is not None else None
        hi = check_number(max, f"measurement {name!r} max") if max is not None else None
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(f"measurement {name!r}: min {lo} is above max {hi}")
        if not isinstance(unit, str):
            raise TypeError(f"measurement {name!r} unit must be a string, got {unit!r}")
        if lo is None and hi is None:
            outcome = "recorded"
        elif (lo is not None and v < lo) or (hi is not None and v > hi):
            outcome = "failed"
        else:
            outcome = "passed"
        return cls(name=name, value=v, unit=unit, min=lo, max=hi, drift=parse_drift(drift),
                   outcome=outcome, source=source)

    def describe_failure(self) -> str:
        unit = f" {self.unit}" if self.unit else ""
        limits = []
        if self.min is not None:
            limits.append(f"min {self.min}{unit}")
        if self.max is not None:
            limits.append(f"max {self.max}{unit}")
        return f"{self.name} = {self.value}{unit} is outside the limits ({', '.join(limits)})"

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "value": self.value, "unit": self.unit, "min": self.min,
                "max": self.max, "drift": self.drift, "outcome": self.outcome, "source": self.source}


# -- calls -----------------------------------------------------------------------------------------

@dataclass
class CallStats:
    """Per-command timings of one case: ``n``, ``p50_ms``, ``p95_ms``, ``max_ms``, ``errors``."""

    ms: List[float] = field(default_factory=list)
    errors: int = 0

    def add(self, ms: float, ok: bool) -> None:
        self.ms.append(ms)
        if not ok:
            self.errors += 1

    def to_dict(self) -> Dict[str, Any]:
        data = sorted(self.ms)
        return {"n": len(data), "p50_ms": _round_ms(percentile(data, 50)),
                "p95_ms": _round_ms(percentile(data, 95)),
                "max_ms": _round_ms(data[-1] if data else 0.0), "errors": self.errors}


def percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile of already sorted values (0 for none)."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(pct / 100.0 * len(sorted_values)))
    return float(sorted_values[min(rank, len(sorted_values)) - 1])


def _round_ms(ms: float) -> float:
    return round(float(ms), 3)


# -- cases -----------------------------------------------------------------------------------------

@dataclass
class Case:
    """One test case (spec section 2.1)."""

    id: str
    step: str = "pytest"
    kind: str = "pytest"
    outcome: str = "passed"
    duration_ms: Union[int, float] = 0
    message: str = ""
    skip_reason: str = ""
    attempt: int = 1
    trace: str = ""
    log: str = ""
    measurements: List[Measurement] = field(default_factory=list)
    calls: Dict[str, CallStats] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "step": self.step,
            "kind": self.kind,
            "outcome": self.outcome,
            "duration_ms": self.duration_ms,
            "message": cap_text(self.message, MESSAGE_MAX_BYTES),
            "skip_reason": self.skip_reason,
            "attempt": self.attempt,
            "trace": self.trace,
            "log": cap_text(self.log, LOG_MAX_BYTES, keep="tail"),
            "measurements": [m.to_dict() for m in self.measurements],
            "calls": {cmd: s.to_dict() for cmd, s in sorted(self.calls.items())},
        }


def count_outcomes(cases: Iterable[Case]) -> Dict[str, int]:
    counts = {k: 0 for k in CASE_OUTCOMES}
    for c in cases:
        if c.outcome in counts:
            counts[c.outcome] += 1
    return counts


def run_outcome(cases: Iterable[Case], valid: bool = True, step_failed: bool = False) -> str:
    """``invalid`` when the run is not valid; else ``error`` when any case is ``error``; else
    ``failed`` when any case failed (or a required step failed); else ``passed``."""
    if not valid:
        return "invalid"
    outcomes = {c.outcome for c in cases}
    if "error" in outcomes:
        return "error"
    if "failed" in outcomes or step_failed:
        return "failed"
    return "passed"


def empty_health() -> Dict[str, Any]:
    return {"valid": True, "reasons": [], "lost_samples": 0, "rate_misses": 0, "busy_retries": 0,
            "reconnects": 0, "lease_lost": False, "measurements_dropped": 0, "trace_truncated": False}


def empty_pod() -> Dict[str, Any]:
    return {"name": "", "board": "", "firmware": "", "gateware": 0, "features_rev": 0,
            "calibration_sha256": "", "connection": ""}


# -- writers ---------------------------------------------------------------------------------------

def build_run_record(*, run_id: str, key: Mapping[str, Any], trigger: str, started: str,
                     finished: str, inputs: Mapping[str, Any], health: Mapping[str, Any],
                     cases: Sequence[Case], duration_ms: Union[int, float], keep: str) -> Dict[str, Any]:
    """The ``run.json`` object (spec section 2)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "key": dict(key),
        "trigger": trigger,
        "started": started,
        "finished": finished,
        "inputs": dict(inputs),
        "health": dict(health),
        "results": {
            "outcome": run_outcome(cases, valid=bool(health.get("valid", True))),
            "duration_ms": duration_ms,
            "counts": count_outcomes(cases),
            "cases": [c.to_dict() for c in cases],
        },
        "keep": {"traces": keep},
    }


def build_part_record(*, step: str, cases: Sequence[Case], health: Mapping[str, Any],
                      tools: Mapping[str, Any], pod: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The ``parts/<step>.json`` object a child process hands to ``benchpod ci`` (spec section 4)."""
    part_health = {k: health.get(k, empty_health()[k]) for k in
                   ("lost_samples", "rate_misses", "busy_retries", "reconnects",
                    "measurements_dropped", "trace_truncated", "reasons")}
    out: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "step": step,
        "kind": "pytest",
        "cases": [c.to_dict() for c in cases],
        "health": part_health,
        "tools": {k: tools.get(k, "") for k in ("sdk", "python", "pytest")},
    }
    if pod:
        out["pod"] = dict(pod)
    return out


def _split_case_id(case_id: str) -> "tuple[str, str]":
    """JUnit ``classname`` and ``name`` for a pytest node id, the way pytest's own junitxml does:
    ``tests/test_a.py::TestX::test_y[1]`` -> ``tests.test_a.TestX``, ``test_y[1]``."""
    parts = case_id.split("::")
    if len(parts) == 1:
        return "", case_id
    names = parts[:-1]
    path = names[0]
    if path.endswith(".py"):
        path = path[:-3]
    path = path.replace("/", ".").replace("\\", ".")
    return ".".join([path] + names[1:]), parts[-1]


def _xml_safe(text: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "?", text or "")


def junit_xml(suite: str, cases: Sequence[Case], *, timestamp: str = "",
              duration_ms: Union[int, float] = 0) -> bytes:
    """The cases as JUnit XML: one ``<testsuite name="<suite>">`` inside ``<testsuites>``."""
    counts = count_outcomes(cases)
    root = ET.Element("testsuites")
    ts = ET.SubElement(root, "testsuite", {
        "name": suite,
        "tests": str(len(cases)),
        "failures": str(counts["failed"]),
        "errors": str(counts["error"]),
        "skipped": str(counts["skipped"] + counts["xfailed"]),
        "time": f"{float(duration_ms) / 1000.0:.3f}",
    })
    if timestamp:
        ts.set("timestamp", timestamp)
    for case in cases:
        classname, name = _split_case_id(case.id)
        tc = ET.SubElement(ts, "testcase", {"classname": classname, "name": name,
                                            "time": f"{float(case.duration_ms) / 1000.0:.3f}"})
        if case.measurements:
            props = ET.SubElement(tc, "properties")
            for m in case.measurements:
                ET.SubElement(props, "property", {"name": m.name, "value": str(m.value)})
        message = _xml_safe(cap_text(case.message, MESSAGE_MAX_BYTES))
        first_line = message.strip().splitlines()[-1] if message.strip() else ""
        if case.outcome == "failed":
            el = ET.SubElement(tc, "failure", {"message": first_line[:500]})
            el.text = message
        elif case.outcome == "error":
            el = ET.SubElement(tc, "error", {"message": first_line[:500]})
            el.text = message
        elif case.outcome == "skipped":
            ET.SubElement(tc, "skipped", {"type": "pytest.skip", "message": _xml_safe(case.skip_reason)})
        elif case.outcome == "xfailed":
            ET.SubElement(tc, "skipped", {"type": "pytest.xfail", "message": _xml_safe(case.skip_reason)})
        if case.log:
            out = ET.SubElement(tc, "system-out")
            out.text = _xml_safe(cap_text(case.log, LOG_MAX_BYTES, keep="tail"))
    ET.indent(root)
    return (b'<?xml version="1.0" encoding="utf-8"?>\n'
            + ET.tostring(root, encoding="utf-8", xml_declaration=False) + b"\n")
