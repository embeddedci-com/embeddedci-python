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


class TransportTimeout(TransportError, TimeoutError):
    """The pod (or the cloud tunnel) did not answer within the timeout.

    Distinct from :class:`ConnectionClosedError`: the link is still up, the reply just did not
    come in time. Also a :class:`TimeoutError`, which the raw socket timeout used to be.
    """


class ConnectionClosedError(TransportError):
    """The connection ended (EOF, reset or a closed tunnel) before the full reply arrived."""


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
    """The pod accepted the request but replied ``{"status":"error"}``.

    :attr:`firmware_message` is the pod's own text, unchanged. The exception's message is that
    text with any fix the firmware spells as a raw protocol command (``{"cmd":"gpio",...}``)
    rewritten into the SDK call that does it (``bp.release_gpio(4)``); the leading words the
    firmware uses (``la voltage not set``, ``pin conflict: LA4 is in use by gpio``) stay as they are.
    """

    def __init__(self, message: str, *, cmd: Optional[str] = None) -> None:
        self.firmware_message = message
        self.cmd = cmd
        text = _sdk_refusal_text(message)
        if cmd:
            super().__init__(f"{cmd}: {text}")
        else:
            super().__init__(text)


class UnsupportedFeatureError(FirmwareError):
    """This pod cannot do what was asked: its firmware or gateware lacks the feature, or the board
    lacks the hardware (the digital-only board has no analog front end).

    Raised before anything is sent when the pod's :attr:`~embeddedci.benchpod.BenchPod.capabilities`
    lack the feature, and for the pod's own answer when it does not know a command
    (``unknown cmd``) or has no analog front end. :attr:`feature` names the capability flag (or the
    command) that is missing, ``""`` when unknown; :attr:`firmware_version` is the pod's firmware
    when known. Check ahead with the ``can_*`` properties or :meth:`BenchPod.supports
    <embeddedci.benchpod.BenchPod.supports>`.

    A :class:`FirmwareError` (and so a :class:`BenchPodError`), which these errors were before.
    """

    def __init__(self, message: str, *, cmd: Optional[str] = None, feature: str = "",
                 firmware_version: str = "") -> None:
        super().__init__(message, cmd=cmd)
        self.feature = feature
        self.firmware_version = firmware_version


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


class UartLinkError(UartTimeout, ConnectionClosedError):
    """The UART proxy link ended with an error (the connection dropped, the pod went away)
    while :class:`~embeddedci.benchpod.uart.UartSession` was waiting for output.

    A :class:`UartTimeout` too, so code written for the timeout still catches it; :attr:`text`
    has everything received before the link died and :attr:`cause` the error that ended it.
    """

    def __init__(self, message: str, *, text: str = "", cause: Optional[BaseException] = None) -> None:
        super().__init__(message, text=text)
        self.cause = cause


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
    ``step``, …). The message names the SDK call that frees it (``release it with
    bp.release_gpio(4)``, ``bp.disable_i2c_sensor()``, ``bp.disable_gps()``, close the UART or SPI
    session); :attr:`firmware_message` keeps the pod's own text.
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


class PodLockedError(FirmwareError):
    """The pod refused a command on its LAN connection: its LAN policy is ``locked`` (or the
    command changes a policy), so the command needs the cloud (``embeddedci:<device>``) or the
    USB console. Firmware text: ``locked: <cmd> needs the cloud or the USB console``."""


class PodBusyError(FirmwareError):
    """The pod refused because it is busy (``busy: …``): a capture or upload is running, or a
    cloud job holds it (:class:`PodLeasedError`)."""


class PodLeasedError(PodBusyError, DeviceBusyError, TransportError):
    """A cloud job holds the pod, so this client may look but not touch.

    Raised for the pod's own refusal on the LAN (``busy: a cloud job holds this pod (<holder>,
    <n> s left)``) and for the server's HTTP 409 when another consumer holds the device lease.
    :attr:`holder` names the job (``""`` when unknown), :attr:`left_s` the seconds its lease has
    left (``None`` when unknown) and :attr:`expires_at` the server's expiry time, if it sent one.

    Also a :class:`FirmwareError`, :class:`DeviceBusyError` and :class:`TransportError`, which
    these refusals were before, so existing handlers keep working.
    """

    def __init__(self, message: str, *, cmd: Optional[str] = None, holder: str = "",
                 left_s: Optional[int] = None, expires_at: str = "",
                 status: Optional[int] = None) -> None:
        super().__init__(message, cmd=cmd)
        self.holder = holder
        self.left_s = left_s
        self.expires_at = expires_at
        self.status = status


class PermissionDeniedError(FirmwareError, TransportError):
    """The caller is not allowed to do this: the pod's ``forbidden: <cmd> needs an organization
    owner or admin`` on a cloud tunnel, or an HTTP 403 from the server (the API key lacks a
    scope, the session token does not cover the device, the user is not an owner or admin).

    :attr:`status` is the HTTP status (403) when the server refused, else ``None``. Also a
    :class:`FirmwareError` and :class:`TransportError`, which these refusals were before.
    """

    def __init__(self, message: str, *, cmd: Optional[str] = None,
                 status: Optional[int] = None) -> None:
        super().__init__(message, cmd=cmd)
        self.status = status


