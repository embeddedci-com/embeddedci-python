"""Waveforms on the 4-20 mA output (``dac_path="current_out"``), and captures of the input in amps.

The SDK works in amps. The pod owns the range (its ``current_out`` reply), so the fake pod here
answers with the firmware's numbers: 4016 to 20078 uA, where 12 mA is 16-bit code 32576
(firmware host test ``test_current_out.c``).
"""

from __future__ import annotations

from typing import Any, List

import pytest

from embeddedci.benchpod import BenchPod, BenchPodError, Capture, Segment
from embeddedci.benchpod.waveforms import Waveform, WaveformLibrary

_RANGE = {"min_ua": 4016, "max_ua": 20078}
_CAL = {"source": "current_in", "calibrated": True, "offset_mv": 4, "offset_uv": 4356,
        "a_uv": 65828041, "b_nv": -1004471}


class _Pod:
    """A pod with the 4-20 mA output and its own current_in fit."""

    caps = ["current_out", "calibrate", "dac", "dac_replay"]

    def __init__(self) -> None:
        self.commands: List[dict] = []
        self.data = b""
        self.counts = [64543, 62560, 60577]      # 4, 12 and 20 mA with the _CAL fit

    def status(self) -> Any:
        return {"board": "stm32h563", "adc_bits": 16, "version": "3.5.0", "caps": self.caps}

    def command(self, req: dict) -> Any:
        self.commands.append(req)
        if req["cmd"] == "current_out":
            return dict(_RANGE) if "ua" not in req else {"ua": max(4016, req["ua"]), "code": 0, **_RANGE}
        if req["cmd"] == "calibrate":
            return _CAL
        return {}

    def samples(self, req: dict) -> List[int]:
        self.commands.append(req)
        return list(self.counts)

    def load_replay(self, *, data, replay, psram=False):
        self.commands.append(replay)
        self.data = data
        return {}

    def close(self) -> None:
        pass


def _bp() -> "tuple[BenchPod, _Pod]":
    t = _Pod()
    return BenchPod(transport=t, lease=False), t


def _cmds(t: _Pod) -> List[str]:
    return [c["cmd"] for c in t.commands]


def test_generate_takes_amps_and_switches_the_voltage_outputs_off():
    bp, t = _bp()
    handle = bp.generate("sine", freq_hz=2, amplitude=0.006, offset=0.012, dac_path="current_out")
    # The range comes from the pod, then the route (voltage outputs off), then the generator.
    assert _cmds(t) == ["current_out", "analog_path", "generate"]
    assert t.commands[0] == {"cmd": "current_out"}
    assert t.commands[1] == {"cmd": "analog_path", "path": "current_out"}
    # 8-bit levels on 4.016..20.016 mA (level 255 is the high byte of the top code).
    assert (t.commands[2]["amplitude"], t.commands[2]["offset"]) == (96, 127)
    assert handle.dac_path == "current_out"

    # Stop returns the loop to its live zero: dac_stop alone would leave it where it stopped.
    t.commands.clear()
    handle.stop()
    assert t.commands == [{"cmd": "dac_stop"}, {"cmd": "current_out", "ua": 4000}]


def test_generate_defaults_to_mid_range_and_refuses_what_does_not_fit():
    bp, t = _bp()
    bp.generate("square", freq_hz=1, amplitude=0.004, dac_path="current_out")
    assert t.commands[-1]["offset"] in (127, 128)                 # the middle of the range
    with pytest.raises(ValueError, match="0.004016..0.02002 A"):
        bp.generate("sine", freq_hz=1, amplitude=0.004, offset=0.002, dac_path="current_out")
    with pytest.raises(ValueError, match="at most 0.016 A"):
        bp.generate("sine", freq_hz=1, amplitude=12, dac_path="current_out")   # mA by mistake


def test_replay_takes_amps_and_maps_them_to_the_pods_codes():
    bp, t = _bp()
    handle = bp.replay([0.004, 0.012, 0.020078], dac_path="current_out", sample_rate_hz=1000)
    assert list(memoryview(t.data).cast("H")) == [0, 32576, 65535]   # 4 mA clips to the live zero
    assert {"cmd": "analog_path", "path": "current_out"} in t.commands
    assert "dac_out" not in _cmds(t)
    t.commands.clear()
    handle.stop()
    assert t.commands == [{"cmd": "dac_stop"}, {"cmd": "current_out", "ua": 4000}]


