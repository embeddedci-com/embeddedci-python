"""generate() on firmware 3.9.1+ (Capabilities.generate16): the calibrated 16-bit form.

The pod gets volts (or microamps on the 4-20 mA output) and converts them with its own per-path
calibration, so the SDK sends the values, not 8-bit levels, and lets the pod route the output.
"""

from __future__ import annotations

import pytest

from embeddedci.benchpod.capabilities import Capabilities
from embeddedci.benchpod.client import BenchPod


class _Fake:
    def __init__(self, caps):
        self.caps = caps
        self.commands = []

    def status(self):
        return {"board": "stm32h563", "adc_bits": 16, "caps": self.caps}

    def command(self, req):
        self.commands.append(req)
        if req.get("cmd") == "current_out":
            return {"min_ua": 4055, "max_ua": 20094}
        if req.get("cmd") == "generate":
            return {"bits": 16} if "generate16" in self.caps else None
        return {}

    def close(self):
        pass


def _pod(monkeypatch, caps):
    monkeypatch.delenv("BENCHPOD_LA_VOLTAGE", raising=False)
    t = _Fake(caps)
    return BenchPod(transport=t, lease=False), t


def test_volts_are_sent_and_the_pod_routes(monkeypatch):
    bp, t = _pod(monkeypatch, ["analog", "dac", "generate16"])
    assert bp.capabilities.generate16
    h = bp.generate("sine", freq_hz=1000, amplitude=0.004, offset=1.65, dac_path="3v3")
    assert t.commands == [{"cmd": "generate", "waveform": "sine", "freq": 1000.0, "path": "3v3",
                           "amplitude_v": 0.004, "offset_v": 1.65}]
    assert h.data == {"bits": 16}


def test_route_false_and_on_capture(monkeypatch):
    bp, t = _pod(monkeypatch, ["generate16"])
    bp.generate("sawtooth", freq_hz=100, amplitude=0.05, offset=2.5, dac_path="5v", route=False,
                on_capture=True, duration=0.5)
    assert t.commands[-1] == {"cmd": "generate", "waveform": "sawtooth", "freq": 100.0, "path": "5v",
                              "amplitude_v": 0.05, "offset_v": 2.5, "duration_ms": 500,
                              "on_capture": True, "route": False}


def test_current_out_in_microamps(monkeypatch):
    bp, t = _pod(monkeypatch, ["generate16", "current_out"])
    bp.generate("square", freq_hz=10, amplitude=0.004, offset=0.012, dac_path="current_out")
    gen = [c for c in t.commands if c["cmd"] == "generate"][-1]
    assert gen["amplitude_ua"] == 4000 and gen["offset_ua"] == 12000 and "path" not in gen


def test_peaks_past_the_path_are_refused_not_clipped(monkeypatch):
    bp, t = _pod(monkeypatch, ["generate16"])
    with pytest.raises(ValueError, match="must stay within 0..3.3 V"):
        bp.generate("sine", freq_hz=10, amplitude=1.0, offset=3.0, dac_path="3v3")
    with pytest.raises(ValueError, match="one 16-bit step"):
        bp.generate("sine", freq_hz=10, amplitude=0, offset=1.0, dac_path="3v3")
    assert not [c for c in t.commands if c["cmd"] == "generate"]


def test_older_firmware_keeps_the_8bit_levels(monkeypatch):
    bp, t = _pod(monkeypatch, ["analog", "dac"])
    assert not bp.capabilities.generate16
    bp.generate("sine", freq_hz=10, amplitude=1.0, offset=2.5, dac_path="5v")
    gen = t.commands[-1]
    assert gen["amplitude"] == 51 and gen["offset"] == 128 and "amplitude_v" not in gen


def test_cloud_parameters():
    assert Capabilities.from_parameters({"cap.generate_bits": "16"}).generate16
    assert not Capabilities.from_parameters({"cap.generate_bits": "8"}).generate16
    assert not Capabilities.from_parameters({}).generate16
