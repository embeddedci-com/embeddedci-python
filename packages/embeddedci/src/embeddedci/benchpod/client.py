"""The :class:`BenchPod` facade — the user-facing API.

Wraps a :class:`~embeddedci.benchpod.transport.base.Transport` with named operations: power,
flash, UART, sensor emulation, analog paths, captures, DAC generate/replay, the in-fabric
control loop and CAN. Designed to drop straight into pytest::

    from embeddedci import benchpod

    with benchpod.BenchPod("192.168.1.213", la_voltage=3.3) as bp:
        bp.power_on(benchpod.INTERNAL)
        assert bp.flash(file="fw.elf", target="target/stm32f1x.cfg",
                        swclk=benchpod.PIN1, swdio=benchpod.PIN2).ok

Conventions across the whole API:

* units are **volts**, **seconds** and **hertz** (``sample_rate_hz``, ``delay``, ``duration``);
* invalid arguments raise :class:`ValueError`; a device, transport or server failure raises a
  :class:`~embeddedci.benchpod.errors.BenchPodError` subclass;
* device state comes back as frozen dataclasses (:mod:`embeddedci.benchpod.state`), captures as
  :mod:`embeddedci.benchpod.results` objects.

:meth:`BenchPod.command`, :attr:`BenchPod.transport` and :attr:`BenchPod.lowlevel` are escape
hatches below that API and are not covered by its stability guarantee.
"""

from __future__ import annotations

import base64
import codecs
import dataclasses
import json
import logging
import math
import os
import re
import time
import weakref
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from . import can as _can
from . import capture as _capture
from . import control_loop as _control_loop
from . import detect as _detect
from . import dsp as _dsp
from . import esp as _esp
from . import expansion as _expansion
from . import feature_map as _feature_map
from . import flash as _flash
from . import i2c as _i2c
from . import i2c_master as _i2c_master
from . import motor_emulator as _motor_emulator
from . import prog as _prog
from . import spi as _spi
from . import usb_host as _usb
from . import uart as _uart
from .capabilities import (ADC_WRAP_SPAN, ADC_WRAP_THRESHOLD, BUS_MASTER_GATEWARE,
                           AffineCal, Capabilities)
from .constants import (
    ADC_SOURCE_PATHS,
    ADC_GAIN_SOURCES,
    ADC_RANGES,
    ADC_SOURCES,
    ANALOG_PATHS,
    CALIBRATE_SOURCES,
    CALIBRATION_PARTS,
    DAC_CAL_PATHS,
    CAN_MODES,
    DAC_OUTPUT_PATHS,
    DAC_PATHS,
    DECODE_PROTOCOLS,
    DEFAULT_DAC_PATH,
    GPIO_MODES,
    LA_STEP_MAX_DELAY_US,
    LA_STEP_MAX_STEPS,
    LA_STEP_MIN_DELAY_US,
    LA_VOLTAGES,
    LA_VOLTAGE_5V,
    PULL_DIRECTIONS,
    PULL_TABLE,
    REPLAY_MAPPINGS,
    PullDirection,
    WAVESHAPES,
    AdcGainSource,
    AdcRange,
    AdcSource,
    AnalogPath,
    CalibrateSource,
    CalibrationPart,
    DacCalPath,
    CanMode,
    DacOutputPath,
    DacPath,
    DecodeProtocol,
    Efuse,
    FpgaImage,
    GpioMode,
    LoopSource,
    Pin,
    ReplayMapping,
    Waveshape,
    check_choice,
    coerce_efuse,
    coerce_pin,
)
from .connection import resolve_connection
from .errors import (BenchPodError, ClientTooOldError, DirectionConflictError, FirmwareError, FirmwareTooOldError,
                     FlashError, PullDirectionError, UnsupportedFeatureError,
                     UsbHostUnavailableError, UsbTimeout, classify_firmware_error, firmware_error)
from .flash import FlashResult
from .gpio import LA_DIRS, ExtPin, GpioPin, LaPinState
from .lease import DEFAULT_LEASE_TTL, DEFAULT_LEASE_WAIT, DeviceLease
from .lowlevel import LowLevel
from .power import PowerProfile, PowerProfileSession
from .replay import DacHandle, Fault, ReplayHandle, normalize_fault
from .results import Capture, CorrelatedCapture, LaCapture, LanClient, LanClients, LanTls, MacAddresses, Trigger
from .wiring import Signal, Wiring
from .state import (
    AdcRangeState,
    AdcRangeTest,
    AdcReading,
    AnalogPathState,
    Calibration,
    CurrentOutput,
    DacOutput,
    FpgaImageInfo,
    LaVoltage,
    LocalLease,
    LoopState,
    ModuleInfo,
    PowerStatus,
    PullState,
    ResetState,
    TargetStatus,
    UsbCcStatus,
)
from .transport import Transport, open_transport
from .transport.cloud import CloudTransport
from .transport.tcp import TcpTransport
from .transport import client_identity
from .transport import tls as _tls
from . import pod_lease as _pod_lease

if TYPE_CHECKING:  # pragma: no cover
    from .sensors import SensorEmulation
    from .server_api import ServerApi
    from .waveforms import Waveform, WaveformLibrary

#: Environment variables the client reads when the matching argument is not passed.
LA_VOLTAGE_ENV = "BENCHPOD_LA_VOLTAGE"
API_BASE_ENV = "BENCHPOD_API_BASE"
API_KEY_ENV = "BENCHPOD_API_KEY"
WIRING_ENV = "BENCHPOD_WIRING"
#: The name of the pod wiring profile to load (``wiring_profile=``); unset = the pod's default.
WIRING_PROFILE_ENV = "BENCHPOD_WIRING_PROFILE"
#: ``local`` keeps the wiring lookup off embeddedci.com (see :attr:`BenchPod.wiring`).
WIRING_SOURCE_ENV = "BENCHPOD_WIRING_SOURCE"
#: Accepted ``wiring_source`` values: ``auto`` (the default) and ``local``.
WIRING_SOURCES = ("auto", "local")
#: Budget, in seconds, for finding a LAN/USB pod's stored wiring profile on embeddedci.com.
_WIRING_LOOKUP_TIMEOUT = 3.0
#: Bytes of a wiring profile per wiring_get / wiring_put (the firmware's WIRING_PIECE_MAX).
_WIRING_PIECE = 600

#: The 4-20 mA output (J9) as a waveform output path. Levels on it are amps.
_CURRENT_OUT = "current_out"
# The DAC output each named analog path drives (cal1/cal2 loop the 5 V / 12 V DAC back to the ADC).
_ANALOG_PATH_DAC = {"dac_3v3": "3v3", "dac_5v": "5v", "dac_12v": "12v", "cal1": "5v", "cal2": "12v",
                    "current_out": _CURRENT_OUT}
#: The sense resistor on the 4-20 mA input (J8): ``current_in`` reads the voltage across it. 249 Ω
#: on analog modules up to v3r4 and the default for firmware that does not say; from v4 the pod
#: reports its own value (``Calibration.sense_ohms``, 100 Ω with the ADC divider in parallel).
_CURRENT_IN_SENSE_OHMS = 249.0
#: The live zero a stopped current waveform returns to (microamps).
_CURRENT_OUT_ZERO_UA = 4000

_log = logging.getLogger("embeddedci.benchpod")

#: Capability flags only the analog board announces: the digital-only board lacks them whatever its
#: firmware (cloud_caps_hw.c).
_ANALOG_FLAGS = frozenset({"scope", "dac_replay", "dac_deep_replay", "dac_control_loop", "dac_cotrig",
                           "dac_loop_sources", "dac_loop_input_map", "calibrate", "current_out"})

#: The capability flag and FPGA_FEATURES bit each gateware image carries. The emulation image has
#: no flag of its own while it runs (``sensors_digital`` says it is installed or running): the
#: pod is on it when it shows neither of the other two, which ``fpga_image`` confirms.
_IMAGE_FLAG = {FpgaImage.LOOP: "dac_control_loop", FpgaImage.DEEP_REPLAY: "dac_deep_replay",
               FpgaImage.EMULATION: "sensors_digital"}
_IMAGE_FEATURE = {FpgaImage.LOOP: 0x01, FpgaImage.DEEP_REPLAY: 0x02, FpgaImage.EMULATION: 0x04}

#: The first gateware whose emulation image has no bus masters (SWD, JTAG, SPI master, I2C
#: master), and the FPGA_FEATURES bit that says an image has them (loop and deep do, emulation
#: does not). Older gateware has them in every image.
BUS_MASTERS_SPLIT_GATEWARE = 65
_FEATURE_BUS_MASTERS = 0x20

#: Pre-trigger samples a capture with a ``send`` keeps, so the decoder sees the first START (an
#: edge trigger fires on that START itself; without them the first message is lost).
_SEND_PRE_SAMPLES = 64

#: Seconds to wait for the server's cached capabilities to follow an image switch.
#: la_pins pages a listing too large for one reply; a full one takes at most 14.
_LA_PINS_MAX_PAGES = 16
_SERVER_CAPS_WAIT = 15.0


def _images_switchable(caps: Capabilities) -> bool:
    """Whether the pod carries both gateware images: it advertises one of the image-bound
    features (the rule the web UI uses too)."""
    return bool(caps.dac_control_loop or caps.dac_deep_replay)


def _maybe_on_emulation(caps: Capabilities) -> bool:
    """Whether the pod may be running the emulation image: it has one (``sensors_digital``) and
    shows neither the loop nor the deep-replay image's feature."""
    return bool(caps.sensors_digital and not _images_switchable(caps))


def _mac_text(v: Any) -> Optional[str]:
    """A MAC from a status field or a stored parameter: lowercase text, None when empty."""
    return v.strip().lower() if isinstance(v, str) and v.strip() else None


def _major_version(version: str) -> Optional[int]:
    """The major number of a firmware version (``"4.0.0"`` -> 4), None when it is not one."""
    m = re.match(r"^v?(\d+)\.", (version or "").strip())
    return int(m.group(1)) if m else None

def _num(v: float) -> Union[int, float]:
    """A number for the wire: an int when it is whole (``1000.0`` -> ``1000``)."""
    f = float(v)
    return int(f) if f.is_integer() else f

def _sdk_version() -> str:
    try:
        from importlib.metadata import version
        return version("embeddedci")
    except Exception:   # a source tree without install
        return "unknown"


def _dict(data: Any) -> Dict[str, Any]:
    return data if isinstance(data, dict) else {}


def _seconds_to_ms(value: Optional[float], name: str) -> int:
    if value is None:
        return 0
    if value < 0:
        raise ValueError(f"{name} must be >= 0 seconds, got {value!r}")
    return int(round(value * 1000))


#: The pod's console log levels, quietest first (:meth:`BenchPod.set_log_level`).
LOG_LEVELS = ("error", "warn", "info", "debug")


def _log_level_from_reply(data: Any) -> str:
    level = _dict(data).get("level")
    if level not in LOG_LEVELS:
        raise BenchPodError(f"log_level: unexpected reply {data!r}")
    return str(level)


def _check_level(level: Any) -> int:
    if level not in (0, 1):
        raise ValueError(f"level must be 0 or 1, got {level!r}")
    return int(level)


#: Why a call refuses an expander pin, by the argument it came in as.
_EXT_PULSES = "pulses are timed by the FPGA, which drives LA channels only"
_EXT_REFUSALS = {
    "trigger": "captures and their triggers sample the LA channels only",
    "send la": "a capture's send runs on the FPGA, which drives LA channels only",
    "dir_la": _EXT_PULSES,
}


def _one_or_many(las: List[int]) -> Any:
    return las[0] if len(las) == 1 else las


@contextmanager
def _classified_errors() -> Iterator[None]:
    """Re-raise a coded pod refusal that reached here as a plain FirmwareError as its specific
    error (PinConflictError, PullConflictError, TriggerTimeout, ...), from its code."""
    try:
        yield
    except FirmwareError as exc:
        specific = classify_firmware_error(exc)
        if specific is exc:
            raise
        raise specific from None


def _can_payload(data: Union[bytes, Sequence[int], None]) -> List[int]:
    payload = list(bytes(data)) if data else []
    if len(payload) > 64:
        raise ValueError(f"a CAN FD frame carries at most 64 data bytes, got {len(payload)}")
    return payload


