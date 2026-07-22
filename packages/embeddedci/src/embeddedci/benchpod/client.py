"""The :class:`BenchPod` facade — the user-facing API.

Wraps a :class:`~embeddedci.benchpod.transport.base.Transport` with intuitive,
named operations: connect, power on/off, flash (and assert ok/not ok), plus a
stubbed I2C sensor hook. Designed to drop straight into pytest::

    from embeddedci import benchpod

    with benchpod.BenchPod("192.168.1.213") as bp:
        bp.power_on(benchpod.INTERNAL)
        assert bp.flash(file="fw.elf", target="target/stm32f1x.cfg",
                        swclk=benchpod.PIN1, swdio=benchpod.PIN2).ok
"""

from __future__ import annotations

import os
import time
from typing import Any, List, Optional, Sequence, Union

from . import can as _can
from . import capture as _capture
from . import control_loop as _control_loop
from . import dsp as _dsp
from . import flash as _flash
from . import i2c as _i2c
from . import sensor as _sensor
from . import uart as _uart
from .capabilities import Capabilities
from .constants import Efuse, Pin, Sensor, coerce_efuse, coerce_pin
from .connection import resolve_connection
from .errors import BenchPodError
from .flash import FlashResult
from .lease import DEFAULT_LEASE_TTL, DEFAULT_LEASE_WAIT, DeviceLease
from .replay import Fault, ReplayHandle, normalize_fault
from .results import AnalogCapture, Capture, LaCapture
from .transport import Transport, open_transport
from .transport.cloud import CloudTransport


