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


def test_rev3_board_features_from_status():
    v3 = Capabilities.from_status({"board": "stm32h563", "board_rev": "v3", "nrst_pin": True,
                                   "caps": ["la", "nrst_pin", "usb_cc"]})
    assert v3.board_rev == "v3" and v3.nrst_pin and v3.usb_cc
    v2 = Capabilities.from_status({"board": "stm32h563", "board_rev": "v2", "nrst_pin": False,
                                   "caps": ["la"]})
    assert v2.board_rev == "v2" and not v2.nrst_pin and not v2.usb_cc


def test_rev3_board_features_from_the_text_console_status():
    # the USB text console has no caps[] list, only top-level booleans
    caps = Capabilities.from_status({"board": "stm32h563", "board_rev": "v3",
                                     "nrst_pin": True, "usb_cc": True})
    assert caps.nrst_pin and caps.usb_cc


def test_boot_health_from_status():
    c = Capabilities.from_status({"board": "stm32h563", "reset": "software",
                                  "last_crash": "HardFault pc=0x1 task=net", "safe_mode": True,
                                  "safe_reason": '2 failed boots in a row, the last in "network"; network off'})
    assert c.safe_mode and c.reset_cause == "software" and c.last_crash.startswith("HardFault")
    assert "safe mode: 2 failed boots" in c.boot_warning() and "unplug and replug" in c.boot_warning()

    clean = Capabilities.from_status({"reset": "power-on", "last_crash": "none", "safe_mode": False})
    assert clean.last_crash == "" and not clean.safe_mode and clean.boot_warning() is None
    # Older firmware reports none of it.
    assert Capabilities.from_status({"board": "stm32h563"}).boot_warning() is None


def test_boot_health_from_parameters():
    c = Capabilities.from_parameters({"cap.safe_mode": "false", "cap.safe_reason": "",
                                      "cap.last_crash": "assert pc=0x0 task=hw", "cap.reset_cause": "software"})
    assert not c.safe_mode and c.last_crash == "assert pc=0x0 task=hw"
    assert c.boot_warning().startswith("the pod crashed and restarted itself")
    assert Capabilities.from_parameters({"cap.safe_mode": "true"}).boot_warning().startswith(
        "the pod is in safe mode. Some features are off")


# -- the policy / update / hardening flags (firmware 3.6 status, server cap.*) ------------------

_STATUS_3_6 = {
    "version": "3.6.0", "board": "stm32h563", "flash_kb": 2048,
    "ota_sig": True, "sig_policy": "audit", "sig_keys": 2, "sig_policy_cmd": True,
    "lan_policy": "locked", "lan_policy_cmd": True, "tunnel_max_tier": True, "lease_state": True,
    "cloud_ca": True, "cloud_proxy": True,
    "lease": {"held": False, "holder": "", "left_s": 0},
    "analog": True,
    "caps": ["signal", "la", "analog", "scope", "dac_limits", "calibrate", "current_out"],
}


def test_status_policy_and_update_flags():
    c = Capabilities.from_status(_STATUS_3_6)
    assert c.flash_kb == 2048 and c.ota_sig and c.sig_policy == "audit" and c.sig_keys == 2
    assert c.sig_policy_cmd and c.lan_policy == "locked" and c.lan_policy_cmd
    assert c.tunnel_max_tier and c.lease_state and c.cloud_ca and c.cloud_proxy
    assert c.analog is True and c.dac_limits


def test_status_of_a_digital_only_board():
    c = Capabilities.from_status({"analog": False, "caps": ["la"]})
    assert c.analog is False and not c.dac_limits


def test_older_firmware_leaves_the_new_flags_unknown():
    c = Capabilities.from_status({"version": "3.1.1", "caps": ["la", "scope"]})
    assert c.analog is None and not c.ota_sig and c.sig_policy == "" and c.flash_kb == 0
    assert not c.lease_state and not c.tunnel_max_tier


def test_server_parameters_policy_and_update_flags():
    c = Capabilities.from_parameters({
        "cap.analog": "true", "cap.dac_limits": "true", "cap.flash_kb": "1024",
        "cap.blob_slots": "true", "cap.ota_sig": "true", "cap.sig_policy": "required",
        "cap.sig_policy_cmd": "true", "cap.lan_policy": "open", "cap.lan_policy_cmd": "true",
        "cap.tunnel_max_tier": "true", "cap.ws_auth_v2": "true", "cap.lease_state": "true",
        "cap.cloud_ca": "true", "cap.cloud_proxy": "true", "cap.pod_current": "true",
    })
    assert c.analog is True and c.dac_limits and c.flash_kb == 1024 and c.blob_slots
    assert c.ota_sig and c.sig_policy == "required" and c.sig_policy_cmd
    assert c.lan_policy == "open" and c.lan_policy_cmd and c.tunnel_max_tier
    assert c.ws_auth_v2 and c.lease_state and c.cloud_ca and c.cloud_proxy and c.pod_current


def test_server_analog_is_tri_state():
    assert Capabilities.from_parameters({"cap.analog": ""}).analog is None
    assert Capabilities.from_parameters({"cap.analog": "false"}).analog is False
    # merge keeps the status's answer when the server has none
    merged = Capabilities.from_status({"analog": False}).merge(Capabilities.from_parameters({}))
    assert merged.analog is False
