"""Emulated GPS receiver helpers.

Thin wrappers over the pod's ``gps_*`` JSON commands: the pod prints NMEA 0183 sentences (RMC,
VTG, GGA, GSA, GSV, GLL) to the DUT's UART RX on one LA channel, like a u-blox module, from
UART2, the gateware's second, transmit-only UART (v48+), so the UART proxy stays free for the
DUT's console. See ``benchpod-firmware/docs/API.md`` ("Emulated GPS receiver").
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, Iterable, Optional, Union

from .constants import GPS_SENTENCES
from .errors import BenchPodError
from .sensor import _require_command

#: The fix fields ``gps_set`` takes (and ``gps_start`` accepts too).
FIX_FIELDS = ("latitude_deg", "longitude_deg", "altitude_m", "speed_kmh", "course_deg",
              "satellites", "hdop", "fix")

UtcLike = Union[_dt.datetime, str, int, float]


def utc_field(utc: UtcLike) -> Union[str, int]:
    """``utc`` as the pod takes it: an aware/naive-UTC datetime or an ISO string -> ISO 8601,
    a number -> whole seconds since 1970."""
    if isinstance(utc, _dt.datetime):
        if utc.tzinfo is not None:
            utc = utc.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(utc, (int, float)):
        return int(utc)
    return str(utc)


def _fix_fields(fix: Dict[str, Any], utc: Optional[UtcLike]) -> Dict[str, Any]:
    req: Dict[str, Any] = {}
    for key, value in fix.items():
        if key not in FIX_FIELDS:
            raise ValueError(f"unknown GPS field {key!r}; use one of {', '.join(FIX_FIELDS)}, utc")
        if value is None:
            continue
        req[key] = int(value) if key in ("satellites", "fix") else float(value)
    if utc is not None:
        req["utc"] = utc_field(utc)
    return req


def sentence_list(sentences: Union[str, Iterable[str]]) -> str:
    names = [s.strip().upper() for s in (sentences.split(",") if isinstance(sentences, str) else sentences)]
    names = [n for n in names if n]
    bad = [n for n in names if n not in GPS_SENTENCES]
    if bad or not names:
        raise ValueError(f"sentences must be some of {', '.join(GPS_SENTENCES)}, got {sentences!r}")
    return ",".join(names)


def gps_start(transport, *, tx: int, baud: int = 9600, rate_hz: int = 1,
              sentences: Union[str, Iterable[str], None] = None,
              utc: Optional[UtcLike] = None, **fix: Any) -> Dict[str, Any]:
    """Start the receiver on LA channel ``tx`` (the DUT's RX). Returns the ``gps_status`` object."""
    req: Dict[str, Any] = {"cmd": "gps_start", "tx": int(tx), "baud": int(baud), "rate_hz": int(rate_hz)}
    if sentences is not None:
        req["sentences"] = sentence_list(sentences)
    req.update(_fix_fields(fix, utc))
    return _require_command(transport)(req)


def gps_set(transport, *, utc: Optional[UtcLike] = None, **fix: Any) -> Dict[str, Any]:
    """Change the fix (any subset of :data:`FIX_FIELDS` and ``utc``). Returns ``gps_status``."""
    req = {"cmd": "gps_set", **_fix_fields(fix, utc)}
    if len(req) == 1:
        raise BenchPodError("gps_set needs at least one field: " + ", ".join(FIX_FIELDS) + ", utc")
    return _require_command(transport)(req)


def gps_stop(transport) -> None:
    """Stop the receiver and release its pin (safe when none runs)."""
    _require_command(transport)({"cmd": "gps_stop"})


def gps_status(transport) -> Dict[str, Any]:
    """The session (tx, baud, rate, sentences, epochs, overruns) and the fix it prints."""
    return _require_command(transport)({"cmd": "gps_status"})