class BenchPod:
    """A connected BenchPod device."""

    def __init__(
        self,
        connection: Optional[str] = None,
        *,
        timeout: float = 30.0,
        transport: Optional[Transport] = None,
        api_base: Optional[str] = None,
        cloud_token: Optional[str] = None,
        cloud_audience: Optional[str] = None,
        api_key: Optional[str] = None,
        lease: bool = True,
        lease_wait: float = DEFAULT_LEASE_WAIT,
        lease_ttl: int = DEFAULT_LEASE_TTL,
    ) -> None:
        """Open a BenchPod.

        ``connection`` is a host[:port], a serial device path, ``"serial"``, or
        ``"embeddedci:<device-name>"`` to drive a named device through embeddedci.com; when
        omitted the ``BENCHPOD_CONNECTION`` environment variable is used. ``api_base`` /
        ``cloud_token`` / ``cloud_audience`` apply only to the ``embeddedci`` destination.
        Pass ``transport`` directly to inject a custom/standalone backend.

        For the ``embeddedci`` (cloud) destination the device is *shared*, so by default the client
        takes an exclusive **lease** on it for the life of this BenchPod — a concurrent run that
        finds it busy waits up to ``lease_wait`` seconds for it to free (raising ``DeviceBusyError``
        on timeout). Set ``lease=False`` to skip locking. Local TCP/serial connections never lease.
        """
        self.timeout = timeout
        self._lease: Optional[DeviceLease] = None
        # Server-orchestration access (waveform library, server-side replay). Over the cloud
        # destination these reuse the transport's session token (including a GitHub OIDC token);
        # on LAN/serial there is none, so an explicit API key reaches the shared library.
        self._api_base = api_base or os.environ.get("BENCHPOD_API_BASE")
        self._api_key = api_key or os.environ.get("BENCHPOD_API_KEY")
        self._caps: Optional[Capabilities] = None
        self._server_api = None  # lazily built ServerApi
        self._waveforms = None   # lazily built WaveformLibrary
        if transport is not None:
            self._transport: Transport = transport
        else:
            spec = resolve_connection(connection)
            self._transport = open_transport(
                spec,
                timeout=timeout,
                api_base=api_base,
                token=cloud_token,
                audience=cloud_audience,
            )
        if lease and isinstance(self._transport, CloudTransport):
            self._lease = DeviceLease(
                api_base=self._transport.api_base,
                token_provider=self._transport._session_token,
                device_name=self._transport.device_name,
                ttl_seconds=lease_ttl,
            )
            try:
                self._lease.acquire(wait_timeout=lease_wait)
            except BaseException:
                self._lease = None
                self._transport.close()
                raise
            self._transport.lease_id = self._lease.lease_id
        # Remember the cloud device name + base so the server-orchestration client can address
        # the same device (for server-side replay) and reuse the transport's session token.
        self._device_name = getattr(self._transport, "device_name", "") or ""
        if not self._api_base:
            self._api_base = getattr(self._transport, "api_base", None)

    # -- lifecycle ----------------------------------------------------------

    @property
    def transport(self) -> Transport:
        return self._transport

    def close(self) -> None:
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

    # -- basic status -------------------------------------------------------

    def ping(self) -> Any:
        """Confirm the pod is reachable."""
        return self._transport.ping()

    def status(self) -> Any:
        """Return firmware/connection status."""
        return self._transport.status()

    # -- LA I/O-bank voltage ------------------------------------------------

    def set_la_voltage(self, voltage: Union[int, float]) -> Any:
        """Select the LA I/O-bank voltage on the pod's TPS2116 mux.

        Accepts volts (``1.8`` / ``3.3``) or millivolts (``1800`` / ``3300``).
        This MUST be set before any LA-bank operation — flashing/SWD, the UART
        proxy, LA capture, pull-ups, or I2C-sensor emulation — otherwise the pod
        refuses those commands with "la voltage not set". Returns the pod's
        ``{"mv", "st"}`` state.
        """
        v = float(voltage)
        mv = int(round(v * 1000)) if v < 100 else int(round(v))
        if mv not in (1800, 3300):
            raise ValueError(
                f"la voltage must be 1.8/3.3 V (or 1800/3300 mV), got {voltage!r}"
            )
        return self._transport.set_la_voltage(mv)

    def get_la_voltage(self) -> Any:
        """Report the LA I/O-bank voltage state (``{"mv", "st"}``); ``mv`` is 0
        when a voltage has not been selected yet."""
        return self._transport.get_la_voltage()

    # -- power --------------------------------------------------------------

    def target_power(self, efuse: Union[Efuse, int] = Efuse.INTERNAL, *,
                     on: bool, delay: Optional[float] = None) -> None:
        """Enable or disable a target-power eFuse.

        ``delay`` (seconds) schedules the change pod-side and returns
        immediately — handy to power-on *during* a UART capture.
        """
        delay_ms = int(round(delay * 1000)) if delay else 0
        self._transport.target_power(coerce_efuse(efuse), on, delay_ms)

    def power_on(self, efuse: Union[Efuse, int] = Efuse.INTERNAL,
                 *, delay: Optional[float] = None) -> None:
        """Power the target on via the given eFuse (default INTERNAL 5V)."""
        self.target_power(efuse, on=True, delay=delay)

    def power_off(self, efuse: Union[Efuse, int] = Efuse.INTERNAL,
                  *, delay: Optional[float] = None) -> None:
        """Power the target off."""
        self.target_power(efuse, on=False, delay=delay)

    # -- flash --------------------------------------------------------------

    def flash(
        self,
        *,
        swclk: Union[Pin, int],
        swdio: Union[Pin, int],
        nreset: Optional[Union[Pin, int]] = None,
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
        """Flash an SWD target and report the result.

        ``swclk``/``swdio``/``nreset`` are LA pins (``benchpod.PIN1``..``PIN12``
        or 1-12). ``target_power`` of ``benchpod.INTERNAL``/``EXTERNAL`` powers
        the target first. By default (``check=True``) a failed flash raises
        :class:`FlashError`/:class:`TargetUnreachableError`; pass ``check=False``
        to get the :class:`FlashResult` and ``assert result.ok`` yourself.
        """
        swclk_i = coerce_pin(swclk, "swclk")
        swdio_i = coerce_pin(swdio, "swdio")
        if swclk_i == swdio_i:
            raise BenchPodError("swclk and swdio must be different LA pins")
        nreset_i = coerce_pin(nreset, "nreset") if nreset is not None else None
        power = coerce_efuse(target_power) if target_power is not None else None

        result = _flash.flash(
            self._transport,
            swclk=swclk_i, swdio=swdio_i, nreset=nreset_i,
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

    # -- LA pin pull-ups (LA1-8) --------------------------------------------

    def pullup(self, la: Union[Pin, int], on: Optional[bool] = None) -> dict:
        """Switch (or query) an LA pin's pull-up resistor. LA1-8 only.

        The pod has fixed pull-ups (LA1/2 = 4.7k, LA3/4 = 2.2k, LA5-8 = 10k) —
        enable them on the I2C SDA/SCL lines so an open-drain bus idles high.
        ``on=None`` queries without changing. Returns e.g.
        ``{"la":1,"pullup":1,"ohms":"4.7k"}``.
        """
        la_i = coerce_pin(la, "la")
        if not 1 <= la_i <= 8:
            raise BenchPodError("pull-ups are only on LA1-8")
        req: dict = {"cmd": "la", "la": la_i}
        if on is not None:
            req["pullup"] = "on" if on else "off"
        return self.command(req)

    def enable_pullup(self, *las: Union[Pin, int]) -> None:
        """Enable the pull-up on one or more LA pins (LA1-8)."""
        for la in las:
            self.pullup(la, on=True)

    def disable_pullup(self, *las: Union[Pin, int]) -> None:
        """Disable the pull-up on one or more LA pins (LA1-8)."""
        for la in las:
            self.pullup(la, on=False)

    def pullup_status(self) -> dict:
        """Return ``{"la_pullup_mask": <bitmask>}`` (bit la-1 set = pull-up on)."""
        return self.command({"cmd": "la"})

    # -- emulated I2C sensor (TCP transport only) ---------------------------

    def enable_i2c_sensor(
        self,
        sensor: Union[Sensor, str] = Sensor.BMP280,
        *,
        sda: Union[Pin, int],
        scl: Union[Pin, int],
        address: int = 0x76,
        temperature_c: Optional[float] = None,
        pressure_pa: Optional[float] = None,
    ) -> dict:
        """Make the pod emulate an I2C sensor (e.g. BMP280) on ``sda``/``scl``.

        The pod becomes an I2C slave the DUT's master can read. Optionally seed
        initial ``temperature_c``/``pressure_pa``. Returns the start response.
        """
        result = _sensor.sensor_start(
            self._transport, sensor, sda=sda, scl=scl, address=address
        )
        if temperature_c is not None or pressure_pa is not None:
            _sensor.sensor_set(self._transport, temperature_c=temperature_c,
                               pressure_pa=pressure_pa)
        return result

    def set_i2c_sensor(self, *, temperature_c: Optional[float] = None,
                       pressure_pa: Optional[float] = None) -> dict:
        """Update the emulated sensor's reported values."""
        return _sensor.sensor_set(self._transport, temperature_c=temperature_c,
                                  pressure_pa=pressure_pa)

    def disable_i2c_sensor(self) -> None:
        """Disarm the emulated sensor (safe if none is active)."""
        _sensor.sensor_stop(self._transport)

    def i2c_sensor_status(self) -> dict:
        """Return sensor + I2C-bus activity counters."""
        return _sensor.sensor_status(self._transport)

    def i2c_sensor_regs(self, start: int = 0, length: int = 256) -> List[int]:
        """Read the emulated register image."""
        return _sensor.sensor_regs(self._transport, start, length)

    def i2c_sensor_la(self, samples: int = 256,
                      sample_rate_mhz: Optional[float] = None) -> List[int]:
        """Raw I2C-bus logic capture (packed bytes)."""
        return _sensor.sensor_la(self._transport, samples, sample_rate_mhz)

    def i2c_sensor_la_decoded(self, samples: int = 1024,
                              sample_rate_mhz: Optional[float] = None
                              ) -> "List[_i2c.I2CTransaction]":
        """Capture the I2C bus and decode it into transactions.

        Convenience over :meth:`i2c_sensor_la` + :func:`benchpod.i2c.decode`.
        Sample fast enough to resolve the bus: ~1 MS/s (the default) gives ~10
        samples per bit at 100 kHz I2C over a ~4 ms window per 1024 bytes.
        """
        raw = self.i2c_sensor_la(samples, sample_rate_mhz)
        return _i2c.decode(raw)

    # -- analog path switching (v2: DAC mux U55 + calibration relays U58) ----

    #: DAC output paths for :meth:`dac_mux` ``ctrl1`` (U47 TMUX): which SMA/ADC
    #: path the buffered DAC drives.
    _DAC_RAILS = {"3v3": 0, "5v": 1, "12v": 2, "12v_adc": 3}
    #: Buffer VMID reference for :meth:`dac_mux` ``ctrl2`` (U48 TMUX).
    _DAC_VMIDS = {"12v_vmid": 0, "adc_vmid": 1, "gnd": 2}

    def dac_mux(self, *, ctrl1: Optional[str] = None,
                ctrl2: Optional[str] = None) -> dict:
        """Route the buffered DAC through the analog muxes (U55 → U47/U48).

        ``ctrl1`` selects which output path the DAC drives:
        ``'3v3'`` / ``'5v'`` / ``'12v'`` / ``'12v_adc'``, ``'off'`` to disable,
        or ``None`` to leave it unchanged. ``ctrl2`` selects the buffer VMID
        reference: ``'12v_vmid'`` / ``'adc_vmid'`` / ``'gnd'`` / ``'off'`` / ``None``.
        Returns the pod's decoded mux state.
        """
        req: dict = {"cmd": "dac_mux"}
        if ctrl1 is not None:
            if ctrl1 == "off":
                req["ctrl1_en"] = 0
            elif ctrl1 in self._DAC_RAILS:
                req["ctrl1_en"] = 1
                req["ctrl1_sel"] = self._DAC_RAILS[ctrl1]
            else:
                raise ValueError(f"ctrl1 must be one of {list(self._DAC_RAILS)}, 'off', or None")
        if ctrl2 is not None:
            if ctrl2 == "off":
                req["ctrl2_en"] = 0
            elif ctrl2 in self._DAC_VMIDS:
                req["ctrl2_en"] = 1
                req["ctrl2_sel"] = self._DAC_VMIDS[ctrl2]
            else:
                raise ValueError(f"ctrl2 must be one of {list(self._DAC_VMIDS)}, 'off', or None")
        return self.command(req)

    def dac_mux_status(self) -> dict:
        """Report the current DAC-mux (U55) state without changing it."""
        return self.command({"cmd": "dac_mux"})

    def cal_switch(self, *, cal1: bool = False, cal2: bool = False,
                   amp_measure: bool = False, cal_path: bool = False) -> dict:
        """Set the calibration relays (U58 → U53). ``cal1``/``cal2`` route the
        5V/12V DAC path to the ADC (mutually exclusive); ``amp_measure`` switches
        the ADC to the amps screw-terminal; ``cal_path`` switches the ADC from
        the front SMA to the calibration path. Returns the decoded relay state.
        """
        if cal1 and cal2:
            raise ValueError("cal1 and cal2 are mutually exclusive")
        return self.command({
            "cmd": "cal_switch",
            "cal1": int(cal1), "cal2": int(cal2),
            "amp_measure": int(amp_measure), "cal_path": int(cal_path),
        })

    def cal_switch_status(self) -> dict:
        """Report the current calibration-relay (U58) state without changing it."""
        return self.command({"cmd": "cal_switch"})

    # -- high-level analog paths (RECOMMENDED) ------------------------------
    # One named path == one fully-specified switch state, defined once in the
    # firmware (analog_path_set), so the SDK never hand-flips muxes/relays and
    # can never disagree with the pod about how a path is wired.

    #: Named analog paths accepted by :meth:`analog_path` / :meth:`dac_output` /
    #: :meth:`adc_read`.  Output paths: 3v3/5v/12v (12v is bipolar ±12V, diff).
    #: ADC sources: ext (front SMA, high-Z ÷12), cal1/cal2 (internal loopbacks),
    #: amp (amps screw terminal).
    ANALOG_PATHS = ("off", "dac_3v3", "dac_5v", "dac_12v",
                    "adc_ext", "cal1", "cal2", "amp")

    def analog_path(self, path: str) -> dict:
        """Apply a named analog path — flips every mux/relay the path needs in
        one step. ``path`` is one of :attr:`ANALOG_PATHS` (aliases ``3v3``/``5v``/
        ``12v`` = ``dac_*``, ``ext``/``sma`` = ``adc_ext``). Returns the pod's
        resulting register state ``{"path", "u55", "u58"}``."""
        return self.command({"cmd": "analog_path", "path": path})

    def dac_output(self, path: str, volts: Optional[float] = None) -> dict:
        """Route a DAC **output** path (``'3v3'``/``'5v'``/``'12v'``/``'off'``)
        and, if ``volts`` is given, set that CALIBRATED voltage — switches flip
        automatically. Returns ``{"path", "mv", "code"}`` (achieved millivolts and
        the DAC code; ``code`` is ``-1`` when only routing)."""
        req: dict = {"cmd": "dac_out", "path": path}
        if volts is not None:
            req["volts"] = float(volts)
        return self.command(req)

    def adc_read(self, source: str = "ext") -> dict:
        """Route an ADC ``source`` (``'ext'`` front SMA / ``'cal1'`` / ``'cal2'`` /
        ``'amp'``) and return a CALIBRATED reading ``{"source", "mv", "count"}``.
        ``'ext'`` applies the front-SMA ÷12 divider, so ``mv`` is the true voltage
        at the ADC input SMA."""
        return self.command({"cmd": "adc_read", "source": source})

    def measure_volts(self, source: str = "ext") -> float:
        """Convenience: :meth:`adc_read` returning just the calibrated volts."""
        return self.adc_read(source)["mv"] / 1000.0

    def route_dac_to_adc(self, rail: str = "5v") -> dict:
        """Convenience: loop the DAC back into the ADC through the calibration
        path for a quick self-cal. ``rail='5v'`` = CAL1 loop, ``'12v'`` = CAL2
        loop. Thin wrapper over :meth:`analog_path` (single source of truth)."""
        rail = rail.lower()
        if rail == "5v":
            return self.analog_path("cal1")
        if rail == "12v":
            return self.analog_path("cal2")
        raise ValueError("rail must be '5v' or '12v'")

    def adc_from_sma(self) -> dict:
        """Return the ADC to the front-panel SMA input (external, high-Z ÷12)."""
        return self.analog_path("adc_ext")

    # -- CAN (v2: FDCAN1 / TCAN1044 transceiver) ----------------------------
    # Single-node board: use a loopback ``mode`` to self-test on one pod. See
    # :mod:`embeddedci.benchpod.can`.

    def can_config(self, *, bitrate: int = 500_000, mode: str = "normal",
                   term: bool = False, fd: bool = False) -> dict:
        """Bring FDCAN1 up. ``mode`` is ``normal``/``internal``/``external``/
        ``listen`` (the two loopback modes let a lone pod self-test). ``term``
        engages the 120 Ω bus termination. Returns ``{bitrate, mode, term}``."""
        return self.command({
            "cmd": "can_config", "bitrate": int(bitrate), "mode": mode,
            "term": bool(term), "fd": bool(fd),
        })

    def can_write(self, can_id: int, data: Union[bytes, Sequence[int], None] = None,
                  *, ext: bool = False, rtr: bool = False) -> dict:
        """Queue one classic frame (0..8 data bytes)."""
        payload = list(bytes(data)) if data else []
        return self.command({
            "cmd": "can_write", "id": int(can_id), "ext": bool(ext),
            "rtr": bool(rtr), "data": payload,
        })

    def can_read(self, *, max: int = 8) -> dict:
        """Drain up to ``max`` received frames. Returns
        ``{"frames": [...], "overflow": N}`` (raw dict; see :meth:`open_can`
        / :class:`~embeddedci.benchpod.can.CanBus` for decoded frames)."""
        return self.command({"cmd": "can_read", "max": int(max)})

    def can_status(self) -> dict:
        """Return the CAN link state (mode, bitrate, error counters, bus-off…)."""
        return self.command({"cmd": "can_status"})

    def can_term(self, on: bool) -> dict:
        """Switch the 120 Ω bus termination in/out (works even with CAN off)."""
        return self.command({"cmd": "can_term", "on": bool(on)})

    def can_respond(self, match_id: int, reply_id: int,
                    reply_data: Union[bytes, Sequence[int], None] = None, *,
                    match_ext: bool = False, reply_ext: bool = False) -> dict:
        """Install an autonomous-responder rule: when a frame with ``match_id``
        arrives, the *firmware* immediately replies with ``reply_id`` +
        ``reply_data`` (answered from the RX ISR — microsecond latency, no host
        round-trip). Turns the pod into a live ECU simulator. Returns
        ``{"rule", "rules"}``."""
        payload = list(bytes(reply_data)) if reply_data else []
        return self.command({
            "cmd": "can_respond", "match_id": int(match_id), "ext": bool(match_ext),
            "reply_id": int(reply_id), "reply_ext": bool(reply_ext),
            "reply_data": payload,
        })

    def can_respond_clear(self) -> dict:
        """Remove all autonomous-responder rules."""
        return self.command({"cmd": "can_respond", "clear": True})

    def can_disable(self) -> dict:
        """Stop FDCAN1 and release the peripheral."""
        return self.command({"cmd": "can_disable"})

    def open_can(self, *, bitrate: int = 500_000, mode: str = "normal",
                 term: bool = False, fd: bool = False) -> "_can.CanBus":
        """Configure CAN and return a :class:`~embeddedci.benchpod.can.CanBus`
        session (disables CAN on exit)::

            with bp.open_can(bitrate=500_000, mode="internal") as can:
                can.write(0x123, [1, 2, 3])
                frame = can.expect(can_id=0x123, timeout=1.0)
        """
        return _can.CanBus(self, bitrate=bitrate, mode=mode, term=term, fd=fd)

    # -- UART capture -------------------------------------------------------

    def capture_uart(
        self,
        *,
        rx: Union[Pin, int],
        tx: Union[Pin, int],
        baud: int = 115200,
        duration: float,
        until: Optional[_uart.Until] = None,
    ) -> "_uart.UartCapture":
        """Capture the DUT's UART output for ``duration`` seconds.

        ``rx`` is the LA channel the pod samples (wire the DUT's TX here); ``tx``
        is driven (DUT's RX). ``until`` (substring/regex/predicate) stops early.
        """
        link = self._transport.uart_proxy_start(
            coerce_pin(rx, "rx"), coerce_pin(tx, "tx"), int(baud)
        )
        return _uart.capture(link, duration=duration, until=until)

    def power_cycle_and_capture(
        self,
        *,
        rx: Union[Pin, int],
        tx: Union[Pin, int],
        efuse: Union[Efuse, int] = Efuse.INTERNAL,
        delay: float = 1.0,
        duration: float = 4.0,
        baud: int = 115200,
        until: Optional[_uart.Until] = None,
        off_settle: float = 0.3,
    ) -> "_uart.UartCapture":
        """Power-cycle the target while capturing its boot output.

        Powers the eFuse off, schedules a power-on ``delay`` seconds out (pod-
        side timer), then enters UART capture so the scheduled power-on — and the
        DUT's boot banner — land *inside* the capture window. ``duration`` should
        comfortably exceed ``delay``.
        """
        self.power_off(efuse)
        if off_settle:
            time.sleep(off_settle)
        self.power_on(efuse, delay=delay)
        return self.capture_uart(rx=rx, tx=tx, baud=baud, duration=duration,
                                 until=until)

    def open_uart(
        self,
        *,
        rx: Union[Pin, int],
        tx: Union[Pin, int],
        baud: int = 115200,
        max_buffer: int = 1 << 20,
    ) -> "_uart.UartSession":
        """Open an event-based UART session (background reader) on the DUT.

        Unlike :meth:`capture_uart` (a fixed window) this starts buffering
        immediately and lets you read at a later point, so you can begin
        listening *before* an action and still catch its output — e.g. a
        **non-delayed** eFuse power-on whose boot banner you still want to see::

            with bp.open_uart(rx=5, tx=4, baud=115200) as uart:
                bp.power_on(bp.INTERNAL)            # immediate, no pod-side delay
                uart.expect("APP_OK", timeout=6)    # banner buffered since power-up

        ``rx`` is the LA channel the pod samples (wire the DUT's TX here); ``tx``
        is driven (DUT's RX). Use as a context manager (or call ``.close()``).
        Over the cloud transport, running other commands (``power_on``, ...) while
        a session is open needs them to use the cloud command channel rather than
        a second tunnel — see ``docs/event-uart-design.md``.
        """
        link = self._transport.uart_proxy_start(
            coerce_pin(rx, "rx"), coerce_pin(tx, "tx"), int(baud)
        )
        return _uart.UartSession(link, max_buffer=max_buffer)

    # -- capabilities -------------------------------------------------------

    @property
    def capabilities(self) -> Capabilities:
        """The device's resolved :class:`Capabilities` (ADC scaling, DAC replay depth, flags).

        Parsed from the device ``status`` and — when the SDK has server access (an API key) —
        enriched with the fuller ``cap.*`` set the server holds (explicit calibration + replay
        depth). Cached after the first call.
        """
        if self._caps is None:
            try:
                status = self.status() or {}
            except Exception:
                status = {}
            caps = Capabilities.from_status(status if isinstance(status, dict) else {})
            api = self._try_server_api()
            if api is not None and self._device_name:
                try:
                    params = api.device_parameters(self._device_name)
                    if params:
                        caps = caps.merge(Capabilities.from_parameters(params))
                except Exception:
                    pass
            self._caps = caps
        return self._caps

    def refresh_capabilities(self) -> Capabilities:
        """Drop the cached capabilities and re-read them."""
        self._caps = None
        return self.capabilities

    # -- ADC / logic-analyzer capture ---------------------------------------

    def scope_capture(self, samples: int = 256, *, sample_rate_mhz: Optional[float] = None,
                      source: str = "ext") -> Capture:
        """Capture ADC samples and return a :class:`Capture` with CALIBRATED volts.

        Works over any transport (LAN/serial or the cloud tunnel). The raw counts are scaled to
        volts with the device's affine ADC model (:attr:`capabilities`), matching what the web
        UI / server streams. For deep multi-second captures pass a large ``samples`` count.
        """
        return _capture.scope_capture(self._transport, self.capabilities, samples=samples,
                                      sample_rate_mhz=sample_rate_mhz, source=source)

    def capture_la(self, samples: int = 4096, *,
                   sample_rate_mhz: Optional[float] = None,
                   stop_dac_after_us: int = 0) -> LaCapture:
        """Capture raw 12-channel logic-analyzer words and return a :class:`LaCapture`.

        ``stop_dac_after_us`` (>0) cuts a concurrently-running DAC that many microseconds into the
        capture, at a sample-precise offset from the capture's hardware t0 (no-op on gw < v21).
        """
        return _capture.capture_la(self._transport, samples=samples,
                                   sample_rate_mhz=sample_rate_mhz,
                                   stop_dac_after_us=stop_dac_after_us)

    def capture_analog(self, *, adc_samples: int = 256, adc_rate_mhz: Optional[float] = None,
                       la_samples: int = 256, la_rate_mhz: Optional[float] = None,
                       stop_dac_after_us: int = 0) -> AnalogCapture:
        """Correlated ADC + LA capture from a single hardware trigger (aligned timebases).

        Set either count to 0 for a single-stream capture. Returns an :class:`AnalogCapture`
        holding the calibrated ADC :class:`Capture` and the raw :class:`LaCapture`.

        ``stop_dac_after_us`` (>0) cuts a concurrently-running DAC that many microseconds into the
        capture (sample-precise from t0), so the window shows the target reacting to the output
        switching off. Pair with ``replay(..., on_capture=True)`` for a phase-locked stimulus →
        capture → deterministic cutoff run (no-op on gw < v21).
        """
        return _capture.capture_analog(
            self._transport, self.capabilities, adc_samples=adc_samples,
            adc_rate_mhz=adc_rate_mhz, la_samples=la_samples, la_rate_mhz=la_rate_mhz,
            stop_dac_after_us=stop_dac_after_us)

    def decode(self, source: "LaCapture | Sequence[int]", protocol: str = "i2c", *,
               sample_rate_hz: Optional[float] = None, **channels):
        """Decode a protocol (``i2c``/``uart``/``spi``) from an LA capture or raw words.

        ``source`` may be a :class:`LaCapture` (its sample rate is used) or a raw list of 12-bit
        words. See :func:`benchpod.decode.decode` for the per-protocol channel/param names.
        """
        from . import decode as _decode

        if isinstance(source, LaCapture):
            words = source.words
            if sample_rate_hz is None:
                sample_rate_hz = source.sample_rate_hz
        else:
            words = list(source)
        return _decode.decode(words, protocol, sample_rate_hz=sample_rate_hz or 0.0, **channels)

    # -- DAC generate / DC / stop -------------------------------------------

    def generate(self, waveform: str = "sine", *, freq: float, amplitude: float,
                 offset: float = 0.0, duration_ms: Optional[int] = None,
                 sample_rate_mhz: Optional[float] = None, on_capture: bool = False) -> Any:
        """Generate a parametric DAC waveform (``sine``/``square``/``sawtooth``/…).

        ``duration_ms=0`` (or omitted-and-looping firmware) plays until :meth:`dac_stop`.
        ``on_capture=True`` defers the start to the next capture's hardware t0 (phase-locked
        co-trigger, gateware >= v27); the reply's ``cotrig`` reports whether it armed.
        """
        req: dict = {"cmd": "generate", "waveform": waveform, "freq": freq,
                     "amplitude": amplitude, "offset": offset}
        if duration_ms is not None:
            req["duration_ms"] = int(duration_ms)
        if sample_rate_mhz is not None:
            req["sample_rate_mhz"] = sample_rate_mhz
        if on_capture:
            req["on_capture"] = True
        return self.command(req)

    def dac_set(self, value: int, *, channel: Optional[int] = None) -> Any:
        """Hold a raw DAC DC code (v1 ``dac_dc`` path). See :meth:`dac_output` for v2 volts."""
        req: dict = {"cmd": "dac_set", "value": int(value)}
        if channel is not None:
            req["channel"] = int(channel)
        return self.command(req)

    def dac_stop(self) -> Any:
        """Stop any running DAC output (generate, replay, or control loop). Idempotent."""
        return self.command({"cmd": "dac_stop"})

    # -- closed-loop DAC control (panel/MPPT emulator) ----------------------

    def control_loop(self, *, curve: "Sequence[int] | str | None" = None,
                     voc_code: Optional[int] = None, sharpness: float = 4.0,
                     points: int = _control_loop.CURVE_POINTS,
                     k: int = _control_loop.DEFAULT_K,
                     vmin: int = _control_loop.DEFAULT_VMIN,
                     vmax: int = _control_loop.DEFAULT_VMAX,
                     tick_div: int = _control_loop.DEFAULT_TICK_DIV
                     ) -> "_control_loop.ControlLoopHandle":
        """Arm the in-fabric closed-loop DAC controller (panel/MPPT emulator).

        Each tick the iCE40 reads the ADC, looks it up in the curve LUT (``V = curve[ADC]``),
        damps toward that target (``k``, Q15) and clamps to ``[vmin, vmax]``, then drives the DAC.
        Provide the curve one of three ways: raw ``curve`` codes (0..65535), a base64url ``curve``
        string, or ``voc_code`` (+ ``sharpness``) to synthesise a solar-panel I-V curve. Omit all
        three to run clamp-only. Needs the loop gateware image (:attr:`Capabilities.dac_control_loop`
        — switch with :meth:`fpga_image`). Returns a :class:`ControlLoopHandle` (context manager;
        :meth:`~ControlLoopHandle.probe` for the live operating point, ``stop`` on exit)::

            with bp.control_loop(voc_code=52000, sharpness=6, vmax=52000) as loop:
                pt = loop.probe()      # IVPoint(i=<ADC current>, v=<DAC voltage>)
        """
        curve_b64 = None
        if isinstance(curve, str):
            curve_b64 = curve
        elif curve is not None:
            curve_b64 = _control_loop.encode_curve_b64url(curve)
        elif voc_code is not None:
            curve_b64 = _control_loop.encode_curve_b64url(
                _control_loop.build_panel_curve(voc_code, sharpness, points))

        req: dict = {"cmd": "dac_control_loop", "k": int(k), "vmin": int(vmin),
                     "vmax": int(vmax), "tick_div": int(tick_div)}
        if curve_b64:
            req["curve"] = curve_b64
        data = self.command(req)
        if not isinstance(data, dict):
            data = {}
        if data.get("armed") is False:
            raise BenchPodError(f"control loop did not arm: {data!r}")
        return _control_loop.ControlLoopHandle(
            probe=self.loop_probe, stop=self.dac_stop, data=data)

    def loop_probe(self) -> "_control_loop.IVPoint":
        """Poll the running control loop's live operating point (:class:`IVPoint`).

        ``i`` is the latest ADC reading (the loop's input "current"), ``v`` the DAC code it drove
        this tick (the "voltage") — both raw codes.
        """
        data = self.command({"cmd": "dac_loop_probe"})
        d = data if isinstance(data, dict) else {}
        return _control_loop.IVPoint(i=int(d.get("i", 0)), v=int(d.get("v", 0)))

    # -- runtime gateware image swap ----------------------------------------

    def fpga_image(self, image: int) -> dict:
        """Swap the iCE40 to another gateware image stored in the config flash, with NO reflash.

        ``image=0`` is the closed-loop demo image (advertises :attr:`Capabilities.dac_control_loop`,
        ``features=1``); ``image=1`` is the deep-DAC-PSRAM-replay image. The FPGA reboots into it
        (~10 ms). Returns ``{"image", "version", "features"}``. Invalidates the cached capabilities
        so the next :attr:`capabilities` read reflects the new image.
        """
        data = self.command({"cmd": "fpga_image", "image": int(image)})
        self._caps = None
        return data if isinstance(data, dict) else {}

    # -- power / current monitoring -----------------------------------------

    def power_status(self) -> dict:
        """Return the INA power-monitor reading (per-rail ``ok``/``bus_mv``/``current_ua``)."""
        return self.command({"cmd": "power_status"})

    # -- DAC arbitrary-waveform replay --------------------------------------

    def _replay_bits(self) -> int:
        b = self.capabilities.dac_replay_bits
        if b and b > 0:
            return b
        # v2 (16-bit ADC) pods replay 16-bit; fall back to 8-bit otherwise.
        return 16 if self.capabilities.adc_bits >= 16 else 8

    def _arm_replay(self, code_bytes: bytes, *, bits: int, dac_path: str,
                    sample_rate_mhz: Optional[float], deep: Optional[bool],
                    on_capture: bool = False) -> ReplayHandle:
        """Route the DAC path, upload the codes and arm a looping replay over the tunnel.

        ``on_capture`` defers the DAC start so it fires on the NEXT capture's hardware t0
        (phase-locked co-trigger, gateware >= v27); the reply's ``cotrig`` reports whether the
        device actually armed (it falls back to an immediate start when it can't co-trigger).
        """
        fn = getattr(self._transport, "load_replay", None)
        if fn is None:
            raise BenchPodError("replay needs a streaming transport (TCP/serial or cloud tunnel)")
        n_samples = len(code_bytes) // (2 if bits > 8 else 1)
        if n_samples == 0:
            raise BenchPodError("replay has no samples")
        if deep is None:
            deep = n_samples > 2048  # exceeds the shallow DAC BRAM depth -> stream from PSRAM
        if dac_path:
            self.command({"cmd": "dac_out", "path": dac_path})
        replay_req: dict = {"cmd": "replay", "samples": n_samples}
        if sample_rate_mhz is not None:
            replay_req["sample_rate_mhz"] = sample_rate_mhz
        if on_capture:
            replay_req["on_capture"] = True
        data = fn(data=code_bytes, replay=replay_req, psram=deep)
        rate_hz = (sample_rate_mhz or 0.0) * 1e6
        d = data if isinstance(data, dict) else {}
        return ReplayHandle(stop=self.dac_stop, samples=n_samples, sample_rate_hz=rate_hz,
                            dac_path=dac_path, deep=deep, data=d,
                            cotrig=bool(d.get("cotrig", False)))

    def replay(self, source: "Capture | Sequence[float] | Sequence[int]", *,
               dac_path: str = "5v", mapping: str = "faithful",
               sample_rate_mhz: Optional[float] = None, deep: Optional[bool] = None,
               fault: "Fault | dict | None" = None,
               are_codes: bool = False, on_capture: bool = False) -> ReplayHandle:
        """Replay a waveform on the DAC, streaming it to the device over the transport.

        ``source`` is a captured :class:`Capture` (its volts are replayed), a sequence of volts
        (floats), or — with ``are_codes=True`` — raw DAC codes. Volts are mapped to codes for the
        ``dac_path`` output (``faithful`` reproduces the voltage, clipping outside range; ``fit``
        auto-scales). The replay LOOPS until the returned :class:`ReplayHandle` is stopped, so it
        can run concurrently with a capture (gateware v18). Use as a context manager::

            with bp.replay(cap, dac_path="5v"):
                la = bp.capture_la(8192, sample_rate_mhz=1)   # runs alongside the DAC

        Pass ``on_capture=True`` to phase-lock the DAC to the NEXT capture: the replay is armed
        (``handle.cotrig`` is True) and only starts driving on that capture's hardware t0
        (gateware >= v27, :attr:`Capabilities.dac_cotrig`)::

            with bp.replay(cap, on_capture=True):      # armed, not yet driving
                a = bp.capture_analog(adc_samples=4096, adc_rate_mhz=0.4)   # fires the DAC at t0
        """
        bits = self._replay_bits()
        path_fs = _dsp.dac_path_fullscale_v(dac_path)
        if isinstance(source, Capture):
            volts = source.volts
            codes = _dsp.volts_to_codes(volts, mapping, path_fs, bits=bits)
        elif are_codes:
            codes = [int(x) for x in source]
        else:
            volts = [float(x) for x in source]
            codes = _dsp.volts_to_codes(volts, mapping, path_fs, bits=bits)
        f = normalize_fault(fault)
        if f:
            codes = _dsp.apply_fault(codes, f, bits=bits)
        return self._arm_replay(_dsp.codes_to_bytes(codes, bits), bits=bits, dac_path=dac_path,
                                sample_rate_mhz=sample_rate_mhz, deep=deep, on_capture=on_capture)

    def replaying(self, source, **kwargs) -> ReplayHandle:
        """Alias for :meth:`replay` that reads as a context manager: ``with bp.replaying(...):``."""
        return self.replay(source, **kwargs)

    def replay_waveform(self, waveform: "str | Any" = None, *, waveform_id: Optional[str] = None,
                        dac_path: str = "5v", mapping: str = "faithful",
                        sample_rate_mhz: Optional[float] = None,
                        window_start: int = 0, window_len: int = 0, target_samples: int = 0,
                        fault: "Fault | dict | None" = None, deep: Optional[bool] = None,
                        server_side: Optional[bool] = None,
                        on_capture: bool = False) -> ReplayHandle:
        """Load a **cloud-stored** waveform from the library and replay it on the DAC.

        ``waveform`` is a waveform id (str) or a :class:`~embeddedci.benchpod.waveforms.Waveform`.
        By default this uses the server's replay DSP (``POST /dac/replay/start``) when the SDK has
        server access and the device is cloud-connected; otherwise it fetches the recording blob
        and replays it over the device tunnel with the SAME DSP applied client-side. Over the cloud
        destination the library reuses the connection's session token (incl. GitHub OIDC); on
        LAN/serial pass an ``api_key`` to reach it.
        """
        wid = waveform_id or (waveform.id if hasattr(waveform, "id") else waveform)
        if not wid:
            raise BenchPodError("replay_waveform needs a waveform id")
        lib = self.waveforms
        wf = lib.get(wid)

        use_server = server_side
        if use_server is None:
            use_server = bool(self._device_name)  # server can only reach a cloud-registered device
        if use_server:
            api = self._require_server_api()
            payload: dict = {"device_id": api.resolve_device_id(self._device_name),
                             "waveform_id": wid, "dac_path": dac_path, "mapping": mapping}
            if sample_rate_mhz is not None:
                payload["sample_rate_mhz"] = sample_rate_mhz
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
            # The server replay is async: the "armed"/"playing" outcome arrives as a later
            # dac.event frame, not this immediate reply, so infer cotrig from the request +
            # the device capability (the pod falls back to immediate start when it can't).
            return ReplayHandle(stop=self.dac_stop, samples=int(data.get("samples", 0) or 0),
                                dac_path=dac_path, deep=bool(self.capabilities.dac_deep_replay),
                                data=data, cotrig=bool(on_capture and self.capabilities.dac_cotrig))

        # Client-side: fetch + DSP + stream over the tunnel.
        bits = self._replay_bits()
        if wf.is_recording:
            raw = lib.download_recording(wid)
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
            codes = _dsp.volts_to_codes(volts, mapping, _dsp.dac_path_fullscale_v(wf.dac_path or dac_path), bits=bits)
            f = normalize_fault(fault)
            if f:
                codes = _dsp.apply_fault(codes, f, bits=bits)
            code_bytes = _dsp.codes_to_bytes(codes, bits)
        else:  # dac_waveform: inline codes
            import base64
            raw = base64.b64decode(lib.samples_b64(wid))
            code_bytes = raw  # already device-ready codes at the waveform's bit width
        return self._arm_replay(code_bytes, bits=bits, dac_path=dac_path or wf.dac_path,
                                sample_rate_mhz=sample_rate_mhz, deep=deep, on_capture=on_capture)

    def save_capture_as_recording(self, capture: Capture, name: str, *,
                                  full_scale_v: Optional[float] = None) -> Any:
        """Save an ADC :class:`Capture` to the cloud library as a ``recording`` (raw 16-bit)."""
        volts = capture.volts
        fs = full_scale_v
        if fs is None:
            peak = max((abs(v) for v in volts), default=1.0)
            fs = peak if peak > 0 else 1.0
        blob = _dsp.encode_recording_volts(volts, fs)
        rate = capture.sample_rate_hz or 0.0
        return self.waveforms.save_recording(name, blob, sample_rate_hz=rate, full_scale_v=fs)

    # -- cloud waveform library + server API --------------------------------

    def _try_server_api(self):
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

    def _require_server_api(self):
        api = self._try_server_api()
        if api is None:
            raise BenchPodError(
                "this operation needs embeddedci server access. Connect over the cloud "
                "('embeddedci:<device>', which reuses its session token) or pass api_key='eci_…' "
                "(or set BENCHPOD_API_KEY) on a LAN/serial connection."
            )
        return api

    @property
    def server_api(self):
        """The :class:`~embeddedci.benchpod.server_api.ServerApi` (needs an API key/session)."""
        return self._require_server_api()

    @property
    def waveforms(self):
        """The cloud :class:`~embeddedci.benchpod.waveforms.WaveformLibrary` (needs an API key)."""
        if self._waveforms is None:
            from .waveforms import WaveformLibrary

            self._waveforms = WaveformLibrary(self._require_server_api())
        return self._waveforms

    # -- low-level pass-throughs (TCP transport only) -----------------------

    def command(self, req: dict) -> Any:
        """Send a raw JSON command (TCP transport only)."""
        cmd = getattr(self._transport, "command", None)
        if cmd is None:
            raise BenchPodError("raw JSON commands are only available on the TCP transport")
        return cmd(req)

    def capture(self, samples: int = 256, *, sample_rate_mhz: Optional[float] = None) -> List[int]:
        """Capture ADC samples (TCP transport only)."""
        fn = getattr(self._transport, "samples", None)
        if fn is None:
            raise BenchPodError("capture is only available on the TCP transport")
        req: dict = {"cmd": "capture", "samples": samples}
        if sample_rate_mhz is not None:
            req["sample_rate_mhz"] = sample_rate_mhz
        return fn(req)

    def la_step(self, la: Union[Pin, int], steps: int, delay_us: int,
                dir_la: Optional[Union[Pin, int]] = None, direction: int = 0) -> Any:
        """Emit a step pulse train on an LA channel (TCP transport only).

        The FPGA runs the train autonomously. With ``dir_la`` set, that channel is
        driven to ``direction`` (0/1) before stepping (step/dir stepper drivers).
        """
        req: dict = {
            "cmd": "la",
            "la": coerce_pin(la, "la"),
            "steps": int(steps),
            "delay_us": int(delay_us),
        }
        if dir_la is not None:
            req["dir_la"] = coerce_pin(dir_la, "dir_la")
            req["direction"] = int(direction)
        return self.command(req)
