"""A hardware-free fake BenchPod transport for the OpenHTF plug tests.

Implements just enough of :class:`embeddedci.benchpod.transport.base.Transport`
to exercise the plug and the phases without a real pod: it records power calls
and every JSON command, answers the commands the v2 SDK sends (``la_voltage``,
``dac_out``, ``generate``, ``dac_stop``, ``analog_path``, ``adc_read``,
``capture`` and ``la_capture`` via ``samples``, control loop, ``fpga_image``,
``la_pins``/``gpio``, ``power_profile``) and serves a canned UART banner.
Inject it with ``benchpod_plug(transport=FakeTransport(...))``.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from embeddedci.benchpod.errors import FirmwareError, TransportError
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
                 fail_capture: bool = False, image: Optional[int] = None,
                 la_words: Optional[List[int]] = None) -> None:
        #: Running gateware image (0 = loop, 1 = deep replay); None = no switchable images.
        self.image = image
        self.banner = banner
        self.power_calls: List[dict] = []
        self.commands: List[dict] = []   # raw `command` + `samples` requests seen
        self.adc_samples = (adc_samples if adc_samples is not None
                            else [0, 64, 128, 192, 255, 192, 128, 64])
        #: Raw 14-channel LA words a `la_capture` returns (bit n = LA{n+1}).
        self.la_words = la_words if la_words is not None else []
        self.fail_capture = fail_capture
        self.closed = False
        #: Capabilities the fake pod reports (the SDK gates GPIO, triggers and power profiles).
        self.caps = ["la", "la_pins", "gpio_read", "capture_trigger", "power_profile"]
        # LA pin ownership, as the firmware keeps it.
        self.function = {la: "none" for la in range(1, 15)}
        self.mode: Dict[int, Any] = {la: None for la in range(1, 15)}
        self.level: Dict[int, Any] = {la: None for la in range(1, 15)}
        #: Levels the "DUT" drives on channels the pod is not driving (bitmask, bit la-1).
        self.inputs = 0
        #: The statistics a power_profile reply carries (µA / mV / µJ / µC, as the firmware sends).
        self.power_stats: Dict[str, Any] = {
            "efuse": 1, "rate_hz": 364.0, "adc_rate_hz": 950.0, "n": 950, "duration_ms": 1000, "avg_ua": 52_000,
            "min_ua": 40_000, "peak_ua": 180_000, "avg_mv": 5010, "min_mv": 4990, "max_mv": 5030,
            "energy_uj": 260_500, "charge_uc": 52_000, "fault": False, "truncated": False}
        #: The bin-averaged trace it carries (µs / µA / mV).
        self.power_samples: Dict[str, Any] = {"t_us": [0, 500_000],
                                              "current_ua": [40_000, 60_000],
                                              "bus_mv": [5010, 5000]}

    def status(self):
        status = {"status": "ok", "fake": True, "caps": list(self.caps)}
        if self.image is not None:
            status["caps"] += ["dac", "dac_replay",
                               "dac_control_loop" if self.image == 0 else "dac_deep_replay"]
        return status

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
            if self.image is not None:
                self.image = req.get("image")
                return {"image": self.image, "version": 27, "features": 1 if self.image == 0 else 2}
            return {"image": req.get("image"), "version": 27, "features": 1}
        if cmd == "la_pins":
            return {"pins": [self._pin(la) for la in range(1, 15)], "levels": self._levels()}
        if cmd == "gpio":
            return self._gpio(req)
        if cmd == "power_profile":
            return self._power_profile(req)
        return None

    # -- LA pin ownership + GPIO (the firmware's la_pins / gpio contract) --
    def _pin(self, la: int) -> dict:
        return {"la": la, "function": self.function[la], "gpio": self.mode[la],
                "level": self.level[la], "pull": None}

    def _levels(self) -> int:
        mask = self.inputs
        for la in range(1, 15):
            if self.mode[la] in ("output", "open_drain") and self.level[la] is not None:
                mask = (mask & ~(1 << (la - 1))) | (self.level[la] << (la - 1))
        return mask

    def _gpio(self, req: dict):
        if "mode" not in req and "level" not in req:
            return {"levels": self._levels(), "pins": [self._pin(la) for la in range(1, 15)]}
        raw = req["la"]
        las = list(range(1, 15)) if raw == "all" else (raw if isinstance(raw, list) else [raw])
        mode = req.get("mode")
        if mode == "off":
            for la in las:
                if self.function[la] == "gpio":
                    self.function[la], self.mode[la], self.level[la] = "none", None, None
        elif mode is not None:
            for la in las:
                if self.function[la] not in ("none", "gpio"):
                    raise FirmwareError(
                        f"pin conflict: LA{la} is in use by {self.function[la]}; "
                        "stop it first", cmd="gpio")
            for la in las:
                self.function[la], self.mode[la] = "gpio", mode
                self.level[la] = (None if mode == "input"
                                  else req.get("level", 1 if mode == "open_drain" else 0))
        else:
            for la in las:
                if self.mode[la] not in ("output", "open_drain"):
                    raise FirmwareError(f"LA{la} is not a gpio output "
                                        f"(function {self.function[la]})", cmd="gpio")
                self.level[la] = req["level"]
        return {"pins": [self._pin(la) for la in las]}

    # -- power profile (no stream_chunks here, so one dict carries the whole reply) --
    def _power_profile(self, req: dict):
        if req.get("action") == "start":
            return {"started": True, "efuse": req.get("efuse", 1), "rate_hz": 950.0}
        if req.get("action") == "status":
            return {"running": False, "efuse": req.get("efuse", 1), "elapsed_ms": 0, "n": 0}
        stats = dict(self.power_stats, efuse=req.get("efuse", self.power_stats["efuse"]))
        keep = int(req.get("keep_samples", 0))
        samples = self.power_samples if keep else {"t_us": [], "current_ua": [], "bus_mv": []}
        return {**samples, "stats": stats}

    def samples(self, req: dict) -> List[int]:
        self.commands.append(req)
        if self.fail_capture:
            raise TransportError("fake capture failure")
        if req.get("cmd") == "la_capture":
            return list(self.la_words)
        return list(self.adc_samples)

    def close(self) -> None:
        self.closed = True
