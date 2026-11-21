"""Exception hierarchy for the BenchPod client."""

from __future__ import annotations

import json
import re
from importlib import resources
from typing import Any, Callable, Dict, Mapping, Optional, Tuple


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


class TlsPinMismatchError(TransportError):
    """The pod presented a TLS certificate other than the one pinned for it (LAN TLS, firmware
    3.9.3+). Either its certificate was replaced or regenerated, or something is intercepting the
    connection. :attr:`addr` is the pod's TLS address, :attr:`pinned` and :attr:`got` the two
    SHA-256 fingerprints. Trust the new one with :func:`embeddedci.benchpod.trust_lan_tls` or
    ``benchpod lan-tls trust``."""

    def __init__(self, addr: str, pinned: str, got: str) -> None:
        super().__init__(
            f"the pod at {addr} presented TLS certificate sha256 {got[:16]}..., but "
            f"{pinned[:16]}... is pinned for it. If its certificate was replaced or regenerated, "
            f"trust the new one (`benchpod lan-tls trust`); otherwise something may be "
            f"intercepting the connection")
        self.addr = addr
        self.pinned = pinned
        self.got = got


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

    A refusal the SDK can act on carries a stable :attr:`code` (``"BP0009"``) and its
    :attr:`values`, and no text (firmware 4.0): the exception's message is the code's message and
    the SDK's fix from the bundled catalog (:func:`error_catalog`). A refusal without a code keeps
    the pod's own text. :attr:`firmware_message` is the text the pod sent (``""`` for a coded
    refusal, except ``BP0030``).
    """

    #: The pod's stable error code (``"BP0009"``), "" for a refusal without one. Match on this,
    #: never on the message text. https://embeddedci.com/docs/errors lists every code.
    code: str = ""
    #: The values sent with the code (``{"name": "k66", "rev": 2}``); ``args`` is Python's own.
    values: Dict[str, Any] = {}
    #: The code's docs page, "" without a code.
    docs_url: str = ""

    def __init__(self, message: str, *, cmd: Optional[str] = None) -> None:
        self.firmware_message = message
        self.cmd = cmd
        self.values = {}
        super().__init__(f"{cmd}: {message}" if cmd else message)


class UnsupportedFeatureError(FirmwareError):
    """This pod cannot do what was asked: its firmware or gateware lacks the feature, or the board
    lacks the hardware (the digital-only board has no analog front end).

    Raised before anything is sent when the pod's :attr:`~embeddedci.benchpod.BenchPod.capabilities`
    lack the feature, and for the pod's own refusals: a command it does not know (``BP0002``),
    gateware too old (``BP0017``), no analog front end (``BP0046``), no module link (``BP0056``),
    the gateware image lacks it (``BP0058``, ``BP0059``), an update target it does not know
    (``BP0060``). :attr:`feature` names the capability flag (or the
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

    ``BP0031``: ``la`` is the channel, ``function`` the function that holds it (``gpio``,
    ``uart_tx``, ``i2c_sda``, ``swd_clk``, ``step``, ...), ``owner`` who holds it (``gpio``, or
    ``sensor:<instance> (<function>)``) and ``instance`` the emulated sensor's name (``""`` for
    none). The message names the SDK call that frees it.
    """

    def __init__(self, message: str, *, cmd: Optional[str] = None, la: int = 0,
                 function: str = "", owner: str = "", instance: str = "") -> None:
        super().__init__(message, cmd=cmd)
        self.la = la
        self.function = function
        self.owner = owner
        self.instance = instance


class PullConflictError(FirmwareError):
    """A bias resistor and a channel's function can't be combined, e.g. LA7's pull-down under a
    UART line or an open-drain output (``BP0032``, ``BP0033``). ``la`` is the channel; the message
    says what to disable."""

    def __init__(self, message: str, *, cmd: Optional[str] = None, la: int = 0) -> None:
        super().__init__(message, cmd=cmd)
        self.la = la


class PullDirectionError(FirmwareError, ValueError):
    """The channel can't pull the way asked: it has no pull resistor (``BP0023``), or its resistor
    pulls the other way on this board (``BP0024``, "LA7 supports pull-down only on this board").
    ``la`` is the channel, ``want`` the direction asked for, ``have`` the one it supports ("" when
    it has none). Also a :class:`ValueError`, which the SDK raised for this before the pod did."""

    def __init__(self, message: str, *, cmd: Optional[str] = None, la: int = 0, want: str = "",
                 have: str = "") -> None:
        super().__init__(message, cmd=cmd)
        self.la = la
        self.want = want
        self.have = have


class DirectionConflictError(FirmwareError):
    """The pod refused to drive a pin that :meth:`BenchPod.la_dir
    <embeddedci.benchpod.BenchPod.la_dir>` marks as wired to a DUT output (a push-pull GPIO output,
    UART TX, SPI SCK / MOSI / CS, SWD, ...), because two push-pull outputs would drive against each
    other (``BP0034``), or refused to mark a driven pin as a DUT output (``BP0035``). ``la`` is the
    channel. Use an ``open_drain`` GPIO, or fix the wiring if the pin is not a DUT output. There is
    no override."""

    def __init__(self, message: str, *, cmd: Optional[str] = None, la: int = 0) -> None:
        super().__init__(message, cmd=cmd)
        self.la = la


class TriggerTimeout(FirmwareError):
    """A triggered capture's condition (``edge`` on LA ``la``) never happened within its timeout
    (``BP0038``; ``BP0039`` for an emulated sensor's bus capture, ``edge`` ``"start"``)."""

    def __init__(self, message: str, *, cmd: Optional[str] = None, la: int = 0,
                 edge: str = "") -> None:
        super().__init__(message, cmd=cmd)
        self.la = la
        self.edge = edge


class PodLockedError(FirmwareError):
    """The pod refused a command on its LAN connection: its LAN policy is ``locked`` (``BP0040``)
    or the command changes a setting the LAN may not (``BP0041`` to ``BP0043``), so it needs the
    cloud (``embeddedci:<device>``) or USB; or plaintext 8080 is off on the LAN (``BP0075``: use
    TLS on 8443, the SDK's default)."""


class PodNotPairedError(PodLockedError):
    """This computer is not paired with the pod (``BP0081``, firmware 4.0.1+): over Ethernet and
    Wi-Fi it may only read the pod's status. Pair it once over the pod's USB cable with
    :meth:`~embeddedci.benchpod.BenchPod.pair` (or ``benchpod setup``), or use the cloud
    (``embeddedci:<device>``). A :class:`PodLockedError`, so code that handles the LAN policy
    handles this too."""


class PodBusyError(FirmwareError):
    """The pod refused because it is busy: an I2C, SPI, UART or SWD session is already open
    (``BP0003``, ``BP0004``, ``BP0049``, ``BP0050``), the engine that runs SWD, SPI and I2C is in
    use (``BP0005``), the storage or its bus is busy (``BP0016``, ``BP0048``), another long
    command or an update runs (``BP0047``, ``BP0051``), the previous cloud command still runs
    (``BP0052``), a step train runs (``BP0066``), or a cloud job holds the pod
    (:class:`PodLeasedError`)."""


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
                 status: Optional[int] = None, purpose: str = "") -> None:
        super().__init__(message, cmd=cmd)
        self.holder = holder
        self.left_s = left_s
        self.expires_at = expires_at
        self.status = status
        self.purpose = purpose


