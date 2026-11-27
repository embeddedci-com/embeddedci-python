"""GPIO expander boards: many more GPIOs over I2C or SPI on a few LA pins.

Attach an off-the-shelf expander breakout with :meth:`BenchPod.expansion
<embeddedci.benchpod.BenchPod.expansion>` and its pins become ``EXT1``..``EXTn``, which the
ordinary GPIO calls take next to the LA channels::

    bp.expansion("tca9555", addr=0x20, sda=1, scl=2)     # probes the chip: EXT1..EXT16
    led = bp.gpio("EXT1", "output")
    led.high()
    bp.set_gpio(["EXT2", "EXT3", "EXT4"], [1, 0, 1])     # one bus transaction
    print(bp.read_gpio("EXT9"))
    bp.expansion(None)                                   # detach: every EXT pin back to input

The host SDK drives the chip through the pod's I2C or SPI master, so EXT pins are as fast as one
bus transaction per call (a few milliseconds over the LAN), not FPGA-timed: pulses, LA captures and
triggers work on LA pins only. :data:`EXPANSION_CHIPS` lists the chips the SDK drives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .errors import BenchPodError

if TYPE_CHECKING:  # pragma: no cover
    from .client import BenchPod
    from .i2c_master import I2CMaster
    from .spi import SpiSession

#: The SDK release that first drives each chip (:attr:`ExpansionChip.since`).
_FIRST = "2.13.21"

_EXT_RE = re.compile(r"^\s*ext(\d+)\s*$", re.IGNORECASE)


class ExpansionError(BenchPodError):
    """An expander could not be attached or used: no chip answers, the LA voltage is out of the
    chip's range, the bus is held by another session, or a call that works on LA pins only was
    given an EXT pin."""


@dataclass(frozen=True)
class ExpansionChip:
    """One expander chip the SDK drives (an entry of :data:`EXPANSION_CHIPS`)."""

    #: The name :meth:`BenchPod.expansion` takes, lower case (``"tca9555"``).
    name: str
    #: ``"i2c"`` or ``"spi"``.
    bus: str
    #: GPIO pins one chip adds.
    pins: int
    #: LA channels the bus uses (SPI chips: the pod's SPI master always takes four).
    la_pins: int
    #: Addresses the chip can have: 7-bit I2C addresses, or the MCP23S17's hardware address 0-7.
    #: Empty for a shift-register chain, which has none.
    addrs: Tuple[int, ...]
    #: The LA voltages (volts) the chip works at, powered from that same voltage.
    voltages: Tuple[float, ...]
    #: ``"in/out"`` (each pin input, push-pull output or open drain), ``"quasi"`` (a high output
    #: is a weak pull-up, about 100 µA), ``"out"`` (outputs only) or ``"in"`` (inputs only).
    directions: str
    #: The fastest bus clock in hertz the SDK runs it at.
    max_hz: int
    #: The SDK version that first drives it.
    since: str
    #: Chips of the same family that the same driver runs.
    aliases: Tuple[str, ...] = ()
    #: One line for the docs.
    note: str = ""
    #: ``"gpio"`` (adds EXT pins) or ``"current"`` (an INA current/power monitor: no pins, read
    #: with ``measure_power(monitor=...)``, ``power_status()`` and ``power_profile(monitor=...)``).
    kind: str = "gpio"
    #: Current monitors: channels one chip has, the shunt inside the chip in ohms (0: external),
    #: the largest shunt voltage in volts (0: no limit checked), and ``(register, mask, value)``
    #: ID registers probed on attach (manufacturer and die ID where the chip has them).
    channels: int = 1
    internal_ohm: float = 0.0
    shunt_max: float = 0.0
    ids: Tuple[Tuple[int, int, int], ...] = ()

    @property
    def chained(self) -> bool:
        """True for a shift-register chain (``count`` chips on one CS)."""
        return not self.addrs


def _range(lo: int, hi: int) -> Tuple[int, ...]:
    return tuple(range(lo, hi + 1))


_BOTH = (1.8, 3.3)
_INA_V = (3.3, 5.0)
_NEW = "4.0.7"
_TI = (0xFE, 0xFFFF, 0x5449)     # manufacturer ID (INA226, INA260, INA3221)
_TI_B = (0x3E, 0xFFFF, 0x5449)   # the same at 0x3E (INA228, INA238)

#: Every expander chip the SDK drives, in the order the docs list them. The one source of truth for
#: the docs page and the MCP/CLI help.
EXPANSION_CHIPS: Tuple[ExpansionChip, ...] = (
    ExpansionChip("tca9555", "i2c", 16, 2, _range(0x20, 0x27), _BOTH, "in/out", 400_000, _FIRST,
                  aliases=("tca9535",), note="TI, 1.65-5.5 V"),
    ExpansionChip("pca9555", "i2c", 16, 2, _range(0x20, 0x27), (3.3,), "in/out", 400_000, _FIRST,
                  aliases=("pca9535",), note="NXP, 2.3-5.5 V: not at 1.8 V"),
    ExpansionChip("mcp23017", "i2c", 16, 2, _range(0x20, 0x27), _BOTH, "in/out", 400_000, _FIRST,
                  note="Microchip, 1.8-5.5 V"),
    ExpansionChip("pcf8574", "i2c", 8, 2, _range(0x20, 0x27), (3.3,), "quasi", 100_000, _FIRST,
                  note="NXP/TI, 2.5-6 V: not at 1.8 V; 100 kHz"),
    ExpansionChip("pcf8574a", "i2c", 8, 2, _range(0x38, 0x3F), (3.3,), "quasi", 100_000, _FIRST,
                  note="the PCF8574 at addresses 0x38-0x3f"),
    ExpansionChip("pcf8575", "i2c", 16, 2, _range(0x20, 0x27), (3.3,), "quasi", 400_000, _FIRST,
                  note="NXP/TI, 2.5-5.5 V: not at 1.8 V"),
    ExpansionChip("tca6416a", "i2c", 16, 2, (0x20, 0x21), _BOTH, "in/out", 400_000, _FIRST,
                  note="TI, 1.65-5.5 V, level-shifting (VCCI = the LA voltage)"),
    ExpansionChip("tca6424a", "i2c", 24, 2, (0x22, 0x23), _BOTH, "in/out", 400_000, _FIRST,
                  note="TI, 1.65-5.5 V, level-shifting (VCCI = the LA voltage)"),
    ExpansionChip("mcp23s17", "spi", 16, 4, _range(0, 7), _BOTH, "in/out", 6_000_000, _FIRST,
                  note="Microchip, SPI; several on one CS by hardware address (A0-A2)"),
    ExpansionChip("74hc595", "spi", 8, 4, (), (3.3,), "out", 6_000_000, _FIRST,
                  note="shift-register chain, outputs only; CS to RCLK (latch), MISO a free pin"),
    ExpansionChip("74hc165", "spi", 8, 5, (), (3.3,), "in", 6_000_000, _FIRST,
                  note="shift-register chain, inputs only; CS to CLK INH, load= to SH/LD, MOSI a "
                       "free pin"),
    # INA current/power monitors (kind "current"): a shunt in series with the DUT's supply, read with
    # measure_power(monitor=...). All run from 2.7-5.5 V (INA219: 3-5.5 V): not at 1.8 V.
    ExpansionChip("ina226", "i2c", 0, 2, _range(0x40, 0x4F), _INA_V, "", 400_000, _NEW, kind="current",
                  shunt_max=81.92e-3, ids=(_TI, (0xFF, 0xFFFF, 0x2260)),
                  note="current monitor: 16-bit, shunt 2.5 µV/step (0.25 µA with 10 ohm), +-81.92 mV, "
                       "bus to 36 V"),
    ExpansionChip("ina228", "i2c", 0, 2, _range(0x40, 0x4F), _INA_V, "", 400_000, _NEW, kind="current",
                  shunt_max=163.84e-3, ids=(_TI_B, (0x3F, 0xFFF0, 0x2280)),
                  note="current monitor: 20-bit, shunt 78 nV/step on the +-40.96 mV range, bus to 85 V"),
    ExpansionChip("ina238", "i2c", 0, 2, _range(0x40, 0x4F), _INA_V, "", 400_000, _NEW, kind="current",
                  shunt_max=163.84e-3, ids=(_TI_B, (0x3F, 0xFFF0, 0x2380)),
                  note="current monitor: 16-bit, shunt 1.25 µV/step on the +-40.96 mV range, bus to 85 V"),
    ExpansionChip("ina219", "i2c", 0, 2, _range(0x40, 0x4F), _INA_V, "", 400_000, _NEW, kind="current",
                  shunt_max=320e-3,
                  note="current monitor: shunt 10 µV/step (1 µA with 10 ohm), +-320 mV, bus to 32 V; "
                       "3-5.5 V; no ID register, so only the address is probed"),
    ExpansionChip("ina260", "i2c", 0, 2, _range(0x40, 0x4F), _INA_V, "", 400_000, _NEW, kind="current",
                  internal_ohm=0.002, ids=(_TI, (0xFF, 0xFFF0, 0x2270)),
                  note="current monitor, internal 2 mohm shunt, 1.25 mA/step, +-15 A: for amps, not µA"),
    ExpansionChip("ina3221", "i2c", 0, 2, _range(0x40, 0x43), _INA_V, "", 400_000, _NEW, kind="current",
                  channels=3, shunt_max=163.8e-3, ids=(_TI, (0xFF, 0xFFFF, 0x3220)),
                  note="current monitor, 3 channels, shunt 40 µV/step (4 µA with 10 ohm), bus to 26 V; "
                       "averaging and conversion time apply to all three"),
)

#: Chips in one shift-register chain at most (one SPI transfer).
CHAIN_MAX = 32


def expansion_chip(name: str) -> ExpansionChip:
    """The :class:`ExpansionChip` called ``name`` (or one of its aliases), case-insensitive."""
    key = str(name or "").strip().lower()
    for chip in EXPANSION_CHIPS:
        if key == chip.name or key in chip.aliases:
            return chip
    known = ", ".join(c.name for c in EXPANSION_CHIPS)
    raise ValueError(f"unknown expander chip {name!r}; the SDK drives: {known}")


def ext_index(value: Any) -> Optional[int]:
    """``n`` for ``"EXTn"`` (case-insensitive), else ``None``."""
    if isinstance(value, str):
        m = _EXT_RE.match(value)
        if m:
            return int(m.group(1))
    return None


# -- state ---------------------------------------------------------------------------------------

#: :attr:`ExtPinState.mode` values: ``none`` is an unclaimed pin (an input, like LA mode).
EXT_MODES = ("none", "input", "output", "open_drain")


@dataclass(frozen=True)
class ExtPinState:
    """One expander pin (``EXTn``) as :meth:`BenchPod.expansion` reports it."""

    #: ``n`` of ``EXTn``.
    ext: int
    #: ``"EXTn"``.
    pin: str
    #: Its name from ``names=`` or the wiring profile, else ``None``.
    name: Optional[str]
    #: The pin on the chip (``"P07"``, ``"GPB0"``, ``"2.QA"``).
    chip_pin: str
    #: The chip's I2C address (or MCP23S17 hardware address); ``None`` for a chain.
    addr: Optional[int]
    #: ``none``, ``input``, ``output`` or ``open_drain``.
    mode: str
    #: The commanded level of an output or open-drain pin.
    level: Optional[int] = None
    #: The pin's live level, when it was read.
    live: Optional[int] = None


@dataclass(frozen=True)
class ExpansionStatus:
    """What :meth:`BenchPod.expansion` returns: the attached expander, or ``attached=False``."""

    attached: bool
    chip: Optional[str] = None
    bus: Optional[str] = None
    #: The LA channels it uses: ``{"sda": 1, "scl": 2}`` or ``{"sck": .., "mosi": .., "miso": ..,
    #: "cs": .., "load": ..}``.
    la: Dict[str, int] = field(default_factory=dict)
    #: The chips' addresses, in EXT order (empty for a chain).
    addrs: Tuple[int, ...] = ()
    #: Chips in a shift-register chain (1 for an addressed chip).
    count: int = 0
    hz: int = 0
    #: ``"pod"`` (the pod's pull-ups, switched on by the SDK), ``"external"`` or ``None`` (SPI).
    pullups: Optional[str] = None
    pins: Tuple[ExtPinState, ...] = ()
    #: Things worth knowing (a quasi-bidirectional chip's weak high, levels that were not read).
    notes: Tuple[str, ...] = ()
    #: The attached INA current monitors (:class:`~embeddedci.benchpod.current_monitor.MonitorStatus`).
    monitors: Tuple[Any, ...] = ()

    @property
    def names(self) -> List[str]:
        """``["EXT1", ...]``, in order."""
        return [p.pin for p in self.pins]


# -- buses ---------------------------------------------------------------------------------------

class _I2CDev:
    """One I2C chip: register writes and reads at ``addr``."""

    def __init__(self, bus: Callable[[], "I2CMaster"], addr: int) -> None:
        self._bus = bus
        self.addr = addr

    def write(self, data: bytes) -> None:
        self._bus().write(self.addr, data)

    def write_read(self, data: bytes, n: int) -> bytes:
        return self._bus().write_read(self.addr, data, n)

    def read(self, n: int) -> bytes:
        return self._bus().read(self.addr, n)


class _SpiDev:
    """The SPI chip(s) on the expander's CS."""

    def __init__(self, bus: Callable[[], "SpiSession"]) -> None:
        self._bus = bus

    def transfer(self, data: bytes) -> bytes:
        return self._bus().transfer(data)


