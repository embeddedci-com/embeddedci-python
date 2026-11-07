"""Errors raised by the AI Vision client.

The board answers every failure as ``{"error": {"code", "message", "detail"?}}``; each ``code``
maps to one exception class here, so a test can catch exactly what it expects. All of them derive
from :class:`AIVisionError`.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Type


class AIVisionError(Exception):
    """Base class for every AI Vision error."""

    #: The board's error code (``bad_request``, ``busy``, ...); empty for client-side errors.
    code: str = ""

    def __init__(self, message: str, *, status: int = 0, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        #: HTTP status of the answer (0 when the request never got one).
        self.status = status
        #: The board's ``detail`` object, when it sent one.
        self.detail = detail


class TransportError(AIVisionError):
    """The board could not be reached (network error, wrong address, board off)."""


class BadRequestError(AIVisionError):
    """The board rejected the arguments (HTTP 422)."""

    code = "bad_request"


class NotCalibratedError(AIVisionError):
    """The call needs a screen calibration: run :meth:`AIVision.calibrate` first."""

    code = "not_calibrated"


class BusyError(AIVisionError):
    """The board is leased by someone else. ``detail`` has ``who`` and ``expires``."""

    code = "busy"


class WaitTimeout(AIVisionError):
    """A wait ran out of time. ``detail`` is the last evaluation (``result``, ``evidence``)."""

    code = "timeout"


class NotFoundError(AIVisionError):
    """Nothing matched. For a press, ``detail["visible_text"]`` lists what was on the screen."""

    code = "not_found"


class ActuatorError(AIVisionError):
    """The actuator failed or refused the action."""

    code = "actuator_error"


class ModelError(AIVisionError):
    """The OCR worker or a Hailo model failed."""

    code = "model_error"


class CameraError(AIVisionError):
    """The camera or the ISP failed to deliver a frame."""

    code = "camera_error"


class StorageError(AIVisionError):
    """The board has no usable SD card on ``/data``."""

    code = "storage_error"


class AuthError(AIVisionError):
    """Missing or wrong API token: pass ``token=`` or set ``AIVISION_TOKEN``."""

    code = "unauthorized"


_BY_CODE: Dict[str, Type[AIVisionError]] = {
    cls.code: cls
    for cls in (BadRequestError, NotCalibratedError, BusyError, WaitTimeout, NotFoundError,
                ActuatorError, ModelError, CameraError, StorageError, AuthError)
}


def from_answer(status: int, body: Optional[Dict[str, Any]], fallback: str) -> AIVisionError:
    """Build the exception for an error answer from the board, or from embeddedci.com when the
    board is reached over the cloud (``{"error": "<message>", "code"?}``: the device lease held by
    someone else, the board offline, a refused token)."""
    err = (body or {}).get("error") if isinstance(body, dict) else None
    if isinstance(err, str) and err:
        return _from_server(status, body or {}, err)
    if not isinstance(err, dict):
        return AIVisionError(f"HTTP {status}: {fallback}".strip(), status=status)
    code = str(err.get("code", ""))
    cls = _BY_CODE.get(code, AIVisionError)
    exc = cls(str(err.get("message", fallback)), status=status, detail=err.get("detail"))
    if cls is AIVisionError:
        exc.code = code
    return exc


def _from_server(status: int, body: Dict[str, Any], message: str) -> AIVisionError:
    """An error embeddedci.com answered for the board."""
    if status == 409 and (body.get("busy") or body.get("code") == "lease_held"):
        return BusyError(message, status=status,
                         detail={"who": body.get("holder"), "expires": body.get("expires_at"), "source": "cloud"})
    if status == 401:
        return AuthError(message, status=status)
    if status in (502, 503, 504):
        return TransportError(message, status=status)
    if status == 404:
        return NotFoundError(message, status=status)
    exc = AIVisionError(message, status=status)
    if isinstance(body.get("code"), str):
        exc.code = body["code"]
    return exc