class PodLeaseLostError(FirmwareError):
    """The pod's lease this session held was stopped from the web app or the cloud (``BP0026``):
    the command was not run. :attr:`by` names who stopped it. The pod says so once; later
    commands run without a lease."""

    def __init__(self, message: str, *, cmd: Optional[str] = None, by: str = "") -> None:
        super().__init__(message, cmd=cmd)
        self.by = by


class WiringError(FirmwareError):
    """The pod refused a wiring-profile operation (``BP0007`` to ``BP0015``): no profile with that
    name, a bad name, a revision conflict, the store is full, the profile is too large or not one
    JSON object, the default cannot be deleted, an upload out of order, or the storage failed.
    :attr:`name` is the profile's name when the pod sent one (``""`` otherwise); :attr:`code` says
    which of these it is and :attr:`values` has the rest (``rev``, ``max``, ``bytes``)."""

    def __init__(self, message: str, *, cmd: Optional[str] = None, name: str = "") -> None:
        super().__init__(message, cmd=cmd)
        self.name = name


class ClientTooOldError(FirmwareError):
    """This SDK is too old for the pod's firmware: the firmware changed a reply or a message in a
    way this SDK would misread, so it is not used with it.

    Raised on connect when the pod announces a client API level (``api``) above
    :data:`~embeddedci.benchpod.feature_map.CLIENT_API`, the highest this SDK handles, and for the
    pod's own refusal ``BP0030`` (the SDK's level is below the firmware's ``min_client_api``).
    :attr:`client_api` is this SDK's level, :attr:`api` the pod's (0 when unknown),
    :attr:`min_client_api` the lowest the pod takes (0 when unknown) and :attr:`firmware_version`
    the pod's firmware when known. The fix is to update the SDK: ``pip install -U embeddedci``.

    Not an :class:`UnsupportedFeatureError`: the pod can do it, this client cannot talk to it. A
    :class:`FirmwareError` (and so a :class:`BenchPodError`).
    """

    def __init__(self, message: str, *, cmd: Optional[str] = None, client_api: int = 0,
                 api: int = 0, min_client_api: int = 0, firmware_version: str = "") -> None:
        super().__init__(message, cmd=cmd)
        self.client_api = client_api
        self.api = api
        self.min_client_api = min_client_api
        self.firmware_version = firmware_version


