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
from embeddedci.benchpod.control_loop import ControlLoopHandle, IVPoint, normalise_loop_params


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


def test_normalise_loop_params_rejects_inverted_clamp():
    """vmin > vmax would pin the DAC at vmin in fabric (the gateware clamps against vmin first)
    and silently discard the ceiling — refuse it before it reaches a live bench."""
    with pytest.raises(ValueError, match="vmin"):
        normalise_loop_params(k=8192, vmin=50000, vmax=10000, tick_div=64)
    # vmin == vmax is legal: it is how a fixed output level is pinned.
    assert normalise_loop_params(k=8192, vmin=2000, vmax=2000, tick_div=64) == (8192, 2000, 2000, 64)


def test_normalise_loop_params_clamps_gain_and_tick():
    # The fabric uses k[14:0]: >32767 would wrap to a tiny gain, and k=0 freezes the output.
    assert normalise_loop_params(k=65535, vmin=0, vmax=65535, tick_div=64)[0] == 32767
    assert normalise_loop_params(k=0, vmin=0, vmax=65535, tick_div=64)[0] == 1
    # The pipelined tick needs >= 8 clk48 cycles.
    assert normalise_loop_params(k=8192, vmin=0, vmax=65535, tick_div=0)[3] == 8


def test_normalise_loop_params_rejects_out_of_range_codes():
    with pytest.raises(ValueError, match="vmax"):
        normalise_loop_params(k=8192, vmin=0, vmax=70000, tick_div=64)


def test_control_loop_rejects_inverted_clamp_before_sending():
    """The client must not even send an arm it knows the device will refuse."""
    bp, t = _bp({"dac_control_loop": {"armed": True}})
    with pytest.raises(ValueError):
        bp.control_loop(voc_code=52000, vmin=50000, vmax=10000)
    assert not any(c.get("cmd") == "dac_control_loop" for c in t.commands)


def test_control_loop_sends_the_clamped_gain():
    bp, t = _bp({"dac_control_loop": {"armed": True}})
    bp.control_loop(voc_code=52000, vmax=52000, k=65535, tick_div=1)
    assert t.commands[-1]["k"] == 32767 and t.commands[-1]["tick_div"] == 8


# -- selectable loop INPUT source (gateware >= v29) ---------------------------
# The open-loop sources are how the DAC/output stage is validated with the ADC out of the
# picture, so what matters here is that a request for one is either honoured exactly or
# refused — never quietly turned into the ADC-driven closed loop.

def test_normalise_loop_source_rejects_unknown_and_frozen_sweep():
    assert cl.normalise_loop_source(None, 0) is None      # unspecified -> device default
    assert cl.normalise_loop_source("fixed", 0) == "fixed"  # a held point needs no step
    assert cl.normalise_loop_source("sweep", 4) == "sweep"
    with pytest.raises(ValueError, match="unknown loop input source"):
        cl.normalise_loop_source("open", 1)
    with pytest.raises(ValueError, match="never advances"):
        cl.normalise_loop_source("sweep", 0)


def test_control_loop_sends_source_only_when_asked():
    bp, t = _bp({"dac_control_loop": {"armed": True, "source": "fixed", "input": 32768}})
    loop = bp.control_loop(curve=[1, 2, 3], source="fixed", input_code=32768)
    cmd = t.commands[-1]
    assert cmd["source"] == "fixed" and cmd["input"] == 32768 and cmd["step"] == 0
    assert loop.source == "fixed" and loop.input_code == 32768
    # Left out -> the fields are ABSENT, so a pod without selectable sources still arms as the
    # ADC-driven closed loop instead of being refused for an unknown field.
    bp.control_loop(curve=[1, 2, 3])
    assert "source" not in t.commands[-1] and "input" not in t.commands[-1]


def test_control_loop_rejects_a_bad_source_before_sending():
    bp, t = _bp({"dac_control_loop": {"armed": True}})
    before = len(t.commands)
    with pytest.raises(ValueError):
        bp.control_loop(curve=[1, 2], source="sweep", step=0)
    assert len(t.commands) == before   # nothing reached the device