# -- chip drivers --------------------------------------------------------------------------------
#
# A driver keeps no state of its own: the Expansion hands it each device's two masks, ``dir_in``
# (1 = the pin is an input / released) and ``out`` (the output latch), and it writes the registers
# that changed. Bit i is the device's pin i (EXT base + i).

class _Driver:
    def __init__(self, chip: ExpansionChip, pins: int) -> None:
        self.chip = chip
        self.pins = pins

    def label(self, i: int) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def init(self, dev: Any) -> Tuple[int, int]:
        """Put every pin in its released state and return ``(dir_in, out)``."""
        raise NotImplementedError  # pragma: no cover

    def apply(self, dev: Any, old: Tuple[int, int], new: Tuple[int, int]) -> None:
        raise NotImplementedError  # pragma: no cover

    def read(self, dev: Any, state: Tuple[int, int]) -> int:
        raise NotImplementedError  # pragma: no cover


def _le(value: int, n: int) -> bytes:
    return value.to_bytes(n, "little")


class _TcaDriver(_Driver):
    """TCA9555 / PCA9555 / TCA9535 / TCA6416A (and, with ``ports=3``, TCA6424A): input, output,
    polarity and configuration registers, one byte per 8-pin port, configuration 1 = input."""

    def __init__(self, chip: ExpansionChip, *, ports: int = 2, stride: int = 2, ai: int = 0) -> None:
        super().__init__(chip, 8 * ports)
        self.ports = ports
        self.inp, self.out, self.pol, self.cfg = 0, stride, 2 * stride, 3 * stride
        self.ai = ai  # auto-increment flag of the command byte (TCA6424A: 0x80)

    def label(self, i: int) -> str:
        return f"P{i // 8}{i % 8}"

    def _w(self, dev: _I2CDev, reg: int, value: int) -> None:
        dev.write(bytes([reg | self.ai]) + _le(value, self.ports))

    def init(self, dev: _I2CDev) -> Tuple[int, int]:
        full = (1 << self.pins) - 1
        self._w(dev, self.pol, 0)
        self._w(dev, self.cfg, full)
        return full, int.from_bytes(dev.write_read(bytes([self.out | self.ai]), self.ports), "little")

    def apply(self, dev: _I2CDev, old: Tuple[int, int], new: Tuple[int, int]) -> None:
        # The latch first, so a pin turning into an output starts at its level.
        if new[1] != old[1]:
            self._w(dev, self.out, new[1])
        if new[0] != old[0]:
            self._w(dev, self.cfg, new[0])

    def read(self, dev: _I2CDev, state: Tuple[int, int]) -> int:
        return int.from_bytes(dev.write_read(bytes([self.inp | self.ai]), self.ports), "little")