class FirmwareTooOldError(UnsupportedFeatureError):
    """The pod runs firmware this SDK does not drive: firmware 3.x, which speaks client API 1.
    This SDK (4.x) needs firmware 4.0.0 or later (client API 2).

    Raised on connect. :attr:`api` is the pod's client API level (1 for firmware that does not
    announce one), :attr:`firmware_version` its firmware. Update the pod with the BenchPod CLI
    (``benchpod flash-self``) or the firmware update in the web app; or keep firmware 3.x and
    use embeddedci 3.x (``pip install 'embeddedci<4'``). An :class:`UnsupportedFeatureError`
    (feature ``"client_api"``)."""

    def __init__(self, message: str, *, cmd: Optional[str] = None, api: int = 0,
                 firmware_version: str = "") -> None:
        super().__init__(message, cmd=cmd, feature="client_api", firmware_version=firmware_version)
        self.api = api


class PermissionDeniedError(FirmwareError, TransportError):
    """The caller is not allowed to do this: the pod's ``BP0045`` (the command needs an
    organization owner or admin) on a cloud tunnel, or an HTTP 403 from the server (the API key lacks a
    scope, the session token does not cover the device, the user is not an owner or admin).

    :attr:`status` is the HTTP status (403) when the server refused, else ``None``. Also a
    :class:`FirmwareError` and :class:`TransportError`, which these refusals were before.
    """

    def __init__(self, message: str, *, cmd: Optional[str] = None,
                 status: Optional[int] = None) -> None:
        super().__init__(message, cmd=cmd)
        self.status = status


class ReplyTooLargeError(FirmwareError):
    """The pod's reply did not fit its reply buffer (``BP0053``) or the cloud channel
    (``BP0054``, with ``bytes`` and ``limit``). The SDK reads such lists in parts where it can
    (``la_pins``)."""


