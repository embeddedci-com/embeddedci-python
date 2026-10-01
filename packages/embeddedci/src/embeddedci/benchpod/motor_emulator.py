"""The BenchPod motor & battery emulator: ECP5 boards on the stack link.

Up to four emulator boards share one SPI link (SCK, MOSI, MISO, SS) wired to four LA pins, plus
PROGRAMN and DONE for configuration. :meth:`BenchPod.open_motor_emulator` arms the SPI master and
returns a :class:`MotorEmulator`::

    with bp.open_motor_emulator(programn="PROGRAMN", done="DONE") as emu:
        emu.configure("emu.bit")                 # every board in the stack at once
        print(emu.probe(0))
        emu.set_pwm(200_000)
        emu.arm(0)
        emu.set_control(0, pwm=True)
        print(emu.sample(0))

Everything the gateware offers is reachable through :meth:`MotorEmulator.read` and
:meth:`MotorEmulator.write` with the register names of the link protocol (``REGISTERS``,
``motor-emulator/ecp5/PROTOCOL.md`` in the firmware repository); the helpers here convert the
common ones to physical units with an :class:`EmulatorCalibration`. Needs gateware 0x0007 or later
(port registers that take a whole burst).
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .errors import BenchPodError

if TYPE_CHECKING:  # pragma: no cover
    from .gpio import GpioPin
    from .spi import SpiSession

#: Register 0x00 reads this on an emulator board ("ME").
EMULATOR_ID = 0x4D45
#: The oldest gateware this driver talks to (whole-burst port registers).
MIN_VERSION = 0x0007
#: The emulator's clock.
CLOCK_HZ = 36_000_000

#: Register addresses, as named in the link protocol.
REGISTERS: Dict[str, int] = {
    "ID": 0x00, "VERSION": 0x01, "BOARD": 0x02, "STATUS": 0x03, "CONTROL": 0x04, "ARM": 0x05,
    "WD_CLEAR": 0x06, "SCRATCH": 0x07, "PWM_PERIOD": 0x08, "DEADTIME": 0x09,
    "DUTY_A": 0x0A, "DUTY_B": 0x0B, "DUTY_C": 0x0C, "MIN_ON": 0x0D,
    "SINC_A": 0x10, "SINC_B": 0x11, "SINC_C": 0x12, "SINC_D": 0x13, "SINC_E": 0x14,
    "SINC_COUNT": 0x15, "BRAKE_DUTY": 0x18, "HALL": 0x1C,
    "MODE": 0x20, "W_SET": 0x21, "WSHIFT": 0x22, "KE": 0x23, "KT": 0x24, "LOAD": 0x25,
    "DAMP": 0x26, "INVJ": 0x27, "JSHIFT": 0x28, "CM_KP": 0x29, "CM_KI": 0x2A,
    "CM_MARGIN": 0x2B, "DT_FRAC": 0x2C, "DT_IDB": 0x2D, "HALL_OFS": 0x2E, "MODEL_RESET": 0x2F,
    "THETA": 0x30, "SPEED": 0x31, "EMF": 0x32, "I0": 0x33, "VCM": 0x34, "IQ": 0x35,
    "MDUTY_A": 0x36, "MDUTY_B": 0x37, "MDUTY_C": 0x38, "MODEL_COUNT": 0x39,
    "I_OFS_A": 0x3A, "I_OFS_B": 0x3B, "I_OFS_C": 0x3C, "VBUS_OFS": 0x3D, "ADV": 0x3E, "R_SUB": 0x3F,
    "ENC_CTRL": 0x40, "ENC_CPR": 0x41, "ENC_POLES": 0x42, "ENC_OFS": 0x43, "ENC_RF_ADDR": 0x44,
    "ENC_RF_DATA": 0x45, "ENC_ANGLE": 0x46, "ENC_COUNT": 0x47, "ENC_STATUS": 0x48,
    "TRIPS": 0x50, "OC_LIM": 0x51, "OC_COUNT": 0x52, "OV_ON": 0x53, "OV_OFF": 0x54,
    "BRK_G": 0x55, "BRK_PAVG": 0x56, "BRK_CAP": 0x57, "BRK_TEST": 0x58, "BRK_V0": 0x59,
    "BRK_V1": 0x5A, "BRK_ENERGY": 0x5B, "HS_LIMIT": 0x5C, "TRIP_SRC": 0x5D, "BAT_I_OFS": 0x5E,
    "BAT_CTRL": 0x60, "BAT_QSHIFT": 0x61, "BAT_R0": 0x62, "BAT_R1": 0x63, "BAT_ALPHA": 0x64,
    "BAT_VMIN": 0x65, "BAT_VMAX": 0x66, "BAT_POS": 0x67, "BAT_TBL_ADDR": 0x68,
    "BAT_TBL_DATA": 0x69, "PV_OFS": 0x6A, "PV_GAIN": 0x6B, "BAT_SET": 0x6C, "BAT_I": 0x6D,
    "BAT_OCV": 0x6E,
    "SYNC_CTRL": 0x70, "TIME_MS": 0x71, "TIME_SUB": 0x72, "SYNC_ERR": 0x73,
    "HALL_FAULT": 0x74, "ENC_FAULT": 0x75, "NOISE_AMP": 0x76, "SHAPE_ADDR": 0x77,
    "SHAPE_DATA": 0x78, "LOG_CTRL": 0x79, "LOG_LEVEL": 0x7A, "LOG_DROPS": 0x7B,
    "EE_ADDR": 0x7C, "EE_DATA": 0x7D, "EE_CMD": 0x7E, "LOG_FIFO": 0xC0,
}

#: The logging channels in stream order (LOG_CTRL bits 0-4).
LOG_CHANNELS: Tuple[str, ...] = ("a", "b", "c", "battery", "bus")

_ISC_ENABLE = bytes([0xC6, 0, 0, 0])
_LSC_BITSTREAM_BURST = bytes([0x7A, 0, 0, 0])
_ISC_DISABLE = bytes([0x26, 0, 0, 0])
#: Words per link read: one ``spi_xfer`` of 768 bytes, less the command, address and turnaround.
_READ_WORDS = (768 - 3) // 2

Register = Union[str, int]


def _addr(reg: Register) -> int:
    if isinstance(reg, str):
        try:
            return REGISTERS[reg.upper()]
        except KeyError:
            raise ValueError(f"unknown emulator register {reg!r}") from None
    if not 0 <= int(reg) <= 0xFF:
        raise ValueError(f"register address {reg} is not 0-255")
    return int(reg)


def _s16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def _u16(v: int) -> int:
    if not -0x8000 <= v <= 0xFFFF:
        raise ValueError(f"{v} does not fit a 16-bit register")
    return v & 0xFFFF


def _sat(v: float, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(round(v))))


@dataclass(frozen=True)
class EmulatorCalibration:
    """Code scales of the sinc channels. The defaults are the design values; replace them with a
    bench calibration for accurate units."""

    #: Phase and DUT battery current codes per ampere.
    codes_per_amp: float = 2048.0
    #: Bus voltage codes per volt.
    codes_per_volt: float = 391.0
    #: Offsets (codes) subtracted from phase A, B, C, the DUT battery current and the bus.
    offsets: Tuple[int, int, int, int, int] = (0, 0, 0, 0, 0)
    #: Sinc sample rate at OSR 64 (the protection and battery model run on every sample set).
    sample_rate_hz: float = 281_250.0

    def amps(self, code: int, channel: int = 0) -> float:
        return (code - self.offsets[channel]) / self.codes_per_amp

    def volts(self, code: int) -> float:
        return (code - self.offsets[4]) / self.codes_per_volt

    def current_code(self, amps: float) -> int:
        return _sat(amps * self.codes_per_amp, -32768, 32767)

    def bus_code(self, volts: float) -> int:
        return _sat(volts * self.codes_per_volt, -32768, 32767)

    def resistance_code(self, ohm: float) -> int:
        """R in the model's units: volts codes = current codes x code >>> 15."""
        code = ohm * self.codes_per_volt / self.codes_per_amp * 32768
        if not -32768 <= code <= 32767:
            raise ValueError(f"{ohm} ohm is out of range (at most "
                             f"{32767 * self.codes_per_amp / self.codes_per_volt / 32768:.2f} ohm)")
        return int(round(code))


