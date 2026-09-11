"""DAC output paths map volts over their real range: 3v3/5v are unipolar, 12v is bipolar ±12 V.

Reference: firmware ``cal_data.h`` ``DAC_CAL`` (12V: −12..+12 V, 0 V at code 128), confirmed on a
pod where ``dac_out 12v 0 V`` answers code 128.
"""

from __future__ import annotations

import pytest

from embeddedci.benchpod import dsp
from embeddedci.benchpod.client import BenchPod


def test_path_ranges():
    assert dsp.dac_path_range_v("3v3") == (0.0, 3.3)
    assert dsp.dac_path_range_v("5v") == (0.0, 5.0)
    assert dsp.dac_path_range_v("12v") == (-12.0, 12.0)


def test_12v_faithful_mapping_is_bipolar():
    lo, hi = dsp.dac_path_range_v("12v")
    assert dsp.volts_to_codes([-12.0, 0.0, 12.0], dsp.FAITHFUL, hi, bits=8, path_min_v=lo) == [0, 128, 255]
    assert dsp.volts_to_codes([0.0], dsp.FAITHFUL, hi, bits=16, path_min_v=lo) == [32768]
    assert dsp.volts_to_codes([-20.0, 20.0], dsp.FAITHFUL, hi, bits=8, path_min_v=lo) == [0, 255]


def test_unipolar_paths_unchanged():
    assert dsp.volts_to_codes([0.0, 2.5, 5.0], dsp.FAITHFUL, 5.0, bits=8) == [0, 128, 255]


def test_recording_replay_on_12v_centres_zero():
    blob = dsp.encode_recording_volts([0.0, 0.0], 5.0)
    rc = dsp.recording_to_replay_codes(blob, src_full_scale_v=5.0, dac_path="12v", bits=16)
    assert rc.codes == [32768, 32768]


class _Fake:
    def __init__(self):
        self.commands = []

    def status(self):
        return {"board": "stm32h563", "adc_bits": 16}

    def command(self, req):
        self.commands.append(req)
        return {}

    def load_replay(self, *, data, replay, psram=False):
        self.data = data
        return {}

    def close(self):
        pass


def test_generate_on_12v_defaults_to_0_volts(monkeypatch):
    monkeypatch.delenv("BENCHPOD_LA_VOLTAGE", raising=False)
    t = _Fake()
    bp = BenchPod(transport=t, lease=False)
    bp.generate("square", freq_hz=10, amplitude=1.0, dac_path="12v")
    gen = t.commands[-1]
    # 24 V over 255 levels: 1 V peak -> 11 levels, default offset 0 V -> mid-scale 128.
    assert gen["amplitude"] == 11 and gen["offset"] == 128
    bp.generate("square", freq_hz=10, amplitude=1.0, offset=-6.0, dac_path="12v")
    assert t.commands[-1]["offset"] == round(6.0 / (24 / 255))
    with pytest.raises(ValueError, match="-12..12"):
        bp.generate("sine", freq_hz=10, amplitude=1.0, offset=13.0, dac_path="12v")


def test_replay_volts_on_12v_centres_zero(monkeypatch):
    monkeypatch.delenv("BENCHPOD_LA_VOLTAGE", raising=False)
    t = _Fake()
    bp = BenchPod(transport=t, lease=False)
    bp.replay([0.0, -12.0, 12.0], dac_path="12v")
    assert list(memoryview(t.data).cast("H")) == [32768, 0, 65535]
