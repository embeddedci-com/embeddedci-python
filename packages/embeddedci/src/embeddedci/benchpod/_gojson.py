"""Dataclasses to dicts in the shape the server's Go decoders put on the wire (``omitempty``)."""

from __future__ import annotations

import dataclasses
from typing import Any, Iterable


def go_dict(obj: Any, omit_empty: Iterable[str] = (), omit_none: Iterable[str] = ()) -> dict:
    """``obj``'s public fields; ``omit_empty`` fields are left out when empty or zero (Go's
    ``omitempty`` on a value), ``omit_none`` ones when ``None`` (``omitempty`` on a pointer)."""
    out = {}
    empty, none = set(omit_empty), set(omit_none)
    for f in dataclasses.fields(obj):
        if f.name.startswith("_"):
            continue
        v = getattr(obj, f.name)
        if f.name in none and v is None:
            continue
        if f.name in empty and (v is None or v == "" or v == 0 or v == [] or v is False):
            continue
        if dataclasses.is_dataclass(v):
            v = v.as_dict() if hasattr(v, "as_dict") else dataclasses.asdict(v)
        elif isinstance(v, list):
            v = [x.as_dict() if hasattr(x, "as_dict") else (dataclasses.asdict(x) if dataclasses.is_dataclass(x) else x)
                 for x in v]
        out[f.name] = v
    return out
