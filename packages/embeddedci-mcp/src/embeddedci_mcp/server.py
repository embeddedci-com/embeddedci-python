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

import dataclasses
import inspect
import json
import os
import re
from dataclasses import replace
import time
from typing import Annotated, Any, Callable, Dict, List, Literal, Optional, Tuple, TypeVar, Union

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from embeddedci.benchpod import (
    EXPANSION_CHIPS,
    ExtPinState,
    AdcSource,
    AnalogPath,
    AdcGainSource,
    AdcRange,
    CalibrateSource,
    CalibrationPart,
    DacCalPath,
    CanMode,
    DacOutputPath,
    DacPath,
    DecodeProtocol,
    Edge,
    Fault,
    FpgaImage,
    GpioMode,
    GpioSend,
    I2CSend,
    LoopInputMap,
    LoopSource,
    ReplayMapping,
    SpiSend,
    Trigger,
    TriggerEdge,
    Waveshape,
    Wiring,
    i2c,
)

try:  # embeddedci 3.0.2+: the profile on the pod's header as text
    from embeddedci.benchpod import header_map
except ImportError:  # pragma: no cover - older SDK: wiring leaves board_header/header_map null
    header_map = None  # type: ignore[assignment]

from embeddedci.benchpod import decode as sdk_decode
from embeddedci.benchpod.capabilities import Capabilities
from embeddedci.benchpod.client import API_BASE_ENV, API_KEY_ENV
from embeddedci.benchpod.cloud_auth import DEFAULT_API_BASE
from embeddedci.benchpod.connection import CLOUD_PREFIX
from embeddedci.benchpod import errors as _errors
from embeddedci.benchpod import protocol as _protocol
from embeddedci.benchpod.errors import FirmwareError
from embeddedci.benchpod.errors import BenchPodError
from embeddedci.benchpod.server_api import ServerApi

from . import cli_login
from . import models as m
from .guide import INSTRUCTIONS, WIRING
from .session import SESSION, SessionStateError
from .summaries import adc_summary, clip, la_summary, pin_state, power_summary, wiring_summary

mcp = FastMCP("embeddedci-benchpod", instructions=INSTRUCTIONS)

T = TypeVar("T")

#: Longest text (UART output, decode traces) returned in one result.
MAX_TEXT = 16_000

LaPin = Annotated[int, Field(ge=1, le=14, description="LA channel (1-14) the signal is wired to.")]
#: An LA channel given as a number or by the name the wiring profile gives it.
LaRef = Annotated[Union[Annotated[int, Field(ge=1, le=14)], str], Field(description=(
    "LA channel 1-14, or a name from the wiring profile — a signal ('READY') or a role "
    "('uart_rx', 'i2c_sda', 'swd_swclk'). Call `wiring` to see the names. The gpio_* tools also "
    "take expander pins ('EXT1') once one is attached (expansion)."))]
PullPin = Annotated[int, Field(ge=1, le=14)]
EfuseRail = Annotated[Literal[1, 2], Field(description="Target-power rail: internal 5 V (efuse 1) or external (efuse 2).")]
WiredEfuse = Annotated[Optional[Literal[1, 2]], Field(description=(
    "Target-power rail: internal 5 V (efuse 1) or external (efuse 2). Omit for the wiring profile's rail."))]
BaudRate = Annotated[int, Field(ge=300, le=4_000_000)]
WiredBaud = Annotated[Optional[int], Field(ge=300, le=4_000_000, description=(
    "UART baud rate; omit for the wiring profile's uart_baud."))]
TriggerLa = Annotated[Optional[Union[Annotated[int, Field(ge=1, le=14)], str]], Field(description=(
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
    "stopping any DAC output, UART session or sensor emulation. false = fail instead."))]


# -- plumbing --------------------------------------------------------------------

def _ann(title: str, *, read_only: bool = False, destructive: bool = False,
         idempotent: bool = False, cloud: bool = False) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=read_only,
                           destructiveHint=None if read_only else destructive,
                           idempotentHint=idempotent, openWorldHint=cloud)


def _refusal_hint(exc: BenchPodError) -> str:
    """What to do about a policy refusal or a missing feature, or ``""``. The classes are new in
    ``embeddedci`` after 2.6.0 (UnsupportedFeatureError after 2.7.0); with an older SDK nothing
    matches and the message stays as it was."""
    def isa(name: str) -> bool:
        cls = getattr(_errors, name, None)
        return cls is not None and isinstance(exc, cls)

    if isa("PodLockedError"):
        return ("The pod's LAN policy keeps this command for the cloud or the USB console: connect "
                "with 'embeddedci:<device>' (or 'usb'), or have an owner open the LAN policy.")
    if isa("PodLeasedError"):  # the message already names the holder and the time left
        return "Wait for that job to finish and retry; reads such as status still work meanwhile."
    if isa("PodBusyError"):
        return "The pod is busy with a capture or upload. Wait for it to finish and retry."
    if isa("PermissionDeniedError"):
        return ("The credential in use is not allowed to do this. It needs an organization owner "
                "or admin (an API key needs the benchpod:admin scope), or access to this device.")
    if isa("TransportTimeout"):
        return "The pod did not answer in time. Check that it is online (status), then retry."
    if isa("UnsupportedFeatureError"):  # embeddedci after 2.7.0
        if getattr(exc, "feature", "") == "analog":
            return ("This is the digital-only board: it has no analog front end (ADC, DAC, "
                    "4-20 mA). Use a pod with the analog board for this.")
        return ("This pod's firmware or gateware lacks the feature (see the capabilities in "
                "status). Update the pod's firmware if a newer release has it; retrying will not "
                "help.")
    return ""


#: How to update this server, for a pod whose firmware is newer than it handles.
_MCP_UPDATE = ("update embeddedci-mcp: uvx embeddedci-mcp@latest or pip install -U embeddedci-mcp, "
               "or update the benchpod plugin; then reconnect")


def _mcp_version() -> str:
    try:
        from importlib.metadata import version
        return version("embeddedci-mcp")
    except Exception:   # a source tree without install
        return "unknown"


def _name_this_client() -> None:
    """Requests from this server carry ``"client": "mcp <version>"`` (embeddedci after 3.0.5)."""
    set_name = getattr(_protocol, "set_client_name", None)
    if callable(set_name):
        set_name(f"mcp {_mcp_version()}")


_name_this_client()


def _client_too_old_text(exc: BenchPodError) -> Optional[str]:
    """The ClientTooOldError of embeddedci after 3.0.5 (a pod above this server's client API
    level on connect, or the pod's BP0030), told as what to update: this server, not the SDK."""
    cls = getattr(_errors, "ClientTooOldError", None)
    if cls is None or not isinstance(exc, cls):
        return None
    fw = getattr(exc, "firmware_version", "") or ""
    pod = f"the pod's firmware {fw}" if fw else "the pod's firmware"
    have, api, need = (getattr(exc, "client_api", 0), getattr(exc, "api", 0),
                       getattr(exc, "min_client_api", 0))
    if getattr(exc, "code", "") == "BP0030":
        why = f"{pod} needs client API {need or '?'} or newer, this server sent {have}"
    else:
        why = f"{pod} speaks client API {api or '?'}, this server handles {have}"
    cmd = getattr(exc, "cmd", None)
    text = (f"this BenchPod MCP server (embeddedci-mcp {_mcp_version()}) is too old for this pod: "
            f"{why}. Tell the user to {_MCP_UPDATE}")
    return f"{cmd}: {text}" if cmd else text


def _coded_text(exc: BenchPodError) -> Optional[str]:
    """A refusal with a stable error code, rendered from the catalog with its fix for an agent
    (``fix.mcp``, naming this server's tools). The pod sends no text with a code."""
    from embeddedci.benchpod.errors import error_catalog
    code = getattr(exc, "code", "")
    entry = error_catalog().get("codes", {}).get(code) if code else None
    if not entry:
        return None
    values = getattr(exc, "values", {}) or {}
    fill = lambda t: re.sub(r"\{([a-z0-9_]+)\}", lambda m: str(values.get(m.group(1), "?")), t)  # noqa: E731
    cmd = getattr(exc, "cmd", None)
    text = f"{code}: {fill(entry['message'])}; {fill(entry['fix']['mcp'])} (docs: {exc.docs_url})"
    return f"{cmd}: {text}" if cmd else text


def _firmware_too_old_text(exc: BenchPodError) -> Optional[str]:
    """The SDK's FirmwareTooOldError: the pod runs firmware 3.x, which this server does not drive."""
    if not isinstance(exc, _errors.FirmwareTooOldError):
        return None
    fw = getattr(exc, "firmware_version", "") or "3.x"
    return (f"the pod runs firmware {fw} (client API {getattr(exc, 'api', 1)}); this BenchPod MCP "
            f"server (embeddedci-mcp {_mcp_version()}) needs firmware 4.0.0 or later. Tell the user "
            "to update the pod's firmware with the BenchPod CLI (benchpod flash-self) or the "
            "firmware update in the web app, then reconnect; retrying will not help")


def _tool_error(exc: BenchPodError) -> ToolError:
    fw_old = _firmware_too_old_text(exc)
    if fw_old:
        return ToolError(f"{type(exc).__name__}: {fw_old}")
    too_old = _client_too_old_text(exc)
    if too_old:
        return ToolError(f"{type(exc).__name__}: {too_old}")
    coded = _coded_text(exc)
    if coded:
        return ToolError(f"{type(exc).__name__}: {coded}")
    msg = f"{type(exc).__name__}: {exc}"
    hint = _refusal_hint(exc)
    return ToolError(f"{msg}. {hint}" if hint else msg)


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
        raise _tool_error(exc) from exc
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
    """An LA channel from a number 1-14 or a wiring-profile name. An expander pin (EXTn) is
    refused with the reason this tool needs an LA channel."""
    if isinstance(value, str):
        resolve = getattr(pod, "_resolve_la", None)
        return resolve(value)[0] if resolve is not None else pod.wiring.la(value)
    return int(value)


def _gpio_ref(pod: Any, value: Any) -> Union[int, str]:
    """For the gpio_* tools: an LA channel number, or ``"EXTn"`` for an expander pin (by EXT
    number or its name)."""
    if isinstance(value, str):
        n = pod._ext(value)
        if n is not None:
            return f"EXT{n}"
    return _la(pod, value)


def ext_pin_state(pin: Any) -> m.ExtPinResult:
    return m.ExtPinResult(pin=pin.pin, name=pin.name, chip_pin=pin.chip_pin, addr=pin.addr,
                          mode=pin.mode, level=pin.level, live=pin.live)


def _any_pin_state(pin: Any) -> Union[m.LaPinResult, m.ExtPinResult]:
    return ext_pin_state(pin) if isinstance(pin, ExtPinState) else pin_state(pin)


def _is_esp_target(target: str) -> bool:
    """``target`` names an ESP32-family chip (``esp32``, ``esp32s3``, ``ESP32-C3``)."""
    return bool(re.fullmatch(r"esp32(-?(s2|s3|c2|c3|c5|c6|h2|p4))?", str(target).strip().lower()))


#: ``flash(target=...)`` names for chips with their own programming pins (the SDK's prog.TARGETS).
_PROG_TARGETS = frozenset({"avr-updi", "updi", "pic", "efm8", "c2", "attiny-tpi", "tpi", "xmega", "pdi", "msp430", "sbw"})


def _is_prog_target(target: str) -> bool:
    return str(target).strip().lower() in _PROG_TARGETS


def _channel(pod: Any, value: Any, key: str) -> int:
    """Like :func:`_la`, but an omitted channel comes from the profile's ``key`` role."""
    if value is not None:
        return _la(pod, value)
    la = getattr(pod.wiring, key)
    if la is None:
        raise ValueError(f"{key} was not given and the wiring profile has no {key}; pass it or "
                         "set it in the profile (see the `wiring` tool)")
    return int(la)


def _trigger(la: Any, edge: str, pre_samples: int = 0) -> Optional[Trigger]:
    """A :class:`Trigger` for the capture tools (the SDK resolves wiring names)."""
    return None if la is None else Trigger(la, edge, pre_samples)  # type: ignore[arg-type]


# -- connection ---------------------------------------------------------------------

def _status() -> m.StatusResult:
    if not SESSION.connected and not SESSION.reconnectable:
        default = SESSION.default_connection
        hint = (f"call connect (the server default is {default!r})" if default
                else "call connect: without a connection it uses the one saved with `benchpod "
                     "set-connection`, else finds the one pod on the LAN over mDNS; "
                     "else pass a host, 'usb' or 'embeddedci:<device>'")
        return m.StatusResult(connected=False, warnings=[f"not connected — {hint}"])
    pod = SESSION.require()
    warnings: List[str] = []
    try:
        firmware = pod.status()
    except FirmwareError as exc:
        # A status too large for the cloud channel (BP0054): the connection works, so report the
        # status as unknown rather than failing.
        if exc.code != "BP0054":
            raise
        firmware = {}
        warnings.append("this pod's status is too large for the cloud channel; the status fields "
                        "are unknown")
    if isinstance(firmware, dict) and firmware.get("short_form") and "lan_tls" not in firmware:
        # The cloud status is the short form: lan_tls is its own command there.
        try:
            tls = pod.lan_tls()
            firmware["lan_tls"] = {"port": tls.port, "https_only": tls.https_only, "sha256": tls.sha256}
        except BenchPodError:
            pass
    # From this status, not the cached capabilities: it describes the boot running now.
    boot = Capabilities.from_status(firmware).boot_warning()
    if boot:
        warnings.append(boot)
    module = firmware.get("module") if isinstance(firmware, dict) else None
    if isinstance(module, dict) and module.get("dac_pinout_ok") is False:
        warnings.append("DAC off: the digital board and the analog module use different flat-cable "
                        "pinouts (module.dac_pinout_ok is false), so every DAC command is refused "
                        "(BP0080). Tell the user the two boards do not belong together: a digital "
                        "v4 or later board needs an analog module v4 or later")
    try:
        la = pod.get_la_voltage().voltage
    except BenchPodError:
        la = None
    lease = firmware.get("lease") if isinstance(firmware, dict) else None
    if isinstance(lease, dict) and lease.get("held") and not pod.leased:
        warnings.append(f"a cloud job holds this pod ({lease.get('holder') or 'cloud'}, "
                        f"{lease.get('left_s', '?')} s left): over the LAN it can be read, not driven")
    local_lease = _local_lease(pod)
    if local_lease is not None and local_lease.get("held") and not local_lease.get("mine"):
        warnings.append(f"{local_lease.get('holder') or 'someone'} holds this pod's lease "
                        f"({local_lease.get('purpose') or 'no purpose given'}, "
                        f"{local_lease.get('left_s', '?')} s left): it can be read, not driven, "
                        "until they are done")
    if isinstance(firmware, dict) and firmware.get("lan_policy") == "locked" and SESSION.kind == "tcp":
        warnings.append("the pod's LAN policy is locked: configuration and firmware commands need "
                        "the cloud ('embeddedci:<device>') or the USB console")
    fallback = getattr(pod.transport, "usb_lan_fallback", "")
    if isinstance(fallback, str) and fallback:
        warnings.append(f"connection 'usb' went to the pod's network address because {fallback}; the "
                        "pod's external LAN policy applies there (`benchpod clear-wifi` turns USB "
                        "networking back on)")
    usb = firmware.get("usb") if isinstance(firmware, dict) else None
    if isinstance(usb, dict) and usb.get("source") == "default":
        warnings.append("the USB port powering the pod offers only default USB power (500 mA), which may "
                        "not carry the pod plus a DUT on the internal 5 V rail: use a USB-C port or "
                        "charger with 1.5 A or more, or power the DUT externally")
    try:
        wiring_source: Optional[str] = pod.wiring.source
    except (BenchPodError, ValueError, OSError):
        wiring_source = None
    dirs = getattr(pod, "pin_directions_status", "")
    if isinstance(dirs, str) and dirs.startswith("not sent") and "firmware" not in dirs:
        warnings.append(f"the wiring's pin directions were {dirs} (call la_dir(from_wiring=true) "
                        "once that pin is released)")
    if la is None:
        warnings.append("LA I/O voltage not selected, and the wiring profile sets none: ask the user for "
                        "the board's I/O voltage (1.8 or 3.3, 5.0 on a digital v4 pod) and set la_mv in the profile (set_wiring, "
                        "then save_wiring), or call set_la_voltage, before flash, UART, LA capture, "
                        "pulls or sensor emulation")
    try:
        macs = pod.mac_addresses(firmware if isinstance(firmware, dict) else {})
        eth_mac, wifi_mac = macs.eth_mac, macs.wifi_mac
    except (BenchPodError, AttributeError):
        eth_mac = wifi_mac = None
    return m.StatusResult(
        connected=True, connection=SESSION.connection, kind=SESSION.kind, leased=pod.leased,
        la_voltage=la, capabilities=m.CapabilitiesInfo.from_caps(pod.capabilities),
        session=SESSION.info(), warnings=warnings, firmware=firmware, wiring_source=wiring_source,
        eth_mac=eth_mac, wifi_mac=wifi_mac, local_lease=local_lease,
    )


def _local_lease(pod: Any) -> Optional[Dict[str, Any]]:
    """The pod's own lease from its status (feature ``lease``), or None when the pod or the SDK
    has none."""
    read = getattr(pod, "local_lease", None)
    if not callable(read):
        return None
    try:
        if not pod.supports("lease"):
            return None
        lease = read()
    except BenchPodError:
        return None
    if not getattr(lease, "reported", False):
        return None
    return {"held": lease.held, "holder": lease.holder, "purpose": lease.purpose,
            "left_s": lease.left_s, "mine": lease.mine}


