"""The switched J1 supplies of the digital board v4 (feature ``dut_rails``): ``rail=`` names all four
power outputs on ``power_on`` / ``power_off`` / ``target_power`` / ``power_cycle_and_capture``,
``efuse=`` stays as the old name, the wiring profile's rail can be a J1 supply, and a J1 supply has
no current monitor."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import BenchPod, J1Rails, Wiring


class Pod:
    def __init__(self, rails: bool, features_rev: int = 22) -> None:
        self.rails = rails
        self.features_rev = features_rev
        self.sent: List[Dict[str, Any]] = []
        self.efuse_calls: List[tuple] = []
        self.on = {"j1_3v3": True, "j1_1v8": True}

    def status(self) -> Dict[str, Any]:
        return {"board": "stm32h563", "adc_bits": 16, "version": "4.0.7", "features_rev": self.features_rev,
                "gateware": 68, "fpga_image": "deep", "hw": ["nrst_pin"] + (["dut_rails"] if self.rails else []),
                "board_rev": "v4" if self.rails else "v3"}

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass

    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        self.efuse_calls.append((efuse, on, delay_ms))

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


def _bp(rails: bool, features_rev: int = 22, wiring: Any = None) -> "tuple[BenchPod, Pod]":
    pod = Pod(rails, features_rev)
    return BenchPod(transport=pod, lease=False, wiring=wiring or {"version": 2}), pod


def test_capability_and_feature():
    bp, _ = _bp(True)
    assert bp.capabilities.dut_rails and bp.supports("dut_rails")
    old, _ = _bp(False)
    assert not old.capabilities.dut_rails and not old.supports("dut_rails")


def test_rail_sends_target_power_rail():
    bp, pod = _bp(True)
    bp.power_off(rail="j1_1v8")
    bp.target_power(on=False, rail="j1_3v3")
    assert pod.sent == [{"cmd": "target_power", "rail": "j1_1v8", "enabled": False},
                        {"cmd": "target_power", "rail": "j1_3v3", "enabled": False}]
    assert bp.target_status().j1_rails == J1Rails(v3v3=False, v1v8=False)
    bp.power_on(rail="j1_3v3")
    assert bp.target_status().j1_rails == J1Rails(v3v3=True, v1v8=False)
    assert pod.efuse_calls == []


def test_rail_names_for_the_efuses_and_efuse_alias():
    bp, pod = _bp(True)
    bp.target_power(on=True, rail="internal")
    bp.target_power(on=True, rail="external")
    bp.target_power(1, on=False)          # the old positional efuse
    bp.target_power(on=False, efuse=2)
    assert pod.efuse_calls == [(1, True, 0), (2, True, 0), (1, False, 0), (2, False, 0)]


def test_both_rail_and_efuse_is_an_error_and_efuse_takes_only_1_and_2():
    bp, pod = _bp(True)
    with pytest.raises(ValueError, match="not both"):
        bp.power_off(1, rail="internal")
    with pytest.raises(ValueError, match="not both"):
        bp.target_power(on=True, efuse=1, rail="j1_3v3")
    with pytest.raises(ValueError):
        bp.target_power(on=True, efuse="j1_3v3")     # the old name keeps its old values
    with pytest.raises(ValueError, match="rail must be"):
        bp.power_on(rail="5v")
    assert pod.sent == [] and pod.efuse_calls == []


def test_the_profile_rail_can_be_a_j1_supply():
    bp, pod = _bp(True, wiring={"version": 2, "efuse": "j1_3v3"})
    assert bp.wiring.efuse == "j1_3v3"
    bp.power_off()
    assert pod.sent[-1] == {"cmd": "target_power", "rail": "j1_3v3", "enabled": False}
    bp.power_on()
    assert pod.sent[-1] == {"cmd": "target_power", "rail": "j1_3v3", "enabled": True}
    assert pod.efuse_calls == []


def test_power_cycle_follows_the_profile_rail(monkeypatch):
    bp, pod = _bp(True, wiring={"version": 2, "efuse": "j1_1v8", "uart_rx": 1, "uart_tx": 2})
    monkeypatch.setattr(bp, "capture_uart", lambda **kw: "captured")
    assert bp.power_cycle_and_capture(delay=0.5, duration=1.0, off_settle=0) == "captured"
    assert [(r["rail"], r["enabled"], r.get("delay_ms")) for r in pod.sent if r["cmd"] == "target_power"] == [
        ("j1_1v8", False, None), ("j1_1v8", True, 500)]


def test_the_profile_validates_the_rail_and_names_load():
    assert Wiring.from_dict({"version": 2, "efuse": "internal"}).efuse == 1
    assert Wiring.from_dict({"version": 2, "efuse": "external"}).efuse == 2
    assert Wiring.from_dict({"version": 2, "efuse": "j1_1v8"}).efuse == "j1_1v8"
    with pytest.raises(ValueError, match="target-power rail"):
        Wiring.from_dict({"version": 2, "efuse": "j1_5v"})
    with pytest.raises(ValueError, match="target-power rail"):
        Wiring.from_dict({"version": 2, "efuse": 3})


def test_a_j1_supply_has_no_meter():
    bp, pod = _bp(True)
    with pytest.raises(ValueError, match="no current monitor"):
        bp.measure_power(1.0, rail="j1_3v3")
    bp2, _ = _bp(True, wiring={"version": 2, "efuse": "j1_3v3"})
    with pytest.raises(ValueError, match="external current monitor"):
        bp2.measure_power(1.0)
    assert pod.sent == []


def test_old_boards_and_firmware_are_refused_before_anything_is_sent():
    old, opod = _bp(False)
    with pytest.raises(ValueError, match="no switchable J1"):
        old.power_on(rail="j1_3v3")
    assert old.target_status().j1_rails is None
    bp, pod = _bp(True, features_rev=21)
    with pytest.raises(ValueError):
        bp.power_on(rail="j1_3v3")
    assert pod.sent == [] and not [r for r in opod.sent if r["cmd"] == "target_power"]
