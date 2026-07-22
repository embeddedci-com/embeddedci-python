"""Closed-loop DAC control + DAC↔capture co-trigger/auto-stop (fake transport, no hardware).

Covers the curve builder/encoder (parity with the web UI's controlLoopCurve.ts), the
ControlLoopHandle, and the command plumbing for control_loop / loop_probe / fpga_image, plus the
new on_capture (co-trigger) and stop_dac_after_us (auto-stop) parameters.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, Iterator, List

import pytest

from embeddedci.benchpod import capture as cap_mod
from embeddedci.benchpod import control_loop as cl
from embeddedci.benchpod.capabilities import Capabilities
from embeddedci.benchpod.client import BenchPod
from embeddedci.benchpod.control_loop import ControlLoopHandle, IVPoint


# -- curve builder / encoder (parity with controlLoopCurve.ts) ---------------

def test_build_panel_curve_shape_and_endpoints():
    c = cl.build_panel_curve(1000, sharpness=2, points=5)
    # v = round(voc * (1 - x**p)), x = i/(n-1); p=2 => [1000, .9375, .75, .4375, 0]*1000
    assert c == [1000, 938, 750, 438, 0]


def test_build_panel_curve_clamps_and_defaults():
    assert cl.build_panel_curve(70000, 4, points=3)[0] == 65535  # clamped to 16-bit
    assert cl.build_panel_curve(-5, 4, points=3)[0] == 0
    assert len(cl.build_panel_curve(50000, 4)) == cl.CURVE_POINTS  # default 256
    with pytest.raises(ValueError):
        cl.build_panel_curve(1000, 4, points=0)


def test_encode_curve_b64url_is_le16_no_padding():
    b64 = cl.encode_curve_b64url([1000, 938, 750, 438, 0])
    assert "=" not in b64  # base64url, no padding
    raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    # little-endian 16-bit
    assert list(raw) == [232, 3, 170, 3, 238, 2, 182, 1, 0, 0]
    assert raw[0] | (raw[1] << 8) == 1000


def test_ivpoint_aliases():
    p = IVPoint(i=123, v=456)
    assert p.current_code == 123 and p.voltage_code == 456


def test_control_loop_handle_stop_idempotent():
    stops: List[int] = []
    h = ControlLoopHandle(probe=lambda: IVPoint(0, 0), stop=lambda: stops.append(1),
                          data={"armed": True, "k": 8192, "curve_pts": 256})
    assert h.armed and h.curve_pts == 256
    with h:
        pass
    h.stop()  # idempotent
    assert len(stops) == 1


# -- client plumbing (fake transport records commands, returns canned replies) --

class FakeTransport:
    def __init__(self, replies: Dict[str, Any] | None = None) -> None:
        self.commands: List[dict] = []
        self.uploads: List[dict] = []
        self._replies = replies or {}

    def status(self):
        return {"board": "stm32h563", "adc_bits": 16, "adc_fullscale_mv": 4096}

    def command(self, req: dict) -> Any:
        self.commands.append(req)
        return self._replies.get(req.get("cmd"), {})

    def load_replay(self, *, data: bytes, replay: dict, psram: bool = False):
        self.uploads.append({"replay": replay, "psram": psram})
        return {"samples": replay.get("samples"), "cotrig": True}

    def close(self):
        pass


def _bp(replies=None) -> "tuple[BenchPod, FakeTransport]":
    t = FakeTransport(replies)
    return BenchPod(transport=t, lease=False), t


def test_control_loop_sends_curve_and_returns_handle():
    bp, t = _bp({"dac_control_loop": {"armed": True, "k": 8192, "vmin": 0, "vmax": 52000,
                                      "tick_div": 64, "curve_pts": 256}})
    loop = bp.control_loop(voc_code=52000, sharpness=6, vmax=52000)
    assert isinstance(loop, ControlLoopHandle) and loop.armed and loop.vmax == 52000
    cmd = t.commands[-1]
    assert cmd["cmd"] == "dac_control_loop" and cmd["vmax"] == 52000 and cmd["tick_div"] == 64
    # curve encoded to base64url of the synthesised panel table
    raw = base64.urlsafe_b64decode(cmd["curve"] + "=" * (-len(cmd["curve"]) % 4))
    assert raw[0] | (raw[1] << 8) == 52000  # Voc at zero current


def test_control_loop_raw_codes_and_clamp_only():
    bp, t = _bp({"dac_control_loop": {"armed": True}})
    bp.control_loop(curve=[100, 200, 300])
    raw = base64.urlsafe_b64decode(t.commands[-1]["curve"] + "==")
    assert list(raw[:2]) == [100, 0]
    # clamp-only: no curve field when none provided
    bp.control_loop(k=4096)
    assert "curve" not in t.commands[-1] and t.commands[-1]["k"] == 4096


def test_control_loop_raises_if_not_armed():
    bp, _ = _bp({"dac_control_loop": {"armed": False}})
    with pytest.raises(Exception):
        bp.control_loop(voc_code=1000)


def test_loop_probe_returns_ivpoint():
    bp, t = _bp({"dac_loop_probe": {"i": 4321, "v": 51000}})
    pt = bp.loop_probe()
    assert isinstance(pt, IVPoint) and pt.i == 4321 and pt.v == 51000
    assert t.commands[-1] == {"cmd": "dac_loop_probe"}


def test_fpga_image_swaps_and_invalidates_caps():
    bp, t = _bp({"fpga_image": {"image": 0, "version": 27, "features": 1}})
    _ = bp.capabilities  # prime the cache
    out = bp.fpga_image(0)
    assert out["features"] == 1
    assert t.commands[-1] == {"cmd": "fpga_image", "image": 0}
    assert bp._caps is None  # cache invalidated so the new image's caps re-read


def test_handle_probe_delegates_to_client():
    bp, t = _bp({"dac_control_loop": {"armed": True}, "dac_loop_probe": {"i": 7, "v": 9}})
    with bp.control_loop(voc_code=1000) as loop:
        pt = loop.probe()
        assert pt.i == 7 and pt.v == 9
    # stop -> dac_stop on context exit
    assert t.commands[-1] == {"cmd": "dac_stop"}


# -- co-trigger (on_capture) + auto-stop (stop_dac_after_us) ------------------

def test_generate_on_capture_sets_flag():
    bp, t = _bp()
    bp.generate("sine", freq=1000, amplitude=1.0, on_capture=True)
    assert t.commands[-1]["cmd"] == "generate" and t.commands[-1]["on_capture"] is True
    bp.generate("sine", freq=1000, amplitude=1.0)  # default off => absent
    assert "on_capture" not in t.commands[-1]


def test_replay_on_capture_threads_and_reads_cotrig():
    bp, t = _bp()
    h = bp.replay([0, 32768, 65535], are_codes=True, on_capture=True)
    assert t.uploads[-1]["replay"]["on_capture"] is True
    assert h.cotrig is True  # from the fake load_replay reply


class FakeCaptureTransport:
    def __init__(self) -> None:
        self.sent: List[dict] = []

    def stream_chunks(self, req: dict) -> Iterator[Dict[str, Any]]:
        self.sent.append(req)
        yield {"status": "ok", "data": [], "more": False}

    # scope_capture uses samples()/stream fallback via _stream_or_samples
    def command(self, req: dict) -> Any:
        self.sent.append(req)
        return {"data": []}


def test_capture_la_stop_dac_after_us():
    t = FakeCaptureTransport()
    cap_mod.capture_la(t, samples=8, sample_rate_mhz=1, stop_dac_after_us=250)
    assert t.sent[-1]["cmd"] == "la_capture" and t.sent[-1]["stop_dac_after_us"] == 250


def test_capture_analog_stop_dac_after_us():
    t = FakeCaptureTransport()
    caps = Capabilities.from_status({"adc_bits": 16, "adc_fullscale_mv": 4096})
    cap_mod.capture_analog(t, caps, adc_samples=4, la_samples=0, stop_dac_after_us=100)
    assert t.sent[-1]["cmd"] == "capture_dual" and t.sent[-1]["stop_dac_after_us"] == 100


def test_capture_stop_dac_absent_when_zero():
    t = FakeCaptureTransport()
    cap_mod.capture_la(t, samples=8, stop_dac_after_us=0)
    assert "stop_dac_after_us" not in t.sent[-1]


# -- capabilities flags ------------------------------------------------------

def test_capabilities_parse_new_flags():
    c = Capabilities.from_parameters({"cap.dac_control_loop": "true", "cap.dac_cotrig": "true"})
    assert c.dac_control_loop is True and c.dac_cotrig is True
    c2 = Capabilities.from_parameters({})
    assert c2.dac_control_loop is False and c2.dac_cotrig is False
