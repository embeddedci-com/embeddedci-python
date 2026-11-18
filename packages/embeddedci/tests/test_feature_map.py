"""The features table (feature_map.json, benchpod-firmware's features/features.json): what a pod
that sends features_rev can do, and that it matches what the caps list said before."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import feature_map
from embeddedci.benchpod.capabilities import Capabilities
from embeddedci.benchpod.client import BenchPod

# A 3.10.10 analog pod on the loop image and gateware 56, as its plain status lists it
# (cloud_caps_emit_list).
LEGACY_LIST = [
    "signal", "gpio", "power", "swd", "uart", "la", "analyzer", "command", "tunnel", "ota",
    "la_pins", "power_profile", "capture_b64", "can", "can_fd", "sensors", "sensor_la_trigger",
    "usb_net", "usb_net_setting", "lan_tls", "wifi_scan", "pod_current", "adc_low_range", "analog",
    "scope", "dac_limits", "calibrate", "cal_dmm", "current_out", "dac", "dac_dc", "dac_replay",
    "dac_control_loop", "dac_cotrig", "generate16", "dac_loop_sources", "dac_loop_input_map",
    "gpio_read", "capture_trigger", "spi_master", "spi_stream", "uart_frame", "nrst_pin", "usb_cc",
]
TOP = {"version": "3.10.10", "board": "stm32h563", "analog": True, "nrst_pin": True,
       "gateware": 56, "sensors_digital": True, "ota_sig": True, "sig_policy_cmd": True,
       "lan_policy_cmd": True, "tunnel_max_tier": True, "lease_state": True, "cloud_ca": True,
       "cloud_proxy": True}
NEW_FACTS = {"features_rev": 1, "fpga_image": "loop",
             "hw": ["wifi", "usb_cc", "pod_current", "adc_low_range"]}

# Flags the old list never carried (frame only, or not at all) that the table now gives.
TABLE_ONLY = {"blob_slots", "ws_auth_v2"}


def _flags(c: Capabilities) -> Dict[str, bool]:
    return {k: v for k, v in vars(c).items() if isinstance(v, bool)}


def test_table_matches_the_old_list():
    old = Capabilities.from_status({**TOP, "caps": LEGACY_LIST})
    new = Capabilities.from_status({**TOP, **NEW_FACTS})
    assert old.features_rev == 0 and new.features_rev == 1
    lost = sorted(k for k, v in _flags(old).items() if v and not getattr(new, k))
    gained = sorted(k for k, v in _flags(new).items() if v and not getattr(old, k))
    assert lost == [], f"the table lost {lost}"
    assert set(gained) <= TABLE_ONLY, f"the table gave more than expected: {gained}"
    assert new.fpga_image == "loop"
    assert {"la_dir", "i2c_master", "diag", "wiring_store", "error_codes"} <= new.features


def test_image_and_gateware_decide():
    deep = Capabilities.from_status({**TOP, **NEW_FACTS, "fpga_image": "deep"})
    assert deep.dac_deep_replay and not deep.dac_control_loop
    old_gw = Capabilities.from_status({**TOP, **NEW_FACTS, "gateware": 55})
    assert not old_gw.uart_frame and old_gw.spi_master and old_gw.bus_master
    hd = Capabilities.from_status({**TOP, **NEW_FACTS, "gateware": 57})
    assert hd.uart_hd and not Capabilities.from_status({**TOP, **NEW_FACTS}).uart_hd
    unknown = Capabilities.from_status({**TOP, **NEW_FACTS, "gateware": 0})
    assert not unknown.gpio_read and not unknown.spi_master and unknown.can_fd


def test_digital_board():
    c = Capabilities.from_status({**TOP, **NEW_FACTS, "analog": False, "nrst_pin": False,
                                  "hw": [], "sensors_digital": False})
    assert c.analog is False
    assert not (c.dac or c.scope or c.calibrate or c.current_out)
    assert not c.usb_cc and not c.pod_current and not c.adc_low_range and not c.sensors_digital
    assert "wifi_scan" not in c.features and "gpio_read" in c.features


def test_rev_above_the_pod(monkeypatch):
    table = json.loads(json.dumps(feature_map.feature_map()))
    table["features"]["future_thing"] = {"rev": 2, "firmware": "next", "board": "any"}
    table["revision"] = 2
    monkeypatch.setattr(feature_map, "feature_map", lambda: table)
    assert "future_thing" not in feature_map.resolve(1, 56, {"analog": True})
    assert "future_thing" in feature_map.resolve(2, 56, {"analog": True})


def test_has_names_without_a_flag():
    new = Capabilities.from_status({**TOP, **NEW_FACTS, "version": "3.10.10"})
    assert new.has("la_dir") and new.has("diag") and new.has("can_fd") and not new.has("nope")
    old = Capabilities.from_status({**TOP, "version": "3.10.3", "gateware": 54, "caps": ["la"]})
    assert not old.has("la_dir") and not old.has("i2c_master")
    assert Capabilities.from_status({**TOP, "version": "3.10.4", "caps": []}).has("la_dir")


def test_first_firmware():
    assert feature_map.first_firmware("uart_frame") == "3.10.8"
    assert feature_map.first_firmware("no_such_feature") == ""


class _Pod:
    """A LAN pod: answers status with or without the caps list."""

    def __init__(self, new_firmware: bool) -> None:
        self.new = new_firmware
        self.requests: List[dict] = []
        self.timeout = 30.0

    def status(self) -> Dict[str, Any]:
        return self.command({"cmd": "status"})

    def status_without_caps(self) -> Dict[str, Any]:
        return self.command({"cmd": "status", "caps": False})

    def command(self, req: dict) -> Any:
        self.requests.append(req)
        if req["cmd"] != "status":
            return {}
        if self.new and req.get("caps") is False:
            return {**TOP, **NEW_FACTS}
        return {**TOP, "caps": LEGACY_LIST}   # old firmware ignores "caps"

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass


@pytest.mark.parametrize("new_firmware", [True, False])
def test_client_asks_without_the_list(new_firmware):
    pod = _Pod(new_firmware)
    bp = BenchPod(transport=pod, lease=False, api_key="", api_base="")
    assert bp.supports("uart_frame") and bp.supports("spi_master")
    assert {"cmd": "status", "caps": False} in pod.requests
    assert bp.capabilities.features_rev == (1 if new_firmware else 0)
    assert bp.supports("la_dir")


def test_bundled_copy_matches_the_firmware():
    """The bundled feature_map.json is benchpod-firmware's features/features.json, byte for byte
    (scripts/sync-feature-map.sh), when that checkout is next to this repository."""
    fw = os.environ.get("BENCHPOD_FIRMWARE_DIR") or str(Path(__file__).resolve().parents[3] / ".." / "benchpod-firmware")
    src = Path(fw) / "features" / "features.json"
    if not src.exists():
        pytest.skip(f"no {src}")
    bundled = Path(feature_map.__file__).with_name("feature_map.json")
    assert bundled.read_bytes() == src.read_bytes(), "run scripts/sync-feature-map.sh"
