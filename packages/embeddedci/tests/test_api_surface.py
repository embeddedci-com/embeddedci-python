"""Freeze the public API: every exported name and every public signature.

The stable surface is ``embeddedci.benchpod.__all__``. This test renders it — names, method and
function signatures, dataclass fields, enum members — and compares against ``api_surface.json``.
Any change fails here on purpose: decide whether it is compatible (additions are; removals,
renames and signature changes are not within a major version), then refresh the snapshot with::

    UPDATE_API_SURFACE=1 pytest tests/test_api_surface.py
"""

from __future__ import annotations

import dataclasses
import enum
import inspect
import json
import os
from pathlib import Path
from typing import Any, Dict

from embeddedci import benchpod

SNAPSHOT = Path(__file__).with_name("api_surface.json")


def _own(obj: Any, cls: type) -> bool:
    return getattr(obj, "__module__", "").startswith("embeddedci") or \
        getattr(obj, "__qualname__", "").startswith(cls.__name__ + ".")


def _describe_class(cls: type) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if issubclass(cls, enum.Enum):
        out["__members__"] = {m.name: m.value for m in cls}  # type: ignore[attr-defined]
        return out
    if dataclasses.is_dataclass(cls):
        out["__fields__"] = [f.name for f in dataclasses.fields(cls)]
    for name, member in sorted(vars(cls).items()):
        if name.startswith("_") and name != "__init__":
            continue
        if name == "__init__" and dataclasses.is_dataclass(cls):
            continue
        if isinstance(member, (staticmethod, classmethod)):
            member = member.__func__
        if isinstance(member, property):
            out[name] = "property"
        elif inspect.isfunction(member) and _own(member, cls):
            out[name] = str(inspect.signature(member))
    return out


def render_surface() -> Dict[str, Any]:
    surface: Dict[str, Any] = {"__all__": sorted(benchpod.__all__)}
    for name in sorted(benchpod.__all__):
        obj = getattr(benchpod, name)
        if inspect.isclass(obj):
            surface[name] = _describe_class(obj)
        elif inspect.isfunction(obj):
            surface[name] = str(inspect.signature(obj))
        elif inspect.ismodule(obj):
            surface[name] = "module"
        elif isinstance(obj, enum.Enum):
            surface[name] = f"{type(obj).__name__}.{obj.name}"
        elif isinstance(obj, (int, str, float)):
            surface[name] = obj
        else:  # typing aliases (Literal[...])
            surface[name] = repr(obj).replace("typing.", "")
    return surface


def test_public_api_matches_snapshot():
    current = render_surface()
    if os.environ.get("UPDATE_API_SURFACE"):
        SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    expected = json.loads(SNAPSHOT.read_text())
    changed = sorted(k for k in set(current) | set(expected) if current.get(k) != expected.get(k))
    assert not changed, (
        "the public API changed for: " + ", ".join(changed)
        + "\nIf intentional (and semver-compatible), refresh with UPDATE_API_SURFACE=1."
    )