@dataclass(frozen=True)
class BoardInfo:
    """What :meth:`MotorEmulator.probe` reads from one board."""

    board: int
    version: int
    #: The board id the EEPROM strap gave (BOARD[1:0]).
    strap_id: int
    #: The EEPROM probe finished, and the EEPROM answered.
    probe_done: bool
    probe_ok: bool


@dataclass(frozen=True)
class EmulatorStatus:
    """STATUS (0x03)."""

    raw: int
    #: The stack FAULT line is high (no fault).
    fault_clear: bool
    #: The board's fault latch is armed (LATCH_Q).
    armed: bool
    #: The 57 V backstop comparator is braking.
    backstop: bool
    sync_level: bool
    watchdog_trip: bool
    pwm_running: bool
    sync_seen: bool
    pll_locked: bool

    @classmethod
    def from_word(cls, w: int) -> "EmulatorStatus":
        b = [bool(w >> i & 1) for i in range(8)]
        return cls(raw=w, fault_clear=b[0], armed=b[1], backstop=b[2], sync_level=b[3],
                   watchdog_trip=b[4], pwm_running=b[5], sync_seen=b[6], pll_locked=b[7])


@dataclass(frozen=True)
class TripState:
    """TRIPS (0x50) and the trip recorder (TRIP_SRC, 0x5D)."""

    raw: int
    #: Overcurrent trips on phase A, B, C (sticky).
    overcurrent: Tuple[bool, bool, bool]
    #: The bus went over HS_LIMIT with the hot-swap on (sticky).
    hot_swap: bool
    #: The brake energy budget is inhibiting the brake (live).
    brake_inhibited: bool
    #: The overvoltage brake is on (live).
    overvoltage_brake: bool
    #: The trip expander's inputs at its last read, active-low names that read 0, or None when
    #: it was never read.
    sources: Optional[Tuple[str, ...]]
    source_raw: int

    _SOURCES = ("OC_TRIP_N", "DRV_NFAULT", "NTC_TRIP_N", "HS_FLT_N", "FAULT")

    @classmethod
    def from_words(cls, trips: int, src: int) -> "TripState":
        sources: Optional[Tuple[str, ...]] = None
        if src & 0x100:
            sources = tuple(n for i, n in enumerate(cls._SOURCES) if not src >> i & 1)
        return cls(raw=trips, overcurrent=(bool(trips & 1), bool(trips & 2), bool(trips & 4)),
                   hot_swap=bool(trips & 8), brake_inhibited=bool(trips & 16),
                   overvoltage_brake=bool(trips & 32), sources=sources, source_raw=src)

    @property
    def tripped(self) -> bool:
        return any(self.overcurrent) or self.hot_swap


