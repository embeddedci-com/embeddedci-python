"""Static, named constants and option types for the BenchPod API.

The firmware speaks raw integers — eFuse ``1``/``2`` and LA channels ``1``-``12``.
These :class:`~enum.IntEnum` types give those wire values intuitive names so test
code reads ``benchpod.INTERNAL`` / ``benchpod.PIN1`` instead of bare numbers.
Because they are ``IntEnum``s they serialize as their integer on the wire, and
the coercion helpers below accept either an enum member or a plain int.

The string options the API accepts (analog paths, ADC sources, loop sources, …)
are :data:`typing.Literal` aliases, so a type checker and an IDE know the valid
values and the MCP server turns them into JSON-schema enums. Each has a runtime
tuple next to it (``DAC_PATHS``, ``ADC_SOURCES``, …) that the client validates
against.

Units, everywhere in the public API: **volts**, **seconds**, **hertz**.
"""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import Dict, Literal, Sequence, Tuple, Union, get_args

from .errors import BenchPodError


class Efuse(IntEnum):
    """Target-power eFuse rail."""

    INTERNAL = 1  # internal 5V supply
    EXTERNAL = 2  # external supply


class Sensor(str, Enum):
    """Emulated I2C sensor model (firmware ships BMP280 today)."""

    BMP280 = "bmp280"


class FpgaImage(IntEnum):
    """Gateware images stored in the pod's iCE40 config flash (see ``BenchPod.fpga_image``)."""

    #: The control-loop image (advertises ``Capabilities.dac_control_loop``).
    LOOP = 0
    #: The deep DAC replay image (replay streams from PSRAM).
    DEEP_REPLAY = 1


# Common BMP280 7-bit I2C addresses (datasheet: SDO low / high).
BMP280_ADDR_PRIMARY = 0x76
BMP280_ADDR_SECONDARY = 0x77


class Pin(IntEnum):
    """Logic-analyzer channel (LA1..LA12) on the iCE40 FPGA bank."""

    PIN1 = 1
    PIN2 = 2
    PIN3 = 3
    PIN4 = 4
    PIN5 = 5
    PIN6 = 6
    PIN7 = 7
    PIN8 = 8
    PIN9 = 9
    PIN10 = 10
    PIN11 = 11
    PIN12 = 12


# Module-level aliases, re-exported from ``benchpod`` so callers can write
# ``benchpod.INTERNAL`` and ``benchpod.PIN1``.
INTERNAL = Efuse.INTERNAL
EXTERNAL = Efuse.EXTERNAL

PIN1 = Pin.PIN1
PIN2 = Pin.PIN2
PIN3 = Pin.PIN3
PIN4 = Pin.PIN4
PIN5 = Pin.PIN5
PIN6 = Pin.PIN6
PIN7 = Pin.PIN7
PIN8 = Pin.PIN8
PIN9 = Pin.PIN9
PIN10 = Pin.PIN10
PIN11 = Pin.PIN11
PIN12 = Pin.PIN12


# -- string option types -------------------------------------------------------

#: A DAC output path. ``12v`` is the bipolar ±12 V output.
DacPath = Literal["3v3", "5v", "12v"]
#: A DAC output path, or ``off`` to park the output (``BenchPod.dac_output``).
DacOutputPath = Literal["3v3", "5v", "12v", "off"]
#: A named analog path: one fully specified mux + relay state (``BenchPod.analog_path``).
AnalogPath = Literal["off", "dac_3v3", "dac_5v", "dac_12v", "adc_ext", "cal1", "cal2", "amp"]
#: Where the ADC reads from: front SMA (``ext``), the two internal DAC loopbacks, or the amps terminal.
AdcSource = Literal["ext", "cal1", "cal2", "amp"]
#: Parametric DAC waveforms the firmware generator produces.
Waveshape = Literal["sine", "square", "sawtooth"]
#: How replayed volts map onto DAC codes: reproduce them (``faithful``) or auto-scale (``fit``).
ReplayMapping = Literal["faithful", "fit"]
#: Where the in-fabric control loop takes its input from.
LoopSource = Literal["adc", "fixed", "sweep"]
#: Protocols the off-device LA decoder understands.
DecodeProtocol = Literal["i2c", "uart", "spi"]
#: FDCAN operating mode; ``internal``/``external`` are loopbacks for a lone pod.
CanMode = Literal["normal", "internal", "external", "listen"]
#: A fault spliced into a replayed waveform.
FaultType = Literal["flatline", "spike", "stuck"]

DAC_PATHS: Tuple[str, ...] = get_args(DacPath)
DAC_OUTPUT_PATHS: Tuple[str, ...] = get_args(DacOutputPath)
ANALOG_PATHS: Tuple[str, ...] = get_args(AnalogPath)
ADC_SOURCES: Tuple[str, ...] = get_args(AdcSource)
WAVESHAPES: Tuple[str, ...] = get_args(Waveshape)
REPLAY_MAPPINGS: Tuple[str, ...] = get_args(ReplayMapping)
LOOP_SOURCES: Tuple[str, ...] = get_args(LoopSource)
DECODE_PROTOCOLS: Tuple[str, ...] = get_args(DecodeProtocol)
CAN_MODES: Tuple[str, ...] = get_args(CanMode)
FAULT_TYPES: Tuple[str, ...] = get_args(FaultType)

#: The analog path each ADC source routes (``capture_adc(source=...)``).
ADC_SOURCE_PATHS: Dict[str, str] = {"ext": "adc_ext", "cal1": "cal1", "cal2": "cal2", "amp": "amp"}

#: LA I/O-bank voltages the pod supports (volts). 1.8 V needs a rev3 pod.
LA_VOLTAGES: Tuple[float, ...] = (1.8, 3.3)

#: Fixed bias network per LA channel (LA1..LA8). LA1-LA6 pull UP to +3V3, LA7/LA8 pull
#: DOWN; LA9-LA12 have none. Source of truth: firmware ``i2c_bus.c``.
PULL_OHMS: Dict[int, str] = {
    1: "4.7k", 2: "4.7k", 3: "2.2k", 4: "2.2k",
    5: "10k", 6: "10k", 7: "10k", 8: "10k",
}
PULLUP_CHANNELS: Tuple[int, ...] = (1, 2, 3, 4, 5, 6)
PULLDOWN_CHANNELS: Tuple[int, ...] = (7, 8)


def check_choice(value: str, choices: Sequence[str], name: str) -> str:
    """Return ``value`` if it is one of ``choices``, else raise a :class:`ValueError` naming them."""
    if value not in choices:
        raise ValueError(f"{name} must be one of {', '.join(repr(c) for c in choices)}; got {value!r}")
    return value


def coerce_efuse(value: Union[Efuse, int]) -> int:
    """Validate and normalize an eFuse selector to ``1`` or ``2`` (:class:`ValueError` otherwise)."""
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"efuse must be 1 (INTERNAL) or 2 (EXTERNAL), got {value!r}") from None
    if ivalue not in (Efuse.INTERNAL, Efuse.EXTERNAL):
        raise ValueError(f"efuse must be 1 (INTERNAL) or 2 (EXTERNAL), got {value!r}")
    return ivalue


def coerce_pin(value: Union[Pin, int], name: str = "pin") -> int:
    """Validate and normalize an LA pin selector to ``1``..``12`` (:class:`ValueError` otherwise)."""
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an LA pin 1-12 (e.g. benchpod.PIN1), got {value!r}") from None
    if not 1 <= ivalue <= 12:
        raise ValueError(f"{name} must be an LA pin 1-12 (e.g. benchpod.PIN1), got {value!r}")
    return ivalue