def test_loop_input_steps_a_running_loop():
    bp, t = _bp({"dac_loop_input": {"source": "fixed", "input": 65535, "step": 0, "v": 123}})
    out = bp.loop_input(65535, source="fixed")
    assert t.commands[-1] == {"cmd": "dac_loop_input", "input": 65535, "source": "fixed"}
    assert out["input"] == 65535
    # An input-only step keeps every other field at its device-side value.
    bp.loop_input(0)
    assert t.commands[-1] == {"cmd": "dac_loop_input", "input": 0}


def test_handle_set_input_updates_its_view():
    bp, t = _bp({"dac_control_loop": {"armed": True, "source": "fixed", "input": 0},
                 "dac_loop_input": {"source": "fixed", "input": 32768, "step": 0, "v": 9}})
    loop = bp.control_loop(curve=[1, 2], source="fixed", input_code=0)
    loop.set_input(32768)
    assert loop.input_code == 32768 and loop.source == "fixed"
    assert t.commands[-1]["cmd"] == "dac_loop_input"


def test_loop_probe_reports_the_loops_own_input():
    # In a fixed/sweep run `i` is an ADC reading nothing is using; `in` is what the loop indexed
    # the curve with. loop_input must prefer it — that is the value a test asserts against.
    bp, _ = _bp({"dac_loop_probe": {"i": 64000, "in": 32768, "source": "fixed", "v": 20000}})
    pt = bp.loop_probe()
    assert pt.loop_input == 32768 and pt.source == "fixed" and pt.i == 64000
    # Older firmware omits both: there the ADC IS the input by construction.
    bp2, _ = _bp({"dac_loop_probe": {"i": 4321, "v": 51000}})
    pt2 = bp2.loop_probe()
    assert pt2.input_code is None and pt2.loop_input == 4321


def test_curve_output_at_matches_the_firmware_and_gateware_index():
    # The number the operator meters against: firmware upsample (LUT entry j <- point
    # j*N//2048) then gateware LUT[input >> 5]. A curve of all-distinct points makes a wrong
    # index visible.
    curve = [1000 + i * 29 for i in range(cl.CURVE_POINTS)]
    for code in (0, 1, 31, 32, 1000, 30000, 32768, 65535):
        want = curve[min(len(curve) - 1, ((code >> 5) * len(curve)) // cl.CURVE_LUT_ENTRIES)]
        assert cl.curve_output_at(curve, code) == want
    assert cl.curve_output_at(curve, -5) == curve[0]
    assert cl.curve_output_at(curve, 10**9) == curve[-1]
    assert cl.curve_output_at([], 5) == 0
    # The panel preset's metered endpoints: 0% input -> Voc, 100% -> 0.
    panel = cl.build_panel_curve(52428, 6)
    assert cl.curve_output_at(panel, cl.input_percent_to_code(0)) == 52428
    assert cl.curve_output_at(panel, cl.input_percent_to_code(100)) == 0


def test_input_percent_to_code_clamps():
    assert cl.input_percent_to_code(0) == 0
    assert cl.input_percent_to_code(100) == cl.CODE_MAX
    assert cl.input_percent_to_code(50) == round(0.5 * cl.CODE_MAX)
    assert cl.input_percent_to_code(-1) == 0 and cl.input_percent_to_code(1000) == cl.CODE_MAX


def test_constant_and_linear_curves():
    flat = cl.build_constant_curve(30000, points=4)
    assert flat == [30000] * 4
    up = cl.build_linear_curve(40000, rising=True, points=5)
    assert up[0] == 0 and up[-1] == 40000 and up == sorted(up)
    down = cl.build_linear_curve(40000, rising=False, points=5)
    assert down[0] == 40000 and down[-1] == 0
    assert cl.build_constant_curve(99999, points=2) == [cl.CODE_MAX] * 2


def test_capabilities_parse_loop_sources():
    c = Capabilities.from_parameters({"cap.dac_control_loop": "true",
                                      "cap.dac_loop_sources": "true"})
    assert c.dac_loop_sources is True
    # Absent on a pre-v29 pod -> False, so a caller offers/uses the ADC source only.
    assert Capabilities.from_parameters({"cap.dac_control_loop": "true"}).dac_loop_sources is False
