"""Newline-delimited JSON framing for the BenchPod TCP API.

Mirrors ``bench-pod-firmware/docs/API.md``: a request is one JSON object on a
line terminated with ``\\n``; a reply is ``{"status":"ok","data":...}`` or
``{"status":"error","message":...}``. Large replies arrive in multiple packets,
each carrying a ``"more"`` boolean; the final packet has ``"more":false``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from .errors import FirmwareError, TransportError


@dataclass
class Reply:
    """A single decoded reply packet."""

    status: str
    data: Any = None
    message: str = ""
    more: bool = False


def encode_request(req: dict) -> bytes:
    """Encode a request dict as a single newline-terminated JSON line."""
    return (json.dumps(req, separators=(",", ":")) + "\n").encode("utf-8")


def parse_reply(line: bytes) -> Reply:
    """Parse one reply line into a :class:`Reply`."""
    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        raise TransportError("empty reply from pod")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransportError(f"malformed reply from pod: {text!r}") from exc
    if not isinstance(obj, dict):
        raise TransportError(f"unexpected reply (not a JSON object): {text!r}")
    return Reply(
        status=str(obj.get("status", "")),
        data=obj.get("data"),
        message=str(obj.get("message", "")),
        more=bool(obj.get("more", False)),
    )


def raise_for_status(reply: Reply, cmd: Optional[str] = None) -> Reply:
    """Raise :class:`FirmwareError` if the reply is an error packet.

    Returns the reply unchanged on success so callers can chain.
    """
    if reply.status == "error":
        raise FirmwareError(reply.message or "unknown firmware error", cmd=cmd)
    if reply.status not in ("ok", "chunk"):
        raise TransportError(f"unexpected reply status {reply.status!r}")
    return reply
