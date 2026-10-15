"""A waveform played without ``dac_path`` goes to the 3.3 V output, never a fixed 5 V.

The DUT is wired to one DAC SMA, and most bench MCUs take 3.3 V at most: a call that leaves the
output out must not drive 5 V into one. With ``route=False`` nothing is switched and the path only
scales the volts, so it follows the output this connection last routed (``cal1`` is the 5 V DAC).
"""

from __future__ import annotations

import pytest

from embeddedci.benchpod.client import BenchPod
from embeddedci.benchpod.constants import DEFAULT_DAC_PATH


class _Fake:
    def __init__(self):
        self.commands = []

    def status(self):
        return {"board": "stm32h563", "adc_bits": 16}

    def command(self, req):
        self.commands.append(req)
        return {}

    def load_replay(self, *, data, replay, psram=False):
        self.replay = replay
        return {}

    def close(self):
        pass


@pytest.fixture
def pod(monkeypatch):
    monkeypatch.delenv("BENCHPOD_LA_VOLTAGE", raising=False)
    t = _Fake()
    return BenchPod(transport=t, lease=False), t


def test_default_is_3v3():
    assert DEFAULT_DAC_PATH == "3v3"


def test_generate_without_a_path_routes_3v3(pod):
    bp, t = pod
    h = bp.generate("sine", freq_hz=100, amplitude=1.0)
    assert h.dac_path == "3v3"
    assert t.commands[-2] == {"cmd": "dac_out", "path": "3v3"}
    # 1 V over 3.3 V / 255 levels; offset at mid-range 1.65 V.
    assert t.commands[-1]["amplitude"] == 77 and t.commands[-1]["offset"] == 127


def test_replay_without_a_path_routes_3v3(pod):
    bp, t = pod
    h = bp.replay([0.0, 1.0, 3.3])
    assert h.dac_path == "3v3"
    assert {"cmd": "dac_out", "path": "3v3"} in t.commands


def test_an_explicit_path_is_kept(pod):
    bp, t = pod
    assert bp.generate("sine", freq_hz=100, amplitude=1.0, dac_path="5v").dac_path == "5v"
    assert t.commands[-2] == {"cmd": "dac_out", "path": "5v"}


@pytest.mark.parametrize(("setup", "expected"), [
    (lambda bp: bp.analog_path("cal1"), "5v"),
    (lambda bp: bp.analog_path("cal2"), "12v"),
    (lambda bp: bp.dac_output("3v3"), "3v3"),
    (lambda bp: bp.generate("sine", freq_hz=10, amplitude=1.0, dac_path="12v"), "12v"),
    (lambda bp: (bp.analog_path("cal1"), bp.analog_path("adc_ext")), "5v"),  # ADC side only
    (lambda bp: (bp.dac_output("3v3"), bp.analog_path("off")), "5v"),  # parked: unknown again
    (lambda bp: None, "5v"),  # nothing routed: the cal1 loopback's scaling, as before
])
def test_route_false_scales_for_the_routed_output(pod, setup, expected):
    bp, t = pod
    setup(bp)
    n = len(t.commands)
    h = bp.generate("square", freq_hz=10, amplitude=1.0, route=False)
    assert h.dac_path == expected
    assert [c["cmd"] for c in t.commands[n:]] == ["generate"]  # nothing switched
