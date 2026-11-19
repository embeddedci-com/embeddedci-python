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

from .errors import TransportError, firmware_error


@dataclass
class Reply:
    """A single decoded reply packet."""

    status: str
    data: Any = None
    message: str = ""
    more: bool = False
    #: The stable error code and its values of an error reply (firmware with error codes).
    code: str = ""
    args: Any = None


#: The pod takes a ``client`` of at most this many printable characters (and drops a longer one).
CLIENT_NAME_MAX = 32
_client_name: Optional[str] = None


def client_name() -> str:
    """The ``client`` every request carries: ``"sdk <version>"`` unless :func:`set_client_name`
    set another (the MCP server sends ``"mcp <version>"``)."""
    global _client_name
    if _client_name is None:
        try:
            from importlib.metadata import version
            _client_name = f"sdk {version('embeddedci')}"
        except Exception:   # a source tree without install
            _client_name = "sdk"
    return _client_name


def set_client_name(name: str) -> None:
    """Name this process's requests (``"mcp 3.0.6"``): the pod puts it in a refusal's text. A name
    longer than :data:`CLIENT_NAME_MAX` or with other than printable ASCII is not sent."""
    global _client_name
    _client_name = str(name)


def stamp_request(req: dict) -> dict:
    """``req`` with this client's ``client_api`` (:data:`~embeddedci.benchpod.feature_map.CLIENT_API`)
    and ``client`` name, the two fields every JSON command carries. Firmware 3.10.13 and older
    ignores them; later firmware refuses a ``client_api`` below its ``min_client_api`` (BP0030).
    A request that already has them keeps its own."""
    from .feature_map import CLIENT_API

    out = dict(req)
    out.setdefault("client_api", CLIENT_API)
    name = client_name()
    if name and len(name) <= CLIENT_NAME_MAX and all(" " <= c <= "~" for c in name):
        out.setdefault("client", name)
    return out


def encode_request(req: dict) -> bytes:
    """Encode a request dict as a single newline-terminated JSON line, with this client's
    ``client_api`` and ``client`` (:func:`stamp_request`)."""
    return (json.dumps(stamp_request(req), separators=(",", ":")) + "\n").encode("utf-8")


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
        code=str(obj.get("code") or ""),
        args=obj.get("args") if isinstance(obj.get("args"), dict) else None,
    )


def raise_for_status(reply: Reply, cmd: Optional[str] = None) -> Reply:
    """Raise :class:`FirmwareError` (its specific subclass, see
    :func:`~embeddedci.benchpod.errors.classify_firmware_error`) if the reply is an error packet.

    Returns the reply unchanged on success so callers can chain.
    """
    if reply.status == "error":
        raise firmware_error(reply.message or "unknown firmware error", cmd=cmd, code=reply.code,
                             values=reply.args)
    if reply.status not in ("ok", "chunk"):
        raise TransportError(f"unexpected reply status {reply.status!r}")
    return reply
