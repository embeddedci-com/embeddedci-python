"""The MCP server: the BenchPod SDK as agent tools.

Every tool is async and runs its SDK call on a worker thread under the session's device lock, so a
long flash or capture never blocks the server's event loop (other requests, pings and cancellation
keep flowing) and concurrent calls cannot interleave commands on the pod. Long operations report
progress while they run.

Error contract: a tool that cannot do what was asked — not connected, an invalid argument, the pod
refusing a command — fails as an MCP tool error (``isError: true``) whose message starts with the
cause, e.g. ``FirmwareError: la voltage not set``. A completed operation with a negative outcome is
a normal result: ``flash`` returns ``ok: false`` with its logs, ``capture_uart`` ``matched: false``.

Results are pydantic models, so every tool publishes an output schema and returns structured
content. No protocol, flash or decode logic lives here — it all comes from the SDK.
"""

import re
import time
from typing import Annotated, Any, Callable, Dict, List, Literal, Optional, TypeVar, Union

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from embeddedci.benchpod import (
    AdcSource,
    AnalogPath,
    CanMode,
    DacOutputPath,
    DacPath,
    DecodeProtocol,
    Edge,
    Fault,
    FpgaImage,
    GpioMode,
    LoopInputMap,
    LoopSource,
    ReplayMapping,
    Trigger,
    TriggerEdge,
    Waveshape,
    Wiring,
    i2c,
)
from embeddedci.benchpod import decode as sdk_decode
from embeddedci.benchpod.capabilities import Capabilities
from embeddedci.benchpod.errors import BenchPodError

from . import models as m
from .guide import INSTRUCTIONS, WIRING
from .session import SESSION, SessionStateError
from .summaries import adc_summary, clip, la_summary, pin_state, power_summary, wiring_summary

mcp = FastMCP("embeddedci-benchpod", instructions=INSTRUCTIONS)

T = TypeVar("T")

#: Longest text (UART output, decode traces) returned in one result.
MAX_TEXT = 16_000

LaPin = Annotated[int, Field(ge=1, le=12, description="LA channel (1-12) the signal is wired to.")]
#: An LA channel given as a number or by the name the wiring profile gives it.
LaRef = Annotated[Union[Annotated[int, Field(ge=1, le=12)], str], Field(description=(
    "LA channel 1-12, or a name from the wiring profile — a signal ('READY') or a role "
    "('uart_rx', 'i2c_sda', 'swd_swclk'). Call `wiring` to see the names."))]
BiasPin = Annotated[int, Field(ge=1, le=8)]
EfuseRail = Annotated[Literal[1, 2], Field(description="Target-power eFuse: 1 = internal 5 V, 2 = external supply.")]
WiredEfuse = Annotated[Optional[Literal[1, 2]], Field(description=(
    "Target-power eFuse: 1 = internal 5 V, 2 = external supply. Omit for the wiring profile's rail."))]
BaudRate = Annotated[int, Field(ge=300, le=4_000_000)]
WiredBaud = Annotated[Optional[int], Field(ge=300, le=4_000_000, description=(
    "UART baud rate; omit for the wiring profile's uart_baud."))]
TriggerLa = Annotated[Optional[Union[Annotated[int, Field(ge=1, le=12)], str]], Field(description=(
    "Wait for an edge or level on this LA channel (a number, or a wiring-profile name) before "
    "sampling, so t = 0 is that moment. Omit for a free-running capture. Needs the "
    "capture_trigger capability."))]
TriggerTimeoutS = Annotated[float, Field(gt=0, le=600, description=(
    "Seconds to wait for the trigger before the capture is abandoned with a TriggerTimeout."))]
GpioLevel = Annotated[Literal[0, 1], Field(description="0 = low; 1 = high (open_drain: released).")]
Samples = Annotated[int, Field(ge=1, le=8_000_000)]
EnvelopePoints = Annotated[int, Field(ge=8, le=2000, description="Points in the returned min/max envelope.")]
RateHz = Annotated[Optional[float], Field(description="Sample rate in Hz; omit for the device maximum.")]
Regex = Annotated[Optional[str], Field(description="Python regular expression; stop as soon as it matches.")]
Byte = Annotated[int, Field(ge=0, le=255)]
Code16 = Annotated[int, Field(ge=0, le=65535)]
SwitchImage = Annotated[bool, Field(description=(
    "If the pod is on the other gateware image, switch it first (~3 s). The switch resets the FPGA, "
    "stopping any DAC output, UART session or I2C sensor emulation. false = fail instead."))]


# -- plumbing --------------------------------------------------------------------

def _ann(title: str, *, read_only: bool = False, destructive: bool = False,
         idempotent: bool = False, cloud: bool = False) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=read_only,
                           destructiveHint=None if read_only else destructive,
                           idempotentHint=idempotent, openWorldHint=cloud)


async def _call(fn: Callable[[], T], *, lock: bool = True) -> T:
    """Run ``fn`` on a worker thread (under the device lock) and map SDK errors to tool errors."""
    def run() -> T:
        if not lock:
            return fn()
        with SESSION.lock:
            return fn()

    try:
        return await anyio.to_thread.run_sync(run)
    except BenchPodError as exc:
        raise ToolError(f"{type(exc).__name__}: {exc}") from exc
    except (ValueError, re.error) as exc:
        raise ToolError(f"invalid argument: {exc}") from exc


async def _report(ctx: Optional[Context], progress: float, message: str) -> None:
    if ctx is None:
        return
    try:
        await ctx.report_progress(progress, None, message)
    except Exception:  # no request context (direct call) or the client went away
        pass


async def _call_reporting(ctx: Optional[Context], label: str, fn: Callable[[], T]) -> T:
    """Like :func:`_call`, sending a progress notification every 2 s until ``fn`` finishes."""
    done = anyio.Event()
    outcome: Dict[str, Any] = {}

    async def heartbeat() -> None:
        start = time.monotonic()
        while True:
            with anyio.move_on_after(2.0):
                await done.wait()
            if done.is_set():
                return
            elapsed = time.monotonic() - start
            await _report(ctx, elapsed, f"{label}: {elapsed:.0f} s")

    async with anyio.create_task_group() as tg:
        tg.start_soon(heartbeat)
        try:
            outcome["value"] = await _call(fn)
        except Exception as exc:
            outcome["error"] = exc
        finally:
            done.set()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _compile(pattern: Optional[str]) -> Optional["re.Pattern[str]"]:
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"invalid argument: until_regex {pattern!r}: {exc}") from exc


def _fault(spec: Optional[m.FaultSpec]) -> Optional[Fault]:
    if spec is None:
        return None
    return Fault(type=spec.type, start=spec.start, width=spec.width, level=spec.level)


def _la(pod: Any, value: Any) -> int:
    """An LA channel from a number 1-12 or a wiring-profile name."""
    return pod.wiring.la(value) if isinstance(value, str) else int(value)


def _channel(pod: Any, value: Any, key: str) -> int:
    """Like :func:`_la`, but an omitted channel comes from the profile's ``key`` role."""
    if value is not None:
        return _la(pod, value)
    la = getattr(pod.wiring, key)
    if la is None:
        raise ValueError(f"{key} was not given and the wiring profile has no {key}; pass it or "
                         "set it in the profile (see the `wiring` tool)")
    return int(la)


def _trigger(la: Any, edge: str) -> Optional[Trigger]:
    """A :class:`Trigger` for the capture tools (the SDK resolves wiring names)."""
    return None if la is None else Trigger(la, edge)  # type: ignore[arg-type]


# -- connection ---------------------------------------------------------------------

def _status() -> m.StatusResult:
    if not SESSION.connected and not SESSION.reconnectable:
        default = SESSION.default_connection
        hint = (f"call connect (the server default is {default!r})" if default
                else "call connect with a host, /dev/tty…, 'usb' or 'embeddedci:<device>'")
        return m.StatusResult(connected=False, warnings=[f"not connected — {hint}"])
    pod = SESSION.require()
    firmware = pod.status()
    warnings: List[str] = []
    # From this status, not the cached capabilities: it describes the boot running now.
    boot = Capabilities.from_status(firmware).boot_warning()
    if boot:
        warnings.append(boot)
    try:
        la = pod.get_la_voltage().voltage
    except BenchPodError:
        la = None
    if la is None:
        warnings.append("LA I/O voltage not selected — call set_la_voltage (1.8 or 3.3, matching "
                        "the DUT) before flash, UART, LA capture, pulls or I2C-sensor emulation")
    return m.StatusResult(
        connected=True, connection=SESSION.connection, kind=SESSION.kind, leased=pod.leased,
        la_voltage=la, capabilities=m.CapabilitiesInfo.from_caps(pod.capabilities),
        session=SESSION.info(), warnings=warnings, firmware=firmware,
    )