_PIN_CONFLICT = re.compile(r"pin conflict: LA(\d+) is in use by (\w+)")
_PULL_CONFLICT = re.compile(r"pull conflict: LA(\d+)")
_TRIGGER_TIMEOUT = re.compile(r"trigger timeout: no (\w+) \w+ on LA(\d+)")
#: "busy: a cloud job holds this pod (<holder>, <n> s left)" (command_handler.c dispatch_line).
_LEASED = re.compile(r"busy: a cloud job holds this pod \((.*), (\d+) s left\)")
#: Policy changes the LAN may not make (pod_policy.c).
_POLICY_ELSEWHERE = ("change it from the cloud or the USB console",
                     "only the USB console can loosen")
#: Older firmware's answer to a command it does not have (command_handler.c).
_UNKNOWN_CMD = "unknown cmd"
#: The digital-only board refusing an analog command (cmd_gate.c cmd_gate_check).
_NO_ANALOG = "this BenchPod has no analog front end"


#: The firmware's "la voltage not set; set it with la_voltage (mv 1800 or 3300) first"
#: (command_handler.c require_la_voltage). Matched on the leading words only.
_LA_VOLTAGE_UNSET = "la voltage not set"
_LA_VOLTAGE_SDK = ("la voltage not set; set the board's I/O voltage first: "
                   "BenchPod(conn, la_voltage=3.3) (1.8 for a 1.8 V target), the "
                   "benchpod_la_voltage fixture in conftest.py, or bp.set_la_voltage(3.3)")

#: Fixes the firmware spells as raw protocol commands (la_pins.c release_hint and the pull and
#: gpio-output checks, command_handler_dap.c, command_handler_dac.c), in the caller's SDK terms.
#: Whole phrases first, so a hint reads naturally; anything not listed passes through unchanged.
_SDK_FIXES = (
    (re.compile(r'release it with \{"cmd":"gpio","la":(\d+),"mode":"off"\}'),
     r"release it with bp.release_gpio(\1)"),
    (re.compile(r"stop the uart proxy first"),
     "close the UART session from bp.open_uart() first (session.close(), or leave its with block)"),
    (re.compile(r"end the SWD session first"),
     "wait for the running flash (its SWD session) to finish"),
    (re.compile(r'stop the sensor emulation first \(\{"cmd":"sensor_stop"\}\)'),
     "stop the sensor emulation first with bp.disable_i2c_sensor()"),
    (re.compile(r'stop the SPI session first \(\{"cmd":"spi_stop"\}\)'),
     "close the SPI session from bp.open_spi() first (spi.close(), or leave its with block)"),
    (re.compile(r'stop the GPS receiver first \(\{"cmd":"gps_stop"\}\)'),
     "stop the GPS emulation first with bp.disable_gps()"),
    (re.compile(r'\(\{"cmd":"spi_stop"\}\)'),
     "(close the SPI session from bp.open_spi() first)"),
    (re.compile(r'\{"cmd":"la","la":(\d+),"pullup":"off"\}'), r"bp.set_pull(\1, False)"),
    (re.compile(r'\{"cmd":"gpio","la":(\d+),"mode":"output"\}'), r'bp.gpio(\1, "output")'),
    (re.compile(r'\{"cmd":"fpga_image","image":0\}'), "bp.fpga_image(0)"),
    (re.compile(r'\{"cmd":"dac_loop_input"\}'), "bp.loop_input()"),
)


def _sdk_refusal_text(message: str) -> str:
    """``message`` (a pod refusal) with its protocol-level fix rewritten into the SDK call.

    ``"pin conflict: LA4 is in use by gpio; release it with {"cmd":"gpio","la":4,"mode":"off"}"``
    becomes ``"pin conflict: LA4 is in use by gpio; release it with bp.release_gpio(4)"``. Text
    with nothing to rewrite comes back unchanged.
    """
    if message.startswith(_LA_VOLTAGE_UNSET):
        return _LA_VOLTAGE_SDK
    for pattern, repl in _SDK_FIXES:
        message = pattern.sub(repl, message)
    return message


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
    if msg.startswith("locked:") or any(p in msg for p in _POLICY_ELSEWHERE):
        return PodLockedError(msg, cmd=exc.cmd)
    m = _LEASED.search(msg)
    if m:
        return PodLeasedError(msg, cmd=exc.cmd, holder=m.group(1), left_s=int(m.group(2)))
    if msg.startswith("busy:"):
        return PodBusyError(msg, cmd=exc.cmd)
    if msg.startswith("forbidden:"):
        return PermissionDeniedError(msg, cmd=exc.cmd)
    if msg.strip() == _UNKNOWN_CMD:
        return UnsupportedFeatureError(msg, cmd=exc.cmd, feature=exc.cmd or "")
    if msg.startswith(_NO_ANALOG):
        return UnsupportedFeatureError(msg, cmd=exc.cmd, feature="analog")
    return exc


def firmware_error(message: str, *, cmd: Optional[str] = None) -> FirmwareError:
    """The :class:`FirmwareError` (its specific subclass when there is one) for a pod refusal."""
    return classify_firmware_error(FirmwareError(message, cmd=cmd))