class _McpRegs:
    """MCP23x17 registers with IOCON.BANK = 0 (A and B side by side)."""

    IODIR, IPOL, GPINTEN, IOCON, GPPU, GPIO, OLAT = 0x00, 0x02, 0x04, 0x0A, 0x0C, 0x12, 0x14
    #: IOCON in BANK = 1, GPINTENB in BANK = 0: writing 0 there sets BANK = 0 either way.
    BANK1_IOCON = 0x05


class _Mcp23017Driver(_Driver):
    def __init__(self, chip: ExpansionChip) -> None:
        super().__init__(chip, 16)

    def label(self, i: int) -> str:
        return f"GP{'AB'[i // 8]}{i % 8}"

    def init(self, dev: _I2CDev) -> Tuple[int, int]:
        r = _McpRegs
        if isinstance(dev, _I2CDev):
            dev.write(bytes([r.BANK1_IOCON, 0x00]))
            dev.write(bytes([r.IOCON, 0x00]))       # BANK 0, sequential addressing
        dev.write(bytes([r.IPOL, 0, 0]))
        dev.write(bytes([r.GPINTEN, 0, 0]))
        dev.write(bytes([r.GPPU, 0, 0]))
        dev.write(bytes([r.IODIR, 0xFF, 0xFF]))
        return 0xFFFF, int.from_bytes(dev.write_read(bytes([r.OLAT]), 2), "little")

    def apply(self, dev: _I2CDev, old: Tuple[int, int], new: Tuple[int, int]) -> None:
        if new[1] != old[1]:
            dev.write(bytes([_McpRegs.OLAT]) + _le(new[1], 2))
        if new[0] != old[0]:
            dev.write(bytes([_McpRegs.IODIR]) + _le(new[0], 2))

    def read(self, dev: _I2CDev, state: Tuple[int, int]) -> int:
        return int.from_bytes(dev.write_read(bytes([_McpRegs.GPIO]), 2), "little")