@mcp.tool(annotations=_ann("Connect to a BenchPod", idempotent=True, cloud=True))
async def connect(
    connection: Annotated[Optional[str], Field(description=(
        "host[:port] (TCP: TLS on 8443 by default, with the pod's certificate pinned on first use; "
        "plaintext only with an explicit :8080 or on the USB network adapter), a serial device "
        "path (the USB console, status only), 'usb' "
        "(auto-detect: the pod's USB network adapter with the full API, else "
        "its console), 'usb:<name>' (one of several pods on USB, by name: 'usb:benchpod-baea06'), "
        "'discover' (mDNS) or 'embeddedci:<device-name>' (cloud). Omit to use the server default "
        "(--connection, else BENCHPOD_CONNECTION, else the connection saved with `benchpod "
        "set-connection`), else to find the one pod on the LAN over mDNS."))] = None,
    la_voltage: Annotated[Optional[Literal[1.8, 3.3, 5.0]], Field(description=(
        "Select the LA I/O-bank voltage right after connecting (the DUT's I/O voltage; 5.0 needs the "
        "digital board v4)."))] = None,
    lease_wait_s: Annotated[float, Field(ge=0, le=3600, description=(
        "Seconds to wait when someone else holds the pod's lease (the cloud device's, or the "
        "pod's own on the LAN and USB network)."))] = 30.0,
    wiring_profile: Annotated[Optional[str], Field(description=(
        "A wiring profile stored on the pod to use (the `wiring` tool lists them); omit for the "
        "pod's default profile."))] = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.StatusResult:
    """Open a BenchPod connection (closing any previous one and its sessions) and report status.

    Over the cloud the device is shared: this takes an exclusive lease, released by `disconnect`
    or after the server's idle timeout (the next tool call then reconnects).
    """
    def op() -> m.StatusResult:
        SESSION.connect(connection, la_voltage=la_voltage, lease_wait=lease_wait_s,
                        wiring_profile=wiring_profile)
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


def _pin21(state: Any) -> Optional[m.Pin21Result]:
    p = getattr(state, "pin21", None)   # embeddedci before digital v4 support has none
    if p is None:
        return None
    return m.Pin21Result(source=p.source, setting=p.setting, measured=p.measured, state=p.state, up=p.up)


@mcp.tool(annotations=_ann("Set LA I/O voltage", idempotent=True))
async def set_la_voltage(la_voltage: Annotated[Optional[Literal[1.8, 3.3, 5.0]], Field(description=(
        "The DUT's I/O voltage. 5.0 needs a pod with the digital board v4 (status capabilities "
        "vref_out). Omit it to read the current voltage and pin 21 without changing anything."))] = None,
) -> m.LaVoltageResult:
    """Select the LA I/O-bank voltage to match the DUT's I/O (1.8 V needs a rev3 pod, 5.0 a digital
    v4 board).

    Usually not needed: connect sets it from the wiring profile's la_mv, and firmware 3.10.10+
    takes it from the profile on the first LA operation. Call it when the profile sets none (ask
    the user), or to override the profile for this session.

    At 5.0 outputs reach 5 V only through their pull-ups: switch on the pull-up (set_pull up) on
    outputs into 5 V CMOS-level inputs. Point the user to https://embeddedci.com/docs/benchpod-targets/5v-rs485#5v-io for the details. On digital
    v4 the result has pin21; when its state is high, tell the user a DUT signal is above the I/O
    voltage (select 5.0 or remove the signal).
    """
    if la_voltage is None:
        state = await _call(lambda: SESSION.require().get_la_voltage())
    else:
        state = await _call(lambda: SESSION.require().set_la_voltage(la_voltage))
    return m.LaVoltageResult(voltage=state.voltage, readback=state.readback, pin21=_pin21(state))


# -- wiring profile -------------------------------------------------------------------

#: Changes that are never saved on their own (autosave): what can hurt a board, or a new board.
_NEVER_AUTOSAVE = ("la_mv", "efuse", "swd_target")


def _pod_wiring_state(pod: Any, result: m.WiringResult) -> m.WiringResult:
    """The pod's stored profiles, what this connection has not saved, and the hint about it."""
    result.autosave = SESSION.wiring_autosave
    try:
        listing = pod.wiring_profiles()
    except BenchPodError:
        return result   # firmware that stores no profiles
    result.default_profile = listing.get("default")
    result.profiles = list(listing.get("profiles") or [])
    w = pod.wiring
    names = {str(p.get("name")) for p in result.profiles}
    stored = pod.stored_wiring(w.profile_name) if w.profile_name in names else None
    cur, ref = w.to_dict(), (stored.to_dict() if stored is not None else {})
    result.unsaved_changes = sorted(k for k in set(cur) | set(ref)
                                    if k != "version" and cur.get(k) != ref.get(k))
    if result.unsaved_changes:
        target = w.profile_name or result.default_profile or "default"
        result.hint = (f"not saved: the web app, CI and other connections still use profile {target!r}. "
                       "Once the user confirms this wiring is right on the board, ask whether to save it: "
                       "save_wiring() for this profile, save_wiring(name=...) for another board.")
        roles = [k for k in result.unsaved_changes if k in ("uart_rx", "uart_tx", "i2c_sda", "i2c_scl",
                                                             "swd_swclk", "swd_swdio", "jtag_tck", "jtag_tms",
                                                             "jtag_tdi", "jtag_tdo", "jtag_trst",
                                                             "spi_sclk", "spi_mosi",
                                                             "spi_miso", "spi_cs", "signals")]
        if any(k in _NEVER_AUTOSAVE for k in result.unsaved_changes) or len(roles) >= 4:
            result.hint += (" This looks like a different board (I/O voltage, power rail, SWD target or "
                            "most pins changed): prefer save_wiring(name='<board>') over overwriting "
                            f"{target!r}.")
    return result


@mcp.tool(annotations=_ann("Wiring profile", read_only=True))
async def wiring() -> m.WiringResult:
    """Which DUT signal is on which LA channel — call this first, before any channel argument.

    Returns the profile this connection uses (normally a profile stored on the pod: profile_name,
    the pod's default unless connect named another): a 14-row pin table, the named signals, the
    power rail, the UART baud and the SWD target, warnings about risky wiring, the pod's stored
    profiles and unsaved_changes (what set_wiring changed that is not stored yet), and header_map:
    the profile on the pod's real I/O header as text, to show the user how to wire their board. Every tool whose
    channel, baud, rail or SWD arguments are omitted takes them from this profile; what the profile
    does not set is undefined, and a tool that needs it says so: ask the user, never guess a pin.
    """
    def op() -> m.WiringResult:
        pod = SESSION.require()
        result = wiring_summary(pod.wiring)
        result.directions = pod.pin_directions_status
        if header_map is not None and hasattr(pod, "board_header"):
            try:
                result.board_header = pod.board_header()
            except Exception:  # noqa: BLE001 - the pin map is extra: a pod that cannot say gets both headers
                result.board_header = None
            result.header_map = header_map(pod.wiring, result.board_header)
        return _pod_wiring_state(pod, result)

    return await _call(op)


@mcp.tool(annotations=_ann("Set the wiring profile", idempotent=True, cloud=True))
async def set_wiring(
    profile: Annotated[Dict[str, Any], Field(description=(
        "The profile as a JSON object (schema version 2): la_mv, efuse, uart_rx/uart_tx/uart_baud, "
        "i2c_sda/i2c_scl/i2c_addr, swd_swclk/swd_swdio/swd_nreset/swd_target, jtag_tck/jtag_tms/jtag_tdi/jtag_tdo/jtag_trst, debug_port (\"swd\" or \"jtag\": what flash uses when "
        "the call passes no pins; null: automatic), spi_*, and signals "
        "[{name, la, direction, active_low, description}], pulls (the pull resistors power_on switches "
        "on, the others off: {\"1\": \"up\", \"2\": \"up\", \"8\": \"down\"}, or a list of channels "
        "that pull their own way, e.g. [1, 2]; today LA1-LA6 pull up, LA7/LA8 down; left out: "
        "power_on leaves the pulls as they are). What it leaves out is undefined (not "
        "wired); a pin set to null is not wired. {\"version\": 1} objects fill in the old default pins."))],
    save: Annotated[bool, Field(description=(
        "Also store it on the pod (as save_wiring does). Default false: this connection only, unless "
        "the server runs with --wiring-autosave."))] = False,
) -> m.WiringResult:
    """Replace the wiring profile this connection uses. It is NOT stored on the pod unless save is
    true (or the server autosaves): the result's unsaved_changes and hint say what is not stored.

    The profile is validated first: two roles or signals on one LA channel, an out-of-range pin or
    an unknown key are rejected with a message naming every problem. Autosave never stores a change
    of the I/O voltage, the power rail or the SWD target: call save_wiring for those.
    """
    def op() -> m.WiringResult:
        pod = SESSION.require()
        before = pod.wiring
        data = dict(profile)
        data.setdefault("version", 2)
        pod.wiring = replace(Wiring.from_dict(data), profile_name=before.profile_name,
                             profile_rev=before.profile_rev, source="dict")
        risky = any(before.to_dict().get(k) != pod.wiring.to_dict().get(k) for k in _NEVER_AUTOSAVE)
        do_save = save or (SESSION.wiring_autosave and not risky)
        if do_save:
            pod.save_wiring()
        result = wiring_summary(pod.wiring, saved=do_save)
        # Assigning the profile sent its pin directions (la_dir) on firmware that has it.
        result.directions = pod.pin_directions_status
        result = _pod_wiring_state(pod, result)
        if SESSION.wiring_autosave and risky and not save:
            result.hint = ("not autosaved: the I/O voltage, power rail or SWD target changed. " +
                           (result.hint or "Call save_wiring once the user confirms it."))
        return result

    return await _call(op)


@mcp.tool(annotations=_ann("Save the wiring profile on the pod", idempotent=True, cloud=True))
async def save_wiring(
    name: Annotated[Optional[str], Field(description=(
        "The pod profile to store it as (1-40 characters: a-z, 0-9, -). Default: the profile this "
        "connection loaded, else the pod's default. Use a new name for a different board."))] = None,
    profile: Annotated[Optional[Dict[str, Any]], Field(description=(
        "A full profile to store instead of this connection's (schema version 2)."))] = None,
) -> m.WiringResult:
    """Store a wiring profile on the pod, where the web app, CI and every other connection see it.

    Ask the user first: saving changes what everyone uses. Saving the profile this connection
    loaded fails with BP0009 when someone else saved it meanwhile (load_wiring shows their version).
    Saving never changes the pod's default profile (set_default_wiring does).
    """
    def op() -> m.WiringResult:
        pod = SESSION.require()
        data = None
        if profile is not None:
            data = dict(profile)
            data.setdefault("version", 2)
        pod.save_wiring(data, name=name)
        result = wiring_summary(pod.wiring, saved=True)
        result.directions = pod.pin_directions_status
        return _pod_wiring_state(pod, result)

    return await _call(op)


@mcp.tool(annotations=_ann("Load a stored wiring profile", idempotent=True))
async def load_wiring(
    name: Annotated[Optional[str], Field(description="The pod profile to use; omit for the pod's default.")] = None,
) -> m.WiringResult:
    """Switch this connection to one of the pod's stored profiles (nothing on the pod changes).
    Use it when the user says the bench is wired for another board, or after a BP0009 conflict."""
    def op() -> m.WiringResult:
        pod = SESSION.require()
        pod.load_wiring(name)
        result = wiring_summary(pod.wiring)
        result.directions = pod.pin_directions_status
        return _pod_wiring_state(pod, result)

    return await _call(op)


@mcp.tool(annotations=_ann("Set the default wiring profile", idempotent=True, cloud=True))
async def set_default_wiring(
    name: Annotated[str, Field(description="The pod profile every connection uses when it names none.")],
) -> m.WiringResult:
    """Make a stored profile the pod's default (ask the user: it changes what everyone gets)."""
    def op() -> m.WiringResult:
        pod = SESSION.require()
        pod.set_default_wiring(name)
        result = wiring_summary(pod.wiring)
        return _pod_wiring_state(pod, result)

    return await _call(op)


@mcp.tool(annotations=_ann("Delete a stored wiring profile", destructive=True, cloud=True))
async def delete_wiring(
    name: Annotated[str, Field(description="The pod profile to delete (not the default).")],
) -> m.WiringResult:
    """Delete one of the pod's stored profiles. Ask the user first. The default cannot be deleted
    (BP0013): make another one the default first."""
    def op() -> m.WiringResult:
        pod = SESSION.require()
        pod.delete_wiring(name)
        result = wiring_summary(pod.wiring)
        return _pod_wiring_state(pod, result)

    return await _call(op)


# -- power ----------------------------------------------------------------------------

def _switch_power(efuse: Optional[int], on: bool, delay: Optional[float],
                  wiring_profile: Optional[str] = None) -> m.PowerResult:
    pod = SESSION.require()
    if on:
        pod.power_on(efuse, delay=delay, wiring_profile=wiring_profile)
    else:
        pod.target_power(efuse, on=False, delay=delay)
    w = pod.wiring
    rail = w.efuse if efuse is None else efuse
    return m.PowerResult(efuse=rail, on=on, delay=delay, profile=w.profile_name or None,
                         la_voltage=w.la_voltage if on else None)


@mcp.tool(annotations=_ann("Power target on", destructive=True, idempotent=True))
async def power_on(
    efuse: WiredEfuse = None,
    delay_s: Annotated[Optional[float], Field(description=(
        "Seconds: schedule the power-on pod-side and return at once (e.g. to power on during a capture)."))] = None,
    wiring_profile: Annotated[Optional[str], Field(description=(
        "A stored wiring profile to power the board with (it becomes this connection's profile); "
        "omit to use the current one."))] = None,
) -> m.PowerResult:
    """Power the board on with its wiring profile applied first: the profile's I/O voltage is set
    (when the pod has none or another one), its pull resistors (pulls; at 1.8 V only on firmware newer than 3.10.12) are set, then the
    rail goes on, and the pod records the profile.
    The result names the rail, the profile and the I/O voltage. Never changes the I/O voltage under
    a board that is already powered."""
    return await _call(lambda: _switch_power(efuse, True, delay_s, wiring_profile))


@mcp.tool(annotations=_ann("Power target off", destructive=True, idempotent=True))
async def power_off(
    efuse: WiredEfuse = None,
    delay_s: Annotated[Optional[float], Field(description="Seconds: schedule the power-off pod-side.")] = None,
) -> m.PowerResult:
    """Switch the target's power rail off. The result says which rail was used."""
    return await _call(lambda: _switch_power(efuse, False, delay_s))


@mcp.tool(annotations=_ann("Profile the target's power"))
async def measure_power(
    duration_s: Annotated[float, Field(gt=0, le=600, description="Seconds to sample (blocking).")],
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
        SESSION.require().measure_power(duration_s, efuse=efuse, rate_hz=rate_hz,
                                        keep_samples=points), points))


@mcp.tool(annotations=_ann("Start a power profile"))
async def power_profile_start(
    efuse: WiredEfuse = None,
    rate_hz: Annotated[float, Field(ge=100, le=500, description=(
        "Samples per second; accurate to ~200 Hz, flattening near 365 Hz above that."))] = 500.0,
    max_duration_s: Annotated[float, Field(gt=0, le=600, description=(
        "Seconds after which the pod stops sampling by itself (the result is then truncated)."))] = 60.0,
) -> m.PowerProfileStartResult:
    """Start sampling the target-power rail in the background, then run the steps you want to
    profile and call power_profile_stop. Replaces any profile already running."""
    def op() -> m.PowerProfileStartResult:
        pod = SESSION.require()
        rail = pod.wiring.efuse if efuse is None else efuse
        SESSION.power_profile = pod.power_profile(efuse=rail, rate_hz=rate_hz,
                                                  max_duration=max_duration_s).start()
        return m.PowerProfileStartResult(efuse=rail, rate_hz=rate_hz, max_duration=max_duration_s)

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
    """Both target-power rails, internal 5 V (efuse 1) and external (efuse 2): on/off and tripped
    state, bus voltage and current draw."""
    def op() -> m.PowerStatusResult:
        pod = SESSION.require()
        ts, ps = pod.target_status(), pod.power_status()

        def rail(n: int) -> m.RailResult:
            e, r = ts.efuse(n), ps.rail(n)
            return m.RailResult(enabled=e.enabled, fault=e.fault, state_valid=e.valid,
                                monitor_ok=r.ok, bus_voltage=r.bus_voltage, current=r.current)

        pod = None
        if ps.pod is not None:
            pod = m.PodPowerResult(monitor_ok=ps.pod.ok, bus_voltage=ps.pod.bus_voltage,
                                   current=ps.pod.current, total_current=ps.total_current)
        return m.PowerStatusResult(internal=rail(1), external=rail(2), pod=pod)

    return await _call(op)


@mcp.tool(annotations=_ann("Reset target", destructive=True))
async def reset_target(
    action: Annotated[Literal["pulse", "hold", "release", "status"], Field(description=(
        "pulse = reset once; hold = keep the target in reset; release = let it run; status = read only."))] = "pulse",
    pulse_s: Annotated[float, Field(gt=0, le=1, description="Pulse length in seconds (at most 1).")] = 0.1,
) -> m.ResetResult:
    """Drive the DUT's reset line from the pod's reset pin (rev3 pods, DUT header J1 pin 22)."""
    def op() -> m.ResetResult:
        pod = SESSION.require()
        if action == "pulse":
            state = pod.reset_target(pulse=pulse_s)
        elif action == "status":
            state = pod.reset_state()
        else:
            state = pod.set_reset(action == "hold")
        return m.ResetResult(asserted=state.asserted, supported=state.supported)

    return await _call(op)


# -- SPI flash --------------------------------------------------------------------------

_SPI_PIN = ("LA channel (or wiring name) for {}; omit for the profile's {}.")
SpiMode = Annotated[Literal[0, 1, 2, 3], Field(description="SPI mode; 1 and 2 need gateware v55+.")]


def _spi_kwargs(sck: Any, mosi: Any, miso: Any, cs: Any, hz: int, mode: int) -> Dict[str, Any]:
    return dict(sck=sck, mosi=mosi, miso=miso, cs=cs, hz=hz, mode=mode)


@mcp.tool(annotations=_ann("Read the SPI flash's ID", read_only=True))
async def spi_flash_info(
    sck: Annotated[Optional[LaRef], Field(description=_SPI_PIN.format("SCK", "spi_sclk"))] = None,
    mosi: Annotated[Optional[LaRef], Field(description=_SPI_PIN.format("MOSI (the chip's DI)", "spi_mosi"))] = None,
    miso: Annotated[Optional[LaRef], Field(description=_SPI_PIN.format("MISO (the chip's DO)", "spi_miso"))] = None,
    cs: Annotated[Optional[LaRef], Field(description=_SPI_PIN.format("CS", "spi_cs"))] = None,
    hz: Annotated[int, Field(ge=190_000, le=6_000_000, description="SCK rate; rounded down to what the pod can do.")] = 1_000_000,
    mode: SpiMode = 0,
) -> m.SpiFlashInfoResult:
    """Read the JEDEC ID of the SPI NOR flash on the SPI pins (gateware v45+, capability spi_master).

    present=false means the ID read all 00 or all FF: nothing is answering (wiring, power, or /WP
    and /HOLD not tied high). The pins are released again afterwards.

    Gateware v65+: the emulation image (loaded by sensor_start) has no SPI master. With no I2C/SPI/UART sensor running the pod switches to the loop image by itself (~3 s); with sensors running this refuses (BP0059): sensor_stop first, or do it before sensor_start.
    """
    def op() -> m.SpiFlashInfoResult:
        pod = SESSION.require()
        with pod.open_spi(**_spi_kwargs(sck, mosi, miso, cs, hz, mode)) as spi:
            i = spi.flash_id()
            return m.SpiFlashInfoResult(jedec_id=i.jedec_id, present=i.present, size=i.size,
                                        status=i.status, hz=spi.hz)

    return await _call(op)


