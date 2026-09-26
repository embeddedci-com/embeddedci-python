"""The :class:`BenchPod` facade — the user-facing API.

Wraps a :class:`~embeddedci.benchpod.transport.base.Transport` with named operations: power,
flash, UART, I2C-sensor emulation, analog paths, captures, DAC generate/replay, the in-fabric
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
import logging
import os
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from . import can as _can
from . import capture as _capture
from . import control_loop as _control_loop
from . import dsp as _dsp
from . import flash as _flash
from . import i2c as _i2c
from . import sensor as _sensor
from . import uart as _uart
from .capabilities import Capabilities
from .constants import (
    ADC_SOURCE_PATHS,
    ADC_SOURCES,
    ANALOG_PATHS,
    CAN_MODES,
    DAC_OUTPUT_PATHS,
    DAC_PATHS,
    DECODE_PROTOCOLS,
    GPIO_MODES,
    LA_VOLTAGES,
    PULLDOWN_CHANNELS,
    PULLUP_CHANNELS,
    REPLAY_MAPPINGS,
    WAVESHAPES,
    AdcSource,
    AnalogPath,
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
    Sensor,
    Waveshape,
    check_choice,
    coerce_efuse,
    coerce_pin,
)
from .connection import resolve_connection
from .errors import BenchPodError, FirmwareError, classify_firmware_error
from .flash import FlashResult
from .gpio import GpioPin, LaPinState
from .lease import DEFAULT_LEASE_TTL, DEFAULT_LEASE_WAIT, DeviceLease
from .lowlevel import LowLevel
from .power import PowerProfile, PowerProfileSession
from .replay import DacHandle, Fault, ReplayHandle, normalize_fault
from .results import Capture, CorrelatedCapture, LaCapture, Trigger
from .wiring import Signal, Wiring
from .state import (
    AdcReading,
    AnalogPathState,
    DacOutput,
    FpgaImageInfo,
    LaVoltage,
    LoopState,
    PowerStatus,
    PullState,
    ResetState,
    TargetStatus,
    UsbCcStatus,
)
from .transport import Transport, open_transport
from .transport.cloud import CloudTransport

if TYPE_CHECKING:  # pragma: no cover
    from .server_api import ServerApi
    from .waveforms import Waveform, WaveformLibrary

#: Environment variables the client reads when the matching argument is not passed.
LA_VOLTAGE_ENV = "BENCHPOD_LA_VOLTAGE"
API_BASE_ENV = "BENCHPOD_API_BASE"
API_KEY_ENV = "BENCHPOD_API_KEY"
WIRING_ENV = "BENCHPOD_WIRING"

#: Levels of the firmware's parametric generator (it builds each waveform from 8-bit codes).
_GENERATOR_MAX_CODE = 255

_log = logging.getLogger("embeddedci.benchpod")

#: The capability flag and FPGA_FEATURES bit each gateware image carries.
_IMAGE_FLAG = {FpgaImage.LOOP: "dac_control_loop", FpgaImage.DEEP_REPLAY: "dac_deep_replay"}
_IMAGE_FEATURE = {FpgaImage.LOOP: 0x01, FpgaImage.DEEP_REPLAY: 0x02}

#: Seconds to wait for the server's cached capabilities to follow an image switch.
_SERVER_CAPS_WAIT = 15.0


def _images_switchable(caps: Capabilities) -> bool:
    """Whether the pod carries both gateware images: it advertises one of the image-bound
    features (the rule the web UI uses too)."""
    return bool(caps.dac_control_loop or caps.dac_deep_replay)


def _dict(data: Any) -> Dict[str, Any]:
    return data if isinstance(data, dict) else {}


def _seconds_to_ms(value: Optional[float], name: str) -> int:
    if value is None:
        return 0
    if value < 0:
        raise ValueError(f"{name} must be >= 0 seconds, got {value!r}")
    return int(round(value * 1000))


def _check_level(level: Any) -> int:
    if level not in (0, 1):
        raise ValueError(f"level must be 0 or 1, got {level!r}")
    return int(level)


def _one_or_many(las: List[int]) -> Any:
    return las[0] if len(las) == 1 else las


@contextmanager
def _classified_errors() -> Iterator[None]:
    """Re-raise a pod refusal as its specific error (PinConflictError, PullConflictError, TriggerTimeout)."""
    try:
        yield
    except FirmwareError as exc:
        specific = classify_firmware_error(exc)
        if specific is exc:
            raise
        raise specific from None


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
        lease: bool = True,
        lease_wait: float = DEFAULT_LEASE_WAIT,
        lease_ttl: int = DEFAULT_LEASE_TTL,
        wiring: Union[Wiring, Mapping[str, Any], str, "os.PathLike[str]", None] = None,
    ) -> None:
        """Open a BenchPod.

        ``connection`` is a host[:port], a serial device path, ``"usb"`` (auto-detect the USB
        console), ``"discover"`` (find one pod on the LAN via mDNS), or ``"embeddedci:<device>"``
        to drive a named device through embeddedci.com. When omitted ``BENCHPOD_CONNECTION`` is
        used. Pass ``transport`` to inject a custom backend instead.

        ``la_voltage`` (1.8 or 3.3 volts, falling back to ``BENCHPOD_LA_VOLTAGE``) selects the LA
        I/O-bank voltage right after connecting. The pod refuses every LA-bank operation —
        flashing, UART, LA capture, pull resistors, I2C-sensor emulation — until one is selected.

        Cloud options: ``api_key`` (or ``BENCHPOD_API_KEY``) authenticates anywhere; without it the
        cloud destination uses GitHub Actions OIDC (``cloud_token``/``cloud_audience`` override
        that). ``api_base`` (or ``BENCHPOD_API_BASE``) points at another embeddedci server. An API
        key also unlocks the cloud waveform library on a LAN/serial connection.

        The cloud device is *shared*, so the client takes an exclusive **lease** on it for the life
        of this object; a run that finds it busy waits up to ``lease_wait`` seconds and then raises
        :class:`~embeddedci.benchpod.errors.DeviceBusyError`. ``lease=False`` skips locking. Local
        TCP/serial connections never lease.

        ``wiring`` is the bench's :class:`~embeddedci.benchpod.wiring.Wiring` profile (a ``Wiring``, a
        dict, or a ``.json``/``.toml`` path) — see :attr:`wiring`.
        """
        self._wiring: Optional[Wiring] = Wiring.coerce(wiring) if wiring is not None else None
        self.timeout = timeout
        self._lease: Optional[DeviceLease] = None
        self._api_base = api_base or os.environ.get(API_BASE_ENV)
        self._api_key = api_key or os.environ.get(API_KEY_ENV)
        self._caps: Optional[Capabilities] = None
        self._server_api: Optional["ServerApi"] = None
        self._waveforms: Optional["WaveformLibrary"] = None
        self._device_name = ""
        #: Hardware controls below the named-path API (raw mux/relay/DAC-code access). Not covered
        #: by the stability guarantee.
        self.lowlevel = LowLevel(self)
        if transport is not None:
            self._transport: Transport = transport
        else:
            spec = resolve_connection(connection)
            self._transport = open_transport(
                spec,
                timeout=timeout,
                api_base=self._api_base,
                token=cloud_token,
                audience=cloud_audience,
                api_key=self._api_key,
            )
        try:
            if lease and isinstance(self._transport, CloudTransport):
                self._lease = DeviceLease(
                    api_base=self._transport.api_base,
                    token_provider=self._transport._session_token,
                    device_name=self._transport.device_name,
                    ttl_seconds=lease_ttl,
                )
                self._lease.acquire(wait_timeout=lease_wait)
                self._transport.lease_id = self._lease.lease_id
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
        except BaseException:
            self.close()
            raise

    # -- lifecycle ----------------------------------------------------------

    @property
    def transport(self) -> Transport:
        """The underlying transport (escape hatch; not covered by the stability guarantee)."""
        return self._transport

    @property
    def leased(self) -> bool:
        """True while this client holds the exclusive lease on a shared cloud device."""
        return self._lease is not None and self._lease.held

    def close(self) -> None:
        """Close the connection and release the cloud lease, if any. Idempotent."""
        try:
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

    @property
    def capabilities(self) -> Capabilities:
        """The device's resolved :class:`Capabilities` (ADC scaling, DAC replay depth, flags).

        Parsed from :meth:`status` and — when the SDK has server access — enriched with the
        fuller ``cap.*`` set the server holds (explicit calibration + replay depth). Cached after
        the first read; :meth:`refresh_capabilities` re-reads it.
        """
        if self._caps is None:
            try:
                status = self.status()
            except BenchPodError:
                status = {}
            caps = Capabilities.from_status(status)
            api = self._try_server_api()
            if api is not None and self._device_name:
                try:
                    params = api.device_parameters(self._device_name)
                    if params:
                        caps = caps.merge(Capabilities.from_parameters(params))
                except BenchPodError:
                    pass
            self._caps = caps
        return self._caps

    def refresh_capabilities(self) -> Capabilities:
        """Drop the cached capabilities and re-read them."""
        self._caps = None
        return self.capabilities

    def _require_capability(self, flag: str, what: str) -> None:
        if not getattr(self.capabilities, flag, False):
            fw = self.capabilities.firmware_version or "unknown"
            raise BenchPodError(f"{what} need newer pod firmware (the {flag!r} capability is missing; "
                                f"this pod runs firmware {fw})")

    # -- wiring profile -------------------------------------------------------

    @property
    def wiring(self) -> Wiring:
        """The bench's :class:`~embeddedci.benchpod.wiring.Wiring` profile: which DUT signal is on
        which LA channel, the target-power rail and the LA I/O voltage.

        Resolved once: the ``wiring`` argument; else the ``BENCHPOD_WIRING`` file; else, for a cloud
        device, the profile stored on embeddedci.com (edited in the web UI); else the defaults.
        Channel arguments fall back to it — ``open_uart()``, ``capture_uart()``, ``flash()``,
        ``enable_i2c_sensor()``, the power rail — and :meth:`signal` / GPIO names come from it.
        Assign a profile to use another one for this connection (not saved; see :meth:`save_wiring`).
        """
        if self._wiring is None:
            self._wiring = self._resolve_wiring()
        return self._wiring

    @wiring.setter
    def wiring(self, value: Union[Wiring, Mapping[str, Any], str, "os.PathLike[str]"]) -> None:
        self._wiring = Wiring.coerce(value)

    def _resolve_wiring(self) -> Wiring:
        env = os.environ.get(WIRING_ENV, "").strip()
        if env:
            return Wiring.load(env)
        if self._device_name:
            api = self._try_server_api()
            if api is not None:
                try:
                    data = api.wiring_profile(api.resolve_device_id(self._device_name))
                    return Wiring.from_dict(data.get("profile") or {}, source="server", strict=False)
                except (BenchPodError, ValueError) as exc:
                    _log.warning("could not load the wiring profile of %s from embeddedci.com (%s); "
                                 "using the defaults", self._device_name, exc)
        return Wiring.defaults()

    def save_wiring(self, wiring: Union[Wiring, Mapping[str, Any], str, None] = None) -> Wiring:
        """Store a wiring profile for this cloud device on embeddedci.com (default: :attr:`wiring`)
        and use it. The server validates it too; a LAN pod keeps its profile in a file instead."""
        profile = self.wiring if wiring is None else Wiring.coerce(wiring)
        if not self._device_name:
            raise BenchPodError("saving a wiring profile needs a cloud device ('embeddedci:<device>'); "
                                "for a LAN pod keep the profile in a file and pass wiring=")
        api = self._require_server_api()
        api.put_wiring(api.resolve_device_id(self._device_name), profile.to_dict())
        self._wiring = profile.with_changes(source="server")
        return self._wiring

    def signal(self, name: str) -> GpioPin:
        """A :class:`~embeddedci.benchpod.gpio.GpioPin` for a named signal (or a role such as
        ``"uart_tx"``) of the wiring profile. Nothing is sent to the pod until you use it."""
        la, signal = self._resolve_la(name, "signal")
        return GpioPin(self, la, signal=signal)

    def _resolve_la(self, value: Any, name: str = "la") -> Tuple[int, Optional[Signal]]:
        """An LA channel from 1-14 / a ``Pin`` / a wiring-profile name (role or signal)."""
        if isinstance(value, str):
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
        return coerce_efuse(self.wiring.efuse if efuse is None else efuse)

    # -- LA I/O-bank voltage ------------------------------------------------

    def set_la_voltage(self, voltage: float) -> LaVoltage:
        """Select the LA I/O-bank voltage: ``1.8`` or ``3.3`` volts (1.8 V needs a rev3 pod).

        Match it to the DUT's I/O voltage. It must be set before any LA-bank operation —
        flashing, the UART proxy, LA capture, pull resistors, I2C-sensor emulation — or the pod
        refuses them with "la voltage not set". :class:`BenchPod` sets it on connect when
        ``la_voltage`` / ``BENCHPOD_LA_VOLTAGE`` is given.
        """
        v = float(voltage)
        match = next((ok for ok in LA_VOLTAGES if abs(v - ok) < 0.05), None)
        if match is None:
            raise ValueError(f"la voltage must be 1.8 or 3.3 (volts), got {voltage!r}")
        return LaVoltage.from_reply(self.command({"cmd": "la_voltage", "mv": int(round(match * 1000))}))

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

    def power_on(self, efuse: Optional[Union[Efuse, int]] = None,
                 *, delay: Optional[float] = None) -> None:
        """Power the target on (omitted ``efuse``: the wiring profile's rail, INTERNAL 5 V by default)."""
        self.target_power(efuse, on=True, delay=delay)

    def power_off(self, efuse: Optional[Union[Efuse, int]] = None,
                  *, delay: Optional[float] = None) -> None:
        """Power the target off."""
        self.target_power(efuse, on=False, delay=delay)

    def target_status(self) -> TargetStatus:
        """Both eFuse rails: enabled, tripped (``fault``), and whether the state was readable."""
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

    # -- flash --------------------------------------------------------------

    def flash(
        self,
        *,
        swclk: Union[Pin, int, str, None] = None,
        swdio: Union[Pin, int, str, None] = None,
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
    ) -> FlashResult:
        """Flash an SWD target through the pod's CMSIS-DAP probe and report the result.

        ``swclk``/``swdio`` are LA pins (``benchpod.PIN1``..``PIN14`` or 1-14). ``nreset`` is a
        flag, not a pin: pass ``True`` when the target's reset line is wired to the pod's reset
        pin (DUT header J1 pin 22). ``target`` is an OpenOCD target config
        (``target/stm32f4x.cfg``) and ``file`` the image. ``target_power`` of
        ``benchpod.INTERNAL``/``EXTERNAL`` powers the target first. Omitted ``swclk``, ``swdio``,
        ``nreset`` and ``target`` come from the wiring profile (:attr:`wiring`).

        OpenOCD runs on THIS machine and needs the ``cmsis_dap_tcp`` backend (newer than 0.12.0).
        By default (``check=True``) a failed flash raises :class:`FlashError` /
        :class:`TargetUnreachableError`; pass ``check=False`` to get the :class:`FlashResult` and
        ``assert result.ok`` yourself.
        """
        swclk_i = self._wired(swclk, "swd_swclk")
        swdio_i = self._wired(swdio, "swd_swdio")
        if swclk_i == swdio_i:
            raise ValueError("swclk and swdio must be different LA pins")
        if nreset is None:
            nreset = self.wiring.swd_nreset
        target = target or self.wiring.swd_target
        power = coerce_efuse(target_power) if target_power is not None else None

        with _classified_errors():
            result = _flash.flash(
                self._transport,
                swclk=swclk_i, swdio=swdio_i, nreset=bool(nreset),
                target=target, file=file, load_address=load_address,
                target_power=power, verify=verify, reset=reset,
                connect_under_reset=connect_under_reset,
                clear_reset_events=clear_reset_events,
                openocd_bin=openocd_bin,
                extra_configs=extra_configs, extra_args=extra_args,
                timeout=timeout, connect_attempts=connect_attempts,
            )
        if check:
            _flash.raise_for_result(result)
        return result

    # -- LA bias resistors (LA1-8) ------------------------------------------
    # LA1-LA6 carry a pull-UP, LA7/LA8 a pull-DOWN (see constants.PULL_OHMS). The resistors are
    # 3V3-referenced, so the pod refuses to engage one while the LA bank is at 1.8 V.

    @staticmethod
    def _bias_channel(la: Union[Pin, int], allowed: Sequence[int], kind: str) -> int:
        la_i = coerce_pin(la, "la")
        if la_i not in allowed:
            if la_i in PULLDOWN_CHANNELS and kind == "pull-up":
                raise ValueError(f"LA{la_i} has a pull-DOWN, not a pull-up; use enable_pulldown")
            if la_i in PULLUP_CHANNELS and kind == "pull-down":
                raise ValueError(f"LA{la_i} has a pull-UP, not a pull-down; use enable_pullup")
            raise ValueError(f"LA{la_i} has no {kind} (bias resistors exist on LA1-LA8 only)")
        return la_i

    def set_pull(self, la: Union[Pin, int], enabled: bool) -> PullState:
        """Engage or release LA ``la``'s fixed bias resistor, whichever way it pulls (LA1-LA8)."""
        la_i = self._bias_channel(la, PULLUP_CHANNELS + PULLDOWN_CHANNELS, "bias resistor")
        reply = self.command({"cmd": "la", "la": la_i, "pullup": "on" if enabled else "off"})
        return PullState.from_reply(reply, la_i)

    def pull_state(self, la: Union[Pin, int]) -> PullState:
        """Read LA ``la``'s bias resistor: engaged, direction, value (LA1-LA8)."""
        la_i = self._bias_channel(la, PULLUP_CHANNELS + PULLDOWN_CHANNELS, "bias resistor")
        return PullState.from_reply(self.command({"cmd": "la", "la": la_i}), la_i)

    def enabled_pulls(self) -> List[int]:
        """The LA channels whose bias resistor is currently engaged."""
        mask = int(_dict(self.command({"cmd": "la"})).get("la_pullup_mask", 0) or 0)
        return [la for la in range(1, 9) if mask >> (la - 1) & 1]

    def enable_pullup(self, *las: Union[Pin, int]) -> None:
        """Engage the pull-up on LA channels from LA1-LA6 — e.g. an open-drain I2C bus's SDA/SCL."""
        for la in las:
            self.set_pull(self._bias_channel(la, PULLUP_CHANNELS, "pull-up"), True)

    def disable_pullup(self, *las: Union[Pin, int]) -> None:
        """Release the pull-up on LA channels from LA1-LA6."""
        for la in las:
            self.set_pull(self._bias_channel(la, PULLUP_CHANNELS, "pull-up"), False)

    def enable_pulldown(self, *las: Union[Pin, int]) -> None:
        """Engage the pull-down on LA7 and/or LA8."""
        for la in las:
            self.set_pull(self._bias_channel(la, PULLDOWN_CHANNELS, "pull-down"), True)

    def disable_pulldown(self, *las: Union[Pin, int]) -> None:
        """Release the pull-down on LA7 and/or LA8."""
        for la in las:
            self.set_pull(self._bias_channel(la, PULLDOWN_CHANNELS, "pull-down"), False)

    # -- emulated I2C sensor ------------------------------------------------

    def enable_i2c_sensor(
        self,
        sensor: Union[Sensor, str] = Sensor.BMP280,
        *,
        sda: Union[Pin, int, str, None] = None,
        scl: Union[Pin, int, str, None] = None,
        address: Optional[int] = None,
        temperature_c: Optional[float] = None,
        pressure_pa: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Make the pod emulate an I2C sensor (a BMP280) on ``sda``/``scl``.

        The pod becomes an I2C target the DUT's controller can read. Engage the pull-ups on SDA/SCL
        first (:meth:`enable_pullup`) so the open-drain bus idles high. Optionally seed
        ``temperature_c``/``pressure_pa``. Omitted ``sda``, ``scl`` and ``address`` come from the
        wiring profile. Returns the pod's start reply.
        """
        sda_i = self._wired(sda, "i2c_sda")
        scl_i = self._wired(scl, "i2c_scl")
        addr = self.wiring.i2c_address if address is None else address
        with _classified_errors():
            result = _sensor.sensor_start(
                self._transport, sensor, sda=sda_i, scl=scl_i, address=addr
            )
            if temperature_c is not None or pressure_pa is not None:
                _sensor.sensor_set(self._transport, temperature_c=temperature_c,
                                   pressure_pa=pressure_pa)
        return result

    def set_i2c_sensor(self, *, temperature_c: Optional[float] = None,
                       pressure_pa: Optional[float] = None) -> Dict[str, Any]:
        """Update the emulated sensor's reported values (at least one)."""
        return _sensor.sensor_set(self._transport, temperature_c=temperature_c,
                                  pressure_pa=pressure_pa)

    def disable_i2c_sensor(self) -> None:
        """Disarm the emulated sensor (safe if none is active)."""
        _sensor.sensor_stop(self._transport)

    def i2c_sensor_status(self) -> Dict[str, Any]:
        """Sensor state and I2C-bus activity counters (transactions, reads, writes, …)."""
        return _sensor.sensor_status(self._transport)

    def i2c_sensor_regs(self, *, start: int = 0, length: int = 256) -> List[int]:
        """Read the emulated sensor's register image."""
        return _sensor.sensor_regs(self._transport, start, length)

    def i2c_sensor_capture(self, samples: int = 1024, *,
                           sample_rate_hz: Optional[float] = None) -> List[_i2c.I2CTransaction]:
        """Capture the emulated sensor's I2C bus and decode it into transactions.

        Sample fast enough to resolve the bus: ~1 MS/s (``sample_rate_hz=1e6``) gives ~10 samples
        per bit at 100 kHz I2C. For a bus on arbitrary LA channels use :meth:`capture_la` +
        :meth:`decode`.
        """
        return _i2c.decode(_sensor.sensor_la(self._transport, samples, sample_rate_hz))

    # -- analog paths ---------------------------------------------------------
    # One named path == one fully-specified switch state, defined once in the firmware, so the SDK
    # never hand-flips muxes/relays and can never disagree with the pod about how a path is wired.
    # Raw mux/relay access lives on :attr:`lowlevel`.

    def analog_path(self, path: AnalogPath) -> AnalogPathState:
        """Apply a named analog path, flipping every mux and relay it needs in one step.

        ``dac_3v3``/``dac_5v``/``dac_12v`` route the DAC to an output (``dac_12v`` is bipolar
        ±12 V); ``adc_ext`` connects the ADC to the front SMA; ``cal1``/``cal2`` loop the 5 V /
        12 V DAC output back into the ADC; ``amp`` reads the amps terminal; ``off`` parks it all.
        """
        check_choice(path, ANALOG_PATHS, "path")
        return AnalogPathState.from_reply(self.command({"cmd": "analog_path", "path": path}))

    def dac_output(self, path: DacOutputPath, *, volts: Optional[float] = None) -> DacOutput:
        """Route a DAC output path and, with ``volts``, drive that CALIBRATED DC voltage.

        ``path`` is ``3v3``/``5v``/``12v`` or ``off``. The returned :class:`DacOutput` carries
        the voltage actually produced (the nearest DAC code to the request).
        """
        check_choice(path, DAC_OUTPUT_PATHS, "path")
        req: Dict[str, Any] = {"cmd": "dac_out", "path": path}
        if volts is not None:
            if path == "off":
                raise ValueError("volts cannot be set on the 'off' path")
            req["volts"] = float(volts)
        return DacOutput.from_reply(self.command(req))

    def adc_read(self, source: AdcSource = "ext") -> AdcReading:
        """Route an ADC ``source`` and return one CALIBRATED reading (a short averaged burst).

        ``ext`` is the front SMA (the ÷12 divider is applied, so ``voltage`` is the true SMA
        voltage); ``cal1``/``cal2`` the internal DAC loopbacks; ``amp`` the amps terminal. The
        pod refuses the reading when the input is still moving (e.g. a DAC left running).
        """
        check_choice(source, ADC_SOURCES, "source")
        return AdcReading.from_reply(self.command({"cmd": "adc_read", "source": source}))

    # -- CAN (FDCAN1 / TCAN1044 transceiver) -----------------------------------
    # Single-node board: use a loopback ``mode`` to self-test on one pod. :meth:`open_can` and
    # :class:`~embeddedci.benchpod.can.CanBus` are the convenient interface; these are the
    # command-level building blocks under it.

    def can_config(self, *, bitrate: int = 500_000, mode: CanMode = "normal",
                   term: bool = False, fd: bool = False) -> Dict[str, Any]:
        """Bring FDCAN1 up. ``mode`` is ``normal``/``internal``/``external``/``listen`` (the two
        loopbacks let a lone pod self-test). ``term`` engages the 120 Ω bus termination."""
        check_choice(mode, CAN_MODES, "mode")
        return _dict(self.command({
            "cmd": "can_config", "bitrate": int(bitrate), "mode": mode,
            "term": bool(term), "fd": bool(fd),
        }))

    def can_write(self, can_id: int, data: Union[bytes, Sequence[int], None] = None,
                  *, ext: bool = False, rtr: bool = False) -> Dict[str, Any]:
        """Queue one classic frame (0..8 data bytes)."""
        payload = list(bytes(data)) if data else []
        if len(payload) > 8:
            raise ValueError(f"a classic CAN frame carries at most 8 data bytes, got {len(payload)}")
        return _dict(self.command({
            "cmd": "can_write", "id": int(can_id), "ext": bool(ext),
            "rtr": bool(rtr), "data": payload,
        }))

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
                    match_ext: bool = False, reply_ext: bool = False) -> Dict[str, Any]:
        """Install an autonomous-responder rule: when a frame with ``match_id`` arrives the
        *firmware* immediately replies with ``reply_id`` + ``reply_data`` (from the RX ISR —
        microsecond latency, no host round-trip). Turns the pod into an ECU simulator."""
        payload = list(bytes(reply_data)) if reply_data else []
        return _dict(self.command({
            "cmd": "can_respond", "match_id": int(match_id), "ext": bool(match_ext),
            "reply_id": int(reply_id), "reply_ext": bool(reply_ext),
            "reply_data": payload,
        }))

    def can_respond_clear(self) -> Dict[str, Any]:
        """Remove all autonomous-responder rules."""
        return _dict(self.command({"cmd": "can_respond", "clear": True}))

    def can_disable(self) -> Dict[str, Any]:
        """Stop FDCAN1 and release the peripheral."""
        return _dict(self.command({"cmd": "can_disable"}))

    def open_can(self, *, bitrate: int = 500_000, mode: CanMode = "normal",
                 term: bool = False, fd: bool = False) -> _can.CanBus:
        """Configure CAN and return a :class:`~embeddedci.benchpod.can.CanBus` session (clears
        responder rules and disables CAN on exit)::

            with bp.open_can(bitrate=500_000, mode="internal") as can:
                can.write(0x123, [1, 2, 3])
                frame = can.expect(can_id=0x123, timeout=1.0)
        """
        return _can.CanBus(self, bitrate=bitrate, mode=mode, term=term, fd=fd)

    # -- UART -----------------------------------------------------------------

    def capture_uart(
        self,
        *,
        rx: Union[Pin, int, str, None] = None,
        tx: Union[Pin, int, str, None] = None,
        baud: Optional[int] = None,
        duration: float,
        until: Optional[_uart.Until] = None,
    ) -> _uart.UartCapture:
        """Capture the DUT's UART output for ``duration`` seconds.

        ``rx`` is the LA channel the pod samples (wire the DUT's TX here); ``tx`` is driven (the
        DUT's RX). ``until`` (substring / compiled regex / predicate) stops early on a match.
        Omitted ``rx``, ``tx`` and ``baud`` come from the wiring profile.
        """
        if duration <= 0:
            raise ValueError(f"duration must be > 0 seconds, got {duration!r}")
        link = self._uart_link(rx, tx, baud)
        return _uart.capture(link, duration=duration, until=until)

    def _uart_link(self, rx: Any, tx: Any, baud: Optional[int]) -> Any:
        rx_i = self._wired(rx, "uart_rx")
        tx_i = self._wired(tx, "uart_tx")
        with _classified_errors():
            return self._transport.uart_proxy_start(
                rx_i, tx_i, int(self.wiring.uart_baud if baud is None else baud))

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
        return self.capture_uart(rx=rx, tx=tx, baud=baud, duration=duration, until=until)

    def open_uart(
        self,
        *,
        rx: Union[Pin, int, str, None] = None,
        tx: Union[Pin, int, str, None] = None,
        baud: Optional[int] = None,
        max_buffer: int = 1 << 20,
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
        """
        link = self._uart_link(rx, tx, baud)
        return _uart.UartSession(link, max_buffer=max_buffer)

    # -- ADC / logic-analyzer capture -----------------------------------------

    def capture_adc(self, samples: int = 4096, *, sample_rate_hz: Optional[float] = None,
                    source: Optional[AdcSource] = None, trigger: Optional[Trigger] = None,
                    trigger_timeout: float = 10.0) -> Capture:
        """Capture ADC samples and return a :class:`Capture` with CALIBRATED volts.

        Works over any transport. ``sample_rate_hz`` omitted = the device's maximum rate; the
        achieved rate is on the result. Above 32768 samples the capture streams from PSRAM, so
        multi-second captures work. ``source`` first routes that ADC source (``ext``/``cal1``/
        ``cal2``/``amp``) — omitted, the current routing is left alone. ``volts`` use the front-SMA
        calibration; for the other sources compare ``counts`` or use :meth:`adc_read`.

        ``trigger`` (a :class:`Trigger`, gateware >= v35) waits for an edge or level on an LA channel
        before sampling, so t = 0 is that moment; a :class:`~embeddedci.benchpod.errors.TriggerTimeout`
        is raised after ``trigger_timeout`` seconds without it.
        """
        label = ""
        if source is not None:
            check_choice(source, ADC_SOURCES, "source")
            self.analog_path(ADC_SOURCE_PATHS[source])  # type: ignore[arg-type]
            time.sleep(0.02)  # relays (~4 ms) + front-end RC settle, as the firmware's adc_read does
            label = source
        trig = self._resolve_trigger(trigger)
        with _classified_errors():
            return _capture.capture_adc(self._transport, self.capabilities, samples=samples,
                                        sample_rate_hz=sample_rate_hz, source=label, trigger=trig,
                                        trigger_timeout=trigger_timeout)

    def capture_la(self, samples: int = 4096, *, sample_rate_hz: Optional[float] = None,
                   stop_dac_after: Optional[float] = None, trigger: Optional[Trigger] = None,
                   trigger_timeout: float = 10.0) -> LaCapture:
        """Capture raw 14-channel logic-analyzer words and return a :class:`LaCapture`.

        ``stop_dac_after`` (seconds) cuts a concurrently-running DAC that far into the capture,
        sample-precise from the capture's hardware t0 (gateware >= v21). ``trigger`` (a
        :class:`Trigger`, gateware >= v35) makes t0 an edge or level on an LA channel —
        ``capture_la(..., trigger=Trigger("READY", "rising"))`` — and raises
        :class:`~embeddedci.benchpod.errors.TriggerTimeout` after ``trigger_timeout`` seconds without it.
        """
        trig = self._resolve_trigger(trigger)
        with _classified_errors():
            return _capture.capture_la(self._transport, samples=samples,
                                       sample_rate_hz=sample_rate_hz, stop_dac_after=stop_dac_after,
                                       trigger=trig, trigger_timeout=trigger_timeout)

    def capture_correlated(self, *, adc_samples: int = 4096,
                           adc_sample_rate_hz: Optional[float] = None,
                           la_samples: int = 4096, la_sample_rate_hz: Optional[float] = None,
                           stop_dac_after: Optional[float] = None,
                           trigger: Optional[Trigger] = None,
                           trigger_timeout: float = 10.0) -> CorrelatedCapture:
        """ADC + LA captured from ONE hardware trigger, so the two timebases align.

        Set either count to 0 for a single stream. ``stop_dac_after`` (seconds) cuts a running DAC
        that far into the capture (gateware >= v21). Pair with ``replay(..., on_capture=True)`` or
        ``generate(..., on_capture=True)`` for a phase-locked stimulus → capture → cutoff run.
        ``trigger`` starts both streams on an LA edge or level (see :meth:`capture_la`).
        Needs a streaming transport (TCP, serial or cloud).
        """
        trig = self._resolve_trigger(trigger)
        with _classified_errors():
            return _capture.capture_correlated(
                self._transport, self.capabilities, adc_samples=adc_samples,
                adc_sample_rate_hz=adc_sample_rate_hz, la_samples=la_samples,
                la_sample_rate_hz=la_sample_rate_hz, stop_dac_after=stop_dac_after,
                trigger=trig, trigger_timeout=trigger_timeout)

    def _resolve_trigger(self, trigger: Optional[Trigger]) -> Optional[Trigger]:
        if trigger is None:
            return None
        if not isinstance(trigger, Trigger):
            raise ValueError(f"trigger must be a Trigger, e.g. Trigger(9, 'rising'), got {trigger!r}")
        self._require_capability("capture_trigger", "capture triggers")
        if isinstance(trigger.la, str):
            return Trigger(self._resolve_la(trigger.la, "trigger")[0], trigger.edge)
        return trigger

    def decode(self, source: Union[LaCapture, Sequence[int]], protocol: DecodeProtocol = "i2c", *,
               sample_rate_hz: Optional[float] = None, **channels: Any) -> list:
        """Decode ``i2c``/``uart``/``spi`` from an LA capture (or raw 14-bit words) off-device.

        Channels by protocol: i2c ``sda``, ``scl``; uart ``rx``, ``baud``; spi ``sclk`` (+ ``mosi``,
        ``miso``, ``cs``, ``mode``). See :func:`embeddedci.benchpod.decode.decode`.
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

    # -- DAC generate / stop ----------------------------------------------------

    def generate(self, waveform: Waveshape = "sine", *, freq_hz: float, amplitude: float,
                 offset: Optional[float] = None, dac_path: DacPath = "5v",
                 duration: Optional[float] = None, sample_rate_hz: Optional[float] = None,
                 on_capture: bool = False, route: bool = True) -> DacHandle:
        """Generate a parametric waveform (``sine``/``square``/``sawtooth``) on a DAC output path.

        ``amplitude`` is the peak in volts and ``offset`` the centre in volts (default: the middle
        of ``dac_path``'s range — 0 V on the bipolar ±12 V ``12v`` path). The firmware builds the
        waveform from 8-bit levels, so volts are
        quantised to ``full_scale / 255`` using the same volts→code mapping as :meth:`replay`.
        ``duration`` (seconds) stops it by itself; omitted, it runs until :meth:`dac_stop` or the
        returned handle's ``stop()``. ``on_capture=True`` defers the start to the next capture's
        hardware t0 (phase-locked co-trigger, gateware >= v27); ``handle.cotrig`` reports whether
        it armed.

        By default the output path is routed first. ``route=False`` keeps the current analog
        switching — e.g. after ``analog_path("cal1")`` for a DAC→ADC loopback, which routing the
        output would undo — and ``dac_path`` then only sets the volts scaling.
        """
        check_choice(waveform, WAVESHAPES, "waveform")
        check_choice(dac_path, DAC_PATHS, "dac_path")
        if freq_hz <= 0:
            raise ValueError(f"freq_hz must be > 0, got {freq_hz!r}")
        vmin, vmax = _dsp.dac_path_range_v(dac_path)
        span = vmax - vmin
        step = span / _GENERATOR_MAX_CODE
        amp_code = int(round(float(amplitude) / step))
        if amplitude <= 0 or amp_code < 1:
            raise ValueError(f"amplitude must be at least {step:.4f} V on the {dac_path} path, "
                             f"got {amplitude!r}")
        if amp_code > _GENERATOR_MAX_CODE:
            raise ValueError(f"amplitude must be at most {span:g} V on the {dac_path} path, got {amplitude!r}")
        off_v = (vmin + vmax) / 2.0 if offset is None else float(offset)
        off_code = int(round((off_v - vmin) / step))
        if not 0 <= off_code <= _GENERATOR_MAX_CODE:
            raise ValueError(f"offset must be within {vmin:g}..{vmax:g} V on the {dac_path} path, "
                             f"got {offset!r}")
        req: Dict[str, Any] = {"cmd": "generate", "waveform": waveform, "freq": float(freq_hz),
                               "amplitude": amp_code, "offset": off_code}
        if duration is not None:
            ms = _seconds_to_ms(duration, "duration")
            if ms <= 0:
                raise ValueError("duration must be at least 1 ms; omit it to run until stopped")
            req["duration_ms"] = ms
        mhz = _capture.rate_mhz(sample_rate_hz)
        if mhz is not None:
            req["sample_rate_mhz"] = mhz
        if on_capture:
            req["on_capture"] = True
        if route:
            self.command({"cmd": "dac_out", "path": dac_path})
        d = _dict(self.command(req))
        return DacHandle(stop=self.dac_stop, dac_path=dac_path, cotrig=bool(d.get("cotrig", False)),
                         data=d)

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
            i=int(d.get("i", 0)), v=int(d.get("v", 0)),
            input_code=None if raw_in is None else int(raw_in),
            source=d.get("source"),
            tripped=None if d.get("tripped") is None else bool(d.get("tripped")))

    # -- gateware image -------------------------------------------------------

    def fpga_image(self, image: Union[FpgaImage, int]) -> FpgaImageInfo:
        """Switch the iCE40 to another gateware image stored on the pod.

        ``FpgaImage.LOOP`` (0) is the control-loop image, ``FpgaImage.DEEP_REPLAY`` (1) the deep
        DAC replay image. The pod reprograms the FPGA from its config flash and cold-resets it
        (~2-3 s), which stops anything running in the FPGA: a DAC output or control loop, a UART
        session, I2C sensor emulation. The selection is written to the config flash, so the pod
        normally stays on it after a power cycle. Drops the cached :attr:`capabilities` so the next
        read reflects the new image.

        You rarely need to call this: :meth:`control_loop`, :meth:`replay` and
        :meth:`replay_waveform` switch automatically when they need the other image
        (``switch_image=True``).
        """
        img = int(image)
        if img not in tuple(FpgaImage):
            raise ValueError(f"image must be FpgaImage.LOOP (0) or FpgaImage.DEEP_REPLAY (1), got {image!r}")
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
        if getattr(caps, flag) or not _images_switchable(caps):
            return None
        if not switch:
            raise BenchPodError(
                f"{needed_for} needs the {image.name} gateware image, but the pod is running the "
                f"other one (capabilities.{flag} is False): pass switch_image=True or call "
                f"fpga_image(FpgaImage.{image.name}) first")
        _log.warning("switching the FPGA to the %s gateware image for %s (~3 s); this resets the "
                     "FPGA, so any DAC output, UART session or I2C sensor emulation stops",
                     image.name, needed_for)
        info = self.fpga_image(image)
        # Judge by the reply's feature bits: on a cloud connection the merged capabilities can lag.
        if not info.features & _IMAGE_FEATURE[image]:
            raise BenchPodError(
                f"switched the FPGA to the {image.name} image for {needed_for}, but the image that "
                f"booted does not carry it (features=0x{info.features:02x})")
        self._wait_for_server_caps(flag)
        return info

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
            if not _images_switchable(caps):
                raise BenchPodError(
                    f"a {n_samples}-sample replay streams from PSRAM, which this pod's gateware "
                    "cannot play (capabilities.dac_deep_replay is False): replay at most 2048 samples")
            switched = self._ensure_image(
                FpgaImage.DEEP_REPLAY, switch=switch_image,
                needed_for=f"a {n_samples}-sample replay (it streams from PSRAM)")
        if dac_path and route:
            self.command({"cmd": "dac_out", "path": dac_path})
        replay_req: Dict[str, Any] = {"cmd": "replay", "samples": n_samples}
        mhz = _capture.rate_mhz(sample_rate_hz)
        if mhz is not None:
            replay_req["sample_rate_mhz"] = mhz
        if on_capture:
            replay_req["on_capture"] = True
        d = _dict(fn(data=code_bytes, replay=replay_req, psram=deep))
        return ReplayHandle(stop=self.dac_stop, samples=n_samples,
                            sample_rate_hz=float(sample_rate_hz or 0.0), dac_path=dac_path,
                            deep=deep, data=d, cotrig=bool(d.get("cotrig", False)),
                            switched_image=switched)

    def replay(self, source: Union[Capture, Sequence[float], Sequence[int]], *,
               dac_path: DacPath = "5v", mapping: ReplayMapping = "faithful",
               sample_rate_hz: Optional[float] = None, deep: Optional[bool] = None,
               fault: Union[Fault, Dict[str, Any], None] = None,
               are_codes: bool = False, on_capture: bool = False,
               route: bool = True, switch_image: bool = True) -> ReplayHandle:
        """Replay a waveform on the DAC, streaming it to the device over the transport.

        ``route=False`` keeps the current analog switching (see :meth:`generate`).

        ``source`` is a :class:`Capture` (its volts are replayed), a sequence of volts, or — with
        ``are_codes=True`` — raw DAC codes. Volts map to codes for ``dac_path`` (``faithful``
        reproduces the voltage, clipping outside range; ``fit`` auto-scales). ``fault`` splices a
        :class:`Fault` in. More than 2048 samples stream from PSRAM, which needs the deep-replay
        gateware image: with ``switch_image`` (the default) the pod is switched to it first (~3 s,
        recorded as the handle's ``switched_image``); ``switch_image=False`` raises instead.
        The replay LOOPS until the returned :class:`ReplayHandle` is stopped,
        so it can run concurrently with a capture (gateware v18)::

            with bp.replay(cap, dac_path="5v"):
                la = bp.capture_la(8192, sample_rate_hz=1e6)

        ``on_capture=True`` arms the DAC to start on the NEXT capture's hardware t0 (gateware
        >= v27, :attr:`Capabilities.dac_cotrig`).
        """
        check_choice(dac_path, DAC_PATHS, "dac_path")
        check_choice(mapping, REPLAY_MAPPINGS, "mapping")
        bits = self._replay_bits()
        vmin, vmax = _dsp.dac_path_range_v(dac_path)
        if isinstance(source, Capture):
            codes = _dsp.volts_to_codes(source.volts, mapping, vmax, bits=bits, path_min_v=vmin)
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
        ``dac_path`` defaults to the path stored with the waveform (else ``5v``).
        ``window_start``/``window_len`` select a slice of a recording; ``target_samples``
        downsamples a shallow replay. By default this uses the server's replay DSP when the device
        is cloud-connected; otherwise it fetches the recording and replays it over the device
        connection with the SAME DSP applied client-side. Needs server access (the cloud session
        or an ``api_key``).

        With ``switch_image`` (the default) a replay that needs the deep-replay gateware image
        switches the pod to it first: client-side, one deeper than 2048 samples; server-side, a
        recording longer than the shallow replay depth when no ``target_samples`` asks for a
        downsampled one (``switch_image=False`` then leaves the server to downsample).
        """
        check_choice(mapping, REPLAY_MAPPINGS, "mapping")
        wid = waveform.id if hasattr(waveform, "id") else waveform
        if not wid:
            raise ValueError("replay_waveform needs a waveform id")
        lib = self.waveforms
        wf = lib.get(wid)  # type: ignore[arg-type]
        if dac_path is None:
            dac_path = wf.dac_path if wf.dac_path in DAC_PATHS else "5v"  # type: ignore[assignment]
        check_choice(dac_path, DAC_PATHS, "dac_path")  # type: ignore[arg-type]

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
            data = api.replay_start(payload)
            # The server replay is async: the armed/playing outcome arrives as a later dac.event
            # frame, so infer cotrig from the request + the device capability.
            return ReplayHandle(stop=self.dac_stop, samples=int(data.get("samples", 0) or 0),
                                sample_rate_hz=float(sample_rate_hz or 0.0), dac_path=dac_path,
                                deep=bool(self.capabilities.dac_deep_replay), data=data,
                                cotrig=bool(on_capture and self.capabilities.dac_cotrig),
                                switched_image=switched)

        # Client-side: fetch + DSP + stream over the transport.
        bits = self._replay_bits()
        if wf.is_recording:
            raw = lib.download_recording(wid)  # type: ignore[arg-type]
            rc = _dsp.recording_to_replay_codes(
                raw, src_full_scale_v=wf.full_scale_v or _dsp.dac_path_fullscale_v(dac_path),
                dac_path=dac_path, mapping=mapping, window_start=window_start,
                window_len=window_len, target_samples=target_samples, bits=bits,
                deep=bool(deep) if deep is not None else (wf.sample_count > 2048),
                max_samples=self.capabilities.dac_replay_max_samples or _dsp.REPLAY_MAX_SAMPLES,
                fault=normalize_fault(fault))
            code_bytes = rc.to_bytes()
        elif wf.segments:
            volts = _dsp.segments_to_volts(wf.segments, wf.sample_rate_hz or 1.0)
            seg_min, seg_max = _dsp.dac_path_range_v(dac_path)  # type: ignore[arg-type]
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
        """Save an ADC :class:`Capture` to the cloud library as a replayable ``recording``."""
        volts = capture.volts
        fs = full_scale_v
        if fs is None:
            peak = max((abs(v) for v in volts), default=1.0)
            fs = peak if peak > 0 else 1.0
        blob = _dsp.encode_recording_volts(volts, fs)
        return self.waveforms.save_recording(name, blob, sample_rate_hz=capture.sample_rate_hz or 0.0,
                                             full_scale_v=fs)

    # -- cloud waveform library + server API ----------------------------------

    def _try_server_api(self) -> Optional["ServerApi"]:
        """Build a :class:`ServerApi` if possible (API key or a cloud session token), else None."""
        if self._server_api is not None:
            return self._server_api
        token_provider = getattr(self._transport, "_session_token", None)
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

    def la_pins(self) -> List[LaPinState]:
        """Every LA channel's function (``none``, ``gpio``, ``uart_rx``, …), GPIO mode and level, and
        bias resistor. A channel that is in use refuses other functions."""
        self._require_capability("la_pins", "LA pin modes and GPIO")
        with _classified_errors():
            data = _dict(self.command({"cmd": "la_pins"}))
        return [LaPinState.from_reply(e) for e in data.get("pins") or []]

    def gpio(self, la: Union[Pin, int, str], mode: GpioMode = "output", *,
             level: Optional[int] = None) -> GpioPin:
        """Use an LA channel as GPIO and return its :class:`~embeddedci.benchpod.gpio.GpioPin`.

        ``la`` is 1-14, a ``Pin`` or a wiring-profile name. ``mode``: ``output`` (push-pull, starts at
        ``level``, default 0), ``open_drain`` (0 pulls low, 1 releases; starts released) or ``input``.
        Raises :class:`~embeddedci.benchpod.errors.PinConflictError` when another function owns the
        channel, and :class:`~embeddedci.benchpod.errors.PullConflictError` when its engaged bias
        resistor can't work with ``mode`` (an open-drain output over LA7/LA8's pull-down). The channel
        stays GPIO — across disconnects — until :meth:`release_gpio`.

        Use :meth:`gpio_pins` for several channels: it claims them as a group, so a conflict on one
        leaves the others untouched.
        """
        la_i, signal = self._resolve_la(la)
        pin = GpioPin(self, la_i, signal=signal)
        pin.configure(mode, level=level)
        return pin

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
        """
        resolved = [self._resolve_la(x) for x in (list(las) if isinstance(las, (list, tuple)) else [las])]
        if not resolved:
            raise ValueError("give at least one LA channel")
        self._gpio_configure([la for la, _ in resolved], mode, level)
        return [GpioPin(self, la, signal=signal) for la, signal in resolved]

    def set_gpio(self, la: Union[Pin, int, str, Sequence[Union[Pin, int, str]]], level: int) -> None:
        """Set the level of a GPIO output or open-drain channel — or of several (a list) at once."""
        las = self._resolve_las(la)
        _check_level(level)
        self._require_capability("la_pins", "GPIO")
        with _classified_errors():
            self.command({"cmd": "gpio", "la": _one_or_many(las), "level": int(level)})

    def read_gpio(self, la: Union[Pin, int, str]) -> int:
        """The live level (0/1) of an LA channel, whatever its function."""
        return self.pin_levels()[self._resolve_la(la)[0]]

    def pin_levels(self) -> Dict[int, int]:
        """The live level of every LA channel: ``{1: 0, 2: 1, …, 14: 0}``.

        Read straight from the pins (gateware >= v35); older gateware answers with the last sample of
        a short logic capture instead.
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
        """Wait until an LA channel reads ``level``; ``False`` if ``timeout`` seconds pass first.

        Polled from the host, so it resolves a few milliseconds plus the round trip — for precise
        timing capture with a :class:`Trigger` and use the capture's timing helpers.
        """
        _check_level(level)
        if timeout < 0 or poll <= 0:
            raise ValueError("timeout must be >= 0 and poll > 0 seconds")
        la_i = self._resolve_la(la)[0]
        deadline = time.monotonic() + timeout
        while True:
            if self.pin_levels()[la_i] == level:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)

    def release_gpio(self, *las: Union[Pin, int, str]) -> None:
        """Stop using channels as GPIO — they go back to high-Z ("LA mode"). Without arguments every
        GPIO channel is released; channels owned by other functions are left alone."""
        self._require_capability("la_pins", "GPIO")
        target: Any = "all" if not las else _one_or_many(self._resolve_las(list(las)))
        with _classified_errors():
            self.command({"cmd": "gpio", "la": target, "mode": "off"})

    def configure_gpio(self, las: Sequence[Union[Pin, int, str]], mode: GpioMode = "output", *,
                       level: Optional[int] = None) -> List[LaPinState]:
        """Claim LA channels as GPIO in one command and return the pod's
        :class:`~embeddedci.benchpod.gpio.LaPinState` for each — the low-level form of
        :meth:`gpio_pins`, for when you want what the pod reports rather than objects to drive."""
        return self._gpio_configure(self._resolve_las(las), mode, level)

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
        pins = [LaPinState.from_reply(e) for e in data.get("pins") or []]
        return pins or [LaPinState(la=la, function="gpio", gpio=mode, level=level) for la in las]

    def _resolve_las(self, la: Any) -> List[int]:
        items = list(la) if isinstance(la, (list, tuple)) else [la]
        if not items:
            raise ValueError("give at least one LA channel")
        return [self._resolve_la(x)[0] for x in items]

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
        return {"cmd": "power_profile", "efuse": rail, "rate_hz": int(round(rate_hz)),
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
        """Emit ``steps`` step pulses on an LA channel, ``delay`` seconds apart (step/dir drivers).

        The FPGA runs the train autonomously and this returns immediately. With ``dir_la`` set,
        that channel is driven to ``direction`` (0/1) first.
        """
        if steps <= 0:
            raise ValueError(f"steps must be > 0, got {steps!r}")
        delay_us = int(round(delay * 1e6))
        if delay_us < 1:
            raise ValueError(f"delay must be at least 1 µs, got {delay!r}")
        if direction not in (0, 1):
            raise ValueError(f"direction must be 0 or 1, got {direction!r}")
        req: Dict[str, Any] = {"cmd": "la", "la": self._resolve_la(la)[0], "steps": int(steps),
                               "delay_us": delay_us}
        if dir_la is not None:
            req["dir_la"] = self._resolve_la(dir_la, "dir_la")[0]
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