class DacPinoutMismatchError(UnsupportedFeatureError):
    """The DAC is off because the digital board and the analog module use different flat-cable
    pinouts (``BP0080``): ``digital`` is the digital board's (``"v3r4+"`` or ``"v3r3"``),
    ``module`` the module's revision from its EEPROM (``"3.4"``, ``"not programmed"``, ...).
    Every command that drives the DAC is refused; the rest works. ``ModuleInfo.dac_pinout_ok``
    says the same ahead. An :class:`UnsupportedFeatureError` (``feature="dac"``)."""

    def __init__(self, message: str, *, cmd: Optional[str] = None, digital: str = "",
                 module: str = "") -> None:
        super().__init__(message, cmd=cmd, feature="dac")
        self.digital = digital
        self.module = module


# -- stable error codes ------------------------------------------------------------------------
#
# Firmware 4.0 sends {"status":"error","code":"BP0009","args":{...}} and no message (BP0030 alone
# carries one, for clients from before the codes). error_catalog.json is benchpod-firmware's
# errors/errors.json (scripts/sync-error-catalog.sh): the message and the SDK's fix for each code.
# https://embeddedci.com/docs/errors lists them all. The SDK never reads a refusal's text.

_CATALOG: Optional[Dict[str, Any]] = None


def error_catalog() -> Dict[str, Any]:
    """The bundled error catalog: ``{"codes": {"BP0001": {...}}, "docs_url": ...}``."""
    global _CATALOG
    if _CATALOG is None:
        try:
            text = resources.files(__package__).joinpath("error_catalog.json").read_text("utf-8")
            _CATALOG = json.loads(text)
        except (OSError, ValueError):
            _CATALOG = {"codes": {}, "docs_url": "https://embeddedci.com/docs/errors#{code}"}
    return _CATALOG


def error_docs_url(code: str) -> str:
    """The docs page of an error code."""
    return str(error_catalog().get("docs_url", "https://embeddedci.com/docs/errors#{code}")).replace(
        "{code}", code)


def _fill(template: str, values: Mapping[str, Any]) -> str:
    return re.sub(r"\{([a-z0-9_]+)\}", lambda m: str(values.get(m.group(1), "?")), template)


def explain(code: str, surface: str = "sdk") -> str:
    """A code's entry as text: message, cause and the fix (``surface``: sdk, cli, mcp or web)."""
    entry = error_catalog().get("codes", {}).get(code.strip().upper())
    if not entry:
        return f"{code}: not in this SDK's catalog; see {error_docs_url(code.strip().upper())}"
    return (f"{code.upper()} {entry['name']}: {entry['message']}\n\n{entry['cause']}\n\n"
            f"Fix: {entry['fix'].get(surface, '')}\n{error_docs_url(code.upper())}")


def _attach_code(exc: FirmwareError, code: str, values: Optional[Mapping[str, Any]]) -> FirmwareError:
    """Put the pod's code and values on exc and render its message from the catalog: ``cmd:
    BP0009: The wiring profile k66 changed ...; load it again ...
    (https://embeddedci.com/docs/errors#BP0009)``. A code this SDK's catalog does not have keeps
    the pod's text, if any, and says to update the SDK."""
    exc.code = code
    exc.values = dict(values or {})
    exc.docs_url = error_docs_url(code) if code else ""
    if not code:
        return exc
    entry = error_catalog().get("codes", {}).get(code)
    if entry:
        text = (f"{code}: {_fill(entry['message'], exc.values)}; "
                f"{_fill(entry['fix']['sdk'], exc.values)} ({exc.docs_url})")
    else:
        said = f"{exc.firmware_message}; " if exc.firmware_message else ""
        text = (f"{code}: {said}this SDK does not know this code yet: update it (pip install -U "
                f"embeddedci) or see {exc.docs_url}")
    exc.args = (f"{exc.cmd}: {text}" if exc.cmd else text,)
    return exc


def _int(v: Any) -> int:
    """An int arg, 0 when it is missing or not a number."""
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    return int(v) if isinstance(v, str) and v.strip().isdigit() else 0


