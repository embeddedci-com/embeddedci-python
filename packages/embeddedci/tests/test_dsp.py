"""Unit tests for the record→replay DSP (mirror of the server's benchpod_recording.go)."""

from __future__ import annotations

import struct

import pytest

from embeddedci.benchpod import dsp


def test_dac_path_fullscale():
    assert dsp.dac_path_fullscale_v("5v") == 5.0
    assert dsp.dac_path_fullscale_v("3v3") == 3.3
    assert dsp.dac_path_fullscale_v("12v") == 12.0
    assert dsp.dac_path_fullscale_v("unknown", 7.0) == 7.0


def test_recording_roundtrip():
    volts = [0.0, 1.0, 2.0, 4.0]
    blob = dsp.encode_recording_volts(volts, 4.0)
    assert len(blob) == len(volts) * 2
    assert dsp.decode_recording_volts(blob, 4.0) == pytest.approx(volts, abs=1e-3)


def test_encode_clips_out_of_range():
    blob = dsp.encode_recording_volts([-1.0, 5.0], 4.0)  # below 0 and above full scale
    lo, hi = struct.unpack("<HH", blob)
    assert lo == 0 and hi == 65535


def test_block_average_downsample_shrinks_and_preserves_short():
    assert dsp.block_average_downsample([1, 2, 3], 10) == [1, 2, 3]
    down = dsp.block_average_downsample(list(range(8000)), 4096)
    assert len(down) <= 4096
    # mean is preserved by block averaging
    assert sum(down) / len(down) == pytest.approx(3999.5, rel=1e-3)


def test_window_volts():
    v = list(range(10))
    assert dsp.window_volts(v, 2, 3) == [2, 3, 4]
    assert dsp.window_volts(v, 8, 0) == [8, 9]
    assert dsp.window_volts(v, -5, 2) == [0, 1]


def test_volts_to_codes_faithful_8bit():
    codes = dsp.volts_to_codes([0.0, 2.5, 5.0], "faithful", 5.0, bits=8)
    assert codes == [0, 128, 255]


def test_volts_to_codes_faithful_16bit_clips():
    codes = dsp.volts_to_codes([-1.0, 0.0, 5.0, 6.0], "faithful", 5.0, bits=16)
    assert codes[0] == 0 and codes[1] == 0 and codes[2] == 65535 and codes[3] == 65535


def test_volts_to_codes_fit_autoscales_shape():
    codes = dsp.volts_to_codes([1.0, 1.5, 2.0], "fit", 5.0, bits=8)
    assert codes[0] == 0 and codes[-1] == 255  # min->0, max->max regardless of full scale


def test_volts_to_codes_fit_flat_is_midscale():
    assert dsp.volts_to_codes([1.0, 1.0, 1.0], "fit", 5.0, bits=8) == [128, 128, 128]
    assert dsp.volts_to_codes([1.0, 1.0], "fit", 5.0, bits=16) == [32768, 32768]


def test_codes_to_bytes_widths():
    assert dsp.codes_to_bytes([0, 255], 8) == b"\x00\xff"
    assert dsp.codes_to_bytes([0, 65535], 16) == b"\x00\x00\xff\xff"


@pytest.mark.parametrize("ftype,expect", [
    ("flatline", [0, 0, 0]),
    ("stuck", [255, 255, 255]),
    ("spike", [255, 255, 255]),
])
def test_apply_fault(ftype, expect):
    codes = list(range(10))
    dsp.apply_fault(codes, {"type": ftype, "start": 3, "width": 3}, bits=8)
    assert codes[3:6] == expect
    assert codes[:3] == [0, 1, 2] and codes[6:] == [6, 7, 8, 9]


def test_apply_fault_level_override():
    codes = [10] * 5
    dsp.apply_fault(codes, {"type": "flatline", "start": 0, "width": 5, "level": 42}, bits=8)
    assert codes == [42] * 5


def test_segments_ramp_hold_step():
    volts = dsp.segments_to_volts([
        {"shape": "hold", "duration_ms": 1, "v_start": 1.0},
        {"shape": "ramp", "duration_ms": 1, "v_start": 0.0, "v_end": 2.0},
        {"shape": "step", "duration_ms": 1, "v_start": 0.0, "v_end": 3.0},
    ], 1000.0)  # 1 ms @ 1kHz => 1 sample each
    assert volts == pytest.approx([1.0, 2.0, 3.0])


def test_recording_to_replay_codes_deep_keeps_depth():
    volts = [i / 100.0 for i in range(200)]
    blob = dsp.encode_recording_volts(volts, 5.0)
    rc = dsp.recording_to_replay_codes(blob, src_full_scale_v=5.0, dac_path="5v",
                                       mapping="faithful", bits=16, deep=True, max_samples=4096)
    assert len(rc) == 200  # deep keeps full depth when it fits PSRAM
    assert len(rc.to_bytes()) == 400


def test_recording_to_replay_codes_shallow_downsamples():
    volts = [i / 8000.0 for i in range(8000)]
    blob = dsp.encode_recording_volts(volts, 5.0)
    rc = dsp.recording_to_replay_codes(blob, src_full_scale_v=5.0, dac_path="5v",
                                       mapping="faithful", bits=8, deep=False, target_samples=4096)
    assert len(rc) <= 4096
    assert len(rc.to_bytes()) == len(rc)  # 8-bit => 1 byte/sample
