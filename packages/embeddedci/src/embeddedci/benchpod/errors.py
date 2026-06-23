"""Exception hierarchy for the BenchPod client."""

from __future__ import annotations

from typing import Optional


class BenchPodError(Exception):
    """Base class for every error this package raises."""


class ConnectionConfigError(BenchPodError):
    """No usable connection was configured, or the spec could not be parsed."""


class TransportError(BenchPodError):
    """A transport-level failure: could not reach or talk to the pod."""


class CloudAuthError(BenchPodError):
    """Could not obtain a cloud session token for the ``embeddedci`` destination.

    Raised when minting the GitHub OIDC token fails (not in a GitHub Action, missing
    ``id-token: write`` permission, or the request failed) or the token exchange with the
    embeddedci server is rejected. The message explains which of these applies.
    """


class FirmwareError(BenchPodError):
    """The pod accepted the request but replied ``{"status":"error"}``."""

    def __init__(self, message: str, *, cmd: Optional[str] = None) -> None:
        self.firmware_message = message
        self.cmd = cmd
        if cmd:
            super().__init__(f"{cmd}: {message}")
        else:
            super().__init__(message)


class FlashError(BenchPodError):
    """Flashing failed — OpenOCD exited non-zero (see ``stderr``)."""


class TargetUnreachableError(FlashError):
    """OpenOCD's probe worked but the target never answered on SWD.

    Almost always means the target is unpowered, mis-wired, or held in reset.
    """