class _PcfDriver(_Driver):
    """PCF8574 / PCF8575: one quasi-bidirectional port register; 1 = weak high (input), 0 = low."""

    def __init__(self, chip: ExpansionChip) -> None:
        super().__init__(chip, chip.pins)
        self.ports = chip.pins // 8

    def label(self, i: int) -> str:
        return f"P{i}" if self.ports == 1 else f"P{i // 8}{i % 8}"

    def init(self, dev: _I2CDev) -> Tuple[int, int]:
        full = (1 << self.pins) - 1
        dev.write(_le(full, self.ports))
        return full, 0

    def apply(self, dev: _I2CDev, old: Tuple[int, int], new: Tuple[int, int]) -> None:
        if (new[0] | new[1]) != (old[0] | old[1]):
            dev.write(_le(new[0] | new[1], self.ports))

    def read(self, dev: _I2CDev, state: Tuple[int, int]) -> int:
        return int.from_bytes(dev.read(self.ports), "little")


class _McpSpiDev:
    """One MCP23S17 on the expander's CS, by hardware address (opcode 0100 A2 A1 A0 R/W)."""

    def __init__(self, bus: Callable[[], "SpiSession"], hw: int) -> None:
        self._bus = bus
        self.addr = hw

    def write(self, data: bytes, *, hw: Optional[int] = None) -> None:
        a = self.addr if hw is None else hw
        self._bus().transfer(bytes([0x40 | a << 1]) + data)

    def write_read(self, data: bytes, n: int) -> bytes:
        rx = self._bus().transfer(bytes([0x41 | self.addr << 1]) + data + bytes(n))
        return rx[1 + len(data):]


