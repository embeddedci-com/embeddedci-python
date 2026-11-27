"""External INA current monitors over the pod's I2C master: µA-class current measurement.

The pod's own INA238 monitors resolve about 125 µA. For a sleep current, put a shunt in series
between the pod's target-power output and the DUT and an INA breakout across it. The monitor is
attached like a GPIO expander and read through the power calls::

    bp.expansion("ina226", addr=0x40, sda=13, scl=14, shunt_ohm=10.0)   # 0.25 µA steps; probes the chip
    prof = bp.measure_power(2.0, monitor="dut")      # a PowerProfile: avg, min, peak, charge, energy
    print(bp.power_status().monitors)                # live bus voltage and current of each monitor
    with bp.power_profile(monitor="dut") as p:       # around a block of code
        run_test()

The host SDK drives the chip through the pod's I2C master on two LA pins, so several monitors at
different addresses share one bus with each other and with a GPIO expander on the same pins. Current
is computed on the host from the shunt voltage and ``shunt_ohm``: the chips' calibration and current
registers are not used (so a wrong ``shunt_ohm`` is the only calibration error). The chips are
the ``kind="current"`` entries of :data:`~embeddedci.benchpod.expansion.EXPANSION_CHIPS`.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

from .errors import BenchPodError
from .expansion import ExpansionChip, ExpansionError
from .power import PowerProfile

if TYPE_CHECKING:  # pragma: no cover
    from .client import BenchPod
    from .i2c_master import I2CMaster

#: Conversion times (seconds) of the INA226, INA260 and INA3221 (3-bit field) and of the INA228
#: and INA238 (3-bit field), and the averaging counts all of them share.
_CT_A = (140e-6, 204e-6, 332e-6, 588e-6, 1.1e-3, 2.116e-3, 4.156e-3, 8.244e-3)
_CT_B = (50e-6, 84e-6, 150e-6, 280e-6, 540e-6, 1052e-6, 2074e-6, 4120e-6)
_AVG = (1, 4, 16, 64, 128, 256, 512, 1024)
#: INA219: the 4-bit ADC field as (samples averaged, seconds for the whole result).
_ADC219 = ((1, 84e-6), (1, 148e-6), (1, 276e-6), (1, 532e-6), (2, 1.06e-3), (4, 2.13e-3),
           (8, 4.26e-3), (16, 8.51e-3), (32, 17.02e-3), (64, 34.05e-3), (128, 68.10e-3))

CurrentMonitorError = ExpansionError


# -- results ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class CurrentReading:
    """One reading: volts, amps, watts."""

    #: Voltage at the chip's bus pin (the load side of the shunt).
    bus_voltage: float
    #: Voltage across the shunt.
    shunt_voltage: float
    #: ``shunt_voltage / shunt_ohm``, signed.
    current: float
    #: ``bus_voltage * current``.
    power: float
    #: The shunt input is at or beyond the chip's range (INA219: its overflow flag).
    overflow: bool = False


@dataclass(frozen=True)
class MonitorStatus:
    """One attached INA monitor, as :meth:`BenchPod.expansion <embeddedci.benchpod.BenchPod.expansion>`
    reports it. ``reading`` is its live reading (``None`` with ``error`` set when it could not be read)."""

    #: The name ``monitor=`` takes (default ``"ina226@0x40"``, INA3221 ``"ina3221@0x40.2"``, or from ``names=``).
    name: str
    chip: str
    addr: int
    channel: int
    shunt_ohm: float
    sda: int
    scl: int
    averaging: int
    #: Seconds the chip takes for one new result.
    cycle: float
    reading: Optional[CurrentReading] = None
    error: Optional[str] = None


# -- the shared I2C session ---------------------------------------------------------------------

class _Dev:
    def __init__(self, bus: Callable[[], "I2CMaster"], addr: int) -> None:
        self._bus, self.addr = bus, addr

    def r16(self, reg: int) -> int:
        return int.from_bytes(self._bus().write_read(self.addr, bytes([reg]), 2), "big")

    def w16(self, reg: int, value: int) -> None:
        self._bus().write(self.addr, bytes([reg]) + (value & 0xFFFF).to_bytes(2, "big"))

    def block(self, reg: int, n: int) -> bytes:
        return self._bus().write_read(self.addr, bytes([reg]), n)


def _s(value: int, bits: int) -> int:
    return value - (1 << bits) if value >= 1 << (bits - 1) else value


def _nearest(options: Sequence[float], want: float) -> int:
    return min(range(len(options)), key=lambda i: abs(options[i] - want))


# -- chip drivers -------------------------------------------------------------------------------

class _Driver:
    """Register model of one INA family: configure, read raw, cycle time. Keeps its settings."""

    def __init__(self, chip: ExpansionChip, channel: int) -> None:
        self.chip = chip
        self.channel = channel
        self.averaging = 16
        self.ct_index = 4
        self.fine = False

    # defaults per family are set by subclasses
    def reset(self, dev: _Dev) -> None:
        raise NotImplementedError  # pragma: no cover

    def configure(self, dev: _Dev, averaging: Optional[int], conversion_time: Optional[float],
                  max_shunt_v: Optional[float]) -> None:
        raise NotImplementedError  # pragma: no cover

    def read(self, dev: _Dev) -> Tuple[float, float, bool]:
        """``(shunt volts, bus volts, overflow)``."""
        raise NotImplementedError  # pragma: no cover

    @property
    def cycle(self) -> float:  # pragma: no cover
        raise NotImplementedError

    def samples(self) -> int:
        """Conversions averaged per result."""
        return self.averaging

    def _avg(self, averaging: Optional[int]) -> int:
        if averaging is None:
            return self.averaging
        if averaging not in _AVG:
            raise ValueError(f"averaging for the {self.chip.name} is one of "
                             f"{', '.join(str(a) for a in _AVG)}, got {averaging!r}")
        return averaging


class _AFamily(_Driver):
    """INA226 / INA260 / INA3221: config at 0, AVG 11:9, VBUSCT 8:6, VSHCT 5:3, MODE 2:0."""

    def _config(self) -> int:
        base = {"ina226": 0x4000, "ina260": 0x6000, "ina3221": 0x7000}[self.chip.name]  # reserved/enable bits
        ct = self.ct_index
        return base | _AVG.index(self.averaging) << 9 | ct << 6 | ct << 3 | 0b111

    def reset(self, dev: _Dev) -> None:
        dev.w16(0x00, 0x8000)

    def configure(self, dev: _Dev, averaging: Optional[int], conversion_time: Optional[float],
                  max_shunt_v: Optional[float]) -> None:
        self.averaging = self._avg(averaging)
        if conversion_time is not None:
            self.ct_index = _nearest(_CT_A, conversion_time)
        dev.w16(0x00, self._config())

    @property
    def cycle(self) -> float:
        n = 3 if self.chip.name == "ina3221" else 1   # the INA3221 converts its 3 channels in turn
        return 2 * _CT_A[self.ct_index] * self.averaging * n

    def read(self, dev: _Dev) -> Tuple[float, float, bool]:
        name = self.chip.name
        if name == "ina226":
            raw = dev.block(0x01, 4)
            shunt = _s(int.from_bytes(raw[:2], "big"), 16) * 2.5e-6
            bus = int.from_bytes(raw[2:], "big") * 1.25e-3
            return shunt, bus, abs(shunt) >= 81.9e-3
        if name == "ina260":
            raw = dev.block(0x01, 4)
            current = _s(int.from_bytes(raw[:2], "big"), 16) * 1.25e-3
            return current * self.chip.internal_ohm, int.from_bytes(raw[2:], "big") * 1.25e-3, False
        base = 1 + 2 * (self.channel - 1)
        raw = dev.block(base, 4)
        shunt = (_s(int.from_bytes(raw[:2], "big"), 16) >> 3) * 40e-6
        bus = (_s(int.from_bytes(raw[2:], "big"), 16) >> 3) * 8e-3
        return shunt, bus, abs(shunt) >= 163.8e-3


class _Ina219(_Driver):
    PGA = (40e-3, 80e-3, 160e-3, 320e-3)

    def __init__(self, chip: ExpansionChip, channel: int) -> None:
        super().__init__(chip, channel)
        self.adc = 7     # 16 samples, 8.51 ms
        self.pga = 3

    def _config(self) -> int:
        adc = self.adc if self.adc < 4 else 0b1000 | (self.adc - 3)
        return 1 << 13 | self.pga << 11 | adc << 7 | adc << 3 | 0b111

    def reset(self, dev: _Dev) -> None:
        dev.w16(0x00, 0x8000)

    def configure(self, dev: _Dev, averaging: Optional[int], conversion_time: Optional[float],
                  max_shunt_v: Optional[float]) -> None:
        if averaging is not None or conversion_time is not None:
            # The chip has one field: 9-12 bit single conversions, or 12 bit averaged 2..128.
            samples = 1 if averaging is None else averaging
            if samples not in (1, 2, 4, 8, 16, 32, 64, 128):
                raise ValueError("averaging for the ina219 is one of 1, 2, 4, 8, 16, 32, 64, 128, "
                                 f"got {averaging!r}")
            if samples == 1:
                want = conversion_time if conversion_time is not None else 532e-6
                self.adc = _nearest([t for n, t in _ADC219 if n == 1], want)
            else:
                self.adc = 3 + (samples.bit_length() - 1)   # 2 -> index 4 ... 128 -> 10
        if max_shunt_v is not None:
            self.pga = next((i for i, v in enumerate(self.PGA) if max_shunt_v <= v), 3)
        dev.w16(0x00, self._config())

    def samples(self) -> int:
        return _ADC219[self.adc][0]

    @property
    def cycle(self) -> float:
        return 2 * _ADC219[self.adc][1]

    def read(self, dev: _Dev) -> Tuple[float, float, bool]:
        raw = dev.block(0x01, 4)
        shunt = _s(int.from_bytes(raw[:2], "big"), 16) * 10e-6
        busreg = int.from_bytes(raw[2:], "big")
        return shunt, (busreg >> 3) * 4e-3, bool(busreg & 1)


class _BFamily(_Driver):
    """INA228 / INA238: CONFIG 0 (RST 15, ADCRANGE 4), ADC_CONFIG 1 (MODE 15:12, VBUSCT 11:9,
    VSHCT 8:6, VTCT 5:3, AVG 2:0), shunt at 4, bus at 5."""

    def reset(self, dev: _Dev) -> None:
        dev.w16(0x00, 0x8000)

    def configure(self, dev: _Dev, averaging: Optional[int], conversion_time: Optional[float],
                  max_shunt_v: Optional[float]) -> None:
        self.averaging = self._avg(averaging)
        if conversion_time is not None:
            self.ct_index = _nearest(_CT_B, conversion_time)
        if max_shunt_v is not None:
            self.fine = max_shunt_v <= 40.96e-3
        dev.w16(0x00, 0x10 if self.fine else 0x00)
        ct = self.ct_index
        # MODE 0xB: continuous shunt and bus (no temperature, so a cycle is two conversions).
        dev.w16(0x01, 0xB << 12 | ct << 9 | ct << 6 | 0b101 << 3 | _AVG.index(self.averaging))

    @property
    def cycle(self) -> float:
        return 2 * _CT_B[self.ct_index] * self.averaging

    def read(self, dev: _Dev) -> Tuple[float, float, bool]:
        if self.chip.name == "ina228":
            raw = dev.block(0x04, 6)
            shunt = (_s(int.from_bytes(raw[:3], "big"), 24) >> 4) * (78.125e-9 if self.fine
                                                                      else 312.5e-9)
            bus = (int.from_bytes(raw[3:], "big") >> 4) * 195.3125e-6
            return shunt, bus, abs(shunt) >= (40.9e-3 if self.fine else 163.8e-3)
        raw = dev.block(0x04, 4)
        shunt = _s(int.from_bytes(raw[:2], "big"), 16) * (1.25e-6 if self.fine else 5e-6)
        bus = int.from_bytes(raw[2:], "big") * 3.125e-3
        return shunt, bus, abs(shunt) >= (40.9e-3 if self.fine else 163.8e-3)


def _driver(chip: ExpansionChip, channel: int) -> _Driver:
    if chip.name in ("ina226", "ina260", "ina3221"):
        return _AFamily(chip, channel)
    if chip.name == "ina219":
        return _Ina219(chip, channel)
    return _BFamily(chip, channel)


# -- the monitor --------------------------------------------------------------------------------

def validate_settings(chip: ExpansionChip, shunt_ohm: float, averaging: Optional[int],
                      max_current: Optional[float]) -> None:
    """Raise ``ValueError`` for settings the chip cannot take, before anything is changed."""
    if averaging is not None:
        ok = (1, 2, 4, 8, 16, 32, 64, 128) if chip.name == "ina219" else _AVG
        if averaging not in ok:
            raise ValueError(f"averaging for the {chip.name} is one of {', '.join(str(a) for a in ok)}, "
                             f"got {averaging!r}")
    if max_current is not None and chip.shunt_max and abs(max_current) * shunt_ohm > chip.shunt_max:
        raise ValueError(f"max_current {max_current:g} A across {shunt_ohm:g} ohm is "
                         f"{abs(max_current) * shunt_ohm * 1000:.1f} mV: the {chip.name} takes at most "
                         f"{chip.shunt_max * 1000:.2f} mV. Use a smaller shunt")


class CurrentMonitor:
    """One INA chip (INA3221: one channel of it) attached by :meth:`BenchPod.expansion
    <embeddedci.benchpod.BenchPod.expansion>` (internal: use ``monitor=`` on the power calls)."""

    def __init__(self, pod: "BenchPod", chip: ExpansionChip, *, addr: int, sda: int, scl: int,
                 hz: int, pullups: str, shunt_ohm: float, channel: int, name: Optional[str]) -> None:
        self._pod = pod
        self.chip = chip
        self.addr, self.sda, self.scl, self.hz, self.pullups = addr, sda, scl, hz, pullups
        self.shunt_ohm = shunt_ohm
        self.channel = channel
        self.name = name or f"{chip.name}@0x{addr:02x}" + (f".{channel}" if chip.channels > 1 else "")
        self._drv = _driver(chip, channel)
        self._dev = _Dev(self._bus, addr)
        self.closed = False
        #: Attached from the wiring profile's ``monitors`` (replaced when the profile changes).
        self.from_profile = False
        #: The profile entry that describes this monitor (what ``save_wiring`` stores).
        self.spec: Optional[Any] = None
        #: The clock and sleep :meth:`readings` pace themselves with (replaceable in tests).
        self.sleep: Callable[[float], None] = time.sleep
        self.clock: Callable[[], float] = time.monotonic

    def _bus(self) -> "I2CMaster":
        if self.closed:
            raise CurrentMonitorError(f"{self.name} is closed")
        return self._pod._own_i2c_session(self.sda, self.scl, hz=self.hz, pullups=self.pullups,
                                          who=self.name)

    # -- attach / config -------------------------------------------------------------------------
    def _attach(self, *, averaging: Optional[int], conversion_time: Optional[float],
                max_current: Optional[float], first_of_chip: bool) -> None:
        """Probe, check the IDs, reset (unless a sibling channel already configured the chip) and
        configure."""
        c = self.chip
        bus = self._bus()
        if not bus.probe(self.addr):
            found = bus.scan()
            seen = ", ".join(f"0x{a:02x}" for a in found) or "nothing"
            raise CurrentMonitorError(
                f"no {c.name} answers at 0x{self.addr:02x} on LA{self.sda} SDA / LA{self.scl} SCL "
                f"(a scan found {seen}). Check the A0/A1 address pins, the breakout's power (the LA "
                "voltage) and its pull-ups")
        for reg, mask, want in c.ids:
            got = self._dev.r16(reg)
            if got & mask != want:
                raise CurrentMonitorError(
                    f"0x{self.addr:02x} answers, but register 0x{reg:02x} reads 0x{got:04x}, not the "
                    f"{c.name}'s 0x{want:04x}: another chip, or a different INA type")
        if first_of_chip:
            self._drv.reset(self._dev)
        max_v = None if max_current is None else abs(max_current) * self.shunt_ohm
        if max_v is not None and c.shunt_max and max_v > c.shunt_max:
            raise ValueError(f"max_current {max_current:g} A across {self.shunt_ohm:g} ohm is "
                             f"{max_v * 1000:.1f} mV: the {c.name} takes at most "
                             f"{c.shunt_max * 1000:.2f} mV. Use a smaller shunt")
        self._drv.configure(self._dev, averaging, conversion_time, max_v)

    @property
    def averaging(self) -> int:
        """Conversions averaged per result."""
        return self._drv.samples()

    @property
    def cycle(self) -> float:
        """Seconds the chip takes for one new result (averaging x conversion times)."""
        return self._drv.cycle

    # -- reading ---------------------------------------------------------------------------------
    def read(self) -> CurrentReading:
        """One reading of the latest finished conversion."""
        shunt, bus, over = self._drv.read(self._dev)
        current = shunt / self.shunt_ohm
        return CurrentReading(bus_voltage=bus, shunt_voltage=shunt, current=current,
                              power=bus * current, overflow=over)

    def period(self, rate_hz: Optional[float] = None) -> float:
        """Seconds between reads: one chip conversion cycle (at least 5 ms), or ``rate_hz`` (at
        most 200: a read is one bus transaction)."""
        return max(self.cycle, 0.005) if rate_hz is None else 1.0 / min(float(rate_hz), 200.0)

    def readings(self, seconds: float, *, rate_hz: Optional[float] = None) -> List[Tuple[float, CurrentReading]]:
        """``(t, reading)`` pairs for ``seconds``, one read per period."""
        if not 0 < seconds <= 3600:
            raise ValueError(f"seconds must be > 0 and <= 3600, got {seconds!r}")
        period = self.period(rate_hz)
        out: List[Tuple[float, CurrentReading]] = []
        t0 = self.clock()
        while True:
            t = self.clock() - t0
            out.append((t, self.read()))
            if t + period >= seconds:
                break
            self.sleep(period)
        return out

    def status(self, *, read: bool = True) -> MonitorStatus:
        reading: Optional[CurrentReading] = None
        error: Optional[str] = None
        if read:
            try:
                reading = self.read()
            except BenchPodError as exc:
                error = str(exc)
        return MonitorStatus(name=self.name, chip=self.chip.name, addr=self.addr,
                             channel=self.channel, shunt_ohm=self.shunt_ohm, sda=self.sda,
                             scl=self.scl, averaging=self.averaging, cycle=self.cycle,
                             reading=reading, error=error)

    def close(self) -> None:
        """Detach: the pod's pull-ups this monitor switched on go off (unless another user needs
        them) and, when nothing else needs the bus, the SDK's I2C session closes. The chip keeps
        running."""
        if not self.closed:
            self.closed = True
            self._pod._forget_monitor(self)


class MonitorProfileSession:
    """A power profile of an external monitor around a block of code (from
    :meth:`BenchPod.power_profile(monitor=...) <embeddedci.benchpod.BenchPod.power_profile>`): the same
    ``start()``, ``stop()``, ``result`` and context manager as the pod's own
    :class:`~embeddedci.benchpod.power.PowerProfileSession`. A background thread reads the chip
    once per conversion cycle (``rate_hz`` at most 200)."""

    def __init__(self, mon: CurrentMonitor, *, rate_hz: Optional[float], keep_samples: int,
                 max_duration: float) -> None:
        self._mon = mon
        self._rate = rate_hz
        self._keep = keep_samples
        self._max = max_duration
        self._pairs: List[Tuple[float, CurrentReading]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._error: Optional[BaseException] = None
        self._truncated = False
        self._result: Optional[PowerProfile] = None

    def _run(self) -> None:
        mon = self._mon
        period = mon.period(self._rate)
        t0 = mon.clock()
        try:
            while True:
                t = mon.clock() - t0
                self._pairs.append((t, mon.read()))
                if t + period >= self._max:
                    self._truncated = True
                    break
                if self._stop.wait(period):
                    break
        except BaseException as exc:  # reported by stop()
            self._error = exc

    def start(self) -> "MonitorProfileSession":
        """Start sampling (the context manager calls this)."""
        self._pairs, self._result, self._error, self._truncated = [], None, None, False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="monitor-profile", daemon=True)
        self._thread.start()
        return self

    def status(self) -> Dict[str, Any]:
        """``{"running", "monitor", "elapsed_ms", "n"}``; ``monitor`` is the monitor's name."""
        n = len(self._pairs)
        t = self._pairs[-1][0] if n else 0.0
        return {"running": bool(self._thread and self._thread.is_alive()), "monitor": self._mon.name,
                "elapsed_ms": int(t * 1000), "n": n}

    def stop(self) -> PowerProfile:
        """Stop sampling and return the :class:`~embeddedci.benchpod.power.PowerProfile` (idempotent)."""
        if self._result is None:
            if self._thread is None:
                raise BenchPodError("the power profile was never started")
            self._stop.set()
            self._thread.join()
            if self._error is not None:
                raise self._error
            if not self._pairs:
                self._pairs.append((0.0, self._mon.read()))
            self._result = PowerProfile.from_readings(
                self._pairs, adc_rate_hz=1.0 / self._mon.cycle, keep_samples=self._keep,
                truncated=self._truncated, monitor=self._mon.name)
        return self._result

    @property
    def result(self) -> PowerProfile:
        """The profile; stops sampling first if it still runs."""
        return self.stop()

    def __enter__(self) -> "MonitorProfileSession":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()