@dataclass(frozen=True)
class Sample:
    """One sinc sample set (:meth:`MotorEmulator.sample`)."""

    #: Raw codes: phase A, B, C, DUT battery current, bus.
    codes: Tuple[int, int, int, int, int]
    #: SINC_COUNT: increments with every set.
    count: int
    #: Phase currents in A (positive = out of the emulator leg).
    phase_currents: Tuple[float, float, float]
    battery_current: float
    bus_voltage: float


@dataclass(frozen=True)
class ConfigResult:
    """The outcome of :meth:`MotorEmulator.configure`."""

    length: int
    seconds: float
    #: DONE went high: every board in the stack loaded the bitstream.
    done: bool


@dataclass
class BatteryModel:
    """A battery pack for :meth:`MotorEmulator.set_battery`.

    ``ocv`` is the open-circuit voltage of the whole pack over the charge drawn, from full to
    empty, at evenly spaced points (2-64 of them); it is resampled onto the gateware's table.
    ``r0_ohm`` is the series resistance, ``r1_ohm`` and ``tau_s`` the RC branch (polarisation).
    """

    capacity_ah: float
    ocv: Sequence[float]
    r0_ohm: float
    r1_ohm: float = 0.0
    tau_s: float = 1.0
    #: State of charge to start from, 0-1.
    soc: float = 1.0
    #: Setpoint clamp; the default is 0 V up to the highest OCV.
    v_min: Optional[float] = None
    v_max: Optional[float] = None
    #: Flip the sign of the measured DUT battery current (positive must be discharge).
    invert_current: bool = False


