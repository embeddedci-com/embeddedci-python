"""Test fixtures: a fake transport behind a real BenchPod, and a helper that calls tools the way an
MCP client does — through FastMCP's argument validation, error mapping and result conversion."""

from __future__ import annotations

import math
from typing import Any, Dict, Iterator, List

import anyio
import pytest

from embeddedci.benchpod import BenchPod
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

    # -- Transport ABC --
    def status(self) -> Any:
        return {"version": "2.0.0", "board": "stm32h563", "adc_bits": 16, "adc_fullscale_mv": 4096,
                "caps": ["signal", "la", "uart", "dac", "dac_replay", "dac_cotrig"]}

    def ping(self) -> Any:
        return "pong"

    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        self.power[efuse] = on
        self.calls.append(("target_power", efuse, on, delay_ms))

    def dap_start(self, swclk: int, swdio: int):
        return FakeRawLink()

    def uart_proxy_start(self, rx: int, tx: int, baud: int):
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
        return {"la": la, "pullup": 1 if req.get("pullup") == "on" else 0,
                "ohms": "4.7k" if la <= 2 else "10k", "pull": "down" if la in (7, 8) else "up",
                "pullups_available": 1}

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
        return {"image": req["image"], "version": 30, "features": 1 if req["image"] == 0 else 0}

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
        if cmd == "capture":
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
