"""Which firmware and gateware have which BenchPod feature: the SDK's copy of the features table.

``feature_map.json`` is benchpod-firmware's ``features/features.json`` (generated there from
``features/features.toml``; ``scripts/sync-feature-map.sh`` copies it here). It ships with the
SDK, so deciding what a pod can do needs no network connection.

A pod with firmware after 3.10.10 answers ``{"cmd":"status","caps":false}`` with
``features_rev`` (the table revision it is built from), ``fpga_image`` and the ``hw`` facts
instead of the ``caps`` list. :func:`resolve` turns those into the set of features the pod has:
every entry with ``rev <= features_rev`` whose gateware, board and ``requires`` facts match.
Older firmware sends the ``caps`` list as before (:meth:`Capabilities.from_status` reads it).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple


@lru_cache(maxsize=1)
def feature_map() -> Dict[str, Any]:
    """The bundled table (``schema``, ``revision``, ``facts``, ``releases``, ``features``)."""
    try:
        text = resources.files(__package__).joinpath("feature_map.json").read_text("utf-8")
        data = json.loads(text)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or data.get("schema") != 1:
        return {"schema": 1, "revision": 0, "facts": {}, "releases": [], "features": {}}
    return data


def revision() -> int:
    """The highest revision this SDK's table knows."""
    return int(feature_map().get("revision", 0) or 0)


def _version(v: Any) -> Optional[Tuple[int, int, int]]:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", str(v or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def facts_from_status(status: Mapping[str, Any]) -> Dict[str, Any]:
    """The table's facts from a ``status`` reply: the top-level fields each fact names
    (``status``), and the ``hw`` list (``hw``). ``fpga_image`` is a string, the rest booleans."""
    hw = status.get("hw")
    hw_names = {str(x).lower() for x in hw} if isinstance(hw, (list, tuple)) else set()
    out: Dict[str, Any] = {}
    for name, f in feature_map().get("facts", {}).items():
        if "status" in f and f["status"] in status:
            v = status[f["status"]]
            out[name] = v if name == "fpga_image" else v is True
        elif "hw" in f:
            out[name] = f["hw"] in hw_names
    if "analog" not in out:
        out["analog"] = True   # firmware before the analog flag: every pod was an analog board
    return out


def facts_from_frame(params: Mapping[str, Any], prefix: str = "") -> Dict[str, Any]:
    """The facts from the cloud capabilities frame (or the server's ``cap.*`` map with
    ``prefix="cap."``), where they are the fields each fact names as ``frame``."""
    out: Dict[str, Any] = {}
    for name, f in feature_map().get("facts", {}).items():
        key = prefix + f["frame"] if "frame" in f else None
        if key is None or key not in params:
            continue
        v = params[key]
        out[name] = str(v) if name == "fpga_image" else str(v).strip().lower() in ("true", "1")
    out.setdefault("analog", True)
    return out


def _requires_ok(req: str, facts: Mapping[str, Any]) -> bool:
    name, _, value = req.partition(":")
    if value:
        return str(facts.get(name, "")) == value
    return facts.get(name) is True


def _board_ok(board: str, facts: Mapping[str, Any]) -> bool:
    analog = facts.get("analog", True) is not False
    return board == "any" or (board == "analog" and analog) or (board == "digital" and not analog)


def resolve(features_rev: int, gateware: int, facts: Mapping[str, Any]) -> FrozenSet[str]:
    """The features of a pod that announced ``features_rev``, runs ``gateware`` (0 when not
    known: gateware-gated features are left out) and has ``facts``."""
    out = set()
    for name, e in feature_map().get("features", {}).items():
        if e.get("status") == "retired":
            continue
        if int(e.get("rev", 0)) > features_rev:
            continue
        if "gateware" in e and gateware < int(e["gateware"]):
            continue
        if not _board_ok(e.get("board", "any"), facts):
            continue
        if not all(_requires_ok(r, facts) for r in e.get("requires", [])):
            continue
        out.add(name)
    return frozenset(out)


def first_firmware(feature: str) -> str:
    """The first firmware release with ``feature`` (``"next"`` when not released yet, ``""`` for a
    name the table does not have): for messages like "needs firmware 3.10.8"."""
    e = feature_map().get("features", {}).get(feature)
    return str(e.get("firmware", "")) if e else ""
