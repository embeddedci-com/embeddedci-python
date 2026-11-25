"""dac_output / generate path ``cal1`` (firmware feature ``dac_out_cal1``, features rev 20)."""
from __future__ import annotations

import pytest

from embeddedci.benchpod import BenchPod, Wiring
from embeddedci.benchpod.constants import DAC_OUTPUT_PATHS, DAC_PATHS
from embeddedci.benchpod.errors import UnsupportedFeatureError

from test_client_v2 import FakeTransport


class PodTransport(FakeTransport):
    def __init__(self, features_rev: int, version: str) -> None:
        super().__init__({"dac_out": lambda r: {"path": r["path"], "mv": 2000, "uv": 2000000, "code16": 26214}
                          if "v" in r else {"path": r["path"]},
                          "generate": {"status": "ok"}})
        self._features_rev = features_rev
        self._version = version

    def status(self):
        return {**super().status(), "api": 2, "min_client_api": 2, "version": self._version,
                "features_rev": self._features_rev}


def _bp(rev: int, version: str):
    t = PodTransport(rev, version)
    return BenchPod(transport=t, lease=False, wiring=Wiring.preset()), t


def test_cal1_is_a_known_path():
    assert "cal1" in DAC_OUTPUT_PATHS and "cal1" in DAC_PATHS


def test_dac_output_cal1_on_new_firmware():
    bp, t = _bp(20, "4.0.6")
    out = bp.dac_output("cal1", volts=2.0)
    assert t.commands[-1] == {"cmd": "dac_out", "path": "cal1", "v": 2.0}
    assert out.path == "cal1" and out.voltage == pytest.approx(2.0, abs=1e-3)


def test_dac_output_cal1_refused_on_old_firmware():
    bp, t = _bp(19, "4.0.5")
    with pytest.raises(UnsupportedFeatureError, match="cal1") as info:
        bp.dac_output("cal1", volts=2.0)
    assert info.value.feature == "dac_out_cal1"
    assert not any(c.get("path") == "cal1" for c in t.commands)  # nothing was sent


def test_generate_cal1_gate_and_command():
    bp, t = _bp(20, "4.0.6")
    bp.generate("sine", freq_hz=100, amplitude=1.0, offset=2.5, dac_path="cal1")
    req = t.commands[-1]
    assert req["path"] == "cal1" and req["amplitude_v"] == 1.0
    old, t2 = _bp(19, "4.0.5")
    with pytest.raises(UnsupportedFeatureError):
        old.generate("sine", freq_hz=100, amplitude=1.0, dac_path="cal1")
    assert not any(c.get("path") == "cal1" for c in t2.commands)


def test_generate_cal1_uses_the_5v_window():
    bp, _ = _bp(20, "4.0.6")
    with pytest.raises(ValueError):
        bp.generate("sine", freq_hz=100, amplitude=4.0, offset=2.5, dac_path="cal1")  # peaks at 6.5 V


def test_replay_refuses_cal1():
    bp, t = _bp(20, "4.0.6")
    with pytest.raises(ValueError, match="cal1"):
        bp.replay([0.1, 0.2], dac_path="cal1")


def test_unrouted_default_follows_cal1_as_5v():
    bp, _ = _bp(20, "4.0.6")
    bp.dac_output("cal1", volts=1.0)
    assert bp._default_dac_path(False) == "5v"