@dataclass(frozen=True)
class BatteryState:
    """The battery model's live values (:meth:`MotorEmulator.battery_state`)."""

    setpoint: float
    current: float
    ocv: float
    #: State of charge, 0-1 (from the table position and the configured capacity).
    soc: Optional[float]
    position: int


@dataclass
class LogData:
    """Complete sample sets from :meth:`MotorEmulator.read_log`."""

    #: Codes per enabled channel (``LOG_CHANNELS`` names), one entry per set.
    channels: Dict[str, List[int]] = field(default_factory=dict)
    #: Sequence words, when enabled (a gap means sets were dropped).
    seq: List[int] = field(default_factory=list)
    #: LOG_DROPS at the read: sets dropped because the FIFO was full.
    drops: int = 0

    def __len__(self) -> int:
        return max((len(v) for v in self.channels.values()), default=len(self.seq))


@dataclass
class _LogState:
    mask: int
    seq: bool
    pending: List[int] = field(default_factory=list)

    @property
    def set_words(self) -> int:
        return bin(self.mask).count("1") + (1 if self.seq else 0)


class MotorEmulator:
    """A stack of emulator boards on one SPI link (from :meth:`BenchPod.open_motor_emulator`).

    A context manager: closing it stops the SPI master and releases PROGRAMN and DONE. Every
    method takes the ``board`` id (0-3) it addresses; writes can go to every board at once with
    ``broadcast=True``.
    """

    def __init__(self, spi: "SpiSession", *, programn: "Optional[GpioPin]" = None,
                 done: "Optional[GpioPin]" = None,
                 calibration: Optional[EmulatorCalibration] = None) -> None:
        self.spi = spi
        self.programn = programn
        self.done = done
        self.calibration = calibration or EmulatorCalibration()
        self._log: Dict[int, _LogState] = {}
        self._qshift: Dict[int, Tuple[int, float]] = {}
        self._closed = False

    # -- lifetime ----------------------------------------------------------------------------
    def close(self) -> None:
        """Stop the SPI master and release the configuration pins. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self.spi.close()
        finally:
            for pin in (self.programn, self.done):
                if pin is not None:
                    pin.release()

    def __enter__(self) -> "MotorEmulator":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- configuration -----------------------------------------------------------------------
    def configure(self, bitstream: Union[bytes, str, "os.PathLike[str]"], *,
                  timeout: float = 2.0) -> ConfigResult:
        """Load a bitstream (``.bit`` from ecppack, bytes or a path) into every board at once.

        Lattice slave SPI, write-only: PROGRAMN pulse, 50 ms, ISC_ENABLE, LSC_BITSTREAM_BURST with
        the whole bitstream in the same frame (staged in the pod's PSRAM), ISC_DISABLE, then wait
        up to ``timeout`` seconds for DONE. Needs PROGRAMN and DONE pins and the pod's
        ``spi_stream`` command. Raises :class:`BenchPodError` when DONE stays low."""
        if self.programn is None or self.done is None:
            raise BenchPodError("configuring needs the PROGRAMN and DONE pins: pass them to "
                                "open_motor_emulator or name them in the wiring profile")
        if isinstance(bitstream, (bytes, bytearray, memoryview)):
            data = bytes(bitstream)
        else:
            with open(bitstream, "rb") as f:
                data = f.read()
        if not data:
            raise ValueError("the bitstream is empty")
        t0 = time.monotonic()
        self.programn.low()
        time.sleep(0.001)
        self.programn.high()
        time.sleep(0.05)
        self.spi.transfer(_ISC_ENABLE)
        self.spi.stream(data, head=_LSC_BITSTREAM_BURST)
        self.spi.transfer(_ISC_DISABLE)
        done = self.done.wait_for(1, timeout=timeout)
        result = ConfigResult(length=len(data), seconds=time.monotonic() - t0, done=done)
        if not done:
            raise BenchPodError(f"DONE stayed low {timeout:g} s after the {len(data)}-byte "
                                "bitstream: a wrong or corrupt bitstream, or a board that did not "
                                "see it (check SCK/MOSI/SS and that every board is powered)")
        self._log.clear()
        self._qshift.clear()
        return result

    # -- register access ---------------------------------------------------------------------
    def read(self, reg: Register, count: Optional[int] = None, *, board: int = 0
             ) -> Union[int, List[int]]:
        """Read one register (an int), or ``count`` words from ``reg`` on (a list; the address
        increments, except on a port register such as LOG_FIFO). ``reg`` is a name from
        ``REGISTERS`` or an address."""
        n = 1 if count is None else int(count)
        if n < 1:
            raise ValueError("count must be at least 1")
        words = self._read(_addr(reg), n, board)
        return words[0] if count is None else words

    def read_signed(self, reg: Register, *, board: int = 0) -> int:
        """Read one register as a signed 16-bit value."""
        return _s16(self._read(_addr(reg), 1, board)[0])

    def write(self, reg: Register, values: Union[int, Sequence[int]], *, board: int = 0,
              broadcast: bool = False) -> None:
        """Write one value or a burst (the address increments, except on a port register such as
        BAT_TBL_DATA or SHAPE_DATA, which takes the whole burst). Values are 16-bit, signed or
        not. ``broadcast`` writes every board in the stack."""
        words = [values] if isinstance(values, int) else list(values)
        if not words:
            raise ValueError("nothing to write")
        _check_board(board)
        tx = bytearray([(0x40 if broadcast else 0) | (board << 4), _addr(reg)])
        for w in words:
            tx += _u16(int(w)).to_bytes(2, "big")
        self.spi.transfer(bytes(tx))

    def _read(self, addr: int, n: int, board: int) -> List[int]:
        _check_board(board)
        rx = self.spi.transfer(bytes([0x80 | (board << 4), addr, 0]) + bytes(2 * n))
        return [int.from_bytes(rx[3 + 2 * i:5 + 2 * i], "big") for i in range(n)]

    # -- identity and state -------------------------------------------------------------------
    def probe(self, board: int = 0) -> BoardInfo:
        """Read ID, VERSION and BOARD. Raises :class:`BenchPodError` when no emulator answers on
        that board id, or its gateware is older than ``MIN_VERSION``."""
        ident, version, b = self._read(0x00, 3, board)
        if ident != EMULATOR_ID:
            raise BenchPodError(f"no motor emulator answers as board {board} (ID read 0x{ident:04x}, "
                                f"expected 0x{EMULATOR_ID:04x})")
        if version < MIN_VERSION:
            raise BenchPodError(f"board {board} runs emulator gateware 0x{version:04x}; this SDK needs "
                                f"0x{MIN_VERSION:04x} or later")
        return BoardInfo(board=board, version=version, strap_id=b & 3, probe_done=bool(b & 0x100),
                         probe_ok=bool(b & 0x200))

    def boards(self) -> List[int]:
        """The board ids (0-3) that answer with the emulator ID."""
        return [b for b in range(4) if self._read(0x00, 1, b)[0] == EMULATOR_ID]

    def status(self, board: int = 0) -> EmulatorStatus:
        return EmulatorStatus.from_word(self._read(0x03, 1, board)[0])

    def time(self, board: int = 0) -> float:
        """The stack time base in seconds: TIME_MS plus TIME_SUB, read together."""
        ms, sub = self._read(0x71, 2, board)
        return ms / 1000.0 + sub / CLOCK_HZ

    # -- bridge ------------------------------------------------------------------------------
    def arm(self, board: int = 0, *, timeout: float = 0.1) -> EmulatorStatus:
        """Pulse ARM (sets the board fault latch) and wait for STATUS to show it armed."""
        self.write("ARM", 1, board=board)
        deadline = time.monotonic() + timeout
        while True:
            st = self.status(board)
            if st.armed or time.monotonic() >= deadline:
                break
        if not st.armed:
            raise BenchPodError(f"board {board} did not arm (STATUS 0x{st.raw:02x}): FAULT is low or a "
                                "trip is still set")
        return st

    def set_control(self, board: int = 0, *, pwm: bool = False, hot_swap: bool = False,
                    brake: bool = False, sync_master: bool = False, sync_required: bool = False) -> None:
        """Write CONTROL: every flag not given is cleared."""
        self.write("CONTROL", int(pwm) | int(hot_swap) << 1 | int(brake) << 2
                   | int(sync_master) << 3 | int(sync_required) << 4, board=board)

    def set_pwm(self, frequency_hz: float, *, deadtime_s: float = 0.0, min_on_s: Optional[float] = None,
                board: int = 0, broadcast: bool = False) -> float:
        """Set the bridge PWM period (and extra dead time, minimum pulse). Returns the frequency
        the 36 MHz clock gives."""
        period = int(round(CLOCK_HZ / frequency_hz))
        if not 32 <= period <= 1023:
            raise ValueError(f"{frequency_hz} Hz is out of range ({CLOCK_HZ / 1023:.0f} - {CLOCK_HZ / 32:.0f} Hz)")
        self.write("PWM_PERIOD", [period, _sat(deadtime_s * CLOCK_HZ, 0, 63)], board=board,
                   broadcast=broadcast)
        if min_on_s is not None:
            self.write("MIN_ON", _sat(min_on_s * CLOCK_HZ, 0, 0xFFFF), board=board, broadcast=broadcast)
        return CLOCK_HZ / period

    def set_duties(self, a: float, b: float, c: float, *, board: int = 0) -> None:
        """Direct duties (MODE 0) as fractions 0-1 of the period at the bus; 0.5 is no phase
        voltage."""
        self.write("DUTY_A", [_sat(x * 65536, 0, 0xFFFF) for x in (a, b, c)], board=board)

    def sample(self, board: int = 0) -> Sample:
        """The latest sinc sample set, in codes and in units."""
        w = self._read(0x10, 6, board)
        codes = tuple(_s16(x) for x in w[:5])
        cal = self.calibration
        return Sample(codes=codes, count=w[5],  # type: ignore[arg-type]
                      phase_currents=(cal.amps(codes[0], 0), cal.amps(codes[1], 1), cal.amps(codes[2], 2)),
                      battery_current=cal.amps(codes[3], 3), bus_voltage=cal.volts(codes[4]))

    def set_shape(self, shape: Sequence[float], *, board: int = 0, broadcast: bool = False) -> None:
        """Load the back-EMF shape: 1024 values over one electrical revolution, -1 to 1 (a sine at
        power-up). The model uses it for the back-EMF and the torque alike."""
        if len(shape) != 1024:
            raise ValueError("the shape has 1024 entries")
        self.write("SHAPE_ADDR", 0, board=board, broadcast=broadcast)
        self.write("SHAPE_DATA", [_sat(x * 32767, -32768, 32767) for x in shape], board=board,
                   broadcast=broadcast)

    # -- protection --------------------------------------------------------------------------
    def trips(self, board: int = 0) -> TripState:
        """TRIPS and the trip recorder's last read."""
        return TripState.from_words(self._read(0x50, 1, board)[0], self._read(0x5D, 1, board)[0])

    def clear_trips(self, board: int = 0) -> None:
        """Clear the sticky trips and the SYNC watchdog trip. PWM_EN stays off: re-arm and
        re-enable explicitly."""
        self.write("TRIPS", 0x000F, board=board)
        self.write("WD_CLEAR", 1, board=board)

    def set_protection(self, board: int = 0, *, overcurrent_a: Optional[float] = None,
                       overcurrent_samples: int = 2, overvoltage_on_v: Optional[float] = None,
                       overvoltage_off_v: Optional[float] = None, hot_swap_limit_v: Optional[float] = None,
                       brake_ohm: Optional[float] = None, brake_avg_w: float = 0.0,
                       brake_budget_j: float = 0.0) -> None:
        """Set the gateware protection; ``None`` turns a limit off. The overvoltage brake, the
        brake budget and the hot-swap act on board 0 only."""
        cal = self.calibration
        if not 1 <= overcurrent_samples <= 15:
            raise ValueError("overcurrent_samples is 1-15")
        self.write("OC_LIM", [0 if overcurrent_a is None else _sat(overcurrent_a * cal.codes_per_amp, 1, 32767),
                              overcurrent_samples], board=board)
        on = 0 if overvoltage_on_v is None else cal.bus_code(overvoltage_on_v)
        off = on if overvoltage_off_v is None else cal.bus_code(overvoltage_off_v)
        g = 0 if brake_ohm is None else _sat(2 ** 24 / (cal.codes_per_volt ** 2 * brake_ohm), 1, 0xFFFF)
        self.write("OV_ON", [on, off, g, _sat(brake_avg_w, 0, 0xFFFF),
                             _sat(brake_budget_j * 1e6 / 4096, 0, 0xFFFF)], board=board)
        self.write("HS_LIMIT", 0 if hot_swap_limit_v is None else cal.bus_code(hot_swap_limit_v), board=board)

    # -- battery model -----------------------------------------------------------------------
    def set_battery(self, model: BatteryModel, *, board: int = 0, run: bool = True,
                    pv_set: bool = False) -> None:
        """Load a :class:`BatteryModel` and (``run``) start it; ``pv_set`` also drives the PV
        board's setpoint input from it (board 0)."""
        cal = self.calibration
        if model.capacity_ah <= 0 or len(model.ocv) < 2 or len(model.ocv) > 64:
            raise ValueError("the model needs a positive capacity and 2-64 OCV points")
        if not 0.0 <= model.soc <= 1.0:
            raise ValueError("soc is 0-1")
        total = model.capacity_ah * 3600 * cal.sample_rate_hz * cal.codes_per_amp   # code-samples
        q = max(8, math.ceil(math.log2(total / 63)))
        if q > 47:
            raise ValueError(f"{model.capacity_ah} Ah is too large for the model")
        entry = 2.0 ** q / total                       # fraction of the capacity per table entry
        table = [cal.bus_code(_interp(model.ocv, min(1.0, k * entry))) for k in range(64)]
        alpha = 0 if model.r1_ohm == 0 else _sat(1e-3 / model.tau_s * 2 ** 20, 1, 0xFFFF)
        if model.r1_ohm and 1e-3 / model.tau_s * 2 ** 20 > 0xFFFF:
            raise ValueError("tau_s must be at least 16 ms")
        v_min = 0.0 if model.v_min is None else model.v_min
        v_max = max(model.ocv) if model.v_max is None else model.v_max
        self.write("BAT_CTRL", 0, board=board)
        self.write("BAT_QSHIFT", [q, cal.resistance_code(model.r0_ohm), cal.resistance_code(model.r1_ohm),
                                  alpha, cal.bus_code(v_min), cal.bus_code(v_max),
                                  _sat((1.0 - model.soc) / entry * 256, 0, 0xFFFF), 0], board=board)
        self.write("BAT_TBL_DATA", table, board=board)
        self.write("BAT_I_OFS", cal.offsets[3], board=board)
        self._qshift[board] = (q, entry)
        self.write("BAT_CTRL", int(run) | int(model.invert_current) << 1 | int(pv_set) << 2, board=board)

    def battery_state(self, board: int = 0) -> BatteryState:
        """The setpoint, the mean current over the last ms, the OCV and the state of charge."""
        pos = self._read(0x67, 1, board)[0]
        sp, i, ocv = (_s16(x) for x in self._read(0x6C, 3, board))
        cal = self.calibration
        entry = self._qshift.get(board, (0, None))[1]
        soc = None if entry is None else max(0.0, 1.0 - pos / 256 * entry)
        return BatteryState(setpoint=sp / cal.codes_per_volt, current=i / cal.codes_per_amp,
                            ocv=ocv / cal.codes_per_volt, soc=soc, position=pos)

    def set_pv_calibration(self, offset: int, gain: int, *, board: int = 0) -> None:
        """PV_SET duty = clamp(offset + setpoint x gain >>> 15, 0, 65535): the PV board's setpoint
        input, from its calibration."""
        self.write("PV_OFS", [_u16(offset), _u16(gain)], board=board)

    # -- logging stream ----------------------------------------------------------------------
    def start_log(self, channels: Sequence[str] = LOG_CHANNELS, *, osr256: bool = False,
                  sequence: bool = True, board: int = 0) -> None:
        """Clear the FIFO and log ``channels`` (names from ``LOG_CHANNELS``) at OSR 64
        (281 kSPS) or OSR 256 (70 kSPS), with a sequence word per set."""
        mask = 0
        for ch in channels:
            if ch not in LOG_CHANNELS:
                raise ValueError(f"unknown log channel {ch!r}; pick from {LOG_CHANNELS}")
            mask |= 1 << LOG_CHANNELS.index(ch)
        if not mask:
            raise ValueError("log at least one channel")
        self.write("LOG_CTRL", 0x8000, board=board)
        self._log[board] = _LogState(mask=mask, seq=sequence)
        self.write("LOG_CTRL", mask | int(osr256) << 5 | 1 << 6 | int(sequence) << 7, board=board)

    def stop_log(self, board: int = 0) -> None:
        self.write("LOG_CTRL", 0, board=board)

    def read_log(self, board: int = 0, *, max_words: int = 65536) -> LogData:
        """Drain what the FIFO holds now (up to ``max_words``) and return the complete sets; a
        partial set waits for the next call."""
        st = self._log.get(board)
        if st is None:
            raise BenchPodError("start_log first")
        level, drops = self._read(0x7A, 2, board)
        n = min(level, max_words)
        while n > 0:
            k = min(n, _READ_WORDS)
            st.pending += self._read(0xC0, k, board)
            n -= k
        out = LogData(channels={c: [] for i, c in enumerate(LOG_CHANNELS) if st.mask >> i & 1},
                      drops=drops)
        per = st.set_words
        whole = len(st.pending) // per * per
        words, st.pending = st.pending[:whole], st.pending[whole:]
        for off in range(0, whole, per):
            k = off
            if st.seq:
                out.seq.append(words[k])
                k += 1
            for c in out.channels:
                out.channels[c].append(_s16(words[k]))
                k += 1
        return out

    # -- board EEPROM ------------------------------------------------------------------------
    def eeprom_read(self, addr: int, length: int, *, board: int = 0, timeout: float = 0.5) -> bytes:
        """Read ``length`` bytes of the board's M24C02 from ``addr``."""
        out = bytearray()
        for a in range(addr, addr + length):
            self.write("EE_ADDR", a & 0xFF, board=board)
            self.write("EE_CMD", 1, board=board)
            self._ee_wait(board, timeout)
            out.append(self._read(0x7D, 1, board)[0] & 0xFF)
        return bytes(out)

    def eeprom_write(self, addr: int, data: bytes, *, board: int = 0, timeout: float = 0.5) -> None:
        """Write bytes to the board's M24C02 (the gateware waits out each write cycle)."""
        for i, b in enumerate(bytes(data)):
            self.write("EE_ADDR", [(addr + i) & 0xFF, b], board=board)
            self._ee_wait(board, timeout)

    def _ee_wait(self, board: int, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            st = self._read(0x7E, 1, board)[0]
            if not st & 1:
                break
            if time.monotonic() >= deadline:
                raise BenchPodError(f"board {board}: the EEPROM stayed busy")
        if st & 2:
            raise BenchPodError(f"board {board}: the EEPROM did not acknowledge")


def _check_board(board: int) -> None:
    if not 0 <= board <= 3:
        raise ValueError("board is 0-3")


def _interp(points: Sequence[float], x: float) -> float:
    """``points`` evenly spaced over 0-1, linearly interpolated at ``x``."""
    pos = x * (len(points) - 1)
    i = min(int(pos), len(points) - 2)
    return points[i] + (points[i + 1] - points[i]) * (pos - i)


__all__ = [
    "MotorEmulator", "EmulatorCalibration", "BatteryModel", "BatteryState", "BoardInfo",
    "EmulatorStatus", "TripState", "Sample", "ConfigResult", "LogData", "REGISTERS",
    "LOG_CHANNELS", "EMULATOR_ID", "MIN_VERSION",
]