def _str(v: Any) -> str:
    return "" if v is None else str(v)


def _leased(v: Mapping[str, Any], cmd: Optional[str]) -> Dict[str, Any]:
    left = v.get("left_s")
    return {"holder": _str(v.get("holder")), "purpose": _str(v.get("purpose")),
            "left_s": int(left) if isinstance(left, (int, float)) and not isinstance(left, bool) else None}


_NO_FIELDS: Callable[[Mapping[str, Any], Optional[str]], Dict[str, Any]] = lambda v, cmd: {}  # noqa: E731
_WIRING = (lambda v, cmd: {"name": _str(v.get("name"))})
_FieldsFn = Callable[[Mapping[str, Any], Optional[str]], Dict[str, Any]]
_LA = (lambda v, cmd: {"la": _int(v.get("la"))})
_PULL_DIR = (lambda v, cmd: {"la": _int(v.get("la")), "want": _str(v.get("want")),
                             "have": _str(v.get("have"))})


def _feature(key: str) -> _FieldsFn:
    return lambda v, cmd: {"feature": _str(v.get(key)) or cmd or ""}


def _pin_conflict(v: Mapping[str, Any], cmd: Optional[str]) -> Dict[str, Any]:
    owner, function = _str(v.get("owner")), _str(v.get("function"))
    return {"la": _int(v.get("la")), "function": function or owner, "owner": owner,
            "instance": _str(v.get("instance"))}


