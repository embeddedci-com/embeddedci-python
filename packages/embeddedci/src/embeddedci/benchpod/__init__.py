"""BenchPod — the SDK and pytest plugin for an EmbeddedCI BenchPod device.

Connect over the network, USB or the cloud, power the target, flash firmware and assert on
what it does, all from a test::

    from embeddedci import benchpod

    def test_boots(benchpod_target, firmware):   # fixtures from the pytest plugin
        result = benchpod_target.flash(
            file=firmware, target="target/stm32f4x.cfg",
            swclk=benchpod.PIN11, swdio=benchpod.PIN12, nreset=True, check=False,
        )
        assert result.ok

Everything listed in ``__all__`` is the stable public API (semantic versioning applies);
:meth:`BenchPod.command`, :attr:`BenchPod.transport` and :attr:`BenchPod.lowlevel` are escape
hatches outside it. Units are volts, seconds and hertz throughout.
"""

from __future__ import annotations

from . import can
from . import control_loop
from . import decode
from . import dsp
from . import i2c
from .can import CanBus, CanFrame, CanReadResult
from .capabilities import Capabilities
from .ci import BuildReporter, NoopBuildReporter, make_build_reporter
from .client import BenchPod
from .connection import ConnSpec, parse_connection, resolve_connection
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
    PIN13,
    PIN14,
    AdcSource,
    AnalogPath,
    CanMode,
    DacOutputPath,
    DacPath,
    DecodeProtocol,
    Edge,
    Efuse,
    FaultType,
    FpgaImage,
    GpioMode,
    LoopSource,
    Pin,
    ReplayMapping,
    Sensor,
    TriggerEdge,
    Waveshape,
)
from .control_loop import (
    ControlLoopHandle,
    IVPoint,
    LoopInputMap,
    build_constant_curve,
    build_linear_curve,
    build_panel_curve,
    curve_output_at,
    encode_curve_b64url,
    input_percent_to_code,
)
from .decode import SpiFrame, UartFrame
from .errors import (
    BenchPodError,
    CanTimeout,
    CloudAuthError,
    ConnectionConfigError,
    DeviceBusyError,
    FirmwareError,
    FlashError,
    PinConflictError,
    PullConflictError,
    TargetUnreachableError,
    TransportError,
    TriggerTimeout,
    UartTimeout,
)
from .flash import FlashResult
from .gpio import GpioPin, LaPinState
from .i2c import I2CByte, I2CMessage, I2CTransaction
from .lease import DeviceLease
from .lowlevel import LowLevel
from .power import PowerProfile, PowerProfileSession
from .replay import DacHandle, Fault, ReplayHandle, Segment
from .results import Capture, CorrelatedCapture, LaCapture, Trigger
from .server_api import ServerApi, ServerApiError
from .state import (
    AdcReading,
    AnalogPathState,
    DacOutput,
    EfuseState,
    FpgaImageInfo,
    LaVoltage,
    LoopState,
    PowerStatus,
    PullState,
    RailPower,
    ResetState,
    TargetStatus,
    UsbCcStatus,
)
from .uart import UartCapture, UartSession
from .waveforms import Waveform, WaveformLibrary
from .wiring import Signal, Wiring

__all__ = [
    "BenchPod",
    # wiring profile
    "Wiring",
    "Signal",
    # LA pin modes + GPIO
    "GpioPin",
    "LaPinState",
    # power profiles
    "PowerProfile",
    "PowerProfileSession",
    # device state
    "LaVoltage",
    "EfuseState",
    "TargetStatus",
    "RailPower",
    "PowerStatus",
    "ResetState",
    "UsbCcStatus",
    "PullState",
    "AnalogPathState",
    "DacOutput",
    "AdcReading",
    "FpgaImageInfo",
    "LoopState",
    # flash + UART
    "FlashResult",
    "UartCapture",
    "UartSession",
    # CAN
    "can",
    "CanBus",
    "CanFrame",
    "CanReadResult",
    # I2C
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
    "CorrelatedCapture",
    "Trigger",
    "UartFrame",
    "SpiFrame",
    # DAC output, replay + waveform library
    "DacHandle",
    "ReplayHandle",
    "Fault",
    "Segment",
    "Waveform",
    "WaveformLibrary",
    "ServerApi",
    # in-fabric DAC control loop
    "control_loop",
    "ControlLoopHandle",
    "IVPoint",
    "LoopInputMap",
    "build_panel_curve",
    "build_constant_curve",
    "build_linear_curve",
    "curve_output_at",
    "input_percent_to_code",
    "encode_curve_b64url",
    # low-level escape hatch
    "LowLevel",
    # connection
    "ConnSpec",
    "resolve_connection",
    "parse_connection",
    # constants + option types
    "Efuse",
    "Pin",
    "Sensor",
    "FpgaImage",
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
    "PIN13",
    "PIN14",
    "DacPath",
    "DacOutputPath",
    "AnalogPath",
    "AdcSource",
    "Waveshape",
    "ReplayMapping",
    "LoopSource",
    "DecodeProtocol",
    "Edge",
    "GpioMode",
    "TriggerEdge",
    "CanMode",
    "FaultType",
    # errors
    "BenchPodError",
    "ConnectionConfigError",
    "TransportError",
    "FirmwareError",
    "FlashError",
    "TargetUnreachableError",
    "DeviceBusyError",
    "CloudAuthError",
    "ServerApiError",
    "UartTimeout",
    "CanTimeout",
    "PinConflictError",
    "PullConflictError",
    "TriggerTimeout",
    # device lease
    "DeviceLease",
    # CI build reporting
    "BuildReporter",
    "NoopBuildReporter",
    "make_build_reporter",
]