@mcp.tool(annotations=_ann("Connect to a BenchPod", idempotent=True, cloud=True))
async def connect(
    connection: Annotated[Optional[str], Field(description=(
        "host[:port] (TCP, default port 8080), a serial device path, 'usb' (auto-detect), "
        "'discover' (mDNS) or 'embeddedci:<device-name>' (cloud). Omit for the server default."))] = None,
    la_voltage: Annotated[Optional[Literal[1.8, 3.3]], Field(description=(
        "Select the LA I/O-bank voltage right after connecting (the DUT's I/O voltage)."))] = None,
    lease_wait: Annotated[float, Field(ge=0, le=3600, description=(
        "Cloud only: seconds to wait when another run holds the shared device."))] = 30.0,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.StatusResult:
    """Open a BenchPod connection (closing any previous one and its sessions) and report status.

    Over the cloud the device is shared: this takes an exclusive lease, released by `disconnect`
    or after the server's idle timeout (the next tool call then reconnects).
    """
    def op() -> m.StatusResult:
        SESSION.connect(connection, la_voltage=la_voltage, lease_wait=lease_wait)
        try:
            return _status()
        except BaseException:
            SESSION.disconnect()  # the pod never answered: don't leave a dead session "connected"
            raise

    return await _call_reporting(ctx, "connecting", op)


@mcp.tool(annotations=_ann("Disconnect", idempotent=True))
async def disconnect() -> m.StatusResult:
    """Close UART/CAN sessions and the connection, releasing a cloud lease. Safe when not connected."""
    await _call(SESSION.disconnect)
    return m.StatusResult(connected=False)


@mcp.tool(annotations=_ann("Pod status", read_only=True))
async def status() -> m.StatusResult:
    """Connection, firmware, capabilities, selected LA voltage and open sessions — with warnings
    for anything that will block the next steps (e.g. no LA voltage). Works when not connected."""
    return await _call(_status)


@mcp.tool(annotations=_ann("Set LA I/O voltage", idempotent=True))
async def set_la_voltage(voltage: Literal[1.8, 3.3]) -> m.LaVoltageResult:
    """Select the LA I/O-bank voltage to match the DUT's I/O (1.8 V needs a rev3 pod).

    Required before flash, UART, LA capture, pull resistors or I2C-sensor emulation.
    """
    state = await _call(lambda: SESSION.require().set_la_voltage(voltage))
    return m.LaVoltageResult(voltage=state.voltage, readback=state.readback)


# -- wiring profile -------------------------------------------------------------------

@mcp.tool(annotations=_ann("Wiring profile", read_only=True))
async def wiring() -> m.WiringResult:
    """Which DUT signal is on which LA channel — call this first, before any channel argument.

    Returns the bench's effective profile: a 12-row pin table (what is wired to each channel and
    its bias resistor), the named signals, the target-power rail, the UART baud and the SWD target,
    plus warnings about risky wiring. Every tool whose channel, baud, rail or SWD arguments are
    omitted takes them from this profile, and channel arguments accept these names.
    """
    return await _call(lambda: wiring_summary(SESSION.require().wiring))


@mcp.tool(annotations=_ann("Set the wiring profile", idempotent=True, cloud=True))
async def set_wiring(
    profile: Annotated[Dict[str, Any], Field(description=(
        "The profile as a JSON object (schema version 1): la_mv, efuse, uart_rx/uart_tx/uart_baud, "
        "i2c_sda/i2c_scl/i2c_addr, swd_swclk/swd_swdio/swd_nreset/swd_target, spi_*, and signals "
        "[{name, la, direction, active_low, description}]. Absent keys take their default; a pin "
        "set to null is not wired."))],
    save: Annotated[bool, Field(description=(
        "Also store the profile for this device on embeddedci.com (cloud devices only), so other "
        "runs and the web UI see it. false = use it for this connection only."))] = False,
) -> m.WiringResult:
    """Replace the wiring profile this connection uses (and optionally store it on embeddedci.com).

    The profile is validated first: two roles or signals on one LA channel, an out-of-range pin or
    an unknown key are rejected with a message naming every problem.
    """
    def op() -> m.WiringResult:
        pod = SESSION.require()
        pod.wiring = Wiring.from_dict(profile)
        if save:
            pod.save_wiring()
        return wiring_summary(pod.wiring, saved=save)

    return await _call(op)


# -- power ----------------------------------------------------------------------------

def _switch_power(efuse: Optional[int], on: bool, delay: Optional[float]) -> m.PowerResult:
    pod = SESSION.require()
    rail = pod.wiring.efuse if efuse is None else efuse
    pod.target_power(rail, on=on, delay=delay)
    return m.PowerResult(efuse=rail, on=on, delay=delay)


@mcp.tool(annotations=_ann("Power target on", destructive=True, idempotent=True))
async def power_on(
    efuse: WiredEfuse = None,
    delay: Annotated[Optional[float], Field(description=(
        "Seconds: schedule the power-on pod-side and return at once (e.g. to power on during a capture)."))] = None,
) -> m.PowerResult:
    """Switch the target's power rail on. The result says which rail was used."""
    return await _call(lambda: _switch_power(efuse, True, delay))


@mcp.tool(annotations=_ann("Power target off", destructive=True, idempotent=True))
async def power_off(
    efuse: WiredEfuse = None,
    delay: Annotated[Optional[float], Field(description="Seconds: schedule the power-off pod-side.")] = None,
) -> m.PowerResult:
    """Switch the target's power rail off. The result says which rail was used."""
    return await _call(lambda: _switch_power(efuse, False, delay))


@mcp.tool(annotations=_ann("Profile the target's power"))
async def measure_power(
    duration: Annotated[float, Field(gt=0, le=600, description="Seconds to sample (blocking).")],
    efuse: WiredEfuse = None,
    rate_hz: Annotated[float, Field(ge=100, le=500, description=(
        "Samples per second. Accurate to ~200 Hz; above that the pod delivers what its sampling "
        "loop allows, flattening near 365 Hz. rate_hz in the result is what you actually got, "
        "adc_rate_hz what the sensor was configured for."))] = 500.0,
    points: Annotated[int, Field(ge=0, le=500, description=(
        "Points of current/voltage trace to return alongside the statistics; 0 = statistics only."))] = 0,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.PowerProfileResult:
    """Measure the DUT's supply current and voltage for a window: average, minimum and peak current,
    voltage, energy and charge (gap-free sampling, so energy is integrated, not estimated).

    Use this for a self-contained window (a boot, a sleep interval). To profile across other tool
    calls, bracket them with power_profile_start / power_profile_stop.
    """
    return await _call_reporting(ctx, "profiling power", lambda: power_summary(
        SESSION.require().measure_power(duration, efuse=efuse, rate_hz=rate_hz,
                                        keep_samples=points), points))


@mcp.tool(annotations=_ann("Start a power profile"))
async def power_profile_start(
    efuse: WiredEfuse = None,
    rate_hz: Annotated[float, Field(ge=100, le=500, description=(
        "Samples per second; accurate to ~200 Hz, flattening near 365 Hz above that."))] = 500.0,
    max_duration: Annotated[float, Field(gt=0, le=600, description=(
        "Seconds after which the pod stops sampling by itself (the result is then truncated)."))] = 60.0,
) -> m.PowerProfileStartResult:
    """Start sampling the target-power rail in the background, then run the steps you want to
    profile and call power_profile_stop. Replaces any profile already running."""
    def op() -> m.PowerProfileStartResult:
        pod = SESSION.require()
        rail = pod.wiring.efuse if efuse is None else efuse
        SESSION.power_profile = pod.power_profile(efuse=rail, rate_hz=rate_hz,
                                                  max_duration=max_duration).start()
        return m.PowerProfileStartResult(efuse=rail, rate_hz=rate_hz, max_duration=max_duration)

    return await _call(op)


@mcp.tool(annotations=_ann("Stop a power profile"))
async def power_profile_stop(
    points: Annotated[int, Field(ge=0, le=500, description=(
        "Points of current/voltage trace to return alongside the statistics; 0 = statistics only."))] = 0,
) -> m.PowerProfileResult:
    """Stop the running power profile and return its statistics (and, with points, a trace)."""
    def op() -> m.PowerProfileResult:
        SESSION.require()
        profile = SESSION.require_power_profile().stop()
        SESSION.power_profile = None
        return power_summary(profile, points)

    return await _call(op)


@mcp.tool(annotations=_ann("Power status", read_only=True))
async def power_status() -> m.PowerStatusResult:
    """Both target-power rails: eFuse on/off and tripped state, bus voltage and current draw."""
    def op() -> m.PowerStatusResult:
        pod = SESSION.require()
        ts, ps = pod.target_status(), pod.power_status()

        def rail(n: int) -> m.RailResult:
            e, r = ts.efuse(n), ps.rail(n)
            return m.RailResult(enabled=e.enabled, fault=e.fault, state_valid=e.valid,
                                monitor_ok=r.ok, bus_voltage=r.bus_voltage, current=r.current)

        return m.PowerStatusResult(internal=rail(1), external=rail(2))

    return await _call(op)


@mcp.tool(annotations=_ann("Reset target", destructive=True))
async def reset_target(
    action: Annotated[Literal["pulse", "hold", "release", "status"], Field(description=(
        "pulse = reset once; hold = keep the target in reset; release = let it run; status = read only."))] = "pulse",
    pulse: Annotated[float, Field(gt=0, le=1, description="Pulse length in seconds (at most 1).")] = 0.1,
) -> m.ResetResult:
    """Drive the DUT's reset line from the pod's reset pin (rev3 pods, DUT header J1 pin 22)."""
    def op() -> m.ResetResult:
        pod = SESSION.require()
        if action == "pulse":
            state = pod.reset_target(pulse=pulse)
        elif action == "status":
            state = pod.reset_state()
        else:
            state = pod.set_reset(action == "hold")
        return m.ResetResult(asserted=state.asserted, supported=state.supported)

    return await _call(op)


# -- flash ------------------------------------------------------------------------------

@mcp.tool(annotations=_ann("Flash firmware over SWD", destructive=True))
async def flash(
    swclk: Annotated[Optional[LaRef], Field(description=(
        "LA channel (or wiring name) wired to the DUT's SWCLK; omit for the profile's swd_swclk."))] = None,
    swdio: Annotated[Optional[LaRef], Field(description=(
        "LA channel (or wiring name) wired to the DUT's SWDIO; omit for the profile's swd_swdio."))] = None,
    target: Annotated[str, Field(description=(
        "OpenOCD target config, e.g. target/stm32f4x.cfg; omit for the profile's swd_target."))] = "",
    file: Annotated[str, Field(description="Firmware image path on the machine running this server.")] = "",
    nreset: Annotated[Optional[bool], Field(description=(
        "The target's reset line is wired to the pod's reset pin: enables connect-under-reset. "
        "Omit for the profile's swd_nreset."))] = None,
    load_address: Annotated[str, Field(description="Load address for raw .bin images, e.g. 0x08000000.")] = "",
    target_power: Annotated[Optional[Literal[1, 2]], Field(description="Power this eFuse on before flashing.")] = None,
    verify: bool = True,
    reset: bool = True,
    connect_under_reset: Optional[bool] = None,
    extra_configs: Optional[List[str]] = None,
    extra_args: Optional[List[str]] = None,
    timeout: Annotated[float, Field(gt=0, le=3600)] = 300.0,
    connect_attempts: Annotated[int, Field(ge=1, le=20)] = 5,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.FlashResult:
    """Program the DUT over SWD through the pod's CMSIS-DAP probe (OpenOCD runs on this server's host).

    The SWD pins, the reset flag and the target config come from the wiring profile when omitted.
    A failed flash is a normal result (ok=false): read target_unreachable (unpowered, mis-wired or
    held in reset), stalled, and the log tails to decide what to change.
    """
    def op() -> m.FlashResult:
        pod = SESSION.require()
        result = pod.flash(
            swclk=_channel(pod, swclk, "swd_swclk"), swdio=_channel(pod, swdio, "swd_swdio"),
            nreset=pod.wiring.swd_nreset if nreset is None else nreset,
            target=target or pod.wiring.swd_target, file=file,
            load_address=load_address, target_power=target_power, verify=verify, reset=reset,
            connect_under_reset=connect_under_reset, extra_configs=tuple(extra_configs or ()),
            extra_args=tuple(extra_args or ()), timeout=timeout,
            connect_attempts=connect_attempts, check=False,
        )
        return m.FlashResult(ok=result.ok, returncode=result.returncode,
                             target_unreachable=result.target_unreachable, stalled=result.stalled,
                             stdout_tail=clip(result.stdout, 4000)[-4000:],
                             stderr_tail=clip(result.stderr, 4000)[-4000:])

    return await _call_reporting(ctx, "flashing", op)


# -- UART -----------------------------------------------------------------------------

def _uart_result(cap: Any) -> m.UartCaptureResult:
    return m.UartCaptureResult(text=clip(cap.text, MAX_TEXT), matched=cap.matched,
                               bytes=len(cap.text.encode("utf-8")), truncated=len(cap.text) > MAX_TEXT)


UartRx = Annotated[Optional[LaRef], Field(description=(
    "LA channel (or wiring name) wired to the DUT's TX; omit for the profile's uart_rx."))]
UartTx = Annotated[Optional[LaRef], Field(description=(
    "LA channel (or wiring name) wired to the DUT's RX; omit for the profile's uart_tx."))]


@mcp.tool(annotations=_ann("Capture UART output"))
async def capture_uart(
    duration: Annotated[float, Field(gt=0, le=600, description="Capture window in seconds.")],
    rx: UartRx = None,
    tx: UartTx = None,
    baud: WiredBaud = None,
    until_regex: Regex = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.UartCaptureResult:
    """Record the DUT's UART output for a fixed window (or until until_regex matches).

    The channels and baud come from the wiring profile when omitted.
    """
    pattern = _compile(until_regex)
    return await _call_reporting(ctx, "capturing UART", lambda: _uart_result(
        SESSION.require().capture_uart(rx=rx, tx=tx, baud=baud, duration=duration, until=pattern)))


@mcp.tool(annotations=_ann("Power-cycle and capture boot log", destructive=True))
async def power_cycle_and_capture(
    rx: UartRx = None,
    tx: UartTx = None,
    efuse: WiredEfuse = None,
    delay: Annotated[float, Field(ge=0, le=60, description="Seconds into the capture the power comes back.")] = 1.0,
    duration: Annotated[float, Field(gt=0, le=600, description="Capture window in seconds; must exceed delay.")] = 4.0,
    baud: WiredBaud = None,
    until_regex: Regex = None,
    off_settle: Annotated[float, Field(ge=0, le=10)] = 0.3,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.UartCaptureResult:
    """Power the target off, then capture UART while it powers back on — the boot banner lands in the window.

    The channels, baud and power rail come from the wiring profile when omitted.
    """
    pattern = _compile(until_regex)
    return await _call_reporting(ctx, "power-cycling", lambda: _uart_result(
        SESSION.require().power_cycle_and_capture(rx=rx, tx=tx, efuse=efuse, delay=delay,
                                                  duration=duration, baud=baud, until=pattern,
                                                  off_settle=off_settle)))


@mcp.tool(annotations=_ann("Open a UART session"))
async def uart_open(
    rx: UartRx = None,
    tx: UartTx = None,
    baud: WiredBaud = None,
) -> m.UartSessionResult:
    """Start buffering the DUT's UART in the background (replacing any open session).

    Open it BEFORE an action whose output matters (power_on, reset_target), then uart_read; use
    uart_write to type into the DUT's console. The channels and baud come from the wiring profile
    when omitted; the result says which were used. A channel used as GPIO must be released first
    (gpio_release).
    """
    def op() -> m.UartSessionResult:
        pod = SESSION.require()
        rx_i, tx_i = _channel(pod, rx, "uart_rx"), _channel(pod, tx, "uart_tx")
        rate = int(pod.wiring.uart_baud if baud is None else baud)
        SESSION.close_uart()
        SESSION.uart = pod.open_uart(rx=rx_i, tx=tx_i, baud=rate)
        SESSION.uart_port = (rx_i, tx_i, rate)
        return m.UartSessionResult(open=True, rx=rx_i, tx=tx_i, baud=rate)

    return await _call(op)


@mcp.tool(annotations=_ann("Write to the DUT console"))
async def uart_write(
    text: str,
    line_ending: Annotated[Literal["none", "lf", "crlf"], Field(description="Appended after text.")] = "lf",
) -> m.UartWriteResult:
    """Send text to the DUT's RX through the open UART session."""
    data = (text + {"none": "", "lf": "\n", "crlf": "\r\n"}[line_ending]).encode("utf-8")

    def op() -> m.UartWriteResult:
        SESSION.require_uart().write(data)
        return m.UartWriteResult(written=len(data))

    return await _call(op, lock=False)


@mcp.tool(annotations=_ann("Read the DUT console", read_only=True))
async def uart_read(
    until_regex: Regex = None,
    timeout: Annotated[float, Field(ge=0, le=600, description=(
        "Seconds to wait for until_regex (or, without one, for any new output)."))] = 2.0,
) -> m.UartReadResult:
    """Return the UART output received since the previous uart_read, optionally waiting for a match."""
    pattern = _compile(until_regex)

    def op() -> m.UartReadResult:
        uart = SESSION.require_uart()
        matched: Optional[bool] = None
        if pattern is not None:
            through_match = uart.read_until(pattern, timeout=timeout)  # searches unread text
            matched = through_match is not None
            new = (through_match or "") + uart.read()
        else:
            new = uart.read(timeout=timeout)
        return m.UartReadResult(text=clip(new, MAX_TEXT), matched=matched, closed=uart.closed,
                                overflowed=uart.overflowed, truncated=len(new) > MAX_TEXT)

    return await _call(op, lock=False)


@mcp.tool(annotations=_ann("Close the UART session", idempotent=True))
async def uart_close() -> m.UartSessionResult:
    """Stop the background UART session (safe when none is open)."""
    await _call(SESSION.close_uart)
    return m.UartSessionResult(open=False)


# -- emulated I2C sensor --------------------------------------------------------------

@mcp.tool(annotations=_ann("Emulate an I2C sensor"))
async def enable_i2c_sensor(
    sda: Annotated[Optional[LaRef], Field(description=(
        "LA channel (or wiring name) of the bus's SDA; omit for the profile's i2c_sda."))] = None,
    scl: Annotated[Optional[LaRef], Field(description=(
        "LA channel (or wiring name) of the bus's SCL; omit for the profile's i2c_scl."))] = None,
    address: Annotated[Optional[int], Field(ge=0x03, le=0x77, description=(
        "7-bit address (BMP280: 0x76 or 0x77); omit for the profile's i2c_addr."))] = None,
    temperature_c: Optional[float] = None,
    pressure_pa: Optional[float] = None,
) -> m.DeviceReply:
    """Make the pod act as a BMP280 on sda/scl for the DUT to read. Engage pull-ups on both lines first (set_pull).

    The channels and address come from the wiring profile when omitted.
    """
    return m.DeviceReply(reply=await _call(lambda: SESSION.require().enable_i2c_sensor(
        sda=sda, scl=scl, address=address, temperature_c=temperature_c, pressure_pa=pressure_pa)) or {})


@mcp.tool(annotations=_ann("Set emulated sensor values", idempotent=True))
async def set_i2c_sensor(temperature_c: Optional[float] = None,
                         pressure_pa: Optional[float] = None) -> m.DeviceReply:
    """Change what the emulated sensor reports (at least one value)."""
    return m.DeviceReply(reply=await _call(lambda: SESSION.require().set_i2c_sensor(
        temperature_c=temperature_c, pressure_pa=pressure_pa)) or {})


@mcp.tool(annotations=_ann("Stop the emulated sensor", idempotent=True))
async def disable_i2c_sensor() -> m.StopResult:
    """Disarm the emulated sensor (safe when none is active)."""
    await _call(lambda: SESSION.require().disable_i2c_sensor())
    return m.StopResult()


@mcp.tool(annotations=_ann("Emulated sensor status", read_only=True))
async def i2c_sensor_status() -> m.DeviceReply:
    """Emulated sensor state plus bus activity counters — did the DUT talk to it at all?"""
    return m.DeviceReply(reply=await _call(lambda: SESSION.require().i2c_sensor_status()) or {})


@mcp.tool(annotations=_ann("Emulated sensor registers", read_only=True))
async def i2c_sensor_regs(start: Annotated[int, Field(ge=0, le=255)] = 0,
                          length: Annotated[int, Field(ge=1, le=256)] = 256) -> m.I2cRegsResult:
    """Read the emulated sensor's register image."""
    data = await _call(lambda: SESSION.require().i2c_sensor_regs(start=start, length=length))
    return m.I2cRegsResult(start=start, bytes=list(data))


@mcp.tool(annotations=_ann("Capture the sensor's I2C bus"))
async def i2c_sensor_capture(
    samples: Annotated[int, Field(ge=64, le=65536)] = 4096,
    sample_rate_hz: Annotated[float, Field(gt=0, le=50_000_000)] = 500_000,
    address: Annotated[Optional[int], Field(description="Also report whether the DUT addressed this device.")] = None,
    register: Annotated[Optional[int], Field(description="With address: the bytes the DUT read from this register.")] = None,
) -> m.I2cCaptureResult:
    """Capture and decode the emulated sensor's I2C bus into a transaction trace (S/Sr/P, addr, data, ACK)."""
    def op() -> m.I2cCaptureResult:
        txns = SESSION.require().i2c_sensor_capture(samples, sample_rate_hz=sample_rate_hz)
        addrs = sorted({msg.address for t in txns for msg in t.messages if msg.address is not None})
        trace = i2c.format_transactions(txns)
        result = m.I2cCaptureResult(transactions=len(txns), addresses=[f"0x{a:02X}" for a in addrs],
                                    trace=clip(trace, MAX_TEXT), truncated=len(trace) > MAX_TEXT)
        if address is not None:
            result.addressed = i2c.addressed(txns, address)
            if register is not None:
                result.register_value = i2c.read_register(txns, address, register)
        return result

    return await _call(op)


# -- bias resistors ---------------------------------------------------------------------

def _pull(state: Any) -> m.PullChannel:
    return m.PullChannel(la=state.la, enabled=state.enabled, direction=state.direction,
                         ohms=state.ohms, available=state.available)


@mcp.tool(annotations=_ann("Set pull resistors", idempotent=True))
async def set_pull(
    las: Annotated[List[BiasPin], Field(min_length=1, max_length=8, description="LA channels (1-8).")],
    enabled: bool,
) -> m.PullStatusResult:
    """Engage or release the fixed bias resistors: LA1-LA6 pull UP (I2C lines), LA7/LA8 pull DOWN.

    The resistors are 3V3-referenced, so the pod refuses to engage them with the LA bank at 1.8 V.
    """
    def op() -> m.PullStatusResult:
        pod = SESSION.require()
        return m.PullStatusResult(channels=[_pull(pod.set_pull(la, enabled)) for la in las])

    return await _call(op)


@mcp.tool(annotations=_ann("Pull resistor status", read_only=True))
async def pull_status() -> m.PullStatusResult:
    """State, direction and value of the bias resistor on LA1-LA8."""
    def op() -> m.PullStatusResult:
        pod = SESSION.require()
        return m.PullStatusResult(channels=[_pull(pod.pull_state(la)) for la in range(1, 9)])

    return await _call(op)


# -- LA pin ownership + GPIO ---------------------------------------------------------------

GpioChannels = Annotated[List[LaRef], Field(min_length=1, max_length=12, description=(
    "LA channels as numbers 1-12 or wiring-profile names."))]
OptionalGpioChannels = Annotated[Optional[List[LaRef]], Field(max_length=12, description=(
    "LA channels as numbers 1-12 or wiring-profile names."))]


@mcp.tool(annotations=_ann("LA pin functions", read_only=True))
async def la_pins() -> m.LaPinsResult:
    """What owns each of the 12 LA channels — none (free), gpio, uart_rx/uart_tx, swd_clk/swd_dio,
    i2c_sda/i2c_scl or step — plus each channel's GPIO mode, commanded level and bias resistor.

    Read this when a tool is refused with a pin conflict: it names the owner to stop. Live pin
    levels come along when the gateware can read them. Captures observe every channel whatever
    owns it.
    """
    def op() -> m.LaPinsResult:
        pod = SESSION.require()
        pins = [pin_state(p) for p in pod.la_pins()]
        levels = None
        if pod.capabilities.gpio_read:
            levels = [m.PinLevel(la=la, level=v) for la, v in sorted(pod.pin_levels().items())]
        return m.LaPinsResult(pins=pins, levels=levels)

    return await _call(op)


@mcp.tool(annotations=_ann("Use LA channels as GPIO", destructive=True, idempotent=True))
async def gpio_mode(
    la: GpioChannels,
    mode: Annotated[GpioMode, Field(description=(
        "output = push-pull; open_drain = 0 pulls low and 1 releases; input = high-Z, level readable."))] = "output",
    level: Annotated[Optional[Literal[0, 1]], Field(description=(
        "Starting level of an output (default 0) or open-drain channel (default 1, released). "
        "Not allowed for input."))] = None,
) -> m.GpioPinsResult:
    """Claim LA channels as GPIO so the pod can drive or read them.

    A channel stays GPIO — across disconnects — until gpio_release, and while it is GPIO nothing
    else can use it: release it before uart_open, flash or enable_i2c_sensor on that channel.
    A channel already owned by another function is refused with a PinConflictError naming the
    owner, and an engaged bias resistor that would fight the mode with a PullConflictError.
    """
    def op() -> m.GpioPinsResult:
        pod = SESSION.require()
        las = [_la(pod, ch) for ch in la]
        # configure_gpio sends the whole list in one command, which the pod validates as a unit —
        # so a conflict on any channel leaves every channel as it was.
        return m.GpioPinsResult(pins=[pin_state(p) for p in pod.configure_gpio(las, mode, level=level)])

    return await _call(op)


@mcp.tool(annotations=_ann("Drive GPIO channels", destructive=True, idempotent=True))
async def gpio_write(la: GpioChannels, level: GpioLevel) -> m.GpioWriteResult:
    """Set the level of GPIO output / open-drain channels (all of them in one pod command).

    The channels must already be in an output or open_drain mode (gpio_mode).
    """
    def op() -> m.GpioWriteResult:
        pod = SESSION.require()
        las = [_la(pod, ch) for ch in la]
        pod.set_gpio(las, level)
        return m.GpioWriteResult(la=las, level=level)

    return await _call(op)


@mcp.tool(annotations=_ann("Read pin levels", read_only=True))
async def gpio_read(la: OptionalGpioChannels = None) -> m.GpioReadResult:
    """The live level (0/1) of LA channels — omit `la` for all 12.

    Works whatever owns a channel; a GPIO input is the usual way to watch a DUT output.
    """
    def op() -> m.GpioReadResult:
        pod = SESSION.require()
        levels = pod.pin_levels()
        wanted = sorted(levels) if la is None else [_la(pod, ch) for ch in la]
        return m.GpioReadResult(levels=[m.PinLevel(la=ch, level=levels[ch]) for ch in wanted])

    return await _call(op)


@mcp.tool(annotations=_ann("Wait for a pin level", read_only=True))
async def gpio_wait(
    la: LaRef,
    level: GpioLevel = 1,
    timeout: Annotated[float, Field(ge=0, le=600, description="Seconds to wait.")] = 5.0,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.GpioWaitResult:
    """Wait until an LA channel reads `level` (reached=false when the timeout passes first).

    Polled from this host, so it resolves to a few milliseconds plus the round trip — for precise
    timing use a triggered capture (capture_la trigger_la) and la_timing instead.
    """
    def op() -> m.GpioWaitResult:
        pod = SESSION.require()
        ch = _la(pod, la)
        start = time.monotonic()
        reached = pod.wait_for_level(ch, level, timeout=timeout)
        return m.GpioWaitResult(la=ch, level=level, reached=reached,
                                waited=round(time.monotonic() - start, 4))

    return await _call_reporting(ctx, "waiting for the pin", op)


@mcp.tool(annotations=_ann("Pulse a GPIO channel", destructive=True))
async def gpio_pulse(
    la: LaRef,
    width: Annotated[float, Field(gt=0, le=10, description="Seconds the pulse is high (and low between pulses).")],
    count: Annotated[int, Field(ge=1, le=10_000_000, description="Number of pulses.")] = 1,
) -> m.GpioPulseResult:
    """Emit FPGA-timed pulses on an LA channel — a trigger for the DUT, or a step/dir motor train.

    The channel must be free or a GPIO output at level 0 (it returns to its GPIO level afterwards).
    The FPGA runs the train by itself, so this returns as soon as it starts.
    """
    def op() -> m.GpioPulseResult:
        pod = SESSION.require()
        ch = _la(pod, la)
        pod.la_step(ch, steps=count, delay=width)
        return m.GpioPulseResult(la=ch, count=count, width=width)

    return await _call(op)


@mcp.tool(annotations=_ann("Release GPIO channels", idempotent=True))
async def gpio_release(la: OptionalGpioChannels = None) -> m.GpioReleaseResult:
    """Stop using channels as GPIO — they go back to high-Z, watched by captures.

    Omit `la` to release every GPIO channel (channels owned by other functions are left alone).
    Do this before starting a UART session, flashing or emulating a sensor on those channels.
    """
    def op() -> m.GpioReleaseResult:
        pod = SESSION.require()
        las = [] if la is None else [_la(pod, ch) for ch in la]
        pod.release_gpio(*las)
        return m.GpioReleaseResult(released=las)

    return await _call(op)


# -- analog ------------------------------------------------------------------------------

@mcp.tool(annotations=_ann("Apply an analog path", idempotent=True))
async def analog_path(path: AnalogPath) -> m.AnalogPathResult:
    """Set every analog mux and relay for a named path in one step.

    dac_3v3/dac_5v/dac_12v route the DAC to an output; adc_ext connects the ADC to the front SMA;
    cal1/cal2 loop the 5 V/12 V DAC output back into the ADC; amp reads the current terminal;
    off parks everything.
    """
    state = await _call(lambda: SESSION.require().analog_path(path))
    return m.AnalogPathResult(path=state.path)


@mcp.tool(annotations=_ann("Set a DC output voltage", destructive=True, idempotent=True))
async def dac_output(
    path: DacOutputPath,
    volts: Annotated[Optional[float], Field(description="Calibrated DC volts to drive; omit to only route.")] = None,
) -> m.DacOutputResult:
    """Route a DAC output (3v3, 5v, 12v = ±12 V, or off) and drive a calibrated DC voltage on it."""
    out = await _call(lambda: SESSION.require().dac_output(path, volts=volts))
    return m.DacOutputResult(path=out.path, voltage=out.voltage, code=out.code)


@mcp.tool(annotations=_ann("Read a voltage", read_only=True))
async def adc_read(source: AdcSource = "ext") -> m.AdcReadResult:
    """One calibrated voltage: ext = front SMA (true volts), cal1/cal2 = DAC loopbacks, amp = current terminal.

    Refused while the input is still moving (e.g. a DAC output left running).
    """
    r = await _call(lambda: SESSION.require().adc_read(source))
    return m.AdcReadResult(source=r.source, voltage=r.voltage, count=r.count, span=r.span)


@mcp.tool(annotations=_ann("Capture an ADC waveform"))
async def capture_adc(
    samples: Samples = 4096,
    sample_rate_hz: RateHz = None,
    source: Annotated[Optional[AdcSource], Field(description="Route this ADC source first; omit to keep routing.")] = None,
    points: EnvelopePoints = 200,
    trigger_la: TriggerLa = None,
    trigger_edge: Annotated[TriggerEdge, Field(description="What starts the capture on trigger_la.")] = "rising",
    trigger_timeout: TriggerTimeoutS = 10.0,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.AdcCaptureResult:
    """Capture the ADC and summarise it: calibrated stats, dominant frequency and a min/max envelope.

    Above 32768 samples the capture streams from PSRAM (multi-second captures work). With
    trigger_la the capture waits for that edge or level, so t = 0 is the trigger moment. The
    capture is kept for replay and save_capture_as_recording.
    """
    def op() -> m.AdcCaptureResult:
        cap = SESSION.require().capture_adc(samples, sample_rate_hz=sample_rate_hz, source=source,
                                            trigger=_trigger(trigger_la, trigger_edge),
                                            trigger_timeout=trigger_timeout)
        SESSION.last_adc = cap
        return adc_summary(cap, points)

    return await _call_reporting(ctx, "capturing ADC", op)


@mcp.tool(annotations=_ann("Capture logic channels"))
async def capture_la(
    samples: Samples = 4096,
    sample_rate_hz: RateHz = None,
    stop_dac_after: Annotated[Optional[float], Field(description=(
        "Seconds into the capture to cut a running DAC output (see the DUT react)."))] = None,
    trigger_la: TriggerLa = None,
    trigger_edge: Annotated[TriggerEdge, Field(description="What starts the capture on trigger_la.")] = "rising",
    trigger_timeout: TriggerTimeoutS = 10.0,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.LaCaptureResult:
    """Capture all 12 LA channels and summarise each: levels, edge count, first edge, estimated frequency.

    With trigger_la the capture starts on that edge or level instead of immediately, so a short
    event can be caught at a high sample rate. The capture is kept for decode_la and la_timing.
    """
    def op() -> m.LaCaptureResult:
        la = SESSION.require().capture_la(samples, sample_rate_hz=sample_rate_hz,
                                          stop_dac_after=stop_dac_after,
                                          trigger=_trigger(trigger_la, trigger_edge),
                                          trigger_timeout=trigger_timeout)
        SESSION.last_la = la
        return la_summary(la)

    return await _call_reporting(ctx, "capturing LA", op)


@mcp.tool(annotations=_ann("Capture ADC and logic together"))
async def capture_correlated(
    adc_samples: Annotated[int, Field(ge=0, le=8_000_000)] = 4096,
    adc_sample_rate_hz: RateHz = None,
    la_samples: Annotated[int, Field(ge=0, le=8_000_000)] = 4096,
    la_sample_rate_hz: RateHz = None,
    stop_dac_after: Annotated[Optional[float], Field(description="Seconds into the capture to cut a running DAC.")] = None,
    points: EnvelopePoints = 200,
    trigger_la: TriggerLa = None,
    trigger_edge: Annotated[TriggerEdge, Field(description="What starts both streams on trigger_la.")] = "rising",
    trigger_timeout: TriggerTimeoutS = 10.0,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.CorrelatedCaptureResult:
    """ADC and LA captured from one hardware trigger, so their timebases align. Both are kept.

    With trigger_la both streams start on that edge or level.
    """
    def op() -> m.CorrelatedCaptureResult:
        cc = SESSION.require().capture_correlated(
            adc_samples=adc_samples, adc_sample_rate_hz=adc_sample_rate_hz, la_samples=la_samples,
            la_sample_rate_hz=la_sample_rate_hz, stop_dac_after=stop_dac_after,
            trigger=_trigger(trigger_la, trigger_edge), trigger_timeout=trigger_timeout)
        if adc_samples:
            SESSION.last_adc = cc.adc
        if la_samples:
            SESSION.last_la = cc.la
        return m.CorrelatedCaptureResult(adc=adc_summary(cc.adc, points), la=la_summary(cc.la))

    return await _call_reporting(ctx, "capturing", op)


@mcp.tool(annotations=_ann("Decode a logic capture"))
async def decode_la(
    protocol: DecodeProtocol,
    sda: Optional[LaPin] = None,
    scl: Optional[LaPin] = None,
    rx: Optional[LaPin] = None,
    baud: Optional[BaudRate] = None,
    sclk: Optional[LaPin] = None,
    mosi: Optional[LaPin] = None,
    miso: Optional[LaPin] = None,
    cs: Optional[LaPin] = None,
    mode: Literal[0, 1, 2, 3] = 0,
    capture: Annotated[Literal["last", "new"], Field(description=(
        "last = decode the previous capture_la; new = capture first (needs samples/sample_rate_hz)."))] = "last",
    samples: Samples = 16384,
    sample_rate_hz: Annotated[float, Field(gt=0, le=50_000_000)] = 1_000_000,
    max_items: Annotated[int, Field(ge=1, le=5000)] = 200,
) -> m.DecodeResult:
    """Decode I2C (sda, scl), UART (rx, baud) or SPI (sclk + mosi/miso/cs, mode) from LA data.

    Sample at least ~10x the bit rate. Decoding the last capture again with other channels is free.
    """
    def op() -> m.DecodeResult:
        pod = SESSION.require()
        if protocol == "i2c" and (sda is None or scl is None):
            raise ValueError("i2c needs sda and scl")
        if protocol == "uart" and (rx is None or baud is None):
            raise ValueError("uart needs rx and baud")
        if protocol == "spi" and sclk is None:
            raise ValueError("spi needs sclk")
        la = SESSION.last_la
        if capture == "new" or la is None:
            la = pod.capture_la(samples, sample_rate_hz=sample_rate_hz)
            SESSION.last_la = la
        channels = {k: v for k, v in (("sda", sda), ("scl", scl), ("rx", rx), ("baud", baud),
                                      ("sclk", sclk), ("mosi", mosi), ("miso", miso), ("cs", cs))
                    if v is not None}
        if protocol == "spi":
            channels["mode"] = mode
        frames = pod.decode(la, protocol, **channels)
        text = None
        if protocol == "i2c":
            items = i2c.format_transactions(frames).splitlines()
        elif protocol == "uart":
            items = [f"{f.start_us / 1e6:.6f}s {f.hex} {f.text!r}" + ("" if f.ok else f" error={f.error}")
                     for f in frames]
            text = clip(sdk_decode.uart_text(frames), MAX_TEXT)
        else:
            items = [f"{f.start_us / 1e6:.6f}s mosi={f.mosi_hex} miso={f.miso_hex}" for f in frames]
        return m.DecodeResult(protocol=protocol, count=len(frames), items=items[:max_items],
                              truncated=len(items) > max_items, text=text)

    return await _call(op)


def _pulse_stats(widths: List[float]) -> m.PulseStats:
    if not widths:
        return m.PulseStats(count=0)
    return m.PulseStats(count=len(widths), min=min(widths), max=max(widths),
                        mean=sum(widths) / len(widths))


@mcp.tool(annotations=_ann("Measure timing on a logic capture", read_only=True))
async def la_timing(
    la: LaPin,
    edge: Annotated[Edge, Field(description="Which transitions to list and to time the delay from.")] = "rising",
    to_la: Annotated[Optional[LaPin], Field(description="Also measure the delay to the next to_edge on this channel.")] = None,
    to_edge: Edge = "rising",
    after: Annotated[float, Field(ge=0, description="Ignore edges earlier than this many seconds into the capture.")] = 0.0,
    max_edges: Annotated[int, Field(ge=1, le=5000)] = 100,
) -> m.LaTimingResult:
    """Timing of one channel of the last capture_la / capture_correlated: edge timestamps, pulse widths,
    frequency and duty cycle; with to_la, the delay from its first edge to the next edge on to_la
    (e.g. a trigger pin to a "result ready" pin). Resolution is one sample; no new capture is taken.
    """
    def op() -> m.LaTimingResult:
        cap = SESSION.last_la
        if cap is None:
            raise SessionStateError("no LA capture yet — run capture_la first")
        times = [t for t in cap.edge_times(la, edge) if t >= after]
        return m.LaTimingResult(
            la=la, edge=edge, edge_times=times[:max_edges], truncated=len(times) > max_edges,
            frequency_hz=cap.frequency(la), duty_cycle=round(cap.duty_cycle(la), 4),
            high_pulses=_pulse_stats(cap.pulse_widths(la, 1)),
            low_pulses=_pulse_stats(cap.pulse_widths(la, 0)),
            to_la=to_la,
            delay=None if to_la is None else cap.delay(la, to_la, from_edge=edge, to_edge=to_edge,
                                                     after=after),
            resolution=1.0 / cap.sample_rate_hz if cap.sample_rate_hz > 0 else 0.0)

    return await _call(op, lock=False)


# -- DAC ------------------------------------------------------------------------------

@mcp.tool(annotations=_ann("Generate a waveform", destructive=True))
async def generate(
    waveform: Waveshape,
    freq_hz: Annotated[float, Field(gt=0)],
    amplitude: Annotated[float, Field(gt=0, description="Peak volts.")],
    offset: Annotated[Optional[float], Field(description="Centre volts; default mid-range of dac_path.")] = None,
    dac_path: DacPath = "5v",
    duration: Annotated[Optional[float], Field(description="Seconds; omit to run until dac_stop.")] = None,
    sample_rate_hz: RateHz = None,
    on_capture: Annotated[bool, Field(description="Start on the next capture's hardware t0 (phase-locked).")] = False,
    route: Annotated[bool, Field(description=(
        "Route dac_path first. false = keep the current analog path (e.g. after analog_path('cal1') "
        "for a DAC-to-ADC loopback); dac_path then only sets the volts scaling."))] = True,
) -> m.GenerateResult:
    """Drive a sine, square or sawtooth on a DAC output (built from 8-bit levels)."""
    handle = await _call(lambda: SESSION.require().generate(
        waveform, freq_hz=freq_hz, amplitude=amplitude, offset=offset, dac_path=dac_path,
        duration=duration, sample_rate_hz=sample_rate_hz, on_capture=on_capture, route=route))
    return m.GenerateResult(waveform=waveform, freq_hz=freq_hz, dac_path=dac_path, cotrig=handle.cotrig)


@mcp.tool(annotations=_ann("Stop the DAC", idempotent=True))
async def dac_stop() -> m.StopResult:
    """Stop any DAC output: generator, replay or control loop."""
    await _call(lambda: SESSION.require().dac_stop())
    return m.StopResult()


def _image_name(info: Any) -> Optional[str]:
    """The tool-level name of the image an SDK switch went to (None when there was no switch)."""
    if info is None:
        return None
    return "loop" if int(info.image) == int(FpgaImage.LOOP) else "deep_replay"


def _replay_result(handle: Any) -> m.ReplayResult:
    return m.ReplayResult(samples=handle.samples, sample_rate_hz=handle.sample_rate_hz,
                          dac_path=handle.dac_path, deep=handle.deep, cotrig=handle.cotrig,
                          switched_image=_image_name(handle.switched_image))


@mcp.tool(annotations=_ann("Replay a waveform", destructive=True))
async def replay(
    volts: Annotated[Optional[List[float]], Field(description="Waveform in volts, one value per sample.")] = None,
    from_last_capture: Annotated[bool, Field(description="Replay the last capture_adc instead of volts.")] = False,
    dac_path: DacPath = "5v",
    mapping: Annotated[ReplayMapping, Field(description="faithful = reproduce volts (clip); fit = auto-scale.")] = "faithful",
    sample_rate_hz: Annotated[Optional[float], Field(description="Replay rate; defaults to the capture's own rate.")] = None,
    fault: Optional[m.FaultSpec] = None,
    on_capture: bool = False,
    route: Annotated[bool, Field(description="Route dac_path first (false = keep the current analog path).")] = True,
    switch_image: SwitchImage = True,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.ReplayResult:
    """Loop a waveform out of the DAC until dac_stop — agent-provided volts or the last ADC capture.

    More than 2048 samples need the deep_replay gateware image, switched to automatically.
    """
    if (volts is None) == (not from_last_capture):
        raise ToolError("invalid argument: pass exactly one of volts or from_last_capture=true")

    def op() -> m.ReplayResult:
        pod = SESSION.require()
        source: Any = volts
        if from_last_capture:
            if SESSION.last_adc is None:
                raise SessionStateError("no ADC capture yet — run capture_adc first")
            source = SESSION.last_adc
        return _replay_result(pod.replay(source, dac_path=dac_path, mapping=mapping,
                                         sample_rate_hz=sample_rate_hz, fault=_fault(fault),
                                         on_capture=on_capture, route=route,
                                         switch_image=switch_image))

    return await _call_reporting(ctx, "uploading replay", op)


@mcp.tool(annotations=_ann("List library waveforms", read_only=True, cloud=True))
async def list_waveforms() -> m.WaveformList:
    """The organisation's cloud waveform library (needs a cloud connection or BENCHPOD_API_KEY)."""
    wfs = await _call(lambda: SESSION.require().waveforms.list())
    return m.WaveformList(waveforms=[m.WaveformInfo(id=w.id, name=w.name, kind=w.kind,
                                                    sample_count=w.sample_count,
                                                    sample_rate_hz=w.sample_rate_hz) for w in wfs])


@mcp.tool(annotations=_ann("Replay a library waveform", destructive=True, cloud=True))
async def replay_waveform(
    waveform_id: str,
    dac_path: Annotated[Optional[DacPath], Field(description="Output path; default the waveform's own (else 5v).")] = None,
    mapping: ReplayMapping = "faithful",
    sample_rate_hz: RateHz = None,
    window_start: Annotated[int, Field(ge=0, description="First recording sample to replay.")] = 0,
    window_len: Annotated[int, Field(ge=0, description="Samples to replay; 0 = to the end.")] = 0,
    target_samples: Annotated[int, Field(ge=0, description="Downsample a shallow replay to this many samples.")] = 0,
    fault: Optional[m.FaultSpec] = None,
    on_capture: bool = False,
    switch_image: SwitchImage = True,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.ReplayResult:
    """Loop a cloud-library waveform out of the DAC until dac_stop (needs server access).

    A recording too long for a shallow replay switches the pod to the deep_replay gateware image
    (unless target_samples asks for a downsampled replay).
    """
    return await _call_reporting(ctx, "arming replay", lambda: _replay_result(
        SESSION.require().replay_waveform(
            waveform_id, dac_path=dac_path, mapping=mapping, sample_rate_hz=sample_rate_hz,
            window_start=window_start, window_len=window_len, target_samples=target_samples,
            fault=_fault(fault), on_capture=on_capture, switch_image=switch_image)))


@mcp.tool(annotations=_ann("Save capture to the library", cloud=True))
async def save_capture_as_recording(
    name: Annotated[str, Field(min_length=1)],
    full_scale_v: Annotated[Optional[float], Field(description="Volts the top code represents; default the peak.")] = None,
) -> m.RecordingResult:
    """Save the last capture_adc to the cloud waveform library as a replayable recording."""
    def op() -> m.RecordingResult:
        pod = SESSION.require()
        if SESSION.last_adc is None:
            raise SessionStateError("no ADC capture yet — run capture_adc first")
        wf = pod.save_capture_as_recording(SESSION.last_adc, name, full_scale_v=full_scale_v)
        return m.RecordingResult(id=wf.id, name=wf.name, kind=wf.kind, sample_count=wf.sample_count)

    return await _call(op)


# -- control loop ------------------------------------------------------------------------

@mcp.tool(annotations=_ann("Arm the DAC control loop", destructive=True))
async def control_loop(
    curve: Annotated[Optional[List[Code16]], Field(description=(
        "Transfer function: DAC output codes indexed by input (1-2048 points, spread over the input range)."))] = None,
    voc_code: Annotated[Optional[int], Field(description="Instead of curve: solar-panel I-V preset with this open-circuit code.")] = None,
    sharpness: Annotated[float, Field(ge=1, le=64, description="Knee sharpness of the panel preset.")] = 4.0,
    points: Annotated[int, Field(ge=2, le=2048)] = 256,
    k: Annotated[int, Field(ge=1, le=32767, description="Damping, Q15 (32767 = jump straight to the target).")] = 8192,
    vmin: Code16 = 0,
    vmax: Code16 = 65535,
    tick_div: Annotated[int, Field(ge=8, le=65535)] = 64,
    source: Annotated[Optional[LoopSource], Field(description=(
        "adc = closed loop; fixed = hold input_code (open loop); sweep = advance by step each tick."))] = None,
    input_code: Code16 = 0,
    step: Code16 = 0,
    input_map: Optional[m.InputMapSpec] = None,
    switch_image: SwitchImage = True,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.LoopArmResult:
    """Run a tabulated transfer function in the FPGA: each tick out = curve[input], damped and clamped.

    Needs the loop gateware image, switched to automatically. Poll with loop_probe, move the input
    with loop_input, stop with dac_stop.
    """
    if curve is not None and not 1 <= len(curve) <= 2048:
        raise ToolError("invalid argument: curve needs 1-2048 points")
    imap = None if input_map is None else LoopInputMap(**input_map.model_dump())

    def op() -> m.LoopArmResult:
        h = SESSION.require().control_loop(curve=curve, voc_code=voc_code, sharpness=sharpness,
                                           points=points, k=k, vmin=vmin, vmax=vmax,
                                           tick_div=tick_div, source=source, input_code=input_code,
                                           step=step, input_map=imap, switch_image=switch_image)
        return m.LoopArmResult(armed=h.armed, k=h.k, vmin=h.vmin, vmax=h.vmax, tick_div=h.tick_div,
                               curve_points=h.curve_pts, source=h.source, input_code=h.input_code,
                               step=h.step, switched_image=_image_name(h.switched_image))

    return await _call_reporting(ctx, "arming the control loop", op)


@mcp.tool(annotations=_ann("Move the loop input", destructive=True))
async def loop_input(
    input_code: Optional[Code16] = None,
    source: Optional[LoopSource] = None,
    step: Optional[Code16] = None,
) -> m.LoopStateResult:
    """Re-target the running loop's input without re-arming (hold a point, meter, move on)."""
    s = await _call(lambda: SESSION.require().loop_input(input_code, source=source, step=step))
    return m.LoopStateResult(source=s.source, input_code=s.input_code, step=s.step, output_code=s.output_code)


@mcp.tool(annotations=_ann("Probe the control loop", read_only=True))
async def loop_probe() -> m.LoopProbeResult:
    """The running loop's live operating point; assert against loop_input, not i, in open-loop runs."""
    pt = await _call(lambda: SESSION.require().loop_probe())
    return m.LoopProbeResult(i=pt.i, v=pt.v, input_code=pt.input_code, source=pt.source,
                             loop_input=pt.loop_input)


@mcp.tool(annotations=_ann("Switch gateware image", destructive=True))
async def fpga_image(
    image: Annotated[Literal["loop", "deep_replay"], Field(description=(
        "loop = control-loop image; deep_replay = deep DAC replay from PSRAM."))],
    ctx: Context = None,  # type: ignore[assignment]
) -> m.FpgaImageResult:
    """Reprogram the pod's FPGA with another stored gateware image (~2-3 s).

    Rarely needed: control_loop, replay and replay_waveform switch automatically. The switch resets
    the FPGA (any DAC output, UART session or I2C sensor emulation stops).
    """
    img = FpgaImage.LOOP if image == "loop" else FpgaImage.DEEP_REPLAY
    info = await _call_reporting(ctx, "switching gateware", lambda: SESSION.require().fpga_image(img))
    return m.FpgaImageResult(image=image, version=info.version, features=info.features)


# -- CAN ------------------------------------------------------------------------------

def _frame(f: Any) -> m.CanFrameInfo:
    return m.CanFrameInfo(id=f.id, id_hex=f"0x{f.id:X}", data=list(f.data), ext=f.ext, rtr=f.rtr, ts_ms=f.ts)


@mcp.tool(annotations=_ann("Open CAN"))
async def can_open(
    bitrate: Annotated[int, Field(ge=10_000, le=1_000_000)] = 500_000,
    mode: Annotated[CanMode, Field(description=(
        "normal = on a bus with other nodes; internal/external = loopback self-test; listen = silent."))] = "normal",
    term: Annotated[bool, Field(description="Engage the 120 ohm termination.")] = False,
) -> m.CanOpenResult:
    """Bring up the pod's CAN interface (replacing any open CAN session)."""
    def op() -> m.CanOpenResult:
        pod = SESSION.require()
        SESSION.close_can()
        SESSION.can = pod.open_can(bitrate=bitrate, mode=mode, term=term)
        SESSION.can_config = (bitrate, mode, term)
        return m.CanOpenResult(open=True, bitrate=bitrate, mode=mode, term=term)

    return await _call(op)


@mcp.tool(annotations=_ann("Send a CAN frame", destructive=True))
async def can_write(
    can_id: Annotated[int, Field(ge=0, le=0x1FFFFFFF)],
    data: Annotated[Optional[List[Byte]], Field(description="0-8 data bytes.")] = None,
    ext: Annotated[bool, Field(description="29-bit extended identifier.")] = False,
    rtr: bool = False,
) -> m.CanWriteResult:
    """Queue one classic CAN frame."""
    await _call(lambda: SESSION.require_can().write(can_id, data or [], ext=ext, rtr=rtr))
    return m.CanWriteResult()


@mcp.tool(annotations=_ann("Read CAN frames", read_only=True))
async def can_read(
    timeout: Annotated[float, Field(ge=0, le=120, description=(
        "With can_id: wait up to this long for it. Without: collect frames for this long (0 = what is buffered)."))] = 0.0,
    can_id: Optional[Annotated[int, Field(ge=0, le=0x1FFFFFFF)]] = None,
    max_frames: Annotated[int, Field(ge=1, le=1000)] = 32,
) -> m.CanReadResult:
    """Read received CAN frames (id, data, timestamps in pod milliseconds)."""
    def op() -> m.CanReadResult:
        SESSION.require()
        bus = SESSION.require_can()
        if can_id is not None:
            f = bus.read_until(can_id, timeout=timeout)
            return m.CanReadResult(frames=[_frame(f)] if f else [], matched=f is not None)
        frames = bus.collect(timeout) if timeout > 0 else bus.read(max_frames)
        return m.CanReadResult(frames=[_frame(f) for f in frames[:max_frames]])

    return await _call(op)


@mcp.tool(annotations=_ann("CAN auto-responder", destructive=True))
async def can_respond(
    match_id: Optional[int] = None,
    reply_id: Optional[int] = None,
    reply_data: Optional[List[Byte]] = None,
    match_ext: bool = False,
    reply_ext: bool = False,
    clear: Annotated[bool, Field(description="Remove all responder rules instead of adding one.")] = False,
) -> m.CanRespondResult:
    """Make the pod firmware answer a CAN id instantly (ECU simulation), or clear all rules."""
    def op() -> m.CanRespondResult:
        bus = SESSION.require_can()
        if clear:
            bus.clear_responders()
            return m.CanRespondResult(cleared=True)
        if match_id is None or reply_id is None:
            raise ValueError("pass match_id and reply_id (or clear=true)")
        reply = bus.add_responder(match_id, reply_id, reply_data or [], match_ext=match_ext,
                                  reply_ext=reply_ext)
        rules = reply.get("rules") if isinstance(reply, dict) else None
        return m.CanRespondResult(rules=int(rules) if rules is not None else None)

    return await _call(op)


@mcp.tool(annotations=_ann("CAN status", read_only=True))
async def can_status() -> m.DeviceReply:
    """CAN link state: mode, bitrate, error counters, bus-off."""
    return m.DeviceReply(reply=await _call(lambda: SESSION.require().can_status()))


@mcp.tool(annotations=_ann("Close CAN", idempotent=True))
async def can_close() -> m.CanOpenResult:
    """Clear responder rules and stop CAN (safe when not open)."""
    await _call(SESSION.close_can)
    return m.CanOpenResult(open=False)


# -- stepper + escape hatch ------------------------------------------------------------

@mcp.tool(annotations=_ann("Step pulse train", destructive=True))
async def la_step(
    la: LaPin,
    steps: Annotated[int, Field(ge=1, le=10_000_000)],
    delay: Annotated[float, Field(gt=0, le=10, description="Seconds between step pulses.")],
    dir_la: Optional[LaPin] = None,
    direction: Literal[0, 1] = 0,
) -> m.StepResult:
    """Emit step pulses on an LA channel (step/dir motor drivers); with dir_la, set direction first.

    The FPGA runs the train by itself; this returns as soon as it starts.
    """
    reply = await _call(lambda: SESSION.require().la_step(la, steps=steps, delay=delay, dir_la=dir_la,
                                                         direction=direction))
    return m.StepResult(la=la, steps=steps, delay=delay, status=str(reply.get("status", "started")))


@mcp.tool(annotations=_ann("Raw firmware command", destructive=True))
async def command(
    request: Annotated[Dict[str, Any], Field(description='A firmware JSON command with a "cmd" key, e.g. {"cmd": "usb_cc"}.')],
) -> m.CommandResult:
    """Escape hatch for firmware commands no tool covers. Prefer the dedicated tools."""
    return m.CommandResult(reply=await _call(lambda: SESSION.require().command(request)))


# -- resources ----------------------------------------------------------------------------

@mcp.resource("benchpod://wiring", name="wiring", title="BenchPod wiring reference",
              description=("The connected device's wiring profile (which DUT signal is on which LA "
                           "channel), then LA channels, bias resistors, eFuses and analog paths."))
def wiring_resource() -> str:
    """The connected bench's own profile followed by the static reference (reference only when
    there is no connection, or when the profile cannot be read)."""
    if SESSION.connected:
        try:
            with SESSION.lock:
                profile = SESSION.require().wiring
            return (f"This bench's wiring profile (source: {profile.source})\n\n"
                    f"{profile.describe()}\n\n{WIRING}")
        except (BenchPodError, ValueError):
            pass
    return WIRING


@mcp.resource("benchpod://help", name="help", title="How to drive a BenchPod",
              description="The server instructions: session start, typical flows, error contract.")
def help_() -> str:
    return INSTRUCTIONS
