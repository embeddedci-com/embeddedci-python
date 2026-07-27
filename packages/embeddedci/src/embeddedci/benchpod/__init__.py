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

from . import can
from . import control_loop
from . import decode
from . import dsp
from . import i2c
from .can import CanBus, CanFrame
from .capabilities import Capabilities
from .client import BenchPod
from .connection import ConnSpec, parse_connection, resolve_connection
from .control_loop import (
    ControlLoopHandle,
    IVPoint,
    build_constant_curve,
    build_linear_curve,
    build_panel_curve,
    curve_output_at,
    encode_curve_b64url,
    input_percent_to_code,
)
from .decode import SpiFrame, UartFrame
from .i2c import I2CByte, I2CMessage, I2CTransaction
from .replay import Fault, ReplayHandle, Segment
from .results import AnalogCapture, Capture, LaCapture
from .server_api import ServerApi, ServerApiError
from .waveforms import Waveform, WaveformLibrary
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
    CanTimeout,
    ConnectionConfigError,
    DeviceBusyError,
    FirmwareError,
    FlashError,
    TargetUnreachableError,
    TransportError,
    UartTimeout,
)
from .lease import DeviceLease
from .ci import BuildReporter, NoopBuildReporter, make_build_reporter
from .flash import FlashResult
from .uart import UartCapture, UartSession

__all__ = [
    "BenchPod",
    "FlashResult",
    "UartCapture",
    "UartSession",
    "UartTimeout",
    "can",
    "CanBus",
    "CanFrame",
    "CanTimeout",
    "i2c",
    "I2CByte",
    "I2CMessage",
    "I2CTransaction",
    # capture / analysis
    "decode",
    "dsp",
    "Capabilities",
    "Capture",
    "LaCapture",
    "AnalogCapture",
    "UartFrame",
    "SpiFrame",
    # DAC replay + waveform library
    "Fault",
    "Segment",
    "ReplayHandle",
    "Waveform",
    "WaveformLibrary",
    "ServerApi",
    "ServerApiError",
    # closed-loop DAC control (panel/MPPT emulator)
    "control_loop",
    "ControlLoopHandle",
    "IVPoint",
    "build_panel_curve",
    "build_constant_curve",
    "build_linear_curve",
    "curve_output_at",
    "input_percent_to_code",
    "encode_curve_b64url",
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
    "DeviceBusyError",
    # device lease
    "DeviceLease",
    # CI build reporting
    "BuildReporter",
    "NoopBuildReporter",
    "make_build_reporter",
]
