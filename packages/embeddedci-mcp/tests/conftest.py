"""Test fixtures: a fake Transport injected into a real BenchPod.

The MCP tools call ``embeddedci.benchpod.BenchPod`` methods; we exercise them
with no hardware by giving BenchPod a :class:`FakeTransport` (which implements
the Transport ABC plus the JSON ``command``/``samples`` extras) and a
:class:`FakeRawLink` for UART/SWD byte streams.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from embeddedci.benchpod import BenchPod
from embeddedci.benchpod.transport.base import Transport

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


class FakeTransport(Transport):
    """In-memory pod that answers the high-level + JSON-command surface."""

    def __init__(self) -> None:
        self.power: dict[int, bool] = {}
        self.calls: list[tuple] = []
        self.uart_data = b"boot\r\nAPP_OK\r\n"
        self._sensor: dict[str, Any] = {"active": False}
        self._regs = list(range(256))

    # -- Transport ABC --
    def status(self) -> Any:
        return {"version": "fake-1.0",
                "caps": ["signal", "gpio", "power", "swd", "i2c_sensor", "uart"]}

    def ping(self) -> Any:
        return "pong"

    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        self.power[efuse] = on
        self.calls.append(("target_power", efuse, on, delay_ms))

    def swd_start(self, swclk: int, swdio: int, nreset: Optional[int]):
        return FakeRawLink()

    def uart_proxy_start(self, rx: int, tx: int, baud: int):
        return FakeRawLink(self.uart_data)

    def close(self) -> None:
        self.calls.append(("close",))

    # -- JSON command extras (TCP / serial-json) --
    def command(self, req: dict) -> Any:
        cmd = req.get("cmd")
        self.calls.append(("command", cmd))
        if cmd in ("status", "ping"):
            return self.status() if cmd == "status" else "pong"
        if cmd == "sensor_start":
            self._sensor = {"active": True, "type": req["type"], "addr": req["addr"],
                            "transactions": 0}
            return {"type": req["type"], "addr": req["addr"]}
        if cmd == "sensor_set":
            return {"type": "bmp280"}
        if cmd == "sensor_stop":
            self._sensor = {"active": False}
            return None
        if cmd == "sensor_status":
            return {"active": self._sensor.get("active", False), "transactions": 3}
        if cmd == "pullup":
            return {"la": req["la"],
                    "pullup": 1 if req.get("state") == "on" else 0, "ohms": "4.7k"}
        if cmd == "pullup_status":
            return {"la_pullup_mask": 3}
        if cmd == "target_status":
            return {"efuse1": {"enabled": 1, "fault": 0, "valid": 1}}
        if cmd == "gpio_set":
            return {"la": req["la"], "state": req["state"]}
        if cmd == "generate":
            return None
        return None

    def samples(self, req: dict) -> list:
        cmd = req.get("cmd")
        self.calls.append(("samples", cmd))
        if cmd == "sensor_regs":
            return self._regs[: req.get("len", 256)]
        if cmd == "sensor_la":
            return []
        if cmd == "measure":
            return [10, 20, 30, 40]
        if cmd == "capture":
            return list(range(req.get("samples", 4)))
        return []


@pytest.fixture(autouse=True)
def _reset_session():
    """Each test starts with a clean, disconnected session."""
    SESSION.disconnect()
    SESSION.default_connection = None
    yield
    SESSION.disconnect()


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def connected(fake_transport: FakeTransport) -> FakeTransport:
    """A session connected to a BenchPod backed by the fake transport."""
    SESSION._pod = BenchPod(transport=fake_transport)
    return fake_transport