class BenchPod:
    """A connected BenchPod device."""

    def __init__(
        self,
        connection: Optional[str] = None,
        *,
        la_voltage: Optional[float] = None,
        timeout: float = 30.0,
        transport: Optional[Transport] = None,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        cloud_token: Optional[str] = None,
        cloud_audience: Optional[str] = None,
        cloud_user_token: Optional[Callable[[], str]] = None,
        lease: bool = True,
        lease_wait: float = DEFAULT_LEASE_WAIT,
        lease_ttl: int = DEFAULT_LEASE_TTL,
        wiring: Union[Wiring, Mapping[str, Any], str, "os.PathLike[str]", None] = None,
        wiring_source: Optional[str] = None,
        wiring_profile: Optional[str] = None,
        lease_purpose: Optional[str] = None,
    ) -> None:
        """Open a BenchPod.

        ``connection`` is a host[:port], a serial device path, ``"usb"`` (auto-detect the USB
        pod), ``"usb:<name>"`` (one of several USB pods, by name), a pod name (``"benchpod-a1b2c3"``:
        found on the LAN over mDNS, else on USB), ``"discover"`` (find one pod on the LAN via
        mDNS), or ``"embeddedci:<device>"``
        to drive a named device through embeddedci.com. When omitted ``BENCHPOD_CONNECTION`` is
        used, else the default connection ``benchpod-cli`` saved (``benchpod set-connection``);
        ``"saved"`` names that one explicitly. Pass ``transport`` to inject a custom backend
        instead.

        ``la_voltage`` (1.8 or 3.3 volts, falling back to ``BENCHPOD_LA_VOLTAGE``) selects the LA
        I/O-bank voltage right after connecting. Without it, the wiring profile's ``la_mv`` is
        used when the pod has none yet, so a profile that sets its I/O voltage needs neither. The
        pod refuses every LA-bank operation (flashing, UART, LA capture, pull resistors, sensor
        emulation) while no voltage is selected.

        Cloud options: ``api_key`` (or ``BENCHPOD_API_KEY``) authenticates anywhere; else
        ``cloud_user_token``, a callable returning a logged-in user's access token, is exchanged
        for a device session; without either the cloud destination uses GitHub Actions OIDC inside
        a workflow (``cloud_token``/``cloud_audience`` override that), and elsewhere the
        ``benchpod login`` session, so a user who ran ``benchpod login`` needs nothing else.
        A session token the SDK mints is renewed before it expires, so a long session keeps
        working; a ``cloud_token`` you pass is used as is. ``api_base`` (or ``BENCHPOD_API_BASE``) points at another embeddedci server. An API
        key also unlocks the cloud waveform library on a LAN/serial connection.

        The pod is *shared*, so the client takes an exclusive **lease** on it for the life of this
        object: the server's lease for the cloud destination, the pod's own lease on the LAN and
        the USB network when the pod has the ``lease`` feature (``lease_purpose`` says what for;
        default: the recorded test run's suite, else ``"sdk"``). A run that finds the pod busy
        waits up to ``lease_wait`` seconds and then raises
        :class:`~embeddedci.benchpod.errors.PodLeasedError` (a
        :class:`~embeddedci.benchpod.errors.DeviceBusyError`) naming the holder. With
        ``BENCHPOD_LEASE`` set (``benchpod ci`` sets it for the processes it starts) the client
        joins that lease instead and never renews or releases it. ``lease=False`` skips locking.
        The USB console never leases.

        ``wiring`` is the bench's :class:`~embeddedci.benchpod.wiring.Wiring` profile (a ``Wiring``, a
        dict, or a ``.json``/``.toml`` path) — see :attr:`wiring`. ``wiring_profile`` (or
        ``BENCHPOD_WIRING_PROFILE``) names one of the profiles stored on the pod (default: the pod's
        default profile); it cannot be combined with ``wiring``. ``wiring_source`` (or
        ``BENCHPOD_WIRING_SOURCE``) is ``"auto"`` (default) or ``"local"``, which never asks
        embeddedci.com for the stored profile (only used for pods whose firmware stores none).
        """
        source = (wiring_source if wiring_source is not None
                  else os.environ.get(WIRING_SOURCE_ENV, "")).strip().lower() or "auto"
        if source not in WIRING_SOURCES:
            what = "wiring_source" if wiring_source is not None else WIRING_SOURCE_ENV
            raise ValueError(f"{what} must be 'auto' or 'local', got {source!r}")
        self._wiring_source = source
        profile_name = (wiring_profile if wiring_profile is not None
                        else os.environ.get(WIRING_PROFILE_ENV, "")).strip()
        if profile_name and (wiring is not None or os.environ.get(WIRING_ENV, "").strip()):
            raise ValueError("pass a wiring file or profile (wiring= / BENCHPOD_WIRING) or the name of a "
                             "profile stored on the pod (wiring_profile= / BENCHPOD_WIRING_PROFILE), not both")
        self._wiring_profile = profile_name
        self._wiring: Optional[Wiring] = Wiring.coerce(wiring) if wiring is not None else None
        self.timeout = timeout
        self._lease: Optional[DeviceLease] = None
        self._pod_lease: Optional["_pod_lease.PodLease"] = None
        # The DAC output this connection last routed (dac_output, analog_path, a routed waveform),
        # so a waveform played with route=False and no dac_path scales for it. None = unknown.
        self._routed_dac_path: Optional[str] = None
        #: The pod put the ADC in the low range by itself, routing ``current_in`` on analog module
        #: v4 (firmware 3.10.13+); the next DAC route puts it back, without saying so.
        self._adc_low_auto = False
        self._api_base = api_base or os.environ.get(API_BASE_ENV)
        self._api_key = api_key or os.environ.get(API_KEY_ENV)
        self._cloud_user_token = cloud_user_token
        self._caps: Optional[Capabilities] = None
        self._server_api: Optional["ServerApi"] = None
        #: The 4-20 mA output's range in amps (min, max), as the pod reported it. Read once.
        self._current_out_span: Optional[Tuple[float, float]] = None
        self._waveforms: Optional["WaveformLibrary"] = None
        self._sensors: Optional["SensorEmulation"] = None
        self._device_name = ""
        # The last I2CMaster this BenchPod opened, to tell its own open session from another
        # connection's when i2c_start is refused as busy.
        self._i2c_master_ref: Optional["weakref.ReferenceType[_i2c_master.I2CMaster]"] = None
        # The last SpiSession open_spi() returned, so an expander can tell it holds the bus engine.
        self._spi_session_ref: Optional["weakref.ReferenceType[_spi.SpiSession]"] = None
        # The GPIO expander attached with expansion(), if any.
        self._expansion: Optional[_expansion.Expansion] = None
        #: What happened to the wiring profile's pin directions the last time the SDK sent them on
        #: its own (on connect, on a new :attr:`wiring`): ``"sent"`` or ``"not sent: <why>"``.
        self.pin_directions_status = "not sent: not connected yet"
        #: Hardware controls below the named-path API (raw mux/relay/DAC-code access). Not covered
        #: by the stability guarantee.
        self.lowlevel = LowLevel(self)
        if transport is not None:
            self._transport: Transport = transport
        else:
            spec = resolve_connection(connection)
            if spec.is_cloud() and not self._api_key and self._cloud_user_token is None:
                # Outside GitHub Actions the `benchpod login` session is the default credential.
                # Kept here (not only in the transport) so the waveform and device routes, which
                # take a user rather than a cloud session token, use it too.
                from .cloud_auth import DEFAULT_API_BASE, login_session_token

                self._cloud_user_token = login_session_token(self._api_base or DEFAULT_API_BASE)
            self._transport = open_transport(
                spec,
                timeout=timeout,
                api_base=self._api_base,
                token=cloud_token,
                audience=cloud_audience,
                api_key=self._api_key,
                user_token=self._cloud_user_token,
            )
        # A test run being recorded (the pytest plugin's run folder): record this pod's calls too.
        from .runs.active import active_run

        run = active_run()
        if run is not None:
            run.attach(self._transport)
        try:
            if lease and isinstance(self._transport, CloudTransport):
                runner_lease = os.environ.get(_pod_lease.LEASE_ENV, "").strip()
                self._lease = DeviceLease(
                    api_base=self._transport.api_base,
                    token_provider=self._transport._session_token,
                    invalidate_token=self._transport._invalidate_token,
                    device_name=self._transport.device_name,
                    ttl_seconds=lease_ttl,
                    lease_id=runner_lease or None,
                    joined=bool(runner_lease),
                )
                self._lease.acquire(wait_timeout=lease_wait)
                self._transport.lease_id = self._lease.lease_id
            elif lease:
                self._take_pod_lease(lease_purpose, lease_ttl, lease_wait)
            # Remember the cloud device name + base so the server client can address the same
            # device (server-side replay) and reuse the transport's session token.
            self._device_name = getattr(self._transport, "device_name", "") or ""
            if not self._api_base:
                self._api_base = getattr(self._transport, "api_base", None)
            if la_voltage is None:
                env = os.environ.get(LA_VOLTAGE_ENV, "").strip()
                if env:
                    try:
                        la_voltage = float(env)
                    except ValueError:
                        raise ValueError(f"{LA_VOLTAGE_ENV} must be 1.8 or 3.3, got {env!r}") from None
            if la_voltage is not None:
                self.set_la_voltage(la_voltage)
            self._auto_pin_directions()
            if la_voltage is None:
                self._auto_la_voltage()
        except BaseException:
            self.close()
            raise
        if run is not None and run.active:
            run.note_pod(self)

    def _take_pod_lease(self, purpose: Optional[str], ttl: int, wait: float) -> None:
        """The pod's own lease on a LAN or USB network connection (feature ``lease``)."""
        if not isinstance(self._transport, TcpTransport):
            if os.environ.get(_pod_lease.LEASE_ENV, "").strip():
                _log.warning("%s is set but this connection (%s) takes no lease",
                             _pod_lease.LEASE_ENV, type(self._transport).__name__)
            return
        try:
            supported = self.supports("lease")
        except BenchPodError as exc:
            _log.debug("connect: could not read the capabilities for the lease: %s", exc)
            supported = False
        self._pod_lease = _pod_lease.open_pod_lease(
            self._transport, supported=supported, purpose=purpose, ttl_s=ttl, wait=wait)

    def local_lease(self) -> LocalLease:
        """The pod's own lease (feature ``lease``) as its status reports it: who holds it, for
        what, how long it has left and whether this session holds it. ``held`` is False when the
        firmware does not report a lease."""
        return LocalLease.from_status(self._caps_status())

    def _auto_la_voltage(self) -> None:
        """On connect: when the pod has no I/O voltage yet and the wiring profile sets one, set it.
        Only for a profile stored on the pod that connect already loaded (to send the pin
        directions, firmware 3.10.4+), so it costs no extra reads otherwise; firmware 3.10.10+ takes the voltage
        from the profile itself on the first LA operation. Never changes a voltage the pod already
        has, and never fails the connection."""
        # A profile stored on the pod (what the firmware itself would use); a file or dict profile
        # sets its voltage at power_on, as before.
        if self._wiring is None or self._wiring.source != "pod":
            return
        try:
            want = self._wiring.la_voltage
            if want is None:
                return
            if self.get_la_voltage().voltage is not None:
                return
            self.set_la_voltage(want)
            _log.info("connect: I/O voltage %g V from the wiring profile %s", want,
                      self._wiring.profile_name or "(this connection's)")
        except Exception as exc:  # noqa: BLE001 -- a convenience; the LA call that needs it says why
            _log.debug("connect: I/O voltage from the wiring profile not set: %s", exc)

    # -- lifecycle ----------------------------------------------------------

    @property
    def transport(self) -> Transport:
        """The underlying transport (escape hatch; not covered by the stability guarantee)."""
        return self._transport

    @property
    def leased(self) -> bool:
        """True while this client holds an exclusive lease: the server's on a cloud device, or the
        pod's own lease on the LAN or the USB network."""
        if self._pod_lease is not None and self._pod_lease.active:
            return True
        return self._lease is not None and self._lease.held

    def close(self) -> None:
        """Close the connection and release the lease, if any (not a lease joined through
        ``BENCHPOD_LEASE``). Idempotent."""
        try:
            self._yield_expansion_bus()
            if self._pod_lease is not None:
                lease, self._pod_lease = self._pod_lease, None
                _pod_lease.close_pod_lease(self._transport, lease)
            self._transport.close()
        finally:
            if self._lease is not None:
                self._lease.release()
                self._lease = None

    def __enter__(self) -> "BenchPod":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- status -------------------------------------------------------------

    def ping(self) -> Any:
        """Confirm the pod is reachable (raises if it is not)."""
        return self._transport.ping()

    def status(self) -> Dict[str, Any]:
        """The pod's status report: firmware version, board, network and advertised features."""
        return _dict(self._transport.status())

    def _caps_status(self) -> Dict[str, Any]:
        """``status`` for the capabilities (:mod:`~embeddedci.benchpod.feature_map`)."""
        return _dict(self._transport.status())

    def diag(self) -> Dict[str, Any]:
        """The pod's health counters: ``heap_free``, ``heap_min``, ``stack_min``, per-task
        ``stacks``, the network stack's ``lwip_mem_*`` and ``pbuf_pool_*`` high-water marks,
        ``malloc_failures``, the console's ``rx_dropped``/``tx_dropped`` and ``board_rev_mv``
        (the ``diag`` command).
        """
        return _dict(self.command({"cmd": "diag"}))

    def mac_addresses(self, status: Optional[Mapping[str, Any]] = None) -> "MacAddresses":
        """The pod's Ethernet and Wi-Fi MACs, for a network that hands out addresses only to
        registered devices.

        From the pod's status (firmware after 3.10.7, LAN and USB: ``eth_mac``, ``wifi_mac``), else
        from what embeddedci.com stored for it (recorded when the pod shipped and refreshed on
        every connect; the cloud status is the short form, without them). Pass a ``status`` already
        read to save a round trip. Fields are None when neither knows them.
        """
        st = dict(status) if status is not None else {}
        if status is None:
            try:
                st = self.status()
            except BenchPodError:
                st = {}
        eth = _mac_text(st.get("eth_mac"))
        if eth or "wifi_mac" in st:
            return MacAddresses(eth_mac=eth, wifi_mac=_mac_text(st.get("wifi_mac")),
                                has_wifi="wifi_mac" in st, source="pod")
        api = self._try_server_api()
        if api is not None and self._device_name:
            try:
                params = api.device_parameters(self._device_name)
            except BenchPodError:
                params = {}
            eth, wifi = _mac_text(params.get("net.eth_mac")), _mac_text(params.get("net.wifi_mac"))
            if eth or wifi:
                return MacAddresses(eth_mac=eth, wifi_mac=wifi, source="server")
        return MacAddresses()

    @property
    def capabilities(self) -> Capabilities:
        """The device's resolved :class:`Capabilities` (ADC scaling, DAC replay depth, flags).

        Parsed from :meth:`status` and — when the SDK has server access — enriched with the
        fuller ``cap.*`` set the server holds (explicit calibration + replay depth). Cached after
        the first successful read; :meth:`refresh_capabilities` re-reads it. When the status read
        fails the result is not cached, so a passing glitch does not leave the session without
        any capability for good.
        """
        if self._caps is None:
            status_ok = True
            try:
                status = self._caps_status()
            except FirmwareError as exc:
                # A status too large for the cloud channel (BP0054): reading it again will not
                # help, so use the server's capabilities alone, and keep them.
                _log.warning("could not read the pod's status for its capabilities: %s", exc)
                status, status_ok = {}, exc.code == "BP0054"
            except BenchPodError as exc:
                _log.warning("could not read the pod's status for its capabilities: %s", exc)
                status, status_ok = {}, False
            if status_ok and status.get("short_form") and self._direct_network():
                status = self._full_status(status)
            caps = Capabilities.from_status(status)
            if caps.features_rev > _feature_map.revision():
                _log.warning(
                    "this pod's firmware has features this SDK does not know (features table "
                    "revision %d, the SDK has %d): update the SDK (pip install -U embeddedci) to use them",
                    caps.features_rev, _feature_map.revision())
            api = self._try_server_api()
            if api is not None and self._device_name:
                try:
                    params = api.device_parameters(self._device_name)
                    if params:
                        caps = caps.merge(Capabilities.from_parameters(params))
                except BenchPodError:
                    pass
            if not status_ok:
                if caps.api or caps.firmware_version:
                    self._check_client_api(caps)
                return caps
            self._caps = caps
        if self._caps.api or self._caps.firmware_version:   # nothing to check without a status
            self._check_client_api(self._caps)
        return self._caps

    def _check_client_api(self, caps: Capabilities) -> None:
        """Refuse a pod this SDK does not drive.

        :class:`FirmwareTooOldError` for firmware 3.x: its client API level is below
        :data:`~embeddedci.benchpod.feature_map.MIN_POD_API` (a pod that announces none counts as
        1; over the USB console, which announces none, its firmware version decides).
        :class:`ClientTooOldError` when the pod speaks a level above
        :data:`~embeddedci.benchpod.feature_map.CLIENT_API`, or takes none this low
        (``min_client_api``): its firmware changed a reply or a message in a way this SDK would
        misread."""
        fw = caps.firmware_version or "unknown"
        level = caps.client_api_level
        if not caps.api and self._console_only():
            major = _major_version(caps.firmware_version)
            level = _feature_map.MIN_POD_API if major is None or major >= 4 else 1
        if level < _feature_map.MIN_POD_API:
            raise FirmwareTooOldError(
                f"the pod runs firmware {fw} (client API {level}); this SDK (embeddedci "
                f"{_sdk_version()}) needs firmware {_feature_map.MIN_FIRMWARE} or later (client API "
                f"{_feature_map.MIN_POD_API}). Update the pod with the BenchPod CLI (`benchpod "
                f"flash-self`) or the firmware update in the web app, or keep this firmware and use "
                f"embeddedci 3.x (pip install 'embeddedci<4')",
                api=level, firmware_version=caps.firmware_version)
        if level <= _feature_map.CLIENT_API and caps.min_client_api <= _feature_map.CLIENT_API:
            return
        raise ClientTooOldError(
            f"this SDK is too old for the pod's firmware {fw}: the pod speaks client API "
            f"{level} (and takes {caps.min_client_api} or newer), this SDK (embeddedci "
            f"{_sdk_version()}) handles client API {_feature_map.CLIENT_API}; update the SDK (pip "
            f"install -U embeddedci)",
            client_api=_feature_map.CLIENT_API, api=level,
            min_client_api=caps.min_client_api, firmware_version=caps.firmware_version)

    def _console_only(self) -> bool:
        """The USB text console (no JSON API), which announces no client API level."""
        from .transport.serial import SerialTransport

        return isinstance(self._transport, SerialTransport)

    def _direct_network(self) -> bool:
        """A LAN or USB network connection: one that should get the pod's full status."""
        from .transport.tcp import TcpTransport
        return isinstance(self._transport, TcpTransport)

    def _full_status(self, status: Dict[str, Any]) -> Dict[str, Any]:
        """``status`` was the short form over a direct connection: the full form did not fit the
        connection whole (more than 2047 bytes, or the connection's send buffer was busy). Ask
        once more, and warn when it is still short: the ADC calibration and feature flags the
        short form leaves out then come from embeddedci.com when this computer is signed in,
        else from the board's defaults."""
        try:
            again = self._caps_status()
        except BenchPodError:
            again = status
        if not again.get("short_form"):
            return again
        _log.warning(
            "the pod sent its short status over the network: its full status did not fit the connection. "
            "The ADC calibration it leaves out comes from embeddedci.com when this computer is "
            "signed in, else from the board's defaults, so ADC readings can be off.")
        return again

    def refresh_capabilities(self) -> Capabilities:
        """Drop the cached capabilities and re-read them."""
        self._caps = None
        return self.capabilities

    def supports(self, feature: str) -> bool:
        """Whether this pod has ``feature``, a :class:`Capabilities` flag (``"spi_master"``,
        ``"capture_trigger"``, ``"can"``, ...) or a name in the features table
        (:mod:`~embeddedci.benchpod.feature_map`: ``"la_dir"``, ``"i2c_master"``, ``"diag"``, ...).
        Reads the capabilities once; ``False`` for a name this SDK does not know. The ``can_*``
        properties name the common ones."""
        return self.capabilities.has(feature)

    @property
    def can_gpio(self) -> bool:
        """LA pin modes and GPIO (:meth:`gpio`, :meth:`la_pins`, :meth:`set_gpio`): capability ``la_pins``."""
        return self.supports("la_pins")

    @property
    def can_trigger(self) -> bool:
        """Triggered captures (``trigger=``): capability ``capture_trigger``."""
        return self.supports("capture_trigger")

    @property
    def can_spi(self) -> bool:
        """The SPI master and SPI NOR flash programming (:meth:`open_spi`, :meth:`spi_flash`):
        capability ``spi_master``."""
        return self.supports("spi_master")

    @property
    def can_profile_power(self) -> bool:
        """Gap-free power profiles (:meth:`measure_power`): capability ``power_profile``."""
        return self.supports("power_profile")

    @property
    def can_calibrate(self) -> bool:
        """On-pod ADC calibration (:meth:`calibrate`): capability ``calibrate``."""
        return self.supports("calibrate")

    @property
    def can_current_out(self) -> bool:
        """The 4-20 mA output (:meth:`current_out`): capability ``current_out``."""
        return self.supports("current_out")

    @property
    def can_reset_target(self) -> bool:
        """The dedicated target-reset pin (:meth:`reset_target`, ``flash(nreset=True)``):
        capability ``nrst_pin``."""
        return self.supports("nrst_pin")

    @property
    def can_analog(self) -> bool:
        """The analog front end (ADC, DAC, 4-20 mA). ``False`` only on the digital-only board;
        firmware that does not say is taken as analog (every older board has it)."""
        return self.capabilities.analog is not False

    def _require_capability(self, flag: str, what: str) -> None:
        if not self.supports(flag):
            fw = self.capabilities.firmware_version or "unknown"
            if flag in _ANALOG_FLAGS and not self.can_analog:
                # The digital board drops these flags: newer firmware would not bring them back.
                raise UnsupportedFeatureError(
                    f"{what} need the analog front end: this is the digital-only board (no DAC, "
                    f"ADC or 4-20 mA)", feature="analog",
                    firmware_version=self.capabilities.firmware_version)
            raise UnsupportedFeatureError(
                f"{what} need newer pod firmware (the {flag!r} capability is missing; "
                f"this pod runs firmware {fw})",
                feature=flag, firmware_version=self.capabilities.firmware_version)

    def _require_gateware(self, version: int, what: str) -> None:
        """Refuse ahead when the pod reports gateware older than ``version``; when it reports none,
        the pod decides."""
        gw = self.capabilities.gateware
        if gw and gw < version:
            raise UnsupportedFeatureError(
                f"{what} need gateware v{version}+ (this pod runs v{gw}): update the pod's firmware",
                feature=f"gateware_v{version}", firmware_version=self.capabilities.firmware_version)

    # -- wiring profile -------------------------------------------------------

    @property
    def wiring(self) -> Wiring:
        """The bench's :class:`~embeddedci.benchpod.wiring.Wiring` profile: which DUT signal is on
        which LA channel, the target-power rail and the LA I/O voltage.

        Resolved once, first match wins:

        1. the ``wiring`` argument (or an assigned profile);
        2. the ``BENCHPOD_WIRING`` file;
        3. a profile stored on the pod: ``wiring_profile`` (or ``BENCHPOD_WIRING_PROFILE``), else the
           pod's default profile. ``source`` is then ``"pod"`` and :attr:`Wiring.profile_name` its
           name. A named profile the pod does not have raises. Firmware that stores no profiles
           skips this step;
        4. (firmware that stores no profiles) the profile stored on embeddedci.com for this pod (edited in the web UI's Wiring tab):
           for a cloud connection, the named device; for a LAN or USB connection, the device the
           pod reports it is registered as (``cloud_status``), when credentials are available
           (``api_key``/``BENCHPOD_API_KEY``, or the ``benchpod login`` session) and the device is
           on that account. ``source`` is then ``"server"``. Skipped with
           ``wiring_source="local"`` / ``BENCHPOD_WIRING_SOURCE=local``;
        5. nothing: :meth:`Wiring.undefined` (no pin wired, no I/O voltage, no power rail). A call
           that needs a pin then says which one to pass.

        The lookup in step 4 never raises: without credentials, with the pod not registered, the
        server unreachable or the device on another account, it logs why and goes on to step 5,
        and on a LAN/USB connection it gives up after about 3 seconds.
        Channel arguments fall back to it — ``open_uart()``, ``capture_uart()``, ``flash()``,
        ``bp.sensors.start()``, the power rail — and :meth:`signal` / GPIO names come from it.
        Assign a profile to use another one for this connection (not saved; see :meth:`save_wiring`,
        :meth:`load_wiring`).
        """
        if self._wiring is None:
            self._wiring = self._resolve_wiring()
        return self._wiring

    @wiring.setter
    def wiring(self, value: Union[Wiring, Mapping[str, Any], str, "os.PathLike[str]"]) -> None:
        self._wiring = Wiring.coerce(value)
        self._auto_pin_directions()

    def _resolve_wiring(self) -> Wiring:
        env = os.environ.get(WIRING_ENV, "").strip()
        if env:
            return Wiring.load(env)
        pod = self._pod_wiring(self._wiring_profile or None)
        if pod is not None:
            return pod
        if self._wiring_profile:
            raise UnsupportedFeatureError(
                f"wiring_profile={self._wiring_profile!r}: this pod's firmware does not store wiring "
                "profiles; update it (`benchpod flash-self`) or pass wiring=", feature="wiring_store")
        if self._wiring_source == "local":
            return Wiring.undefined()
        if not self._device_name:  # a LAN or USB pod
            return self._local_pod_server_wiring() or Wiring.undefined()
        api = self._try_server_api()
        if api is not None:
            try:
                data = api.wiring_profile(api.resolve_device_id(self._device_name))
                return Wiring.from_dict(data.get("profile") or {}, source="server", strict=False)
            except (BenchPodError, ValueError) as exc:
                _log.warning("could not load the wiring profile of %s from embeddedci.com (%s); "
                             "nothing is wired", self._device_name, exc)
        return Wiring.undefined()

    def _wiring_credential(self, deadline: float) -> Optional[Tuple[Optional[str], Optional[Callable[[], str]]]]:
        """``(api_key, token_provider)`` for the wiring lookup of a LAN/USB pod, or None without
        any: the API key, else the caller's user token, else the ``benchpod login`` session."""
        if self._api_key:
            return self._api_key.strip(), None
        if self._cloud_user_token is not None:
            return None, self._cloud_user_token
        from . import cli_login

        if not cli_login.has_session():
            return None
        from .cloud_auth import DEFAULT_API_BASE

        base = self._api_base or DEFAULT_API_BASE
        return None, lambda: cli_login.access_token(
            base, timeout=max(0.5, deadline - time.monotonic()))

    def _pod_cloud_device_id(self, deadline: float) -> str:
        """The embeddedci.com device id the pod says it is registered as (``cloud_status``), or
        "" when it is not registered. Asks with the lookup's short timeout."""
        cmd = getattr(self._transport, "command", None)
        if cmd is None:
            return ""
        budget = max(0.5, deadline - time.monotonic())
        saved = {}
        for attr in ("timeout", "dial_timeout"):
            value = getattr(self._transport, attr, None)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > budget:
                saved[attr] = value
                setattr(self._transport, attr, budget)
        try:
            data = _dict(cmd({"cmd": "cloud_status"}))
        finally:
            for attr, value in saved.items():
                setattr(self._transport, attr, value)
        if data.get("configured") is False:
            return ""
        return str(data.get("device_id") or "").strip()

    def _local_pod_server_wiring(self) -> Optional[Wiring]:
        """The profile stored on embeddedci.com for the device a LAN/USB pod is registered as, or
        None (logged) when there is no credential, no registration, or the lookup fails."""
        deadline = time.monotonic() + _WIRING_LOOKUP_TIMEOUT
        try:
            cred = self._wiring_credential(deadline)
            if cred is None:
                _log.info("wiring: no embeddedci.com credentials (BENCHPOD_API_KEY or `benchpod "
                          "login`), so the stored profile is not used; nothing is wired")
                return None
            try:
                device_id = self._pod_cloud_device_id(deadline)
            except BenchPodError as exc:
                # The USB console of current STM32 firmware has no cloud_status: expected, so quieter.
                level = logging.INFO if type(self._transport).__name__ == "SerialTransport" \
                    else logging.WARNING
                _log.log(level, "wiring: could not ask the pod which embeddedci.com device it is "
                         "(%s); nothing is wired", exc)
                return None
            if not device_id:
                _log.info("wiring: the pod is not registered on embeddedci.com; nothing is wired")
                return None
            from .server_api import ServerApi, ServerApiError

            api_key, token_provider = cred
            api = ServerApi(api_base=self._api_base, api_key=api_key, token_provider=token_provider,
                            timeout=max(0.5, deadline - time.monotonic()))
            try:
                data = api.wiring_profile(device_id)
            except ServerApiError as exc:
                if exc.status in (401, 403, 404):
                    _log.warning("wiring: the pod's embeddedci.com device %s is not on this account "
                                 "(HTTP %s); nothing is wired", device_id, exc.status)
                    return None
                raise
            return Wiring.from_dict(data.get("profile") or {}, source="server", strict=False)
        except Exception as exc:  # the lookup is a convenience: never fail the caller over it
            _log.warning("wiring: could not load the pod's profile from embeddedci.com (%s); "
                         "nothing is wired", exc)
            return None

    # -- wiring profiles stored on the pod (firmware wiring_store.h) --------------------------

    def _pod_cmd(self) -> Optional[Callable[[Dict[str, Any]], Any]]:
        return getattr(self._transport, "command", None)

    def _pod_wiring_list(self) -> Optional[Dict[str, Any]]:
        """The pod's ``wiring_list``, or None when its firmware stores no profiles."""
        cmd = self._pod_cmd()
        if cmd is None:
            return None
        try:
            listing = _dict(cmd({"cmd": "wiring_list"}))
            return listing if isinstance(listing.get("profiles"), list) else None
        except FirmwareError:
            return None   # a pod that refuses the listing: no stored profile to use
        except BenchPodError as exc:
            if type(self._transport).__name__ == "SerialTransport":
                return None   # the USB console runs no JSON commands
            raise exc

    def _pod_wiring_get(self, name: str) -> Tuple[Dict[str, Any], int]:
        """One profile's JSON from the pod, read in pieces; a save meanwhile starts over."""
        cmd = self._pod_cmd()
        assert cmd is not None
        for _ in range(3):
            buf, rev = b"", None
            while True:
                piece = _dict(cmd({"cmd": "wiring_get", "name": name, "offset": len(buf)}))
                if rev is not None and piece.get("rev") != rev:
                    break
                rev = int(piece.get("rev", 0))
                b64 = str(piece.get("data_b64", ""))
                chunk = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
                buf += chunk
                if not chunk or len(buf) >= int(piece.get("total", 0)):
                    return json.loads(buf.decode("utf-8")), rev
        raise BenchPodError(f"wiring profile {name!r} kept changing while it was read; try again")

    def _pod_wiring(self, name: Optional[str]) -> Optional[Wiring]:
        """The pod's profile ``name`` (None: its default) as a Wiring, :meth:`Wiring.undefined` when
        the pod has no profiles yet, or None when its firmware stores none."""
        listing = self._pod_wiring_list()
        if listing is None:
            return None
        names = [str(p.get("name")) for p in listing.get("profiles") or []]
        target = name or listing.get("default")
        if not target:
            return replace(Wiring.undefined(), source="pod")
        if target not in names:
            raise FirmwareError(
                f"the pod has no wiring profile {target!r} (it has: {', '.join(names) or 'none'})",
                cmd="wiring_get")
        data, rev = self._pod_wiring_get(str(target))
        prof = Wiring.from_dict(data, source="pod", strict=False)
        return replace(prof, source="pod", profile_name=str(target), profile_rev=rev)

    def wiring_profiles(self) -> Dict[str, Any]:
        """The wiring profiles stored on the pod: ``{"default": "k66", "profiles": [{"name": "k66",
        "rev": 3, "bytes": 812}, ...], "max_profiles": 16, "max_bytes": 4096, "free_bytes": ...}``.
        Raises :class:`UnsupportedFeatureError` on firmware that stores none."""
        listing = self._pod_wiring_list()
        if listing is None:
            raise UnsupportedFeatureError("this pod's firmware does not store wiring profiles; update it "
                                          "(`benchpod flash-self`)", feature="wiring_store")
        return listing

    def stored_wiring(self, name: Optional[str] = None) -> Optional[Wiring]:
        """The pod's stored profile ``name`` (None: its default) without switching this connection to
        it; None when the pod has no such profile or its firmware stores none."""
        try:
            return self._pod_wiring(name)
        except FirmwareError:
            return None

    def load_wiring(self, name: Optional[str] = None) -> Wiring:
        """Switch this connection to the pod's profile ``name`` (None: the pod's default) and return
        it. Nothing on the pod changes; :meth:`set_default_wiring` changes the default."""
        prof = self._pod_wiring(name)
        if prof is None:
            raise UnsupportedFeatureError("this pod's firmware does not store wiring profiles; update it "
                                          "(`benchpod flash-self`)", feature="wiring_store")
        changed = prof != self._wiring
        self._wiring = prof
        if changed:
            self._auto_pin_directions()
        return prof

    def save_wiring(self, wiring: Union[Wiring, Mapping[str, Any], str, None] = None, *,
                    name: Optional[str] = None) -> Wiring:
        """Store a wiring profile on the pod and use it on this connection.

        ``wiring``: the profile to store (a :class:`Wiring`, a dict or a ``.json``/``.toml`` path;
        default: :attr:`wiring`). ``name``: the pod profile to store it as; default: the profile this
        connection loaded (:attr:`Wiring.profile_name`), else the pod's default, else ``"default"``.
        Saving to the profile this connection loaded checks that nobody saved it meanwhile and raises
        :class:`FirmwareError` ``BP0009`` when someone did, instead of overwriting their change.
        Saving never changes which profile is the pod's default (:meth:`set_default_wiring`),
        except that the pod's first profile becomes its default.

        Works over every connection (LAN, USB, cloud), signed in or not. Pods whose firmware stores
        no profiles: a cloud device's single profile on embeddedci.com is replaced (``name`` must
        not be given); a LAN or USB pod raises (update the firmware).
        """
        profile = self.wiring if wiring is None else Wiring.coerce(wiring)
        listing = self._pod_wiring_list()
        if listing is None:
            return self._save_server_wiring(profile, name)
        loaded = self._wiring.profile_name if self._wiring is not None else ""
        target = name or loaded or listing.get("default") or "default"
        if_rev: Optional[int] = None
        if target == loaded and self._wiring is not None:
            if_rev = self._wiring.profile_rev
        data = json.dumps(profile.to_dict(), separators=(",", ":")).encode("utf-8")
        cmd = self._pod_cmd()
        assert cmd is not None
        done: Dict[str, Any] = {}
        with _classified_errors():
            for off in range(0, len(data), _WIRING_PIECE):
                req: Dict[str, Any] = {"cmd": "wiring_put", "name": target, "offset": off, "total": len(data),
                                       "data_b64": base64.urlsafe_b64encode(data[off:off + _WIRING_PIECE])
                                       .decode("ascii").rstrip("=")}
                if off == 0 and if_rev is not None:
                    req["if_revision"] = if_rev
                done = _dict(cmd(req))
        saved = replace(profile, source="pod", profile_name=target, profile_rev=int(done.get("rev", 0)))
        changed = saved != self._wiring
        self._wiring = saved
        if changed:
            self._auto_pin_directions()
        return saved

    def set_default_wiring(self, name: str) -> str:
        """Make the pod's profile ``name`` the default: what a connection loads when it names none.
        This connection keeps the profile it has (:meth:`load_wiring` switches)."""
        cmd = self._pod_cmd()
        if cmd is None or self._pod_wiring_list() is None:
            raise UnsupportedFeatureError("this pod's firmware does not store wiring profiles; update it "
                                          "(`benchpod flash-self`)", feature="wiring_store")
        with _classified_errors():
            return str(_dict(cmd({"cmd": "wiring_default", "name": name})).get("default", name))

    def delete_wiring(self, name: str) -> None:
        """Delete the pod's profile ``name`` (not the default: make another one the default first)."""
        cmd = self._pod_cmd()
        if cmd is None or self._pod_wiring_list() is None:
            raise UnsupportedFeatureError("this pod's firmware does not store wiring profiles; update it "
                                          "(`benchpod flash-self`)", feature="wiring_store")
        with _classified_errors():
            cmd({"cmd": "wiring_delete", "name": name})

    def _save_server_wiring(self, profile: Wiring, name: Optional[str]) -> Wiring:
        """Firmware without profile storage: the cloud device's single profile on embeddedci.com."""
        if name:
            raise UnsupportedFeatureError(f"save_wiring(name={name!r}): this pod's firmware keeps one wiring "
                                          "profile; update it (`benchpod flash-self`) to store several",
                                          feature="wiring_store")
        if not self._device_name:
            raise UnsupportedFeatureError("saving a wiring profile on this pod needs newer firmware "
                                          "(`benchpod flash-self`), or a cloud connection "
                                          "('embeddedci:<device>')", feature="wiring_store")
        api = self._require_server_api()
        api.put_wiring(api.resolve_device_id(self._device_name), profile.to_dict())
        changed = profile != self._wiring
        self._wiring = profile.with_changes(source="server")
        if changed:
            self._auto_pin_directions()
        return self._wiring

    def signal(self, name: str) -> GpioPin:
        """A :class:`~embeddedci.benchpod.gpio.GpioPin` for a named signal (or a role such as
        ``"uart_tx"``) of the wiring profile, or an :class:`~embeddedci.benchpod.gpio.ExtPin` for a
        named expander pin. Nothing is sent to the pod until you use it."""
        return self._gpio_pin(self._pin_refs(name)[0])

    def _resolve_la(self, value: Any, name: str = "la", why: str = "") -> Tuple[int, Optional[Signal]]:
        """An LA channel from 1-14 / a ``Pin`` / a wiring-profile name (role or signal). An
        expander pin is refused: ``why`` says why this call needs an LA channel."""
        if isinstance(value, str):
            n = self._ext(value)
            if n is not None:
                exp = self._require_expansion()
                reason = why or _EXT_REFUSALS.get(name, "this call works on LA channels 1-14 only")
                raise _expansion.ExpansionError(
                    f"{value} is an expander pin (EXT{n}, {exp.chip.name} pin "
                    f"{exp.pin_state(n).chip_pin}), not an LA channel: {reason}")
            profile = self.wiring
            la = profile.la(value)
            key = value.strip().lower()
            return la, next((s for s in profile.signals if s.name.lower() == key), None)
        return coerce_pin(value, name), None

    def _wired(self, value: Any, key: str) -> int:
        """``value`` as an LA channel, or the wiring profile's ``key`` when ``value`` is None."""
        if value is not None:
            return self._resolve_la(value, key)[0]
        la = getattr(self.wiring, key)
        if la is None:
            raise ValueError(f"{key} was not given and the wiring profile has no {key}; pass it or set "
                             "it in the profile")
        return la

    def _efuse(self, efuse: Optional[Union[Efuse, int]]) -> int:
        if efuse is None and self.wiring.efuse is None:
            raise ValueError("efuse was not given and the wiring profile sets no power rail: pass efuse=1 "
                             "(internal 5 V) or efuse=2 (external supply), or set efuse in the profile")
        return coerce_efuse(self.wiring.efuse if efuse is None else efuse)

    # -- LA I/O-bank voltage ------------------------------------------------

    def set_la_voltage(self, voltage: float) -> LaVoltage:
        """Select the LA I/O-bank voltage: ``1.8`` or ``3.3`` volts (1.8 V needs a rev3 pod), or
        ``5.0`` on a pod with the digital board v4 (feature ``la_5v``).

        At 5.0 the bank stays at 3.3 V, J1 pin 21 (the pull-up rail) goes to 5 V, and outputs
        reach 5 V through their pull-ups: https://embeddedci.com/docs/benchpod-targets/5v-rs485#5v-io
        Raises :class:`~embeddedci.benchpod.errors.FirmwareError` ``BP0028`` when 5 V did not come
        up. :attr:`LaVoltage.pin21` reports pin 21.

        Match it to the DUT's I/O voltage. It must be set before any LA-bank operation (flashing,
        the UART proxy, LA capture, pull resistors, sensor emulation), or the pod refuses them with
        "la voltage not set". Usually it comes from the wiring profile (``la_mv``): :class:`BenchPod`
        sets it on connect, from ``la_voltage`` / ``BENCHPOD_LA_VOLTAGE`` when given, else from the
        profile, and firmware 3.10.10+ takes it from the profile on the first LA operation.
        """
        v = float(voltage)
        if abs(v - LA_VOLTAGE_5V) < 0.05:
            # 5 V I/O (digital v4): the pod keeps the bank at 3.3 V and moves pin 21 to 5 V.
            if not self.supports("la_5v"):
                raise ValueError("5 V I/O needs a BenchPod with the digital board v4 or later and "
                                 "firmware that has it (feature la_5v): use 1.8 or 3.3")
            with _classified_errors():
                return LaVoltage.from_reply(self.command({"cmd": "la_voltage", "mv": 5000}))
        match = next((ok for ok in LA_VOLTAGES if abs(v - ok) < 0.05), None)
        if match is None:
            five = " or 5.0 (this pod has 5 V I/O)" if self._has_5v() else ""
            raise ValueError(f"la voltage must be 1.8 or 3.3{five} (volts), got {voltage!r}")
        return LaVoltage.from_reply(self.command({"cmd": "la_voltage", "mv": int(round(match * 1000))}))

    def _has_5v(self) -> bool:
        try:
            return self.supports("la_5v")
        except BenchPodError:
            return False

    def get_la_voltage(self) -> LaVoltage:
        """The current LA I/O-bank voltage (``voltage`` is ``None`` until one is selected)."""
        return LaVoltage.from_reply(self.command({"cmd": "la_voltage"}))

    # -- power --------------------------------------------------------------

    def target_power(self, efuse: Optional[Union[Efuse, int]] = None, *,
                     on: bool, delay: Optional[float] = None) -> None:
        """Enable or disable a target-power eFuse (omitted: the wiring profile's rail, INTERNAL by
        default).

        ``delay`` (seconds) schedules the change pod-side and returns immediately — handy to
        power on *during* a UART capture.
        """
        self._transport.target_power(self._efuse(efuse), bool(on), _seconds_to_ms(delay, "delay"))

    def j1_power(self, rail: str, *, on: bool) -> None:
        """Switch a supply pin on J1 of the digital board v4 (feature ``dut_rails``): ``"3v3"``
        is pin 17 (+3.3 V, current limited to about 1.45 A) and ``"1v8"`` pin 18 (+1.8 V).

        Both are on from boot, so a DUT powered from J1 works as on older boards, and they are
        independent of the target-power eFuses and of resets: :meth:`power_off` and
        :meth:`reset_target` leave them as they are. :attr:`TargetStatus.j1_rails` reports them.
        Older boards have both pins always on: raises :class:`ValueError` there.
        """
        name = {"3v3": "j1_3v3", "1v8": "j1_1v8"}.get(str(rail).lower())
        if name is None:
            raise ValueError(f"rail must be '3v3' (J1 pin 17) or '1v8' (J1 pin 18), got {rail!r}")
        if not self.supports("dut_rails"):
            raise ValueError("this BenchPod has no switchable J1 supplies (digital board v4 with "
                             "firmware that has feature dut_rails): pins 17 and 18 are always on")
        with _classified_errors():
            self.command({"cmd": "target_power", "rail": name, "enabled": bool(on)})

    def power_on(self, efuse: Optional[Union[Efuse, int]] = None,
                 *, delay: Optional[float] = None, wiring_profile: Optional[str] = None) -> None:
        """Power the target on with its wiring profile applied first.

        In order: ``wiring_profile`` (a profile stored on the pod) becomes this connection's
        profile (:meth:`load_wiring`); the profile's I/O voltage is set when the pod has none or
        another one; its pull resistors (:attr:`Wiring.pulls`) are switched on and the others off;
        then the rail goes on (omitted ``efuse``: the profile's rail), and the pod
        records which profile powered the board (``target_status``). The I/O voltage is never
        changed under a board that is already powered: that raises, set it with
        :meth:`set_la_voltage` yourself if you mean it.
        """
        if wiring_profile:
            self.load_wiring(wiring_profile)
        w = self.wiring
        rail = self._efuse(efuse)
        if w.la_voltage is not None:
            current = self.get_la_voltage().voltage
            if current is None or abs(current - w.la_voltage) > 0.05:
                if current is not None and self._rail_on(rail):
                    raise BenchPodError(
                        f"the board is powered with I/O at {current:g} V and the wiring profile wants "
                        f"{w.la_voltage:g} V: power it off first, or call set_la_voltage() if you mean it")
                self.set_la_voltage(w.la_voltage)
                _log.info("power_on: I/O set to %g V from the wiring profile %s", w.la_voltage,
                          w.profile_name or "(this connection's)")
        self._apply_profile_pulls(w)
        cmd = self._pod_cmd()
        if w.profile_name and cmd is not None and type(self._transport).__name__ != "SerialTransport":
            req: Dict[str, Any] = {"cmd": "target_power", "efuse": rail, "enabled": True,
                                   "delay_ms": _seconds_to_ms(delay, "delay"), "profile": w.profile_name}
            with _classified_errors():
                cmd(req)
            return
        self.target_power(rail, on=True, delay=delay)

    def _apply_profile_pulls(self, w: Wiring) -> None:
        """Switch the pull resistors the profile lists on and the others off (``pulls`` None: leave
        them). Firmware before ``la_pull_1v8`` has them at 3.3 V only (at 1.8 V they are left
        off), and the console (serial) transport can't set them."""
        if w.pulls is None or type(self._transport).__name__ == "SerialTransport":
            return
        volts = w.la_voltage if w.la_voltage is not None else self.get_la_voltage().voltage
        if not self._pulls_at(volts):
            if volts is not None:
                _log.warning("power_on: this firmware has no pull resistors at %g V; the profile's "
                             "pulls stay off (update the firmware)", volts)
            return
        wanted = w.pull_directions()
        for la in PULL_TABLE:
            self.set_pull(la, wanted.get(la, "off"))
        _log.info("power_on: pull resistors from the wiring profile: %s",
                  ", ".join(f"LA{la} {d}" for la, d in sorted(wanted.items())) or "none")

    def _rail_on(self, rail: int) -> bool:
        try:
            return bool(self.target_status().efuse(rail).enabled)
        except BenchPodError:
            return False

    def power_off(self, efuse: Optional[Union[Efuse, int]] = None,
                  *, delay: Optional[float] = None) -> None:
        """Power the target off."""
        self.target_power(efuse, on=False, delay=delay)

    def target_status(self) -> TargetStatus:
        """Both eFuse rails: enabled, tripped (``fault``), and whether the state was readable.
        On the digital board v4 also ``j1_rails``: whether J1 pins 17 and 18 are on."""
        return TargetStatus.from_reply(self.command({"cmd": "target_status"}))

    def power_status(self) -> PowerStatus:
        """The INA power monitors: bus voltage and current on each target-power rail."""
        return PowerStatus.from_reply(self.command({"cmd": "power_status"}))

    def reset_target(self, *, pulse: float = 0.1) -> ResetState:
        """Pulse the target's reset line for ``pulse`` seconds (rev3 pods, DUT header J1 pin 22).

        Resets the DUT without power-cycling it. The pod times the pulse and replies once the line
        is released, so the DUT is already running again when this returns. At most 1 s.
        """
        if not 0 < pulse <= 1.0:
            raise ValueError(f"pulse must be > 0 and at most 1 second, got {pulse!r}")
        ms = max(1, int(round(pulse * 1000)))
        return ResetState.from_reply(self.command({"cmd": "nrst", "pulse_ms": ms}))

    def set_reset(self, asserted: bool) -> ResetState:
        """Hold the target in reset (``True``) or release it (``False``) — rev3 pods."""
        return ResetState.from_reply(self.command({"cmd": "nrst", "assert": bool(asserted)}))

    def reset_state(self) -> ResetState:
        """Whether the pod is currently holding the target in reset."""
        return ResetState.from_reply(self.command({"cmd": "nrst"}))

    def usb_cc(self) -> UsbCcStatus:
        """The pod's USB-C cable orientation and the current the upstream source advertises (rev3)."""
        return UsbCcStatus.from_reply(self.command({"cmd": "usb_cc"}))

    def usb_network(self, on: Optional[bool] = None) -> bool:
        """The pod's USB network setting (firmware 3.9.0+): whether its USB port is also a network
        adapter. Returns the stored setting (``True`` = on).

        ``on=False`` turns the adapter off. Only the device may change it: this works over the
        pod's USB network link itself (connection ``usb``); over Ethernet, Wi-Fi or the cloud the
        pod refuses with :class:`PodLockedError`. The pod replies, then re-enumerates as its USB
        console only, which ends this connection. Turning it back on needs the USB console:
        ``benchpod usb-network on``.
        """
        req: Dict[str, Any] = {"cmd": "usb_network"}
        if on is not None:
            req["enabled"] = bool(on)
        data = self.command(req)
        return isinstance(data, dict) and data.get("enabled") is True

    # -- USB host mode and USB flashing (firmware 4.0.3+) --------------------------------

    def _require_usb(self, feature: str, what: str) -> None:
        """Refuse ahead, in words, when the pod's firmware lacks the USB ``feature``."""
        if self.supports(feature):
            return
        fw = self.capabilities.firmware_version or "unknown"
        first = _feature_map.first_firmware(feature) or "4.0.3"
        raise UnsupportedFeatureError(
            f"{what} needs firmware {first} or later (this pod runs firmware {fw}): update the pod",
            feature=feature, firmware_version=self.capabilities.firmware_version)

    @staticmethod
    def _has_usb_a(board_rev: str) -> Optional[bool]:
        """Whether a ``status`` ``board_rev`` (``"v4"``) has the USB-A host port (digital v4 and
        later); ``None`` when the pod does not say."""
        # "v3r4" = the pre-rename name of v4 (firmware 4.0.x); both count.
        m = re.fullmatch(r"v(\d+)(?:r(\d+))?", str(board_rev or "").strip().lower())
        return None if m is None else (int(m.group(1)), int(m.group(2) or 0)) >= (3, 4)

    def usb_host_status(self) -> "_usb.UsbHostState":
        """The pod's USB host state: ``mode`` (``device``, ``starting``, ``host``, ``stopping``),
        the ``port``, whether a device is ``attached``, how far its ``enumeration`` got,
        ``connected`` and ``line_speed`` (a board that draws power but is not ``connected`` has
        no D+/D- link to the pod) and the ``device`` that enumerated last with its configuration
        and interfaces. Firmware 4.0.3 and later (feature ``usb_host_mode``)."""
        self._require_usb("usb_host_mode", "USB host mode")
        with _classified_errors():
            return _usb.UsbHostState.from_reply(self.command({"cmd": "usb_host"}))

    def usb_host(self, enable: bool = True, *, port: Optional[str] = None,
                 timeout_s: int = 60) -> "_usb.UsbHostState":
        """Start (``enable=True``) or stop USB host mode and return the :class:`UsbHostState`.

        In host mode the pod's USB port becomes a full-speed host for the board under test, for
        ``timeout_s`` seconds (1 to 3600; starting again renews it), and reports the device that
        enumerates (:meth:`usb_host_status`, :meth:`usb_wait_device`). The pod then is **off the
        computer's USB** (no USB console, no USB network adapter): start it over the LAN or the
        cloud; over the pod's own USB link it raises
        :class:`~embeddedci.benchpod.errors.UsbHostUnavailableError` (``BP0076``). When the timeout
        runs out, or ``usb_host(False)`` is called, the pod is a USB device again.

        ``port``: ``"usb_a"`` (the USB-A host port of digital v4 and later) or ``"usb_c"`` (the
        pod's own USB-C connector through a breakout, on any pod). Left out, the SDK picks
        ``"usb_a"`` on a board that has the port (``board_rev`` v4 or later), ``"usb_c"`` on an
        older one, and tries ``"usb_a"`` first (falling back to ``"usb_c"``) when the pod does not
        say. Waits until the pod is in host mode; raises
        :class:`~embeddedci.benchpod.errors.UsbTimeout` when it is not within 10 s.
        """
        self._require_usb("usb_host_mode", "USB host mode")
        with _classified_errors():
            if not enable:
                self.command({"cmd": "usb_host", "enabled": False})
                return self._usb_wait_mode(("device",))
            if not 1 <= int(timeout_s) <= 3600:
                raise ValueError(f"timeout_s must be 1 to 3600 seconds, got {timeout_s!r}")
            if port is not None:
                check_choice(port, ("usb_a", "usb_c"), "port")
                ports: Tuple[str, ...] = (port,)
            else:
                has_a = self._has_usb_a(self.capabilities.board_rev)
                ports = ("usb_a", "usb_c") if has_a is None else (("usb_a",) if has_a else ("usb_c",))
            last: Optional[BaseException] = None
            for i, p in enumerate(ports):
                try:
                    self.command({"cmd": "usb_host", "enabled": True, "port": p, "timeout_s": int(timeout_s)})
                    break
                except FirmwareError as exc:
                    last = classify_firmware_error(exc)
                    # No such port (BP0077), or a pod that asks for the port (BP0006): next port.
                    if i + 1 < len(ports) and (isinstance(last, UsbHostUnavailableError)
                                               and last.code == "BP0077" or last.code == "BP0006"):
                        continue
                    raise last from None
            else:  # pragma: no cover - the loop breaks or raises
                raise last  # type: ignore[misc]
            return self._usb_wait_mode(("host",))

    def _usb_wait_mode(self, modes: Tuple[str, ...], timeout: float = 10.0) -> "_usb.UsbHostState":
        """Poll the host state until its mode is one of ``modes``."""
        deadline = time.monotonic() + timeout
        while True:
            st = _usb.UsbHostState.from_reply(self.command({"cmd": "usb_host"}))
            if st.mode in modes:
                return st
            if time.monotonic() >= deadline:
                raise UsbTimeout(f"USB host mode did not reach {modes[0]!r} within {timeout:g} s "
                                 f"(it is {st.mode!r})")
            time.sleep(_usb.POLL_INTERVAL)

    def usb_wait_device(self, vid: Union[int, str, None] = None, pid: Union[int, str, None] = None,
                        timeout: float = 10.0) -> "_usb.UsbDevice":
        """Wait until a USB device is mounted on the pod's host port and return its
        :class:`UsbDevice` (IDs, strings, speed, configuration, interfaces).

        ``vid`` / ``pid`` (an int, or hex text like ``"16c0"``) wait for that device only. Raises
        :class:`~embeddedci.benchpod.errors.UsbHostError` when host mode is off (:meth:`usb_host`
        starts it) and :class:`~embeddedci.benchpod.errors.UsbTimeout` after ``timeout`` seconds;
        its message says how far the board got (no pull-up, wrong speed, a stuck enumeration).
        """
        v, p = _usb._parse_id(vid, "vid"), _usb._parse_id(pid, "pid")
        return _usb.wait_device(self.usb_host_status, vid=v, pid=p, timeout=timeout)

    def usb_flash_status(self) -> "_usb.UsbFlashState":
        """The running USB flash, or the last one's outcome (:class:`UsbFlashState`)."""
        self._require_usb("usb_flash", "USB flashing")
        with _classified_errors():
            return _usb.UsbFlashState.from_reply(self.command({"cmd": "usb_flash"}))

    def usb_flash_abort(self) -> "_usb.UsbFlashState":
        """Stop a running USB flash and return its state (``result`` is ``"aborted"``)."""
        self._require_usb("usb_flash", "USB flashing")
        with _classified_errors():
            return _usb.UsbFlashState.from_reply(self.command({"cmd": "usb_flash", "abort": True}))

    def usb_flash(
        self,
        file: Union[str, "os.PathLike[str]", bytes],
        *,
        address: Union[int, str, None] = None,
        name: Optional[str] = None,
        leave: bool = True,
        mode: Optional[str] = None,
        port: Optional[str] = None,
        timeout: Optional[float] = None,
        device_timeout: float = 10.0,
        progress: Optional[Callable[[int, int], None]] = None,
        verify: bool = False,
    ) -> "_usb.UsbFlashResult":
        """Flash the board on the pod's USB host port from ``file`` and return a
        :class:`UsbFlashResult` (a failure raises :class:`~embeddedci.benchpod.errors.UsbFlashError`
        with the pod's ``result``). Firmware 4.0.3 and later (feature ``usb_flash``); works over
        the LAN and the cloud.

        The SDK picks the loader from the file and the device that mounted, so there is nothing
        to choose (``result.reason`` says why):

        - a Teensy loader (16C0:0478): ``teensy``; a DFU interface (STM32 ROM loader, Black Pill):
          ``dfu``, at the address in the ``.hex`` / ``.dfu`` file, else 0x08000000 on an STM32;
        - a drive (Pico, Adafruit, tinyuf2, DAPLink, micro:bit): ``uf2`` for a ``.uf2`` file, else
          ``file``, copied onto the drive as ``name`` (default: the file's name in 8.3);
        - an Arduino or Adafruit AVR board, or a USB-serial chip with a file that is not an ESP
          image: ``avr``; an Arduino, Adafruit or Seeed SAMD board: ``samd`` (address from the
          file, else where a sketch starts: 0x2000, 0x4000 on a SAMD51);
        - an Espressif device, or a USB-serial chip with an ESP image (starts with 0xE9): ``esp``,
          at ``address`` (default 0, a merged image);
        - a Raspberry Pi Pico (RP2040) or Pico 2 (RP2350) in BOOTSEL with a ``.bin`` or ``.hex``
          file: ``picoboot`` (feature ``usb_flash_picoboot``), at ``address`` (default
          0x10000000): erase, write, read every page back and compare, then boot. A ``.uf2`` file
          goes to the UF2 drive as for any board.

        ``.hex`` and ``.dfu`` files are converted to a binary with their start address (gaps up to
        64 KB filled with 0xFF); a ``.bin`` takes ``address`` (``int`` or ``"0x2000"``). ``leave``
        ``False`` keeps a ``dfu``/``teensy``/``avr``/``samd``/``esp`` board in its loader at the
        end. ``mode`` overrides the choice (one of :data:`~embeddedci.benchpod.usb_host.MODES`).
        ``verify=True`` reads the drive back after a ``file`` or ``uf2`` flash (feature
        ``usb_flash_readback``): the loader's ``FAIL.TXT`` is reported as the failure (a DAPLink
        board says why it refused the file) and a UF2 file is compared with the loader's
        ``CURRENT.UF2`` while the board stays in it; ``result.note`` says what was found, or that the
        check was skipped. ``picoboot`` always reads every page back. A ``file`` flash also sends
        the computer's clock, so the file on the drive has today's date.

        Starts host mode itself when it is off (``port`` as in :meth:`usb_host`), waits up to
        ``device_timeout`` s for a device, switches the pod to the deep-replay gateware image the
        way ``flash()`` of an ESP32 does (~3 s), stages the file in the pod's PSRAM and polls the
        flash. ``progress(done, total)`` is called as it goes (sectors, blocks or bytes, by mode).
        The flash is aborted after ``timeout`` seconds (default: a minute plus the file size at
        2.5 KB/s) or on Ctrl-C. While it runs the pod refuses captures, DAC playback and uploads.
        Host mode stays on afterwards (it ends by its own timeout, or ``usb_host(False)``).
        """
        self._require_usb("usb_flash", "USB flashing")
        self._require_usb("usb_host_mode", "USB host mode")
        image = _usb.load_image(file)
        addr = None if address is None else _esp.parse_address(address)
        stage = getattr(self._transport, "stage_psram", None)
        if stage is None:
            raise FlashError("flashing over USB needs a network, USB or cloud connection (the file "
                             "is uploaded into the pod's memory first), not the serial console")
        self._yield_expansion_bus()
        st = self.usb_host_status()
        if not st.hosting or st.remaining_s < _usb.MIN_HOST_REMAINING_S:
            st = self.usb_host(True, port=port if port is not None else (st.port if st.hosting else None),
                               timeout_s=_usb.FLASH_HOST_TIMEOUT_S)
        device = self.usb_wait_device(timeout=device_timeout)
        plan = _usb.choose_mode(image, device, address=addr, mode=mode)
        spec = _usb.MODES[plan.mode]
        if plan.mode in _usb.MODE_FEATURES:
            self._require_usb(_usb.MODE_FEATURES[plan.mode], f"USB flashing with {plan.mode}")
        if spec.binary:
            data, _file_addr = image.binary()
        else:
            data = image.raw
        req: Dict[str, Any] = {"cmd": "usb_flash", "mode": plan.mode, "len": len(data)}
        if plan.address is not None:
            req["address"] = plan.address
        if plan.mode == "file":
            req["name"] = name or _usb.fat_name(image.path or "firmware.bin")
        if spec.leave and not leave:
            req["leave"] = False
        if verify and plan.mode in ("file", "uf2"):
            self._require_usb("usb_flash_readback", "Reading the drive back after a USB flash")
            req["check_fail_txt" if plan.mode == "file" else "verify"] = True
        if plan.mode == "file" and self.supports("usb_flash_readback"):
            req["time"] = int(time.time())   # the pod has no clock: dates the file on the drive
        # The file is staged in PSRAM, which only the deep-replay gateware image writes.
        self._ensure_image(FpgaImage.DEEP_REPLAY, needed_for="USB flashing", switch=True)
        with _classified_errors():
            final = _usb.run_flash(self.command, stage, data, req,
                                   timeout=timeout if timeout is not None else _usb.flash_timeout(len(data)),
                                   progress=progress)
        return _usb.UsbFlashResult(
            ok=True, mode=plan.mode, reason=plan.reason, length=len(data), address=plan.address,
            name=str(req.get("name") or ""), protocol=final.protocol, ms=final.ms, device=device,
            state=final, note=final.note)

    # -- checks of the board's USB firmware (firmware 4.0.3+) ----------------------------

    def usb_check_status(self) -> "_usb.UsbCheckState":
        """The running USB check, or the last one's outcome (:class:`UsbCheckState`)."""
        self._require_usb("usb_check", "USB checks")
        with _classified_errors():
            return _usb.UsbCheckState.from_reply(self.command({"cmd": "usb_check"}))

    def usb_check_abort(self) -> "_usb.UsbCheckState":
        """Stop a running USB check and return its state (``error`` is ``"aborted"``). The pod puts
        eFuse 1 and the bus back as they were."""
        self._require_usb("usb_check", "USB checks")
        with _classified_errors():
            return _usb.UsbCheckState.from_reply(self.command({"cmd": "usb_check", "abort": True}))

    def usb_check(
        self,
        test: str,
        *,
        count: Optional[int] = None,
        off_ms: Union[int, Sequence[int], None] = None,
        timeout_ms: Optional[int] = None,
        duration_ms: Optional[int] = None,
        settle_ms: Optional[int] = None,
        port: Optional[str] = None,
        device_timeout: float = 10.0,
        timeout: Optional[float] = None,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> "_usb.UsbCheckState":
        """Check the USB firmware of the board on the pod's host port and return the
        :class:`UsbCheckState` once it ended (``passed`` is the verdict, ``result`` the numbers; a
        failing board is a result, not an exception). Firmware 4.0.3 and later (feature
        ``usb_check``); the board's VBUS must come from target-power eFuse 1 and eFuse 1 must be on
        (:meth:`power_on`), or the pod refuses with
        :class:`~embeddedci.benchpod.errors.UsbCheckNoPowerError` (``BP0088``; every test but
        ``reset``). ``test`` is one of :data:`~embeddedci.benchpod.usb_host.CHECKS`:

        - ``"enumerate"``: power-cycles the board ``count`` times (eFuse 1 off for ``off_ms``) and
          times power-on to pull-up, descriptor and mounted (``result["connect_ms"]`` and so on, each
          ``min``/``avg``/``max``);
        - ``"power"``: eFuse 1's current for ``duration_ms`` against the configuration's
          ``bMaxPower`` (``result["verdict"]``);
        - ``"suspend"``: suspends the bus (``settle_ms``, then ``duration_ms`` of measuring), checks
          the current against the 2.5 mA a bus-powered device may draw, resumes and checks the board
          still answers;
        - ``"reset"``: ``count`` port resets and the time to enumerate again;
        - ``"replug"``: VBUS dropouts of ``off_ms`` (one length or a list, ms) ``count`` times each:
          does the board come back (``result["lengths"]``).

        ``timeout_ms`` is how long each cycle waits for the board. Arguments left out take the pod's
        defaults, and the pod checks their ranges (a bad one is a
        :class:`~embeddedci.benchpod.errors.UsbCheckRefusedError`, ``BP0087``, as is a check with host
        mode off). Starts host mode itself when it is off (``port`` as in :meth:`usb_host`) and
        waits up to ``device_timeout`` s for the board. The check is aborted after ``timeout``
        seconds (default: its worst case plus a margin) or on Ctrl-C; ``progress(done, total)``
        counts cycles (milliseconds for ``power`` and ``suspend``). While it runs ``usb_flash`` and
        the USB serial port are refused. Host mode stays on afterwards.
        """
        self._require_usb("usb_check", "USB checks")
        self._require_usb("usb_host_mode", "USB host mode")
        if test not in _usb.CHECKS:
            raise ValueError(f"test must be one of {', '.join(_usb.CHECKS)}, got {test!r}")
        args: Dict[str, Any] = {}
        for key, value in (("count", count), ("timeout_ms", timeout_ms), ("duration_ms", duration_ms),
                           ("settle_ms", settle_ms)):
            if value is not None:
                args[key] = int(value)
        if off_ms is not None:
            args["off_ms"] = int(off_ms) if isinstance(off_ms, (int, float)) else [int(x) for x in off_ms]
        st = self.usb_host_status()
        if not st.hosting or st.remaining_s < _usb.MIN_HOST_REMAINING_S:
            st = self.usb_host(True, port=port if port is not None else (st.port if st.hosting else None),
                               timeout_s=_usb.FLASH_HOST_TIMEOUT_S)
        self.usb_wait_device(timeout=device_timeout)
        req: Dict[str, Any] = {"cmd": "usb_check", "test": test, **args}
        with _classified_errors():
            return _usb.run_check(self.command, req,
                                  timeout=timeout if timeout is not None else _usb.check_timeout(test, args),
                                  progress=progress)

    # -- HID interfaces and input reports (firmware 4.0.3+) ------------------------------

    def usb_hid_interfaces(self) -> "_usb.UsbHidState":
        """The HID interfaces of the board on the pod's host port (the first two), each with its
        whole report descriptor (:class:`UsbHidState`; the SDK reads a long one in pages). Firmware
        4.0.3 and later (feature ``usb_hid``). Refused when host mode is off, no HID interface is
        mounted, or a ``usb_flash`` or ``usb_check`` runs."""
        self._require_usb("usb_hid", "USB HID")
        with _classified_errors():
            st = _usb.UsbHidState.from_reply(self.command({"cmd": "usb_hid"}))
            full = []
            for itf in st.interfaces:
                data = bytearray(itf.descriptor)
                cut = itf.truncated
                # The descriptors share one reply: a long one is cut and read on with ``offset``.
                while cut and len(data) < itf.descriptor_len and len(data) < 4096:
                    more = _usb.UsbHidState.from_reply(
                        self.command({"cmd": "usb_hid", "instance": itf.instance, "offset": len(data)}))
                    page = next((i for i in more.interfaces if i.instance == itf.instance), None)
                    if page is None or not page.descriptor:
                        break
                    data += page.descriptor
                    cut = page.truncated
                full.append(dataclasses.replace(itf, descriptor=bytes(data),
                                                truncated=len(data) < itf.descriptor_len))
            return dataclasses.replace(st, interfaces=tuple(full))

    def usb_hid_capture(self, enable: bool = True) -> "_usb.UsbHidCapture":
        """Start (``enable=True``) or stop the capture of the board's HID input reports and return
        its state (:class:`UsbHidCapture`). Every report of both interfaces goes into one ring of
        64 on the pod, with its instance and time, until :meth:`usb_hid_read` drains it; stopping
        frees the ring. The capture ends by itself when the board detaches (a board that resets
        needs a new one) and when host mode ends. While it runs ``usb_flash`` and ``usb_check``
        are refused. Firmware 4.0.3 and later (feature ``usb_hid``)."""
        self._require_usb("usb_hid", "USB HID")
        with _classified_errors():
            return _usb.UsbHidCapture.from_reply(self.command({"cmd": "usb_hid", "capture": bool(enable)}))

    def usb_hid_read(self, count: int = 1, timeout: float = 2.0,
                     instance: Optional[int] = None) -> "_usb.UsbHidReports":
        """Take up to ``count`` input reports (1 to 32) from the capture ring, waiting up to
        ``timeout`` seconds (0.001 to 10) for one when it holds none, and return them
        (:class:`UsbHidReports`: ``reports`` with ``ms``, ``instance`` and ``data``; possibly none).
        ``instance`` takes only that interface's reports (0 or 1) and leaves the others in the ring.
        A read while no capture runs starts one and leaves it running; stop it with
        ``usb_hid_capture(False)``. ``capturing`` false in the result means the capture ended (the
        board detached or host mode ended). Firmware 4.0.3 and later (feature ``usb_hid``)."""
        self._require_usb("usb_hid", "USB HID")
        if isinstance(count, bool) or not 1 <= int(count) <= _usb.HID_MAX_COUNT:
            raise ValueError(f"count must be 1 to {_usb.HID_MAX_COUNT}, got {count!r}")
        if not 0 < float(timeout) <= _usb.HID_MAX_TIMEOUT_S:
            raise ValueError(f"timeout must be above 0 and at most {_usb.HID_MAX_TIMEOUT_S:g} seconds, got {timeout!r}")
        req: Dict[str, Any] = {"cmd": "usb_hid", "read": True, "count": int(count),
                               "timeout_ms": max(1, int(round(float(timeout) * 1000)))}
        if instance is not None:
            if isinstance(instance, bool) or instance not in (0, 1):
                raise ValueError(f"instance must be 0 or 1, got {instance!r}")
            req["instance"] = int(instance)
        with _classified_errors():
            return _usb.UsbHidReports.from_reply(self.command(req))

    # -- console log level --------------------------------------------------

    def log_level(self) -> str:
        """How much the pod prints on its USB console: ``"error"``, ``"warn"``, ``"info"`` (the
        default) or ``"debug"`` (adds the per-command trace and the retry detail of the cloud,
        Wi-Fi and DHCP loops). Firmware newer than 3.10.12 (feature ``log_level``); raises
        :class:`UnsupportedFeatureError` on older firmware, which prints everything."""
        self._require_capability("log_level", "Console log levels")
        return _log_level_from_reply(self.command({"cmd": "log_level"}))

    def set_log_level(self, level: str) -> str:
        """Set the console log level (one of :data:`LOG_LEVELS`) and return it. The pod stores it:
        it survives a reboot and a firmware update. It changes only console output, so any
        connection may set it."""
        name = str(level).strip().lower()
        if name not in LOG_LEVELS:
            raise ValueError(f"level must be one of {', '.join(LOG_LEVELS)}, got {level!r}")
        self._require_capability("log_level", "Console log levels")
        return _log_level_from_reply(self.command({"cmd": "log_level", "set": name}))

    # -- LAN TLS ------------------------------------------------------------

    def lan_tls(self) -> "LanTls":
        """The pod's LAN API over TLS (firmware 3.9.3+): port 8443, the HTTPS-only setting and the
        certificate in use (its SHA-256 is what clients pin). Raises
        :class:`UnsupportedFeatureError` on older firmware."""
        return LanTls.from_reply(self.command({"cmd": "lan_tls"}))

    # -- LAN pairing ----------------------------------------------------------

    def lan_clients(self, *, add: Optional[str] = None, spki_sha256: Optional[str] = None,
                    remove: Optional[str] = None, clear: bool = False) -> "LanClients":
        """The computers paired with the pod for use over Ethernet and Wi-Fi (firmware 4.0.1+).

        Over the LAN the pod answers only status to a computer that is not paired
        (:class:`PodNotPairedError`). ``add`` (a name) with ``spki_sha256`` pairs a key,
        ``remove`` unpairs the computers with that name or the one key starting with those hex
        digits (8 or more), ``clear`` unpairs all; each change needs USB (the console or the USB
        network) or the cloud (an organization owner or admin), never the LAN. Returns the list;
        :attr:`LanClient.this` marks this computer. To pair this computer, :meth:`pair`."""
        req: Dict[str, Any] = {"cmd": "lan_clients"}
        if add is not None:
            if not spki_sha256:
                raise ValueError("lan_clients(add=...) needs spki_sha256")
            req.update(add=client_identity.sanitize_name(add), spki_sha256=spki_sha256.lower())
        elif remove is not None:
            req["remove"] = str(remove)
        elif clear:
            req["clear"] = True
        data = self.command(req)
        mine = self._client_key_hash()
        clients = list((data or {}).get("clients") or [])
        total = int((data or {}).get("count") or 0)
        while 0 < len(clients) < total:   # the pod sends 12 a reply
            more = self.command({"cmd": "lan_clients", "offset": len(clients)})
            page = list((more or {}).get("clients") or [])
            if not page:
                break
            clients.extend(page)
        if isinstance(data, dict):
            data = dict(data, clients=clients)
        return LanClients.from_reply(data, mine)

    def pair(self, name: Optional[str] = None) -> "LanClients":
        """Pair this computer with the pod (firmware 4.0.1+), so it may drive the pod over Ethernet
        and Wi-Fi, not only read its status. Run it once over USB (``BenchPod("usb")``: the USB
        network adapter or the console) or the cloud (``"embeddedci:<device>"``, as an
        organization owner or admin); ``benchpod setup`` does the same. ``name`` defaults to this
        computer's host name. Over USB it also pins the pod's LAN certificate for its LAN address,
        so later LAN connections check they reach this very pod. Returns the list."""
        key = client_identity.client_key_hash()
        out = self.lan_clients(add=name or client_identity.pairing_name(), spki_sha256=key)
        self._pin_lan_certificate()
        return out

    def set_lan_pairing(self, on: bool) -> bool:
        """Turn LAN pairing on (the default) or off (every computer on the network may use the pod,
        as before firmware 4.0.1). Over USB or the cloud only. Returns the new setting."""
        data = self.command({"cmd": "lan_policy", "pairing": bool(on)})
        return bool((data or {}).get("pairing"))

    def _client_key_hash(self) -> str:
        try:
            return client_identity.client_key_hash()
        except Exception:
            return ""

    def _pin_lan_certificate(self) -> None:
        """After pairing over USB: pin the pod's certificate for its LAN address (best effort)."""
        try:
            sha = str((self.command({"cmd": "lan_tls"}) or {}).get("sha256") or "")
            ip = str((self._transport.status() or {}).get("ip") or "")
        except Exception:
            return
        if len(sha) == 64 and ip and not _tls.is_usb_link(ip):
            try:
                _tls.set_pin(ip, sha)
            except (OSError, ValueError):
                pass

    def set_lan_tls_https_only(self, on: bool) -> "LanTls":
        """Turn HTTPS-only on (plaintext 8080 refused on Ethernet and Wi-Fi; the USB link keeps it)
        or off. Turning it off needs a TLS, USB or cloud connection: over plaintext Ethernet or Wi-Fi
        the pod refuses with :class:`PodLockedError`. On a cloud connection it needs an
        organization owner or admin."""
        return LanTls.from_reply(self.command({"cmd": "lan_tls", "https_only": bool(on)}))

    def regenerate_lan_tls(self) -> "LanTls":
        """Have the pod make a new key and self-signed certificate. Same transports as turning
        HTTPS-only off. On a network connection this computer's pin follows."""
        info = LanTls.from_reply(self.command({"cmd": "lan_tls", "regenerate": True}))
        self._repin(info)
        return info

    def set_lan_tls_cert(self, cert_pem: Union[str, bytes], key_pem: Union[str, bytes]) -> "LanTls":
        """Install a certificate (PEM, optionally followed by its chain) and its unencrypted private
        key (EC P-256/P-384 or RSA) for port 8443. The pod checks both and that they match before it
        stores them. Network connections only (TLS, or the USB network adapter); this computer's pin
        follows."""
        from .transport.tcp import TcpTransport

        if not isinstance(self._transport, TcpTransport):
            raise UnsupportedFeatureError(
                "set_lan_tls_cert needs a network connection (TLS or the USB network adapter)",
                cmd="lan_tls", feature="lan_tls")
        c = cert_pem.encode() if isinstance(cert_pem, str) else bytes(cert_pem)
        k = key_pem.encode() if isinstance(key_pem, str) else bytes(key_pem)
        if b"ENCRYPTED" in k:
            raise ValueError("the private key is encrypted; the pod needs it without a passphrase")
        bundle = c.rstrip(b"\n") + b"\n" + k
        if len(bundle) > 7664:
            raise ValueError(f"certificate and key are {len(bundle)} bytes; the pod takes at most 7664")
        import hashlib

        reqs: List[Dict[str, Any]] = [
            {"cmd": "lan_tls"},
            {"cmd": "ota_begin", "size": len(bundle), "sha256": hashlib.sha256(bundle).hexdigest(),
             "target": "tls"},
        ]
        for off in range(0, len(bundle), 768):
            reqs.append({"cmd": "ota_data", "offset": off,
                         "data": base64.urlsafe_b64encode(bundle[off:off + 768]).rstrip(b"=").decode()})
        reqs += [{"cmd": "ota_end"}, {"cmd": "ota_commit"}, {"cmd": "lan_tls"}]
        try:
            replies = self._transport.command_sequence(reqs)
        except FirmwareError:
            try:
                self.command({"cmd": "ota_abort"})
            except BenchPodError:
                pass
            raise
        for name, rep in (("ota_end", replies[-3]), ("ota_commit", replies[-2])):
            state = rep.get("state") if isinstance(rep, dict) else None
            want = "verified" if name == "ota_end" else "installed"
            if state != want:
                why = rep.get("error") if isinstance(rep, dict) else rep
                raise FirmwareError(f"{name}: {why or state}", cmd=name)
        info = LanTls.from_reply(replies[-1])
        self._repin(info)
        return info

    def _repin(self, info: "LanTls") -> None:
        """After this computer changed the pod's certificate: pin the new one for its address."""
        from .transport import tls as _tls
        from .transport.tcp import TcpTransport

        t = self._transport
        if not isinstance(t, TcpTransport) or not info.sha256:
            return
        host, port = t._split_addr()
        if _tls.is_usb_link(host):
            return
        _tls.set_pin(host, info.sha256, port=port, subject=info.subject)

    # -- flash --------------------------------------------------------------

    def flash(
        self,
        *,
        swclk: Union[Pin, int, str, None] = None,
        swdio: Union[Pin, int, str, None] = None,
        tck: Union[Pin, int, str, None] = None,
        tms: Union[Pin, int, str, None] = None,
        tdi: Union[Pin, int, str, None] = None,
        tdo: Union[Pin, int, str, None] = None,
        trst: Union[Pin, int, str, None] = None,
        nreset: Optional[bool] = None,
        target: str = "",
        file: str = "",
        load_address: str = "",
        target_power: Optional[Union[Efuse, int]] = None,
        verify: bool = True,
        reset: bool = True,
        connect_under_reset: Optional[bool] = None,
        clear_reset_events: bool = True,
        openocd_bin: Optional[str] = None,
        extra_configs: Sequence[str] = (),
        extra_args: Sequence[str] = (),
        timeout: float = 300.0,
        connect_attempts: int = 5,
        check: bool = True,
        build: str = "",
        repo: str = "",
        ref: str = "",
        artifact: str = "",
        server_side: Optional[bool] = None,
        rx: Union[Pin, int, str, None] = None,
        tx: Union[Pin, int, str, None] = None,
        boot: Union[Pin, int, str, None] = None,
        device: str = "",
        clk: Union[Pin, int, str, None] = None,
        data: Union[Pin, int, str, None] = None,
        reset_pin: Union[Pin, int, str, None] = None,
        erase: str = "chip",
        dry_run: bool = False,
    ) -> FlashResult:
        """Flash a target over SWD or JTAG through the pod's CMSIS-DAP probe and report the result.

        ``swclk``/``swdio`` are LA pins (``benchpod.PIN1``..``PIN14`` or 1-14). ``tck``, ``tms``,
        ``tdi``, ``tdo`` and ``trst`` are the JTAG pins (``trst`` is optional). The port is chosen
        for you (:func:`~embeddedci.benchpod.flash.select_port`): JTAG pins passed in the call (and
        no SWD pins) mean JTAG, SWD pins passed (and no JTAG pins) mean SWD; else the wiring
        profile's ``debug_port`` (``"swd"``/``"jtag"``) decides; else JTAG when the pins for it are
        known (given, else the profile's ``jtag_*``) and either no SWD pins are or the target is
        JTAG only (:data:`~embeddedci.benchpod.flash.JTAG_ONLY_TARGETS`: ``esp32``, ``esp32s2``,
        the RISC-V names), otherwise SWD. JTAG needs
        the ``jtag`` firmware feature (:class:`UnsupportedFeatureError` before anything is armed).
        ``nreset`` is a
        flag, not a pin: pass ``True`` when the target's reset line is wired to the pod's reset
        pin (DUT header J1 pin 22). ``target`` is an OpenOCD target config
        (``target/stm32f4x.cfg``) and ``file`` the image. ``target_power`` of
        ``benchpod.INTERNAL``/``EXTERNAL`` powers the target first. Omitted ``swclk``, ``swdio``,
        ``nreset`` and ``target`` come from the wiring profile (:attr:`wiring`).

        Instead of ``file``, flash a build uploaded to embeddedci.com (the ``upload-artifact``
        action or the ``build_report`` fixture): ``build`` is the build id the upload printed
        (``"latest"``: the newest one), ``repo`` (``owner/name``) and ``ref`` (branch or tag) pick
        the newest build of that repo/ref, and ``artifact`` names a file when the build holds
        several (else its .elf, then .hex, then .bin). On a cloud connection
        (``embeddedci:<device>``) embeddedci.com runs OpenOCD (nothing to install here); on a LAN or
        USB connection the file is downloaded and flashed by the local OpenOCD, which needs an API
        key or ``benchpod login``. ``server_side`` forces one or the other. A server-side flash
        always verifies and resets, and takes no OpenOCD extras. :attr:`FlashResult.artifact` says
        which build was flashed; ``target`` falls back to the target the build recorded.

        An ESP32-family ``target`` (``"esp32"``, ``"esp32s3"``, ``"esp32c3"``, ...) has no SWD: the
        pod writes ``file`` through the chip's UART ROM bootloader instead, the way esptool does (no
        OpenOCD or esptool here, and it works over the cloud too). ``file`` is a ``.bin`` written at
        ``load_address`` (default 0, so an ``esptool merge_bin`` image) or an ESP-IDF
        ``flasher_args.json`` (bootloader, partition table and app at their offsets). Wire the
        target's TXD0 to ``rx`` and RXD0 to ``tx`` (default: the profile's ``uart_rx``/``uart_tx``),
        IO0 (IO9 on the C3/C6) to ``boot`` (default: a profile signal named BOOT, IO0 or IO9) and EN
        to the pod's reset pin (``nreset``; a profile signal named EN also works). The pod checks
        each region against the chip's MD5 and, with ``reset``, starts the new image; ``stdout``
        names the chip and every region written. Needs firmware with the ``esp_flash`` command.

        A chip with its own programming pins and no SWD, JTAG or bootloader is flashed by the pod
        itself: ``target="avr-updi"`` (AVR tinyAVR 0/1/2, megaAVR 0, AVR DA/DB/DD), ``"pic"``
        (PIC16F1xxx, PIC16F18xxx, PIC18F Q-series, low-voltage entry), ``"efm8"`` (Silicon Labs EFM8
        and C8051F, C2), ``"attiny-tpi"`` (ATtiny4/5/9/10/20/40/102/104) ``"xmega"`` (XMEGA, PDI) or ``"msp430"`` (MSP430 FRAM parts, Spy-Bi-Wire: ``clk`` is the chip's TEST pin and ``data`` its RST pin; gateware v52 or newer);
        the protocol names ``updi``, ``pic``, ``c2``, ``tpi`` and ``pdi`` work too. ``file`` is a
        ``.hex``, ``.elf`` or ``.bin`` (a ``.bin`` goes at ``load_address``, default 0, into the
        flash). The SDK reads the chip's ID first, finds it in its device table
        (:mod:`embeddedci.benchpod.prog`), cuts the image into the chip's memories (flash, EEPROM,
        fuses or configuration words, user row or user ID: a ``.hex`` from avr-gcc or XC8 already
        has them at their usual addresses), writes and reads back every page. ``device`` names the
        chip (``"ATtiny1616"``) when its ID is shared or you want the check; omitted, the ID decides.
        ``erase`` is ``"chip"`` (the default, before the first write when the image has flash; it
        also unlocks a locked chip), ``"page"`` (only the pages written; the TPI chips erase their
        whole code section instead) or ``"none"``. ``reset`` says whether the chip runs its program
        afterwards. Pins: ``clk`` and ``data`` are LA pins (``data`` only for ``avr-updi``; default:
        a wiring-profile signal named for the protocol, ``ICSPCLK`` / ``ICSPDAT``, ``C2CK`` /
        ``C2D``, ``TPICLK`` / ``TPIDATA``, ``PDI_CLK`` / ``PDI_DATA`` or ``UPDI``, else the profile's
        ``swd_swclk`` / ``swd_swdio``). ``pic`` and ``attiny-tpi`` also drive the chip's reset
        (MCLR, RESET): the pod's reset pin by default (``nreset=True``), or an LA pin as
        ``reset_pin`` (default: a profile signal named ``MCLR`` or ``RESET``). ``dry_run=True``
        only enters programming mode and reads the chip's ID (a wiring test): ``stdout`` names the
        chip, nothing is written. The ATtiny4/5/9/10/20/40 program at 5 V only, which the pod's I/O
        cannot give (:class:`~embeddedci.benchpod.prog.ProgDeviceError`). A failure raises
        :class:`~embeddedci.benchpod.prog.ProgFlashError` (or a subclass) with what to check.
        Needs firmware with the ``prog_flash`` feature and the ``xip_prog`` blob (``benchpod
        install-blobs``).

        Local OpenOCD runs on THIS machine and needs the ``cmsis_dap_tcp`` backend (newer than
        0.12.0). By default (``check=True``) a failed flash raises :class:`FlashError` /
        :class:`TargetUnreachableError`; pass ``check=False`` to get the :class:`FlashResult` and
        ``assert result.ok`` yourself.
        """
        from_build = bool(build or repo or ref or artifact)
        self._yield_expansion_bus()
        if from_build and file:
            raise ValueError("pass either file or a build (build/repo/ref/artifact), not both")
        if _prog.is_prog_target(target):
            if from_build:
                raise ValueError(f"{target} is flashed from a file (a .hex, .elf or .bin), not an "
                                 "uploaded build")
            try:
                return self._flash_prog(
                    target, file, device=device, clk=clk, data=data, reset_pin=reset_pin,
                    nreset=nreset, load_address=load_address, erase=erase, dry_run=dry_run,
                    run_after=reset,
                    power=coerce_efuse(target_power) if target_power is not None else None)
            except _prog.ProgFlashError as exc:
                if check:
                    raise
                return FlashResult(ok=False, returncode=1, stdout="", stderr=str(exc),
                                   target_unreachable=isinstance(exc, _prog.ProgNoAnswerError))
        if _esp.is_esp_target(target or self.wiring.swd_target):
            if from_build:
                raise ValueError("an ESP32 target is flashed from a file (a .bin or flasher_args.json), "
                                 "not an uploaded build")
            result = self._flash_esp(file, rx=rx, tx=tx, boot=boot, nreset=nreset,
                                     load_address=load_address, reset=reset)
            if check and not result.ok:
                from .errors import FlashError

                raise FlashError(f"flash ({target or self.wiring.swd_target}): {result.stderr}")
            return result
        if nreset is None:
            nreset = self.wiring.swd_nreset
        target = target or self.wiring.swd_target
        power = coerce_efuse(target_power) if target_power is not None else None
        pins = self._flash_pins(swclk, swdio, tck, tms, tdi, tdo, trst, target)
        self._ensure_bus_masters("JTAG" if pins["port"] == "jtag" else "SWD")

        art: Optional[Dict[str, Any]] = None
        tmpdir = ""
        if from_build:
            api = self._require_server_api()
            art = api.build_artifact(build=build, repo=repo, ref=ref, artifact=artifact)
            if not target:
                target = str((art.get("wiring_defaults") or {}).get("target") or "")
            use_server = bool(self._device_name) if server_side is None else server_side
            if use_server:
                if pins["port"] == "jtag":
                    raise ValueError("embeddedci.com cannot flash over JTAG yet: pass server_side=False "
                                     "to download the build and flash it with the local OpenOCD")
                result = self._flash_build_on_server(
                    api, art, swclk=pins["swclk"], swdio=pins["swdio"], nreset=bool(nreset), target=target,
                    load_address=load_address, power=power, verify=verify, reset=reset,
                    connect_under_reset=connect_under_reset, openocd_bin=openocd_bin,
                    extra_configs=extra_configs, extra_args=extra_args, timeout=timeout)
                if check:
                    _flash.raise_for_result(result)
                return result
            import tempfile

            tmpdir = tempfile.mkdtemp(prefix="benchpod-build-")
            file = os.path.join(tmpdir, os.path.basename(str(art["name"])))
            with open(file, "wb") as fh:
                fh.write(api.download_build_artifact(art))
            _log.info("flash: %s from build %s (%s)", art.get("name"), art.get("job_id"),
                     " ".join(str(art[k]) for k in ("repo", "ref", "sha") if art.get(k)))

        try:
            result = self._flash_local(
                pins=pins, nreset=bool(nreset), target=target, file=file,
                load_address=load_address, power=power, verify=verify, reset=reset,
                connect_under_reset=connect_under_reset, clear_reset_events=clear_reset_events,
                openocd_bin=openocd_bin, extra_configs=extra_configs, extra_args=extra_args,
                timeout=timeout, connect_attempts=connect_attempts)
        finally:
            if tmpdir:
                import shutil

                shutil.rmtree(tmpdir, ignore_errors=True)
        result.artifact = art
        if check:
            _flash.raise_for_result(result)
        return result

    def detect(self, *, invasive: bool = True,
               methods: Optional[Sequence[str]] = None) -> "_detect.DetectResult":
        """Find out which chip is wired to the pod.

        Tries every probe the wiring profile makes possible and reports what answered as a
        :class:`~embeddedci.benchpod.detect.DetectResult`: ``powered``, ``findings`` (each a
        :class:`~embeddedci.benchpod.detect.DetectFinding` with ``method``, ``chip``, ``vendor``,
        ``core``, ``id``, ``confidence`` ``"high"``/``"medium"``/``"low"``, ``pins`` and ``raw``),
        ``skipped`` and ``notes``. With target power off nothing is probed (``powered`` is
        ``False``); a pin owned by a UART, GPIO or sensor is not probed (``skipped`` names the
        owner); a probe that errors or gets no answer is a ``skipped`` entry, not an exception.
        Needs the LA voltage (the profile's ``la_mv``).

        Probes: ``swd`` and ``jtag`` read the debug port's IDs without halting, resetting or
        writing the chip (pod firmware with the ``dap_id`` feature; older firmware is reported as
        skipped), and the chip is named from its ID register (an STM32's DEV_ID, a Nordic part
        number) or JTAG IDCODE; ``usb`` reads the device already mounted on the pod's host port
        (host mode is never turned on). ``updi``, ``pic``, ``c2``, ``tpi``, ``pdi``, ``sbw`` (the chip's
        programming-mode ID, ``prog_flash``) and ``esp32`` (the ROM loader's chip name) reset the
        chip. A programming protocol is tried only when the profile has a signal named for it
        (``UPDI``, ``ICSPCLK`` and ``ICSPDAT``, ``C2CK`` and ``C2D``, ``TPICLK`` and ``TPIDATA``,
        ``PDI_CLK`` and ``PDI_DATA``, ``SBWTCK`` or ``TEST`` and ``SBWTDIO`` or ``RST``), never on the SWD pins. ``pic`` and ``tpi`` also need the chip's
        reset: a signal named ``MCLR`` or ``RESET``, else the pod's reset pin when the profile sets
        ``swd_nreset``. ``esp32`` needs ``uart_rx``, ``uart_tx`` and a ``BOOT`` / ``IO0`` / ``IO9``
        signal, and EN (a signal named ``EN``, else the reset pin under ``swd_nreset``).

        ``invasive=False`` runs only ``swd``, ``jtag`` and ``usb`` (what the web app runs by
        itself); ``methods`` limits the probes to those named (a named method the profile does not
        wire is reported in ``skipped``). Raises :class:`ValueError` for an unknown method name and
        the usual connection errors when the pod cannot be reached.
        """
        _detect.normalize_methods(methods)   # a bad name raises before the pod is touched
        self._yield_expansion_bus()

        def prepare(what: str) -> None:
            if what == "swd":
                self._ensure_bus_masters("SWD")
            elif what == "jtag":
                self._ensure_bus_masters("JTAG")
            elif what == "prog":
                self._require_prog()
                self._ensure_image(FpgaImage.DEEP_REPLAY, needed_for="detecting a chip over its "
                                   "programming pins", switch=True)

        def owners() -> Dict[int, str]:
            if not self.supports("la_pins"):
                return {}
            out: Dict[int, str] = {}
            for p in self.la_pins():
                if p.function and p.function != "none":
                    out[p.la] = f"{p.function} ({p.gpio})" if p.function == "gpio" and p.gpio else p.function
            return out

        def usb_state() -> Any:
            return self.usb_host_status()

        env = _detect.DetectEnv(
            command=self.command, wiring=self.wiring, supports=self.supports,
            target_status=self.target_status, la_voltage=lambda: self.get_la_voltage().voltage,
            pin_owners=owners, usb_state=usb_state, prepare=prepare)
        return _detect.run(env, invasive=invasive, methods=methods)

    def _flash_build_on_server(
        self, api: "ServerApi", art: Dict[str, Any], *, swclk: int, swdio: int, nreset: bool,
        target: str, load_address: str, power: Optional[Efuse], verify: bool, reset: bool,
        connect_under_reset: Optional[bool], openocd_bin: Optional[str],
        extra_configs: Sequence[str], extra_args: Sequence[str], timeout: float,
    ) -> FlashResult:
        """Have embeddedci.com flash an uploaded build through this (cloud) pod."""
        if not self._device_name:
            raise BenchPodError(
                "a server-side flash needs a pod registered on embeddedci.com: connect with "
                "'embeddedci:<device>', or pass server_side=False to flash with the local OpenOCD")
        unsupported = [name for name, given in (
            ("verify=False", not verify), ("reset=False", not reset),
            ("openocd_bin", bool(openocd_bin)), ("extra_configs", bool(extra_configs)),
            ("extra_args", bool(extra_args))) if given]
        if unsupported:
            raise ValueError(
                f"{', '.join(unsupported)} only apply to the local OpenOCD; pass server_side=False "
                "to flash this build from this machine")
        if not target:
            raise ValueError("target is required (an OpenOCD config such as target/stm32f4x.cfg): "
                             "pass target= or set swd_target in the wiring profile")
        body: Dict[str, Any] = {
            "job_id": art["job_id"], "pack_id": art["pack_id"], "artifact_name": art["name"],
            "target": target, "load_addr": load_address, "swclk": swclk, "swdio": swdio,
            "nreset": nreset, "efuse": int(power) if power is not None else 0,
        }
        if connect_under_reset is not None:
            body["connect_under_reset"] = bool(connect_under_reset)
        _log.info("flash: embeddedci.com flashes %s from build %s", art.get("name"), art.get("job_id"))
        data = api.flash_build(api.resolve_device_id(self._device_name), body,
                               timeout=timeout + 60.0)
        ok = bool(data.get("ok"))
        return FlashResult(ok=ok, returncode=0 if ok else 1, stdout=str(data.get("output") or ""),
                           stderr=str(data.get("error") or ""),
                           target_unreachable=bool(data.get("target_unreachable")), artifact=art)

    # -- ESP32 targets (UART ROM bootloader, through flash()) ------------------

    def _flash_esp(self, file: str, *, rx: Any, tx: Any, boot: Any, nreset: Optional[bool],
                   load_address: str, reset: bool) -> FlashResult:
        """flash() for an ESP32-family target: the pod's esp_flash, see :mod:`embeddedci.benchpod.esp`."""
        if not file:
            raise ValueError("an ESP32 target needs file: a .bin (written at load_address, default 0) "
                             "or an ESP-IDF flasher_args.json")

        def by_signal(names: Sequence[str]) -> Optional[int]:
            for s in self.wiring.signals:
                if s.name.upper() in names:
                    return s.la
            return None

        pins: Dict[str, Any] = {"rx": self._wired(rx, "uart_rx"), "tx": self._wired(tx, "uart_tx")}
        boot_la = self._resolve_la(boot, "boot")[0] if boot is not None else by_signal(_esp.BOOT_SIGNALS)
        if boot_la is None:
            raise ValueError("boot was not given and the wiring profile has no BOOT (or IO0 / IO9) "
                             "signal: pass the LA pin wired to the target's IO0 (IO9 on the C3/C6)")
        pins["boot"] = boot_la
        en_la = by_signal(_esp.EN_SIGNALS)
        if nreset or en_la is None:
            if nreset is False:
                raise ValueError("an ESP32's EN goes on the pod's reset pin (nreset=True) or on a "
                                 "wiring-profile signal named EN")
            pins["en"] = "nrst"
        else:
            pins["en"] = en_la

        regions, settings = _esp.load_images(file, load_address=_esp.parse_address(load_address))
        size = _esp.parse_flash_size(settings.get("flash_size"))
        stage = getattr(self._transport, "stage_psram", None)
        # The image is staged in PSRAM, which only the deep-replay gateware image writes: a pod left
        # on the emulation (or loop) image is switched first (~3 s).
        self._ensure_image(FpgaImage.DEEP_REPLAY, needed_for="ESP32 flashing", switch=True)
        r = _esp.run(self.command, stage, regions, pins=pins, flash_size=size, run_after=reset)
        lines = [f"{r.chip or 'ESP32'}: {len(r.regions)} region(s) at {r.baud} baud in {r.seconds:.1f} s"]
        lines += [f"0x{g.offset:06x} {g.length} bytes {g.result} md5 {g.md5}" for g in r.regions]
        return FlashResult(ok=r.ok, returncode=0 if r.ok else 1, stdout="\n".join(lines),
                           stderr=r.error)

    # -- chips with their own programming pins (UPDI, PIC, C2, TPI, PDI) -----------------------

    def _require_prog(self) -> None:
        """Refuse ahead, in words, when the pod's firmware lacks ``prog_flash``."""
        if self.supports("prog_flash"):
            return
        fw = self.capabilities.firmware_version or "unknown"
        raise UnsupportedFeatureError(
            f"flashing a chip over its programming pins needs newer pod firmware with the "
            f"'prog_flash' feature (this pod runs firmware {fw}): update the pod and install its "
            f"blobs (benchpod install-blobs)", feature="prog_flash",
            firmware_version=self.capabilities.firmware_version)

    def _prog_pins(self, proto: str, clk: Any, data: Any, reset_pin: Any,
                   nreset: Optional[bool]) -> Tuple[Dict[str, Any], str]:
        """The ``prog_flash`` pins of a flash and the same in words for error messages. A pin the
        caller passes beats a wiring-profile signal named for the protocol, which beats the
        profile's SWD pins."""
        def by_signal(names: Sequence[str]) -> Optional[int]:
            for s in self.wiring.signals:
                if s.name.upper() in names:
                    return s.la
            return None

        def pick(value: Any, names: Sequence[str], role: str, what: str) -> int:
            if value is not None:
                return self._resolve_la(value, what)[0]
            la = by_signal(tuple(names))
            if la is not None:
                return la
            la = getattr(self.wiring, role)
            if la is None:
                raise ValueError(
                    f"{what} was not given and the wiring profile has neither a signal named "
                    f"{' / '.join(names) if names else what.upper()} nor {role}: pass {what}= "
                    "(an LA pin)")
            return la

        uart = proto == "updi"
        pins: Dict[str, Any] = {}
        pins["data"] = pick(data, _prog.DATA_SIGNALS[proto], "swd_swdio", "data")
        text = [f"data on LA{pins['data']}"]
        if not uart:
            pins["clk"] = pick(clk, _prog.CLK_SIGNALS[proto], "swd_swclk", "clk")
            text.insert(0, f"clk on LA{pins['clk']}")
            if pins["clk"] == pins["data"]:
                raise ValueError("clk and data must be different LA pins")
        elif clk is not None:
            raise ValueError("avr-updi uses one pin: pass data=, not clk=")
        if proto in ("pic", "tpi"):
            if reset_pin is not None:
                pins["reset"] = self._resolve_la(reset_pin, "reset_pin")[0]
            elif nreset is False or (nreset is None and by_signal(_prog.RESET_SIGNALS) is not None):
                la = by_signal(_prog.RESET_SIGNALS)
                if la is None:
                    raise ValueError(
                        f"the chip's reset needs the pod's reset pin (nreset=True) or an LA pin "
                        f"(reset_pin=, or a wiring-profile signal named MCLR or RESET)")
                pins["reset"] = la
            else:
                pins["reset"] = "nrst"
            if pins["reset"] in (pins.get("clk"), pins["data"]):
                raise ValueError("reset_pin must be another pin than clk and data")
            text.append("reset on " + ("the pod's reset pin" if pins["reset"] == "nrst"
                                       else f"LA{pins['reset']}"))
        return pins, ", ".join(text)

    def _flash_prog(self, target: str, file: str, *, device: str, clk: Any, data: Any,
                    reset_pin: Any, nreset: Optional[bool], load_address: str, erase: str,
                    dry_run: bool, run_after: bool, power: Optional[Efuse]) -> FlashResult:
        """flash() for a chip with its own programming pins: the pod's prog_flash, see
        :mod:`embeddedci.benchpod.prog`."""
        proto = _prog.protocol_for(target)
        if erase not in ("chip", "page", "none"):
            raise ValueError(f"erase must be 'chip', 'page' or 'none', not {erase!r}")
        if not file and not dry_run:
            raise ValueError(f"{target} needs file: a .hex, .elf or .bin (or pass dry_run=True to only "
                             "read the chip's ID)")
        pins, pin_text = self._prog_pins(proto, clk, data, reset_pin, nreset)
        fam = _prog.families()[proto]
        segments: List[_prog.Segment] = []
        if file:
            segments = _prog.load_image(file, load_address=_esp.parse_address(load_address))
        self._require_prog()
        stage = getattr(self._transport, "stage_psram", None)
        # The image is staged in PSRAM, which only the deep-replay gateware image writes (as for
        # ESP32 flashing): a pod left on the emulation (or loop) image is switched first (~3 s).
        self._ensure_image(FpgaImage.DEEP_REPLAY, needed_for="flashing over programming pins",
                           switch=True)
        if power is not None:
            self._transport.target_power(power, True)
            time.sleep(_flash.TARGET_POWER_SETTLE)
        try:
            volts = self.get_la_voltage().voltage
        except BenchPodError:
            volts = None
        vtext = f"{volts:g} V" if volts else "not set"
        try:
            named = self.supports("prog_named")   # firmware 4.0.6+: named chip fields, else packed flags
            chip_id, dev = _prog.identify(self.command, proto, pins, name=device, named=named,
                                          pin_text=pin_text, volts=vtext)
        except DirectionConflictError as exc:
            raise _prog.direction_error(exc, pin_text) from exc
        lines = [f"{dev.name} ({fam.title}), ID {chip_id}"]
        _prog.check_voltage(dev, volts, erase=erase if file else "none")
        if file:
            _prog.check_erase(dev, erase)
        if dry_run:
            lines.append("dry run: nothing was written")
            return FlashResult(ok=True, returncode=0, stdout="\n".join(lines), stderr="")
        regions, notes = _prog.split_regions(segments, dev, ignore=fam.ignore)
        if erase == "chip" and not any(r.mem == "flash" for r in regions):
            erase = "page"  # an image without flash must not wipe the chip's program
            notes.append("the image has no flash: no chip erase, only the pages written are erased")
        try:
            r = _prog.run(self.command, stage, proto, dev, regions, pins=pins, erase=erase,
                          run_after=run_after, pin_text=pin_text, volts=vtext, chip_id=chip_id,
                          named=named)
        except DirectionConflictError as exc:
            raise _prog.direction_error(exc, pin_text) from exc
        lines += [f"{g.mem} at 0x{g.addr:x}: {g.length} bytes written and verified in "
                  f"{g.seconds:.1f} s" for g in r.regions]
        lines += notes
        lines.append(f"{len(r.regions)} region(s) in {r.seconds:.1f} s"
                     + ("; the chip runs its program" if run_after else "; the chip is held in reset"))
        return FlashResult(ok=r.ok, returncode=0 if r.ok else 1, stdout="\n".join(lines),
                           stderr=r.error)

    def _flash_pins(self, swclk: Any, swdio: Any, tck: Any, tms: Any, tdi: Any, tdo: Any,
                    trst: Any, target: str) -> Dict[str, Any]:
        """The pins and port of a flash: ``swclk``, ``swdio``, ``tck``, ``tms``, ``tdi``, ``tdo``,
        ``trst`` (None: not used) and ``port`` (``"swd"``/``"jtag"``, :func:`flash.select_port`).
        A pin the caller passes beats the wiring profile's."""
        swd_keys = ("swd_swclk", "swd_swdio")
        jtag_keys = ("jtag_tck", "jtag_tms", "jtag_tdi", "jtag_tdo")
        jtag_given = (tck, tms, tdi, tdo, trst)
        swd_given = (swclk, swdio)

        def unset(given: Sequence[Any], keys: Sequence[str]) -> List[str]:
            return [k for g, k in zip(given, keys) if g is None and getattr(self.wiring, k) is None]

        swd_missing, jtag_missing = unset(swd_given, swd_keys), unset(jtag_given, jtag_keys)
        debug_port = getattr(self.wiring, "debug_port", None)
        if (debug_port is None and len(swd_missing) == 2 and len(jtag_missing) == 4
                and all(g is None for g in swd_given + jtag_given)):
            # name the pin like the SWD-only flash always did
            self._wired(swclk, "swd_swclk")
        port = _flash.select_port(
            swd=not swd_missing, jtag=not jtag_missing, target=target,
            swd_given=any(g is not None for g in swd_given),
            jtag_given=any(g is not None for g in jtag_given), debug_port=debug_port,
            swd_missing=swd_missing, jtag_missing=jtag_missing)
        swd: List[Optional[int]] = [None, None]
        if port == "swd" or len(swd_missing) < 2 or any(g is not None for g in swd_given):
            swd = [self._wired(g, k) for g, k in zip(swd_given, swd_keys)]
        jtag: List[Optional[int]] = [None] * 4
        if port == "jtag":
            jtag = [self._wired(g, k) for g, k in zip(jtag_given[:4], jtag_keys)]
        have_swd = swd[0] is not None
        out: Dict[str, Any] = {"swclk": swd[0], "swdio": swd[1], "tck": None, "tms": None,
                               "tdi": None, "tdo": None, "trst": None, "port": port}
        if have_swd and swd[0] == swd[1]:
            raise ValueError("swclk and swdio must be different LA pins")
        if port == "jtag":
            out.update(zip(("tck", "tms", "tdi", "tdo"), jtag))
            out["trst"] = (self._resolve_la(trst, "jtag_trst")[0] if trst is not None
                           else self.wiring.jtag_trst)
            names = {"tck": "tck", "tms": "tms", "tdi": "tdi", "tdo": "tdo", "trst": "trst"}
            used: Dict[int, str] = {}
            for name in names:
                la = out[name]
                if la is None:
                    continue
                other = used.get(la)
                # an ARM part's SWCLK/SWDIO are its TCK/TMS: the probe wires them once
                if other is not None:
                    raise ValueError(f"{other} and {name} must be different LA pins")
                used[la] = name
            for swd_name, jtag_name in (("swclk", "tck"), ("swdio", "tms")):
                la = out[swd_name]
                if la is not None and la in used and used[la] != jtag_name:
                    raise ValueError(f"{swd_name} and {used[la]} must be different LA pins "
                                     f"({swd_name} may only share a pin with {jtag_name})")
            self._require_jtag()
        return out

    def _require_jtag(self) -> None:
        """Refuse ahead, in words, when the pod has no JTAG (firmware feature ``jtag``)."""
        if self.supports("jtag"):
            return
        fw = self.capabilities.firmware_version or "unknown"
        raise UnsupportedFeatureError(
            f"JTAG needs newer pod firmware with the 'jtag' feature (and gateware v61+; not the "
            f"emulation FPGA image); this pod runs firmware {fw}: update the pod",
            feature="jtag", firmware_version=self.capabilities.firmware_version)

    def _flash_local(self, *, pins: Dict[str, Any], nreset: bool, target: str, file: str,
                     load_address: str, power: Optional[Efuse], verify: bool, reset: bool,
                     connect_under_reset: Optional[bool], clear_reset_events: bool,
                     openocd_bin: Optional[str], extra_configs: Sequence[str],
                     extra_args: Sequence[str], timeout: float, connect_attempts: int) -> FlashResult:
        with _classified_errors():
            return _flash.flash(
                self._transport,
                swclk=pins["swclk"], swdio=pins["swdio"], tck=pins["tck"], tms=pins["tms"],
                tdi=pins["tdi"], tdo=pins["tdo"], trst=pins["trst"], port=pins["port"],
                nreset=nreset,
                target=target, file=file, load_address=load_address,
                target_power=power, verify=verify, reset=reset,
                connect_under_reset=connect_under_reset,
                clear_reset_events=clear_reset_events,
                openocd_bin=openocd_bin,
                extra_configs=extra_configs, extra_args=extra_args,
                timeout=timeout, connect_attempts=connect_attempts,
            )

    # -- LA pull resistors -------------------------------------------------------
    # The host names a direction ("up", "down", "off"); the pod picks the resistor and reports its
    # value. Which channels have one and which way each can pull depends on the board: on today's
    # boards LA1-LA6 pull up, LA7/LA8 down and LA9-LA14 have none (constants.PULL_TABLE). The
    # pull-ups go to the LA bank rail, so they work at 1.8 V too on firmware with the la_pull_1v8
    # feature; older firmware refuses to engage one while the LA bank is at 1.8 V.

    def _pulls_at(self, volts: Optional[float]) -> bool:
        """Whether the pod engages pull resistors at LA voltage ``volts``: at 3.3 V always, at
        1.8 V on firmware with the ``la_pull_1v8`` feature (older firmware refuses them there)."""
        if volts is None:
            return False
        if abs(volts - 3.3) <= 0.05:
            return True
        if abs(volts - 1.8) > 0.05:
            return False
        try:
            return self.supports("la_pull_1v8")
        except BenchPodError:
            return False

    @staticmethod
    def _pull_want(la: int, pull: Optional[str]) -> str:
        """``pull`` as "up" / "down" / "off" (``None`` and ``"none"`` are off)."""
        if pull is None:
            return "off"
        if isinstance(pull, bool):
            raise TypeError(f'set_pull() takes "up", "down" or "off", not {pull!r} (the boolean form '
                            "is gone in embeddedci 4.0)")
        want = str(pull).strip().lower()
        if want == "none":
            want = "off"
        check_choice(want, PULL_DIRECTIONS, "pull")
        return want

    @staticmethod
    def _pull_refusal(la: int, want: str, supported: Tuple[str, ...]) -> PullDirectionError:
        """The pod's own refusal (BP0023 / BP0024), for what the SDK refuses before asking."""
        if not supported:
            return firmware_error(f"LA{la} has no pull resistor on this board", cmd="la_pull",
                                  code="BP0023", values={"la": la})  # type: ignore[return-value]
        have = supported[0]
        return firmware_error(f"LA{la} supports pull-{have} only on this board", cmd="la_pull",
                              code="BP0024", values={"la": la, "want": want, "have": have})  # type: ignore[return-value]

    def set_pull(self, la: Union[Pin, int], pull: Optional[PullDirection]) -> PullState:
        """Pull LA ``la`` ``"up"``, ``"down"`` or ``"off"`` (``None`` is off too) and return its
        :class:`PullState`.

        The pod picks the resistor; :attr:`PullState.ohms` says which. A direction the channel's
        resistor can't pull raises :class:`~embeddedci.benchpod.errors.PullDirectionError` (BP0024,
        "LA7 supports pull-down only on this board"), and ``"up"``/``"down"`` on a channel without
        one too (BP0023). ``"off"`` is fine on every channel.
        """
        la_i = coerce_pin(la, "la")
        want = self._pull_want(la_i, pull)  # type: ignore[arg-type]
        with _classified_errors():
            reply = self.command({"cmd": "la_pull", "la": la_i, "pull": want})
        return PullState.from_reply(reply, la_i)

    def pull_state(self, la: Union[Pin, int]) -> PullState:
        """Read LA ``la``'s pull resistor: engaged, direction, value, supported directions."""
        la_i = coerce_pin(la, "la")
        with _classified_errors():
            return PullState.from_reply(self.command({"cmd": "la_pull", "la": la_i}), la_i)

    def enabled_pulls(self) -> List[int]:
        """The LA channels whose pull resistor is currently engaged (either direction)."""
        mask = int(_dict(self.command({"cmd": "la_pull"})).get("la_pullup_mask", 0) or 0)
        return [la for la in range(1, 15) if mask >> (la - 1) & 1]

    def enable_pullup(self, *las: Union[Pin, int]) -> None:
        """Pull LA channels up, e.g. an open-drain I2C bus's SDA/SCL (LA1-LA6 on today's boards).
        Same as ``set_pull(la, "up")``."""
        for la in las:
            self.set_pull(la, "up")

    def disable_pullup(self, *las: Union[Pin, int]) -> None:
        """Release a pull-up. Same as ``set_pull(la, "off")`` on a channel that pulls up."""
        for la in las:
            self._release_pull(la, "up")

    def enable_pulldown(self, *las: Union[Pin, int]) -> None:
        """Pull LA channels down (LA7/LA8 on today's boards). Same as ``set_pull(la, "down")``."""
        for la in las:
            self.set_pull(la, "down")

    def disable_pulldown(self, *las: Union[Pin, int]) -> None:
        """Release a pull-down. Same as ``set_pull(la, "off")`` on a channel that pulls down."""
        for la in las:
            self._release_pull(la, "down")

    def _release_pull(self, la: Union[Pin, int], kind: str) -> None:
        """Switch the pull off, refusing a channel that can't pull ``kind`` at all (so a typo
        like disable_pullup(7) is not silently a no-op)."""
        la_i = coerce_pin(la, "la")
        supported = self.pull_state(la_i).supported
        if kind not in supported:
            raise self._pull_refusal(la_i, kind, supported)
        self.set_pull(la_i, "off")

    # -- emulated sensors ---------------------------------------------------------

    @property
    def sensors(self) -> "SensorEmulation":
        """The pod's user-defined sensor emulation (:class:`~embeddedci.benchpod.sensors.SensorEmulation`):
        ``bp.sensors.start("bme280", pins={"sda": 2, "scl": 1})`` returns a
        :class:`~embeddedci.benchpod.sensors.Sensor` to ``set``, ``fault`` and ``stop``. Needs
        firmware with the ``sensors`` capability."""
        if self._sensors is None:
            from .sensors import SensorEmulation

            self._sensors = SensorEmulation(self)
        return self._sensors

    # -- analog paths ---------------------------------------------------------
    # One named path == one fully-specified switch state, defined once in the firmware, so the SDK
    # never hand-flips muxes/relays and can never disagree with the pod about how a path is wired.
    # Raw mux/relay access lives on :attr:`lowlevel`.

    def analog_path(self, path: AnalogPath, *, range: Optional[AdcRange] = None) -> AnalogPathState:
        """Apply a named analog path, flipping every mux and relay it needs in one step.

        ``dac_3v3``/``dac_5v``/``dac_12v`` route the DAC to an output (``dac_12v`` is bipolar
        ±12 V); ``adc_ext`` connects the ADC to the front SMA; ``cal1``/``cal2`` loop the 5 V /
        12 V DAC output back into the ADC; ``current_in`` reads the 4-20 mA measurement terminal; ``off`` parks it all.
        ``current_out`` is the 4-20 mA output (J9): it only switches the DAC voltage outputs off,
        for a waveform played as a current (:meth:`current_out` does it for a held level).

        ``range`` also sets the ADC input range afterwards (:meth:`adc_range`, analog module
        v3r4+); without it the range stays as it is, except that ``off`` and ``cal2`` put it back
        to ``normal``.
        """
        check_choice(path, ANALOG_PATHS, "path")
        req: Dict[str, Any] = {"cmd": "analog_path", "path": path}
        if range is not None:
            check_choice(range, ADC_RANGES, "range")
            req["range"] = range
        before = self.capabilities.adc_range
        state = AnalogPathState.from_reply(self.command(req))
        self._follow_adc_range(state.range)
        self._note_auto_range(path == "current_in", range, before, state.range)
        if path in _ANALOG_PATH_DAC:
            self._routed_dac_path = _ANALOG_PATH_DAC[path]
        elif path == "off":
            self._routed_dac_path = None  # adc_ext / current_in only move the ADC side
        return state

    def dac_output(self, path: DacOutputPath, *, volts: Optional[float] = None) -> DacOutput:
        """Route a DAC output path and, with ``volts``, drive that CALIBRATED DC voltage.

        ``path`` is ``3v3``/``5v``/``12v``, ``cal1`` or ``off``. ``cal1`` sets the level on the 5 V
        DAC stage routed into the ADC loopback (relays K1 and K4), so ``adc_read(source="cal1")``
        reads it back without any relay switching in between; the 5 V output is disconnected while
        it is on. It needs firmware with feature ``dac_out_cal1`` (else
        :class:`UnsupportedFeatureError`), and the pod checks ``volts`` against the 5 V window. The
        returned :class:`DacOutput` carries the voltage actually produced (the nearest DAC code to
        the request).
        """
        check_choice(path, DAC_OUTPUT_PATHS, "path")
        self._require_dac_cal1(path)
        req: Dict[str, Any] = {"cmd": "dac_out", "path": path}
        if volts is not None:
            if path == "off":
                raise ValueError("volts cannot be set on the 'off' path")
            req["v"] = float(volts)
        out = DacOutput.from_reply(self.command(req))
        self._routed_dac_path = None if path == "off" else path
        self._dac_routed()
        return out

    # -- the 4-20 mA output (J9) ------------------------------------------------
    # An XTR116 two-wire transmitter. The pod owns the conversion (firmware `current_out`); the
    # SDK only changes units: amps here, microamps on the wire.

    def current_out(self, current: float) -> CurrentOutput:
        """Hold ``current`` (amps) on the 4-20 mA output, terminal J9: ``0.012`` is 12 mA.

        The returned :class:`CurrentOutput` carries the current actually held (the nearest
        16-bit DAC code, 0.25 µA apart) and the range the output can do. It cannot go below its
        live zero (a little above 4 mA) or above about 20.1 mA, so there is no 0 mA and no 21 mA
        level. ``0.004`` gives the live zero; the pod refuses anything else outside the range.

        **The output is loop powered.** J9 pin 1 (plus) goes to the plus of an external,
        floating loop supply (8 V plus 20 mA times the loop resistance, 30 V at most); pin 2
        (minus) is the loop return through the receiver and is not pod ground. Nothing in the
        loop may touch pod ground, also not through a target that shares a ground with the pod.
        Do not wire J9 straight into J8. The pod cannot see the loop: the call succeeds with
        the supply off.

        **The DAC is shared** with the 3.3 V / 5 V / ±12 V outputs. This call switches those
        outputs off first. :meth:`dac_output`, :meth:`generate` and :meth:`replay` also move the
        loop current, and :meth:`dac_stop` leaves it where it was: call ``current_out(0.004)`` to
        go back to 4 mA.

        Raises :class:`ValueError` for a value that is not a current in amps, and
        :class:`BenchPodError` when the pod refuses (out of range, or DAC limits are set).
        """
        amps = float(current)
        if not math.isfinite(amps) or amps <= 0:
            raise ValueError(f"current must be a positive current in amps, got {current!r}")
        if amps >= 1.0:
            raise ValueError(f"current is in amps: use {amps / 1000:g} for {amps:g} mA")
        self._require_capability("current_out", "4-20 mA output commands")
        return CurrentOutput.from_reply(self.command({"cmd": "current_out", "ua": round(amps * 1e6)}))

    def current_out_range(self) -> CurrentOutput:
        """What the 4-20 mA output can do (``min_current`` / ``max_current``, amps). Moves nothing."""
        self._require_capability("current_out", "4-20 mA output commands")
        return CurrentOutput.from_reply(self.command({"cmd": "current_out"}))

    def adc_read(self, source: AdcSource = "ext", *, range: Optional[AdcRange] = None) -> AdcReading:
        """Route an ADC ``source`` and return one CALIBRATED reading (a short averaged burst).

        ``ext`` is the front SMA (the ÷12 divider is applied, so ``voltage`` is the true SMA
        voltage); ``cal1``/``cal2`` the internal DAC loopbacks; ``current_in`` the 4-20 mA measurement terminal. The
        pod refuses the reading when the input is still moving (e.g. a DAC left running).

        On ``current_in`` the reading also carries ``current``, the loop current in amps, and
        already has this pod's calibration offset taken out (:meth:`calibrate`); ``offset`` says
        how much that was. From analog module v4 (100 Ω sense resistor, 30 mA limiter, firmware
        3.10.13+) ``current_in`` reads in the low range unless ``range`` says otherwise: about
        1.7 µA per count, up to the limiter's 28.5 to 31.5 mA. The next route of another source or
        output gives the normal range back. ``sense_ohms`` is the resistance ``current`` was
        computed with.

        ``range`` switches the ADC input range first (:meth:`adc_range`): ``"low"`` reads up to
        ±5.46 V at about 6x the resolution on analog module v3r4+ (not on ``cal2``). The reading
        says which range it was taken in, its full scale, and ``overrange`` when it clipped.
        """
        check_choice(source, ADC_SOURCES, "source")
        req: Dict[str, Any] = {"cmd": "adc_read", "source": source}
        if range is not None:
            check_choice(range, ADC_RANGES, "range")
            req["range"] = range
        before = self.capabilities.adc_range
        reading = AdcReading.from_reply(self.command(req))
        self._follow_adc_range(reading.range)
        self._note_auto_range(source == "current_in", range, before, reading.range)
        return reading

    # -- the ADC input range (analog module v3r4+) -------------------------------
    # Relay K5 swaps the divider in front of the ADC: normal ÷12 (±32.8 V) or low ÷2 (±5.46 V).
    # Captures are raw counts, so the SDK keeps the fit it scales them with on the range in use.

    def adc_range(self, range: Optional[AdcRange] = None) -> AdcRangeState:
        """Read the ADC input range, or switch it to ``range`` (``"normal"`` / ``"low"``).

        The low range (analog module v3r4 and later, firmware 3.9.5+) is ±5.46 V at about
        167 µV per count, against ±32.8 V and ~1 mV in the normal range: about 6x the resolution
        on signals up to ±5 V. **Use 1x leads**: its input is 1.82 MΩ, not the 1 MΩ a 10x probe
        expects. It applies to ``ext``, ``cal1`` and ``current_in``; ``cal2`` has none. It stays
        until changed, ``analog_path("off")`` or ``"cal2"``, or a restart of the pod.

        Switching waits for the relay to settle, and from then on :meth:`capture_adc` scales with
        the new range's fit. Raises :class:`BenchPodError` when the board has no low range.
        """
        req: Dict[str, Any] = {"cmd": "adc_range"}
        if range is not None:
            check_choice(range, ADC_RANGES, "range")
            req["range"] = range
        state = AdcRangeState.from_reply(self.command(req))
        if range is not None:
            self._adc_low_auto = False
        self._set_adc_range_fit(state)
        return state

    def adc_range_test(self) -> AdcRangeTest:
        """Self-test the ADC low range: the 5 V output drives the cal1 loopback at 1 V and 3.5 V,
        and each point is read in both ranges. ``passed`` is ``False`` when the ranges disagree by
        more than 5 % + 30 mV (a relay that does not switch reads 6x off). Takes about 0.5 s and
        leaves the outputs off and the range normal. Disconnect anything on the 5 V output."""
        result = AdcRangeTest.from_reply(self.command({"cmd": "adc_range", "test": True}))
        self._follow_adc_range("normal")
        return result

    def _set_adc_range_fit(self, state: AdcRangeState) -> None:
        """Put the range's fit into the capabilities, so captures scale with it."""
        caps = self.capabilities
        caps.adc_range = state.range
        caps.adc_range_fullscale = state.fullscale
        caps.adc_low_range = state.supported
        if state.b:
            unwrap = caps.adc_affine.unwrap if caps.adc_affine is not None else True
            caps.adc_affine = AffineCal(a=state.a or 0.0, b=state.b, unwrap=unwrap)

    def _note_auto_range(self, current_in: bool, asked: Optional[str], before: str, after: str) -> None:
        """After a route: on analog module v4 ``current_in`` without a ``range`` takes the low
        range by itself, and the next route of another path gives it back."""
        if asked is not None or not current_in:
            self._adc_low_auto = False
        elif after == "low" and before != "low":
            self._adc_low_auto = True

    def _dac_routed(self) -> None:
        """A DAC output was routed: a low range ``current_in`` picked by itself is normal again."""
        if self._adc_low_auto:
            self._adc_low_auto = False
            self._follow_adc_range("normal")

    def _follow_adc_range(self, rng: str) -> None:
        """A reply said the pod is in range ``rng``: re-read the fit when the cached one is for
        another range (``off`` and ``cal2`` put the range back to normal)."""
        if not rng:
            return  # firmware before 3.9.5: one range
        caps = self.capabilities
        if caps.adc_range == rng or (not caps.adc_range and rng == "normal"):
            return
        self.adc_range()

    # -- the pod's own calibration ---------------------------------------------
    # The ADC fits are built into the firmware and the same on every pod. `calibrate` lets a pod
    # measure what it can on itself and keep it in flash: today the offset of `current_in`.

    def calibrate(self, source: CalibrateSource = "current_in") -> Calibration:
        """Calibrate ``source`` on the pod and store the result in its flash.

        ``current_in`` is the 4-20 mA input (J8) and the only source a pod can calibrate on its own.
        **Disconnect J8 first**: with nothing connected the terminal is at 0 V, so what the pod
        reads is its ADC offset. It takes about 0.3 s. Every later ``adc_read("current_in")`` has
        that offset taken out, and from firmware 3.9.3 every other source too (``ext``, ``cal1``,
        ``cal2``, and ``capture_adc`` volts): they share the same front end. It survives a reboot
        and a firmware update.

        Raises :class:`BenchPodError` when the pod refuses: something is driving J8 (more than
        150 mV; 50 mV before firmware 3.9.3), or the input is still moving. The stored calibration
        is then unchanged.
        """
        check_choice(source, CALIBRATE_SOURCES, "source")
        self._require_capability("calibrate", "calibration")
        return Calibration.from_reply(self.command({"cmd": "calibrate", "source": source}))

    def calibration(self) -> Calibration:
        """The calibration this pod has stored (``calibrated`` is ``False`` when it has none)."""
        self._require_capability("calibrate", "calibration")
        return Calibration.from_reply(self.command({"cmd": "calibrate"}))

    def clear_calibration(self, part: Optional[CalibrationPart] = None) -> Calibration:
        """Remove the pod's stored calibration. It goes back to the built-in fits.

        ``part`` removes one part only (firmware 3.9.4+): ``"offset"`` (the ADC offset),
        ``"dac"`` (the DMM lines of every DAC output) or ``"adc"`` (the ADC gains).
        """
        self._require_capability("calibrate", "calibration")
        if part is None:
            return Calibration.from_reply(self.command({"cmd": "calibrate", "clear": True}))
        check_choice(part, CALIBRATION_PARTS, "part")
        self._require_capability("cal_dmm", "partial calibration clears")
        return Calibration.from_reply(self.command({"cmd": "calibrate", "clear": part}))

    #: The set points a DMM calibration drives on each output, in volts: well inside the range,
    #: away from the output floor near 0 V. The pod's ADC gain step uses the same ones.
    DMM_SET_POINTS: Dict[str, Tuple[float, float]] = {"3v3": (0.5, 3.0), "5v": (1.0, 4.0), "12v": (-5.0, 5.0)}

    def calibrate_dac(self, path: DacCalPath,
                      points: Sequence[Tuple[int, float]]) -> Calibration:
        """Store this pod's own line for a DAC output from two DMM readings (firmware 3.9.4+).

        ``points`` is two ``(code16, volts)`` pairs: the 16-bit code the pod drove (the
        ``code16`` of :meth:`dac_output`) and what a DMM read on that output's SMA. Disconnect
        the DUT from the output first. A typical run::

            for v in pod.DMM_SET_POINTS["5v"]:
                out = pod.dac_output("5v", volts=v)
                points.append((out.code16, dmm.read_volts()))
            pod.calibrate_dac("5v", points)
            pod.dac_output("off")

        From then on :meth:`dac_output`, the calibrated :meth:`generate` and the DAC output
        limits convert with this line. Raises :class:`BenchPodError` when the pod refuses: the
        codes are too close, a reading is far from what the output should give (the DMM is on
        another SMA), or the gain is more than 2 % off. The stored line is then unchanged.
        """
        check_choice(path, DAC_CAL_PATHS, "path")
        pts = list(points)
        if len(pts) != 2:
            raise ValueError("calibrate_dac needs exactly two (code16, volts) points")
        (c1, v1), (c2, v2) = pts
        self._require_capability("cal_dmm", "DMM calibration")
        return Calibration.from_reply(self.command({
            "cmd": "calibrate", "dac": path, "code1": int(c1), "uv1": int(round(float(v1) * 1e6)),
            "code2": int(c2), "uv2": int(round(float(v2) * 1e6))}))

    def calibrate_adc_gain(self, source: AdcGainSource, *, range: AdcRange = "normal") -> Calibration:
        """Measure and store an ADC source's gain through a loopback (firmware 3.9.4+).

        The pod drives two set points on a DAC output that has its DMM line
        (:meth:`calibrate_dac`) and compares what the ADC reads with what the output puts out:
        ``cal1`` through the 5 V output, ``cal2`` through the ±12 V output, and ``ext`` (the front
        SMA, which captures and the control loop use) through an SMA cable from the ±12 V output
        to the ADC input. ``current_in`` follows ``cal1``. Needs the ADC offset first
        (:meth:`calibrate`). ``Calibration.measured`` says what was measured. Takes about 0.7 s
        and switches the outputs off afterwards.

        ``range="low"`` (analog module v3r4+, firmware 3.9.5+) measures the ADC low range's gain
        instead, through ``cal1`` only (the 5 V output at 1 V and 4 V): one gain serves every
        source in the low range (``Calibration.adc_low_gain_ppm``). It leaves the range normal.
        """
        check_choice(source, ADC_GAIN_SOURCES, "source")
        check_choice(range, ADC_RANGES, "range")
        self._require_capability("cal_dmm", "DMM calibration")
        req: Dict[str, Any] = {"cmd": "calibrate", "adc": source}
        if range != "normal":
            if source != "cal1":
                raise ValueError("the low range is calibrated through cal1, and serves every source")
            req["range"] = range
        cal = Calibration.from_reply(self.command(req))
        if range != "normal":
            self._follow_adc_range("normal")
        return cal

    # -- The analog module (two-board BenchPod, digital v3r3) -------------------

    def board_header(self) -> Optional[str]:
        """Which I/O header this pod has: ``"front"`` (the 2x13 connector on the front of digital
        v3r3 and later) or ``"j1"`` (the 2x11 pin headers of the one-piece boards up to v3-r2);
        None when the pod cannot tell. Asked once per connection: the read-only ``module`` command
        (it answers on a module-link board and refuses on the others, over the cloud too), else the
        status. :func:`~embeddedci.benchpod.header.header_map` writes the wiring on it."""
        cached = getattr(self, "_board_header", False)
        if cached is not False:
            return cached
        from .header import header_from_status

        header: Optional[str] = None
        try:
            self.command({"cmd": "module"})
            header = "front"
        except BenchPodError as e:
            # BP0056: a digital board without the module link, so its header is J1.
            header = "j1" if getattr(e, "code", "") == "BP0056" else None
        if header is None:
            try:
                header = header_from_status(self.status())
            except BenchPodError:
                header = None
        self._board_header = header
        return header

    def module(self) -> ModuleInfo:
        """The analog module and its EEPROM: identity, revision, serial and stored calibration.

        Only on a two-board BenchPod (digital v3r3); other boards answer with a ``BenchPodError``.
        """
        return ModuleInfo.from_reply(self.command({"cmd": "module"}))

    def set_module_id(self, serial: str, rev: str, module_type: str = "analog", *,
                      replace: bool = False) -> ModuleInfo:
        """Write the analog module's identity into its EEPROM: ``serial`` (up to 16 characters),
        hardware ``rev`` as ``"major.minor"`` (``"3.3"`` for analog-v3r3) and ``module_type``.

        The stored calibration stays. A module that already has another serial is refused unless
        ``replace=True``: a serial names one module for life. The revision decides features (the ADC
        low range from 3.4) after the pod's next restart.
        """
        if not serial or len(serial) > 16 or not serial.isprintable() or '"' in serial or "\\" in serial:
            raise ValueError("serial: 1 to 16 printable characters, no quotes or backslashes")
        if not re.fullmatch(r"\d{1,3}\.\d{1,3}", rev):
            raise ValueError('rev: "major.minor", for example "3.3"')
        current = self.module()
        if current.eeprom == "ok" and current.serial and current.serial != serial and not replace:
            raise BenchPodError(f"module: this module already has serial {current.serial!r}; "
                                "pass replace=True to change it")
        return ModuleInfo.from_reply(self.command({"cmd": "module", "eeprom": "id", "type": module_type,
                                                   "rev": rev, "serial": serial}))

    def store_adc_compensation(self, hf_dc_gain: float, c_top_pf: float) -> Calibration:
        """Record the ADC input's measured frequency response on the pod (firmware 3.9.5+).

        ``hf_dc_gain`` is the high-frequency gain against DC and ``c_top_pf`` the divider's fitted
        top-leg capacitance, both from ``adc_comp_sweep.py`` in the firmware's calibration scripts.
        Reference only: no reading is scaled with them. Read it back as ``Calibration.adc_comp``.
        """
        self._require_capability("calibrate", "calibration")
        cal = Calibration.from_reply(self.command({"cmd": "calibrate", "comp_hf_dc_e4": round(hf_dc_gain * 1e4),
                                                   "comp_c_top_ff": round(c_top_pf * 1e3)}))
        if cal.adc_comp is None:
            raise BenchPodError("this pod's firmware cannot record the ADC frequency response (needs 3.9.5+)")
        return cal

    # -- CAN (FDCAN1 / TCAN1044 transceiver) -----------------------------------
    # Single-node board: use a loopback ``mode`` to self-test on one pod. :meth:`open_can` and
    # :class:`~embeddedci.benchpod.can.CanBus` are the convenient interface; these are the
    # command-level building blocks under it.

    def can_config(self, *, bitrate: int = 500_000, mode: CanMode = "normal",
                   term: bool = False, fd: bool = False,
                   data_bitrate: Optional[int] = None) -> Dict[str, Any]:
        """Bring FDCAN1 up. ``mode`` is ``normal``/``internal``/``external``/``listen`` (the two
        loopbacks let a lone pod self-test). ``term`` engages the 120 Ω bus termination.

        ``data_bitrate`` turns CAN FD on (firmware 3.10.2+, capability ``can_fd``): frames carry
        up to 64 bytes, and their data phase switches to ``data_bitrate`` (up to 8 Mbit/s) when
        it is above ``bitrate``. Rates with exact timing work, for example 1, 2, 4, 5 and
        8 Mbit/s. ``fd=True`` without ``data_bitrate`` is CAN FD without bit-rate switching
        (``data_bitrate=bitrate``). Without either, the bus is classic CAN."""
        check_choice(mode, CAN_MODES, "mode")
        msg: Dict[str, Any] = {"cmd": "can_start", "bitrate": int(bitrate), "mode": mode,
                               "term": bool(term)}
        if data_bitrate is None and fd:
            data_bitrate = bitrate
        if data_bitrate is not None:
            if int(data_bitrate) < int(bitrate):
                raise ValueError(f"data_bitrate ({data_bitrate}) must be at least bitrate ({bitrate})")
            self._require_capability("can_fd", "CAN FD frames")
            msg["data_bitrate"] = int(data_bitrate)
        return _dict(self.command(msg))

    def can_write(self, can_id: int, data: Union[bytes, Sequence[int], None] = None,
                  *, ext: bool = False, rtr: bool = False, fd: Optional[bool] = None,
                  brs: Optional[bool] = None) -> Dict[str, Any]:
        """Queue one frame: 0..8 data bytes on classic CAN, up to 64 on CAN FD.

        On a CAN FD bus (:meth:`can_config` with ``data_bitrate``) a frame is an FD frame with
        bit-rate switching unless ``fd=False`` (a classic frame, for a classic node on the same
        bus) or ``brs=False``. An FD length other than 0..8, 12, 16, 20, 24, 32, 48 or 64 is
        padded with zeros to the next one; the reply's ``dlc`` is the length sent."""
        payload = _can_payload(data)
        if len(payload) > 8:
            self._require_capability("can_fd", "CAN frames longer than 8 bytes")
        msg: Dict[str, Any] = {"cmd": "can_write", "id": int(can_id), "ext": bool(ext),
                               "rtr": bool(rtr), "data": payload}
        if fd is not None:
            msg["fd"] = bool(fd)
        if brs is not None:
            msg["brs"] = bool(brs)
        return _dict(self.command(msg))

    def can_read(self, *, max_frames: int = 8) -> _can.CanReadResult:
        """Drain up to ``max_frames`` received frames from the pod's RX ring (non-blocking)."""
        return _can.CanReadResult.from_reply(self.command({"cmd": "can_read", "max": int(max_frames)}))

    def can_status(self) -> Dict[str, Any]:
        """The CAN link state (mode, bitrate, error counters, bus-off, …)."""
        return _dict(self.command({"cmd": "can_status"}))

    def can_term(self, on: bool) -> Dict[str, Any]:
        """Switch the 120 Ω bus termination in/out (works even with CAN off)."""
        return _dict(self.command({"cmd": "can_term", "on": bool(on)}))

    def can_respond(self, match_id: int, reply_id: int,
                    reply_data: Union[bytes, Sequence[int], None] = None, *,
                    match_ext: bool = False, reply_ext: bool = False,
                    reply_fd: Optional[bool] = None,
                    reply_brs: Optional[bool] = None) -> Dict[str, Any]:
        """Install an autonomous-responder rule: when a frame with ``match_id`` arrives the
        *firmware* immediately replies with ``reply_id`` + ``reply_data`` (from the RX ISR —
        microsecond latency, no host round-trip). Turns the pod into an ECU simulator.

        On a CAN FD bus the reply is an FD frame (up to 64 bytes) with bit-rate switching unless
        ``reply_fd=False`` or ``reply_brs=False``, as for :meth:`can_write`."""
        payload = _can_payload(reply_data)
        if len(payload) > 8:
            self._require_capability("can_fd", "CAN frames longer than 8 bytes")
        msg: Dict[str, Any] = {"cmd": "can_respond", "match_id": int(match_id), "ext": bool(match_ext),
                               "reply_id": int(reply_id), "reply_ext": bool(reply_ext),
                               "reply_data": payload}
        if reply_fd is not None:
            msg["reply_fd"] = bool(reply_fd)
        if reply_brs is not None:
            msg["reply_brs"] = bool(reply_brs)
        return _dict(self.command(msg))

    def can_respond_clear(self) -> Dict[str, Any]:
        """Remove all autonomous-responder rules."""
        return _dict(self.command({"cmd": "can_respond", "clear": True}))

    def can_disable(self) -> Dict[str, Any]:
        """Stop FDCAN1 and release the peripheral."""
        return _dict(self.command({"cmd": "can_stop"}))

    def open_can(self, *, bitrate: int = 500_000, mode: CanMode = "normal",
                 term: bool = False, fd: bool = False,
                 data_bitrate: Optional[int] = None) -> _can.CanBus:
        """Configure CAN and return a :class:`~embeddedci.benchpod.can.CanBus` session (clears
        responder rules and disables CAN on exit)::

            with bp.open_can(bitrate=500_000, mode="internal") as can:
                can.write(0x123, [1, 2, 3])
                frame = can.expect(0x123, timeout=1.0)

        ``data_bitrate`` makes it a CAN FD bus (see :meth:`can_config`)::

            with bp.open_can(bitrate=500_000, data_bitrate=2_000_000, mode="internal") as can:
                can.write(0x123, bytes(range(64)))
        """
        return _can.CanBus(self, bitrate=bitrate, mode=mode, term=term, fd=fd,
                           data_bitrate=data_bitrate)

    # -- UART -----------------------------------------------------------------

    def capture_uart(
        self,
        *,
        rx: Union[Pin, int, str, None] = None,
        tx: Union[Pin, int, str, None] = None,
        baud: Optional[int] = None,
        duration: float,
        until: Optional[_uart.Until] = None,
        parity: Optional[str] = None,
        stop_bits: Optional[int] = None,
        half_duplex: bool = False,
        entry: Optional[_uart.UartEntry] = None,
        de: Union[Pin, int, str, None] = None,
        de_active_low: bool = False,
        port: Optional[str] = None,
        dtr: Optional[bool] = None,
        rts: Optional[bool] = None,
    ) -> _uart.UartCapture:
        """Capture the DUT's UART output for ``duration`` seconds.

        ``rx`` is the LA channel the pod samples (wire the DUT's TX here); ``tx`` is driven (the
        DUT's RX). ``until`` (substring / compiled regex / predicate) stops early on a match.
        Omitted ``rx``, ``tx``, ``baud``, ``parity`` and ``stop_bits`` come from the wiring profile.
        ``parity`` (``"none"``, ``"even"``, ``"odd"``) and ``stop_bits`` (1 or 2) set the frame
        format, as in :meth:`open_uart`, and so do ``half_duplex`` and ``entry``.

        ``port="usb"`` captures the serial port of the board the pod hosts over USB instead of LA
        pins, as in :meth:`open_uart` (``baud``, ``parity``, ``stop_bits``, ``dtr`` and ``rts``
        apply; ``rx`` and ``tx`` do not).
        """
        if duration <= 0:
            raise ValueError(f"duration must be > 0 seconds, got {duration!r}")
        link = self._uart_link(rx, tx, baud, parity, stop_bits, half_duplex, entry, de, de_active_low,
                               port=port, dtr=dtr, rts=rts)
        return _uart.capture(link, duration=duration, until=until)

    def _uart_link(self, rx: Any, tx: Any, baud: Optional[int], parity: Optional[str] = None,
                   stop_bits: Optional[int] = None, half_duplex: bool = False,
                   entry: Optional[_uart.UartEntry] = None,
                   de: Any = None, de_active_low: bool = False, port: Optional[str] = None,
                   dtr: Optional[bool] = None, rts: Optional[bool] = None) -> Any:
        if parity is not None and parity not in ("none", "even", "odd"):
            raise ValueError(f"parity must be 'none', 'even' or 'odd', got {parity!r}")
        if stop_bits is not None and (isinstance(stop_bits, bool) or stop_bits not in (1, 2)):
            raise ValueError(f"stop_bits must be 1 or 2, got {stop_bits!r}")
        if port is not None:
            return self._usb_uart_link(rx, tx, baud, parity, stop_bits, half_duplex, entry, de,
                                       port, dtr, rts)
        if dtr is not None or rts is not None:
            raise ValueError("dtr and rts are the control lines of the USB serial port: pass port=\"usb\"")
        # Omitted: the wiring profile's frame (uart_parity, uart_stop_bits; 8N1 unless it says so).
        if parity is None:
            parity = getattr(self.wiring, "uart_parity", None) or "none"
        if stop_bits is None:
            stop_bits = getattr(self.wiring, "uart_stop_bits", None) or 1
        if parity != "none" or stop_bits != 1:
            if not self.supports("uart_frame"):
                fw = self.capabilities.firmware_version or "unknown"
                raise UnsupportedFeatureError(
                    f"UART parity and stop bits need gateware v56 (feature uart_frame; this pod "
                    f"runs firmware {fw}): update the pod's firmware",
                    feature="uart_frame", firmware_version=self.capabilities.firmware_version)
        if entry is not None and not isinstance(entry, _uart.UartEntry):
            raise ValueError(f"entry must be a UartEntry, got {type(entry).__name__}")
        if half_duplex or entry is not None:
            if not self.supports("uart_hd"):
                fw = self.capabilities.firmware_version or "unknown"
                raise UnsupportedFeatureError(
                    f"UART half_duplex and entry need gateware v57 (capability uart_hd; this pod "
                    f"runs firmware {fw}): update the pod's firmware",
                    feature="uart_hd", firmware_version=self.capabilities.firmware_version)
        if de is not None and not self.supports("uart_de"):
            fw = self.capabilities.firmware_version or "unknown"
            raise UnsupportedFeatureError(
                f"UART de (RS-485 direction) needs gateware v58 (feature uart_de; this pod runs "
                f"firmware {fw}): update the pod's firmware",
                feature="uart_de", firmware_version=self.capabilities.firmware_version)
        if entry is not None and not self.supports("nrst_pin"):
            raise UnsupportedFeatureError(
                "UART entry needs the pod's reset pin, which this board does not have (v3 boards "
                "have it): open the UART without entry and reset the target yourself",
                feature="nrst_pin", firmware_version=self.capabilities.firmware_version)
        rx_i = self._wired(rx, "uart_rx")
        if half_duplex:
            # One wire: tx defaults to rx, not to the profile's uart_tx.
            tx_i = rx_i if tx is None else self._wired(tx, "uart_tx")
            if tx_i != rx_i:
                raise ValueError(f"half_duplex runs on one pin: rx is LA{rx_i} and tx is LA{tx_i}; "
                                 "omit tx or give the same channel")
        else:
            tx_i = self._wired(tx, "uart_tx")
        b = int(self.wiring.uart_baud if baud is None else baud)
        kw: Dict[str, Any] = {}
        if parity != "none" or stop_bits != 1:
            kw.update(parity=parity, stop_bits=stop_bits)
        if half_duplex:
            kw["half_duplex"] = True
        if entry is not None:
            kw["entry"] = entry
        if de is not None:
            de_i = self._wired(de, "uart_de")
            if de_i in (rx_i, tx_i):
                raise ValueError(f"de needs a pin of its own: LA{de_i} is the UART's rx or tx")
            kw["de"] = de_i
            if de_active_low:
                kw["de_active_low"] = True
        with _classified_errors():
            return self._transport.uart_proxy_start(rx_i, tx_i, b, **kw)

    def _usb_uart_link(self, rx: Any, tx: Any, baud: Optional[int], parity: Optional[str],
                       stop_bits: Optional[int], half_duplex: bool, entry: Any, de: Any,
                       port: str, dtr: Optional[bool], rts: Optional[bool]) -> Any:
        """The raw link of ``uart_proxy_start`` on the board's USB serial port: no LA channel, no
        wiring profile (its frame and baud are for the DUT's UART pins) and no I/O voltage."""
        if port != "usb":
            raise ValueError(f"port must be \"usb\" (the board's USB serial port) or left out for LA "
                             f"pins, got {port!r}")
        if rx is not None or tx is not None:
            raise ValueError("rx and tx are LA channels: with port=\"usb\" the UART is the board's USB "
                             "serial port and has none")
        if half_duplex or entry is not None or de is not None:
            raise ValueError("half_duplex, entry and de are for UARTs on LA pins: they do not apply "
                             "with port=\"usb\"")
        self._require_usb("uart_usb", "The USB serial port")
        kw: Dict[str, Any] = {"port": "usb", "parity": parity or "none", "stop_bits": stop_bits or 1,
                              "dtr": True if dtr is None else bool(dtr),
                              "rts": True if rts is None else bool(rts)}
        with _classified_errors():
            return self._transport.uart_proxy_start(None, None, int(115200 if baud is None else baud), **kw)

    def power_cycle_and_capture(
        self,
        *,
        rx: Union[Pin, int, str, None] = None,
        tx: Union[Pin, int, str, None] = None,
        efuse: Optional[Union[Efuse, int]] = None,
        delay: float = 1.0,
        duration: float = 4.0,
        baud: Optional[int] = None,
        until: Optional[_uart.Until] = None,
        off_settle: float = 0.3,
        parity: Optional[str] = None,
        stop_bits: Optional[int] = None,
    ) -> _uart.UartCapture:
        """Power-cycle the target while capturing its boot output.

        Powers the eFuse off, waits ``off_settle`` seconds, schedules a power-on ``delay`` seconds
        out (pod-side timer), then captures UART for ``duration`` seconds — so the power-on and
        the DUT's boot banner land *inside* the window. ``duration`` must exceed ``delay``.
        """
        if duration <= delay:
            raise ValueError(f"duration ({duration!r}s) must exceed delay ({delay!r}s) or the "
                             "power-on lands after the capture window")
        self.power_off(efuse)
        if off_settle:
            time.sleep(off_settle)
        self.power_on(efuse, delay=delay)
        return self.capture_uart(rx=rx, tx=tx, baud=baud, duration=duration, until=until,
                                 parity=parity, stop_bits=stop_bits)

    def open_uart(
        self,
        *,
        rx: Union[Pin, int, str, None] = None,
        tx: Union[Pin, int, str, None] = None,
        baud: Optional[int] = None,
        max_buffer: int = 1 << 20,
        parity: Optional[str] = None,
        stop_bits: Optional[int] = None,
        encoding: str = "utf-8",
        half_duplex: bool = False,
        entry: Optional[_uart.UartEntry] = None,
        de: Union[Pin, int, str, None] = None,
        de_active_low: bool = False,
        port: Optional[str] = None,
        dtr: Optional[bool] = None,
        rts: Optional[bool] = None,
    ) -> _uart.UartSession:
        """Open an event-based UART session (a background reader buffers the DUT's output).

        Unlike :meth:`capture_uart` (a fixed window) it buffers from the moment it opens, so you
        can start listening *before* an action and read the result afterwards, and write to the
        DUT's console::

            with bp.open_uart(rx=5, tx=4) as uart:
                bp.power_on(benchpod.INTERNAL)      # immediate, no pod-side delay
                uart.expect("login:", timeout=6)
                uart.write("help\\n")

        Over the cloud, other commands run on the cloud command channel while a session is open.
        Omitted ``rx``, ``tx`` and ``baud`` come from the wiring profile. A channel in GPIO mode (or
        used by anything else) is refused with :class:`~embeddedci.benchpod.errors.PinConflictError`.

        The frame is the wiring profile's (``uart_parity``, ``uart_stop_bits``; 8N1 unless it says
        otherwise) unless ``parity`` (``"none"``, ``"even"``, ``"odd"``) or ``stop_bits``
        (1 or 2) say otherwise. The LA pins are 1.8/3.3 V only: a 5 V UART needs a level shifter, and
        an RS-485 bus (Modbus RTU) an external RS-485 transceiver, between the pod and the device::

            with bp.open_uart(rx=5, tx=4, baud=19200, parity="even") as uart:
                uart.write(request)

        Anything but 8N1 needs firmware 3.10.8+ (gateware v56, capability ``uart_frame``); older
        firmware is refused with :class:`~embeddedci.benchpod.errors.UnsupportedFeatureError`
        before anything is sent. The receiver checks the parity bit and the first stop bit; a
        byte with a wrong parity bit is still delivered, and ``status()["uart_rx_parity_error"]``
        turns true for the session.

        ``encoding`` turns the received bytes into text (and text you write into bytes). For a
        binary protocol pass ``"latin-1"``: every byte is one character, so
        ``uart.read().encode("latin-1")`` gives the bytes back unchanged.

        ``half_duplex=True`` runs a one-wire UART on ``rx`` alone (``tx`` defaults to it): the pod
        only pulls the line low and lets the pull-up hold it high, so the device answers on the
        same wire, and the pod does not read back its own bytes. Use LA1 to LA6 (the pod turns
        their pull-up on for the session) or a pull-up on the board.

        ``entry`` runs a boot-mode entry on the pod before the session starts: reset with TX held
        low, release, then the first bytes, timed on the pod (see :class:`UartEntry`)::

            entry = benchpod.UartEntry(reset_ms=5, hold_ms=3, idle_ms=2, data=b"\\x00")
            with bp.open_uart(rx=3, half_duplex=True, entry=entry, encoding="latin-1") as uart:
                uart.write(...)

        ``de`` is the LA channel on an RS-485 transceiver's DE (tied to /RE): the pod drives it
        itself, high from before the first start bit of what it sends to the end of the last stop
        bit, low otherwise, so the transceiver listens whenever the pod is not sending and a fast
        device's reply is not lost (switching DE from the host takes tens of milliseconds).
        ``de_active_low`` inverts it. Needs gateware v58 (feature ``uart_de``)::

            with bp.open_uart(rx=5, tx=4, de=9, baud=19200, parity="even",
                              encoding="latin-1") as uart:
                uart.write(request)          # no DE handling on the host

        ``half_duplex`` and ``entry`` need gateware v57 (capability ``uart_hd``); ``entry`` also needs the pod's reset pin
        (capability ``nrst_pin``). Older pods are refused with
        :class:`~embeddedci.benchpod.errors.UnsupportedFeatureError` before anything is sent.

        ``port="usb"`` opens the serial port of the board the pod hosts over USB
        (:meth:`usb_host`) instead of LA pins: a CDC ACM port, or the FTDI, CP210x or CH34x chip of
        a dev kit, so a test reads the boot log over the cable the board was flashed through. No LA
        channel, pull-up or I/O voltage is used, and ``rx``, ``tx``, ``half_duplex``, ``entry`` and
        ``de`` do not apply. ``baud`` (default 115200), ``parity``, ``stop_bits`` and the control
        lines ``dtr`` and ``rts`` (both ``True`` by default: many boards reset or enter their loader
        on them) are applied by the pod, and again when the board re-enumerates. Needs firmware
        4.0.3+ (feature ``uart_usb``, else :class:`~embeddedci.benchpod.errors.UnsupportedFeatureError`).
        A pod without host mode or a serial port is refused with
        :class:`~embeddedci.benchpod.errors.UsbSerialUnavailableError` (``BP0085``), and while a
        :meth:`usb_flash` or :meth:`usb_check` runs with
        :class:`~embeddedci.benchpod.errors.UsbSerialBusyError` (``BP0086``)::

            bp.usb_host(timeout_s=300)
            bp.usb_flash("build/app.bin", address=0x10000)
            with bp.open_uart(port="usb", baud=115200) as uart:
                uart.expect("ready", timeout=10)
        """
        codecs.lookup(encoding)  # an unknown encoding fails here, before the proxy starts
        link = self._uart_link(rx, tx, baud, parity, stop_bits, half_duplex, entry, de, de_active_low,
                               port=port, dtr=dtr, rts=rts)
        return _uart.UartSession(link, max_buffer=max_buffer, encoding=encoding)

    # -- SPI master / SPI flash ------------------------------------------------

    def open_spi(
        self,
        *,
        sck: Union[Pin, int, str, None] = None,
        mosi: Union[Pin, int, str, None] = None,
        miso: Union[Pin, int, str, None] = None,
        cs: Union[Pin, int, str, None] = None,
        hz: int = 1_000_000,
        mode: int = 0,
    ) -> _spi.SpiSession:
        """Arm the SPI master on four LA pins and return a :class:`~embeddedci.benchpod.spi.SpiSession`
        (a context manager; closing it releases the pins)::

            with bp.open_spi(sck=13, mosi=14, miso=6, cs=5, hz=6_000_000) as spi:
                print(spi.flash_id())
                spi.flash_program(open("fw.bin", "rb").read())

        Omitted pins come from the wiring profile (``spi_sclk``, ``spi_mosi``, ``spi_miso``,
        ``spi_cs``). ``hz`` is rounded down to 24 MHz / (2 * n), n = 2..63 (6 MHz .. 190 kHz); the
        session's ``hz`` is the rate used. ``mode`` is 0..3 (CPOL = mode / 2, CPHA = mode % 2);
        modes 1 and 2 need gateware v55+. Needs gateware v45+ (capability ``spi_master``); an SWD
        flash or an I2C session cannot run while a session is open.
        """
        self._require_capability("spi_master", "the SPI master")
        self._yield_expansion_bus()
        if isinstance(mode, bool) or mode not in (0, 1, 2, 3):
            raise ValueError(f"mode must be 0, 1, 2 or 3, got {mode!r}")
        if mode in (1, 2):
            self._require_gateware(BUS_MASTER_GATEWARE, "SPI modes 1 and 2")
        session = self._start_spi(self._wired(sck, "spi_sclk"), self._wired(mosi, "spi_mosi"),
                                  self._wired(miso, "spi_miso"), self._wired(cs, "spi_cs"),
                                  hz=hz, mode=mode)
        self._spi_session_ref = weakref.ref(session)
        return session

    def _start_spi(self, sck: int, mosi: int, miso: int, cs: int, *, hz: int,
                   mode: int) -> _spi.SpiSession:
        """``spi_start`` on four resolved channels (open_spi, and an expander's own session)."""
        self._require_capability("spi_master", "the SPI master")
        self._ensure_bus_masters("the SPI master")
        req = {"cmd": "spi_start", "sck": sck, "mosi": mosi, "miso": miso, "cs": cs,
               "clock_hz": int(hz), "mode": int(mode)}
        stage = getattr(self._transport, "stage_psram", None)
        with _classified_errors():
            info = _dict(self.command(req))
        return _spi.SpiSession(self.command, info, stage=stage)

    # -- I2C master --------------------------------------------------------------

    def open_i2c(
        self,
        sda: Union[Pin, int, str, None] = None,
        scl: Union[Pin, int, str, None] = None,
        *,
        hz: int = 100_000,
        pullups: str = "pod",
    ) -> _i2c_master.I2CMaster:
        """Arm the pod's I2C master on two LA pins and return an
        :class:`~embeddedci.benchpod.i2c_master.I2CMaster` (a context manager; closing it releases
        the pins)::

            bp.enable_pullup(1, 2)                      # SDA, SCL on LA1, LA2
            with bp.open_i2c(hz=400_000) as i2c:
                print([hex(a) for a in i2c.scan()])
                chip_id = i2c.read_reg(0x76, 0xD0)[0]

        Omitted pins come from the wiring profile (``i2c_sda``, ``i2c_scl``). ``hz`` is
        10 kHz..1 MHz; the session's ``hz`` is the rate the pod runs, and a real bus runs somewhat
        slower because the SCL rise time adds to each period. ``pullups="pod"`` (the default) needs
        both pins on LA1..LA6 with the pod's pull-ups switched on (:meth:`enable_pullup`; they pull to
        3.3 V, so the LA voltage must be 3.3 V); ``"external"`` when the board has its own. Needs
        gateware v55+. SWD, SPI and I2C sessions exclude each other.
        """
        sda_i, scl_i = self._wired(sda, "i2c_sda"), self._wired(scl, "i2c_scl")
        self._yield_expansion_bus()
        i2c = self._start_i2c(sda_i, scl_i, hz=hz, pullups=pullups)
        self._i2c_master_ref = weakref.ref(i2c)
        return i2c

    def _start_i2c(self, sda: int, scl: int, *, hz: int, pullups: str) -> _i2c_master.I2CMaster:
        """``i2c_start`` on two resolved channels (open_i2c, and an expander's own session)."""
        check_choice(pullups, _i2c_master.PULLUPS, "pullups")
        if isinstance(hz, bool) or not _i2c_master.HZ_MIN <= int(hz) <= _i2c_master.HZ_MAX:
            raise ValueError(f"hz must be {_i2c_master.HZ_MIN}..{_i2c_master.HZ_MAX}, got {hz!r}")
        self._require_gateware(BUS_MASTER_GATEWARE, "the I2C master")
        self._ensure_bus_masters("the I2C master")
        req = {"cmd": "i2c_start", "sda": sda, "scl": scl, "clock_hz": int(hz), "pullups": pullups}
        if req["sda"] == req["scl"]:
            raise ValueError("sda and scl must be different LA pins")
        try:
            with _classified_errors():
                info = _dict(self.command(req))
        except FirmwareError as exc:
            if exc.code == "BP0003":   # i2c_session_open
                exc.args = (self._i2c_busy_text(exc),)
            raise
        return _i2c_master.I2CMaster(self._classified_command, info)

    def _i2c_busy_text(self, exc: FirmwareError) -> str:
        """The refusal of ``i2c_start`` while a session is open, naming who holds it: this
        BenchPod's own unclosed :meth:`open_i2c` session, or another connection to the pod."""
        prefix = f"{exc.cmd}: " if exc.cmd else ""
        mine = self._i2c_master_ref() if self._i2c_master_ref is not None else None
        if mine is not None and not mine.closed:
            return (f"{prefix}i2c busy: this BenchPod already has an I2C session open (LA{mine.sda} "
                    f"SDA, LA{mine.scl} SCL): close it first (i2c.close(), or leave its with block)")
        where = ""
        try:
            st = _dict(self.command({"cmd": "i2c_status"}))
            if st.get("active"):
                where = f" (LA{st.get('sda')} SDA, LA{st.get('scl')} SCL, {st.get('clock_hz')} Hz)"
        except Exception:
            pass
        return (f"{prefix}i2c busy: another connection to this pod holds its I2C master{where}, "
                "for example the desktop app, the web app, an AI agent or another script. Close the "
                "I2C session there first; it is not one this script opened")

    def _classified_command(self, req: Dict[str, Any]) -> Any:
        with _classified_errors():
            return self.command(req)

    def spi_flash(
        self,
        image: Union[bytes, str, "os.PathLike[str]"],
        addr: int = 0,
        *,
        erase: bool = True,
        verify: bool = True,
        hold_reset: bool = False,
        sck: Union[Pin, int, str, None] = None,
        mosi: Union[Pin, int, str, None] = None,
        miso: Union[Pin, int, str, None] = None,
        cs: Union[Pin, int, str, None] = None,
        hz: int = 6_000_000,
        mode: int = 0,
        progress: Optional[_spi.Progress] = None,
    ) -> _spi.SpiFlashResult:
        """Program an image (bytes or a file path) into the SPI NOR flash on the SPI pins.

        Erases the sectors the image covers (unless ``erase=False``), writes it 768 bytes per
        command and verifies every chunk (unless ``verify=False``). ``hold_reset`` holds the DUT
        in reset for the whole job (rev3 pods), so its own controller does not drive the same
        bus; it is released again afterwards, also on failure. Pins come from the wiring
        profile unless given. ``progress(done, total)`` is called as it goes.
        """
        data = bytes(image) if isinstance(image, (bytes, bytearray, memoryview)) else open(image, "rb").read()
        if hold_reset:
            self.set_reset(True)
        try:
            with self.open_spi(sck=sck, mosi=mosi, miso=miso, cs=cs, hz=hz, mode=mode) as spi:
                return spi.flash_program(data, addr, erase=erase, verify=verify, progress=progress)
        finally:
            if hold_reset:
                self.set_reset(False)

    # -- motor & battery emulator ----------------------------------------------

    def open_motor_emulator(
        self,
        *,
        sck: Union[Pin, int, str, None] = None,
        mosi: Union[Pin, int, str, None] = None,
        miso: Union[Pin, int, str, None] = None,
        cs: Union[Pin, int, str, None] = None,
        programn: Union[Pin, int, str, None] = None,
        done: Union[Pin, int, str, None] = None,
        hz: int = 6_000_000,
        calibration: Optional[_motor_emulator.EmulatorCalibration] = None,
    ) -> _motor_emulator.MotorEmulator:
        """Open the motor & battery emulator stack on its SPI link and return a
        :class:`~embeddedci.benchpod.motor_emulator.MotorEmulator` (a context manager)::

            with bp.open_motor_emulator() as emu:
                emu.configure("emu.bit")
                print(emu.probe(0))

        The SPI pins come from the wiring profile unless given. ``programn`` (open drain) and
        ``done`` (input) default to the profile's ``PROGRAMN`` and ``DONE`` signals when it has
        them; without them the stack can be used but not configured. The link runs at up to
        6 MHz, mode 0. Configuring needs the pod's ``spi_stream`` command.
        """
        pins: Dict[str, Optional[int]] = {}
        for key, value, default in (("programn", programn, "PROGRAMN"), ("done", done, "DONE")):
            if value is not None:
                pins[key] = self._resolve_la(value, key)[0]
            else:
                try:
                    pins[key] = self.wiring.la(default)
                except ValueError:
                    pins[key] = None
        if hz > 6_000_000:
            raise ValueError("the emulator link runs at up to 6 MHz")
        spi = self.open_spi(sck=sck, mosi=mosi, miso=miso, cs=cs, hz=hz, mode=0)
        pin_pn = pin_done = None
        try:
            pin_pn = self.gpio(pins["programn"], "open_drain") if pins["programn"] else None
            pin_done = self.gpio(pins["done"], "input") if pins["done"] else None
        except Exception:
            spi.close()
            if pin_pn is not None:
                pin_pn.release()
            raise
        return _motor_emulator.MotorEmulator(spi, programn=pin_pn, done=pin_done, calibration=calibration)

    # -- ADC / logic-analyzer capture -----------------------------------------

    def capture_adc(self, samples: int = 4096, *, sample_rate_hz: Optional[float] = None,
                    source: Optional[AdcSource] = None, trigger: Optional[Trigger] = None,
                    trigger_timeout: float = 10.0, range: Optional[AdcRange] = None,
                    oversample: bool = False) -> Capture:
        """Capture ADC samples and return a :class:`Capture` with CALIBRATED volts.

        Works over any transport. ``sample_rate_hz`` omitted = the device's maximum rate; the
        achieved rate is on the result. Above 32768 samples the capture streams from PSRAM, so
        multi-second captures work. ``source`` first routes that ADC source (``ext``/``cal1``/
        ``cal2``/``current_in``) — omitted, the current routing is left alone. ``volts`` use the front-SMA
        calibration; for the other sources compare ``counts`` or use :meth:`adc_read`.

        ``source="current_in"`` (the 4-20 mA input, J8) is the exception: on a pod that reports its
        own fit for it (:attr:`Capabilities.calibrate`) ``volts`` are scaled with that fit, and
        ``currents`` holds the loop current per sample in amps. Below 400 kS/s a capture reads
        about 10 to 20 µA higher than :meth:`adc_read`: the input stage shifts slightly at slow
        sample rates, and that is not corrected.

        ``trigger`` (a :class:`Trigger`, gateware >= v35) waits for an edge or level on an LA channel
        before sampling, so t = 0 is that moment; a :class:`~embeddedci.benchpod.errors.TriggerTimeout`
        is raised after ``trigger_timeout`` seconds without it.

        ``range`` sets the ADC input range first (:meth:`adc_range`): ``"low"`` captures up to
        ±5.46 V at about 6x the resolution (analog module v3r4+). Without it the capture is taken
        in the range the pod is in. ``Capture.range`` says which, and ``Capture.overrange`` whether
        a sample clipped.

        ``oversample=True`` converts at the ADC's maximum rate (400 kS/s) and averages down to
        ``sample_rate_hz``, for lower noise and a smaller step at 0 V. The conversions must fit in
        the pod's memory, about 5 s of capture, or it raises :class:`BenchPodError`. The rate is the
        nearest whole ratio of 400 kS/s, so check ``sample_rate_hz`` on the result. ``volts`` and
        ``mean_counts`` keep the fraction, ``counts`` are rounded raw codes, ``oversample`` is how
        many conversions each sample averages, and ``clipped_samples`` counts clipped conversions.
        The average lowers the bandwidth to about 0.44 times the returned rate.
        """
        label, fit = self._route_adc_source(source, range)
        trig = self._resolve_trigger(trigger)
        take = _capture.capture_adc_oversampled if oversample else _capture.capture_adc
        with _classified_errors():
            cap = take(self._transport, self.capabilities, samples=samples,
                       sample_rate_hz=sample_rate_hz, source=label, trigger=trig,
                       trigger_timeout=trigger_timeout)
        return self._scale_current_in(cap, fit)

    def _route_adc_source(self, source: Optional[str],
                          rng: Optional[str] = None) -> Tuple[str, Optional[Calibration]]:
        """Route an ADC ``source`` for a capture (``None`` leaves the routing alone) in range
        ``rng`` (``None`` leaves it as it is). Returns the label for the result and, for
        ``current_in`` on a pod that reports one, the pod's own fit for that input in the range
        in use, read before the capture starts."""
        if source is None:
            if rng is not None:
                self.adc_range(rng)  # type: ignore[arg-type]
            return "", None
        check_choice(source, ADC_SOURCES, "source")
        self.analog_path(ADC_SOURCE_PATHS[source], range=rng)  # type: ignore[arg-type]
        time.sleep(0.02)  # relays (~4 ms) + front-end RC settle, as the firmware's adc_read does
        fit = self.calibration() if source == "current_in" and self.capabilities.calibrate else None
        return source, fit

    @staticmethod
    def _scale_current_in(cap: Capture, fit: Optional[Calibration]) -> Capture:
        """Rescale a capture of the 4-20 mA input with the pod's fit and fill ``currents`` (amps)."""
        if fit is not None and fit.b:
            if cap.mean_counts:
                unwrapped: Any = (c + ADC_WRAP_SPAN for c in cap.mean_counts)
            else:
                unwrapped = (c + ADC_WRAP_SPAN if c < ADC_WRAP_THRESHOLD else c for c in cap.counts)
            cap.volts = [fit.a + fit.b * c for c in unwrapped]
            ohms = fit.sense_ohms or _CURRENT_IN_SENSE_OHMS
            cap.currents = [v / ohms for v in cap.volts]
        return cap

    def capture_la(self, samples: int = 4096, *, sample_rate_hz: Optional[float] = None,
                   stop_dac_after: Optional[float] = None, trigger: Optional[Trigger] = None,
                   trigger_timeout: float = 10.0,
                   send: Optional[_i2c_master.Send] = None) -> LaCapture:
        """Capture raw 14-channel logic-analyzer words and return a :class:`LaCapture`.

        ``sample_rate_hz`` goes up to 12 MS/s, or 48 MS/s on gateware v50 and later
        (:attr:`Capabilities` ``la_max_rate_hz``): above 12 MS/s the pod runs 48, 24 or 16 MS/s and
        records only the changes, so a signal changing more than about 6 million times per second
        (all channels together) for longer than its buffer lasts stops early: the capture then holds
        the samples up to there and :attr:`LaCapture.truncated` says why. ``LaCapture.sample_rate_hz``
        is always the rate the pod ran. ``Trigger(..., pre_samples=N)`` also returns samples from
        before the trigger (gateware v50+, any rate up to 48 MS/s). At 12 MS/s and below a capture
        deeper than the pod's burst buffer has to fit it at the rate asked: leave ``sample_rate_hz``
        out and the pod runs the fastest rate that fits; a rate that does not fit raises
        :class:`FirmwareError` naming the most samples at that rate and the fastest rate for that
        depth (firmware 3.10.0+; older firmware slowed the capture down).

        ``stop_dac_after`` (seconds) cuts a concurrently-running DAC that far into the capture,
        sample-precise from the capture's hardware t0 (gateware >= v21). ``trigger`` (a
        :class:`Trigger`, gateware >= v35) makes t0 an edge or level on an LA channel —
        ``capture_la(..., trigger=Trigger("READY", "rising"))`` — and raises
        :class:`~embeddedci.benchpod.errors.TriggerTimeout` after ``trigger_timeout`` seconds without it.

        ``send`` (gateware v55+) runs one transaction right after the capture arms, in the same
        command, so a trigger on the bus line catches it (over the cloud too): an
        :class:`~embeddedci.benchpod.i2c_master.I2CSend` (an open :meth:`open_i2c` session), a
        :class:`~embeddedci.benchpod.i2c_master.SpiSend` (an open :meth:`open_spi` session) or a
        :class:`~embeddedci.benchpod.i2c_master.GpioSend` (a GPIO output). Trigger on the line it
        starts with: SDA falling, CS falling, or the GPIO pin's edge. Its result is
        :attr:`LaCapture.send_result`; a failed transfer is reported there and the capture still
        returns. With an edge trigger and no ``pre_samples``, a send keeps 64 samples from before
        the trigger, so the decoder sees the first START::

            with bp.open_i2c() as i2c:
                cap = bp.capture_la(8192, sample_rate_hz=12e6, trigger=Trigger("i2c_sda", "falling"),
                                    send=i2c.send(0x76, b"\xd0", read=1))
            assert cap.send_result.ok
            print(bp.decode(cap, "i2c", sda=1, scl=2))
        """
        trig = self._resolve_trigger(trigger)
        send_req = None
        if send is not None:
            send_req = self._send_request(send)
            if trig is not None and trig.edge in ("rising", "falling") and not trig.pre_samples:
                trig = Trigger(trig.la, trig.edge, _SEND_PRE_SAMPLES)
        with _classified_errors():
            return _capture.capture_la(self._transport, samples=samples,
                                       sample_rate_hz=sample_rate_hz, stop_dac_after=stop_dac_after,
                                       trigger=trig, trigger_timeout=trigger_timeout, send=send_req)

    def _send_request(self, send: Any) -> Dict[str, Any]:
        if isinstance(send, _i2c_master.GpioSend) and isinstance(send.la, str):
            send = _i2c_master.GpioSend(self._resolve_la(send.la, "send la")[0], send.level)
        if not isinstance(send, (_i2c_master.I2CSend, _i2c_master.SpiSend, _i2c_master.GpioSend)):
            raise ValueError(f"send must be an I2CSend, SpiSend or GpioSend, got {send!r}")
        req = send.to_request()
        self._require_gateware(BUS_MASTER_GATEWARE, "send and capture")
        if not isinstance(send, _i2c_master.GpioSend):
            self._ensure_bus_masters("the I2C master" if isinstance(send, _i2c_master.I2CSend)
                                     else "the SPI master")
        return req

    def capture_correlated(self, *, adc_samples: int = 4096,
                           adc_sample_rate_hz: Optional[float] = None,
                           la_samples: int = 4096, la_sample_rate_hz: Optional[float] = None,
                           stop_dac_after: Optional[float] = None,
                           trigger: Optional[Trigger] = None,
                           trigger_timeout: float = 10.0,
                           source: Optional[AdcSource] = None) -> CorrelatedCapture:
        """ADC + LA captured from ONE hardware trigger, so the two timebases align.

        Set either count to 0 for a single stream. ``stop_dac_after`` (seconds) cuts a running DAC
        that far into the capture (gateware >= v21). Pair with ``replay(..., on_capture=True)`` or
        ``generate(..., on_capture=True)`` for a phase-locked stimulus → capture → cutoff run.
        ``trigger`` starts both streams on an LA edge or level (see :meth:`capture_la`).
        Needs a streaming transport (TCP, serial or cloud).

        ``source`` first routes that ADC source for the analog half, as in :meth:`capture_adc`;
        omitted, the routing is left alone. ``source="current_in"`` captures the 4-20 mA input
        (J8) next to the logic channels: ``adc.currents`` is then the loop current in amps. There
        is one ADC, so a capture is the SMA voltage or the J8 current, never both.
        """
        label, fit = self._route_adc_source(source if adc_samples else None)
        trig = self._resolve_trigger(trigger)
        with _classified_errors():
            cc = _capture.capture_correlated(
                self._transport, self.capabilities, adc_samples=adc_samples,
                adc_sample_rate_hz=adc_sample_rate_hz, la_samples=la_samples,
                la_sample_rate_hz=la_sample_rate_hz, stop_dac_after=stop_dac_after,
                trigger=trig, trigger_timeout=trigger_timeout)
        if label:
            cc.adc.source = label
        self._scale_current_in(cc.adc, fit)
        return cc

    def _resolve_trigger(self, trigger: Optional[Trigger]) -> Optional[Trigger]:
        if trigger is None:
            return None
        if not isinstance(trigger, Trigger):
            raise ValueError(f"trigger must be a Trigger, e.g. Trigger(9, 'rising'), got {trigger!r}")
        self._require_capability("capture_trigger", "capture triggers")
        if isinstance(trigger.la, str):
            return Trigger(self._resolve_la(trigger.la, "trigger")[0], trigger.edge, trigger.pre_samples)
        return trigger

    def decode(self, source: Union[LaCapture, Sequence[int]], protocol: DecodeProtocol = "i2c", *,
               sample_rate_hz: Optional[float] = None, **channels: Any) -> Any:
        """Decode a protocol from an LA capture (or raw 14-bit words) off-device.

        Channels by protocol: i2c ``sda``, ``scl``; uart ``rx``, ``baud``; spi ``sclk`` (+ ``mosi``,
        ``miso``, ``cs``, ``mode``); usb ``dp``, ``dm`` (+ ``speed``); modbus ``rx``, ``baud`` (+
        ``tx``); can ``can``, ``bitrate`` (+ ``data_bitrate``, ``sample_point``); pwm ``channels``;
        onewire ``onewire``; smbus / pmbus ``sda``, ``scl`` (+ ``pec``). USB returns a
        :class:`~embeddedci.benchpod.usb_decode.UsbDecode`, PWM a
        :class:`~embeddedci.benchpod.pwm_decode.PwmDecode`, the others a list. See
        :func:`embeddedci.benchpod.decode.decode`.
        """
        from . import decode as _decode

        check_choice(protocol, DECODE_PROTOCOLS, "protocol")
        if isinstance(source, LaCapture):
            words = source.words
            if sample_rate_hz is None:
                sample_rate_hz = source.sample_rate_hz
        else:
            words = list(source)
        return _decode.decode(words, protocol, sample_rate_hz=sample_rate_hz or 0.0, **channels)

    # -- output paths: volts, or amps on the 4-20 mA output ------------------------

    def _output_range(self, dac_path: str) -> tuple:
        """``(min, max)`` an output path spans, in its own unit: volts, or AMPS on ``current_out``.

        The 4-20 mA output's range is the pod's own (``current_out_range``), read once.
        """
        if dac_path != _CURRENT_OUT:
            return _dsp.dac_path_range_v(dac_path)
        if self._current_out_span is None:
            rng = self.current_out_range()
            self._current_out_span = (rng.min_current, rng.max_current)
        return self._current_out_span

    def _route_output(self, dac_path: str) -> None:
        """Route an output before a waveform plays on it. The 4-20 mA output only switches the DAC
        voltage outputs off: they share the DAC and would follow the current."""
        if dac_path == _CURRENT_OUT:
            self.command({"cmd": "analog_path", "path": _CURRENT_OUT})
        else:
            self.command({"cmd": "dac_out", "path": dac_path})
            self._dac_routed()
        self._routed_dac_path = dac_path

    def _require_dac_cal1(self, path: Optional[str]) -> None:
        """Refuse ahead, in words, when ``path`` is ``cal1`` and the pod's firmware lacks it."""
        if path != "cal1" or self.supports("dac_out_cal1"):
            return
        fw = self.capabilities.firmware_version or "unknown"
        first = _feature_map.first_firmware("dac_out_cal1") or "the next release"
        raise UnsupportedFeatureError(
            f"path 'cal1' (the 5 V DAC stage routed into the ADC loopback) needs firmware {first} "
            f"or later (this pod runs firmware {fw}): update the pod, or use analog_path('cal1') "
            "after dac_output('5v', ...)",
            feature="dac_out_cal1", firmware_version=self.capabilities.firmware_version)

    @staticmethod
    def _refuse_replay_cal1(dac_path: Optional[str]) -> None:
        if dac_path == "cal1":
            raise ValueError("dac_path 'cal1' is for dac_output and generate; replay on the 5 V "
                             "stage with dac_path='5v' and analog_path('cal1') (route=False)")

    def _default_dac_path(self, route: bool) -> str:
        """The output a waveform plays on when ``dac_path`` is left out.

        Routed (the default): ``3v3``, the lowest voltage output, so a call that leaves the output
        out cannot put 5 V on a 3.3 V pin. With ``route=False`` nothing is switched and the path
        only sets the volts scaling, so it follows what this connection last routed (``cal1`` is
        the 5 V DAC, ``cal2`` the 12 V one), else ``5v``, the cal1 loopback's scaling.
        """
        if route:
            return DEFAULT_DAC_PATH
        routed = self._routed_dac_path
        return "5v" if routed in (None, "cal1") else routed

    def _stop_current_out(self) -> None:
        """Stop a waveform on the 4-20 mA output and return the loop to its live zero. ``dac_stop``
        alone leaves the DAC, and so the loop, where the waveform stopped."""
        self.dac_stop()
        self.command({"cmd": "current_out", "ua": _CURRENT_OUT_ZERO_UA})

    def _stop_for(self, dac_path: str) -> Callable[[], None]:
        return self._stop_current_out if dac_path == _CURRENT_OUT else self.dac_stop

    # -- DAC generate / stop ----------------------------------------------------

    def generate(self, waveform: Waveshape = "sine", *, freq_hz: float, amplitude: float,
                 offset: Optional[float] = None, dac_path: Optional[DacPath] = None,
                 duration: Optional[float] = None, sample_rate_hz: Optional[float] = None,
                 on_capture: bool = False, route: bool = True) -> DacHandle:
        """Generate a parametric waveform (``sine``/``square``/``sawtooth``) on a DAC output path.

        ``amplitude`` is the peak in volts and ``offset`` the center in volts (default: the middle
        of ``dac_path``'s range — 0 V on the bipolar ±12 V ``12v`` path). The pod takes the volts,
        converts them with its per-path calibration and plays the waveform at the DAC's full 16
        bits; both peaks must be on the path (a peak within 5% of an end the pod cannot quite
        reach is clamped to it).

        ``dac_path="current_out"`` plays it as a current on the 4-20 mA output (J9):
        ``amplitude`` and ``offset`` are then AMPS (``amplitude=0.006, offset=0.012`` swings 6 to
        18 mA, in steps of about 0.25 µA). The DAC voltage outputs are switched off, and stopping
        the handle returns the loop to 4 mA. See :meth:`current_out` for the wiring.
        ``duration`` (seconds) stops it by itself; omitted, it runs until :meth:`dac_stop` or the
        returned handle's ``stop()``. ``on_capture=True`` defers the start to the next capture's
        hardware t0 (phase-locked co-trigger, gateware >= v27); ``handle.cotrig`` reports whether
        it armed.

        By default the output path is routed first. ``route=False`` keeps the current analog
        switching — e.g. after ``analog_path("cal1")`` for a DAC→ADC loopback, which routing the
        output would undo — and ``dac_path`` then only sets the volts scaling.

        ``dac_path`` left out is ``3v3``, the lowest voltage output: pass the output your DUT pin
        tolerates. With ``route=False`` it is the output this connection last routed instead
        (``cal1`` → ``5v``, ``cal2`` → ``12v``), else ``5v``.
        """
        check_choice(waveform, WAVESHAPES, "waveform")
        if dac_path is None:
            dac_path = self._default_dac_path(route)  # type: ignore[assignment]
        check_choice(dac_path, DAC_PATHS, "dac_path")
        if route:
            self._require_dac_cal1(dac_path)
        elif dac_path == "cal1":
            dac_path = "5v"  # no routing: the path only sets the volts scaling
        if freq_hz <= 0:
            raise ValueError(f"freq_hz must be > 0, got {freq_hz!r}")
        vmin, vmax = self._output_range(dac_path)
        current = dac_path == _CURRENT_OUT
        unit = "A" if current else "V"
        amp = float(amplitude)
        step = (vmax - vmin) / 65535.0
        if not amp >= step:
            raise ValueError(f"amplitude must be at least {step:.3g} {unit} (one 16-bit step) on the "
                             f"{dac_path} path, got {amplitude!r}")
        off = (vmin + vmax) / 2.0 if offset is None else float(offset)
        # A little slack for the float edges of the range; the pod checks against its calibration.
        slack = (vmax - vmin) * 1e-6
        if off - amp < vmin - slack or off + amp > vmax + slack:
            raise ValueError(f"the peaks {off - amp:.4g} to {off + amp:.4g} {unit} must stay within "
                             f"{vmin:.4g}..{vmax:.4g} {unit} on the {dac_path} path")
        req: Dict[str, Any] = {"cmd": "generate", "waveform": waveform, "freq_hz": _num(freq_hz)}
        if current:
            req["amplitude_ua"] = round(amp * 1e6)
            req["offset_ua"] = round(off * 1e6)
        else:
            req.update({"path": dac_path, "amplitude_v": amp, "offset_v": off})
        if duration is not None:
            ms = _seconds_to_ms(duration, "duration")
            if ms <= 0:
                raise ValueError("duration must be at least 1 ms; omit it to run until stopped")
            req["duration_ms"] = ms
        rate = _capture.rate_hz(sample_rate_hz)
        if rate is not None:
            req["sample_rate_hz"] = rate
        if on_capture:
            req["on_capture"] = True
        if not route:
            req["route"] = False
        d = _dict(self.command(req))
        if route:
            self._routed_dac_path = dac_path  # the pod routed it
            if dac_path != _CURRENT_OUT:
                self._dac_routed()
        return DacHandle(stop=self._stop_for(dac_path), dac_path=dac_path,
                         cotrig=bool(d.get("cotrig", False)), data=d)

    def dac_stop(self) -> None:
        """Stop any DAC output: generator, replay or control loop. Idempotent."""
        self.command({"cmd": "dac_stop"})

    # -- in-fabric DAC control loop -------------------------------------------

    def control_loop(self, *, curve: Union[Sequence[int], str, None] = None,
                     voc_code: Optional[int] = None, sharpness: float = 4.0,
                     points: int = _control_loop.CURVE_POINTS,
                     k: int = _control_loop.DEFAULT_K,
                     vmin: int = _control_loop.DEFAULT_VMIN,
                     vmax: int = _control_loop.DEFAULT_VMAX,
                     tick_div: int = _control_loop.DEFAULT_TICK_DIV,
                     source: Optional[LoopSource] = None,
                     input_code: int = 0,
                     step: int = 0,
                     input_map: Optional[_control_loop.LoopInputMap] = None,
                     switch_image: bool = True,
                     ) -> _control_loop.ControlLoopHandle:
        """Arm the in-fabric DAC control loop.

        Each tick the iCE40 takes an input, looks it up in the curve LUT (``out = curve[in]``),
        damps toward that target (``k``, Q15) and clamps to ``[vmin, vmax]``, then drives the DAC.
        Provide the curve as raw ``curve`` codes (0..65535), a base64url ``curve`` string, or
        ``voc_code`` (+ ``sharpness``) to synthesise the solar-panel I-V preset; omit all to run
        clamp-only. Needs the loop gateware image (:attr:`Capabilities.dac_control_loop`): with
        ``switch_image`` (the default) a pod on the deep-replay image is switched first (~3 s, see
        :meth:`fpga_image`), and ``switch_image=False`` raises :class:`BenchPodError` instead. The
        handle's ``switched_image`` records a switch.

        ``source`` picks the INPUT (needs :attr:`Capabilities.dac_loop_sources`; ``None`` = device
        default, the live ADC): ``"adc"`` closes the loop around the DUT, ``"fixed"`` holds
        ``input_code`` with the ADC out of the path, ``"sweep"`` advances the input by ``step``
        every tick. ``input_map`` (:class:`LoopInputMap`, gateware >= v30) lets the curve be
        authored in engineering units of the sense chain instead of raw ADC counts.

        Returns a :class:`ControlLoopHandle` (context manager; ``probe()`` for the live operating
        point, stops on exit)::

            curve = benchpod.build_panel_curve(52000, 6)
            with bp.control_loop(curve=curve, source="fixed", input_code=0) as loop:
                assert loop.probe().v == pytest.approx(benchpod.curve_output_at(curve, 0), abs=200)
                loop.set_input(benchpod.input_percent_to_code(50))
        """
        curve_b64 = None
        if isinstance(curve, str):
            curve_b64 = curve
        elif curve is not None:
            curve_b64 = _control_loop.encode_curve_b64url(curve)
        elif voc_code is not None:
            curve_b64 = _control_loop.encode_curve_b64url(
                _control_loop.build_panel_curve(voc_code, sharpness, points))

        # Same guards the firmware applies, before the round trip, so a bad clamp or a frozen
        # "sweep" is a ValueError here instead of a device error.
        k, vmin, vmax, tick_div = _control_loop.normalise_loop_params(k, vmin, vmax, tick_div)
        source = _control_loop.normalise_loop_source(source, step)
        req: Dict[str, Any] = {"cmd": "dac_control_loop", "k": int(k), "vmin": int(vmin),
                               "vmax": int(vmax), "tick_div": int(tick_div)}
        if curve_b64:
            req["curve"] = curve_b64
        if source is not None:
            # Only sent when asked for, so a pod without selectable sources still arms as the
            # ADC-driven closed loop instead of being refused.
            req["source"] = source
            req["input"] = int(input_code)
            req["step"] = int(step)
        if input_map is not None:
            req.update(input_map.to_request())
        # After every argument check, so a bad call never reprograms the FPGA.
        switched = self._ensure_image(FpgaImage.LOOP, needed_for="the control loop",
                                      switch=switch_image)
        data = _dict(self.command(req))
        if data.get("armed") is False:
            raise BenchPodError(f"control loop did not arm: {data!r}")
        return _control_loop.ControlLoopHandle(
            probe=self.loop_probe, stop=self.dac_stop, data=data, set_input=self.loop_input,
            switched_image=switched)

    def loop_input(self, input_code: Optional[int] = None, *,
                   source: Optional[LoopSource] = None, step: Optional[int] = None) -> LoopState:
        """Re-target a RUNNING loop's input without re-arming or re-uploading the curve.

        The open-loop stepping flow — hold a curve point, meter the output, move to the next.
        Omitted fields keep their device-side value. Needs :attr:`Capabilities.dac_loop_sources`.
        The returned ``output_code`` is the output at the instant of the write; poll
        :meth:`loop_probe` for the settled value.
        """
        source = _control_loop.normalise_loop_source(source, step if step is not None else 1)
        req: Dict[str, Any] = {"cmd": "dac_loop_input"}
        if input_code is not None:
            req["input"] = int(input_code)
        if source is not None:
            req["source"] = source
        if step is not None:
            req["step"] = int(step)
        return LoopState.from_reply(self.command(req))

    def loop_probe(self) -> _control_loop.IVPoint:
        """Poll the running control loop's live operating point (:class:`IVPoint`).

        ``i`` is the latest ADC reading and ``v`` the DAC code the loop drove this tick;
        :attr:`~IVPoint.loop_input` is the value the loop ACTUALLY indexed the curve with — the
        one to assert against, since in a fixed/sweep run the ADC is not in the path.
        """
        d = _dict(self.command({"cmd": "dac_loop_probe"}))
        raw_in = d.get("in")
        return _control_loop.IVPoint(
            i=int(d.get("i", 0)), v=int(d.get("code16", 0)),
            input_code=None if raw_in is None else int(raw_in),
            source=d.get("source"),
            tripped=None if d.get("tripped") is None else bool(d.get("tripped")))

    # -- gateware image -------------------------------------------------------

    def fpga_image(self, image: Union[FpgaImage, int]) -> FpgaImageInfo:
        """Switch the iCE40 to another gateware image stored on the pod.

        ``FpgaImage.LOOP`` (0) is the control-loop image, ``FpgaImage.DEEP_REPLAY`` (1) the deep
        DAC replay image, ``FpgaImage.EMULATION`` (2) the sensor emulation image (gateware v54+,
        W25Q slot ``gw2``; ``bp.sensors.start()`` loads it by itself, and the pod never switches
        back on its own; leaving it is refused while I2C, SPI or UART sensors run). The pod
        reconfigures the FPGA with the image, which stops anything running in the FPGA: a DAC output
        or control loop, a UART session, sensor emulation. From firmware 4.0.6 it loads the image
        straight from its slot in about 0.1 s and writes nothing to the config flash; asking for the
        image that already runs does nothing. After a restart or power cycle the pod runs the loop
        image (older firmware: 2 to 3 s, the image was written to the config flash and kept across a
        power cycle). Drops the cached :attr:`capabilities` so the next read reflects the new image.

        You rarely need to call this: :meth:`control_loop`, :meth:`replay` and
        :meth:`replay_waveform` switch automatically when they need the other image
        (``switch_image=True``).
        """
        img = int(image)
        if img not in tuple(FpgaImage):
            raise ValueError(f"image must be FpgaImage.LOOP (0), FpgaImage.DEEP_REPLAY (1) or FpgaImage.EMULATION (2), got {image!r}")
        data = self.command({"cmd": "fpga_image", "image": img})
        self._caps = None
        return FpgaImageInfo.from_reply(data)

    def _ensure_image(self, image: FpgaImage, *, needed_for: str,
                      switch: bool) -> Optional[FpgaImageInfo]:
        """Make sure the pod runs gateware ``image`` before ``needed_for``.

        Returns the :class:`FpgaImageInfo` of the switch it made, or ``None`` when none was needed:
        the pod is already on ``image``, or it carries a single image (nothing to switch to — the
        operation goes ahead and the firmware decides). With ``switch=False`` a needed switch
        raises :class:`BenchPodError`.
        """
        flag = _IMAGE_FLAG[image]
        caps = self.capabilities
        if image is FpgaImage.EMULATION:
            running = self._running_image()
            if running is None or running.image == FpgaImage.EMULATION:
                return None
        elif getattr(caps, flag):
            return None
        elif _maybe_on_emulation(caps):
            running = self._running_image()
            if running is None or running.image != FpgaImage.EMULATION:
                return None
            digital = self.sensors.running_digital()
            if digital:
                raise BenchPodError(
                    f"{needed_for} needs the {image.name} gateware image, but the pod runs the "
                    f"emulation image for the emulated sensors {', '.join(digital)}: stop them "
                    f"first (bp.sensors.stop()); then the SDK switches back by itself")
        elif not _images_switchable(caps):
            return None
        if not switch:
            raise BenchPodError(
                f"{needed_for} needs the {image.name} gateware image, but the pod is running the "
                f"other one (capabilities.{flag} is False): pass switch_image=True or call "
                f"fpga_image(FpgaImage.{image.name}) first")
        _log.warning("switching the FPGA to the %s gateware image for %s (~3 s); this resets the "
                     "FPGA, so any DAC output, UART session or sensor emulation stops",
                     image.name, needed_for)
        info = self.fpga_image(image)
        if image is FpgaImage.EMULATION:
            return info
        # Judge by the reply's feature bits: on a cloud connection the merged capabilities can lag.
        if not info.features & _IMAGE_FEATURE[image]:
            raise BenchPodError(
                f"switched the FPGA to the {image.name} image for {needed_for}, but the image that "
                f"booted does not carry it (features=0x{info.features:02x})")
        self._wait_for_server_caps(flag)
        return info

    # -- bus masters (not in the emulation image from gateware v65) ------------

    def has_bus_masters(self) -> Optional[bool]:
        """Whether the running gateware image has the bus masters (SWD, JTAG, the SPI master and
        the I2C master): always on gateware before v65; from v65 on the loop and deep-replay
        images have them and the emulation image does not. ``None`` when the pod does not say."""
        caps = self.capabilities
        if 0 < caps.gateware < BUS_MASTERS_SPLIT_GATEWARE:
            return True
        if caps.gateware >= BUS_MASTERS_SPLIT_GATEWARE and caps.fpga_image in ("loop", "deep"):
            return True
        running = self._running_image()
        if running is None or running.version <= 0:
            return None
        if running.version < BUS_MASTERS_SPLIT_GATEWARE:
            return True
        return bool(running.features & _FEATURE_BUS_MASTERS)

    def _ensure_bus_masters(self, feature: str) -> Optional[FpgaImageInfo]:
        """Before ``feature`` (``"SWD"``, ``"JTAG"``, ``"the SPI master"``, ``"the I2C master"``)
        runs: make sure the running gateware image has the bus masters.

        Nothing to do on gateware before v65 or on the loop and deep-replay images. On the
        emulation image of gateware v65+ with digital sensors running, raises the
        ``BP0059`` :class:`~embeddedci.benchpod.errors.UnsupportedFeatureError` (stop the sensors
        first, or do this before starting them); with none running, switches the pod to the loop
        image (~3 s, logged) and returns that switch's :class:`FpgaImageInfo`.
        """
        if self.has_bus_masters() is not False:
            return None
        digital = self.sensors.running_digital()
        if digital:
            from .errors import firmware_error

            exc = firmware_error("", cmd=feature, code="BP0059",
                                 values={"feature": feature, "image": "loop"})
            exc.args = (f"{exc}; the emulated sensors running now are {', '.join(digital)} "
                        f"(or do this before starting them)",)
            raise exc
        info = self._ensure_image(FpgaImage.LOOP, needed_for=feature, switch=True)
        if info is None:   # a single-image pod: nothing to switch to
            return None
        if not info.features & _FEATURE_BUS_MASTERS and info.version >= BUS_MASTERS_SPLIT_GATEWARE:
            raise BenchPodError(f"switched the FPGA to the loop image for {feature}, but the image "
                                f"that booted has no bus masters (features=0x{info.features:02x})")
        return info

    def _running_image(self) -> Optional[FpgaImageInfo]:
        """The image the iCE40 runs (``{"cmd":"fpga_image"}``), None when the pod does not say."""
        try:
            data = self.command({"cmd": "fpga_image"})
        except BenchPodError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("image"), int):
            return None
        return FpgaImageInfo.from_reply(data)

    def _wait_for_server_caps(self, flag: str) -> None:
        """After an image switch, wait until the server's cached capabilities show ``flag`` (the pod
        re-announces them), so server-side operations and merged capabilities see the new image."""
        api = self._try_server_api()
        if api is None or not self._device_name:
            return
        deadline = time.monotonic() + _SERVER_CAPS_WAIT
        while True:
            try:
                params = api.device_parameters(self._device_name)
                if str(params.get(f"cap.{flag}", "")).lower() == "true":
                    break
            except BenchPodError:
                pass
            if time.monotonic() >= deadline:
                _log.warning("the server still reports the previous gateware image %.0f s after the "
                             "switch", _SERVER_CAPS_WAIT)
                break
            time.sleep(0.5)
        self._caps = None

    # -- DAC arbitrary-waveform replay ----------------------------------------

    def _replay_bits(self) -> int:
        b = self.capabilities.dac_replay_bits
        if b and b > 0:
            return b
        # v2 (16-bit ADC) pods replay 16-bit; fall back to 8-bit otherwise.
        return 16 if self.capabilities.adc_bits >= 16 else 8

    def _arm_replay(self, code_bytes: bytes, *, bits: int, dac_path: str,
                    sample_rate_hz: Optional[float], deep: Optional[bool],
                    on_capture: bool = False, route: bool = True,
                    switch_image: bool = True) -> ReplayHandle:
        """Route the DAC path, upload the codes and arm a looping replay over the transport."""
        fn = getattr(self._transport, "load_replay", None)
        if fn is None:
            raise BenchPodError("replay needs a TCP or cloud connection (it streams the waveform "
                                "to the pod)")
        n_samples = len(code_bytes) // (2 if bits > 8 else 1)
        if n_samples == 0:
            raise ValueError("replay has no samples")
        if deep is None:
            deep = n_samples > 2048  # exceeds the shallow DAC BRAM depth -> stream from PSRAM
        caps = self.capabilities
        switched: Optional[FpgaImageInfo] = None
        if deep and caps.dac_replay and not caps.dac_deep_replay:
            # The loop image accepts a PSRAM replay and then outputs nothing (measured on a pod), so
            # switch to the deep-replay image — or refuse — rather than arm a silent DAC.
            if not _images_switchable(caps) and not _maybe_on_emulation(caps):
                raise BenchPodError(
                    f"a {n_samples}-sample replay streams from PSRAM, which this pod's gateware "
                    "cannot play (capabilities.dac_deep_replay is False): replay at most 2048 samples")
            switched = self._ensure_image(
                FpgaImage.DEEP_REPLAY, switch=switch_image,
                needed_for=f"a {n_samples}-sample replay (it streams from PSRAM)")
        if dac_path and route:
            self._route_output(dac_path)
        replay_req: Dict[str, Any] = {"cmd": "replay", "samples": n_samples}
        rate = _capture.rate_hz(sample_rate_hz)
        if rate is not None:
            replay_req["sample_rate_hz"] = rate
        if on_capture:
            replay_req["on_capture"] = True
        d = _dict(fn(data=code_bytes, replay=replay_req, psram=deep))
        return ReplayHandle(stop=self._stop_for(dac_path), samples=n_samples,
                            sample_rate_hz=float(sample_rate_hz or 0.0), dac_path=dac_path,
                            deep=deep, data=d, cotrig=bool(d.get("cotrig", False)),
                            switched_image=switched)

    def replay(self, source: Union[Capture, Sequence[float], Sequence[int]], *,
               dac_path: Optional[DacPath] = None, mapping: ReplayMapping = "faithful",
               sample_rate_hz: Optional[float] = None, deep: Optional[bool] = None,
               fault: Union[Fault, Dict[str, Any], None] = None,
               are_codes: bool = False, on_capture: bool = False,
               route: bool = True, switch_image: bool = True) -> ReplayHandle:
        """Replay a waveform on the DAC, streaming it to the device over the transport.

        ``route=False`` keeps the current analog switching (see :meth:`generate`). ``dac_path``
        left out is ``3v3``, or with ``route=False`` the output last routed (see :meth:`generate`).

        ``source`` is a :class:`Capture` (its volts are replayed), a sequence of volts, or — with
        ``are_codes=True`` — raw DAC codes. Volts map to codes for ``dac_path`` (``faithful``
        reproduces the voltage, clipping outside range; ``fit`` auto-scales). ``fault`` splices a
        :class:`Fault` in. More than 2048 samples stream from PSRAM, which needs the deep-replay
        gateware image: with ``switch_image`` (the default) the pod is switched to it first (~3 s,
        recorded as the handle's ``switched_image``); ``switch_image=False`` raises instead.
        The replay LOOPS until the returned :class:`ReplayHandle` is stopped,
        so it can run concurrently with a capture (gateware v18)::

            with bp.replay(cap, dac_path="3v3"):
                la = bp.capture_la(8192, sample_rate_hz=1e6)

        ``on_capture=True`` arms the DAC to start on the NEXT capture's hardware t0 (gateware
        >= v27, :attr:`Capabilities.dac_cotrig`).

        ``dac_path="current_out"`` replays on the 4-20 mA output (J9): a sequence is then AMPS,
        and a :class:`Capture` of ``current_in`` replays the current it measured (its
        ``currents``), so a loop captured on J8 can be played back on J9. A capture of a voltage
        source has no current to reproduce: it needs ``mapping="fit"``, which stretches its shape
        over the output's range. Stopping the handle returns the loop to 4 mA. See
        :meth:`current_out` for the wiring.
        """
        if dac_path is None:
            dac_path = self._default_dac_path(route)  # type: ignore[assignment]
        check_choice(dac_path, DAC_PATHS, "dac_path")
        self._refuse_replay_cal1(dac_path)
        check_choice(mapping, REPLAY_MAPPINGS, "mapping")
        bits = self._replay_bits()
        vmin, vmax = self._output_range(dac_path)
        if isinstance(source, Capture):
            values = source.volts
            if dac_path == _CURRENT_OUT:
                if source.currents:
                    values = source.currents
                elif mapping != "fit":
                    raise ValueError("this capture is in volts, not a current: replay it on the "
                                     "current_out path with mapping='fit', or capture current_in")
            codes = _dsp.volts_to_codes(values, mapping, vmax, bits=bits, path_min_v=vmin)
            if sample_rate_hz is None and source.sample_rate_hz > 0:
                sample_rate_hz = source.sample_rate_hz
        elif are_codes:
            codes = [int(x) for x in source]
        else:
            codes = _dsp.volts_to_codes([float(x) for x in source], mapping, vmax, bits=bits,
                                        path_min_v=vmin)
        f = normalize_fault(fault)
        if f:
            codes = _dsp.apply_fault(codes, f, bits=bits)
        return self._arm_replay(_dsp.codes_to_bytes(codes, bits), bits=bits, dac_path=dac_path,
                                sample_rate_hz=sample_rate_hz, deep=deep, on_capture=on_capture,
                                route=route, switch_image=switch_image)

    def replay_waveform(self, waveform: Union[str, "Waveform"], *,
                        dac_path: Optional[DacPath] = None, mapping: ReplayMapping = "faithful",
                        sample_rate_hz: Optional[float] = None,
                        window_start: int = 0, window_len: int = 0, target_samples: int = 0,
                        fault: Union[Fault, Dict[str, Any], None] = None,
                        deep: Optional[bool] = None, server_side: Optional[bool] = None,
                        on_capture: bool = False, switch_image: bool = True) -> ReplayHandle:
        """Load a **cloud-stored** waveform from the library and replay it on the DAC.

        ``waveform`` is a waveform id or a :class:`~embeddedci.benchpod.waveforms.Waveform`.
        ``dac_path`` defaults to the path stored with the waveform (else ``3v3``).
        ``window_start``/``window_len`` select a slice of a recording; ``target_samples``
        downsamples a shallow replay. By default this uses the server's replay DSP when the device
        is cloud-connected; otherwise it fetches the recording and replays it over the device
        connection with the SAME DSP applied client-side. Needs server access (the cloud session
        or an ``api_key``).

        With ``switch_image`` (the default) a replay that needs the deep-replay gateware image
        switches the pod to it first: client-side, one deeper than 2048 samples; server-side, a
        recording longer than the shallow replay depth when no ``target_samples`` asks for a
        downsampled one (``switch_image=False`` then leaves the server to downsample).

        ``dac_path="current_out"`` replays on the 4-20 mA output (J9). A recording of the 4-20 mA
        input (``Waveform.unit == "mA"``) replays as the same current; a voltage recording there,
        or a current recording on a voltage output, needs ``mapping="fit"``.
        """
        check_choice(mapping, REPLAY_MAPPINGS, "mapping")
        wid = waveform.id if hasattr(waveform, "id") else waveform
        if not wid:
            raise ValueError("replay_waveform needs a waveform id")
        lib = self.waveforms
        wf = lib.get(wid)  # type: ignore[arg-type]
        if dac_path is None:
            dac_path = wf.dac_path if wf.dac_path in DAC_PATHS else DEFAULT_DAC_PATH  # type: ignore[assignment]
        check_choice(dac_path, DAC_PATHS, "dac_path")  # type: ignore[arg-type]
        self._refuse_replay_cal1(dac_path)
        path_unit = "mA" if dac_path == _CURRENT_OUT else "V"
        wf_unit = getattr(wf, "unit", "V") or "V"
        if getattr(wf, "kind", "") == "recording" and wf_unit != path_unit and mapping != "fit":
            raise ValueError(f"this recording is in {wf_unit} and the {dac_path} output is in "
                             f"{path_unit}: use mapping='fit' to replay its shape there")
        if dac_path == _CURRENT_OUT:
            self._output_range(dac_path)  # needs the capability; refuse before anything moves

        use_server = server_side
        if use_server is None:
            use_server = bool(self._device_name)  # the server can only reach a cloud-registered device
        if use_server:
            api = self._require_server_api()
            switched: Optional[FpgaImageInfo] = None
            caps = self.capabilities
            if switch_image and caps.dac_replay and not caps.dac_deep_replay:
                # On the loop image the server downsamples to the shallow depth; switch instead (as
                # the web UI does) unless the caller asked for a downsampled replay.
                want = int(window_len) or max(0, int(wf.sample_count or 0) - int(window_start))
                shallow = caps.dac_replay_max_samples or _dsp.REPLAY_MAX_SAMPLES
                if want > shallow and not 0 < target_samples <= shallow:
                    switched = self._ensure_image(
                        FpgaImage.DEEP_REPLAY, switch=True,
                        needed_for=f"a full-length {want}-sample replay")
            payload: Dict[str, Any] = {"device_id": api.resolve_device_id(self._device_name),
                                       "waveform_id": wid, "dac_path": dac_path, "mapping": mapping}
            mhz = _capture.rate_mhz(sample_rate_hz)
            if mhz is not None:
                payload["sample_rate_mhz"] = mhz
            if window_start:
                payload["window_start"] = int(window_start)
            if window_len:
                payload["window_len"] = int(window_len)
            if target_samples:
                payload["target_samples"] = int(target_samples)
            if on_capture:
                payload["on_capture"] = True
            f = normalize_fault(fault)
            if f:
                payload["fault"] = f
            if dac_path == _CURRENT_OUT:
                self._route_output(dac_path)  # the DAC voltage outputs must not follow the current
            data = api.replay_start(payload)
            # The server replay is async: the armed/playing outcome arrives as a later dac.event
            # frame, so infer cotrig from the request + the device capability.
            return ReplayHandle(stop=self._stop_for(dac_path),  # type: ignore[arg-type]
                                samples=int(data.get("samples", 0) or 0),
                                sample_rate_hz=float(sample_rate_hz or 0.0), dac_path=dac_path,
                                deep=bool(self.capabilities.dac_deep_replay), data=data,
                                cotrig=bool(on_capture and self.capabilities.dac_cotrig),
                                switched_image=switched)

        # Client-side: fetch + DSP + stream over the transport.
        bits = self._replay_bits()
        # The library stores currents in mA, so the 4-20 mA output's range is taken in mA here.
        path_range = None
        if dac_path == _CURRENT_OUT:
            lo_a, hi_a = self._output_range(dac_path)  # type: ignore[arg-type]
            path_range = (lo_a * 1000.0, hi_a * 1000.0)
        if wf.is_recording:
            raw = lib.download_recording(wid)  # type: ignore[arg-type]
            rc = _dsp.recording_to_replay_codes(
                raw, path_range=path_range,
                src_full_scale_v=wf.full_scale_v or _dsp.dac_path_fullscale_v(dac_path),
                dac_path=dac_path, mapping=mapping, window_start=window_start,
                window_len=window_len, target_samples=target_samples, bits=bits,
                deep=bool(deep) if deep is not None else (wf.sample_count > 2048),
                max_samples=self.capabilities.dac_replay_max_samples or _dsp.REPLAY_MAX_SAMPLES,
                fault=normalize_fault(fault))
            code_bytes = rc.to_bytes()
        elif wf.segments:
            volts = _dsp.segments_to_volts(wf.segments, wf.sample_rate_hz or 1.0)
            seg_min, seg_max = path_range or _dsp.dac_path_range_v(dac_path)  # type: ignore[arg-type]
            codes = _dsp.volts_to_codes(volts, mapping, seg_max, bits=bits, path_min_v=seg_min)
            f = normalize_fault(fault)
            if f:
                codes = _dsp.apply_fault(codes, f, bits=bits)
            code_bytes = _dsp.codes_to_bytes(codes, bits)
        else:  # dac_waveform: inline codes, already at the waveform's bit width
            code_bytes = base64.b64decode(lib.samples_b64(wid))  # type: ignore[arg-type]
        return self._arm_replay(code_bytes, bits=bits, dac_path=dac_path,  # type: ignore[arg-type]
                                sample_rate_hz=sample_rate_hz, deep=deep, on_capture=on_capture,
                                switch_image=switch_image)

    def save_capture_as_recording(self, capture: Capture, name: str, *,
                                  full_scale_v: Optional[float] = None) -> "Waveform":
        """Save an ADC :class:`Capture` to the cloud library as a replayable ``recording``.

        A capture of ``current_in`` (the 4-20 mA input) is stored as a current, in mA, so it
        replays as the same current on the ``current_out`` path; ``full_scale_v`` is then mA too.
        """
        unit = "mA" if capture.currents else "V"
        volts = [a * 1000.0 for a in capture.currents] if capture.currents else capture.volts
        fs = full_scale_v
        if fs is None:
            peak = max((abs(v) for v in volts), default=1.0)
            fs = peak if peak > 0 else 1.0
        blob = _dsp.encode_recording_volts(volts, fs)
        return self.waveforms.save_recording(name, blob, sample_rate_hz=capture.sample_rate_hz or 0.0,
                                             full_scale_v=fs, unit=unit)

    # -- cloud waveform library + server API ----------------------------------

    def _try_server_api(self) -> Optional["ServerApi"]:
        """Build a :class:`ServerApi` if possible, else None. Credentials, in order: the API key; the
        logged-in user's token (the server's device and waveform routes take a user, not a cloud
        session token minted from one); the cloud transport's session token (API key or OIDC)."""
        if self._server_api is not None:
            return self._server_api
        token_provider = self._cloud_user_token or getattr(self._transport, "_session_token", None)
        if not self._api_key and token_provider is None:
            return None
        from .server_api import ServerApi

        self._server_api = ServerApi(
            api_base=self._api_base, api_key=self._api_key,
            token_provider=token_provider if not self._api_key else None,
            lease_id=getattr(self._transport, "lease_id", None),
        )
        return self._server_api

    def _require_server_api(self) -> "ServerApi":
        api = self._try_server_api()
        if api is None:
            raise BenchPodError(
                "this operation needs embeddedci server access. Connect over the cloud "
                "('embeddedci:<device>', which reuses its session token) or pass api_key='eci_…' "
                "(or set BENCHPOD_API_KEY) on a LAN/serial connection."
            )
        return api

    @property
    def server_api(self) -> "ServerApi":
        """The :class:`~embeddedci.benchpod.server_api.ServerApi` (needs an API key or cloud session)."""
        return self._require_server_api()

    @property
    def waveforms(self) -> "WaveformLibrary":
        """The cloud :class:`~embeddedci.benchpod.waveforms.WaveformLibrary` (needs server access)."""
        if self._waveforms is None:
            from .waveforms import WaveformLibrary

            self._waveforms = WaveformLibrary(self._require_server_api())
        return self._waveforms

    # -- LA pin modes + GPIO --------------------------------------------------
    # The LA pins on the iCE40 (not STM32 GPIOs). Each has one function at a time; see gpio.py.

    def la_pins(self, la: Union[Pin, int, str, Sequence[Union[Pin, int, str]], None] = None
                ) -> List[LaPinState]:
        """Every LA channel's function (``none``, ``gpio``, ``uart_rx``, …), GPIO mode and level, and
        bias resistor. A channel that is in use refuses other functions.

        ``la``: only these channels (a channel, a wiring name, or a list), in pin order. A listing
        too large for one cloud reply (a long sensor instance name on every pin) is read in parts by
        itself: in pages (``from``/``next``) on firmware with ``la_pins_paged``, else in two
        halves."""
        self._require_capability("la_pins", "LA pin modes and GPIO")
        if la == "all":
            la = None
        if la is not None:
            want = sorted(set(self._resolve_las(la, why="la_pins lists LA channels")))
            return [p for p in self._la_pins_query(want) if p.la in want]
        try:
            return self._la_pins_query(None)
        except FirmwareError as e:
            if e.code not in ("BP0053", "BP0054"):   # reply_too_large, cloud_reply_too_large
                raise
            first = self._la_pins_query(list(range(1, 8)))
            return first + [p for p in self._la_pins_query(list(range(8, 15))) if p.la >= 8]

    def _la_pins_query(self, las: Optional[List[int]]) -> List[LaPinState]:
        """One la_pins listing. On firmware with ``la_pins_paged`` a reply that could not carry
        every pin says ``next``: ask again ``from`` there (with the same ``la``) and join the pins;
        ``pull_dirs`` comes from the last reply. The loop stops after 16 replies, or when ``next``
        does not move forward."""
        req: Dict[str, Any] = {"cmd": "la_pins"}
        if las is not None:
            req["la"] = las
        paged = self.supports("la_pins_paged")
        entries: List[Any] = []
        data: Dict[str, Any] = {}
        last = 1   # the pin the last reply started from
        for _ in range(_LA_PINS_MAX_PAGES):
            with _classified_errors():
                data = _dict(self.command(req))
            entries.extend(data.get("pins") or [])
            nxt = data.get("next")
            if not paged or not isinstance(nxt, int) or isinstance(nxt, bool):
                break
            if nxt <= last or nxt > 14:
                _log.warning("la_pins: next %s does not follow from %s; the listing stops there",
                             nxt, last)
                break
            last = nxt
            req = dict(req, **{"from": nxt})
        else:
            _log.warning("la_pins: still not done after %d replies; the listing stops there",
                         _LA_PINS_MAX_PAGES)
        # Digital v4 says the directions once for every channel ("pull_dirs").
        dirs = data.get("pull_dirs")
        return [LaPinState.from_reply(e, pull_dirs=dirs if isinstance(dirs, list) else None)
                for e in entries]

    def _entry_pull_dirs(self, entries: Any) -> Optional[Tuple[str, ...]]:
        """The pull directions for listing replies that do not say them (la_dir, gpio): on digital
        v4 every channel pulls both ways, said only once in the la_pins reply."""
        if not isinstance(entries, list):
            return None
        bare = any(isinstance(e, dict) and isinstance(e.get("pull"), dict) and "dirs" not in e["pull"]
                   for e in entries)
        return ("up", "down") if bare and self.supports("la_pull_strength") else None

    def la_dir(self, la: Union[Pin, int, str, Sequence[Union[Pin, int, str]], None] = None,
               dir: Optional[str] = None) -> List[LaPinState]:
        """Tell the pod which way each pin's DUT signal goes, and return the changed pins' state.

        ``la`` is a channel, a wiring name, a list of them, or ``"all"``; ``dir`` is ``dut_input``,
        ``dut_output``, ``bidir`` or ``unknown`` (the default after a reboot). On a ``dut_output``
        pin the pod refuses every push-pull driver (a GPIO ``output``, UART TX, SPI SCK / MOSI /
        CS, SWD, ...) with :class:`~embeddedci.benchpod.errors.DirectionConflictError`; open
        drain and inputs still work. A pin that is driven can't be marked ``dut_output`` until it is
        released. With no arguments, it lists every pin. :meth:`apply_pin_directions` sends the
        wiring profile's directions.
        """
        req: Dict[str, Any] = {"cmd": "la_dir"}
        if la is not None or dir is not None:
            if la is None or dir is None:
                raise ValueError("give both la and dir, or neither to list every pin")
            check_choice(dir, LA_DIRS, "dir")
            req["la"] = "all" if la == "all" else _one_or_many(self._resolve_las(
                la, why="la_dir marks LA channels; an EXT pin's direction is its GPIO mode"))
            req["dir"] = dir
        with _classified_errors():
            data = self.command(req)
        entries = data.get("pins") if isinstance(data, dict) else data
        dirs = self._entry_pull_dirs(entries)
        return ([LaPinState.from_reply(e, pull_dirs=dirs) for e in entries or []]
                if isinstance(entries, list) else [])

    def pin_directions(self, wiring: Optional[Wiring] = None) -> Dict[int, str]:
        """The DUT direction of every pin the wiring profile names, as :meth:`la_dir` takes it:
        ``{5: "dut_output", 4: "dut_input", 1: "bidir", ...}`` (:meth:`Wiring.pin_dut_dirs
        <embeddedci.benchpod.wiring.Wiring.pin_dut_dirs>`).

        Each role and signal has a default, and the profile's ``dut_dirs`` overrides it. Roles:
        ``uart_rx`` and ``spi_miso`` are DUT outputs; ``uart_tx``, ``spi_sclk``, ``spi_mosi``,
        ``spi_cs`` and ``swd_swclk`` DUT inputs; the I2C pins and ``swd_swdio`` ``bidir``.
        Signals: ``input`` (the pod watches it) is a DUT output, ``output`` a DUT input,
        ``open_drain`` and ``bidir`` are ``bidir``."""
        w = wiring if wiring is not None else self.wiring
        return w.pin_dut_dirs()

    def apply_pin_directions(self, wiring: Optional[Wiring] = None) -> List[LaPinState]:
        """Send :meth:`pin_directions` of the wiring profile to the pod (:meth:`la_dir`), one
        command per direction, and mark the pins the profile doesn't name ``unknown``. Returns the
        pins' new state.

        The SDK does this on its own when it connects to a pod with firmware 3.10.4+ and when
        :attr:`wiring` changes (:attr:`pin_directions_status` says how it went); the pod forgets
        the directions on a reboot. Raises
        :class:`~embeddedci.benchpod.errors.DirectionConflictError` when a pin the profile calls a
        DUT output is driven right now; the other directions are sent first."""
        dirs = self.pin_directions(wiring)
        by_dir: Dict[str, List[int]] = {}
        for la in range(1, 15):
            by_dir.setdefault(dirs.get(la, "unknown"), []).append(la)
        out: List[LaPinState] = []
        # "unknown" first, so a pin moving to another role never refuses on its old mark;
        # dut_output last, so a driven pin refusing it leaves every other direction in place.
        for d in ("unknown", "dut_input", "bidir", "dut_output"):
            if by_dir.get(d):
                out += self.la_dir(by_dir[d], d)
        return out

    def _auto_pin_directions(self) -> None:
        """Send the wiring's pin directions on connect and on a new profile, when the firmware
        has ``la_dir``. Raises only :class:`ClientTooOldError` and :class:`FirmwareTooOldError` (the
        pod's client API level is not this SDK's); anything else goes to
        :attr:`pin_directions_status`."""
        if getattr(self, "_transport", None) is None:
            return
        try:
            if type(self._transport).__name__ == "SerialTransport":
                self.pin_directions_status = "not sent: the USB console has no la_dir"
                # connect's client API check: the console announces no level, its version decides
                self._check_client_api(Capabilities.from_status(_dict(self._caps_status())))
                return
            # The cached capabilities when there are some; else one status read that is not cached
            # (the first real use of capabilities reads and caches them as before). It is also
            # connect's client API check: the one error raised from here.
            if self._caps is not None:
                caps = self.capabilities
            else:
                caps = Capabilities.from_status(_dict(self._caps_status()))
                self._check_client_api(caps)
            if not caps.la_dir:
                fw = caps.firmware_version or "unknown"
                self.pin_directions_status = f"not sent: this pod's firmware ({fw}) has no la_dir"
                return
            self.apply_pin_directions()
            self.pin_directions_status = "sent"
        except ClientTooOldError:
            self.pin_directions_status = "not sent: this SDK is too old for the pod's firmware"
            raise
        except FirmwareTooOldError:
            self.pin_directions_status = "not sent: the pod's firmware is too old for this SDK"
            raise
        except Exception as exc:  # a convenience on connect: report it, never fail the caller
            self.pin_directions_status = f"not sent: {exc}"
            _log.warning("pin directions from the wiring were not sent to the pod: %s", exc)

    def gpio(self, la: Union[Pin, int, str], mode: GpioMode = "output", *,
             level: Optional[int] = None) -> GpioPin:
        """Use an LA channel as GPIO and return its :class:`~embeddedci.benchpod.gpio.GpioPin`.

        ``la`` is 1-14, a ``Pin`` or a wiring-profile name; with an expander attached
        (:meth:`expansion`) also ``"EXT1"``..``"EXTn"`` or an EXT pin's name, which returns an
        :class:`~embeddedci.benchpod.gpio.ExtPin`. ``mode``: ``output`` (push-pull, starts at
        ``level``, default 0), ``open_drain`` (0 pulls low, 1 releases; starts released) or ``input``.
        Raises :class:`~embeddedci.benchpod.errors.PinConflictError` when another function owns the
        channel, and :class:`~embeddedci.benchpod.errors.PullConflictError` when its engaged bias
        resistor can't work with ``mode`` (an open-drain output over LA7/LA8's pull-down). The channel
        stays GPIO — across disconnects — until :meth:`release_gpio`.

        Use :meth:`gpio_pins` for several channels: it claims them as a group, so a conflict on one
        leaves the others untouched.
        """
        return self.gpio_pins([la], mode, level=level)[0]

    def gpio_pins(self, las: Sequence[Union[Pin, int, str]], mode: GpioMode = "output", *,
                  level: Optional[int] = None) -> List[GpioPin]:
        """Claim several LA channels as GPIO in **one** command, and return a
        :class:`~embeddedci.benchpod.gpio.GpioPin` each, in the order given.

        Same arguments as :meth:`gpio`, which is the one-channel form. The pod validates the whole
        group before it claims any of it, so a
        :class:`~embeddedci.benchpod.errors.PinConflictError` or
        :class:`~embeddedci.benchpod.errors.PullConflictError` on any channel leaves every channel
        as it was — unlike calling :meth:`gpio` in a loop, which would leave the earlier ones
        claimed::

            reset, boot0 = bp.gpio_pins(["RESET", "BOOT0"], "open_drain")

        EXT pins in the list are set up after the LA channels, in one bus transaction per chip.
        """
        refs = self._pin_refs(las)
        self._gpio_configure_refs(refs, mode, level)
        return [self._gpio_pin(r) for r in refs]

    def set_gpio(self, la: Union[Pin, int, str, Sequence[Union[Pin, int, str]]],
                 level: Union[int, Sequence[int]]) -> None:
        """Set the level of a GPIO output or open-drain channel — or of several (a list) at once.

        ``level`` is one level for all of them, or a list with one level per channel. LA channels
        take one command per distinct level; EXT pins one bus transaction per expander chip."""
        refs = self._pin_refs(la)
        if isinstance(level, (list, tuple)):
            if len(level) != len(refs):
                raise ValueError(f"give one level per channel: {len(refs)} channels, {len(level)} levels")
            levels = [_check_level(v) for v in level]
        else:
            levels = [_check_level(level)] * len(refs)
        by_level: Dict[int, List[int]] = {}
        ext: List[Tuple[int, int]] = []
        for (kind, n, _), lvl in zip(refs, levels):
            if kind == "ext":
                ext.append((n, lvl))
            else:
                by_level.setdefault(lvl, []).append(n)
        if by_level:
            self._require_capability("la_pins", "GPIO")
            for lvl, las in by_level.items():
                with _classified_errors():
                    self.command({"cmd": "gpio", "la": _one_or_many(las), "level": lvl})
        if ext:
            self._require_expansion().set_levels([n for n, _ in ext], [v for _, v in ext])

    def read_gpio(self, la: Union[Pin, int, str]) -> int:
        """The live level (0/1) of an LA channel, whatever its function, or of an EXT pin."""
        kind, n, _ = self._pin_refs(la)[0]
        if kind == "ext":
            return self._require_expansion().read([n])[0]
        return self.pin_levels()[n]

    def pin_levels(self) -> Dict[int, int]:
        """The live level of every LA channel: ``{1: 0, 2: 1, …, 14: 0}``.

        Read straight from the pins (gateware >= v35); older gateware answers with the last sample of
        a short logic capture instead. EXT pins are in :meth:`expansion`'s status.
        """
        if self.capabilities.gpio_read:
            with _classified_errors():
                mask = int(_dict(self.command({"cmd": "gpio"})).get("levels") or 0)
        else:
            cap = self.capture_la(64, sample_rate_hz=1_000_000)
            mask = cap.words[-1] if cap.words else 0
        return {ch: (mask >> (ch - 1)) & 1 for ch in range(1, 15)}

    def wait_for_level(self, la: Union[Pin, int, str], level: int, *, timeout: float,
                       poll: float = 0.005) -> bool:
        """Wait until an LA channel (or an EXT pin) reads ``level``; ``False`` if ``timeout``
        seconds pass first.

        Polled from the host, so it resolves a few milliseconds plus the round trip — for precise
        timing capture with a :class:`Trigger` and use the capture's timing helpers.
        """
        _check_level(level)
        if timeout < 0 or poll <= 0:
            raise ValueError("timeout must be >= 0 and poll > 0 seconds")
        kind, n, _ = self._pin_refs(la)[0]
        if kind == "ext":
            exp = self._require_expansion()
            read: Callable[[], int] = lambda: exp.read([n])[0]
        else:
            read = lambda: self.pin_levels()[n]
        deadline = time.monotonic() + timeout
        while True:
            if read() == level:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)

    def release_gpio(self, *las: Union[Pin, int, str]) -> None:
        """Stop using channels as GPIO — they go back to high-Z ("LA mode"), EXT pins to input.
        Without arguments every GPIO channel and every EXT pin is released; channels owned by other
        functions are left alone."""
        if not las:
            self._require_capability("la_pins", "GPIO")
            with _classified_errors():
                self.command({"cmd": "gpio", "la": "all", "mode": "off"})
            exp = self._expansion
            if exp is not None:
                used = [n for n, m in exp.modes.items() if m != "none"]
                if used:
                    exp.configure(used, "none", None)
            return
        refs = self._pin_refs(list(las))
        la_list = [n for kind, n, _ in refs if kind == "la"]
        ext = [n for kind, n, _ in refs if kind == "ext"]
        if la_list:
            self._require_capability("la_pins", "GPIO")
            with _classified_errors():
                self.command({"cmd": "gpio", "la": _one_or_many(la_list), "mode": "off"})
        if ext:
            self._require_expansion().configure(ext, "none", None)

    def configure_gpio(self, las: Sequence[Union[Pin, int, str]], mode: GpioMode = "output", *,
                       level: Optional[int] = None) -> List[Any]:
        """Claim LA channels as GPIO in one command and return the pod's
        :class:`~embeddedci.benchpod.gpio.LaPinState` for each — the low-level form of
        :meth:`gpio_pins`, for when you want what the pod reports rather than objects to drive.
        An EXT pin's entry is an :class:`~embeddedci.benchpod.expansion.ExtPinState`."""
        return self._gpio_configure_refs(self._pin_refs(las), mode, level)

    def _gpio_configure_refs(self, refs: List[Tuple[str, int, Optional[Signal]]], mode: str,
                             level: Optional[int]) -> List[Any]:
        ext = [n for kind, n, _ in refs if kind == "ext"]
        la_list = [n for kind, n, _ in refs if kind == "la"]
        exp = self._require_expansion() if ext else None
        if exp is not None:  # refuse a mode the chip can't do before anything is claimed
            check_choice(mode, GPIO_MODES, "mode")
            if level is not None:
                if mode == "input":
                    raise ValueError("level only applies to an output or open_drain pin")
                _check_level(level)
            for n in ext:
                exp._check_mode(n, mode)
        by_la: Dict[int, LaPinState] = {}
        if la_list:
            by_la = {p.la: p for p in self._gpio_configure(la_list, mode, level)}
        if exp is not None:
            exp.configure(ext, mode, level)
        return [exp.pin_state(n) if kind == "ext" and exp is not None
                else by_la.get(n, LaPinState(la=n, function="gpio", gpio=mode, level=level))
                for kind, n, _ in refs]

    def _gpio_configure(self, las: List[int], mode: str, level: Optional[int]) -> List[LaPinState]:
        check_choice(mode, GPIO_MODES, "mode")
        req: Dict[str, Any] = {"cmd": "gpio", "la": _one_or_many(las), "mode": mode}
        if level is not None:
            if mode == "input":
                raise ValueError("level only applies to an output or open_drain pin")
            req["level"] = _check_level(level)
        self._require_capability("la_pins", "GPIO")
        with _classified_errors():
            data = _dict(self.command(req))
        entries = data.get("pins") or []
        dirs = self._entry_pull_dirs(entries)
        pins = [LaPinState.from_reply(e, pull_dirs=dirs) for e in entries]
        return pins or [LaPinState(la=la, function="gpio", gpio=mode, level=level) for la in las]

    def _resolve_las(self, la: Any, why: str = "") -> List[int]:
        items = list(la) if isinstance(la, (list, tuple)) else [la]
        if not items:
            raise ValueError("give at least one LA channel")
        return [self._resolve_la(x, why=why)[0] for x in items]

    # -- GPIO expanders -------------------------------------------------------

    def expansion(
        self,
        chip: Optional[str] = "",
        *,
        addr: Union[int, Sequence[int], None] = None,
        sda: Union[Pin, int, str, None] = None,
        scl: Union[Pin, int, str, None] = None,
        spi: Union[Sequence[Union[Pin, int, str]], Mapping[str, Union[Pin, int, str]], None] = None,
        load: Union[Pin, int, str, None] = None,
        count: Optional[int] = None,
        hz: Optional[int] = None,
        pullups: Optional[str] = None,
        names: Union[Mapping[Any, str], Sequence[Optional[str]], None] = None,
    ) -> "_expansion.ExpansionStatus":
        """Attach a GPIO expander board, report it, or detach it.

        ``expansion("tca9555", addr=0x20, sda=1, scl=2)`` attaches one: it checks the LA voltage is
        one the chip runs at, probes the chip, puts every pin in input and names them
        ``EXT1``..``EXTn``. From then on :meth:`gpio`, :meth:`gpio_pins`, :meth:`set_gpio`,
        :meth:`read_gpio`, :meth:`wait_for_level`, :meth:`release_gpio` and :meth:`signal` take
        ``"EXTn"`` (or the names) next to LA channels. ``expansion()`` returns the
        :class:`~embeddedci.benchpod.expansion.ExpansionStatus` (chip, bus pins, each pin's mode and
        live level); ``expansion(None)`` puts every pin back to input and detaches.
        :data:`~embeddedci.benchpod.expansion.EXPANSION_CHIPS` lists the chips.

        I2C chips: ``addr`` is the 7-bit address (default: the chip's first), or a list of them for
        several chips on one bus (EXT pins numbered in that order); ``sda``/``scl`` default to the
        wiring profile's ``i2c_sda``/``i2c_scl``, so the expander can share the DUT's bus.
        ``pullups`` is ``"pod"`` (the pod's pull-ups on LA1-LA6, switched on here; they pull to the LA
        voltage) or ``"external"`` (the breakout's own); by default ``"pod"`` when both pins are on
        LA1-LA6 and the pod has pull-ups at this LA voltage (3.3 V, or 1.8 V on firmware with the
        ``la_pull_1v8`` feature). ``hz`` defaults to 400 kHz (the chip's limit when lower).

        ``names`` names the pins: ``{"EXT1": "LED1", ...}`` or a list in EXT order. Omitted
        arguments come from an ``expansion`` object in the wiring profile, when it has one
        (``{"chip": "tca9555", "addr": "0x20", "sda": 1, "scl": 2, "names": {...}}``).

        The SDK opens the bus on the first EXT access and keeps it open; it closes it on its own
        before :meth:`flash`, :meth:`open_i2c`, :meth:`open_spi` and :meth:`close`. An
        :meth:`open_i2c` session on the same two pins is used as it is. EXT pins are not
        FPGA-timed: pulses, captures, triggers and :meth:`la_dir` refuse them.
        """
        exp = self._expansion
        given = any(v is not None for v in (addr, sda, scl, spi, load, count, hz, pullups, names))
        if chip == "":
            if given:
                raise ValueError("name the chip to attach, e.g. expansion('tca9555', addr=0x20)")
            return exp.status() if exp is not None else _expansion.ExpansionStatus(attached=False)
        if chip is None:
            if given:
                raise ValueError("expansion(None) detaches; it takes no other arguments")
            self._detach_expansion()
            return _expansion.ExpansionStatus(attached=False)
        info = _expansion.expansion_chip(chip)
        block = self._wiring_expansion(info)
        lv = self.get_la_voltage().voltage
        if lv is None:
            raise _expansion.ExpansionError("set the LA voltage first (set_la_voltage(3.3) or 1.8): "
                                            "the expander runs at the LA voltage")
        if not any(abs(lv - v) < 0.05 for v in info.voltages):
            ok = " or ".join(f"{v:g} V" for v in info.voltages)
            raise _expansion.ExpansionError(
                f"the {info.name} does not run at the LA voltage {lv:g} V (it needs {ok}); pick a chip "
                "for this voltage (EXPANSION_CHIPS lists them) or change the LA voltage")
        default_hz = min(400_000 if info.bus == "i2c" else 1_000_000, info.max_hz)
        hz_i = int(hz if hz is not None else block.get("hz") or default_hz)
        if isinstance(hz, bool) or hz_i <= 0 or hz_i > info.max_hz:
            raise ValueError(f"hz must be 1..{info.max_hz} for the {info.name}, got {hz!r}")
        names_arg = names if names is not None else block.get("names")
        pull: Optional[str] = None
        if info.bus == "i2c":
            if spi is not None or load is not None or (count not in (None, 1)):
                raise ValueError(f"the {info.name} is an I2C chip: spi, load and count do not apply")
            sda_i = self._wired(sda if sda is not None else block.get("sda"), "i2c_sda")
            scl_i = self._wired(scl if scl is not None else block.get("scl"), "i2c_scl")
            if sda_i == scl_i:
                raise ValueError("sda and scl must be different LA pins")
            la_map = {"sda": sda_i, "scl": scl_i}
            addrs = self._expansion_addrs(info, addr if addr is not None else block.get("addr"))
            n_chips = 1
            pull = pullups if pullups is not None else block.get("pullups")
            pod_ok = self._pulls_at(lv) and sda_i <= 6 and scl_i <= 6
            if pull is None:
                pull = "pod" if pod_ok else "external"
            check_choice(pull, _i2c_master.PULLUPS, "pullups")
            if pull == "pod" and not pod_ok:
                raise _expansion.ExpansionError(
                    "pullups='pod' needs both pins on LA1-LA6 (only they have pod pull-ups) and an "
                    "LA voltage of 1.8 V or 3.3 V; use pullups='external' with pull-ups on the breakout")
        else:
            if sda is not None or scl is not None or pullups is not None:
                raise ValueError(f"the {info.name} is an SPI chip: sda, scl and pullups do not "
                                 "apply (give spi=(sck, mosi, miso, cs))")
            la_map = self._expansion_spi_pins(info, spi if spi is not None else block.get("spi"))
            load_v = load if load is not None else block.get("load")
            if info.name == "74hc165":
                if load_v is None:
                    raise ValueError("the 74hc165 needs load=<LA channel> wired to SH/LD (pulsed "
                                     "low before each read)")
                la_map["load"] = self._resolve_la(load_v, "load")[0]
            elif load_v is not None:
                raise ValueError(f"load only applies to the 74hc165, not the {info.name}")
            if len(set(la_map.values())) != len(la_map):
                raise ValueError("each SPI signal (and load) needs its own LA pin: "
                                 + ", ".join(f"{k}=LA{v}" for k, v in la_map.items()))
            count_v = count if count is not None else block.get("count")
            if info.chained:
                if addr is not None:
                    raise ValueError(f"a {info.name} chain has no address: give count= instead")
                n_chips = 1 if count_v is None else count_v
                if isinstance(n_chips, bool) or not isinstance(n_chips, int) or not \
                        1 <= n_chips <= _expansion.CHAIN_MAX:
                    raise ValueError(f"count is the chips in the chain, 1..{_expansion.CHAIN_MAX}, "
                                     f"got {count_v!r}")
                addrs = []
            else:
                if count_v not in (None, 1):
                    raise ValueError(f"count is for shift-register chains; give several "
                                     f"{info.name} addresses with addr=[...] instead")
                n_chips = 1
                addrs = self._expansion_addrs(info, addr if addr is not None else block.get("addr"))
        self._ensure_bus_masters("the I2C master" if info.bus == "i2c" else "the SPI master")
        self._detach_expansion()
        new = _expansion.Expansion(self, info, addrs=addrs, count=n_chips, la=la_map,
                                   hz=hz_i, pullups=pull, names={})
        new.names = _expansion.parse_names(names_arg, new.total)
        self._check_expansion_names(new)
        if "load" in la_map:
            self._gpio_configure([la_map["load"]], "output", 1)
            new.load_claimed = True
        if pull == "pod":
            self.enable_pullup(la_map["sda"], la_map["scl"])
            new.pod_pullups = (la_map["sda"], la_map["scl"])
        self._expansion = new
        try:
            new.attach()
        except BaseException:
            self._expansion = None
            try:
                new.release_bus()
                if new.pod_pullups:
                    self.disable_pullup(*new.pod_pullups)
                if new.load_claimed:
                    self.release_gpio(la_map["load"])
            except Exception:
                pass
            raise
        return new.status()

    def _detach_expansion(self) -> None:
        exp, self._expansion = self._expansion, None
        if exp is None:
            return
        try:
            exp.detach()
        finally:
            if exp.load_claimed:
                self.release_gpio(exp.la["load"])
            if exp.pod_pullups:
                mine = self._i2c_master_ref() if self._i2c_master_ref is not None else None
                if mine is None or mine.closed:  # the user's own session still needs them
                    self.disable_pullup(*exp.pod_pullups)

    def _yield_expansion_bus(self) -> None:
        """Close the expander's own bus session so the pod's SWD/SPI/I2C engine is free."""
        exp = getattr(self, "_expansion", None)
        if exp is not None:
            exp.release_bus()

    def _wiring_expansion(self, info: "_expansion.ExpansionChip") -> Dict[str, Any]:
        """The wiring profile's ``expansion`` object when it is for this chip, else ``{}``."""
        try:
            block = self.wiring.extra.get("expansion")
        except Exception:
            return {}
        if not isinstance(block, Mapping):
            return {}
        named = block.get("chip")
        if named is not None and _expansion.expansion_chip(str(named)).name != info.name:
            return {}
        return dict(block)

    def _expansion_spi_pins(self, info: "_expansion.ExpansionChip", spi: Any) -> Dict[str, int]:
        """``spi`` as ``{"sck", "mosi", "miso", "cs"}``: a 4-tuple, a mapping, or None for the wiring
        profile's ``spi_sclk``/``spi_mosi``/``spi_miso``/``spi_cs``."""
        keys = ("sck", "mosi", "miso", "cs")
        roles = ("spi_sclk", "spi_mosi", "spi_miso", "spi_cs")
        if spi is None:
            values: List[Any] = [None] * 4
        elif isinstance(spi, Mapping):
            unknown = set(spi) - set(keys)
            if unknown:
                raise ValueError(f"spi takes the keys sck, mosi, miso, cs; got {sorted(unknown)}")
            values = [spi.get(k) for k in keys]
        elif isinstance(spi, (list, tuple)) and len(spi) == 4:
            values = list(spi)
        else:
            raise ValueError("spi is (sck, mosi, miso, cs) or {'sck': .., 'mosi': .., 'miso': .., "
                             "'cs': ..}; the pod's SPI master needs all four pins (a 74HC595's MISO "
                             "and a 74HC165's MOSI can be any free pin)")
        return {k: self._wired(v, role) for k, v, role in zip(keys, values, roles)}

    @staticmethod
    def _expansion_addrs(info: "_expansion.ExpansionChip", addr: Any) -> List[int]:
        items = list(addr) if isinstance(addr, (list, tuple)) else [addr]
        out: List[int] = []
        for a in items:
            if a is None:
                a = info.addrs[0]
            if not info.addrs:  # pragma: no cover - chains never get here
                raise ValueError(f"the {info.name} has no address")
            if isinstance(a, str):
                try:
                    a = int(a, 0)
                except ValueError:
                    raise ValueError(f"addr {a!r} is not a number") from None
            if isinstance(a, bool) or not isinstance(a, int) or a not in info.addrs:
                lo, hi = info.addrs[0], info.addrs[-1]
                what = "hardware addresses" if info.bus == "spi" else "addresses"
                raise ValueError(f"the {info.name} has {what} 0x{lo:02x}..0x{hi:02x}, got {a!r}")
            if a in out:
                raise ValueError(f"addr 0x{a:02x} is listed twice")
            out.append(a)
        return out

    def _check_expansion_names(self, exp: "_expansion.Expansion") -> None:
        try:
            w = self.wiring
        except Exception:
            return
        taken = {s.name.lower() for s in w.signals} | {k for k, _ in w.assignments()}
        clash = sorted(n for n in exp.names.values() if n.lower() in taken)
        if clash:
            raise ValueError(f"names: {', '.join(clash)} already name LA channels in the wiring profile")

    def _require_expansion(self) -> "_expansion.Expansion":
        if self._expansion is None:
            raise _expansion.ExpansionError("no expander is attached: attach one with "
                                            "bp.expansion('tca9555', addr=0x20, sda=1, scl=2)")
        return self._expansion

    def _expansion_pin_state(self, n: int) -> "_expansion.ExtPinState":
        exp = self._require_expansion()
        return exp.pin_state(n, exp.read([n])[0])

    def _ext(self, value: Any) -> Optional[int]:
        """The EXT number ``value`` names, or ``None`` when it is not an expander pin."""
        exp = getattr(self, "_expansion", None)
        if exp is not None:
            return exp.resolve(value)
        n = _expansion.ext_index(value)
        if n is not None and not any(sig.name.lower() == str(value).strip().lower()
                                     for sig in self.wiring.signals):
            raise _expansion.ExpansionError(
                f"{value} is an expander pin, and no expander is attached: attach one with "
                "bp.expansion('tca9555', addr=0x20, sda=1, scl=2)")
        return None

    def _pin_refs(self, la: Any) -> List[Tuple[str, int, Optional[Signal]]]:
        """Each of ``la`` (one or a list) as ``("la", channel, signal)`` or ``("ext", n, None)``."""
        items = list(la) if isinstance(la, (list, tuple)) else [la]
        if not items:
            raise ValueError("give at least one LA channel")
        out: List[Tuple[str, int, Optional[Signal]]] = []
        for x in items:
            n = self._ext(x) if isinstance(x, str) else None
            if n is not None:
                out.append(("ext", n, None))
            else:
                la_i, signal = self._resolve_la(x)
                out.append(("la", la_i, signal))
        return out

    def _gpio_pin(self, ref: Tuple[str, int, Optional[Signal]]) -> GpioPin:
        kind, n, signal = ref
        if kind == "ext":
            exp = self._require_expansion()
            return ExtPin(self, n, name=exp.names.get(n))
        return GpioPin(self, n, signal=signal)

    # -- power profiles -------------------------------------------------------

    def measure_power(self, duration: float, *, efuse: Optional[Union[Efuse, int]] = None,
                      rate_hz: float = 500.0, keep_samples: int = 0) -> PowerProfile:
        """Profile a target-power rail for ``duration`` seconds (blocking) and return the
        :class:`~embeddedci.benchpod.power.PowerProfile`: average, minimum and peak current, voltage,
        energy and charge. ``efuse`` defaults to the wiring profile's rail; ``keep_samples`` (up to
        4096) also returns a bin-averaged trace."""
        if not 0 < duration <= 600:
            raise ValueError(f"duration must be > 0 and <= 600 seconds, got {duration!r}")
        req = self._power_request(efuse, rate_hz, keep_samples)
        req["duration_ms"] = max(1, int(round(duration * 1000)))
        return PowerProfile.from_chunks(self._power_profile_chunks(req))

    def power_profile(self, *, efuse: Optional[Union[Efuse, int]] = None, rate_hz: float = 500.0,
                      keep_samples: int = 4096, max_duration: float = 60.0) -> PowerProfileSession:
        """A power profile around a block of code::

            with bp.power_profile() as prof:
                run_inference()
            assert prof.result.peak_current < 0.5

        Sampling stops by itself after ``max_duration`` seconds (``result.truncated``).
        """
        if not 0 < max_duration <= 600:
            raise ValueError(f"max_duration must be > 0 and <= 600 seconds, got {max_duration!r}")
        req = self._power_request(efuse, rate_hz, keep_samples)
        req["max_duration_ms"] = int(round(max_duration * 1000))
        return PowerProfileSession(self, req)

    def _power_request(self, efuse: Optional[Union[Efuse, int]], rate_hz: float,
                       keep_samples: int) -> Dict[str, Any]:
        if not 100 <= rate_hz <= 500:
            raise ValueError(f"rate_hz must be 100..500, got {rate_hz!r} (the pod samples one "
                             "reading per firmware pass; above ~200 Hz it delivers what it can, "
                             "flattening near 365 Hz, and the result's rate_hz says what you got)")
        if not 0 <= int(keep_samples) <= 4096:
            raise ValueError(f"keep_samples must be 0..4096, got {keep_samples!r}")
        rail = self._efuse(efuse)
        self._require_capability("power_profile", "power profiles")
        # The API takes hertz as a float like every other rate, but the firmware's JSON parser
        # wants a whole number here — send 1000, not 1000.0.
        return {"cmd": "power_profile", "efuse": rail, "sample_rate_hz": int(round(rate_hz)),
                "keep_samples": int(keep_samples)}

    def _power_profile_chunks(self, req: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        stream = getattr(self._transport, "stream_chunks", None)
        with _classified_errors():
            if stream is not None:
                yield from stream(req)
            else:
                yield {"data": _dict(self.command(req))}

    # -- LA step pulse train --------------------------------------------------

    def la_step(self, la: Union[Pin, int], *, steps: int, delay: float,
                dir_la: Optional[Union[Pin, int]] = None, direction: int = 0) -> Dict[str, Any]:
        """Emit ``steps`` step pulses on an LA channel (step/dir drivers).

        ``delay`` is HALF the step period: each pulse is high for ``delay`` seconds, then low for
        ``delay``, so pulses are ``2 * delay`` apart (``1 / (2 * delay)`` steps per second).
        ``steps`` is 1..65535 and ``delay`` 4 µs..65.535 ms (the gateware counters are 16-bit);
        split a longer move into several trains. The FPGA runs the train autonomously and this
        returns immediately. With ``dir_la`` set, that channel is driven to ``direction`` (0/1)
        first.
        """
        if not 1 <= steps <= LA_STEP_MAX_STEPS:
            raise ValueError(f"steps must be 1..{LA_STEP_MAX_STEPS} (the pod's step counter is "
                             f"16-bit; split a longer move), got {steps!r}")
        delay_us = int(round(delay * 1e6))
        if not LA_STEP_MIN_DELAY_US <= delay_us <= LA_STEP_MAX_DELAY_US:
            raise ValueError(f"delay must be {LA_STEP_MIN_DELAY_US} µs..{LA_STEP_MAX_DELAY_US / 1e3:g} ms "
                             f"(half the step period), got {delay!r}")
        if direction not in (0, 1):
            raise ValueError(f"direction must be 0 or 1, got {direction!r}")
        req: Dict[str, Any] = {"cmd": "la_step", "la": self._resolve_la(la, why=_EXT_PULSES)[0],
                               "steps": int(steps),
                               "delay_us": delay_us}
        if dir_la is not None:
            req["dir_la"] = self._resolve_la(dir_la, "dir_la", why=_EXT_PULSES)[0]
            req["direction"] = int(direction)
        return _dict(self.command(req))

    # -- raw commands ---------------------------------------------------------

    def command(self, req: Dict[str, Any]) -> Any:
        """Send one raw JSON command (``{"cmd": ..., ...}``) and return its ``data``.

        An escape hatch for firmware commands the SDK does not wrap; raises
        :class:`~embeddedci.benchpod.errors.FirmwareError` when the pod refuses. Works over TCP,
        serial (JSON console mode) and the cloud. Not covered by the stability guarantee.
        """
        cmd = getattr(self._transport, "command", None)
        if cmd is None:
            raise BenchPodError(f"{type(self._transport).__name__} does not support JSON commands")
        if not isinstance(req, dict) or not req.get("cmd"):
            raise ValueError("a raw command needs a 'cmd' field, e.g. {'cmd': 'status'}")
        with _classified_errors():
            return cmd(req)