def test_capture_of_the_input_is_in_amps_with_the_pods_own_fit():
    bp, t = _bp()
    cap = bp.capture_adc(3, source="current_in", sample_rate_hz=100_000)
    assert _cmds(t) == ["analog_path", "calibrate", "capture"]
    assert cap.source == "current_in"
    assert cap.currents == pytest.approx([0.004, 0.012, 0.020], abs=5e-6)
    assert cap.volts == pytest.approx([0.996, 2.988, 4.98], abs=2e-3)
    # Every other source has no current.
    assert bp.capture_adc(3, source="ext").currents == []


def test_a_captured_loop_replays_as_the_same_current():
    bp, t = _bp()
    cap = bp.capture_adc(3, source="current_in", sample_rate_hz=100_000)
    bp.replay(cap, dac_path="current_out")
    codes = list(memoryview(t.data).cast("H"))
    assert codes[0] == 0 and abs(codes[1] - 32576) <= 20 and abs(codes[2] - 65217) <= 20
    # A voltage capture has no current to reproduce: only its shape, with fit.
    volts = Capture(counts=[1, 2], volts=[0.5, 2.5], sample_rate_hz=1000.0, source="ext")
    with pytest.raises(ValueError, match="mapping='fit'"):
        bp.replay(volts, dac_path="current_out")
    bp.replay(volts, dac_path="current_out", mapping="fit")
    assert list(memoryview(t.data).cast("H")) == [0, 65535]


def test_the_current_path_needs_the_capability():
    bp, t = _bp()
    t.caps = ["dac", "dac_replay"]                 # older firmware
    with pytest.raises(BenchPodError, match="current_out"):
        bp.generate("sine", freq_hz=1, amplitude=0.004, dac_path="current_out")
    with pytest.raises(BenchPodError, match="current_out"):
        bp.replay([0.012], dac_path="current_out")
    assert t.commands == []


class _Api:
    """Records what the waveform library sends to the server."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def request(self, method: str, path: str, **kw: Any):
        self.calls.append((method, path, kw))
        return 200, {"id": "w1", "name": "n", "kind": "segments"}


def test_library_segments_on_the_current_path_are_amps_stored_as_milliamps():
    api = _Api()
    WaveformLibrary(api).save_segments("ramp", dac_path="current_out", segments=[
        Segment("ramp", 2.0, 0.004, 0.020), Segment("hold", 1.0, 0.012)])
    body = api.calls[-1][2]["json_body"]
    assert body["dac_path"] == "current_out"
    assert [(s["v_start"], s["v_end"]) for s in body["segments"]] == [(4.0, 20.0), (12.0, 12.0)]
    # Voltage paths are untouched.
    WaveformLibrary(api).save_segments("v", dac_path="5v", segments=[Segment("hold", 1.0, 2.5)])
    assert api.calls[-1][2]["json_body"]["segments"][0]["v_start"] == 2.5


def test_library_recordings_carry_their_unit():
    assert Waveform.from_json({"id": "a", "name": "n", "kind": "recording", "unit": "mA"}).unit == "mA"
    assert Waveform.from_json({"id": "a", "name": "n", "kind": "recording"}).unit == "V"   # older server
    api = _Api()
    WaveformLibrary(api).save_recording("loop", b"\x00\x00\xff\xff", sample_rate_hz=1000, full_scale_v=25, unit="mA")
    assert "unit=mA" in api.calls[-1][1]
    WaveformLibrary(api).save_recording("v", b"\x00\x00\xff\xff", sample_rate_hz=1000, full_scale_v=5)
    assert "unit=" not in api.calls[-1][1]
    with pytest.raises(ValueError, match="unit"):
        WaveformLibrary(api).save_recording("x", b"\x00\x00", sample_rate_hz=1, full_scale_v=1, unit="A")
