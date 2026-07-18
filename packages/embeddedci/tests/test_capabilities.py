"""Unit tests for capability parsing + ADC scaling."""

from __future__ import annotations

import pytest

from embeddedci.benchpod.capabilities import ADC_AFFINE_DEFAULTS, Capabilities


def test_from_status_naive_linear_v1():
    caps = Capabilities.from_status({"board": "pod_b", "adc_bits": 8, "adc_fullscale_mv": 3300})
    assert caps.adc_affine is None
    # naive linear: mid code -> ~half full scale
    assert caps.counts_to_volts(0) == pytest.approx(0.0)
    assert caps.counts_to_volts(255) == pytest.approx(3.3, rel=1e-6)


def test_from_status_v2_board_uses_affine_default():
    caps = Capabilities.from_status({"board": "stm32h563", "adc_bits": 16, "adc_fullscale_mv": 4096})
    assert caps.adc_affine is not None
    assert caps.adc_affine == ADC_AFFINE_DEFAULTS["stm32h563"]
    # affine model differs from the naive full-scale model
    naive = (61446 / 65535) * 4.096
    assert abs(caps.counts_to_volts(61446) - naive) > 0.1


def test_affine_unwrap_for_low_counts():
    caps = Capabilities.from_status({"board": "stm32h563", "adc_bits": 16})
    # a low raw count is unwrapped (+65536) before the affine fit -> a negative-ish reading
    assert caps.counts_to_volts(100) < caps.counts_to_volts(60000)


def test_from_parameters_full_set():
    params = {
        "cap.board": "stm32h563", "cap.adc_bits": "16", "cap.adc_fullscale_mv": "4096",
        "cap.adc_cal_a": "65.7889", "cap.adc_cal_b": "-0.001003762", "cap.adc_cal_unwrap": "true",
        "cap.dac_replay": "true", "cap.dac_deep_replay": "true",
        "cap.dac_replay_bits": "16", "cap.dac_replay_max_samples": "2097152",
        "cap.scope": "true", "cap.analyzer": "true",
    }
    caps = Capabilities.from_parameters(params)
    assert caps.dac_replay and caps.dac_deep_replay
    assert caps.dac_replay_bits == 16 and caps.dac_replay_max_samples == 2097152
    assert caps.scope and caps.analyzer
    assert caps.adc_affine is not None and caps.adc_affine.b == pytest.approx(-0.001003762)


def test_from_parameters_integer_uv_nv_cal():
    # the firmware ships cal as integer microvolts / nanovolts-per-count
    params = {"adc_cal_a_uv": "65788900", "adc_cal_b_nv": "-1003762", "adc_cal_unwrap": "true",
              "cap.adc_bits": "16"}
    caps = Capabilities.from_parameters(params)
    assert caps.adc_affine is not None
    assert caps.adc_affine.a == pytest.approx(65.7889, rel=1e-4)
    assert caps.adc_affine.b == pytest.approx(-0.001003762, rel=1e-4)


def test_merge_prefers_server_params():
    status = Capabilities.from_status({"board": "stm32h563", "adc_bits": 16})
    server = Capabilities.from_parameters({"cap.dac_replay_max_samples": "2097152",
                                           "cap.dac_replay_bits": "16", "cap.dac_deep_replay": "true"})
    merged = status.merge(server)
    assert merged.dac_replay_max_samples == 2097152
    assert merged.dac_deep_replay is True
    assert merged.board == "stm32h563"  # kept from status (server had none)
