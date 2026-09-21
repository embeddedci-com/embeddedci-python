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
    board_rev: str = Field("", description="PCB revision: v2, v3 or unknown.")
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
    nrst_pin: bool = Field(False, description="Dedicated target-reset pin (reset_target, flash nreset).")
    usb_cc: bool = Field(False, description='USB-C CC monitoring (command {"cmd": "usb_cc"}).')

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


# -- wiring profile ----------------------------------------------------------------

class WiringSignal(BaseModel):
    """A named DUT signal the profile puts on one LA channel."""

    name: str
    la: int
    direction: str = Field(description="input, output, open_drain or bidir, as seen from the pod.")
    active_low: bool = False
    description: str = ""


class WiringPin(BaseModel):
    la: int
    wired_to: Optional[str] = Field(None, description=(
        "The role (uart_rx, i2c_sda, swd_swclk, …) or signal name on this channel; null = unused."))
    pull: Optional[str] = Field(None, description="Fixed bias resistor, e.g. '4.7k up'; null on LA9-LA12.")


class WiringResult(BaseModel):
    """The bench's effective wiring profile: which DUT signal is on which LA channel."""

    source: str = Field(description="Where the profile came from: defaults, file, server or dict.")
    version: int = 1
    la_voltage: float = Field(description="LA I/O-bank voltage the profile asks for, in volts.")
    efuse: int = Field(description="Target-power rail the tools default to.")
    uart_baud: int
    i2c_address: int = Field(description="Emulated-sensor address as a 7-bit integer.")
    swd_nreset: bool
    swd_target: str
    pins: List[WiringPin] = Field(description="LA1-LA12 with what is wired to each and its bias resistor.")
    signals: List[WiringSignal] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list,
                                description="Wiring that works but is risky (e.g. an I2C bus with no pull-up).")
    profile: Dict[str, Any] = Field(default_factory=dict, description="The profile as its JSON object.")
    saved: bool = Field(False, description="set_wiring stored this profile on embeddedci.com.")


# -- LA pin ownership + GPIO -------------------------------------------------------

class LaPinResult(BaseModel):
    la: int
    function: str = Field(description=(
        "What owns the channel: none (free, watched by captures), gpio, uart_rx, uart_tx, swd_clk, "
        "swd_dio, i2c_sda, i2c_scl, step or step_dir."))
    gpio: Optional[str] = Field(None, description="GPIO mode when function is gpio: input, output or open_drain.")
    level: Optional[int] = Field(None, description="Commanded level of a GPIO output / open-drain channel.")
    pull: Optional[str] = Field(None, description="Bias direction: up, down, or null on LA9-LA12.")
    pull_ohms: Optional[str] = None
    pull_on: bool = False
    in_use: bool = Field(description="A function other than none owns the channel.")


class PinLevel(BaseModel):
    la: int
    level: int


class LaPinsResult(BaseModel):
    pins: List[LaPinResult]
    levels: Optional[List[PinLevel]] = Field(None, description=(
        "Live pin levels; null when the gateware cannot read them (no gpio_read capability)."))


class GpioPinsResult(BaseModel):
    pins: List[LaPinResult] = Field(description="The channels this call configured or released.")


class GpioWriteResult(BaseModel):
    la: List[int]
    level: int


class GpioReadResult(BaseModel):
    levels: List[PinLevel]


class GpioWaitResult(BaseModel):
    la: int
    level: int
    reached: bool = Field(description="False means the timeout passed without the channel reaching level.")
    waited: float = Field(description="Seconds spent waiting.")


class GpioPulseResult(BaseModel):
    la: int
    count: int
    width: float = Field(description="Seconds each pulse is high (and low between pulses).")


class GpioReleaseResult(BaseModel):
    released: List[int] = Field(description="Channels released; empty list = every GPIO channel.")


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


class PowerProfileResult(BaseModel):
    """A target-power rail profiled over time. Currents are amps, voltages volts, energy joules,
    charge coulombs, durations seconds."""

    efuse: int
    rate_hz: float = Field(description="Samples per second actually delivered (measured). The pod reads one sensor register per firmware pass, so this lands below what was asked for — roughly 350-450 Hz. Every sample carries its own timestamp, so the trace is exact regardless.")
    adc_rate_hz: float = Field(default=0.0, description="Conversion rate the current sensor was configured for — the ceiling, not what arrived.")
    n: int = Field(description="Raw samples the statistics cover.")
    duration: float
    avg_current: float
    min_current: float
    peak_current: float
    avg_voltage: float
    min_voltage: float
    max_voltage: float
    energy: float = Field(description="Joules, integrated over every sample.")
    charge: float = Field(description="Coulombs.")
    avg_power: float = Field(description="Watts (energy over duration).")
    fault: bool = Field(description="The eFuse tripped (over-current or short) during the profile.")
    truncated: bool = Field(description="Sampling stopped at max_duration rather than on request.")
    trace_step: float = Field(0.0, description="Seconds covered by each trace point.")
    trace_current: List[float] = Field(default_factory=list, description="Amps per trace point.")
    trace_voltage: List[float] = Field(default_factory=list, description="Volts per trace point.")


class PowerProfileStartResult(BaseModel):
    running: bool = True
    efuse: int
    rate_hz: float = Field(description="Sample rate asked for; the achieved rate is in the stop result.")
    max_duration: float = Field(description="Seconds after which the pod stops sampling by itself.")


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
    trigger: Optional[str] = Field(None, description=(
        "The trigger that started the capture, e.g. 'LA9 rising'; null for a free-running capture. "
        "t = 0 is the trigger moment."))


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
    trigger: Optional[str] = Field(None, description=(
        "The trigger that started the capture, e.g. 'LA9 rising'; null for a free-running capture. "
        "t = 0 is the trigger moment."))


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