class _Mcp23s17Driver(_Mcp23017Driver):
    DEFVAL = 0x06

    def init(self, dev: _McpSpiDev) -> Tuple[int, int]:  # type: ignore[override]
        r = _McpRegs
        # IOCON.HAEN = 1 so the hardware address counts. A chip with HAEN = 0 answers address 0
        # whatever its pins say, so that write goes to address 0 too.
        for hw in sorted({0, dev.addr}):
            dev.write(bytes([r.BANK1_IOCON, 0x00]), hw=hw)
            dev.write(bytes([r.IOCON, 0x08]), hw=hw)
        # DEFVAL only matters with interrupts on (off here): a safe register to probe with.
        dev.write(bytes([self.DEFVAL, 0xA5, 0x5A]))
        got = dev.write_read(bytes([self.DEFVAL]), 2)
        dev.write(bytes([self.DEFVAL, 0, 0]))
        if got != b"\xa5\x5a":
            raise ExpansionError(f"no mcp23s17 answers at hardware address {dev.addr} (a test "
                                 f"register read back {got.hex() or 'nothing'}, not a55a). Check "
                                 "SCK, MOSI, MISO, CS, the A0-A2 pins and the chip's power")
        return super().init(dev)  # type: ignore[arg-type]


class _Shift595Driver(_Driver):
    """A 74HC595 chain: bytes shifted out MSB first, the last byte into the first chip; CS rising
    latches them (RCLK). EXT1 is the first chip's QA."""

    def __init__(self, chip: ExpansionChip, count: int) -> None:
        super().__init__(chip, 8 * count)
        self.count = count

    def label(self, i: int) -> str:
        return f"{i // 8 + 1}.Q{'ABCDEFGH'[i % 8]}"

    def _write(self, dev: _SpiDev, out: int) -> None:
        dev.transfer(bytes(reversed(_le(out, self.count))))

    def init(self, dev: _SpiDev) -> Tuple[int, int]:
        self._write(dev, 0)
        return 0, 0

    def apply(self, dev: _SpiDev, old: Tuple[int, int], new: Tuple[int, int]) -> None:
        if new[1] != old[1]:
            self._write(dev, new[1])

    def read(self, dev: _SpiDev, state: Tuple[int, int]) -> int:
        return state[1]  # no read-back: the last value written


class _ShiftInDev(_SpiDev):
    def __init__(self, bus: Callable[[], "SpiSession"], load: Callable[[], None]) -> None:
        super().__init__(bus)
        self.load = load


class _Shift165Driver(_Driver):
    """A 74HC165 chain: SH/LD pulsed low loads the inputs, then the first chip's H comes out first.
    EXT1 is the first chip's A (the chip whose QH drives MISO)."""

    def __init__(self, chip: ExpansionChip, count: int) -> None:
        super().__init__(chip, 8 * count)
        self.count = count

    def label(self, i: int) -> str:
        return f"{i // 8 + 1}.{'ABCDEFGH'[i % 8]}"

    def init(self, dev: _ShiftInDev) -> Tuple[int, int]:
        return (1 << self.pins) - 1, 0

    def apply(self, dev: _ShiftInDev, old: Tuple[int, int], new: Tuple[int, int]) -> None:
        pass  # inputs only

    def read(self, dev: _ShiftInDev, state: Tuple[int, int]) -> int:
        dev.load()
        return int.from_bytes(dev.transfer(bytes(self.count)), "little")


