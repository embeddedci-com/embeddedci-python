"""Tool result and structured-argument models.

Every tool returns one of these, so FastMCP publishes an output schema for it and clients get
structured content. Units follow the SDK: volts, amps, seconds, hertz.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# -- structured arguments --------------------------------------------------------

class FaultSpec(BaseModel):
    """A fault spliced into a replayed waveform."""

    type: Literal["flatline", "spike", "stuck"]
    start: int = Field(0, ge=0, description="First replay sample the fault covers.")
    width: int = Field(0, ge=0, description="Samples covered; 0 = to the end.")
    level: Optional[int] = Field(None, ge=0, le=65535,
                                 description="Raw DAC code to hold (default per fault type).")


class InputMapSpec(BaseModel):
    """Engineering-units input map for the control loop (gateware >= v30)."""

    mv_per_unit: float = Field(gt=0, description="Sense chain gain at the front SMA, mV per unit.")
    range_min: float = Field(description="Input value mapped to the first curve point.")
    range_max: float = Field(description="Input value mapped to the last curve point.")
    mv_at_zero: float = Field(0.0, description="Sense chain offset, mV at a value of 0.")
    trip: Optional[float] = Field(None, description="Optional trip level, same unit.")


# -- connection ------------------------------------------------------------------

class CapabilitiesInfo(BaseModel):
    board: str = ""
    firmware_version: str = ""
    adc_bits: int = 0
    scope: bool = False
    analyzer: bool = False
    dac_replay: bool = False
    dac_deep_replay: bool = False
    dac_replay_max_samples: int = 0
    dac_cotrig: bool = False
    dac_control_loop: bool = False
    dac_loop_sources: bool = False
    dac_loop_input_map: bool = False
    la_pins: bool = Field(False, description="LA pin ownership and GPIO (la_pins, gpio_* tools).")
    gpio_read: bool = Field(False, description="Live pin levels read directly (else via a short capture).")
    capture_trigger: bool = Field(False, description="Triggered captures (trigger_la on the capture tools).")
    power_profile: bool = Field(False, description="Power profiles (measure_power, power_profile_start).")

    @classmethod
    def from_caps(cls, caps: Any) -> "CapabilitiesInfo":
        return cls(**{name: getattr(caps, name) for name in cls.model_fields})


class SessionInfo(BaseModel):
    uart_open: bool = False
    can_open: bool = False
    power_profile_running: bool = Field(False, description="power_profile_start ran without power_profile_stop.")
    last_adc_capture_samples: Optional[int] = None
    last_la_capture_samples: Optional[int] = None
    idle_disconnect_after: Optional[float] = Field(
        None, description="Seconds of inactivity after which the cloud lease is released.")


class StatusResult(BaseModel):
    connected: bool
    connection: Optional[str] = None
    kind: Optional[str] = Field(None, description="tcp, serial, discover or embeddedci (cloud).")
    leased: bool = False
    la_voltage: Optional[float] = Field(None, description="Selected LA bank voltage; null = not set.")
    capabilities: Optional[CapabilitiesInfo] = None
    session: SessionInfo = Field(default_factory=SessionInfo)
    warnings: List[str] = Field(default_factory=list)
    firmware: Dict[str, Any] = Field(default_factory=dict, description="The pod's raw status report.")


class LaVoltageResult(BaseModel):
    voltage: Optional[float]
    readback: Optional[float] = None


# -- power -----------------------------------------------------------------------

class PowerResult(BaseModel):
    efuse: int
    on: bool
    delay: Optional[float] = None


class RailResult(BaseModel):
    enabled: bool
    fault: bool = Field(description="The eFuse tripped (over-current or short on the target rail).")
    state_valid: bool
    monitor_ok: bool
    bus_voltage: float
    current: float = Field(description="Amps.")


class PowerStatusResult(BaseModel):
    internal: RailResult
    external: RailResult


class ResetResult(BaseModel):
    asserted: bool
    supported: bool = True


# -- flash + UART ------------------------------------------------------------------

class FlashResult(BaseModel):
    ok: bool
    returncode: int
    target_unreachable: bool
    stalled: bool
    stdout_tail: str
    stderr_tail: str


class UartCaptureResult(BaseModel):
    text: str
    matched: bool
    bytes: int
    truncated: bool = Field(description="text keeps the start and the end when output is long.")


class UartSessionResult(BaseModel):
    open: bool
    rx: Optional[int] = None
    tx: Optional[int] = None
    baud: Optional[int] = None


class UartWriteResult(BaseModel):
    written: int


class UartReadResult(BaseModel):
    text: str = Field(description="Everything received since the previous uart_read.")
    matched: Optional[bool] = Field(None, description="Whether until_regex matched (null without one).")
    closed: bool
    overflowed: bool
    truncated: bool


# -- I2C sensor + pulls ---------------------------------------------------------------

class I2cCaptureResult(BaseModel):
    transactions: int
    addresses: List[str]
    trace: str
    truncated: bool
    addressed: Optional[bool] = None
    register_value: Optional[List[int]] = None


class I2cRegsResult(BaseModel):
    start: int
    bytes: List[int]


class PullChannel(BaseModel):
    la: int
    enabled: bool
    direction: str
    ohms: str
    available: bool


class PullStatusResult(BaseModel):
    channels: List[PullChannel]


# -- analog + captures ----------------------------------------------------------------

class AnalogPathResult(BaseModel):
    path: str


class DacOutputResult(BaseModel):
    path: str
    voltage: Optional[float] = Field(description="Voltage produced; null when only routing.")
    code: Optional[int]


class AdcReadResult(BaseModel):
    source: str
    voltage: float
    count: int
    span: int


class AdcCaptureResult(BaseModel):
    samples: int
    sample_rate_hz: float
    duration: float
    source: Optional[str] = None
    mean: float
    min: float
    max: float
    peak_to_peak: float
    rms: float
    rms_ac: float
    dominant_frequency_hz: Optional[float] = None
    envelope_step: float = Field(description="Seconds covered by each envelope point.")
    envelope_min: List[float]
    envelope_max: List[float]


class LaChannelSummary(BaseModel):
    la: int
    initial: int
    final: int
    edges: int
    high_fraction: float
    first_edge: Optional[float] = Field(None, description="Seconds from capture start.")
    est_frequency_hz: Optional[float] = None


class LaCaptureResult(BaseModel):
    samples: int
    sample_rate_hz: float
    duration: float
    channels: List[LaChannelSummary]


class PulseStats(BaseModel):
    count: int
    min: Optional[float] = Field(None, description="Seconds.")
    max: Optional[float] = Field(None, description="Seconds.")
    mean: Optional[float] = Field(None, description="Seconds.")


class LaTimingResult(BaseModel):
    la: int
    edge: str
    edge_times: List[float] = Field(description="Edge timestamps in seconds from the capture start.")
    truncated: bool = Field(description="More edges exist than edge_times lists.")
    frequency_hz: Optional[float] = Field(None, description="From the rising edges; null with fewer than two.")
    duty_cycle: float
    high_pulses: PulseStats
    low_pulses: PulseStats
    to_la: Optional[int] = None
    delay: Optional[float] = Field(None, description=(
        "Seconds from the first `edge` on la (at or after `after`) to the next `to_edge` on to_la; "
        "null when to_la is not given or either edge is missing."))
    resolution: float = Field(description="Timestamp resolution in seconds (one sample).")


class CorrelatedCaptureResult(BaseModel):
    adc: AdcCaptureResult
    la: LaCaptureResult


class DecodeResult(BaseModel):
    protocol: str
    count: int
    items: List[str]
    truncated: bool
    text: Optional[str] = Field(None, description="UART only: the decoded bytes as text.")


# -- DAC ------------------------------------------------------------------------------

class GenerateResult(BaseModel):
    waveform: str
    freq_hz: float
    dac_path: str
    cotrig: bool


class StopResult(BaseModel):
    stopped: bool = True


class ReplayResult(BaseModel):
    samples: int
    sample_rate_hz: float
    dac_path: str
    deep: bool
    cotrig: bool
    switched_image: Optional[Literal["loop", "deep_replay"]] = Field(None, description=(
        "The gateware image this call switched the pod to; null when no switch was needed."))


class WaveformInfo(BaseModel):
    id: str
    name: str
    kind: str
    sample_count: int
    sample_rate_hz: float


class WaveformList(BaseModel):
    waveforms: List[WaveformInfo]


class RecordingResult(BaseModel):
    id: str
    name: str
    kind: str
    sample_count: int


# -- control loop ---------------------------------------------------------------------

class LoopArmResult(BaseModel):
    armed: bool
    k: int
    vmin: int
    vmax: int
    tick_div: int
    curve_points: int
    source: Optional[str]
    input_code: int
    step: int
    switched_image: Optional[Literal["loop", "deep_replay"]] = Field(None, description=(
        "The gateware image this call switched the pod to; null when no switch was needed."))


class LoopStateResult(BaseModel):
    source: Optional[str]
    input_code: int
    step: int
    output_code: int


class LoopProbeResult(BaseModel):
    i: int = Field(description="Latest raw ADC reading.")
    v: int = Field(description="DAC code driven this tick.")
    input_code: Optional[int]
    source: Optional[str]
    loop_input: int = Field(description="The value the loop indexed the curve with.")


class FpgaImageResult(BaseModel):
    image: str
    version: int
    features: int


# -- CAN ------------------------------------------------------------------------------

class CanOpenResult(BaseModel):
    open: bool
    bitrate: Optional[int] = None
    mode: Optional[str] = None
    term: Optional[bool] = None


class CanFrameInfo(BaseModel):
    id: int
    id_hex: str
    data: List[int]
    ext: bool
    rtr: bool
    ts_ms: int


class CanReadResult(BaseModel):
    frames: List[CanFrameInfo]
    matched: Optional[bool] = None


class CanWriteResult(BaseModel):
    queued: bool = True


class CanRespondResult(BaseModel):
    cleared: bool = False
    rules: Optional[int] = None


class CommandResult(BaseModel):
    reply: Any = None


class DeviceReply(BaseModel):
    """A firmware reply passed through unmodified (diagnostic fields vary by firmware)."""

    reply: Dict[str, Any] = Field(default_factory=dict)


class StepResult(BaseModel):
    la: int
    steps: int
    delay: float
    status: str = "started"
