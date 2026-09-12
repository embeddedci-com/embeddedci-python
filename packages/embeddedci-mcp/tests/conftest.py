"""Test fixtures: a fake transport behind a real BenchPod, and a helper that calls tools the way an
MCP client does — through FastMCP's argument validation, error mapping and result conversion."""

from __future__ import annotations

import math
from typing import Any, Dict, Iterator, List, Optional

import anyio
import pytest

from embeddedci.benchpod import BenchPod
from embeddedci.benchpod.constants import PULL_OHMS, PULLDOWN_CHANNELS
from embeddedci.benchpod.errors import FirmwareError
from embeddedci.benchpod.transport.base import Transport

import embeddedci_mcp.session as session_mod
from embeddedci_mcp.server import mcp
from embeddedci_mcp.session import SESSION


class FakeRawLink:
    """A bounded byte stream: yields ``data`` once, then EOF (``b""``)."""

    def __init__(self, data: bytes = b"") -> None:
        self._buf = bytearray(data)
        self.written = bytearray()
        self.closed = False

    def read(self, n: int) -> bytes:
        if self.closed or not self._buf:
            return b""
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def write(self, data: bytes) -> int:
        self.written += data
        return len(data)

    def close(self) -> None:
        self.closed = True


def sine_counts(n: int, *, rate_hz: float = 100_000.0, freq_hz: float = 1000.0) -> List[int]:
    """Raw ADC counts that the stm32h563 affine model scales to 2.5 V ± 1 V at ``freq_hz``."""
    return [int(63052 + 996 * math.sin(2 * math.pi * freq_hz * i / rate_hz)) for i in range(n)]