def _driver(chip: ExpansionChip, count: int) -> _Driver:
    if chip.name in ("tca9555", "pca9555", "tca6416a"):
        return _TcaDriver(chip)
    if chip.name == "tca6424a":
        return _TcaDriver(chip, ports=3, stride=4, ai=0x80)
    if chip.name == "mcp23017":
        return _Mcp23017Driver(chip)
    if chip.name == "mcp23s17":
        return _Mcp23s17Driver(chip)
    if chip.name in ("pcf8574", "pcf8574a", "pcf8575"):
        return _PcfDriver(chip)
    if chip.name == "74hc595":
        return _Shift595Driver(chip, count)
    if chip.name == "74hc165":
        return _Shift165Driver(chip, count)
    raise ValueError(f"no driver for {chip.name}")  # pragma: no cover - EXPANSION_CHIPS is closed


# -- the attached expander -----------------------------------------------------------------------

class _Device:
    def __init__(self, dev: Any, addr: Optional[int], base: int) -> None:
        self.dev = dev
        self.addr = addr
        #: EXT number of the device's pin 0.
        self.base = base
        self.dir_in = 0
        self.out = 0


class Expansion:
    """The expander attached to a :class:`~embeddedci.benchpod.BenchPod` (internal: use
    :meth:`BenchPod.expansion <embeddedci.benchpod.BenchPod.expansion>` and the GPIO calls)."""

    def __init__(self, pod: "BenchPod", chip: ExpansionChip, *, addrs: Sequence[int], count: int,
                 la: Dict[str, int], hz: int, pullups: Optional[str],
                 names: Mapping[int, str]) -> None:
        # la: {"sda", "scl"} for I2C; {"sck", "mosi", "miso", "cs"} (+ "load") for SPI.
        self._pod = pod
        self.chip = chip
        self.la = dict(la)
        self.hz = hz
        self.pullups = pullups
        self.count = count
        self.driver = _driver(chip, count)
        #: The pins whose pod pull-ups the SDK switched on for this expander (off on detach).
        self.pod_pullups: Optional[Tuple[int, int]] = None
        #: True once the SDK claimed the 74HC165's load pin as a GPIO output (released on detach).
        self.load_claimed = False
        self._own_spi: Optional["SpiSession"] = None
        self.devices: List[_Device] = []
        base = 1
        if chip.bus == "i2c":
            for addr in addrs:
                self.devices.append(_Device(_I2CDev(self._i2c, addr), addr, base))
                base += self.driver.pins
        elif chip.name == "mcp23s17":
            for hw in addrs:
                self.devices.append(_Device(_McpSpiDev(self._spi, hw), hw, base))
                base += self.driver.pins
        elif chip.name == "74hc165":
            self.devices.append(_Device(_ShiftInDev(self._spi, self._pulse_load), None, base))
            base += self.driver.pins
        else:
            self.devices.append(_Device(_SpiDev(self._spi), None, base))
            base += self.driver.pins
        self.total = base - 1
        self.names: Dict[int, str] = dict(names)
        self.modes: Dict[int, str] = {n: "none" for n in range(1, self.total + 1)}

    # -- the bus ---------------------------------------------------------------------------------
    def _i2c(self) -> "I2CMaster":
        return self._pod._own_i2c_session(self.la["sda"], self.la["scl"], hz=self.hz,
                                          pullups=self.pullups or "external",
                                          who=f"the {self.chip.name} on EXT pins",
                                          err=ExpansionError)

    def _check_engine_free(self, bus: str) -> None:
        pod = self._pod
        other = pod._i2c_master_ref() if pod._i2c_master_ref is not None else None
        what = "an I2C session open (open_i2c)"
        if other is not None and not other.closed:
            raise ExpansionError(
                f"the {self.chip.name} on EXT pins needs the pod's {bus} master, but this BenchPod "
                f"has {what}, and SPI, I2C and SWD share one engine on the pod: close that session "
                "first")

    def _spi(self) -> "SpiSession":
        pod = self._pod
        pins = (self.la["sck"], self.la["mosi"], self.la["miso"], self.la["cs"])
        theirs = pod._spi_session_ref() if pod._spi_session_ref is not None else None
        if theirs is not None and not theirs.closed:
            if (theirs.sck, theirs.mosi, theirs.miso, theirs.cs) == pins and theirs.mode == 0:
                return theirs
            raise ExpansionError(
                f"this BenchPod has an SPI session open on SCK LA{theirs.sck} / MOSI LA{theirs.mosi} "
                f"/ MISO LA{theirs.miso} / CS LA{theirs.cs}, and the {self.chip.name} is on LA"
                f"{pins[0]} / LA{pins[1]} / LA{pins[2]} / LA{pins[3]}: the pod has one SPI master. "
                "Close that session first")
        self._check_engine_free("SPI")
        if self._own_spi is None or self._own_spi.closed:
            self._own_spi = pod._start_spi(*pins, hz=self.hz, mode=0)
        return self._own_spi

    def _pulse_load(self) -> None:
        """SH/LD low then high: the 74HC165 chain takes its inputs."""
        self._pod.set_gpio(self.la["load"], 0)
        self._pod.set_gpio(self.la["load"], 1)

    def release_bus(self) -> None:
        """Close the SDK's own bus session (not the user's), so flash, open_i2c and open_spi can
        have the pod's bus engine. The next EXT access opens it again."""
        if self.chip.bus == "i2c":
            self._pod._release_own_i2c()
        spi, self._own_spi = self._own_spi, None
        if spi is not None and not spi.closed:
            try:
                spi.close()
            except Exception:
                pass

    # -- lifetime --------------------------------------------------------------------------------
    def attach(self) -> None:
        """Probe every chip and put all its pins in their released state (input; a 74HC595 low)."""
        for d in self.devices:
            if self.chip.bus == "i2c" and not self._i2c().probe(d.addr):  # type: ignore[arg-type]
                found = self._i2c().scan()
                seen = ", ".join(f"0x{a:02x}" for a in found) or "nothing"
                raise ExpansionError(
                    f"no {self.chip.name} answers at 0x{d.addr:02x} on LA{self.la['sda']} SDA / "
                    f"LA{self.la['scl']} SCL (a scan found {seen}). Check the address jumpers, the "
                    "breakout's power (the LA voltage) and its pull-ups")
            d.dir_in, d.out = self.driver.init(d.dev)

    def detach(self) -> None:
        """Every pin back to input, then the SDK's bus session closed."""
        try:
            for d in self.devices:
                rel = self._released(d)
                if (d.dir_in, d.out) != rel:
                    self.driver.apply(d.dev, (d.dir_in, d.out), rel)
                    d.dir_in, d.out = rel
        finally:
            self.release_bus()

    # -- pins ------------------------------------------------------------------------------------
    def resolve(self, value: Any) -> Optional[int]:
        """The EXT number of ``"EXTn"`` or a pin name, else ``None``."""
        n = ext_index(value)
        if n is not None:
            if not 1 <= n <= self.total:
                raise ValueError(f"EXT{n} does not exist: the {self.chip.name} has EXT1..EXT{self.total}")
            return n
        if isinstance(value, str):
            key = value.strip().lower()
            for k, name in self.names.items():
                if name.lower() == key:
                    return k
        return None

    def label(self, n: int) -> str:
        return self.names.get(n) or f"EXT{n}"

    def _device(self, n: int) -> Tuple[_Device, int]:
        for d in self.devices:
            if d.base <= n < d.base + self.driver.pins:
                return d, n - d.base
        raise ValueError(f"EXT{n} does not exist")  # pragma: no cover - resolve() checked it

    def _released(self, d: _Device) -> Tuple[int, int]:
        """Every pin of ``d`` released: inputs, or for an outputs-only chain all low."""
        if self.chip.directions == "out":
            return 0, 0
        return (1 << self.driver.pins) - 1, d.out

    def _check_mode(self, n: int, mode: str) -> None:
        d = self.chip.directions
        if d == "out" and mode in ("input", "open_drain"):
            raise ExpansionError(f"{self.label(n)} is on a {self.chip.name}, which has outputs only "
                                 f"(push-pull): {mode} is not possible; use output")
        if d == "in" and mode in ("output", "open_drain"):
            raise ExpansionError(f"{self.label(n)} is on a {self.chip.name}, which has inputs only")

    def _commit(self, plan: Dict[int, Tuple[int, int]]) -> None:
        """Write the new ``(dir_in, out)`` of each device index in ``plan``: one bus transaction per
        changed register per chip."""
        for i, (dir_in, out) in plan.items():
            d = self.devices[i]
            self.driver.apply(d.dev, (d.dir_in, d.out), (dir_in, out))
            d.dir_in, d.out = dir_in, out

    def _plan(self) -> Dict[int, List[int]]:
        return {i: [d.dir_in, d.out] for i, d in enumerate(self.devices)}

    def configure(self, ext: Sequence[int], mode: str, level: Optional[int]) -> None:
        """Put pins in ``mode`` (``input``/``output``/``open_drain``, or ``none`` to release)."""
        for n in ext:
            self._check_mode(n, mode)
        plan = self._plan()
        for n in ext:
            d, bit = self._device(n)
            i = self.devices.index(d)
            m = 1 << bit
            st = plan[i]
            if mode in ("input", "none"):
                if self.chip.directions == "out":
                    st[1] &= ~m  # a 74HC595 output can't float: released is low
                else:
                    st[0] |= m
            elif mode == "output":
                lvl = 0 if level is None else level
                st[1] = (st[1] | m) if lvl else (st[1] & ~m)
                st[0] &= ~m
            else:  # open_drain: 0 = driven low, 1 = released (input); starts released
                lvl = 1 if level is None else level
                st[1] &= ~m
                st[0] = (st[0] | m) if lvl else (st[0] & ~m)
        self._commit({i: (s[0], s[1]) for i, s in plan.items()})
        for n in ext:
            self.modes[n] = mode

    def set_levels(self, ext: Sequence[int], levels: Sequence[int]) -> None:
        """Drive output and open-drain pins: one bus transaction per chip."""
        plan = self._plan()
        for n, lvl in zip(ext, levels):
            mode = self.modes[n]
            if mode not in ("output", "open_drain"):
                raise ExpansionError(f"{self.label(n)} is not an output (it is {mode}): make it one "
                                     f"with gpio({self.label(n)!r}, 'output') first")
            d, bit = self._device(n)
            st = plan[self.devices.index(d)]
            m = 1 << bit
            if mode == "output":
                st[1] = (st[1] | m) if lvl else (st[1] & ~m)
            else:
                st[0] = (st[0] | m) if lvl else (st[0] & ~m)
        self._commit({i: (s[0], s[1]) for i, s in plan.items()})

    def read(self, ext: Sequence[int]) -> List[int]:
        """Live levels: one bus read per chip involved."""
        cache: Dict[int, int] = {}
        out = []
        for n in ext:
            d, bit = self._device(n)
            i = self.devices.index(d)
            if i not in cache:
                cache[i] = self.driver.read(d.dev, (d.dir_in, d.out))
            out.append((cache[i] >> bit) & 1)
        return out

    def commanded(self, n: int) -> Optional[int]:
        mode = self.modes[n]
        d, bit = self._device(n)
        if mode == "output":
            return (d.out >> bit) & 1
        if mode == "open_drain":
            return (d.dir_in >> bit) & 1
        return None

    def pin_state(self, n: int, live: Optional[int] = None) -> ExtPinState:
        d, bit = self._device(n)
        return ExtPinState(ext=n, pin=f"EXT{n}", name=self.names.get(n),
                           chip_pin=self.driver.label(bit), addr=d.addr, mode=self.modes[n],
                           level=self.commanded(n), live=live)

    def status(self) -> ExpansionStatus:
        notes: List[str] = []
        live: List[Optional[int]] = [None] * self.total
        try:
            live = list(self.read(range(1, self.total + 1)))
        except Exception as exc:  # the status still says what is attached
            notes.append(f"levels not read: {exc}")
        if self.chip.chained:
            notes.append(f"a {self.chip.name} chain does not answer, so it was not probed: check the "
                         "wiring by driving or reading a pin")
        if self.chip.name == "74hc595":
            notes.append("74HC595 levels are the last values written (it has no read-back)")
        if self.chip.directions == "quasi":
            notes.append(f"the {self.chip.name} is quasi-bidirectional: a high output is a weak "
                         "pull-up (about 100 uA) and cannot source current")
        return ExpansionStatus(
            attached=True, chip=self.chip.name, bus=self.chip.bus, la=dict(self.la),
            addrs=tuple(d.addr for d in self.devices if d.addr is not None), count=self.count,
            hz=self.hz, pullups=self.pullups,
            pins=tuple(self.pin_state(n, live[n - 1]) for n in range(1, self.total + 1)),
            notes=tuple(notes))


