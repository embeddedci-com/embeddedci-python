"""A hardware-free fake BenchPod transport for the OpenHTF plug tests.

Implements just enough of :class:`embeddedci.benchpod.transport.base.Transport`
to exercise the plug and the phases without a real pod: it records power calls
and every JSON command, answers the commands the v2 SDK sends (``la_voltage``,
``dac_out``, ``generate``, ``dac_stop``, ``analog_path``, ``adc_read``,
``capture`` via ``samples``, control loop, ``fpga_image``) and serves a canned
UART banner. Inject it with ``benchpod_plug(transport=FakeTransport(...))``.
"""

from __future__ import annotations

import threading
from typing import List, Optional

from embeddedci.benchpod.errors import TransportError
from embeddedci.benchpod.transport.base import RawLink, Transport


class FakeUartLink:
    """A RawLink that emits ``payload`` once, then blocks until closed.

    Mirrors a real proxy link: ``read`` blocks while the DUT is quiet and returns
    ``b""`` only at EOF/close, so ``embeddedci.benchpod.uart.capture`` ends either
    on its ``until`` match or when its duration timer calls ``close()``.
    """

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._sent = False
        self._closed = threading.Event()

    def read(self, n: int) -> bytes:
        if self._closed.is_set():
            return b""
        if not self._sent:
            self._sent = True
            return self._payload
        self._closed.wait()  # park until close() (the capture timer or teardown)
        return b""

    def write(self, data: bytes) -> int:
        return len(data)

    def close(self) -> None:
        self._closed.set()


class FakeTransport(Transport):
    """Minimal in-memory BenchPod transport.

    ``adc_samples`` are the raw counts a ``capture`` returns; the default is a clean
    triangle (min 0, max 255, mean 127.875). The fake ``status`` carries no ADC
    calibration, so the SDK scales counts linearly: ``volts = count * 3.3 / 255``.
    ``fail_capture=True`` makes captures raise, to exercise cleanup paths.
    """

    def __init__(self, banner: bytes = b"reset\r\nAPP_OK build 1\r\n",
                 adc_samples: Optional[List[int]] = None,
                 fail_capture: bool = False) -> None:
        self.banner = banner
        self.power_calls: List[dict] = []
        self.commands: List[dict] = []   # raw `command` + `samples` requests seen
        self.adc_samples = (adc_samples if adc_samples is not None
                            else [0, 64, 128, 192, 255, 192, 128, 64])
        self.fail_capture = fail_capture
        self.closed = False

    def status(self):
        return {"status": "ok", "fake": True}

    def ping(self):
        return {"status": "ok", "data": "pong"}

    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        self.power_calls.append({"efuse": efuse, "on": on, "delay_ms": delay_ms})

    def dap_start(self, swclk: int, swdio: int) -> RawLink:
        raise NotImplementedError("fake transport does not flash")

    def uart_proxy_start(self, rx: int, tx: int, baud: int) -> RawLink:
        return FakeUartLink(self.banner)

    # JSON commands. Returns the reply *data* (what the real transport.command yields).
    def command(self, req: dict):
        self.commands.append(req)
        cmd = req.get("cmd")
        if cmd == "la_voltage":
            return {"mv": req.get("mv", 0)}
        if cmd == "analog_path":
            return {"path": req.get("path"), "u55": 0x03, "u58": 0x09}
        if cmd == "dac_out":
            has_v = "volts" in req
            return {"path": req.get("path"),
                    "mv": int(round(float(req["volts"]) * 1000)) if has_v else 0,
                    "code": 128 if has_v else -1}
        if cmd == "generate":
            return {"cotrig": bool(req.get("on_capture", False))}
        if cmd == "adc_read":
            src = req.get("source", "ext")
            return {"source": src, "mv": 12034 if src == "ext" else 2502, "count": 63049,
                    "span": 3}
        if cmd == "dac_control_loop":
            return {"armed": True, "k": req.get("k"), "vmin": req.get("vmin"),
                    "vmax": req.get("vmax"), "tick_div": req.get("tick_div"), "curve_pts": 256}
        if cmd == "dac_loop_probe":
            return {"i": 4096, "v": 51000}
        if cmd == "fpga_image":
            return {"image": req.get("image"), "version": 27, "features": 1}
        return None

    def samples(self, req: dict) -> List[int]:
        self.commands.append(req)
        if self.fail_capture:
            raise TransportError("fake capture failure")
        return list(self.adc_samples)

    def close(self) -> None:
        self.closed = True
