"""A fake LAN pod for the run-recording tests (test_runs.py copies it into pytester suites)."""

from __future__ import annotations

from typing import Any, Dict, Iterator, List

from embeddedci.benchpod.transport.base import Transport

STATUS = {"version": "3.10.11", "board": "stm32h563", "board_rev": "v3", "analog": True,
          "gateware": 58, "features_rev": 0, "caps": ["calibrate", "la_pins"]}
CALIBRATION = {"source": "current_in", "calibrated": True, "offset": 0.0012}


class _Link:
    def __init__(self) -> None:
        self.error = None
        self.closed = False

    def read(self, n: int) -> bytes:
        return b"x" * min(n, 10)

    def write(self, data: bytes) -> int:
        return len(data)

    def close(self) -> None:
        self.closed = True


class FakePod(Transport):
    """A LAN-like pod: status (via command, like the TCP transport), samples, streams, raw links."""

    def __init__(self, status: Dict[str, Any] = STATUS) -> None:
        self.timeout = 30.0
        self.addr = "benchpod-abc123.local:8080"
        self.status_reply = dict(status)
        self.requests: List[dict] = []
        self.busy_retries = 0

    def command(self, req: dict) -> Any:
        self.requests.append(req)
        cmd = req.get("cmd")
        if cmd == "status":
            return dict(self.status_reply)
        if cmd == "calibrate":
            return dict(CALIBRATION)
        if cmd == "boom":
            raise RuntimeError("BP0412 the pod refused")
        if cmd == "big":
            return {"blob": "y" * 6000}
        if cmd == "busy_twice":
            self.busy_retries += 2
            return {}
        return {"echo": req}

    def command_sequence(self, reqs: List[dict]) -> List[Any]:
        return [self.command(r) for r in reqs]

    def samples(self, req: dict) -> List[int]:
        return list(range(int(req.get("n", 10))))

    def stream_chunks(self, req: dict) -> Iterator[Dict[str, Any]]:
        yield {"data": [1, 2, 3], "more": True}
        yield {"data": [4, 5], "more": False}

    def status(self) -> Any:
        return self.command({"cmd": "status"})

    def status_without_caps(self) -> Any:
        return self.command({"cmd": "status", "caps": False})

    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        self.requests.append({"cmd": "target_power", "efuse": efuse, "on": on})

    def dap_start(self, swclk, swdio, packet_size=None, packet_count=None, wait_ms=None):
        return _Link()

    def uart_proxy_start(self, rx, tx, baud, parity="none", stop_bits=1, half_duplex=False,
                         entry=None, de=None, de_active_low=False):
        return _Link()

    def close(self) -> None:
        pass
