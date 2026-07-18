"""Replay tests against a fake transport (no hardware): direct replay, faults, concurrency."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import dsp
from embeddedci.benchpod.client import BenchPod
from embeddedci.benchpod.replay import Fault, ReplayHandle, Segment
from embeddedci.benchpod.results import Capture


class FakeTransport:
    """Records commands + load_replay uploads; serves a v2-looking status."""

    def __init__(self) -> None:
        self.commands: List[dict] = []
        self.uploads: List[dict] = []

    def status(self):
        return {"board": "stm32h563", "adc_bits": 16, "adc_fullscale_mv": 4096}

    def command(self, req: dict) -> Any:
        self.commands.append(req)
        return {}

    def load_replay(self, *, data: bytes, replay: dict, psram: bool = False):
        self.uploads.append({"len": len(data), "replay": replay, "psram": psram, "data": data})
        return {"samples": replay.get("samples")}

    def close(self):
        pass


def _bp() -> "tuple[BenchPod, FakeTransport]":
    t = FakeTransport()
    return BenchPod(transport=t, lease=False), t


def test_replay_bits_v2_is_16():
    bp, _ = _bp()
    assert bp._replay_bits() == 16


def test_replay_capture_maps_volts_and_routes_path():
    bp, t = _bp()
    cap = Capture(counts=[], volts=[0.0, 2.5, 5.0], sample_rate_hz=1000.0)
    handle = bp.replay(cap, dac_path="5v", sample_rate_mhz=0.4)
    assert isinstance(handle, ReplayHandle)
    # routed the DAC path first
    assert {"cmd": "dac_out", "path": "5v"} in t.commands
    up = t.uploads[-1]
    # 3 samples, 16-bit => 6 bytes; not deep (<=2048)
    assert up["len"] == 6 and up["psram"] is False
    assert up["replay"]["samples"] == 3
    codes = list(memoryview(up["data"]).cast("H"))
    assert codes[0] == 0 and codes[2] == 65535


def test_replay_codes_direct():
    bp, t = _bp()
    bp.replay([0, 32768, 65535], dac_path="5v", are_codes=True)
    codes = list(memoryview(t.uploads[-1]["data"]).cast("H"))
    assert codes == [0, 32768, 65535]


def test_replay_applies_fault():
    bp, t = _bp()
    volts = [1.0] * 10
    bp.replay(volts, dac_path="5v", fault=Fault("flatline", start=0, width=10, level=0))
    codes = list(memoryview(t.uploads[-1]["data"]).cast("H"))
    assert codes == [0] * 10


def test_replay_deep_for_large_waveforms():
    bp, t = _bp()
    bp.replay([100] * 5000, dac_path="5v", are_codes=True)
    assert t.uploads[-1]["psram"] is True  # >2048 samples -> stream from PSRAM (deep)


def test_replay_handle_context_manager_stops():
    bp, t = _bp()
    with bp.replay([1.0, 2.0], dac_path="5v"):
        pass
    assert {"cmd": "dac_stop"} in t.commands


def test_replaying_while_capturing_pattern():
    bp, t = _bp()
    # arm a looping replay, run a (fake) capture alongside, then stop — the v18 concurrency story.
    handle = bp.replaying([100] * 4000, dac_path="5v", are_codes=True)
    assert t.uploads[-1]["psram"] is True
    handle.stop()
    assert {"cmd": "dac_stop"} in t.commands


def test_segment_and_fault_helpers_to_dict():
    assert Segment("ramp", 5, 0.0, 1.0).to_dict() == {
        "shape": "ramp", "duration_ms": 5.0, "v_start": 0.0, "v_end": 1.0}
    assert Fault("spike", 1, 2, 255).to_dict() == {
        "type": "spike", "start": 1, "width": 2, "level": 255}
