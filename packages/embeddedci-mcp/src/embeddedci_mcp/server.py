"""The MCP server: every BenchPod capability mapped to an MCP tool.

Each tool is a thin adapter — coerce JSON-friendly arguments, call the matching
:class:`~embeddedci.benchpod.BenchPod` method, and return a JSON-serializable
result. Device/firmware failures (the :class:`BenchPodError` family) are turned
into structured ``{"ok": false, "error": ..., "error_type": ...}`` results so an
agent can reason about them instead of seeing a raw traceback.

No protocol, flash, or decode logic lives here; it all comes from the
``embeddedci`` SDK. This module only exposes it.
"""

from __future__ import annotations

import functools
import re
from typing import Any, Dict, List, Optional, Union

from mcp.server.fastmcp import FastMCP

from embeddedci.benchpod import i2c
from embeddedci.benchpod.errors import BenchPodError

from .session import SESSION

mcp = FastMCP("embeddedci-benchpod")


# -- helpers ----------------------------------------------------------------

def _safe(fn):
    """Wrap a tool so SDK errors become structured results, not tracebacks.

    ``functools.wraps`` preserves the wrapped function's signature and
    annotations, so FastMCP still derives the correct input schema.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BenchPodError as exc:
            return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}

    return wrapper


def _tail(text: str, limit: int = 4000) -> str:
    """Keep only the last ``limit`` chars (flash logs can be huge)."""
    if not text:
        return text
    return text if len(text) <= limit else "…(truncated)…\n" + text[-limit:]


def _flash_result(result) -> dict:
    return {
        "ok": result.ok,
        "returncode": result.returncode,
        "target_unreachable": result.target_unreachable,
        "stalled": result.stalled,
        "stdout_tail": _tail(result.stdout),
        "stderr_tail": _tail(result.stderr),
    }


def _summarize_samples(data, head: int = 32) -> dict:
    data = list(data)
    out: dict = {"count": len(data), "head": data[:head], "truncated": len(data) > head}
    if data:
        out["min"] = min(data)
        out["max"] = max(data)
    return out


def _compile_until(until_regex: Optional[str]):
    return re.compile(until_regex) if until_regex else None


# -- lifecycle / status -----------------------------------------------------

@mcp.tool()
@_safe
def connect(connection: Optional[str] = None) -> dict:
    """Open a BenchPod connection and return its status.

    ``connection`` is a host[:port] (wifi/TCP, default port 8080), a serial
    device path (e.g. ``/dev/tty.usbserial-0001``), or ``"serial"`` to
    auto-detect. When omitted, the server's ``--connection`` default or the
    ``BENCHPOD_CONNECTION`` environment variable is used. Re-connecting closes
    any previous connection first.
    """
    from embeddedci.benchpod.connection import resolve_connection

    spec = resolve_connection(connection or SESSION.default_connection)
    pod = SESSION.connect(connection)
    info: dict = {
        "connected": True,
        "kind": spec.kind,
        "target": spec.addr or spec.device or "(auto-detect)",
    }
    try:
        info["status"] = pod.status()
    except BenchPodError as exc:  # connected, but status round-trip failed
        info["status_error"] = str(exc)
    return info


@mcp.tool()
@_safe
def disconnect() -> dict:
    """Close the current BenchPod connection (safe if none is open)."""
    SESSION.disconnect()
    return {"connected": False}


@mcp.tool()
@_safe
def ping() -> dict:
    """Confirm the pod is reachable."""
    return {"ping": SESSION.require().ping()}


@mcp.tool()
@_safe
def status() -> Any:
    """Return firmware/connection status (dict over TCP, text over serial)."""
    return SESSION.require().status()


# -- power ------------------------------------------------------------------

@mcp.tool()
@_safe
def power_on(efuse: int = 1, delay: Optional[float] = None) -> dict:
    """Power the target on via an eFuse (1 = internal 5V, 2 = external).

    ``delay`` (seconds) schedules the power-on pod-side and returns immediately —
    use it to power-on *during* a UART capture so the boot banner lands in-window.
    """
    SESSION.require().power_on(efuse, delay=delay)
    return {"ok": True, "efuse": efuse, "on": True, "delay": delay}


@mcp.tool()
@_safe
def power_off(efuse: int = 1, delay: Optional[float] = None) -> dict:
    """Power the target off via an eFuse (1 = internal 5V, 2 = external)."""
    SESSION.require().power_off(efuse, delay=delay)
    return {"ok": True, "efuse": efuse, "on": False, "delay": delay}


@mcp.tool()
@_safe
def target_power(efuse: int, on: bool, delay: Optional[float] = None) -> dict:
    """Enable or disable a target-power eFuse explicitly."""
    SESSION.require().target_power(efuse, on=on, delay=delay)
    return {"ok": True, "efuse": efuse, "on": on, "delay": delay}


@mcp.tool()
@_safe
def target_status() -> Any:
    """Read the eFuse enabled/fault/valid state (RP2350B)."""
    return SESSION.require().command({"cmd": "target_status"})


# -- flash ------------------------------------------------------------------

@mcp.tool()
@_safe
def flash(
    swclk: int,
    swdio: int,
    nreset: Optional[int] = None,
    target: str = "",
    file: str = "",
    load_address: str = "",
    target_power: Optional[int] = None,
    verify: bool = True,
    reset: bool = True,
    connect_under_reset: Optional[bool] = None,
    clear_reset_events: bool = True,
    extra_configs: Optional[List[str]] = None,
    extra_args: Optional[List[str]] = None,
    timeout: float = 300.0,
    connect_attempts: int = 5,
) -> dict:
    """Flash an SWD target via the OpenOCD remote_bitbang bridge.

    ``swclk``/``swdio``/``nreset`` are LA channels (1-12). ``target`` is an
    OpenOCD target cfg (e.g. ``target/stm32f4x.cfg``); ``file`` is the firmware
    image. ``target_power`` (1/2) powers the target before flashing. Returns a
    structured result with ``ok`` plus ``stdout_tail``/``stderr_tail`` — inspect
    ``target_unreachable``/``stalled`` to diagnose failures. Over a serial
    bit-bang link, set ``verify=false`` (the long verify phase is flaky there).
    """
    result = SESSION.require().flash(
        swclk=swclk, swdio=swdio, nreset=nreset,
        target=target, file=file, load_address=load_address,
        target_power=target_power, verify=verify, reset=reset,
        connect_under_reset=connect_under_reset,
        clear_reset_events=clear_reset_events,
        extra_configs=tuple(extra_configs or ()),
        extra_args=tuple(extra_args or ()),
        timeout=timeout, connect_attempts=connect_attempts,
        check=False,
    )
    return _flash_result(result)


# -- UART capture -----------------------------------------------------------

@mcp.tool()
@_safe
def capture_uart(
    rx: int,
    tx: int,
    duration: float,
    baud: int = 115200,
    until_regex: Optional[str] = None,
) -> dict:
    """Capture the DUT's UART output for ``duration`` seconds.

    ``rx`` is the LA channel the pod samples (wire the DUT's TX here); ``tx`` is
    driven (the DUT's RX). ``until_regex`` (a Python regex) stops the capture
    early on first match. Returns ``{text, lines, matched}``.
    """
    cap = SESSION.require().capture_uart(
        rx=rx, tx=tx, baud=baud, duration=duration,
        until=_compile_until(until_regex),
    )
    return {"text": cap.text, "lines": cap.lines, "matched": cap.matched}


@mcp.tool()
@_safe
def power_cycle_and_capture(
    rx: int,
    tx: int,
    efuse: int = 1,
    delay: float = 1.0,
    duration: float = 4.0,
    baud: int = 115200,
    until_regex: Optional[str] = None,
    off_settle: float = 0.3,
) -> dict:
    """Power-cycle the target while capturing its boot output.

    Powers ``efuse`` off, schedules a power-on ``delay`` seconds out (pod-side
    timer), then captures UART for ``duration`` seconds so the boot banner lands
    inside the window. ``duration`` should comfortably exceed ``delay``.
    """
    cap = SESSION.require().power_cycle_and_capture(
        rx=rx, tx=tx, efuse=efuse, delay=delay, duration=duration, baud=baud,
        until=_compile_until(until_regex), off_settle=off_settle,
    )
    return {"text": cap.text, "lines": cap.lines, "matched": cap.matched}


# -- emulated I2C sensor ----------------------------------------------------

@mcp.tool()
@_safe
def enable_i2c_sensor(
    sda: int,
    scl: int,
    sensor: str = "bmp280",
    address: int = 0x76,
    temperature_c: Optional[float] = None,
    pressure_pa: Optional[float] = None,
) -> dict:
    """Make the pod emulate an I2C sensor (e.g. BMP280) on ``sda``/``scl``.

    The pod becomes an I2C slave the DUT's master can read. Enable pull-ups on
    the SDA/SCL LA channels first (``enable_pullup``) so the open-drain bus idles
    high. Optionally seed ``temperature_c``/``pressure_pa``.
    """
    return SESSION.require().enable_i2c_sensor(
        sensor, sda=sda, scl=scl, address=address,
        temperature_c=temperature_c, pressure_pa=pressure_pa,
    )


@mcp.tool()
@_safe
def set_i2c_sensor(temperature_c: Optional[float] = None,
                   pressure_pa: Optional[float] = None) -> dict:
    """Update the emulated sensor's reported values (at least one required)."""
    return SESSION.require().set_i2c_sensor(
        temperature_c=temperature_c, pressure_pa=pressure_pa
    )


@mcp.tool()
@_safe
def disable_i2c_sensor() -> dict:
    """Disarm the emulated sensor (safe if none is active)."""
    SESSION.require().disable_i2c_sensor()
    return {"ok": True}


@mcp.tool()
@_safe
def i2c_sensor_status() -> dict:
    """Return sensor + I2C-bus activity counters (transactions, writes, …)."""
    return SESSION.require().i2c_sensor_status()


@mcp.tool()
@_safe
def i2c_sensor_regs(start: int = 0, length: int = 256) -> dict:
    """Read the emulated sensor's register image."""
    regs = SESSION.require().i2c_sensor_regs(start, length)
    return {"start": start, "length": len(regs), "bytes": regs}


@mcp.tool()
@_safe
def i2c_sensor_la_decoded(samples: int = 1024,
                          sample_rate_mhz: Optional[float] = None) -> dict:
    """Capture the I2C bus and decode it into a human-readable trace.

    Returns a one-line-per-transaction ``trace`` (e.g.
    ``S 0x76W+ 0xD0+ Sr 0x76R+ 0x58- P``), the transaction count, and the set of
    addresses seen — not the raw sample array.
    """
    txns = SESSION.require().i2c_sensor_la_decoded(samples, sample_rate_mhz)
    addrs = sorted({m.address for t in txns for m in t.messages if m.address is not None})
    return {
        "trace": i2c.format_transactions(txns),
        "transactions": len(txns),
        "addresses": [f"0x{a:02X}" for a in addrs],
    }


@mcp.tool()
@_safe
def i2c_read_register(
    address: int,
    register: int,
    samples: int = 4096,
    sample_rate_mhz: float = 0.5,
) -> dict:
    """Capture the I2C bus and return the bytes the DUT read from ``register``.

    Decodes the live waveform and looks for a register-pointer write to
    ``address`` followed by a read. Returns ``{value, addressed, trace}``;
    ``value`` is ``null`` if that read pattern wasn't captured in the window.
    """
    txns = SESSION.require().i2c_sensor_la_decoded(samples, sample_rate_mhz)
    return {
        "value": i2c.read_register(txns, address, register),
        "addressed": i2c.addressed(txns, address),
        "trace": i2c.format_transactions(txns),
    }


# -- LA pin pull-ups (LA1-8) ------------------------------------------------

@mcp.tool()
@_safe
def enable_pullup(las: List[int]) -> dict:
    """Enable the fixed pull-up on one or more LA channels (LA1-8 only)."""
    pod = SESSION.require()
    pod.enable_pullup(*las)
    return pod.pullup_status()


@mcp.tool()
@_safe
def disable_pullup(las: List[int]) -> dict:
    """Disable the pull-up on one or more LA channels (LA1-8 only)."""
    pod = SESSION.require()
    pod.disable_pullup(*las)
    return pod.pullup_status()


@mcp.tool()
@_safe
def pullup_status() -> dict:
    """Return ``{"la_pullup_mask": <bitmask>}`` (bit la-1 set = pull-up on)."""
    return SESSION.require().pullup_status()


# -- low-level pass-throughs (TCP / serial-json) ----------------------------

@mcp.tool()
@_safe
def command(request: Dict[str, Any]) -> Any:
    """Send a raw JSON command to the pod (escape hatch).

    ``request`` must include a ``cmd`` key, e.g. ``{"cmd": "status"}``. Use this
    for firmware commands not covered by a dedicated tool.
    """
    return SESSION.require().command(request)


@mcp.tool()
@_safe
def gpio_set(la: int, state: Union[int, str]) -> Any:
    """Drive an LA channel: ``state`` 1 = high, 0 = low, ``"z"`` = high-Z."""
    return SESSION.require().gpio_set(la, state)


@mcp.tool()
@_safe
def capture_adc(samples: int = 256, sample_rate_mhz: Optional[float] = None) -> dict:
    """Capture ADC samples; returns count/min/max and the first 32 values."""
    data = SESSION.require().capture(samples, sample_rate_mhz=sample_rate_mhz)
    return _summarize_samples(data)


@mcp.tool()
@_safe
def signal_generate(
    waveform: str,
    freq: float,
    amplitude: float,
    offset: float = 0.0,
    duration_ms: Optional[int] = None,
    sample_rate_mhz: Optional[float] = None,
) -> Any:
    """Generate a DAC waveform (sine/square/sawtooth/…) on the analog output."""
    req: dict = {"cmd": "generate", "waveform": waveform,
                 "freq": freq, "amplitude": amplitude, "offset": offset}
    if duration_ms is not None:
        req["duration_ms"] = duration_ms
    if sample_rate_mhz is not None:
        req["sample_rate_mhz"] = sample_rate_mhz
    return SESSION.require().command(req)


@mcp.tool()
@_safe
def measure(
    waveform: str,
    freq: float,
    amplitude: float,
    offset: float = 0.0,
    samples: int = 256,
    sample_rate_mhz: Optional[float] = None,
) -> dict:
    """Drive the DAC and capture the ADC loopback; returns a sample summary."""
    pod = SESSION.require()
    fn = getattr(pod.transport, "samples", None)
    if fn is None:
        raise BenchPodError("measure is only available on the TCP transport")
    req: dict = {"cmd": "measure", "waveform": waveform, "freq": freq,
                 "amplitude": amplitude, "offset": offset, "samples": samples}
    if sample_rate_mhz is not None:
        req["sample_rate_mhz"] = sample_rate_mhz
    return _summarize_samples(fn(req))


# -- resources (read-only context for the agent) ----------------------------

_WIRING = """\
BenchPod LA channel wiring (example mapping from the BMP280 HIL example).
The pod has no fixed pin roles — it exposes 12 generic LA channels (LA1-12) and
any DUT signal can be on any of them. This is just how this bench is wired:

  DUT signal                         Pod LA channel   eFuse / notes
  ---------------------------------  --------------   -----------------------
  SWCLK  (SWD clock)                 LA11
  SWDIO  (SWD data)                  LA12
  NRST   (optional reset)            LA3
  UART: DUT TX  -> pod samples       LA5              (capture_uart rx)
  UART: DUT RX  <- pod drives        LA4              (capture_uart tx)
  I2C SDA                            LA2              4.7k pull-up (LA1/2)
  I2C SCL                            LA1              4.7k pull-up (LA1/2)
  Target 5V power                    eFuse 1          1=internal 5V, 2=external

Pull-ups exist only on LA1-8 (LA1/2=4.7k, LA3/4=2.2k, LA5-8=10k); enable them on
the I2C SDA/SCL channels so the open-drain bus idles high before arming the
emulated sensor. These are bench defaults — confirm against the actual wiring.
"""

_HELP = """\
This server drives an EmbeddedCI BenchPod (hardware-in-the-loop tester) so you
can power, flash, and probe a real target board.

Typical workflow:
  1. connect(connection)            — open the pod (host[:port], /dev/tty*, or 'serial')
  2. flash(swclk, swdio, nreset,    — program the DUT over SWD
           target, file, ...)         (over serial bit-bang use verify=false)
  3. enable_pullup([sda, scl])      — for I2C work, idle the bus high
     enable_i2c_sensor(sda, scl)    — have the pod emulate a sensor
  4. power_cycle_and_capture(rx, tx,— reboot the DUT and capture its boot log
           delay, duration, until_regex)
  5. i2c_sensor_status() /          — confirm the DUT actually probed the bus
     i2c_read_register(addr, reg)

Errors come back as {"ok": false, "error": ..., "error_type": ...} rather than
raising. See the benchpod://wiring resource for the LA channel pin map.
"""


@mcp.resource("benchpod://wiring")
def wiring() -> str:
    """Default LA channel → DUT signal pin map and eFuse table."""
    return _WIRING


@mcp.resource("benchpod://help")
def help_() -> str:
    """How to drive a HIL run with these tools (canonical workflow order)."""
    return _HELP