#: Every code in the catalog: the exception class it raises and how its args fill that class's
#: fields (``values`` keeps them all). A code missing here (newer than this SDK) is a plain
#: :class:`FirmwareError` with the code on it.
_BY_CODE: Dict[str, Tuple[type, _FieldsFn]] = {
    "BP0001": (FirmwareError, _NO_FIELDS),                       # la_voltage_not_set
    "BP0002": (UnsupportedFeatureError, _feature("cmd")),        # unknown_command
    "BP0003": (PodBusyError, _NO_FIELDS),                        # i2c_session_open
    "BP0004": (PodBusyError, _NO_FIELDS),                        # spi_session_open
    "BP0005": (PodBusyError, _NO_FIELDS),                        # bus_engine_busy {want}
    "BP0006": (FirmwareError, _NO_FIELDS),                       # missing_argument {arg}
    "BP0007": (WiringError, _WIRING),                            # wiring_profile_not_found
    "BP0008": (WiringError, _WIRING),                            # wiring_bad_name
    "BP0009": (WiringError, _WIRING),                            # wiring_revision_conflict {rev}
    "BP0010": (WiringError, _WIRING),                            # wiring_store_full {max}
    "BP0011": (WiringError, _WIRING),                            # wiring_profile_too_large
    "BP0012": (WiringError, _WIRING),                            # wiring_not_json_object
    "BP0013": (WiringError, _WIRING),                            # wiring_delete_default
    "BP0014": (WiringError, _WIRING),                            # wiring_upload_out_of_order
    "BP0015": (WiringError, _WIRING),                            # wiring_storage_failed
    "BP0016": (PodBusyError, _NO_FIELDS),                        # storage_busy
    "BP0017": (UnsupportedFeatureError, _feature("feature")),    # gateware_too_old {feature, min}
    "BP0018": (FirmwareError, _NO_FIELDS),                       # uart_same_pin {la}
    "BP0019": (FirmwareError, _NO_FIELDS),                       # uart_half_duplex_two_pins
    "BP0020": (FirmwareError, _NO_FIELDS),                       # uart_entry_no_reset_pin
    "BP0021": (FirmwareError, _NO_FIELDS),                       # uart_entry_invalid {arg, max}
    "BP0022": (FirmwareError, _NO_FIELDS),                       # uart_de_pin {la}
    "BP0023": (PullDirectionError, _PULL_DIR),                   # la_no_pull {la}
    "BP0024": (PullDirectionError, _PULL_DIR),                   # la_pull_direction {la, want, have}
    "BP0025": (PodLeasedError, _leased),                         # lease_busy
    "BP0026": (PodLeaseLostError, lambda v, cmd: {"by": _str(v.get("by"))}),   # lease_lost
    "BP0027": (FirmwareError, _NO_FIELDS),                       # lease_not_held {id}
    "BP0028": (FirmwareError, _NO_FIELDS),                       # vref_5v_missing {mv}
    "BP0029": (FirmwareError, _NO_FIELDS),                       # pull_rail_down {mv}
    "BP0030": (ClientTooOldError,                                # client_too_old
               lambda v, cmd: {"client_api": _int(v.get("client_api")), "api": _int(v.get("api")),
                               "min_client_api": _int(v.get("min_client_api"))}),
    "BP0031": (PinConflictError, _pin_conflict),                 # la_pin_in_use
    "BP0032": (PullConflictError, _LA),                          # la_pull_down_engaged
    "BP0033": (PullConflictError, _LA),                          # la_pull_down_conflict
    "BP0034": (DirectionConflictError, _LA),                     # la_dut_output
    "BP0035": (DirectionConflictError, _LA),                     # la_driven_pin
    "BP0036": (FirmwareError, _NO_FIELDS),                       # la_not_gpio_output {la, function}
    "BP0037": (FirmwareError, _NO_FIELDS),                       # la_voltage_pins_in_use {pins}
    "BP0038": (TriggerTimeout,                                   # trigger_timeout
               lambda v, cmd: {"la": _int(v.get("la")), "edge": _str(v.get("edge"))}),
    "BP0039": (TriggerTimeout,                                   # sensor_trigger_timeout
               lambda v, cmd: {"la": _int(v.get("la")), "edge": "start"}),
    "BP0040": (PodLockedError, _NO_FIELDS),                      # lan_locked {cmd}
    "BP0041": (PodLockedError, _NO_FIELDS),                      # policy_not_from_lan {setting}
    "BP0042": (PodLockedError, _NO_FIELDS),                      # setting_cloud_or_usb {setting}
    "BP0043": (PodLockedError, _NO_FIELDS),                      # setting_device_only {setting}
    "BP0044": (PodLockedError, _NO_FIELDS),                      # policy_loosen_usb_only (retired)
    "BP0045": (PermissionDeniedError, _NO_FIELDS),               # role_required {cmd}
    "BP0046": (UnsupportedFeatureError,                          # no_analog_front_end {cmd}
               lambda v, cmd: {"feature": "analog"}),
    "BP0047": (PodBusyError, _NO_FIELDS),                        # pod_busy
    "BP0048": (PodBusyError, _NO_FIELDS),                        # storage_bus_busy {what}
    "BP0049": (PodBusyError, _NO_FIELDS),                        # uart_session_open
    "BP0050": (PodBusyError, _NO_FIELDS),                        # swd_session_open
    "BP0051": (PodBusyError, _NO_FIELDS),                        # update_in_progress {target, holder}
    "BP0052": (PodBusyError, _NO_FIELDS),                        # cloud_command_busy
    "BP0053": (ReplyTooLargeError, _NO_FIELDS),                  # reply_too_large
    "BP0054": (ReplyTooLargeError, _NO_FIELDS),                  # cloud_reply_too_large {bytes, limit}
    "BP0055": (FirmwareError, _NO_FIELDS),                       # sensor_instance_unknown {instance}
    "BP0056": (UnsupportedFeatureError,                          # no_module_link {cmd}
               lambda v, cmd: {"feature": "module"}),
    "BP0057": (FirmwareError, _NO_FIELDS),                       # image_switch_sensors_running {sensors}
    "BP0058": (UnsupportedFeatureError, _feature("feature")),    # fpga_image_required {feature, image}
    "BP0059": (UnsupportedFeatureError, _feature("feature")),    # fpga_image_sensors_first
    "BP0060": (UnsupportedFeatureError, _feature("target")),     # ota_unknown_target {target}
    "BP0061": (FirmwareError, _NO_FIELDS),                       # i2c_needs_pullups {la}
    "BP0062": (FirmwareError, _NO_FIELDS),                       # i2c_session_not_open
    "BP0063": (FirmwareError, _NO_FIELDS),                       # i2c_engine_reset
    "BP0064": (UnsupportedFeatureError,                          # loop_sweep_with_map
               lambda v, cmd: {"feature": "dac_loop_input_map"}),
    "BP0065": (FirmwareError, _NO_FIELDS),                       # output_used_by_sensor {output, instance}
    "BP0066": (PodBusyError, _NO_FIELDS),                        # step_busy
    "BP0067": (FirmwareError, _NO_FIELDS),                       # samples_out_of_range {max}
    "BP0068": (FirmwareError, _NO_FIELDS),                       # la_rate_above_ceiling
    "BP0069": (FirmwareError, _NO_FIELDS),                       # la_rate_too_deep
    "BP0070": (FirmwareError, _NO_FIELDS),                       # capture_dual_too_deep
    "BP0071": (FirmwareError, _NO_FIELDS),                       # capture_overflowed
    "BP0072": (FirmwareError, _NO_FIELDS),                       # psram_too_small
    "BP0073": (FirmwareError, _NO_FIELDS),                       # psram_bandwidth
    "BP0074": (FirmwareError, _NO_FIELDS),                       # signatures_always_required
    "BP0075": (PodLockedError, _NO_FIELDS),                      # lan_plaintext_off
    "BP0076": (UnsupportedFeatureError,                          # usb_host_over_usb
               lambda v, cmd: {"feature": "usb_host"}),
    "BP0077": (UnsupportedFeatureError,                          # usb_host_port_unavailable {port}
               lambda v, cmd: {"feature": "usb_host"}),
    "BP0078": (UnsupportedFeatureError,                          # usb_host_no_crystal
               lambda v, cmd: {"feature": "usb_host"}),
    "BP0079": (FirmwareError, _NO_FIELDS),                       # usb_host_other_port {port}
    "BP0080": (DacPinoutMismatchError,                           # dac_pinout_mismatch
               lambda v, cmd: {"digital": _str(v.get("digital")), "module": _str(v.get("module"))}),
    "BP0081": (PodNotPairedError, _NO_FIELDS),                   # lan_not_paired {cmd}
    "BP0082": (FirmwareError, _NO_FIELDS),                       # lan_clients_full {max}
}


