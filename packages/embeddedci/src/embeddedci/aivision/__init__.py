"""Client for the EmbeddedCI AI Vision board (ai-vision-2).

The board watches a device's screen through a camera, reads text, finds buttons and screens, and
presses them through an actuator. This package talks to its HTTP API with the standard library::

    from embeddedci.aivision import AIVision

    with AIVision("ai-vision-2-5dcec6.local") as v:
        v.wait_for("Home", timeout=20)
        v.press("Radio", verify={"text": "FM"})
"""

from __future__ import annotations

from .client import ENV_CONNECTION, ENV_TOKEN, MAX_VERIFY_S, MAX_WAIT_REQUEST_S, AIVision, resolve_connection
from .discovery import DiscoveredBoard, discover
from .errors import (ActuatorError, AIVisionError, AuthError, BadRequestError, BusyError, CameraError,
                     ModelError, NotCalibratedError, NotFoundError, StorageError, TransportError, WaitTimeout)
from .results import (Box, Calibration, CheckItem, CheckResult, Comparison, Element, Health, Identification,
                      Image, Lease, OCRResult, PressResult, Snapshot, TextBlock, WaitResult)

__all__ = [
    "AIVision", "resolve_connection", "discover", "DiscoveredBoard",
    "ENV_CONNECTION", "ENV_TOKEN", "MAX_WAIT_REQUEST_S", "MAX_VERIFY_S",
    "AIVisionError", "TransportError", "BadRequestError", "NotCalibratedError", "BusyError", "WaitTimeout",
    "NotFoundError", "ActuatorError", "ModelError", "CameraError", "StorageError", "AuthError",
    "Box", "Element", "TextBlock", "OCRResult", "Health", "Comparison", "Identification", "WaitResult",
    "CheckItem", "CheckResult", "PressResult", "Calibration", "Snapshot", "Image", "Lease",
]
