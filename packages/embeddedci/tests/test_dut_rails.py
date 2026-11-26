"""J1 pins 17 and 18 on the digital board v4 (feature ``dut_rails``): ``j1_power`` sends
``target_power`` with ``rail``, ``target_status`` reports ``j1_rails``, older boards are refused
before anything is sent."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import BenchPod, J1Rails


class Pod:
    def __init__(self, rails: bool, features_rev: int = 22) -> None:
        self.rails = rails
        self.features_rev = features_rev
        self.sent: List[Dict[str, Any]] = []
        self.on = {"j1_3v3": True, "j1_1v8": True}

    def status(self) -> Dict[str, Any]:
        return {"board": "stm32h563", "adc_bits": 16, "version": "4.0.7", "features_rev": self.features_rev,
                "gateware": 68, "fpga_image": "deep", "hw": ["nrst_pin"] + (["dut_rails"] if self.rails else []),
                "board_rev": "v4" if self.rails else "v3"}

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass

    def command(self, req: dict) -> Any:
        if req.get("cmd") == "status":
            return self.status()
        if req["cmd"] == "la_dir":   # the wiring's pin directions, sent at connect
            return {"pins": []}
        self.sent.append(req)
        if req["cmd"] == "target_power":
            self.on[req["rail"]] = req["enabled"]
            return {"rail": req["rail"], "enabled": req["enabled"]}
        if req["cmd"] == "target_status":
            d: Dict[str, Any] = {"efuse1": {"enabled": False, "fault": False, "valid": True},
                                 "efuse2": {"enabled": False, "fault": False, "valid": True},
                                 "status_supported": True}
            if self.rails:
                d["j1_rails"] = {"v3v3": self.on["j1_3v3"], "v1v8": self.on["j1_1v8"]}
            return d
        raise AssertionError(req)


def _bp(rails: bool, features_rev: int = 22) -> "tuple[BenchPod, Pod]":
    pod = Pod(rails, features_rev)
    return BenchPod(transport=pod, lease=False, wiring={"version": 2}), pod


def test_capability_and_feature():
    bp, _ = _bp(True)
    assert bp.capabilities.dut_rails and bp.supports("dut_rails")
    old, _ = _bp(False)
    assert not old.capabilities.dut_rails and not old.supports("dut_rails")


def test_j1_power_sends_rail():
    bp, pod = _bp(True)
    bp.j1_power("1v8", on=False)
    bp.j1_power("3v3", on=False)
    assert pod.sent == [{"cmd": "target_power", "rail": "j1_1v8", "enabled": False},
                        {"cmd": "target_power", "rail": "j1_3v3", "enabled": False}]
    assert bp.target_status().j1_rails == J1Rails(v3v3=False, v1v8=False)
    bp.j1_power("3v3", on=True)
    assert bp.target_status().j1_rails == J1Rails(v3v3=True, v1v8=False)


def test_bad_rail_and_old_boards_send_nothing():
    bp, pod = _bp(True)
    with pytest.raises(ValueError, match="3v3"):
        bp.j1_power("5v", on=True)
    old, opod = _bp(False)
    with pytest.raises(ValueError, match="no switchable J1"):
        old.j1_power("3v3", on=True)
    assert pod.sent == [] and opod.sent == []
    assert old.target_status().j1_rails is None


def test_old_firmware_refused():
    bp, pod = _bp(True, features_rev=21)
    with pytest.raises(ValueError):
        bp.j1_power("3v3", on=True)
    assert pod.sent == []