def classify_firmware_error(exc: FirmwareError) -> FirmwareError:
    """The specific :class:`FirmwareError` subclass for a pod refusal, from its code (a plain
    FirmwareError with a known code, as a fake transport might raise, gets its class); a refusal
    without a code, or already classified, comes back unchanged. Never reads the text."""
    if type(exc) is not FirmwareError or _BY_CODE.get(exc.code, (FirmwareError,))[0] is FirmwareError:
        return exc
    return firmware_error(exc.firmware_message, cmd=exc.cmd, code=exc.code, values=exc.values)


def firmware_error(message: str, *, cmd: Optional[str] = None, code: str = "",
                   values: Optional[Mapping[str, Any]] = None) -> FirmwareError:
    """The :class:`FirmwareError` (its specific subclass when there is one) for a pod refusal.
    ``code`` / ``values``: the pod's stable error code and its args.

    The code decides the class (:data:`_BY_CODE`) and the message (the catalog's message and SDK
    fix; the pod sends none). A refusal without a code is a plain :class:`FirmwareError` with the
    pod's text."""
    v = dict(values or {})
    known = _BY_CODE.get(code) if code else None
    if known is not None:
        cls, fields = known
        exc: FirmwareError = cls(message, cmd=cmd, **fields(v, cmd))
    else:
        exc = FirmwareError(message, cmd=cmd)
    return _attach_code(exc, code or "", values)
