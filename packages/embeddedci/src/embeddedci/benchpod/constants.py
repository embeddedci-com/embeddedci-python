"""Static, named constants for the BenchPod API.

The firmware speaks raw integers — eFuse ``1``/``2`` and LA channels ``1``-``12``.
These :class:`~enum.IntEnum` types give those wire values intuitive names so test
code reads ``benchpod.INTERNAL`` / ``benchpod.PIN1`` instead of bare numbers.
Because they are ``IntEnum``s they serialize as their integer on the wire, and
the coercion helpers below accept either an enum member or a plain int.
"""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import Union

from .errors import BenchPodError


class Efuse(IntEnum):
    """Target-power eFuse rail."""

    INTERNAL = 1  # internal 5V supply
    EXTERNAL = 2  # external supply


class Sensor(str, Enum):
    """Emulated I2C sensor model (firmware ships BMP280 today)."""

    BMP280 = "bmp280"


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


def coerce_efuse(value: Union[Efuse, int]) -> int:
    """Validate and normalize an eFuse selector to ``1`` or ``2``."""
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        raise BenchPodError(f"efuse must be 1 (INTERNAL) or 2 (EXTERNAL), got {value!r}") from None
    if ivalue not in (Efuse.INTERNAL, Efuse.EXTERNAL):
        raise BenchPodError(
            f"efuse must be 1 (INTERNAL) or 2 (EXTERNAL), got {value!r}"
        )
    return ivalue


def coerce_pin(value: Union[Pin, int], name: str = "pin") -> int:
    """Validate and normalize an LA pin selector to an int in ``1``..``12``."""
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        raise BenchPodError(f"{name} must be an LA pin 1-12 (e.g. benchpod.PIN1), got {value!r}") from None
    if not 1 <= ivalue <= 12:
        raise BenchPodError(
            f"{name} must be an LA pin 1-12 (e.g. benchpod.PIN1), got {value!r}"
        )
    return ivalue
