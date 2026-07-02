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

import time
from typing import Any, List, Optional, Sequence, Union

from . import flash as _flash
from . import i2c as _i2c
from . import sensor as _sensor
from . import uart as _uart
from .constants import Efuse, Pin, Sensor, coerce_efuse, coerce_pin
from .connection import resolve_connection
from .errors import BenchPodError
from .flash import FlashResult
from .lease import DEFAULT_LEASE_TTL, DEFAULT_LEASE_WAIT, DeviceLease
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