def parse_names(names: Union[Mapping[Any, Any], Sequence[Any], None], total: int) -> Dict[int, str]:
    """``names`` as ``{ext: name}``: a mapping from ``"EXTn"``/``n`` to a name, or a list of names
    in EXT order (``None`` entries are skipped)."""
    out: Dict[int, str] = {}
    if names is None:
        return out
    items: List[Tuple[Any, Any]]
    if isinstance(names, Mapping):
        items = list(names.items())
    elif isinstance(names, (list, tuple)):
        items = [(i + 1, v) for i, v in enumerate(names)]
    else:
        raise ValueError("names must be a dict {'EXT1': 'LED1', ...} or a list of names in EXT order")
    for key, value in items:
        if value is None:
            continue
        n = key if isinstance(key, int) and not isinstance(key, bool) else ext_index(key)
        if n is None or not 1 <= n <= total:
            raise ValueError(f"names: {key!r} is not one of EXT1..EXT{total}")
        name = str(value).strip()
        if not name or ext_index(name) is not None:
            raise ValueError(f"names: {value!r} is not a usable name (empty, or itself EXTn)")
        out[n] = name
    lower = [v.lower() for v in out.values()]
    dup = sorted({v for v in lower if lower.count(v) > 1})
    if dup:
        raise ValueError(f"names: {', '.join(dup)} used for more than one pin")
    return out