@mcp.tool(annotations=_ann("Program an SPI flash", destructive=True))
async def spi_flash_program(
    file: Annotated[str, Field(description="Image path on the machine running this server (raw binary).")],
    addr: Annotated[int, Field(ge=0, lt=1 << 24, description="Flash address to write the image at.")] = 0,
    erase: Annotated[bool, Field(description="Erase the 4 KB sectors the image covers first.")] = True,
    verify: Annotated[bool, Field(description="The pod reads every chunk back and fails on a difference.")] = True,
    hold_reset: Annotated[bool, Field(description=(
        "Hold the DUT in reset for the job (its controller then cannot drive the same bus); released after."))] = False,
    sck: Annotated[Optional[LaRef], Field(description=_SPI_PIN.format("SCK", "spi_sclk"))] = None,
    mosi: Annotated[Optional[LaRef], Field(description=_SPI_PIN.format("MOSI (the chip's DI)", "spi_mosi"))] = None,
    miso: Annotated[Optional[LaRef], Field(description=_SPI_PIN.format("MISO (the chip's DO)", "spi_miso"))] = None,
    cs: Annotated[Optional[LaRef], Field(description=_SPI_PIN.format("CS", "spi_cs"))] = None,
    hz: Annotated[int, Field(ge=190_000, le=6_000_000)] = 6_000_000,
    mode: SpiMode = 0,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.SpiFlashProgramResult:
    """Erase, write and verify an image into the SPI NOR flash wired to the SPI pins.

    25-series parts, first 16 MB. Throughput is roughly 30 KB/s over the LAN (erase is extra) and
    slower over the cloud. Fails with the pod's message when nothing answers, the part is
    write-protected, or a chunk does not verify.

    Gateware v65+: the emulation image (loaded by sensor_start) has no SPI master. With no I2C/SPI/UART sensor running the pod switches to the loop image by itself (~3 s); with sensors running this refuses (BP0059): sensor_stop first, or do it before sensor_start.
    """
    def op() -> m.SpiFlashProgramResult:
        pod = SESSION.require()
        r = pod.spi_flash(file, addr, erase=erase, verify=verify, hold_reset=hold_reset,
                          **_spi_kwargs(sck, mosi, miso, cs, hz, mode))
        return m.SpiFlashProgramResult(jedec_id=r.jedec_id, addr=r.addr, length=r.length,
                                       erased=r.erased, verified=r.verified, seconds=round(r.seconds, 2))

    return await _call_reporting(ctx, "programming the SPI flash", op)


@mcp.tool(annotations=_ann("Read an SPI flash range", read_only=True))
async def spi_flash_read(
    addr: Annotated[int, Field(ge=0, lt=1 << 24)],
    length: Annotated[int, Field(gt=0, le=16 << 20)],
    file: Annotated[str, Field(description=(
        "Write the bytes to this path on the server's machine; omit to get up to 4096 bytes back as hex."))] = "",
    sck: Annotated[Optional[LaRef], Field(description=_SPI_PIN.format("SCK", "spi_sclk"))] = None,
    mosi: Annotated[Optional[LaRef], Field(description=_SPI_PIN.format("MOSI (the chip's DI)", "spi_mosi"))] = None,
    miso: Annotated[Optional[LaRef], Field(description=_SPI_PIN.format("MISO (the chip's DO)", "spi_miso"))] = None,
    cs: Annotated[Optional[LaRef], Field(description=_SPI_PIN.format("CS", "spi_cs"))] = None,
    hz: Annotated[int, Field(ge=190_000, le=6_000_000)] = 6_000_000,
    mode: SpiMode = 0,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.SpiFlashReadResult:
    """Read bytes back from the SPI NOR flash, to a file or (up to 4096 bytes) as hex."""
    if not file and length > 4096:
        raise ValueError("more than 4096 bytes needs a file path")

    def op() -> m.SpiFlashReadResult:
        pod = SESSION.require()
        with pod.open_spi(**_spi_kwargs(sck, mosi, miso, cs, hz, mode)) as spi:
            data = spi.flash_read(addr, length)
        if file:
            with open(file, "wb") as f:
                f.write(data)
            return m.SpiFlashReadResult(addr=addr, length=len(data), file=file)
        return m.SpiFlashReadResult(addr=addr, length=len(data), hex=data.hex())

    return await _call_reporting(ctx, "reading the SPI flash", op)


@mcp.tool(annotations=_ann("SPI transfer", destructive=True))
async def spi_transfer(
    tx_hex: Annotated[str, Field(description="Bytes to send, as hex (e.g. 9f000000 reads a JEDEC ID). Up to 4096 bytes.")],
    sck: Annotated[Optional[LaRef], Field(description=_SPI_PIN.format("SCK", "spi_sclk"))] = None,
    mosi: Annotated[Optional[LaRef], Field(description=_SPI_PIN.format("MOSI", "spi_mosi"))] = None,
    miso: Annotated[Optional[LaRef], Field(description=_SPI_PIN.format("MISO", "spi_miso"))] = None,
    cs: Annotated[Optional[LaRef], Field(description=_SPI_PIN.format("CS", "spi_cs"))] = None,
    hz: Annotated[int, Field(ge=190_000, le=6_000_000)] = 1_000_000,
    mode: SpiMode = 0,
) -> m.SpiTransferResult:
    """One full-duplex SPI transaction (CS asserted for all of tx) with any SPI device on the SPI pins.

    Returns what came back on MISO. The SPI master is armed for the call and released after it.

    Gateware v65+: the emulation image (loaded by sensor_start) has no SPI master. With no I2C/SPI/UART sensor running the pod switches to the loop image by itself (~3 s); with sensors running this refuses (BP0059): sensor_stop first, or do it before sensor_start.
    """
    data = bytes.fromhex(tx_hex.replace(" ", ""))
    if not data or len(data) > 4096:
        raise ValueError("tx_hex must be 1..4096 bytes")

    def op() -> m.SpiTransferResult:
        pod = SESSION.require()
        with pod.open_spi(**_spi_kwargs(sck, mosi, miso, cs, hz, mode)) as spi:
            return m.SpiTransferResult(rx_hex=spi.transfer(data).hex(), hz=spi.hz)

    return await _call(op)


# -- I2C master -------------------------------------------------------------------------

@mcp.tool(annotations=_ann("Open the I2C master", destructive=True))
async def i2c_open(
    sda: Annotated[Optional[LaRef], Field(description="SDA channel; omit for the profile's i2c_sda.")] = None,
    scl: Annotated[Optional[LaRef], Field(description="SCL channel; omit for the profile's i2c_scl.")] = None,
    hz: Annotated[int, Field(ge=10_000, le=1_000_000, description="SCL rate.")] = 100_000,
    pullups: Annotated[Literal["pod", "external"], Field(description=(
        "pod: channels with the pod's pull-up on (set_pull(pull=\"up\") first, LA1-LA6 today); "
        "external: the board has its own."))] = "pod",
) -> m.I2cSessionResult:
    """Make the pod an I2C master on two LA pins (gateware v55+), replacing any open session.

    Then i2c_transfer, i2c_scan, or capture_la with send. SWD, SPI and I2C exclude each other.
    "i2c busy" means another connection (the desktop app, the web app, a script) holds the
    pod's I2C master: close it there.

    Gateware v65+: the emulation image (loaded by sensor_start) has no I2C master. With no I2C/SPI/UART sensor running the pod switches to the loop image by itself (~3 s); with sensors running this refuses (BP0059): sensor_stop first, or do it before sensor_start.
    """
    def op() -> m.I2cSessionResult:
        pod = SESSION.require()
        SESSION.close_i2c()
        i2c = pod.open_i2c(sda, scl, hz=hz, pullups=pullups)
        SESSION.i2c = i2c
        return m.I2cSessionResult(open=True, sda=i2c.sda, scl=i2c.scl, hz=i2c.hz, pullups=i2c.pullups)

    return await _call(op)


@mcp.tool(annotations=_ann("I2C transfer", destructive=True))
async def i2c_transfer(
    addr: Annotated[int, Field(ge=0, le=127, description="7-bit address.")],
    write_hex: Annotated[str, Field(description="Bytes to write, hex (up to 128), e.g. a register.")] = "",
    read: Annotated[int, Field(ge=0, le=248, description="Bytes to read after a repeated START.")] = 0,
) -> m.I2cTransferResult:
    """Write, read, or write then read on the open I2C session. Neither = a probe.

    A NACK is not an error: ack is false and nack_at says which byte.
    """
    data = bytes.fromhex(write_hex.replace(" ", ""))

    def op() -> m.I2cTransferResult:
        r = SESSION.require_i2c().xfer(addr, data, read)
        return m.I2cTransferResult(addr=r.addr, ack=r.ack, rx_hex=r.data.hex(), nack_at=r.nack_at)

    return await _call(op)


@mcp.tool(annotations=_ann("Scan the I2C bus", read_only=True))
async def i2c_scan(
    start: Annotated[int, Field(ge=0, le=127)] = 0x08,
    end: Annotated[int, Field(ge=0, le=127)] = 0x77,
) -> m.I2cScanResult:
    """Which addresses answer on the open I2C session."""
    def op() -> m.I2cScanResult:
        found = SESSION.require_i2c().scan(start, end)
        return m.I2cScanResult(found=found, found_hex=[f"0x{a:02x}" for a in found])

    return await _call(op)


@mcp.tool(annotations=_ann("Close the I2C master", idempotent=True))
async def i2c_close() -> m.I2cSessionResult:
    """Release the I2C master's pins (safe when none is open)."""
    await _call(SESSION.close_i2c)
    return m.I2cSessionResult(open=False)


# -- flash ------------------------------------------------------------------------------

@mcp.tool(annotations=_ann("Flash firmware over SWD or JTAG", destructive=True))
async def flash(
    swclk: Annotated[Optional[LaRef], Field(description=(
        "LA channel (or wiring name) wired to the DUT's SWCLK; omit for the profile's swd_swclk."))] = None,
    swdio: Annotated[Optional[LaRef], Field(description=(
        "LA channel (or wiring name) wired to the DUT's SWDIO; omit for the profile's swd_swdio."))] = None,
    tck: Annotated[Optional[LaRef], Field(description=(
        "JTAG: LA channel (or wiring name) wired to the DUT's TCK; omit for the profile's jtag_tck."))] = None,
    tms: Annotated[Optional[LaRef], Field(description=(
        "JTAG: LA channel (or wiring name) wired to the DUT's TMS; omit for the profile's jtag_tms."))] = None,
    tdi: Annotated[Optional[LaRef], Field(description=(
        "JTAG: LA channel (or wiring name) wired to the DUT's TDI; omit for the profile's jtag_tdi."))] = None,
    tdo: Annotated[Optional[LaRef], Field(description=(
        "JTAG: LA channel (or wiring name) wired to the DUT's TDO; omit for the profile's jtag_tdo."))] = None,
    trst: Annotated[Optional[LaRef], Field(description=(
        "JTAG: LA channel (or wiring name) wired to the DUT's nTRST (optional); omit for the "
        "profile's jtag_trst."))] = None,
    target: Annotated[str, Field(description=(
        "OpenOCD target config, e.g. target/stm32f4x.cfg; omit for the profile's swd_target. "
        "'esp32' (or esp32s3, esp32c3, ...) flashes an ESP32-family target over its UART ROM "
        "bootloader instead of SWD. 'avr-updi' (tinyAVR 0/1/2, megaAVR 0, AVR DA/DB/DD), 'pic' "
        "(PIC16F1xxx, PIC16F18xxx, PIC18F Q), 'efm8' (Silicon Labs EFM8, C8051F), 'attiny-tpi' "
        "(ATtiny4/5/9/10/20/40/102/104), 'xmega' and 'msp430' (MSP430 FRAM parts over Spy-Bi-Wire: "
        "clk = TEST, data = RST; gateware v52+) flash a chip over its own programming pins "
        "instead (updi, pic, c2, tpi, pdi and sbw work too)."))] = "",
    file: Annotated[str, Field(description=(
        "Firmware image path on the machine running this server. Leave empty to flash an uploaded "
        "build instead (build/repo/ref/artifact). For an ESP32: a .bin written at 0 (esptool "
        "merge_bin) or an ESP-IDF build/flasher_args.json. For avr-updi, pic, efm8, attiny-tpi and "
        "xmega: a .hex, .elf or .bin (a .bin goes at load_address into the flash). Empty is fine "
        "with dry_run."))] = "",
    nreset: Annotated[Optional[bool], Field(description=(
        "The target's reset line is wired to the pod's reset pin: enables connect-under-reset. "
        "Omit for the profile's swd_nreset."))] = None,
    load_address: Annotated[str, Field(description="Load address for raw .bin images, e.g. 0x08000000.")] = "",
    target_power: Annotated[Optional[Literal[1, 2]], Field(description=(
        "Power this rail on before flashing: internal 5 V (efuse 1) or external (efuse 2)."))] = None,
    verify: bool = True,
    reset: bool = True,
    connect_under_reset: Optional[bool] = None,
    extra_configs: Optional[List[str]] = None,
    extra_args: Optional[List[str]] = None,
    timeout_s: Annotated[float, Field(gt=0, le=3600)] = 300.0,
    connect_attempts: Annotated[int, Field(ge=1, le=20)] = 5,
    build: Annotated[str, Field(description=(
        "Flash a build uploaded to embeddedci.com instead of a file: its build id (the upload's "
        "build_id output), or 'latest' for the newest one that matches repo/ref."))] = "",
    repo: Annotated[str, Field(description=(
        "With build: the GitHub repository of the uploaded build, 'owner/name'."))] = "",
    ref: Annotated[str, Field(description=(
        "With build: the branch or tag of the uploaded build, e.g. 'main'."))] = "",
    artifact: Annotated[str, Field(description=(
        "With build: the file to flash when the build holds several (default: its .elf, then .hex, "
        "then .bin)."))] = "",
    server_side: Annotated[Optional[bool], Field(description=(
        "With build: true has embeddedci.com run OpenOCD (the default on an 'embeddedci:<pod>' "
        "connection), false downloads the file and runs OpenOCD here (the default on LAN/USB)."))] = None,
    rx: Annotated[Optional[LaRef], Field(description=(
        "ESP32 only: LA channel (or wiring name) wired to the target's TXD0; omit for the "
        "profile's uart_rx."))] = None,
    tx: Annotated[Optional[LaRef], Field(description=(
        "ESP32 only: LA channel (or wiring name) wired to the target's RXD0; omit for the "
        "profile's uart_tx."))] = None,
    boot: Annotated[Optional[LaRef], Field(description=(
        "ESP32 only: LA channel (or wiring name) wired to the target's IO0 (IO9 on the C3/C6); "
        "omit for a profile signal named BOOT, IO0 or IO9."))] = None,
    device: Annotated[str, Field(description=(
        "avr-updi, pic, efm8, attiny-tpi, xmega: the chip, e.g. 'ATtiny1616' or 'PIC16F18446'. "
        "Omit: the chip's ID identifies it. Given, the ID must match."))] = "",
    clk: Annotated[Optional[LaRef], Field(description=(
        "pic, efm8, attiny-tpi, xmega: LA channel (or wiring name) wired to the chip's clock "
        "(ICSPCLK, C2CK, TPICLK, PDI_CLK); omit for a profile signal of that name, else swd_swclk."))] = None,
    data: Annotated[Optional[LaRef], Field(description=(
        "avr-updi, pic, efm8, attiny-tpi, xmega: LA channel (or wiring name) wired to the chip's "
        "data pin (UPDI, ICSPDAT, C2D, TPIDATA, PDI_DATA); omit for a profile signal of that name, "
        "else swd_swdio."))] = None,
    reset_pin: Annotated[Optional[LaRef], Field(description=(
        "pic and attiny-tpi: LA channel (or wiring name) wired to the chip's reset (MCLR, RESET), "
        "driven open drain; omit for the pod's reset pin (nreset) or a profile signal named MCLR "
        "or RESET."))] = None,
    erase: Annotated[Literal["chip", "page", "none"], Field(description=(
        "avr-updi, pic, efm8, attiny-tpi, xmega: 'chip' (default, before the first write when the "
        "image has flash; also unlocks a locked chip), 'page' (only the pages written; TPI chips "
        "erase their whole code section) or 'none'."))] = "chip",
    dry_run: Annotated[bool, Field(description=(
        "avr-updi, pic, efm8, attiny-tpi, xmega: only enter programming mode and read the chip's "
        "ID (a wiring test): stdout_tail names the chip, nothing is written. Other targets (SWD, "
        "JTAG, ESP32) have no dry run and refuse it: use the detect tool to read their IDs."))] = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.FlashResult:
    """Program the DUT over SWD or JTAG through the pod's CMSIS-DAP probe.

    SWD or JTAG is chosen for you, there is no transport argument: JTAG when the four JTAG pins
    (`tck`, `tms`, `tdi`, `tdo`, from the arguments or the profile's jtag_*) are known and either
    no SWD pins are, or the target is JTAG only (esp32, esp32s2, gd32vf103, fe310, k210 and riscv*
    OpenOCD configs, e.g. target/esp32.cfg); otherwise SWD. JTAG pins passed in the call mean JTAG,
    SWD pins passed mean SWD, else the profile's debug_port (swd or jtag) decides. JTAG needs the pod's `jtag` feature
    (newer firmware): without it the call fails before anything is armed.

    Gateware v65+: SWD and JTAG are not in the emulation image (loaded by sensor_start). Flash
    first, then start the sensors (one image switch). With no I2C/SPI/UART sensor running the pod
    switches to the loop image by itself (~3 s); with sensors running the flash refuses (BP0059):
    sensor_stop first. Flashing over UART or USB, or with the pod's own programming pins, switches
    images on its own as before.

    Give either `file` (a path on this server's host) or an uploaded build (`build`, or
    `repo`/`ref` for the newest build of a branch). OpenOCD runs on this server's host, except for
    an uploaded build on a cloud connection, which embeddedci.com flashes itself. The SWD pins, the
    reset flag and the target config come from the wiring profile when omitted. A failed flash is a
    normal result (ok=false): read target_unreachable (unpowered, mis-wired or held in reset),
    stalled, and the log tails to decide what to change; `artifact` says which build was flashed.

    With target 'esp32' (any ESP32-family name) the pod writes `file` through the target's UART
    ROM bootloader instead: TXD0 on `rx`, RXD0 on `tx`, IO0 on `boot`, EN on the pod's reset pin
    (`nreset`); `file` is a .bin written at `load_address` (default 0) or an ESP-IDF
    flasher_args.json. No OpenOCD or esptool needed; works over the cloud too. stdout_tail names
    the chip and each region written; `reset` false leaves the target in its loader.

    A chip with its own programming pins and no SWD, JTAG or bootloader is flashed by the pod:
    target 'avr-updi' (AVR tinyAVR 0/1/2, megaAVR 0, AVR DA/DB/DD, one pin), 'pic' (PIC16F1xxx,
    PIC16F18xxx, PIC18F Q-series, low-voltage entry), 'efm8' (Silicon Labs EFM8 and C8051F, C2),
    'attiny-tpi' (ATtiny4/5/9/10/20/40/102/104), 'xmega' (XMEGA, PDI) or 'msp430' (MSP430 FRAM parts,
    Spy-Bi-Wire: clk = TEST, data = RST, needs gateware v52+). `file` is a .hex, .elf or
    .bin; the tool reads the chip's ID, finds it in its device table, cuts the image into the
    chip's memories (flash, EEPROM, fuses or configuration words, user row or user ID) at their
    usual hex addresses and writes and reads back each page. Pins come from `clk`, `data` and
    `reset_pin`, else from wiring-profile signals named for the protocol (ICSPCLK, C2CK, UPDI, ...),
    else the profile's swd_swclk and swd_swdio; pic and attiny-tpi also use the pod's reset pin
    (`nreset`, the default) for MCLR or RESET. `erase` is 'chip' by default; `dry_run` only reads
    the chip's ID (check the wiring first). `dry_run` is for these targets only: an SWD, JTAG or
    ESP32 flash refuses it (use `detect` to see what answers on those pins). ATtiny4/5/9/10/20/40 need 5 V, which the pod's 1.8 and
    3.3 V I/O cannot give: the call fails before anything is written. A failure is ok=false with
    stderr_tail saying what to check (wiring, power, locked chip: erase 'chip' unlocks it). Needs
    firmware with the `prog_flash` feature and the xip_prog blob (`benchpod install-blobs`).
    """
    from_build = {k: v for k, v in (("build", build), ("repo", repo), ("ref", ref),
                                    ("artifact", artifact)) if v}
    if server_side is not None:
        from_build["server_side"] = server_side

    def op() -> m.FlashResult:
        pod = SESSION.require()
        if _is_prog_target(target):
            if from_build:
                raise ValueError(f"{target} is flashed from a file (a .hex, .elf or .bin), not an "
                                 "uploaded build")
            if "dry_run" not in inspect.signature(type(pod).flash).parameters:
                raise ValueError(f"flashing with target {target} needs a newer embeddedci SDK; "
                                 "upgrade embeddedci")
            r = pod.flash(target=target, file=file, load_address=load_address, nreset=nreset,
                          reset=reset, check=False, target_power=target_power, device=device,
                          clk=None if clk is None else _la(pod, clk),
                          data=None if data is None else _la(pod, data),
                          reset_pin=None if reset_pin is None else _la(pod, reset_pin),
                          erase=erase, dry_run=dry_run)
            return m.FlashResult(ok=r.ok, returncode=r.returncode,
                                 target_unreachable=r.target_unreachable, stalled=False,
                                 stdout_tail=clip(r.stdout, 4000)[-4000:],
                                 stderr_tail=clip(r.stderr, 4000)[-4000:])
        if dry_run:
            raise ValueError("dry_run only applies to the pin-programmer targets (avr-updi, pic, "
                             "efm8, attiny-tpi, xmega): an SWD, JTAG or ESP32 flash has no dry run. "
                             "Use the detect tool to read what answers on those pins without "
                             "flashing")
        if _is_esp_target(target or pod.wiring.swd_target):
            if from_build:
                raise ValueError("an ESP32 target is flashed from a file (a .bin or "
                                 "flasher_args.json), not an uploaded build")
            if "boot" not in inspect.signature(type(pod).flash).parameters:
                raise ValueError("flashing an ESP32 needs a newer embeddedci SDK; upgrade embeddedci")
            r = pod.flash(target=target or pod.wiring.swd_target, file=file,
                          load_address=load_address, nreset=nreset, reset=reset, check=False,
                          rx=None if rx is None else _la(pod, rx),
                          tx=None if tx is None else _la(pod, tx),
                          boot=None if boot is None else _la(pod, boot))
            return m.FlashResult(ok=r.ok, returncode=r.returncode, target_unreachable=False,
                                 stalled=False, stdout_tail=clip(r.stdout, 4000)[-4000:],
                                 stderr_tail=clip(r.stderr, 4000)[-4000:])
        if from_build and "build" not in inspect.signature(pod.flash).parameters:
            raise ValueError("flashing an uploaded build (build/repo/ref/artifact) needs a newer "
                             "embeddedci SDK; upgrade embeddedci, or pass file= with a local image")
        jtag_args = {k: _la(pod, v) for k, v in (("tck", tck), ("tms", tms), ("tdi", tdi),
                                                 ("tdo", tdo), ("trst", trst)) if v is not None}
        if jtag_args and "tck" not in inspect.signature(pod.flash).parameters:
            raise ValueError("flashing over JTAG needs a newer embeddedci SDK; upgrade embeddedci")
        if "tck" in inspect.signature(pod.flash).parameters:
            # the SDK picks SWD or JTAG from the pins and the target
            pins: Dict[str, Any] = {"swclk": None if swclk is None else _la(pod, swclk),
                                    "swdio": None if swdio is None else _la(pod, swdio), **jtag_args}
        else:
            pins = {"swclk": _channel(pod, swclk, "swd_swclk"),
                    "swdio": _channel(pod, swdio, "swd_swdio")}
        result = pod.flash(
            **pins,
            nreset=pod.wiring.swd_nreset if nreset is None else nreset,
            target=target or pod.wiring.swd_target, file=file,
            load_address=load_address, target_power=target_power, verify=verify, reset=reset,
            connect_under_reset=connect_under_reset, extra_configs=tuple(extra_configs or ()),
            extra_args=tuple(extra_args or ()), timeout=timeout_s,
            connect_attempts=connect_attempts, check=False,
            **from_build,
        )
        art = getattr(result, "artifact", None)
        return m.FlashResult(ok=result.ok, returncode=result.returncode,
                             target_unreachable=result.target_unreachable, stalled=result.stalled,
                             stdout_tail=clip(result.stdout, 4000)[-4000:],
                             stderr_tail=clip(result.stderr, 4000)[-4000:],
                             artifact=m.FlashedBuild(
                                 build=str(art.get("job_id") or ""), name=str(art.get("name") or ""),
                                 repo=art.get("repo"), ref=art.get("ref"), sha=art.get("sha"),
                             ) if art else None)

    return await _call_reporting(ctx, "flashing", op)


# -- detect -----------------------------------------------------------------------------

DetectMethod = Literal["swd", "jtag", "updi", "pic", "c2", "tpi", "pdi", "sbw", "esp32", "usb"]


@mcp.tool(annotations=_ann("Detect the chip on the board", idempotent=True))
async def detect(
    invasive: Annotated[bool, Field(description=(
        "true (default): also run the probes that reset the chip (programming-mode ID read, ESP32 "
        "ROM loader). false: only swd, jtag and usb, which never reset or write the chip."))] = True,
    methods: Annotated[Optional[List[DetectMethod]], Field(description=(
        "Only these probes: swd, jtag, updi, pic, c2, tpi, pdi, sbw, esp32, usb. Omit for every probe "
        "the wiring profile makes possible. A named probe the profile does not wire is reported "
        "in skipped."))] = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.DetectToolResult:
    """Find out which chip is wired to the pod, from the wiring profile.

    Needs target power on (power_on) and the LA voltage set; with the rail off nothing is probed
    (powered=false). `swd` and `jtag` read the debug port's IDs without halting, resetting or
    writing the chip (pod firmware with the `dap_id` feature; older firmware is reported in
    skipped) and name the chip from its ID register (an STM32's DEV_ID, a Nordic part) or JTAG
    IDCODE. `usb` reads the device already mounted on the pod's host port (usb_host first: detect
    never turns host mode on). With invasive=true the chip's programming protocols (`updi`, `pic`,
    `c2`, `tpi`, `pdi`, `sbw`: read the ID in programming mode) and `esp32` (the ROM loader names the
    chip) run too; they reset the chip, and an ESP32 is left in its ROM loader. A programming
    protocol is probed only when the profile has a signal named for it (UPDI, ICSPCLK + ICSPDAT,
    C2CK + C2D, TPICLK + TPIDATA, PDI_CLK + PDI_DATA, SBWTCK or TEST + SBWTDIO or RST (an MSP430 over Spy-Bi-Wire, firmware 4.0.6; a JTAG-locked chip is reported at low confidence); pic and tpi also MCLR or RESET, else the
    reset pin when the profile sets swd_nreset; esp32: uart_rx, uart_tx and a BOOT, IO0 or IO9
    signal, plus EN or swd_nreset): never on the SWD pins. A probe whose pins another tool owns
    (gpio, uart, sensor) is skipped with the owner, and a probe that errors or gets no answer is a
    skipped entry, not a failure.

    Each finding has chip, vendor, core, id and a confidence: high is an exact table match, medium
    a known vendor and core with an unknown part, low a debug port alone. Report medium and low
    findings as such: they are not a chip name."""
    def op() -> m.DetectToolResult:
        pod = SESSION.require()
        if not hasattr(type(pod), "detect"):
            raise ValueError("detecting the chip needs a newer embeddedci SDK; upgrade embeddedci")
        res = pod.detect(invasive=invasive, methods=methods)
        d = res.to_dict()
        return m.DetectToolResult(
            powered=d["powered"], findings=[m.DetectFindingInfo(**f) for f in d["findings"]],
            skipped=[m.DetectSkipped(**s) for s in d["skipped"]], notes=d["notes"])

    return await _call_reporting(ctx, "detecting", op)


# -- UART -----------------------------------------------------------------------------

def _uart_result(cap: Any) -> m.UartCaptureResult:
    return m.UartCaptureResult(text=clip(cap.text, MAX_TEXT), matched=cap.matched,
                               bytes=len(cap.text.encode("utf-8")), truncated=len(cap.text) > MAX_TEXT)


UartRx = Annotated[Optional[LaRef], Field(description=(
    "LA channel (or wiring name) wired to the DUT's TX; omit for the profile's uart_rx."))]
UartTx = Annotated[Optional[LaRef], Field(description=(
    "LA channel (or wiring name) wired to the DUT's RX; omit for the profile's uart_tx."))]
UartParity = Annotated[Optional[Literal["none", "even", "odd"]], Field(description=(
    "Parity bit after the 8 data bits; omit for the profile's uart_parity (none unless set). "
    "Anything but 8N1 needs firmware 3.10.8+ (gateware v56)."))]
UartStopBits = Annotated[Optional[Literal[1, 2]], Field(description=(
    "Stop bits after the data (and parity) bits; omit for the profile's uart_stop_bits (1 unless set)."))]
UartHalfDuplex = Annotated[bool, Field(description=(
    "One-wire UART: rx alone carries both directions (tx defaults to rx). The pod only pulls the "
    "line low and its pull-up (LA1-LA6) or the board's holds it high; the pod does not read back "
    "its own bytes. For boot-ROM programming pins such as RL78 TOOL0. Needs gateware v57."))]


class UartEntryArg(BaseModel):
    """A boot-mode entry the pod times itself before the session starts."""

    reset_ms: int = Field(10, ge=0, le=1000, description="Target held in reset (the pod's reset pin), ms.")
    tx_low: bool = Field(True, description="Hold TX low across the reset release (a boot strap on the UART pin).")
    hold_ms: int = Field(10, ge=0, le=1000, description="After the reset release, TX stays low this long, ms.")
    idle_ms: int = Field(10, ge=0, le=1000, description="From TX release (line idles high) to the first byte, ms.")
    data_hex: str = Field("", pattern=r"^([0-9a-fA-F]{2}){0,32}$",
                          description="Bytes sent right after idle_ms, as hex (at most 32 bytes).")


UartEntryOpt = Annotated[Optional[UartEntryArg], Field(description=(
    "Run a boot-mode entry on the pod first: TX low, reset, release, then the first bytes, all "
    "timed on the pod. Needs gateware v57 and the pod's reset pin. Omit for a plain session."))]


def _uart_frame(pod: Any, method: str, parity: Optional[str], stop_bits: Optional[int]) -> Dict[str, Any]:
    """The frame-format keyword arguments for an SDK UART call: none when both are omitted (the SDK
    takes the wiring profile's frame) or 8N1 on an older SDK; refused when the SDK predates them."""
    if parity is None and stop_bits is None:
        return {}
    has_frame = "parity" in inspect.signature(getattr(type(pod), method)).parameters
    if not has_frame and (parity or "none") == "none" and (stop_bits or 1) == 1:
        return {}
    if not has_frame:
        raise ValueError("UART parity and stop bits need a newer embeddedci SDK; upgrade embeddedci")
    return {k: v for k, v in (("parity", parity), ("stop_bits", stop_bits)) if v is not None}


UartDe = Annotated[Optional[LaRef], Field(description=(
    "LA channel (or wiring name) wired to an RS-485 transceiver's DE + /RE. The pod drives it high "
    "while it sends and low otherwise, so the transceiver listens whenever the pod is not "
    "sending and a fast device's reply is heard. Needs gateware v58. Omit for a plain UART."))]
UartDeActiveLow = Annotated[bool, Field(description="Drive DE low while sending (an active-low enable).")]


def _uart_de(pod: Any, method: str, de: Any, de_active_low: bool) -> Dict[str, Any]:
    """The RS-485 DE keyword arguments for an SDK UART call: none when unused."""
    if de is None:
        return {}
    if "de" not in inspect.signature(getattr(type(pod), method)).parameters:
        raise ValueError("UART de needs a newer embeddedci SDK; upgrade embeddedci")
    return {"de": de, "de_active_low": True} if de_active_low else {"de": de}


UartPort = Annotated[Optional[Literal["usb"]], Field(description=(
    "usb: the serial port of the board the pod hosts over USB (usb_host; a CDC ACM port, or the "
    "FTDI, CP210x or CH34x chip of a dev kit) instead of LA pins: the boot log over the cable the "
    "board was flashed through. No LA channel, wiring profile or I/O voltage is used; rx, tx, "
    "half_duplex, entry and de do not apply. Needs pod firmware 4.0.3+ and host mode on with the "
    "board mounted. Omit for the UART on LA pins."))]
UartDtr = Annotated[Optional[bool], Field(description=(
    "port usb only: the DTR line (default true). Many boards reset or enter their loader on DTR "
    "and RTS: set them to what the board expects."))]
UartRts = Annotated[Optional[bool], Field(description="port usb only: the RTS line (default true).")]


def _uart_usb(pod: Any, method: str, port: Optional[str], dtr: Optional[bool],
              rts: Optional[bool]) -> Dict[str, Any]:
    """The USB serial port keyword arguments for an SDK UART call: none when unused; refused when
    the SDK predates them."""
    if port is None and dtr is None and rts is None:
        return {}
    if "port" not in inspect.signature(getattr(type(pod), method)).parameters:
        raise ValueError("the USB serial port needs a newer embeddedci SDK; upgrade embeddedci")
    kw: Dict[str, Any] = {"port": port}
    if dtr is not None:
        kw["dtr"] = dtr
    if rts is not None:
        kw["rts"] = rts
    return kw


def _uart_line(pod: Any, method: str, half_duplex: bool, entry: Optional[UartEntryArg]) -> Dict[str, Any]:
    """The one-wire and boot-mode entry keyword arguments for an SDK UART call: none when unused,
    so an older SDK still works; refused when the SDK predates them."""
    kw: Dict[str, Any] = {}
    if half_duplex:
        kw["half_duplex"] = True
    if entry is not None:
        from embeddedci.benchpod import UartEntry
        kw["entry"] = UartEntry(reset_ms=entry.reset_ms, tx_low=entry.tx_low, hold_ms=entry.hold_ms,
                                idle_ms=entry.idle_ms, data=bytes.fromhex(entry.data_hex))
    if kw and "half_duplex" not in inspect.signature(getattr(type(pod), method)).parameters:
        raise ValueError("UART half_duplex and entry need a newer embeddedci SDK; upgrade embeddedci")
    return kw


@mcp.tool(annotations=_ann("Capture UART output"))
async def capture_uart(
    duration_s: Annotated[float, Field(gt=0, le=600, description="Capture window in seconds.")],
    rx: UartRx = None,
    tx: UartTx = None,
    baud: WiredBaud = None,
    until_regex: Regex = None,
    parity: UartParity = None,
    stop_bits: UartStopBits = None,
    half_duplex: UartHalfDuplex = False,
    entry: UartEntryOpt = None,
    de: UartDe = None,
    de_active_low: UartDeActiveLow = False,
    port: UartPort = None,
    dtr: UartDtr = None,
    rts: UartRts = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.UartCaptureResult:
    """Record the DUT's UART output for a fixed window (or until until_regex matches).

    The channels, baud, parity and stop bits come from the wiring profile when omitted (8N1 unless
    the profile says otherwise).
    half_duplex runs it on one wire, and entry runs a pod-timed boot-mode entry first (reset
    with TX low, release, first bytes).
    de: the pod switches an RS-485 transceiver's DE on that LA channel while it sends.
    port usb captures the hosted board's USB serial port instead (baud default 115200, dtr, rts).
    """
    pattern = _compile(until_regex)

    def op() -> m.UartCaptureResult:
        pod = SESSION.require()
        return _uart_result(pod.capture_uart(rx=rx, tx=tx, baud=baud, duration=duration_s, until=pattern,
                                             **_uart_frame(pod, "capture_uart", parity, stop_bits),
                                             **_uart_line(pod, "capture_uart", half_duplex, entry),
                                             **_uart_de(pod, "capture_uart", de, de_active_low),
                                             **_uart_usb(pod, "capture_uart", port, dtr, rts)))

    return await _call_reporting(ctx, "capturing UART", op)


@mcp.tool(annotations=_ann("Power-cycle and capture boot log", destructive=True))
async def power_cycle_and_capture(
    rx: UartRx = None,
    tx: UartTx = None,
    efuse: WiredEfuse = None,
    delay_s: Annotated[float, Field(ge=0, le=60, description="Seconds into the capture the power comes back.")] = 1.0,
    duration_s: Annotated[float, Field(gt=0, le=600, description="Capture window in seconds; must exceed delay_s.")] = 4.0,
    baud: WiredBaud = None,
    until_regex: Regex = None,
    off_settle_s: Annotated[float, Field(ge=0, le=10)] = 0.3,
    parity: UartParity = None,
    stop_bits: UartStopBits = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.UartCaptureResult:
    """Power the target off, then capture UART while it powers back on — the boot banner lands in the window.

    The channels, baud, frame (parity, stop bits) and power rail come from the wiring profile when
    omitted.
    """
    pattern = _compile(until_regex)

    def op() -> m.UartCaptureResult:
        pod = SESSION.require()
        return _uart_result(pod.power_cycle_and_capture(
            rx=rx, tx=tx, efuse=efuse, delay=delay_s, duration=duration_s, baud=baud, until=pattern,
            off_settle=off_settle_s, **_uart_frame(pod, "power_cycle_and_capture", parity, stop_bits)))

    return await _call_reporting(ctx, "power-cycling", op)


@mcp.tool(annotations=_ann("Open a UART session"))
async def uart_open(
    rx: UartRx = None,
    tx: UartTx = None,
    baud: WiredBaud = None,
    parity: UartParity = None,
    stop_bits: UartStopBits = None,
    half_duplex: UartHalfDuplex = False,
    entry: UartEntryOpt = None,
    de: UartDe = None,
    de_active_low: UartDeActiveLow = False,
    port: UartPort = None,
    dtr: UartDtr = None,
    rts: UartRts = None,
) -> m.UartSessionResult:
    """Start buffering the DUT's UART in the background (replacing any open session).

    Open it BEFORE an action whose output matters (power_on, reset_target), then uart_read; use
    uart_write to type into the DUT's console. The channels and baud come from the wiring profile
    when omitted; the result says which were used. So do parity and stop_bits (8N1 unless the profile
    says otherwise). The LA pins are 1.8/3.3 V only: a 5 V UART needs a level shifter and an RS-485 bus
    (Modbus RTU) an external RS-485 transceiver in between. A channel used as GPIO must be released
    first (gpio_release). half_duplex runs the session on one wire (rx; tx defaults to it), and
    entry runs a pod-timed boot-mode entry before it starts (reset with TX held low, release, the
    first bytes), for boot-ROM programming such as RL78 TOOL0. de puts an RS-485 transceiver's
    DE on an LA channel the pod switches itself while it sends.
    port usb opens the serial port of the board the pod hosts over USB instead of LA pins (usb_host
    first; baud default 115200, parity, stop_bits, dtr and rts apply; the result has no rx or tx).
    """
    def op() -> m.UartSessionResult:
        pod = SESSION.require()
        if port is not None:
            if rx is not None or tx is not None or half_duplex or entry is not None or de is not None:
                raise ValueError("port usb is the board's USB serial port: rx, tx, half_duplex, entry "
                                 "and de are for UARTs on LA pins")
            rate_usb = 115200 if baud is None else int(baud)
            kw = _uart_usb(pod, "open_uart", port, dtr, rts)
            SESSION.close_uart()
            SESSION.uart = pod.open_uart(baud=rate_usb, **_uart_frame(pod, "open_uart", parity, stop_bits), **kw)
            SESSION.uart_port = None
            return m.UartSessionResult(open=True, baud=rate_usb, parity=parity or "none",
                                       stop_bits=stop_bits or 1, port="usb",
                                       dtr=True if dtr is None else dtr, rts=True if rts is None else rts)
        if dtr is not None or rts is not None:
            raise ValueError("dtr and rts are the control lines of the USB serial port: pass port usb")
        rx_i = _channel(pod, rx, "uart_rx")
        tx_i = rx_i if half_duplex and tx is None else _channel(pod, tx, "uart_tx")
        rate = int(pod.wiring.uart_baud if baud is None else baud)
        frame = _uart_frame(pod, "open_uart", parity, stop_bits)
        line = _uart_line(pod, "open_uart", half_duplex, entry)
        line.update(_uart_de(pod, "open_uart", de, de_active_low))
        SESSION.close_uart()
        SESSION.uart = pod.open_uart(rx=rx_i, tx=tx_i, baud=rate, **frame, **line)
        SESSION.uart_port = (rx_i, tx_i, rate)
        used_parity = parity if parity is not None else getattr(pod.wiring, "uart_parity", None) or "none"
        used_stop = stop_bits if stop_bits is not None else getattr(pod.wiring, "uart_stop_bits", None) or 1
        return m.UartSessionResult(open=True, rx=rx_i, tx=tx_i, baud=rate,
                                   parity=used_parity, stop_bits=used_stop,
                                   half_duplex=half_duplex)

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
    timeout_s: Annotated[float, Field(ge=0, le=600, description=(
        "Seconds to wait for until_regex (or, without one, for any new output)."))] = 2.0,
) -> m.UartReadResult:
    """Return the UART output received since the previous uart_read, optionally waiting for a match."""
    pattern = _compile(until_regex)

    def op() -> m.UartReadResult:
        uart = SESSION.require_uart()
        matched: Optional[bool] = None
        if pattern is not None:
            through_match = uart.read_until(pattern, timeout=timeout_s)  # searches unread text
            matched = through_match is not None
            new = (through_match or "") + uart.read()
        else:
            new = uart.read(timeout=timeout_s)
        return m.UartReadResult(text=clip(new, MAX_TEXT), matched=matched, closed=uart.closed,
                                overflowed=uart.overflowed, truncated=len(new) > MAX_TEXT)

    return await _call(op, lock=False)


@mcp.tool(annotations=_ann("Close the UART session", idempotent=True))
async def uart_close() -> m.UartSessionResult:
    """Stop the background UART session (safe when none is open)."""
    await _call(SESSION.close_uart)
    return m.UartSessionResult(open=False)


# -- emulated sensors --------------------------------------------------------------------

#: The sensor library version the sensor tools use (None: BENCHPOD_SENSOR_LIBRARY, the CLI's
#: sensor_library setting, else the version the SDK pins). sensor_library(version=...) sets it.
_SENSOR_LIBRARY: Dict[str, Optional[str]] = {"version": None}

SensorDefinition = Annotated[Union[str, Dict[str, Any]], Field(description=(
    "A library sensor name ('bme280', see sensor_library), a .yaml/.json path on the machine "
    "running this server, the definition itself as YAML or JSON text, or a JSON object. Write "
    "your own from a datasheet in the format sensor_format describes."))]
SensorInstance = Annotated[str, Field(pattern=r"^[a-z0-9_]{1,16}$", description=(
    "Instance name, 1 to 16 lowercase letters, digits or _; sensor_start defaults it to the "
    "definition's name (pass one when that name is longer)."))]
SensorPins = Annotated[Optional[Dict[str, Union[int, str]]], Field(description=(
    "Role -> LA channel (1-14) or wiring name. I2C: sda, scl (default: the profile's i2c_sda/i2c_scl), "
    "optional drdy. SPI: sclk, mosi, miso, cs (default: the profile's spi_*; no data-ready pin). UART: "
    "tx (pod to DUT RX, required), optional rx. Analog: output 'dac' (default) or 'current'."))]
SensorValuesArg = Annotated[Dict[str, float], Field(description=(
    "Param key -> value, e.g. {\"temperature_c\": 31.5}; the keys, units and ranges are the "
    "definition's params (sensor_show, or the params sensor_start returns)."))]
LibraryVersion = Annotated[Optional[str], Field(description=(
    "Sensor library version: 'latest', a release such as '0.2.0', or 'default' (the version the "
    "SDK pins). Sets it for the other sensor tools too. Omit to keep the current one."))]


def _library(version: Optional[str] = None) -> Any:
    from embeddedci.benchpod.sensors import sensor_library

    if version is not None:
        _SENSOR_LIBRARY["version"] = version
    return sensor_library(_SENSOR_LIBRARY["version"])


def _emulation() -> Any:
    sensors = SESSION.require().sensors
    sensors.library_version = _SENSOR_LIBRARY["version"]
    return sensors


def _sensor_result(sensor: Any) -> m.SensorStarted:
    return m.SensorStarted(instance=sensor.instance, kind=sensor.kind, name=sensor.name,
                           params=sensor.params, info=sensor.info, started=sensor.started)


@mcp.tool(annotations=_ann("List library sensors", read_only=True, cloud=True))
async def sensor_library(version: LibraryVersion = None) -> m.SensorLibraryResult:
    """The sensor library: ready definitions the pod can emulate (name, kind, label) and the version in use.

    Sensor flow: sensor_library -> sensor_show(name) for its params -> set_la_voltage and, for I2C,
    set_pull(pull="up") on SDA/SCL -> sensor_start(definition=name, pins) -> sensor_set / sensor_fault /
    sensor_stimulus_load + sensor_stimulus_start -> sensor_status, sensor_regs -> sensor_stop.
    No library entry for the part? Write a definition from its datasheet (sensor_format) and pass
    the YAML to sensor_validate, then sensor_start. Needs no pod.
    """
    def op() -> m.SensorLibraryResult:
        lib = _library(version)
        return m.SensorLibraryResult(version=lib.version, source=lib.source, warning=lib.warning,
                                     entries=[e.to_dict() for e in lib.entries])

    return await _call(op, lock=False)


@mcp.tool(annotations=_ann("Show a library sensor", read_only=True, cloud=True))
async def sensor_show(name: Annotated[str, Field(description="Library entry name ('sht4x') or path.")],
                      version: LibraryVersion = None) -> m.SensorShowResult:
    """One library definition: its YAML (a starting point for your own part) and the params sensor_set takes. Needs no pod."""
    def op() -> m.SensorShowResult:
        from embeddedci.benchpod.sensors import load_definition

        lib = _library(version)
        entry = lib.get(name)
        doc = load_definition(entry.name, library=lib)
        return m.SensorShowResult(version=lib.version, path=entry.path, name=entry.name,
                                  kind=entry.kind, label=entry.label,
                                  params=list(doc.get("params") or []), yaml=lib.text(entry.name))

    return await _call(op, lock=False)


@mcp.tool(annotations=_ann("Sensor definition format", read_only=True, cloud=True))
async def sensor_format(version: LibraryVersion = None) -> m.TextResult:
    """The sensor definition and stimulus format (Markdown): read it before writing a definition from a datasheet. Needs no pod."""
    def op() -> m.TextResult:
        text = _library(version).format_doc()
        return m.TextResult(text=text or "this library version has no docs/format.md")

    return await _call(op, lock=False)


@mcp.tool(annotations=_ann("Validate a sensor definition", read_only=True))
async def sensor_validate(definition: SensorDefinition) -> m.SensorValidateResult:
    """Check a definition: its header here, then a dry-run compile on the pod (nothing is kept).

    Without a connected pod only the header (format, kind, name, params, size) is checked. A
    compile error names the place, e.g. "registers[4].s16be: unknown name 'ax' at 17": fix it
    and validate again.
    """
    def op() -> m.SensorValidateResult:
        from embeddedci.benchpod.sensors import load_definition

        doc = load_definition(definition, library=_library())
        if not SESSION.connected:
            return m.SensorValidateResult(ok=True, checked="header", name=doc["name"],
                                          kind=doc["kind"], report={})
        with SESSION.lock:
            report = _emulation().validate(doc)
        return m.SensorValidateResult(ok=True, checked="pod", name=doc["name"], kind=doc["kind"],
                                      report=report)

    return await _call(op, lock=False)


@mcp.tool(annotations=_ann("Start an emulated sensor"))
async def sensor_start(
    definition: SensorDefinition,
    instance: Optional[SensorInstance] = None,
    pins: SensorPins = None,
    address: Annotated[Optional[Union[int, str]], Field(description=(
        "I2C: the definition's address or one of its alt_addresses (0x77 or \"0x77\"); omit for its address."))] = None,
    baud: Annotated[Optional[int], Field(ge=300, le=4_000_000, description="UART: override the definition's baud rate.")] = None,
    values: Annotated[Optional[Dict[str, float]], Field(description="Initial param values; omitted ones start at their defaults.")] = None,
) -> m.SensorStarted:
    """Make the pod play a sensor for the DUT: upload the definition (library name or your own YAML/JSON) and start it on pins.

    Set the LA voltage first, and the pull-ups for I2C (set_pull(la=[sda, scl], pull="up"), LA1-LA6).
    Starting an instance name that runs replaces it. Up to 4 I2C sensors (several may share one
    bus at different addresses), 1 SPI, 1 UART and 2 analog sensors (one per output) run at once.
    I2C/SPI/UART sensors load the pod's emulation FPGA image if another runs (image_loaded in the
    result); it stays after sensor_stop, and control_loop / deep replays switch back by themselves
    once no I2C/SPI/UART sensor runs. Pod limits: UART 8N1 only, one FIFO sensor at a time,
    conversion/command times at most 65.535 ms (4 distinct per I2C sensor), no excitation: adc.
    The result lists the params sensor_set takes.
    """
    return await _call(lambda: _sensor_result(_emulation().start(
        definition, instance=instance, pins=pins, address=address, baud=baud, values=values)))


@mcp.tool(annotations=_ann("Set emulated sensor values", idempotent=True))
async def sensor_set(instance: SensorInstance, values: SensorValuesArg) -> m.DeviceReply:
    """Change what a running sensor reports; the DUT sees it on its next read or message. Out-of-range values are refused with the range."""
    return m.DeviceReply(reply=await _call(lambda: _emulation().set(instance, values)) or {})


@mcp.tool(annotations=_ann("Load a sensor stimulus"))
async def sensor_stimulus_load(stimulus: Annotated[Union[str, Dict[str, Any]], Field(description=(
        "A stimulus (kind: stimulus): YAML or JSON text, a JSON object, or a path on the machine running "
        "this server. Signals are params ('temperature_c', or 'env.temperature_c' with several sensors); "
        "segments hold, step, ramp, sine, saw, pulse, noise, data, repeat; optional faults at_s."))]) -> m.DeviceReply:
    """Upload values over time (and timed faults) for the running sensors; replaces the loaded stimulus. sensor_stimulus_start plays it."""
    return m.DeviceReply(reply=await _call(lambda: _emulation().load_stimulus(stimulus)) or {})


@mcp.tool(annotations=_ann("Play the sensor stimulus"))
async def sensor_stimulus_start(at_s: Annotated[Optional[float], Field(ge=0, description=(
        "Start this many seconds into the stimulus; omit for t = 0."))] = None) -> m.DeviceReply:
    """Play the loaded stimulus; it ends after its last segment (signals hold their last value) unless it has repeat: true."""
    return m.DeviceReply(reply=await _call(lambda: _emulation().start_stimulus(at_s)) or {})


@mcp.tool(annotations=_ann("Stop the sensor stimulus", idempotent=True))
async def sensor_stimulus_stop() -> m.DeviceReply:
    """Stop the stimulus; the values stay where it left them."""
    return m.DeviceReply(reply=await _call(lambda: _emulation().stop_stimulus()) or {})


@mcp.tool(annotations=_ann("Inject a sensor fault"))
async def sensor_fault(
    instance: SensorInstance,
    type: Annotated[Literal["stuck", "offset", "gain", "no_response", "corrupt", "bus_stuck",
                            "garbage", "drop", "baud_error", "open", "short"], Field(description=(
        "stuck (readings freeze), offset/gain (param + value), no_response (I2C NACK, SPI MISO "
        "released, UART silent), corrupt (register at XOR mask), bus_stuck (I2C SDA low, at most "
        "1 s), garbage/drop/baud_error (UART), open/short (analog)."))],
    duration_s: Annotated[Optional[float], Field(gt=0, description="Omit to last until sensor_fault_clear.")] = None,
    param: Annotated[Optional[str], Field(description="The param a stuck/offset/gain fault applies to.")] = None,
    value: Annotated[Optional[float], Field(description="offset/gain amount, drop probability, baud error percent, or a stuck/open/short value.")] = None,
    at: Annotated[Optional[int], Field(ge=0, le=0xFFFF, description="corrupt: the register.")] = None,
    mask: Annotated[Optional[int], Field(ge=0, le=255, description="corrupt: the XOR mask.")] = None,
    rate_hz: Annotated[Optional[float], Field(gt=0, description="garbage: bytes per second (default 100).")] = None,
) -> m.DeviceReply:
    """Make a running sensor misbehave now, to test the DUT's error handling."""
    fields = {"param": param, "value": value, "at": at, "mask": mask, "rate_hz": rate_hz}
    return m.DeviceReply(reply=await _call(lambda: _emulation().fault(
        instance, type, duration_s=duration_s, **fields)) or {})


@mcp.tool(annotations=_ann("Clear sensor faults", idempotent=True))
async def sensor_fault_clear(instance: SensorInstance) -> m.DeviceReply:
    """End a sensor's faults (injected with sensor_fault or by the stimulus)."""
    return m.DeviceReply(reply=await _call(lambda: _emulation().clear_fault(instance)) or {})


@mcp.tool(annotations=_ann("Stop emulated sensors", idempotent=True))
async def sensor_stop(instance: Annotated[Optional[SensorInstance], Field(description=(
        "The instance to stop; omit to stop every sensor."))] = None) -> m.SensorStopResult:
    """Stop a sensor (or all): the pod releases its pins and forgets the definition. The emulation FPGA image stays loaded."""
    names = await _call(lambda: _emulation().stop(instance))
    return m.SensorStopResult(instances=list(names or []))


@mcp.tool(annotations=_ann("Emulated sensor status", read_only=True))
async def sensor_status() -> m.DeviceReply:
    """Running sensors (slot, pins, values, faults with left_s, bus counters: writes and last_write say the DUT talked to it; transfers is always null), the stimulus and the FPGA image."""
    return m.DeviceReply(reply=await _call(lambda: _emulation().status()) or {})


@mcp.tool(annotations=_ann("Emulated sensor registers", read_only=True))
async def sensor_regs(instance: SensorInstance,
                      start: Annotated[int, Field(ge=0, le=255, description=(
                          "Offset in the 256-byte register image (i2c.page is its window)."))] = 0,
                      length: Annotated[Optional[int], Field(ge=1, le=256, description=(
                          "Bytes to read; omit for the rest of the image."))] = None) -> m.I2cRegsResult:
    """The bytes an I2C/SPI sensor answers now (the DUT's own writes included)."""
    data = await _call(lambda: _emulation().regs(instance, start=start, length=length))
    return m.I2cRegsResult(start=start, bytes=list(data))


@mcp.tool(annotations=_ann("Capture a sensor's I2C bus"))
async def sensor_capture(
    instance: SensorInstance,
    samples: Annotated[int, Field(ge=1, le=4096, description=(
        "Packed bytes to capture (4 samples each)."))] = 4096,
    address: Annotated[Optional[int], Field(description="Also report whether the DUT addressed this device.")] = None,
    register: Annotated[Optional[int], Field(description="With address: the bytes the DUT read from this register.")] = None,
) -> m.I2cCaptureResult:
    """Wait for the DUT's next transaction on an I2C sensor's bus (up to 2 s), capture it from its START and decode it into a transaction trace (S/Sr/P, addr, data, ACK). A quiet bus is a trigger timeout error. Firmware without the sensor_la_trigger capability captures at once instead. I2C only: capture an SPI or UART sensor's pins with capture_la."""
    def op() -> m.I2cCaptureResult:
        txns = _emulation().i2c_transactions(instance, samples)
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


# -- pull resistors ---------------------------------------------------------------------

def _pull(state: Any) -> m.PullChannel:
    # embeddedci before 3.0.3 has no state / supported on PullState
    direction = state.direction
    supported = getattr(state, "supported", None)
    if supported is None:
        supported = (direction,) if direction in ("up", "down") else ()
    return m.PullChannel(la=state.la, state=getattr(state, "state", direction if state.enabled else "off"),
                         supported=list(supported), enabled=state.enabled, direction=direction,
                         ohms=state.ohms, available=state.available,
                         strength=getattr(state, "strength", None),
                         held=getattr(state, "held", None))


@mcp.tool(annotations=_ann("Set pull resistors", idempotent=True))
async def set_pull(
    la: Annotated[List[PullPin], Field(min_length=1, max_length=14, description="LA channels (1-14).")],
    pull: Annotated[Literal["up", "down", "off"], Field(description=(
        "The direction: up, down or off. The pod picks the resistor from the pin's function (ohms and "
        "strength in the result). A channel pulls only the ways its resistor allows (pull_status: "
        "supported): on the digital board v4 every channel pulls up or down; on older boards LA1-LA6 "
        "pull up, LA7/LA8 down, LA9-LA14 have none, and the pod refuses the rest (BP0024, BP0023)."))],
) -> m.PullStatusResult:
    """Pull LA channels up or down, or switch their pull resistor off. For an I2C bus pull SDA and
    SCL up. The pull-ups pull to the LA voltage (1.8 V or 3.3 V)."""
    def op() -> m.PullStatusResult:
        pod = SESSION.require()
        return m.PullStatusResult(channels=[_pull(pod.set_pull(ch, pull)) for ch in la])

    return await _call(op)


@mcp.tool(annotations=_ann("USB network setting", destructive=True))
async def usb_network(
    enabled: Annotated[Optional[Literal[False]], Field(description=(
        "Omit to read the setting. false turns the pod's USB network adapter off (only over the "
        "pod's USB network link: connection 'usb'); the pod then re-enumerates as its USB console "
        "only and this connection ends. Turning it back on needs the USB console: the user runs "
        "`benchpod usb-network on`."))] = None,
) -> m.UsbNetworkResult:
    """Read the pod's USB network setting, or turn the adapter off. Only the device changes it:
    Ethernet, Wi-Fi and the cloud may only read it."""
    def op() -> m.UsbNetworkResult:
        pod = SESSION.require()
        on = pod.usb_network(None if enabled is None else False)
        note = ""
        if enabled is False:
            note = ("The pod re-enumerates as its USB console only; this connection ends. Ask the user to run "
                    "`benchpod usb-network on` over the USB console to turn it back on.")
        return m.UsbNetworkResult(enabled=on, note=note)

    return await _call(op)


def _usb_device(d: Any) -> m.UsbDeviceInfo:
    cfg = d.config
    return m.UsbDeviceInfo(
        vid=d.vid, pid=d.pid, vid_pid=d.vid_pid, speed=d.speed, manufacturer=d.manufacturer,
        product=d.product, serial=d.serial, max_power_ma=cfg.max_power_ma if cfg else None,
        interfaces=[m.UsbInterfaceInfo(number=i.number, alt=i.alt, interface_class=i.interface_class,
                                       class_name=i.class_name, subclass=i.subclass,
                                       protocol=i.protocol, endpoints=i.endpoints)
                    for i in d.interfaces])


def _usb_host_result(st: Any, note: str = "") -> m.UsbHostResult:
    return m.UsbHostResult(
        mode=st.mode, port=st.port, hosting=st.hosting, attached=st.attached,
        enumeration=st.enumeration, connected=st.connected, line_speed=st.line_speed,
        enumerations=st.enumerations, remaining_s=st.remaining_s,
        device=_usb_device(st.device) if st.device is not None else None, note=note)


#: The SDK's USB flash error (an SDK before 4.0.3 has none: nothing raises it then).
_UsbFlashError: Any = getattr(_errors, "UsbFlashError", None) or type("_NoUsbFlashError", (Exception,), {})


def _need_usb_sdk(pod: Any) -> None:
    if not hasattr(type(pod), "usb_flash"):
        raise ValueError("USB host mode and flashing need a newer embeddedci SDK; upgrade embeddedci")


@mcp.tool(annotations=_ann("USB host mode", destructive=True))
async def usb_host(
    enabled: Annotated[Optional[bool], Field(description=(
        "Omit to read the state. true starts host mode (or renews its timeout), false stops it."))] = None,
    port: Annotated[Optional[Literal["usb_a", "usb_c"]], Field(description=(
        "usb_a: the USB-A host port (digital v4 and later). usb_c: the pod's own USB-C connector "
        "through a breakout, on any pod. Omit to let the server pick from the board revision."))] = None,
    timeout_s: Annotated[int, Field(ge=1, le=3600, description=(
        "Seconds until the pod goes back to being a USB device by itself."))] = 60,
    wait_s: Annotated[float, Field(ge=0, le=60, description=(
        "With enabled true, or with it omitted while hosting: wait up to this long for a board to "
        "mount. 0 does not wait."))] = 0.0,
    vid: Annotated[Optional[str], Field(description='Wait for this vendor ID only (hex, "16c0").')] = None,
    pid: Annotated[Optional[str], Field(description='Wait for this product ID only (hex, "0478").')] = None,
) -> m.UsbHostResult:
    """Make the pod a USB host for the board under test, or read what is on its host port.

    Needs pod firmware 4.0.3 or later and a LAN or cloud connection: while the pod hosts it is off
    the computer's USB (no console, no USB network), and over connection 'usb' it is refused
    (BP0076). The report names the device that enumerated (IDs, strings, interfaces). A board
    that draws power but shows connected=false has no D+/D- link to the pod; line_speed 'low' from
    a full-speed board means D+ and D- are swapped. usb_flash starts host mode itself when needed.
    Host mode ends by its timeout_s, or with enabled=false."""
    def op() -> m.UsbHostResult:
        pod = SESSION.require()
        _need_usb_sdk(pod)
        note = ""
        if enabled is True:
            st = pod.usb_host(True, port=port, timeout_s=timeout_s)
        elif enabled is False:
            st = pod.usb_host(False)
        else:
            st = pod.usb_host_status()
        if wait_s > 0 and st.hosting:
            try:
                pod.usb_wait_device(vid=vid, pid=pid, timeout=wait_s)
            except TimeoutError as exc:
                note = str(exc)
            st = pod.usb_host_status()
        return _usb_host_result(st, note)

    return await _call(op)


@mcp.tool(annotations=_ann("Flash over USB", destructive=True))
async def usb_flash(
    file: Annotated[str, Field(description=(
        "Firmware path on the machine running this server: .uf2, .bin, .hex or .dfu."))],
    address: Annotated[Optional[str], Field(description=(
        'Start address for a .bin ("0x08000000" for an STM32, "0x2000" for a SAMD sketch, "0x10000" '
        "for an ESP32 app). Omit: from a .hex or .dfu file, else the loader's usual one."))] = None,
    name: Annotated[Optional[str], Field(description=(
        "For a board that shows a drive: the file name on it (8.3). Omit: the file's name."))] = None,
    leave: Annotated[bool, Field(description=(
        "false keeps a DFU, Teensy, AVR, SAMD or ESP board in its bootloader at the end."))] = True,
    mode: Annotated[Optional[Literal["uf2", "file", "dfu", "teensy", "avr", "samd", "esp", "picoboot"]], Field(description=(
        "Override the loader. Omit: the server picks it from the file and the board (see reason in "
        "the result)."))] = None,
    port: Annotated[Optional[Literal["usb_a", "usb_c"]], Field(description=(
        "Host port when host mode is off and has to be started; omit for the board's own."))] = None,
    timeout_s: Annotated[Optional[float], Field(gt=0, le=3600, description=(
        "Abort the flash after this long. Omit: a minute plus the file size at 2.5 KB/s."))] = None,
    verify: Annotated[bool, Field(description=(
        "Read the drive back after a drive flash: the loader's FAIL.TXT is reported as the failure "
        "(a DAPLink board says why it refused the file), and a UF2 file is compared with the "
        "loader's CURRENT.UF2 while the board stays in it. The result's note says what was found. "
        "A Pico flashed through picoboot always reads every page back."))] = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.UsbFlashToolResult:
    """Flash the board on the pod's USB host port, the way a computer would. Needs pod firmware
    4.0.3 or later and a LAN or cloud connection. There is nothing to choose: the loader comes
    from the file and the board that mounted (a UF2 or drag-and-drop drive, DFU or STM32 ROM
    loader, Teensy, Arduino AVR or SAMD, ESP32, a Pico in BOOTSEL: a .uf2 goes to its drive, a
    .bin or .hex through PICOBOOT with every page read back), and `reason` says why. .hex and .dfu
    files are converted to a binary with their address. Starts host mode when it is off; the board
    must be in its bootloader where it needs a button (Pico BOOTSEL, STM32 BOOT0, Teensy). A drive
    flash sets the file's date from this computer's clock. A failed flash is
    a normal result (ok=false): read result, phase and note. While it runs the pod refuses
    captures, DAC playback and uploads."""
    def op() -> m.UsbFlashToolResult:
        pod = SESSION.require()
        _need_usb_sdk(pod)
        try:
            extra: Dict[str, Any] = {}
            if verify:
                if "verify" not in inspect.signature(type(pod).usb_flash).parameters:
                    raise ValueError("verify needs a newer embeddedci SDK; upgrade embeddedci")
                extra["verify"] = True
            r = pod.usb_flash(file, address=address, name=name, leave=leave, mode=mode, port=port,
                              timeout=timeout_s, **extra)
        except _UsbFlashError as exc:
            st = exc.state
            return m.UsbFlashToolResult(
                ok=False, mode=exc.mode, result=exc.result, phase=exc.phase, note=str(exc),
                length=st.length if st else 0, protocol=st.protocol if st else "")
        return m.UsbFlashToolResult(
            ok=True, mode=r.mode, reason=r.reason, length=r.length, address=r.address, name=r.name,
            protocol=r.protocol, ms=r.ms, result="ok", note=getattr(r, "note", ""),
            device=_usb_device(r.device) if r.device is not None else None)

    return await _call_reporting(ctx, "flashing over USB", op)


@mcp.tool(annotations=_ann("Check the board's USB firmware", destructive=True))
async def usb_check(
    test: Annotated[Literal["enumerate", "power", "suspend", "reset", "replug"], Field(description=(
        "enumerate: power-cycles the board through the internal 5 V (efuse 1) rail and times power-on to pull-up, descriptor "
        "and mounted. power: the board's current (internal 5 V, efuse 1) against the configuration's bMaxPower. suspend: the "
        "suspended current against the 2.5 mA a bus-powered device may draw, resume, and the board "
        "still answers. reset: port resets and the time to enumerate again. replug: VBUS dropouts "
        "of off_ms: does the board come back."))],
    count: Annotated[Optional[int], Field(ge=1, le=200, description=(
        "enumerate (default 10) and reset (default 20): cycles. replug (default 10, up to 100): "
        "cycles per dropout length."))] = None,
    off_ms: Annotated[Union[int, List[int], None], Field(description=(
        "enumerate: how long the rail stays off, 50 to 10000 (default 500). replug: a dropout length "
        "or a list of up to 6, 1 to 5000 ms (default [5, 20, 100, 500])."))] = None,
    timeout_ms: Annotated[Optional[int], Field(ge=500, le=30000, description=(
        "enumerate, reset, replug: how long to wait for the board after each cycle (default 5000)."))] = None,
    duration_ms: Annotated[Optional[int], Field(ge=200, le=60000, description=(
        "power (default 3000) and suspend (default 1000): the measuring time."))] = None,
    settle_ms: Annotated[Optional[int], Field(ge=20, le=5000, description=(
        "suspend: the wait after the bus is suspended before measuring (default 200)."))] = None,
    port: Annotated[Optional[Literal["usb_a", "usb_c"]], Field(description=(
        "Host port when host mode is off and has to be started; omit for the board's own."))] = None,
    timeout_s: Annotated[Optional[float], Field(gt=0, le=3600, description=(
        "Abort the check after this long. Omit: its worst case plus a margin."))] = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.UsbCheckToolResult:
    """Check the USB firmware of the board on the pod's host port, with numbers a computer cannot
    get. Needs pod firmware 4.0.3 or later, a LAN or cloud connection, and the board's USB power
    (VBUS) wired to the internal 5 V (efuse 1) target power and switched on (power_on; every test
    but reset). Starts host mode when it is off. The check runs in the background of the
    pod and this call returns when it ends: passed is the verdict, result the numbers. A board that
    fails is a normal result (passed false). While it runs, usb_flash and the USB serial port are
    refused. A refusal says why (host mode off, no board, the rail off or not powering the board)."""
    def op() -> m.UsbCheckToolResult:
        pod = SESSION.require()
        if not hasattr(type(pod), "usb_check"):
            raise ValueError("USB checks need a newer embeddedci SDK; upgrade embeddedci")
        st = pod.usb_check(test, count=count, off_ms=off_ms, timeout_ms=timeout_ms,
                           duration_ms=duration_ms, settle_ms=settle_ms, port=port, timeout=timeout_s)
        return m.UsbCheckToolResult(
            test=st.test or test, ran=st.error == "", passed=st.passed, error=st.error, phase=st.phase,
            done=st.done, total=st.total, ms=st.ms, result=st.result)

    return await _call_reporting(ctx, "checking the board's USB firmware", op)


@mcp.tool(annotations=_ann("USB HID interfaces and input reports"))
async def usb_hid(
    action: Annotated[Literal["list", "start", "stop", "read"], Field(description=(
        "list: the HID interfaces of the board (the first two) with their report descriptors. "
        "start: begin catching input reports (keyboard keys, mouse, gamepad, custom HID) into a "
        "ring of 64 on the pod. read: take reports from the ring. stop: end the capture and free "
        "the ring."))] = "list",
    count: Annotated[int, Field(ge=1, le=32, description="read: at most this many reports.")] = 1,
    timeout_s: Annotated[float, Field(gt=0, le=10, description=(
        "read: when the ring holds no report, wait up to this long for one. The result holds what "
        "is there, possibly none."))] = 2.0,
    instance: Annotated[Optional[Literal[0, 1]], Field(description=(
        "read: only the reports of this HID interface (the others stay in the ring)."))] = None,
) -> m.UsbHidResult:
    """Read the HID interfaces of the board on the pod's USB host port and its input reports (a
    keyboard's keys, a mouse, a gamepad). Needs pod firmware 4.0.3 or later and usb_host on with
    the board mounted. Typical: usb_hid(list), usb_hid(start), press a key or drive the device,
    usb_hid(read, count, timeout_s), usb_hid(stop). A read without a start starts the capture and
    leaves it running. The capture ends by itself when the board detaches or host mode ends
    (capturing false in a read). While it runs, usb_flash and usb_check are refused. Reports are
    hex: with several report IDs the first byte is the ID."""
    def op() -> m.UsbHidResult:
        pod = SESSION.require()
        if not hasattr(type(pod), "usb_hid_read"):
            raise ValueError("USB HID needs a newer embeddedci SDK; upgrade embeddedci")
        if action == "list":
            st = pod.usb_hid_interfaces()
            return m.UsbHidResult(action=action, attached=st.attached, interfaces=[
                m.UsbHidInterfaceInfo(instance=i.instance, interface=i.interface, protocol=i.protocol,
                                      descriptor_len=i.descriptor_len, descriptor_hex=i.descriptor.hex(),
                                      truncated=i.truncated) for i in st.interfaces])
        if action in ("start", "stop"):
            cap = pod.usb_hid_capture(action == "start")
            return m.UsbHidResult(action=action, capturing=cap.capturing, buffered=cap.buffered,
                                  dropped=cap.dropped)
        got = pod.usb_hid_read(count=count, timeout=timeout_s, instance=instance)
        return m.UsbHidResult(action=action, capturing=got.capturing, dropped=got.dropped, reports=[
            m.UsbHidReportInfo(ms=r.ms, instance=r.instance, data_hex=r.data.hex()) for r in got.reports])

    return await _call(op)


@mcp.tool(annotations=_ann("Console log level", idempotent=True))
async def log_level(
    level: Annotated[Optional[Literal["error", "warn", "info", "debug"]], Field(description=(
        "Omit to read the level. Set one to change it; the pod stores it across reboots and "
        "firmware updates."))] = None,
) -> m.LogLevelResult:
    """Read or set how much the pod prints on its USB console (firmware newer than 3.10.12). info
    is the default; debug also prints every command trace and the retry detail of the cloud,
    Wi-Fi and DHCP loops on the pod's console; warn and error print less. It changes only the
    console, never a tool's result."""
    def op() -> m.LogLevelResult:
        pod = SESSION.require()
        return m.LogLevelResult(level=pod.log_level() if level is None else pod.set_log_level(level))

    return await _call(op)


@mcp.tool(annotations=_ann("LAN TLS", destructive=True))
async def lan_tls(
    https_only: Annotated[Optional[bool], Field(description=(
        "Omit to read. True: the pod refuses plaintext port 8080 on Ethernet and Wi-Fi, so only TLS "
        "(8443) crosses the LAN; its USB link keeps 8080. False turns it back off, which the pod "
        "only takes over TLS, USB or the cloud."))] = None,
    regenerate: Annotated[bool, Field(description=(
        "True: the pod makes a new key and self-signed certificate. Clients that pinned the old one "
        "must trust the new one (`benchpod lan-tls trust`); this server's pin follows."))] = False,
) -> m.LanTlsResult:
    """The pod's LAN API over TLS: port 8443 (this server's default for a LAN address), HTTPS-only, and the certificate
    clients pin (sha256). Connections to a network address use TLS by default when the pod has it.
    Installing your own certificate: `benchpod lan-tls set-cert` or the SDK's set_lan_tls_cert."""
    def op() -> m.LanTlsResult:
        pod = SESSION.require()
        if regenerate:
            info = pod.regenerate_lan_tls()
        elif https_only is not None:
            info = pod.set_lan_tls_https_only(https_only)
        else:
            info = pod.lan_tls()
        return m.LanTlsResult(port=info.port, https_only=info.https_only, sessions=info.sessions,
                              origin=info.origin, key=info.key, subject=info.subject, san=list(info.san),
                              not_after=info.not_after, sha256=info.sha256, error=info.error,
                              connected_over_tls=bool(getattr(pod.transport, "last_tls", False)))

    return await _call(op)


@mcp.tool(annotations=_ann("Pull resistor status", read_only=True))
async def pull_status() -> m.PullStatusResult:
    """Every LA channel's pull resistor: state (up, down or off), the directions it supports
    (empty without a resistor) and its value."""
    def op() -> m.PullStatusResult:
        pod = SESSION.require()
        return m.PullStatusResult(channels=[_pull(pod.pull_state(la)) for la in range(1, 15)])

    return await _call(op)


# -- LA pin ownership + GPIO ---------------------------------------------------------------

GpioChannels = Annotated[List[LaRef], Field(min_length=1, max_length=256, description=(
    "LA channels as numbers 1-14 or wiring-profile names, and expander pins 'EXT1'..'EXTn' (or "
    "their names) once an expander is attached (expansion)."))]
OptionalGpioChannels = Annotated[Optional[List[LaRef]], Field(max_length=256, description=(
    "LA channels as numbers 1-14 or wiring-profile names, and expander pins 'EXT1'..'EXTn' (or "
    "their names) once an expander is attached (expansion)."))]


@mcp.tool(annotations=_ann("LA pin functions", read_only=True))
async def la_pins() -> m.LaPinsResult:
    """What owns each of the 14 LA channels — none (free), gpio, uart_rx/uart_tx, swd_clk/swd_dio,
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


@mcp.tool(annotations=_ann("Set pin directions", idempotent=True))
async def la_dir(
    la: Annotated[Optional[Union[List[LaRef], Literal["all"]]], Field(description=(
        "Channels or wiring names, or 'all'. Omit with dir to list every pin."))] = None,
    dir: Annotated[Optional[Literal["dut_input", "dut_output", "bidir", "unknown"]], Field(description=(
        "Which way the DUT signal goes. On dut_output the pod refuses push-pull drivers."))] = None,
    from_wiring: Annotated[bool, Field(description=(
        "Send the wiring profile's directions again (each role's and signal's default, or the "
        "profile's dut_dirs)."))] = False,
) -> m.GpioPinsResult:
    """Tell the pod which way each pin's DUT signal goes, so it never drives against a DUT output.

    connect and set_wiring already send the wiring profile's directions (firmware 3.10.4+);
    use this for a pin the profile doesn't name. The pod forgets them on a reboot.
    """
    def op() -> m.GpioPinsResult:
        pod = SESSION.require()
        if from_wiring:
            pins = pod.apply_pin_directions()
        else:
            target = la if la == "all" or la is None else [_la(pod, ch) for ch in la]
            pins = pod.la_dir(target, dir)
        return m.GpioPinsResult(pins=[pin_state(p) for p in pins])

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
    else can use it: release it before uart_open, flash or sensor_start on that channel.
    A channel already owned by another function is refused with a PinConflictError naming the
    owner and the tool that frees it, and an engaged bias resistor that would fight the mode with a PullConflictError.
    """
    def op() -> m.GpioPinsResult:
        pod = SESSION.require()
        las = [_gpio_ref(pod, ch) for ch in la]
        # configure_gpio sends the LA channels in one command, which the pod validates as a unit —
        # so a conflict on any channel leaves every channel as it was. EXT pins follow.
        return m.GpioPinsResult(pins=[_any_pin_state(p)
                                      for p in pod.configure_gpio(las, mode, level=level)])

    return await _call(op)


@mcp.tool(annotations=_ann("Drive GPIO channels", destructive=True, idempotent=True))
async def gpio_write(
    la: GpioChannels,
    level: Annotated[Union[Literal[0, 1], List[Literal[0, 1]]], Field(description=(
        "0 = low; 1 = high (open_drain: released). One level for every channel, or a list with "
        "one level per channel."))],
) -> m.GpioWriteResult:
    """Set the level of GPIO output / open-drain channels (LA channels in one pod command per
    level; EXT pins in one bus transaction per expander chip).

    The channels must already be in an output or open_drain mode (gpio_mode).
    """
    def op() -> m.GpioWriteResult:
        pod = SESSION.require()
        las = [_gpio_ref(pod, ch) for ch in la]
        pod.set_gpio(las, level)
        return m.GpioWriteResult(la=las, level=level)

    return await _call(op)


@mcp.tool(annotations=_ann("Read pin levels", read_only=True))
async def gpio_read(la: OptionalGpioChannels = None) -> m.GpioReadResult:
    """The live level (0/1) of LA channels and expander pins — omit `la` for all 14 LA channels
    and every EXT pin.

    Works whatever owns a channel; a GPIO input is the usual way to watch a DUT output.
    """
    def op() -> m.GpioReadResult:
        pod = SESSION.require()
        if la is None:
            out = [m.PinLevel(la=ch, level=v) for ch, v in sorted(pod.pin_levels().items())]
            if pod._expansion is not None:
                out += [m.PinLevel(la=p.pin, level=p.live) for p in pod.expansion().pins
                        if p.live is not None]
            return m.GpioReadResult(levels=out)
        refs = [_gpio_ref(pod, ch) for ch in la]
        levels = pod.pin_levels() if any(isinstance(r, int) for r in refs) else {}
        return m.GpioReadResult(levels=[
            m.PinLevel(la=r, level=levels[r] if isinstance(r, int) else pod.read_gpio(r))
            for r in refs])

    return await _call(op)


@mcp.tool(annotations=_ann("Wait for a pin level", read_only=True))
async def gpio_wait(
    la: LaRef,
    level: GpioLevel = 1,
    timeout_s: Annotated[float, Field(ge=0, le=600, description="Seconds to wait.")] = 5.0,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.GpioWaitResult:
    """Wait until an LA channel or EXT pin reads `level` (reached=false when timeout_s passes
    first).

    Polled from this host, so it resolves to a few milliseconds plus the round trip — for precise
    timing use a triggered capture (capture_la trigger_la) and la_timing instead.
    """
    def op() -> m.GpioWaitResult:
        pod = SESSION.require()
        ch = _gpio_ref(pod, la)
        start = time.monotonic()
        reached = pod.wait_for_level(ch, level, timeout=timeout_s)
        return m.GpioWaitResult(la=ch, level=level, reached=reached,
                                waited=round(time.monotonic() - start, 4))

    return await _call_reporting(ctx, "waiting for the pin", op)


@mcp.tool(annotations=_ann("Pulse a GPIO channel", destructive=True))
async def gpio_pulse(
    la: LaRef,
    width_s: Annotated[float, Field(ge=4e-6, le=0.065535,
                                  description="Seconds the pulse is high (and low between pulses), "
                                              "4 us to 65.535 ms.")],
    count: Annotated[int, Field(ge=1, le=65535, description="Number of pulses (at most 65535).")] = 1,
) -> m.GpioPulseResult:
    """Emit FPGA-timed pulses on an LA channel — a trigger for the DUT, or a step/dir motor train.

    The channel must be free or a GPIO output at level 0 (it returns to its GPIO level afterwards).
    The FPGA runs the train by itself, so this returns as soon as it starts.
    """
    def op() -> m.GpioPulseResult:
        pod = SESSION.require()
        ch = _gpio_ref(pod, la)  # an EXT pin: la_step refuses it, saying why
        pod.la_step(ch, steps=count, delay=width_s)
        return m.GpioPulseResult(la=ch, count=count, width=width_s)

    return await _call(op)


@mcp.tool(annotations=_ann("Release GPIO channels", idempotent=True))
async def gpio_release(la: OptionalGpioChannels = None) -> m.GpioReleaseResult:
    """Stop using channels as GPIO — they go back to high-Z, watched by captures (EXT pins: input).

    Omit `la` to release every GPIO channel and EXT pin (channels owned by other functions are left
    alone).
    Do this before starting a UART session, flashing or emulating a sensor on those channels.
    """
    def op() -> m.GpioReleaseResult:
        pod = SESSION.require()
        las = [] if la is None else [_gpio_ref(pod, ch) for ch in la]
        pod.release_gpio(*las)
        return m.GpioReleaseResult(released=las)

    return await _call(op)


# -- GPIO expanders ----------------------------------------------------------------------

_CHIP_NAMES = ", ".join(c.name for c in EXPANSION_CHIPS)


def _expansion_result(st: Any) -> m.ExpansionResult:
    if not st.attached:
        return m.ExpansionResult(attached=False, chips=[c.name for c in EXPANSION_CHIPS])
    return m.ExpansionResult(attached=True, chip=st.chip, bus=st.bus, la=dict(st.la),
                             addrs=list(st.addrs), count=st.count, hz=st.hz, pullups=st.pullups,
                             pins=[ext_pin_state(p) for p in st.pins], notes=list(st.notes) or None)


@mcp.tool(annotations=_ann("GPIO expander board", destructive=True, idempotent=True))
async def expansion(
    chip: Annotated[Optional[str], Field(description=(
        f"The expander chip to attach: {_CHIP_NAMES}. Omit for the status (what is attached, its "
        "pins' modes and live levels)."))] = None,
    detach: Annotated[bool, Field(description=(
        "Detach the expander: every EXT pin back to input, its bus released."))] = False,
    addr: Annotated[Optional[Union[int, List[int]]], Field(description=(
        "I2C chips: the 7-bit address (default the chip's first, 0x20 = 32), or a list for several "
        "chips on one bus (EXT pins in that order). mcp23s17: hardware address 0-7."))] = None,
    sda: Annotated[Optional[LaRef], Field(description="I2C SDA; omit for the wiring's i2c_sda.")] = None,
    scl: Annotated[Optional[LaRef], Field(description="I2C SCL; omit for the wiring's i2c_scl.")] = None,
    spi: Annotated[Optional[List[LaRef]], Field(min_length=4, max_length=4, description=(
        "SPI chips: [sck, mosi, miso, cs]; omit for the wiring's spi_* roles. All four are needed: "
        "a 74hc595's MISO and a 74hc165's MOSI are free pins."))] = None,
    load: Annotated[Optional[LaRef], Field(description=(
        "74hc165 only: the LA channel on SH/LD, pulsed low before each read."))] = None,
    count: Annotated[Optional[int], Field(ge=1, le=32, description=(
        "74hc595 / 74hc165: chips in the chain (8 pins each)."))] = None,
    hz: Annotated[Optional[int], Field(ge=1, le=6_000_000, description=(
        "Bus clock; default 400 kHz for I2C (100 kHz pcf8574), 1 MHz for SPI."))] = None,
    pullups: Annotated[Optional[Literal["pod", "external"]], Field(description=(
        "I2C pull-ups: pod (LA1-LA6 only, pull to the LA voltage, switched on here) or external (the "
        "breakout's). Default pod on LA1-LA6 when the pod has pull-ups at this voltage (1.8 V: "
        "firmware newer than 3.10.12), else external."))] = None,
    names: Annotated[Optional[Dict[str, str]], Field(description=(
        "Names for the pins, {'EXT1': 'LED1', ...}; usable wherever EXTn is."))] = None,
) -> m.ExpansionResult:
    """Attach an I2C/SPI GPIO expander board, report it, or detach it.

    Attaching checks the LA voltage, probes the chip, puts every pin in input and names the pins
    EXT1..EXTn. Then gpio_mode, gpio_write, gpio_read, gpio_wait and gpio_release take 'EXTn' (or
    the names) next to LA channels; a write to several EXT pins is one bus transaction per chip.
    EXT pins are not FPGA-timed: gpio_pulse, la_step, capture triggers and la_dir refuse them.
    The expander's bus closes on its own before flash, i2c_open and SPI tools; i2c_open on the same
    two pins shares the bus with it.

    Gateware v65+: the emulation image (loaded by sensor_start) has no I2C or SPI master (the expander runs on it). With no I2C/SPI/UART sensor running the pod switches to the loop image by itself (~3 s); with sensors running this refuses (BP0059): sensor_stop first, or do it before sensor_start.
    """
    def op() -> m.ExpansionResult:
        pod = SESSION.require()
        if detach:
            if chip is not None:
                raise ValueError("give chip or detach=true, not both")
            return _expansion_result(pod.expansion(None))
        if chip is None:
            return _expansion_result(pod.expansion(
                addr=addr, sda=sda, scl=scl, spi=spi, load=load, count=count, hz=hz,
                pullups=pullups, names=names))
        return _expansion_result(pod.expansion(chip, addr=addr, sda=sda, scl=scl, spi=spi,
                                               load=load, count=count, hz=hz, pullups=pullups,
                                               names=names))

    return await _call(op)


# -- analog ------------------------------------------------------------------------------

AdcRangeArg = Annotated[Optional[AdcRange], Field(description=(
    "ADC input range: normal (±32.8 V, ~1 mV/count) or low (±5.46 V, ~167 µV/count, 1.82 MΩ input: 1x leads only; "
    "analog module v3r4+). Omit to keep the range the pod is in."))]


@mcp.tool(annotations=_ann("Apply an analog path", idempotent=True))
async def analog_path(path: AnalogPath, range: AdcRangeArg = None) -> m.AnalogPathResult:
    """Set every analog mux and relay for a named path in one step.

    dac_3v3/dac_5v/dac_12v route the DAC to an output; adc_ext connects the ADC to the front SMA;
    cal1/cal2 loop the 5 V/12 V DAC output back into the ADC; current_in reads the 4-20 mA input (J8);
    current_out switches the DAC voltage outputs off for the 4-20 mA output (J9); off parks everything.
    range also sets the ADC input range (see adc_range); off and cal2 put it back to normal.
    """
    state = await _call(lambda: SESSION.require().analog_path(path, range=range))
    return m.AnalogPathResult(path=state.path, range=state.range or None)


@mcp.tool(annotations=_ann("Read or switch the ADC input range", idempotent=True))
async def adc_range(
    range: AdcRangeArg = None,
    test: Annotated[bool, Field(description=(
        "Run the self-test instead: the 5 V output drives the cal1 loopback at 1 V and 3.5 V and both ranges read it. "
        "Nothing may be connected to the 5 V output."))] = False,
) -> m.AdcRangeResult:
    """Read or switch the ADC input range (analog module v3r4 and later, firmware 3.9.5+).

    normal is the ÷12 divider: ±32.8 V, about 1 mV per count. low is ÷2: ±5.46 V, about 167 µV per count,
    6x the resolution for signals up to ±5 V. The low range's input is 1.82 MΩ: tell the user to use 1x
    leads, not a 10x scope probe. It applies to ext, cal1 and current_in (not cal2), and stays until changed,
    analog_path off or cal2, or a pod restart. capture_adc and adc_read scale with the range in use.
    A board without the low range refuses low. Use low for small signals; switch back to normal when a
    reading or capture reports overrange.
    """
    result = None
    if test:
        res = await _call(lambda: SESSION.require().adc_range_test())
        result = m.AdcRangeTestResult(passed=res.passed, points=[
            m.AdcRangeTestPointResult(set_voltage=p.set_voltage, normal=p.normal, low=p.low, ok=p.ok)
            for p in res.points])
    st = await _call(lambda: SESSION.require().adc_range(None if test else range))
    return m.AdcRangeResult(range=st.range, supported=st.supported, fullscale=st.fullscale,
                            normal_fullscale=st.normal_fullscale, low_fullscale=st.low_fullscale, test=result)


@mcp.tool(annotations=_ann("Set a DC output voltage", destructive=True, idempotent=True))
async def dac_output(
    path: DacOutputPath,
    volts: Annotated[Optional[float], Field(description="Calibrated DC volts to drive; omit to only route.")] = None,
) -> m.DacOutputResult:
    """Route a DAC output (3v3, 5v, 12v = ±12 V, cal1, or off) and drive a calibrated DC voltage on it.

    cal1 sets the level on the 5 V DAC stage routed into the ADC loopback (the 5 V output is
    disconnected while it is on), so adc_read source cal1 reads it back with no relay switching in
    between. It needs firmware with feature dac_out_cal1 (the pod refuses older firmware with a clear error).
    """
    out = await _call(lambda: SESSION.require().dac_output(path, volts=volts))
    # code16 is new in embeddedci 2.11; the MCP still runs on older SDKs (CI: mcp-min-sdk).
    return m.DacOutputResult(path=out.path, voltage=out.voltage, code=out.code,
                             code16=getattr(out, "code16", None))


@mcp.tool(annotations=_ann("Set the 4-20 mA output current", destructive=True, idempotent=True))
async def current_out(
    current: Annotated[Optional[float], Field(description=(
        "Loop current to hold, in AMPS: 0.004 to 0.020 (0.012 is 12 mA). Omit to only read the range."))] = None,
) -> m.CurrentOutResult:
    """Hold a current on the 4-20 mA output (terminal J9), in amps, or read the range it can do.

    The output is loop powered: it needs an external floating supply on J9 (pin 1 plus to the supply
    plus, pin 2 through the receiver to the supply minus; 8 V plus 20 mA times the loop resistance,
    30 V at most). Nothing in the loop may touch pod ground, also not through a target that shares
    a ground with the pod. Ask the user how J9 is wired before driving it. Never wire J9 straight into J8.
    The pod cannot see the loop: the call succeeds with the supply off.
    It cannot go below its live zero (a little above 0.004 A) or above about 0.0201 A: there is no 0 mA or 21 mA level.
    0.004 gives the lowest current; anything else outside the range fails with the pod's message.
    The DAC is shared: this switches the 3v3/5v/12v outputs off, and dac_output, generate and replay
    also move the loop current. dac_stop leaves the loop where it was: set 0.004 to go back to 4 mA.
    """
    if current is None:
        out = await _call(lambda: SESSION.require().current_out_range())
    else:
        out = await _call(lambda: SESSION.require().current_out(current))
    return m.CurrentOutResult(current=out.current, code=out.code,
                              min_current=out.min_current, max_current=out.max_current)


@mcp.tool(annotations=_ann("Read a voltage", read_only=True))
async def adc_read(source: AdcSource = "ext", range: AdcRangeArg = None) -> m.AdcReadResult:
    """One calibrated voltage: ext = front SMA (true volts), cal1/cal2 = DAC loopbacks, current_in = 4-20 mA input (J8).

    current_in also returns current, the loop current in amps. range low reads small signals (up to ±5.46 V)
    at 6x the resolution on analog module v3r4+; overrange says the input clipped. From analog module v4
    current_in reads in the low range unless range is given (the next other route gives normal back).

    Refused while the input is still moving (e.g. a DAC output left running).
    """
    r = await _call(lambda: SESSION.require().adc_read(source, range=range))
    return m.AdcReadResult(source=r.source, voltage=r.voltage, count=r.count, span=r.span,
                           offset=r.offset, current=r.current, sense_ohms=getattr(r, "sense_ohms", None), range=r.range or None,
                           fullscale=r.fullscale, overrange=r.overrange)


def _calibration_result(c: Any) -> m.CalibrationResult:
    comp = getattr(c, "adc_comp", None)  # an older embeddedci SDK has no such field
    dac = {k: (None if v is None else m.DacLineResult(a=v.a, b=v.b, offset=v.offset, gain_ppm=v.gain_ppm))
           for k, v in (c.dac or {}).items()}
    return m.CalibrationResult(source=c.source, calibrated=c.calibrated, offset=c.offset, a=c.a, b=c.b,
                               count=c.count, span=c.span, samples=c.samples, dac=dac,
                               adc_gain_ppm=dict(c.adc_gain_ppm or {}), dmm=c.dmm, measured=c.measured,
                               adc_low_gain_ppm=c.adc_low_gain_ppm,
                               adc_comp=None if comp is None else {
                                   "hf_dc_gain": comp.hf_dc_gain, "c_top_pf": comp.c_top_pf},
                               zero_count=c.zero_count)


@mcp.tool(annotations=_ann("Read the pod's calibration", read_only=True))
async def calibration() -> m.CalibrationResult:
    """The calibration the pod has stored: the ADC offset (measured on current_in, terminal J8) and the fit it uses,
    and on firmware 3.9.4+ the DMM-guided parts (dac: a line per DAC output, adc_gain_ppm: a gain per ADC source).

    calibrated=false means the pod has no ADC offset: analog readings can be off by up to about 50 mV.
    """
    return _calibration_result(await _call(lambda: SESSION.require().calibration()))


@mcp.tool(annotations=_ann("Calibrate the pod", destructive=True, idempotent=True))
async def calibrate(
    source: Annotated[CalibrateSource, Field(description="What to calibrate. Only current_in, the 4-20 mA input.")] = "current_in",
    clear: Annotated[bool, Field(description="True removes the stored calibration instead of measuring one.")] = False,
    clear_part: Annotated[Optional[CalibrationPart], Field(description=(
        "With clear=true, remove only this part: offset, dac (the DMM lines) or adc (the ADC gains). Firmware 3.9.4+."))] = None,
) -> m.CalibrationResult:
    """Calibrate the pod's ADC offset on its 4-20 mA input (current_in, terminal J8) and store it on the pod.

    Nothing may be connected to J8: ask the user to disconnect it and confirm before calling.
    The pod reads its open input (0 V) and keeps that offset in flash. Every later adc_read on current_in
    has it taken out, and on firmware 3.9.3+ every other ADC source and capture too (they share the front end).
    Once per pod is enough: it survives reboots and firmware updates.
    The pod refuses, and keeps what it had, when something is driving J8 (more than 150 mV; 50 mV before 3.9.3).
    clear=true removes the stored calibration (all of it, or clear_part only).
    """
    if clear:
        return _calibration_result(await _call(lambda: SESSION.require().clear_calibration(clear_part)))
    return _calibration_result(await _call(lambda: SESSION.require().calibrate(source)))


@mcp.tool(annotations=_ann("Store a DAC output's DMM calibration", destructive=True, idempotent=True))
async def calibrate_dac(
    path: Annotated[DacCalPath, Field(description="The DAC output: 3v3, 5v or 12v.")],
    code1: Annotated[int, Field(ge=0, le=65535, description="The 16-bit code of the first set point: code16 from dac_output.")],
    volts1: Annotated[float, Field(description="What the DMM read at the first set point, in volts.")],
    code2: Annotated[int, Field(ge=0, le=65535, description="The 16-bit code of the second set point.")],
    volts2: Annotated[float, Field(description="What the DMM read at the second set point, in volts.")],
) -> m.CalibrationResult:
    """Store this pod's own line for a DAC output from two DMM readings (firmware 3.9.4+, cap cal_dmm).

    The flow, with the user holding a bench DMM: ask them to disconnect the DUT from the output and put the DMM
    (DC volts) on that output's SMA. Drive each set point with dac_output(path, volts) and keep its code16
    (3v3: 0.5 and 3.0 V, 5v: 1.0 and 4.0 V, 12v: -5 and +5 V), ask the user what the DMM reads, then call this
    with both points and switch the output off (dac_output path off). From then on dac_output, generate and
    the DAC output limits use this line. The pod refuses, and keeps what it had, when the codes are too close,
    a reading is far from what the output should give (DMM on the wrong SMA) or the gain is more than 2 % off.
    """
    return _calibration_result(await _call(
        lambda: SESSION.require().calibrate_dac(path, [(code1, volts1), (code2, volts2)])))


@mcp.tool(annotations=_ann("Measure an ADC gain through a loopback", destructive=True, idempotent=True))
async def calibrate_adc_gain(
    source: Annotated[AdcGainSource, Field(description=(
        "cal1 (and current_in) through the 5 V output, cal2 through the ±12 V output, ext (the front SMA, "
        "captures and the control loop) through an SMA cable from the ±12 V output to the ADC input."))],
    range: Annotated[AdcRange, Field(description=(
        "low measures the ADC low range's gain instead (analog module v3r4+, firmware 3.9.5+): source must be "
        "cal1, and the one gain serves every source in the low range."))] = "normal",
) -> m.CalibrationResult:
    """Measure and store an ADC source's gain against a DMM-calibrated DAC output (firmware 3.9.4+).

    Needs the ADC offset (calibrate) and the driving output's DMM line (calibrate_dac on 5v for cal1, 12v for
    cal2 and ext). For ext, ask the user to connect the SMA cable first. The pod drives two set points itself
    and switches the outputs off afterwards; measured says what it saw. Nothing to read on a DMM.
    """
    return _calibration_result(await _call(lambda: SESSION.require().calibrate_adc_gain(source, range=range)))


@mcp.tool(annotations=_ann("Capture an ADC waveform"))
async def capture_adc(
    samples: Samples = 4096,
    sample_rate_hz: RateHz = None,
    source: Annotated[Optional[AdcSource], Field(description="Route this ADC source first; omit to keep routing.")] = None,
    points: EnvelopePoints = 200,
    trigger_la: TriggerLa = None,
    trigger_edge: Annotated[TriggerEdge, Field(description="What starts the capture on trigger_la.")] = "rising",
    trigger_timeout_s: TriggerTimeoutS = 10.0,
    range: AdcRangeArg = None,
    oversample: Annotated[bool, Field(description=(
        "Convert at 400 kS/s and average down to sample_rate_hz: lower noise and a smaller step at 0 V, "
        "for short captures (about 5 s at most)."))] = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.AdcCaptureResult:
    """Capture the ADC and summarise it: calibrated stats, dominant frequency and a min/max envelope.

    source current_in captures the 4-20 mA input (J8): the stats are then the loop current in amps
    (unit "A"), and replay(from_last_capture=true, dac_path="current_out") plays it back as a current.

    Above 32768 samples the capture streams from PSRAM (multi-second captures work). With
    trigger_la the capture waits for that edge or level, so t = 0 is the trigger moment. The
    capture is kept for replay and save_capture_as_recording. range low captures small signals
    (up to ±5.46 V) at 6x the resolution (see adc_range); clipped_samples > 0 means it clipped.
    oversample suits measuring small or near-0 V signals at low rates; the result's oversample says
    how many conversions each sample averages, and it fails if they do not fit in the pod's memory.
    """
    def op() -> m.AdcCaptureResult:
        cap = SESSION.require().capture_adc(samples, sample_rate_hz=sample_rate_hz, source=source,
                                            trigger=_trigger(trigger_la, trigger_edge),
                                            trigger_timeout=trigger_timeout_s, range=range,
                                            oversample=oversample)
        SESSION.last_adc = cap
        return adc_summary(cap, points)

    return await _call_reporting(ctx, "capturing ADC", op)


@mcp.tool(annotations=_ann("Capture logic channels"))
async def capture_la(
    samples: Samples = 4096,
    sample_rate_hz: RateHz = None,
    stop_dac_after_s: Annotated[Optional[float], Field(description=(
        "Seconds into the capture to cut a running DAC output (see the DUT react)."))] = None,
    trigger_la: TriggerLa = None,
    trigger_edge: Annotated[TriggerEdge, Field(description="What starts the capture on trigger_la.")] = "rising",
    trigger_timeout_s: TriggerTimeoutS = 10.0,
    trigger_pre_samples: Annotated[int, Field(ge=0, le=16_777_215, description=(
        "With trigger_la: also return up to this many samples from before the trigger "
        "(gateware v50+)."))] = 0,
    send: Annotated[Optional[m.SendSpec], Field(description=(
        "Run one transaction right after the capture arms (gateware v55+): i2c (the i2c_open "
        "session, else one from the wiring), spi (SPI pins from the wiring) or gpio (a GPIO "
        "output). Without trigger_la it triggers on SDA / CS falling or the gpio pin's edge."))] = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.LaCaptureResult:
    """Capture all 14 LA channels and summarise each: levels, edge count, first edge, estimated frequency.

    With trigger_la the capture starts on that edge or level instead of immediately, so a short
    event can be caught at a high sample rate. The capture is kept for decode_la and la_timing.
    sample_rate_hz goes up to 12 MS/s, or 48 MS/s on gateware v50+ (48, 24 or 16 MS/s); above
    12 MS/s a signal changing over ~6 million times per second for long stops early (truncated);
    retry at 12 MS/s.

    send i2c or spi needs the pod's bus master, which the emulation image (gateware v65+) lacks:
    with no I2C/SPI/UART sensor running the pod switches to the loop image by itself (~3 s); with
    sensors running it refuses (BP0059): sensor_stop first, or capture before sensor_start.
    """
    def op() -> m.LaCaptureResult:
        pod = SESSION.require()
        if send is not None:
            la = _capture_with_send(pod, send, samples, sample_rate_hz, stop_dac_after_s, trigger_la,
                                    trigger_edge, trigger_timeout_s, trigger_pre_samples)
        else:
            la = pod.capture_la(samples, sample_rate_hz=sample_rate_hz,
                                stop_dac_after=stop_dac_after_s,
                                trigger=_trigger(trigger_la, trigger_edge, trigger_pre_samples),
                                trigger_timeout=trigger_timeout_s)
        SESSION.last_la = la
        return la_summary(la)

    return await _call_reporting(ctx, "capturing LA", op)


def _capture_with_send(pod: Any, spec: m.SendSpec, samples: int, rate: Optional[float],
                       stop_dac_after: Optional[float], trigger_la: Any, edge: str, timeout: float,
                       pre: int) -> Any:
    """capture_la with a send: the session it needs (opened from the wiring when none is), and the
    trigger on the line the transaction starts with when none is given."""
    def capture(send: Any, auto_la: int, auto_edge: str) -> Any:
        trig = _trigger(trigger_la, edge, pre) if trigger_la is not None else Trigger(auto_la, auto_edge, pre)
        return pod.capture_la(samples, sample_rate_hz=rate, stop_dac_after=stop_dac_after,
                              trigger=trig, trigger_timeout=timeout, send=send)

    if spec.bus == "i2c":
        if spec.addr is None:
            raise ValueError("an i2c send needs addr")
        send = I2CSend(spec.addr, bytes.fromhex(spec.write_hex.replace(" ", "")), spec.read)
        if SESSION.i2c is not None:
            return capture(send, SESSION.i2c.sda, "falling")
        with pod.open_i2c() as i2c:
            return capture(send, i2c.sda, "falling")
    if spec.bus == "spi":
        send = SpiSend(bytes.fromhex(spec.tx_hex.replace(" ", "")))
        with pod.open_spi(hz=spec.spi_hz, mode=spec.spi_mode) as spi:
            return capture(send, spi.cs, "falling")
    if spec.la is None or spec.level is None:
        raise ValueError("a gpio send needs la and level")
    la_i = _la(pod, spec.la)
    return capture(GpioSend(la_i, spec.level), la_i, "falling" if spec.level == 0 else "rising")


@mcp.tool(annotations=_ann("Capture ADC and logic together"))
async def capture_correlated(
    adc_samples: Annotated[int, Field(ge=0, le=8_000_000)] = 4096,
    adc_sample_rate_hz: RateHz = None,
    la_samples: Annotated[int, Field(ge=0, le=8_000_000)] = 4096,
    la_sample_rate_hz: RateHz = None,
    stop_dac_after_s: Annotated[Optional[float], Field(description="Seconds into the capture to cut a running DAC.")] = None,
    points: EnvelopePoints = 200,
    trigger_la: TriggerLa = None,
    trigger_edge: Annotated[TriggerEdge, Field(description="What starts both streams on trigger_la.")] = "rising",
    trigger_timeout_s: TriggerTimeoutS = 10.0,
    source: Annotated[Optional[AdcSource], Field(description=(
        "Route this ADC source first for the analog half; omit to keep routing. current_in captures "
        "the 4-20 mA input (J8) and the analog stats are then amps."))] = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.CorrelatedCaptureResult:
    """ADC and LA captured from one hardware trigger, so their timebases align. Both are kept.

    With trigger_la both streams start on that edge or level. There is one ADC: the analog half is
    the SMA voltage or, with source current_in, the loop current on J8 (unit "A"), never both.
    """
    def op() -> m.CorrelatedCaptureResult:
        cc = SESSION.require().capture_correlated(
            adc_samples=adc_samples, adc_sample_rate_hz=adc_sample_rate_hz, la_samples=la_samples,
            la_sample_rate_hz=la_sample_rate_hz, stop_dac_after=stop_dac_after_s,
            trigger=_trigger(trigger_la, trigger_edge), trigger_timeout=trigger_timeout_s, source=source)
        if adc_samples:
            SESSION.last_adc = cc.adc
        if la_samples:
            SESSION.last_la = cc.la
        SESSION.correlated = bool(adc_samples and la_samples)
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
    dp: Annotated[Optional[LaPin], Field(description="usb: the channel on D+.")] = None,
    dm: Annotated[Optional[LaPin], Field(description="usb: the channel on D-.")] = None,
    speed: Annotated[Literal["auto", "full", "low"], Field(description=(
        "usb: full (12 Mbit/s), low (1.5 Mbit/s) or auto (from the idle state)."))] = "auto",
    tx: Annotated[Optional[LaPin], Field(description=(
        "modbus: the other direction on a two-line tap (rx alone for an RS-485 receive output)."))] = None,
    can: Annotated[Optional[LaPin], Field(description="can: the transceiver's RXD (or the controller's TX/RX).")] = None,
    bitrate: Annotated[Optional[float], Field(gt=0, le=5_000_000, description="can: nominal bit rate, bit/s.")] = None,
    data_bitrate: Annotated[Optional[float], Field(gt=0, le=12_000_000, description=(
        "can: CAN FD data bit rate, bit/s (left out: the nominal rate)."))] = None,
    sample_point: Annotated[Optional[float], Field(gt=0, lt=1, description=(
        "can: where a bit is read, as a fraction of it (default 0.75)."))] = None,
    channels: Annotated[Optional[List[LaPin]], Field(description="pwm: the channels to measure.")] = None,
    servo: Annotated[bool, Field(description="pwm: also read the high pulses as servo positions (1 ms = 0 %, 2 ms = 100 %).")] = False,
    onewire: Annotated[Optional[LaPin], Field(description="onewire: the bus line.")] = None,
    pec: Annotated[Literal["auto", "on", "off"], Field(description=(
        "smbus/pmbus: auto = a trailing byte that matches is a PEC; on = every transaction ends in one."))] = "auto",
    capture: Annotated[Literal["last", "new"], Field(description=(
        "last = decode the previous capture_la; new = capture first (needs samples/sample_rate_hz)."))] = "last",
    samples: Samples = 16384,
    sample_rate_hz: Annotated[float, Field(gt=0, le=50_000_000)] = 1_000_000,
    max_items: Annotated[int, Field(ge=1, le=5000)] = 200,
) -> m.DecodeResult:
    """Decode I2C (sda, scl), UART (rx, baud), SPI (sclk + mosi/miso/cs, mode), USB (dp, dm,
    speed), Modbus RTU (rx, baud, tx), CAN/CAN FD (can, bitrate, data_bitrate), PWM (channels),
    1-Wire (onewire) or SMBus/PMBus (sda, scl, pec) from LA data.

    Sample at least ~10x the bit rate; USB full speed needs a 48 MS/s capture, low speed 6 MS/s;
    CAN 6 samples per data bit; 1-Wire 1 MS/s (4 MS/s in overdrive).
    USB items are transactions (token, data, handshake; repeated NAKs merged), transfers the
    control transfers with the SETUP request and descriptors decoded (an enumeration).
    Decoding the last capture again with other channels is free.
    """
    channels_arg = channels or []

    def op() -> m.DecodeResult:
        pod = SESSION.require()
        if protocol == "i2c" and (sda is None or scl is None):
            raise ValueError("i2c needs sda and scl")
        if protocol == "uart" and (rx is None or baud is None):
            raise ValueError("uart needs rx and baud")
        if protocol == "spi" and sclk is None:
            raise ValueError("spi needs sclk")
        if protocol == "usb" and (dp is None or dm is None):
            raise ValueError("usb needs dp and dm")
        if protocol == "modbus" and (rx is None or baud is None):
            raise ValueError("modbus needs rx and baud")
        if protocol == "can" and (can is None or bitrate is None):
            raise ValueError("can needs can (the channel) and bitrate")
        if protocol == "pwm" and not channels_arg:
            raise ValueError("pwm needs channels")
        if protocol == "onewire" and onewire is None:
            raise ValueError("onewire needs onewire (the channel)")
        if protocol in ("smbus", "pmbus") and (sda is None or scl is None):
            raise ValueError(f"{protocol} needs sda and scl")
        if protocol == "usb":
            try:
                from embeddedci.benchpod import usb_decode
            except ImportError:
                raise ValueError("usb decode needs embeddedci 4.0.2 or later: pip install -U embeddedci") from None
        la = SESSION.last_la
        if capture == "new" or la is None:
            la = pod.capture_la(samples, sample_rate_hz=sample_rate_hz)
            SESSION.last_la = la
        channels = {k: v for k, v in (("sda", sda), ("scl", scl), ("rx", rx), ("baud", baud),
                                      ("sclk", sclk), ("mosi", mosi), ("miso", miso), ("cs", cs),
                                      ("dp", dp), ("dm", dm), ("tx", tx), ("can", can),
                                      ("bitrate", bitrate), ("data_bitrate", data_bitrate),
                                      ("sample_point", sample_point), ("onewire", onewire))
                    if v is not None}
        if protocol == "pwm":
            channels["channels"] = list(channels_arg)
            channels["servo"] = servo
        if protocol in ("smbus", "pmbus"):
            channels["pec"] = pec
        if protocol == "spi":
            channels["mode"] = mode
        if protocol == "usb":
            channels["speed"] = speed
        frames = pod.decode(la, protocol, **channels)
        try:
            SESSION.last_decode = _decode_view(la, protocol, channels, frames)
        except Exception:  # the capture view's copy never fails the decode itself
            SESSION.last_decode = None
        text = None
        if protocol == "usb":
            items = usb_decode.format_transactions(frames)
            transfers = usb_decode.format_transfers(frames)
            return m.DecodeResult(protocol=protocol, count=len(items), items=items[:max_items],
                                  truncated=len(items) > max_items, speed=frames.speed,
                                  transfers=transfers[:max_items], errors=frames.error_count,
                                  sof_count=frames.sof_count)
        if protocol in _LA_FORMATTERS:
            items = _LA_FORMATTERS[protocol](frames)
            n = len(frames.channels) if protocol == "pwm" else len(frames)
            errors = 0 if protocol == "pwm" else sum(1 for f in frames if not f.ok)
            return m.DecodeResult(protocol=protocol, count=n, items=items[:max_items],
                                  truncated=len(items) > max_items, errors=errors)
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


def _t(us: float) -> str:
    return f"{us / 1e6:.6f}s"


def _fmt_modbus(frames) -> List[str]:
    return [f"{_t(f.start_us)} {f.role} addr {f.addr} {f.summary}" + ("" if f.ok else f" error={f.error}")
            for f in frames]


def _fmt_can(frames) -> List[str]:
    out = []
    for f in frames:
        kind = "" if f.kind == "data" else f" {f.kind}"
        fd = (" FD" + (" BRS" if f.brs else "")) if f.fd else ""
        out.append(f"{_t(f.start_us)} {f.id_hex}{kind}{fd} [{f.len}] {f.data_hex}".rstrip()
                   + ("" if f.ok else f" error={f.error}"))
    return out


def _fmt_pwm(res) -> List[str]:
    out = []
    for c in res.channels:
        if not c.count:
            out.append(f"LA{c.channel}: no full period (level {c.level})")
            continue
        line = (f"LA{c.channel}: {c.count} periods, {c.freq_hz.mean:.6g} Hz ({c.freq_hz.min:.6g}-{c.freq_hz.max:.6g}), "
                f"duty {100 * c.duty.mean:.2f} % ({100 * c.duty.min:.2f}-{100 * c.duty.max:.2f}), "
                f"high {c.high_us.mean:.4g} us, jitter {c.jitter_us:.3g} us")
        if c.glitches:
            line += f", {c.glitches} glitches"
        if c.servo:
            line += f", servo {c.servo.pulse_ms.mean:.3f} ms ({c.servo.position_pct.mean:.1f} %)"
        out.append(line)
    return out


def _fmt_onewire(tx) -> List[str]:
    out = []
    for t in tx:
        parts = [_t(t.start_us), "reset" if t.reset else "(no reset)"]
        if t.reset:
            parts.append("presence" if t.presence else "no presence")
        if t.overdrive:
            parts.append("overdrive")
        parts += [p for p in (t.rom_command_name, t.rom, t.function, t.detail) if p]
        if t.bytes_hex:
            parts.append(f"[{t.bytes_hex}]")
        out.append(" ".join(parts) + ("" if t.ok else f" error={t.error}"))
    return out


def _fmt_smbus(tx) -> List[str]:
    out = []
    for t in tx:
        cmd = t.command_name or (f"cmd 0x{t.command:02X}" if t.command is not None else "")
        parts = [p for p in (_t(t.start_us), t.addr_hex, t.kind, cmd, t.data_hex, t.value) if p]
        if t.pec != "none":
            parts.append(f"PEC {t.pec}")
        out.append(" ".join(parts) + ("" if t.ok else f" error={t.error}"))
    return out


_LA_FORMATTERS = {"modbus": _fmt_modbus, "can": _fmt_can, "pwm": _fmt_pwm, "onewire": _fmt_onewire,
                  "smbus": _fmt_smbus, "pmbus": _fmt_smbus}


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

def _dac_path_arg(dac_path: Optional[str], route: bool) -> Dict[str, Any]:
    """The dac_path keyword for generate/replay. Left out and routed: 3v3, the lowest voltage
    output, passed explicitly so an older SDK (whose default is 5v) plays there too. Left out with
    route false: omitted, so the SDK scales for the output already routed (cal1 = 5v)."""
    if dac_path is not None:
        return {"dac_path": dac_path}
    return {"dac_path": "3v3"} if route else {}


@mcp.tool(annotations=_ann("Generate a waveform", destructive=True))
async def generate(
    waveform: Waveshape,
    freq_hz: Annotated[float, Field(gt=0)],
    amplitude: Annotated[float, Field(gt=0, description="Peak volts. On dac_path current_out: peak AMPS (0.006 = 6 mA).")],
    offset: Annotated[Optional[float], Field(description=(
        "Center volts; default mid-range of dac_path. On current_out: center AMPS (0.012 = 12 mA)."))] = None,
    dac_path: Annotated[Optional[DacPath], Field(description=(
        "DAC output. Omitted: 3v3, the lowest voltage output (with route false: the output last routed, "
        "cal1 = 5v). Pick the one the DUT pin tolerates: 5v and 12v can damage a 3.3 V pin. "
        "cal1 = the 5 V stage routed into the ADC loopback (5 V output disconnected; needs firmware "
        "with dac_out_cal1)."))] = None,
    duration_s: Annotated[Optional[float], Field(description="Seconds; omit to run until dac_stop.")] = None,
    sample_rate_hz: RateHz = None,
    on_capture: Annotated[bool, Field(description="Start on the next capture's hardware t0 (phase-locked).")] = False,
    route: Annotated[bool, Field(description=(
        "Route dac_path first. false = keep the current analog path (e.g. after analog_path('cal1') "
        "for a DAC-to-ADC loopback); dac_path then only sets the volts scaling."))] = True,
) -> m.GenerateResult:
    """Drive a sine, square or sawtooth on a DAC output.

    The pod takes the volts, calibrates them for dac_path and plays the waveform at the DAC's full
    16 bits; both peaks must be on the path.

    dac_path current_out plays it as a current on the 4-20 mA output (J9), with amplitude and offset
    in amps and steps of about 0.25 uA. See current_out for the wiring it needs. The DAC voltage
    outputs are switched off for it. dac_stop then leaves the loop where the waveform stopped: call
    current_out(0.004) to go back to 4 mA.
    """
    handle = await _call(lambda: SESSION.require().generate(
        waveform, freq_hz=freq_hz, amplitude=amplitude, offset=offset, **_dac_path_arg(dac_path, route),
        duration=duration_s, sample_rate_hz=sample_rate_hz, on_capture=on_capture, route=route))
    return m.GenerateResult(waveform=waveform, freq_hz=freq_hz, dac_path=handle.dac_path, cotrig=handle.cotrig)


@mcp.tool(annotations=_ann("Stop the DAC", idempotent=True))
async def dac_stop() -> m.StopResult:
    """Stop any DAC output: generator, replay or control loop.

    The 4-20 mA output follows the DAC, so after a waveform on current_out the loop stays where it
    stopped: call current_out(0.004) to return it to 4 mA.
    """
    await _call(lambda: SESSION.require().dac_stop())
    return m.StopResult()


def _image_name(info: Any) -> Optional[str]:
    """The tool-level name of the image an SDK switch went to (None when there was no switch)."""
    if info is None:
        return None
    return {int(FpgaImage.LOOP): "loop", int(FpgaImage.DEEP_REPLAY): "deep_replay",
            int(FpgaImage.EMULATION): "emulation"}.get(int(info.image), str(int(info.image)))


def _replay_result(handle: Any) -> m.ReplayResult:
    return m.ReplayResult(samples=handle.samples, sample_rate_hz=handle.sample_rate_hz,
                          dac_path=handle.dac_path, deep=handle.deep, cotrig=handle.cotrig,
                          switched_image=_image_name(handle.switched_image))


@mcp.tool(annotations=_ann("Replay a waveform", destructive=True))
async def replay(
    volts: Annotated[Optional[List[float]], Field(description=(
        "Waveform in volts, one value per sample. On dac_path current_out the values are AMPS (0.004 to 0.020)."))] = None,
    from_last_capture: Annotated[bool, Field(description="Replay the last capture_adc instead of volts.")] = False,
    waveform_id: Annotated[Optional[str], Field(description=(
        "Instead of volts: a waveform from the cloud library (list_waveforms; needs server access)."))] = None,
    dac_path: Annotated[Optional[DacPath], Field(description=(
        "DAC output. Omitted: 3v3, the lowest voltage output (with route false: the output last routed, "
        "cal1 = 5v; for waveform_id: the waveform's own, else 3v3). Pick the one the DUT pin "
        "tolerates: 5v and 12v can damage a 3.3 V pin. cal1 is for generate and dac_output, not replay."))] = None,
    mapping: Annotated[ReplayMapping, Field(description="faithful = reproduce volts (clip); fit = auto-scale.")] = "faithful",
    sample_rate_hz: Annotated[Optional[float], Field(description="Replay rate; defaults to the capture's own rate.")] = None,
    window_start: Annotated[int, Field(ge=0, description="waveform_id: first recording sample to replay.")] = 0,
    window_len: Annotated[int, Field(ge=0, description="waveform_id: samples to replay; 0 = to the end.")] = 0,
    target_samples: Annotated[int, Field(ge=0, description=(
        "waveform_id: downsample a shallow replay to this many samples."))] = 0,
    fault: Optional[m.FaultSpec] = None,
    on_capture: bool = False,
    route: Annotated[bool, Field(description="Route dac_path first (false = keep the current analog path).")] = True,
    switch_image: SwitchImage = True,
    ctx: Context = None,  # type: ignore[assignment]
) -> m.ReplayResult:
    """Loop a waveform out of the DAC until dac_stop: agent-provided volts, the last ADC capture,
    or a waveform from the cloud library (waveform_id).

    More than 2048 samples need the deep_replay gateware image, switched to automatically (a
    library waveform with target_samples is downsampled instead). dac_path current_out replays on
    the 4-20 mA output (J9) in amps. A last capture of current_in, or a library recording with unit
    mA, replays as the same current; a voltage recording there needs mapping fit. See current_out
    for the wiring. After dac_stop, call current_out(0.004) to go back to 4 mA.
    """
    given = sum((volts is not None, from_last_capture, waveform_id is not None))
    if given != 1:
        raise ToolError("invalid argument: pass exactly one of volts, from_last_capture=true or waveform_id")
    if waveform_id is not None:
        return await _call_reporting(ctx, "arming replay", lambda: _replay_result(
            SESSION.require().replay_waveform(
                waveform_id, dac_path=dac_path, mapping=mapping, sample_rate_hz=sample_rate_hz,
                window_start=window_start, window_len=window_len, target_samples=target_samples,
                fault=_fault(fault), on_capture=on_capture, switch_image=switch_image)))

    def op() -> m.ReplayResult:
        pod = SESSION.require()
        source: Any = volts
        if from_last_capture:
            if SESSION.last_adc is None:
                raise SessionStateError("no ADC capture yet — run capture_adc first")
            source = SESSION.last_adc
        return _replay_result(pod.replay(source, **_dac_path_arg(dac_path, route), mapping=mapping,
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
                                                    sample_rate_hz=w.sample_rate_hz,
                                                    unit=getattr(w, "unit", "V") or "V",
                                                    dac_path=getattr(w, "dac_path", "") or None)
                                     for w in wfs])


# -- cloud (embeddedci.com, no pod connection needed) -------------------------------------

def _cloud_api() -> Tuple[ServerApi, str]:
    """A server client from BENCHPOD_API_KEY, else from the `benchpod login` session."""
    base = os.environ.get(API_BASE_ENV) or DEFAULT_API_BASE
    key = os.environ.get(API_KEY_ENV)
    if key:
        return ServerApi(api_base=base, api_key=key.strip()), "api_key"
    return ServerApi(api_base=base, token_provider=lambda: cli_login.access_token(base)), "benchpod_login"


@mcp.tool(annotations=_ann("List cloud BenchPods", read_only=True, cloud=True))
async def cloud_list_devices() -> m.CloudDeviceList:
    """The BenchPods registered to your embeddedci.com organisation, and whether each is online.
    Drive one with connect(connection) using the returned `connection` ('embeddedci:<name>').

    Needs no pod connection. Authenticates with BENCHPOD_API_KEY, else the session saved by
    `benchpod login` (~/.config/benchpod-cli/token.json), refreshing it when it has expired.
    """
    def op() -> m.CloudDeviceList:
        api, auth = _cloud_api()
        devices = [m.CloudDevice(
            id=str(d.get("id", "")), name=str(d.get("name", "")), online=bool(d.get("online")),
            connection=f"{CLOUD_PREFIX}{d.get('name', '')}", last_active_at=d.get("last_active_at"),
            parameters={str(k): str(v) for k, v in (d.get("parameters") or {}).items()},
        ) for d in api.list_devices()]
        return m.CloudDeviceList(auth=auth, devices=devices)

    return await _call(op, lock=False)


def _runs_api() -> Tuple[Any, str]:
    """The SDK's runs API over the same credentials as the other cloud_* tools."""
    try:
        from embeddedci.benchpod.runs.api import RunsApi
    except ImportError as exc:  # an SDK older than the runs API
        raise ToolError("reading test runs needs a newer embeddedci SDK (embeddedci.benchpod.runs.api); "
                        "upgrade the embeddedci package") from exc
    server, auth = _cloud_api()
    return RunsApi(server), auth


def _lookup(fn: Callable[[], T]) -> Callable[[], T]:
    def op() -> T:
        try:
            return fn()
        except LookupError as exc:
            raise ToolError(str(exc)) from exc
    return op


@mcp.tool(annotations=_ann("List test runs", read_only=True, cloud=True))
async def cloud_runs_list(
    suite: Annotated[Optional[str], Field(description="Only this suite (the run's key.suite).")] = None,
    dut: Annotated[Optional[str], Field(description="Only this board under test ('none' for pod-only suites).")] = None,
    build: Annotated[Optional[str], Field(description="Only the runs linked to this build id.")] = None,
    pod: Annotated[Optional[str], Field(description="Only the runs on this pod (name or id).")] = None,
    ref: Annotated[Optional[str], Field(description="Only the runs of this git branch or ref of the tests.")] = None,
    limit: Annotated[int, Field(ge=1, le=200, description="How many runs, newest first.")] = 20,
    before: Annotated[Optional[str], Field(description="The next_before of the previous page.")] = None,
) -> m.CloudRunList:
    """Test runs uploaded to embeddedci.com (benchpod ci, pytest --embeddedci-upload, build_report),
    newest first, with their outcome and counts. Needs no pod connection; authenticates like
    cloud_list_devices."""
    def op() -> m.CloudRunList:
        api, auth = _runs_api()
        data = api.list_runs(suite=suite, dut=dut, build=build, pod=pod, ref=ref, limit=limit, before=before)
        return m.CloudRunList(auth=auth, runs=list(data.get("runs") or []),
                              next_before=data.get("next_before"), org_pods=data.get("org_pods"))

    return await _call(op, lock=False)


@mcp.tool(annotations=_ann("Read a test run", read_only=True, cloud=True))
async def cloud_run_get(
    run_id: str,
    full: Annotated[bool, Field(description="Also return the whole run.json (every case, inputs, calls).")] = False,
) -> m.CloudRun:
    """One test run: outcome, counts, health, the pod and tests it ran with, the failed cases with
    the first line of their message, and the measurements outside their limits. cloud_run_trace
    reads a failed case's pod calls; cloud_run_compare shows what changed since an earlier run."""
    def op() -> m.CloudRun:
        from embeddedci.benchpod.runs.api import summarize_run

        api, auth = _runs_api()
        doc = api.get_run(run_id)
        return m.CloudRun(auth=auth, summary=summarize_run(doc), files=list(doc.get("files") or []),
                          run=doc if full else None)

    return await _call(op, lock=False)


@mcp.tool(annotations=_ann("Compare test runs", read_only=True, cloud=True))
async def cloud_run_compare(
    run_id: str,
    compare_with: Annotated[str, Field(description=(
        "'previous' (the newest earlier valid run of the same suite and DUT), a run id, or "
        "'baseline:<branch>' (the newest valid run on that branch)."))] = "previous",
) -> m.CloudRunCompare:
    """What changed between an earlier run (a) and this one (b): newly failing and fixed cases,
    measurements that moved or broke a limit, slower tests and pod calls, and the inputs that differ
    (pod firmware, wiring, DUT firmware), so the likely cause sits next to the change."""
    def op() -> m.CloudRunCompare:
        api, auth = _runs_api()
        data = api.compare_runs(run_id, compare_with)
        found = bool(data.get("found", True))
        return m.CloudRunCompare(auth=auth, found=found, message=data.get("message"),
                                 diff={k: v for k, v in data.items() if k not in ("found", "message")})

    return await _call(op, lock=False)


@mcp.tool(annotations=_ann("Read a test case's pod calls", read_only=True, cloud=True))
async def cloud_run_trace(
    run_id: str,
    case_id: Annotated[str, Field(description=(
        "The case id (pytest node id), or a unique end of it ('test_ack')."))],
    errors_only: Annotated[bool, Field(description="Only the calls that failed.")] = False,
    limit: Annotated[int, Field(ge=1, le=2000, description="How many lines, from the start.")] = 200,
) -> m.CloudRunTrace:
    """Every call a test case made to the pod (what it sent, how long it took, what the pod
    answered), from the trace uploaded with the run. Only failed and error cases have one."""
    def op() -> m.CloudRunTrace:
        api, auth = _runs_api()
        t = api.case_trace(run_id, case_id, errors_only=errors_only, limit=limit)
        return m.CloudRunTrace(auth=auth, case=t["case"], trace=t["trace"], lines=t["lines"],
                               total=t["total"], errors=t["errors"], shown=t["shown"],
                               truncated=t["truncated"])

    return await _call(_lookup(op), lock=False)


@mcp.tool(annotations=_ann("Save capture to the library", cloud=True))
async def save_capture_as_recording(
    name: Annotated[str, Field(min_length=1)],
    full_scale_v: Annotated[Optional[float], Field(description="Volts the top code represents; default the peak.")] = None,
) -> m.RecordingResult:
    """Save the last capture_adc to the cloud waveform library as a replayable recording.

    A capture of current_in (the 4-20 mA input) is stored as a current, unit mA; full_scale_v is
    then in mA too.
    """
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
    image: Annotated[Literal["loop", "deep_replay", "emulation"], Field(description=(
        "loop = control-loop image; deep_replay = deep DAC replay from PSRAM; "
        "emulation = sensor emulation (sensor_start loads it by itself)."))],
    ctx: Context = None,  # type: ignore[assignment]
) -> m.FpgaImageResult:
    """Load another stored gateware image into the pod's FPGA (about 0.1 s from firmware 4.0.6,
    2-3 s before; asking for the image that already runs does nothing).

    Rarely needed: control_loop and replay switch automatically (also back from
    the emulation image once no I2C/SPI/UART sensor runs). The pod never switches back by itself,
    and refuses to leave the emulation image while I2C/SPI/UART sensors run (sensor_stop first).
    The switch resets the FPGA (any DAC output or UART session stops). After a restart or power
    cycle the pod runs the loop image (firmware 4.0.6+).

    Gateware v65+: SWD, JTAG, the SPI master and the I2C master are not in the emulation image.
    flash, spi_*, i2c_*, expansion and capture_la with send i2c/spi switch from it to the loop image
    by themselves when no sensor runs, and refuse (BP0059) while sensors run.
    """
    img = {"loop": FpgaImage.LOOP, "deep_replay": FpgaImage.DEEP_REPLAY,
           "emulation": FpgaImage.EMULATION}[image]
    info = await _call_reporting(ctx, "switching gateware", lambda: SESSION.require().fpga_image(img))
    return m.FpgaImageResult(image=image, version=info.version, features=info.features)


# -- CAN ------------------------------------------------------------------------------

def _frame(f: Any) -> m.CanFrameInfo:
    return m.CanFrameInfo(id=f.id, id_hex=f"0x{f.id:X}", data=list(f.data), ext=f.ext, rtr=f.rtr,
                          fd=f.fd, brs=f.brs, ts_ms=f.ts)


@mcp.tool(annotations=_ann("Open CAN"))
async def can_open(
    bitrate: Annotated[int, Field(ge=10_000, le=1_000_000)] = 500_000,
    mode: Annotated[CanMode, Field(description=(
        "normal = on a bus with other nodes; internal/external = loopback self-test; listen = silent."))] = "normal",
    term: Annotated[bool, Field(description="Engage the 120 ohm termination.")] = False,
    data_bitrate: Annotated[Optional[int], Field(ge=10_000, le=8_000_000, description=(
        "CAN FD: the data-phase bit rate (bit/s). Omit for classic CAN. Above bitrate, frames switch to it "
        "after arbitration; equal to bitrate, FD frames without switching. Exact rates only, e.g. 1, 2, 4, "
        "5 or 8 Mbit/s. Needs pod firmware 3.10.2+."))] = None,
) -> m.CanOpenResult:
    """Bring up the pod's CAN interface (replacing any open CAN session): classic CAN, or CAN FD
    (up to 64-byte frames) with data_bitrate."""
    def op() -> m.CanOpenResult:
        pod = SESSION.require()
        SESSION.close_can()
        SESSION.can = pod.open_can(bitrate=bitrate, mode=mode, term=term, data_bitrate=data_bitrate)
        SESSION.can_config = (bitrate, mode, term)
        return m.CanOpenResult(open=True, bitrate=bitrate, mode=mode, term=term,
                               data_bitrate=data_bitrate, fd=data_bitrate is not None)

    return await _call(op)


@mcp.tool(annotations=_ann("Send a CAN frame", destructive=True))
async def can_write(
    can_id: Annotated[int, Field(ge=0, le=0x1FFFFFFF)],
    data: Annotated[Optional[List[Byte]], Field(max_length=64, description=(
        "0-8 data bytes on classic CAN, up to 64 on CAN FD (padded with zeros to 12, 16, 20, 24, 32, 48 or 64)."))] = None,
    ext: Annotated[bool, Field(description="29-bit extended identifier.")] = False,
    rtr: bool = False,
    fd: Annotated[Optional[bool], Field(description=(
        "CAN FD bus only: false sends a classic frame. Default: FD frames on an FD bus."))] = None,
    brs: Annotated[Optional[bool], Field(description=(
        "CAN FD frames only: false keeps the data phase at the nominal bitrate. Default: switch."))] = None,
) -> m.CanWriteResult:
    """Queue one CAN frame (classic, or CAN FD on a bus opened with data_bitrate)."""
    reply = await _call(lambda: SESSION.require_can().write(can_id, data or [], ext=ext, rtr=rtr,
                                                            fd=fd, brs=brs))
    r = reply if isinstance(reply, dict) else {}
    return m.CanWriteResult(dlc=r.get("dlc"), fd=r.get("fd"), brs=r.get("brs"))


@mcp.tool(annotations=_ann("Read CAN frames", read_only=True))
async def can_read(
    timeout_s: Annotated[float, Field(ge=0, le=120, description=(
        "With can_id: wait up to this long for it. Without: collect frames for this long (0 = what is buffered)."))] = 0.0,
    can_id: Optional[Annotated[int, Field(ge=0, le=0x1FFFFFFF)]] = None,
    max_frames: Annotated[int, Field(ge=1, le=1000)] = 32,
) -> m.CanReadResult:
    """Read received CAN frames (id, data, timestamps in pod milliseconds)."""
    def op() -> m.CanReadResult:
        SESSION.require()
        bus = SESSION.require_can()
        if can_id is not None:
            f = bus.read_until(can_id, timeout=timeout_s)
            return m.CanReadResult(frames=[_frame(f)] if f else [], matched=f is not None)
        frames = bus.collect(timeout_s) if timeout_s > 0 else bus.read(max_frames)
        return m.CanReadResult(frames=[_frame(f) for f in frames[:max_frames]])

    return await _call(op)


@mcp.tool(annotations=_ann("CAN auto-responder", destructive=True))
async def can_respond(
    match_id: Optional[int] = None,
    reply_id: Optional[int] = None,
    reply_data: Annotated[Optional[List[Byte]], Field(max_length=64, description=(
        "Reply bytes: up to 8 on classic CAN, 64 on CAN FD."))] = None,
    match_ext: bool = False,
    reply_ext: bool = False,
    reply_fd: Annotated[Optional[bool], Field(description=(
        "CAN FD bus only: false replies with a classic frame. Default: FD on an FD bus."))] = None,
    reply_brs: Annotated[Optional[bool], Field(description=(
        "CAN FD replies only: false keeps the data phase at the nominal bitrate."))] = None,
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
                                  reply_ext=reply_ext, reply_fd=reply_fd, reply_brs=reply_brs)
        rules = reply.get("rules") if isinstance(reply, dict) else None
        return m.CanRespondResult(rules=int(rules) if rules is not None else None)

    return await _call(op)


@mcp.tool(annotations=_ann("CAN status", read_only=True))
async def can_status() -> m.DeviceReply:
    """CAN link state: mode, bitrate, data_bitrate (CAN FD), error counters, bus-off."""
    return m.DeviceReply(reply=await _call(lambda: SESSION.require().can_status()))


@mcp.tool(annotations=_ann("Close CAN", idempotent=True))
async def can_close() -> m.CanOpenResult:
    """Clear responder rules, stop CAN and switch the 120 ohm termination off (safe when not open)."""
    await _call(SESSION.close_can)
    return m.CanOpenResult(open=False)


# -- stepper + escape hatch ------------------------------------------------------------

@mcp.tool(annotations=_ann("Step pulse train", destructive=True))
async def la_step(
    la: LaPin,
    steps: Annotated[int, Field(ge=1, le=65535, description="Number of step pulses (at most 65535).")],
    delay_s: Annotated[float, Field(ge=4e-6, le=0.065535,
                                  description="HALF the step period in seconds: each pulse is high for "
                                              "delay_s, then low for delay_s, so pulses are 2 x delay_s apart. "
                                              "4 us to 65.535 ms.")],
    dir_la: Optional[LaPin] = None,
    direction: Literal[0, 1] = 0,
) -> m.StepResult:
    """Emit step pulses on an LA channel (step/dir motor drivers); with dir_la, set direction first.

    The step rate is 1 / (2 x delay_s). The FPGA runs the train by itself; this returns as soon as it
    starts. More than 65535 steps: send several trains (wait for the pod between them).
    """
    reply = await _call(lambda: SESSION.require().la_step(la, steps=steps, delay=delay_s, dir_la=dir_la,
                                                         direction=direction))
    return m.StepResult(la=la, steps=steps, delay=delay_s, status=str(reply.get("status", "started")))


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


# The last capture and decode in full, for a capture view (the BenchPod desktop app draws them
# with zoom and decode overlays). Resources, not tools: an agent reads the summaries the capture
# tools return, and these can be megabytes.

#: Most LA edges / ADC samples a resource returns; past them it says truncated.
CAPTURE_VIEW_MAX_EDGES = 4_000_000
CAPTURE_VIEW_MAX_SAMPLES = 4_194_304


def _decode_view(la: Any, protocol: str, channels: Dict[str, Any], frames: Any) -> dict:
    """A decode in the capture view's shape: I2C transactions with timed segments, UART and SPI
    frames (the EmbeddedCI web app's decode result)."""
    view: Dict[str, Any] = {"seq": SESSION.la_seq, "protocol": protocol,
                            "channels": {k: v for k, v in channels.items()}}
    if protocol in ("usb", "pwm"):
        view.update({k: v for k, v in frames.as_dict().items() if k != "protocol"})
    elif protocol in ("modbus", "can"):
        view["frames"] = [f.as_dict() for f in frames]
    elif protocol in ("onewire", "smbus", "pmbus"):
        view["transactions"] = [t.as_dict() for t in frames]
    elif protocol == "i2c":
        view["transactions"] = i2c.timed_transactions(la.words, channels["sda"], channels["scl"],
                                                      la.sample_rate_hz)
    else:
        view["frames"] = [dataclasses.asdict(f) for f in frames]
    return view


@mcp.resource("benchpod://capture/la", name="capture_la_data", mime_type="application/json",
              title="Last logic capture, every edge",
              description=("The last capture_la / capture_correlated / decode_la capture as run-length "
                           "edges [sample index, 14-bit word] from sample 0, for drawing it. Large: use "
                           "capture_la's summary, la_timing and decode_la to reason about it."))
def capture_la_resource() -> str:
    la = SESSION.last_la
    if la is None:
        return json.dumps({"seq": 0})
    edges: List[List[int]] = []
    prev = None
    truncated = False
    for i, w in enumerate(la.words):
        if w != prev:
            if len(edges) >= CAPTURE_VIEW_MAX_EDGES:
                truncated = True
                break
            edges.append([i, w])
            prev = w
    return json.dumps({
        "seq": SESSION.la_seq, "correlated": SESSION.correlated, "sample_rate_hz": la.sample_rate_hz,
        "count": len(la.words), "pre_samples": getattr(la, "pre_samples", 0),
        "stopped_early": getattr(la, "truncated", "") or "", "edges_truncated": truncated, "edges": edges,
    }, separators=(",", ":"))


@mcp.resource("benchpod://capture/adc", name="capture_adc_data", mime_type="application/json",
              title="Last ADC capture, every sample",
              description=("The last capture_adc / capture_correlated as calibrated samples (volts, or "
                           "mA for the 4-20 mA input), for drawing it. Large: use capture_adc's summary "
                           "to reason about it."))
def capture_adc_resource() -> str:
    cap = SESSION.last_adc
    if cap is None:
        return json.dumps({"seq": 0})
    current = bool(getattr(cap, "currents", None))
    values = [v * 1000.0 for v in cap.currents] if current else list(cap.volts)
    truncated = len(values) > CAPTURE_VIEW_MAX_SAMPLES
    values = values[:CAPTURE_VIEW_MAX_SAMPLES]
    return json.dumps({
        "seq": SESSION.adc_seq, "correlated": SESSION.correlated, "sample_rate_hz": cap.sample_rate_hz,
        "count": len(cap.volts), "unit": "mA" if current else "V", "source": cap.source or None,
        "range": getattr(cap, "range", "") or None, "samples_truncated": truncated,
        "samples": [round(v, 6) for v in values],
    }, separators=(",", ":"))


@mcp.resource("benchpod://capture/decode", name="capture_decode_data", mime_type="application/json",
              title="Last decode, with timing",
              description=("The last decode_la in full: I2C transactions with timed segments, UART / "
                           "SPI / Modbus / CAN frames, USB packets, transactions and transfers, 1-Wire and "
                           "SMBus transactions, or PWM channels, for drawing on the capture. seq is the "
                           "capture it decoded."))
def capture_decode_resource() -> str:
    return json.dumps(SESSION.last_decode or {"seq": 0}, separators=(",", ":"))


# -- AI Vision board ------------------------------------------------------------------------

from . import vision as _vision  # noqa: E402  (registers the vision_* tools on this server)

_vision.register(mcp)
