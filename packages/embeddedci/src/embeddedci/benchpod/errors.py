"""Exception hierarchy for the BenchPod client."""

from __future__ import annotations

import re
from typing import Optional


class BenchPodError(Exception):
    """Base class for every error this package raises."""


class ConnectionConfigError(BenchPodError):
    """No usable connection was configured, or the spec could not be parsed."""


class TransportError(BenchPodError):
    """A transport-level failure: could not reach or talk to the pod."""


class DeviceBusyError(BenchPodError):
    """The shared cloud BenchPod is held by another consumer and did not free within the wait
    timeout. Raised when acquiring the device lease times out."""


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


class UartTimeout(BenchPodError):
    """An event-based UART read (``UartSession.expect``) timed out.

    Carries the text accumulated so far in :attr:`text` so the caller can see
    what the DUT actually sent.
    """

    def __init__(self, message: str, *, text: str = "") -> None:
        self.text = text
        super().__init__(message)


class CanTimeout(BenchPodError):
    """A :meth:`CanBus.expect` waited for a matching CAN frame but none arrived.

    Carries the frames seen so far in :attr:`frames` so the caller can inspect
    what actually landed on the bus.
    """

    def __init__(self, message: str, *, frames=None) -> None:
        self.frames = list(frames or [])
        super().__init__(message)


class PinConflictError(FirmwareError):
    """The pod refused because an LA channel is already used by another function.

    ``la`` is the channel and ``function`` its owner (``gpio``, ``uart_tx``, ``i2c_sda``, ``swd_clk``,
    ``step``, …). The message says how to free it — e.g. release a GPIO pin with
    :meth:`BenchPod.release_gpio` before starting a UART session on it.
    """

    def __init__(self, message: str, *, cmd: Optional[str] = None, la: int = 0,
                 function: str = "") -> None:
        super().__init__(message, cmd=cmd)
        self.la = la
        self.function = function


class PullConflictError(FirmwareError):
    """A bias resistor and a channel's function can't be combined — e.g. LA7's pull-down under a UART
    line or an open-drain output. ``la`` is the channel; the message says what to disable."""

    def __init__(self, message: str, *, cmd: Optional[str] = None, la: int = 0) -> None:
        super().__init__(message, cmd=cmd)
        self.la = la


class TriggerTimeout(FirmwareError):
    """A triggered capture's condition (``edge`` on LA ``la``) never happened within its timeout."""

    def __init__(self, message: str, *, cmd: Optional[str] = None, la: int = 0,
                 edge: str = "") -> None:
        super().__init__(message, cmd=cmd)
        self.la = la
        self.edge = edge


_PIN_CONFLICT = re.compile(r"pin conflict: LA(\d+) is in use by (\w+)")
_PULL_CONFLICT = re.compile(r"pull conflict: LA(\d+)")
_TRIGGER_TIMEOUT = re.compile(r"trigger timeout: no (\w+) \w+ on LA(\d+)")


def classify_firmware_error(exc: FirmwareError) -> FirmwareError:
    """The specific :class:`FirmwareError` subclass for a pod refusal, or ``exc`` itself."""
    if type(exc) is not FirmwareError:
        return exc
    msg = exc.firmware_message
    m = _PIN_CONFLICT.search(msg)
    if m:
        return PinConflictError(msg, cmd=exc.cmd, la=int(m.group(1)), function=m.group(2))
    m = _PULL_CONFLICT.search(msg)
    if m:
        return PullConflictError(msg, cmd=exc.cmd, la=int(m.group(1)))
    m = _TRIGGER_TIMEOUT.search(msg)
    if m:
        return TriggerTimeout(msg, cmd=exc.cmd, la=int(m.group(2)), edge=m.group(1))
    return exc
