"""BenchPod — a pytest-friendly client for an EmbeddedCI BenchPod device.

Connect over wifi/network or serial, power the target, flash firmware and assert
the result, all from a test::

    from embeddedci import benchpod

    def test_boots(benchpod_device):  # the `benchpod` fixture is also available
        benchpod_device.power_on(benchpod.INTERNAL)
        result = benchpod_device.flash(
            file="firmware.elf", target="target/stm32f1x.cfg",
            swclk=benchpod.PIN1, swdio=benchpod.PIN2, nreset=benchpod.PIN3,
            target_power=benchpod.INTERNAL,
        )
        assert result.ok
"""

from __future__ import annotations

from . import i2c
from .client import BenchPod
from .connection import ConnSpec, parse_connection, resolve_connection
from .i2c import I2CByte, I2CMessage, I2CTransaction
from .constants import (
    BMP280_ADDR_PRIMARY,
    BMP280_ADDR_SECONDARY,
    EXTERNAL,
    INTERNAL,
    PIN1,
    PIN2,
    PIN3,
    PIN4,
    PIN5,
    PIN6,
    PIN7,
    PIN8,
    PIN9,
    PIN10,
    PIN11,
    PIN12,
    Efuse,
    Pin,
    Sensor,
)
from .errors import (
    BenchPodError,
    ConnectionConfigError,
    FirmwareError,
    FlashError,
    TargetUnreachableError,
    TransportError,
    UartTimeout,
)
from .ci import BuildReporter, NoopBuildReporter, make_build_reporter
from .flash import FlashResult
from .uart import UartCapture, UartSession

__all__ = [
    "BenchPod",
    "FlashResult",
    "UartCapture",
    "UartSession",
    "UartTimeout",
    "i2c",
    "I2CByte",
    "I2CMessage",
    "I2CTransaction",
    # connection
    "ConnSpec",
    "resolve_connection",
    "parse_connection",
    # constants
    "Efuse",
    "Pin",
    "Sensor",
    "INTERNAL",
    "EXTERNAL",
    "BMP280_ADDR_PRIMARY",
    "BMP280_ADDR_SECONDARY",
    "PIN1",
    "PIN2",
    "PIN3",
    "PIN4",
    "PIN5",
    "PIN6",
    "PIN7",
    "PIN8",
    "PIN9",
    "PIN10",
    "PIN11",
    "PIN12",
    # errors
    "BenchPodError",
    "ConnectionConfigError",
    "TransportError",
    "FirmwareError",
    "FlashError",
    "TargetUnreachableError",
    # CI build reporting
    "BuildReporter",
    "NoopBuildReporter",
    "make_build_reporter",
]
