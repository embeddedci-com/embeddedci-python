"""The features table (feature_map.json, benchpod-firmware's features/features.json): what a pod
that sends features_rev can do."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import feature_map
from embeddedci.benchpod.capabilities import Capabilities
from embeddedci.benchpod.client import BenchPod

# A firmware 4.0 analog pod on the loop image and gateware 56.
TOP = {"version": "4.0.0", "board": "stm32h563", "analog": True, "nrst_pin": True,
       "gateware": 56, "sensors_digital": True, "api": 2, "min_client_api": 2}
NEW_FACTS = {"features_rev": 10, "fpga_image": "loop",
             "hw": ["wifi", "usb_cc", "pod_current", "adc_low_range"]}


def test_the_table_gives_the_flags():
    c = Capabilities.from_status({**TOP, **NEW_FACTS})
    assert c.features_rev == 10 and c.fpga_image == "loop"
    assert c.scope and c.dac and c.dac_control_loop and c.serial and c.analyzer and c.ota
    assert c.capture_b64 and c.gpio_read and c.spi_master and c.uart_frame and c.can_fd
    assert c.usb_cc and c.pod_current and c.adc_low_range and c.nrst_pin and c.sensors_digital
    assert {"la_dir", "i2c_master", "diag", "wiring_store", "error_codes", "json_names_4",
            "generate_volts_only", "can_start_stop", "la_step_pull_split"} <= c.features


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
    new = Capabilities.from_status({**TOP, **NEW_FACTS})
    assert new.has("la_dir") and new.has("diag") and new.has("can_fd") and not new.has("nope")
    assert new.has("serial") and new.has("uart")
    # A status without the table (firmware 3.x, which the SDK refuses) gives nothing.
    old = Capabilities.from_status({"version": "3.10.4", "gateware": 54, "caps": ["la"]})
    assert not old.has("la_dir") and not old.has("i2c_master") and not old.has("la")


def test_first_firmware():
    assert feature_map.first_firmware("uart_frame") == "3.10.8"
    assert feature_map.first_firmware("no_such_feature") == ""


class _Pod:
    """A LAN pod with firmware 4.0."""

    def __init__(self) -> None:
        self.requests: List[dict] = []
        self.timeout = 30.0

    def status(self) -> Dict[str, Any]:
        return self.command({"cmd": "status"})

    def command(self, req: dict) -> Any:
        self.requests.append(req)
        return {**TOP, **NEW_FACTS} if req["cmd"] == "status" else {}

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass


def test_client_reads_the_table():
    pod = _Pod()
    bp = BenchPod(transport=pod, lease=False, api_key="", api_base="")
    assert bp.supports("uart_frame") and bp.supports("spi_master")
    assert {"cmd": "status"} in pod.requests and not [r for r in pod.requests if "caps" in r]
    assert bp.capabilities.features_rev == 10
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