class FakeTransport(Transport):
    """In-memory pod answering the JSON-command and streaming surface the SDK uses."""

    def __init__(self) -> None:
        self.power: Dict[int, bool] = {}
        self.calls: List[tuple] = []
        self.requests: List[dict] = []
        self.uploads: List[dict] = []
        self.uart_data = b"boot\r\nAPP_OK\r\n"
        self.uart_links: List[FakeRawLink] = []
        self._sensor: Dict[str, Any] = {"active": False}
        self.la_mv = 0
        self.reset_asserted = False
        self.can_rx: List[dict] = []
        self.rules = 0
        #: Running gateware image (0 = loop, 1 = deep replay); None = a pod without switchable images.
        self.image = None
        #: Capabilities the pod reports; drop entries to test a pod without a feature.
        self.caps = ["signal", "la", "uart", "dac", "dac_replay", "dac_cotrig",
                     "la_pins", "gpio_read", "capture_trigger", "power_profile"]
        # -- LA pin ownership (the firmware's table): function, gpio mode and commanded level.
        self.function: Dict[int, str] = {la: "none" for la in range(1, 13)}
        self.mode: Dict[int, Any] = {la: None for la in range(1, 13)}
        self.level: Dict[int, Any] = {la: None for la in range(1, 13)}
        self.pull_on: Dict[int, bool] = {la: False for la in range(1, 9)}
        #: Levels the "DUT" drives on channels the pod is not driving (bitmask, bit la-1).
        self.inputs = 0
        #: Set to a firmware message to make the next streamed capture / power profile fail with it.
        self.error: Optional[str] = None
        self.power_profile_running = False
        self.power_efuse = 1
        #: The bin-averaged trace a power_profile reply carries (µA / mV, µs since the start).
        self.power_samples: Dict[str, Any] = {
            "t_us": [0, 250_000, 500_000, 750_000],
            "current_ua": [40_000, 180_000, 50_000, 48_000],
            "bus_mv": [5010, 4990, 5000, 5005]}
        #: The statistics its last chunk carries (µA / mV / µJ / µC, as the firmware sends them).
        self.power_stats: Dict[str, Any] = {
            "rate_hz": 364.0, "adc_rate_hz": 950.0, "n": 950, "duration_ms": 1000, "avg_ua": 52_000, "min_ua": 40_000,
            "peak_ua": 180_000, "avg_mv": 5010, "min_mv": 4990, "max_mv": 5030,
            "energy_uj": 260_500, "charge_uc": 52_000, "fault": False, "truncated": False}

    # -- Transport ABC --
    def status(self) -> Any:
        caps = list(self.caps)
        if self.image is not None:
            caps.append("dac_control_loop" if self.image == 0 else "dac_deep_replay")
        return {"version": "2.0.0", "board": "stm32h563", "adc_bits": 16, "adc_fullscale_mv": 4096,
                "caps": caps}

    def ping(self) -> Any:
        return "pong"

    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        self.power[efuse] = on
        self.calls.append(("target_power", efuse, on, delay_ms))

    def dap_start(self, swclk: int, swdio: int):
        return FakeRawLink()

    def uart_proxy_start(self, rx: int, tx: int, baud: int):
        for la, fn in list(self.function.items()):  # a new proxy replaces the previous one
            if fn in ("uart_rx", "uart_tx"):
                self.function[la] = "none"
        for la in (rx, tx):
            if self.function[la] != "none":
                raise FirmwareError(self._conflict(la), cmd="uart_proxy_start")
        self.function[rx], self.function[tx] = "uart_rx", "uart_tx"
        link = FakeRawLink(self.uart_data)
        self.uart_links.append(link)
        return link

    def close(self) -> None:
        self.calls.append(("close",))

    # -- JSON commands --
    def command(self, req: dict) -> Any:
        self.requests.append(req)
        cmd = req["cmd"]
        self.calls.append(("command", cmd))
        handler = getattr(self, f"_cmd_{cmd}", None)
        return handler(req) if handler else {}

    def _cmd_status(self, req):
        return self.status()

    def _cmd_la_voltage(self, req):
        if "mv" in req:
            self.la_mv = req["mv"]
        return {"mv": self.la_mv, "st": 1, "readback_mv": self.la_mv}

    def _cmd_target_status(self, req):
        return {"efuse1": {"enabled": int(self.power.get(1, False)), "fault": 0, "valid": 1},
                "efuse2": {"enabled": 0, "fault": 1, "valid": 1}, "status_supported": True}

    def _cmd_power_status(self, req):
        return {"internal": {"ok": True, "bus_mv": 5010, "current_ua": 42000},
                "external": {"ok": False, "bus_mv": 0, "current_ua": 0}}

    def _cmd_nrst(self, req):
        if "assert" in req:
            self.reset_asserted = bool(req["assert"])
        return {"supported": True, "asserted": self.reset_asserted}

    def _cmd_sensor_start(self, req):
        self._sensor = {"active": True, "type": req["type"], "addr": req["addr"]}
        return {"type": req["type"], "addr": req["addr"]}

    def _cmd_sensor_set(self, req):
        return {"type": "bmp280"}

    def _cmd_sensor_stop(self, req):
        self._sensor = {"active": False}
        return None

    def _cmd_sensor_status(self, req):
        return {"active": self._sensor.get("active", False), "transactions": 3}

    def _cmd_la(self, req):
        if "steps" in req:
            return {"la": req["la"], "steps": req["steps"], "delay_us": req["delay_us"],
                    "status": "started"}
        if "la" not in req:
            return {"la_pullup_mask": 3, "pullups_available": 1}
        la = req["la"]
        if "pullup" in req:
            self.pull_on[la] = req["pullup"] == "on"
        return {"la": la, "pullup": int(self.pull_on.get(la, False)),
                "ohms": PULL_OHMS.get(la, ""), "pull": "down" if la in PULLDOWN_CHANNELS else "up",
                "pullups_available": 1}

    # -- LA pin ownership + GPIO (the firmware's la_pins / gpio contract) --
    def _conflict(self, la: int) -> str:
        fn = self.function[la]
        how = (f'release it with {{"cmd":"gpio","la":{la},"mode":"off"}}' if fn == "gpio"
               else "stop the uart proxy first")
        return f"pin conflict: LA{la} is in use by {fn}; {how}"

    def _pin(self, la: int) -> Dict[str, Any]:
        pull = None
        if la in PULL_OHMS:
            pull = {"dir": "down" if la in PULLDOWN_CHANNELS else "up", "ohms": PULL_OHMS[la],
                    "on": self.pull_on[la]}
        return {"la": la, "function": self.function[la], "gpio": self.mode[la],
                "level": self.level[la], "pull": pull}

    def _levels(self) -> int:
        mask = self.inputs
        for la in range(1, 13):
            if self.mode[la] in ("output", "open_drain") and self.level[la] is not None:
                mask = (mask & ~(1 << (la - 1))) | (self.level[la] << (la - 1))
        return mask

    def _cmd_la_pins(self, req):
        return {"pins": [self._pin(la) for la in range(1, 13)], "levels": self._levels()}

    def _cmd_gpio(self, req):
        if "mode" not in req and "level" not in req:  # read live levels
            return {"levels": self._levels(), "pins": [self._pin(la) for la in range(1, 13)]}
        raw = req["la"]
        las = list(range(1, 13)) if raw == "all" else (raw if isinstance(raw, list) else [raw])
        mode = req.get("mode")
        if mode == "off":
            for la in las:
                if self.function[la] == "gpio":
                    self.function[la], self.mode[la], self.level[la] = "none", None, None
            return {"pins": [self._pin(la) for la in las]}
        if mode is not None:
            for la in las:  # validate everything before changing anything
                if self.function[la] not in ("none", "gpio"):
                    raise FirmwareError(self._conflict(la), cmd="gpio")
                if mode == "open_drain" and la in (7, 8) and self.pull_on[la]:
                    raise FirmwareError(
                        f"pull conflict: LA{la} has its 10k pull-down engaged, which gpio open_drain "
                        "can't work with (a released line would read low); disable it with "
                        f'{{"cmd":"la","la":{la},"pullup":"off"}} or use another channel', cmd="gpio")
            for la in las:
                self.function[la], self.mode[la] = "gpio", mode
                self.level[la] = (None if mode == "input"
                                  else req.get("level", 1 if mode == "open_drain" else 0))
            return {"pins": [self._pin(la) for la in las]}
        for la in las:
            if self.mode[la] not in ("output", "open_drain"):
                raise FirmwareError(
                    f"LA{la} is not a gpio output (function {self.function[la]}); configure it with "
                    f'{{"cmd":"gpio","la":{la},"mode":"output"}}', cmd="gpio")
            self.level[la] = req["level"]
        return {"pins": [self._pin(la) for la in las]}

    # -- power profile (start/status come through `command`; the result streams in chunks) --
    def _cmd_power_profile(self, req):
        if self.error:
            raise FirmwareError(self.error, cmd="power_profile")
        if req.get("action") == "status":
            return {"running": self.power_profile_running, "efuse": self.power_efuse,
                    "elapsed_ms": 100, "n": 95}
        self.power_profile_running = True
        self.power_efuse = int(req.get("efuse", 1))
        return {"started": True, "efuse": self.power_efuse, "rate_hz": 950.0}

    def _cmd_analog_path(self, req):
        return {"path": req["path"], "u55": 3, "u58": 9}

    def _cmd_dac_out(self, req):
        has_v = "volts" in req
        return {"path": req["path"], "mv": int(round(req["volts"] * 1000)) if has_v else 0,
                "code": 128 if has_v else -1}

    def _cmd_adc_read(self, req):
        return {"source": req.get("source", "ext"), "mv": 3301, "count": 63049, "span": 2}

    def _cmd_generate(self, req):
        return {"cotrig": bool(req.get("on_capture"))}

    def _cmd_fpga_image(self, req):
        if self.image is not None:
            self.image = req["image"]
        return {"image": req["image"], "version": 30, "features": 1 if req["image"] == 0 else 2}

    def _cmd_dac_control_loop(self, req):
        return {"armed": True, "k": req["k"], "vmin": req["vmin"], "vmax": req["vmax"],
                "tick_div": req["tick_div"], "curve_pts": 256, "source": req.get("source"),
                "input": req.get("input", 0), "step": req.get("step", 0)}

    def _cmd_dac_loop_input(self, req):
        return {"source": req.get("source", "fixed"), "input": req.get("input", 0),
                "step": req.get("step", 0), "v": 1234}

    def _cmd_dac_loop_probe(self, req):
        return {"i": 64000, "in": 32768, "source": "fixed", "v": 20000}

    def _cmd_can_write(self, req):
        self.can_rx.append({"id": req["id"], "data": req["data"], "ext": req["ext"],
                            "rtr": req["rtr"], "ts": 100 + len(self.can_rx)})
        return {"queued": True}

    def _cmd_can_read(self, req):
        frames, self.can_rx = self.can_rx[: req["max"]], self.can_rx[req["max"]:]
        return {"frames": frames, "overflow": 0}

    def _cmd_can_respond(self, req):
        if req.get("clear"):
            self.rules = 0
            return {"rules": 0}
        self.rules += 1
        return {"rule": self.rules - 1, "rules": self.rules}

    def _cmd_can_status(self, req):
        return {"mode": "internal", "bitrate": 500000, "bus_off": False}

    # -- chunked / streaming --
    def samples(self, req: dict) -> list:
        self.requests.append(req)
        if req["cmd"] == "sensor_regs":
            return list(range(req.get("len", 256)))
        return []

    def stream_chunks(self, req: dict) -> Iterator[Dict[str, Any]]:
        self.requests.append(req)
        cmd = req["cmd"]
        if self.error:
            raise FirmwareError(self.error, cmd=cmd)
        if cmd == "power_profile":
            self.power_profile_running = False
            if "efuse" in req:
                self.power_efuse = int(req["efuse"])
            yield {"status": "ok", **self.power_samples, "more": True}
            yield {"status": "ok", "t_us": [], "current_ua": [], "bus_mv": [],
                   "stats": dict(self.power_stats, efuse=self.power_efuse), "more": False}
        elif cmd == "capture":
            yield {"status": "ok", "data": sine_counts(req["samples"]), "adc_rate_hz": 100_000.0,
                   "more": False}
        elif cmd == "la_capture":
            n = req["samples"]
            # LA1 toggles every 50 samples (10 kHz at 1 MS/s); LA3 stays high.
            edges = [[i, (0b100 | ((i // 50) % 2))] for i in range(0, n, 50)]
            yield {"status": "ok", "la": True, "la_edges": edges, "la_upto": n,
                   "la_rate_hz": 1_000_000.0, "more": False}
        elif cmd == "capture_dual":
            yield {"status": "ok", "data": sine_counts(req["adc_samples"]),
                   "adc_rate_hz": 100_000.0, "la_rate_hz": 1_000_000.0, "more": True}
            yield {"status": "ok", "la": True, "la_edges": [[0, 1]], "la_upto": req["la_samples"],
                   "more": False}

    def load_replay(self, *, data: bytes, replay: dict, psram: bool = False) -> Any:
        self.uploads.append({"len": len(data), "replay": replay, "psram": psram})
        return {"samples": replay["samples"], "cotrig": bool(replay.get("on_capture"))}


def without_caps(transport: "FakeTransport", *caps: str) -> None:
    """Make the connected pod report firmware without ``caps`` (capabilities are cached on connect)."""
    transport.caps = [c for c in transport.caps if c not in caps]
    SESSION.require().refresh_capabilities()


def call(tool: str, /, **arguments: Any) -> Any:
    """Call a tool through FastMCP and return its structured result."""
    async def run() -> Any:
        return await mcp.call_tool(tool, arguments)

    result = anyio.run(run)
    if isinstance(result, tuple):
        return result[1]
    return result


@pytest.fixture(autouse=True)
def _reset_session(monkeypatch):
    """Each test starts with a clean, disconnected session and no environment defaults."""
    for var in ("BENCHPOD_CONNECTION", "BENCHPOD_LA_VOLTAGE", "BENCHPOD_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    SESSION.disconnect()
    SESSION.default_connection = None
    SESSION.default_la_voltage = None
    SESSION.idle_timeout = 600.0
    yield
    SESSION.disconnect()


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def fake_open(monkeypatch, fake_transport):
    """Make ``connect`` build a BenchPod over the fake transport; records the requested args."""
    seen: Dict[str, Any] = {}

    def factory(connection, **kwargs):
        seen["connection"] = connection
        seen.update(kwargs)
        return BenchPod(transport=fake_transport, lease=False, la_voltage=kwargs.get("la_voltage"))

    monkeypatch.setattr(session_mod, "BenchPod", factory)
    return seen


@pytest.fixture
def connected(fake_open, fake_transport) -> FakeTransport:
    """A session connected (via the connect tool) to the fake pod, LA bank at 3.3 V."""
    call("connect", connection="192.168.1.50", la_voltage=3.3)
    return fake_transport
