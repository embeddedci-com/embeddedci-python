"""The four target-power supplies (``"5v"``, ``"external"``, ``"3v3"``, ``"1v8"``): ``supply=`` names them
on ``power_on`` / ``power_off`` / ``target_power`` / ``power_cycle_and_capture``, ``efuse=`` stays as a
deprecated keyword, the wiring profile's supply can be a switched one (feature ``supply_3v3_1v8``), and
the switched supplies have no current monitor."""

from __future__ import annotations

import warnings
from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import BenchPod, Wiring
from embeddedci.benchpod.state import PowerStatus, SupplyState, TargetStatus


class Pod:
    def __init__(self, switched: bool, features_rev: int = 22) -> None:
        self.switched = switched
        self.features_rev = features_rev
        self.sent: List[Dict[str, Any]] = []
        self.efuse_calls: List[tuple] = []
        self.on = {"3v3": True, "1v8": True}

    def status(self) -> Dict[str, Any]:
        return {"board": "stm32h563", "adc_bits": 16, "version": "4.0.7", "features_rev": self.features_rev,
                "gateware": 68, "fpga_image": "deep",
                "hw": ["nrst_pin"] + (["supply_3v3_1v8"] if self.switched else []),
                "board_rev": "v4" if self.switched else "v3"}

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
            self.on[req["supply"]] = req["enabled"]
            return {"supply": req["supply"], "enabled": req["enabled"]}
        if req["cmd"] == "target_status":
            off = {"enabled": False, "fault": False, "valid": True}
            d: Dict[str, Any] = {"efuse1": dict(off), "efuse2": dict(off), "status_supported": True,
                                 "supplies": {"5v": dict(off), "external": dict(off)}}
            if self.switched:
                d["supplies"]["3v3"] = {"enabled": self.on["3v3"]}
                d["supplies"]["1v8"] = {"enabled": self.on["1v8"]}
            return d
        raise AssertionError(req)


def _bp(switched: bool, features_rev: int = 22, wiring: Any = None) -> "tuple[BenchPod, Pod]":
    pod = Pod(switched, features_rev)
    return BenchPod(transport=pod, lease=False, wiring=wiring or {"version": 2}), pod


def test_capability_and_feature():
    bp, _ = _bp(True)
    assert bp.capabilities.supply_3v3_1v8 and bp.supports("supply_3v3_1v8")
    old, _ = _bp(False)
    assert not old.capabilities.supply_3v3_1v8 and not old.supports("supply_3v3_1v8")


def test_switched_supplies_send_target_power_supply():
    bp, pod = _bp(True)
    bp.power_off("1v8")
    bp.target_power(on=False, supply="3v3")
    assert pod.sent == [{"cmd": "target_power", "supply": "1v8", "enabled": False},
                        {"cmd": "target_power", "supply": "3v3", "enabled": False}]
    st = bp.target_status()
    assert st.supplies["3v3"] == SupplyState(enabled=False) and st.supplies["1v8"].enabled is False
    bp.power_on("3v3")
    assert bp.target_status().supply("3v3").enabled and not bp.target_status().supply("1v8").enabled
    assert pod.efuse_calls == []


def _tp(pod):
    return [(r["supply"], r["enabled"]) for r in pod.sent if r["cmd"] == "target_power"]


def test_5v_and_external_and_the_old_names():
    bp, pod = _bp(True)    # features revision 22: the pod takes the supply key
    bp.target_power(on=True, supply="5v")
    bp.target_power(on=True, supply="external")
    bp.target_power(on=True, supply="internal")   # the old name of 5v
    bp.target_power(1, on=False)                  # the old positional efuse number
    bp.target_power(on=False, supply=2)
    assert _tp(pod) == [("5v", True), ("external", True), ("5v", True), ("5v", False), ("external", False)]
    with pytest.warns(DeprecationWarning, match="efuse is deprecated"):
        bp.target_power(on=False, efuse=2)
    assert _tp(pod)[-1] == ("external", False) and pod.efuse_calls == []


def test_older_firmware_gets_the_efuse_key():
    bp, pod = _bp(False, features_rev=21)
    bp.target_power(on=True, supply="5v")
    bp.power_off("external")
    assert pod.efuse_calls == [(1, True, 0), (2, False, 0)] and pod.sent == []


def test_both_supply_and_efuse_is_an_error_and_unknown_supplies_list_the_four():
    bp, pod = _bp(True)
    pod.sent.clear()
    with pytest.raises(ValueError, match="not both"):
        bp.power_off("5v", efuse=1)
    with pytest.raises(ValueError, match="not both"):
        bp.target_power(on=True, efuse=1, supply="3v3")
    with pytest.raises(ValueError):
        bp.target_power(on=True, efuse="3v3")     # the old name keeps its old values
    with pytest.raises(ValueError, match="'5v', 'external', '3v3', '1v8'"):
        bp.power_on("12v")
    with pytest.raises(ValueError, match="supply must be"):
        bp.power_on(True)
    assert _tp(pod) == [] and pod.efuse_calls == []


def test_the_profile_supply_can_be_a_switched_one():
    bp, pod = _bp(True, wiring={"version": 2, "supply": "3v3"})
    assert bp.wiring.supply == "3v3"
    bp.power_off()
    assert pod.sent[-1] == {"cmd": "target_power", "supply": "3v3", "enabled": False}
    bp.power_on()
    assert pod.sent[-1] == {"cmd": "target_power", "supply": "3v3", "enabled": True}
    assert pod.efuse_calls == []


def test_power_cycle_follows_the_profile_supply(monkeypatch):
    bp, pod = _bp(True, wiring={"version": 2, "supply": "1v8", "uart_rx": 1, "uart_tx": 2})
    monkeypatch.setattr(bp, "capture_uart", lambda **kw: "captured")
    assert bp.power_cycle_and_capture(delay=0.5, duration=1.0, off_settle=0) == "captured"
    assert [(r["supply"], r["enabled"], r.get("delay_ms")) for r in pod.sent if r["cmd"] == "target_power"] == [
        ("1v8", False, None), ("1v8", True, 500)]


def test_the_profile_validates_the_supply_and_reads_the_legacy_key():
    assert Wiring.from_dict({"version": 2, "efuse": 1}).supply == "5v"
    assert Wiring.from_dict({"version": 2, "efuse": 2}).supply == "external"
    assert Wiring.from_dict({"version": 2, "supply": "internal"}).supply == "5v"
    assert Wiring.from_dict({"version": 2, "supply": "1v8"}).supply == "1v8"
    assert Wiring.from_dict({"version": 2, "supply": "5v", "efuse": 1}).supply == "5v"
    with pytest.raises(ValueError, match="supply .*must be"):
        Wiring.from_dict({"version": 2, "supply": "j1_5v"})
    with pytest.raises(ValueError, match="different supplies"):
        Wiring.from_dict({"version": 2, "supply": "external", "efuse": 1})
    with pytest.raises(ValueError, match="efuse must be 1"):
        Wiring.from_dict({"version": 2, "efuse": 3})
    with pytest.raises(ValueError, match="efuse must be 1"):
        Wiring.from_dict({"version": 2, "efuse": "3v3"})


def test_wiring_serialization_and_the_deprecated_efuse_view():
    d = Wiring(supply="external").to_dict()
    assert d["supply"] == "external" and d["efuse"] == 2
    d = Wiring(supply="3v3").to_dict()
    assert d["supply"] == "3v3" and "efuse" not in d
    assert "efuse" not in Wiring().to_dict() and Wiring().to_dict()["supply"] is None
    with pytest.warns(DeprecationWarning, match="Wiring.efuse"):
        assert Wiring(supply="5v").efuse == 1
    with pytest.warns(DeprecationWarning):
        assert Wiring(efuse=2).supply == "external"
    with pytest.warns(DeprecationWarning):
        assert Wiring(supply="5v").with_changes(efuse=2).supply == "external"
    with pytest.raises(ValueError, match="not both"):
        Wiring(supply="5v", efuse=1)
    assert Wiring.from_dict(Wiring(supply="external").to_dict()).supply == "external"


def test_a_switched_supply_has_no_meter():
    bp, pod = _bp(True)
    with pytest.raises(ValueError, match="no current monitor"):
        bp.measure_power(1.0, supply="3v3")
    bp2, _ = _bp(True, wiring={"version": 2, "supply": "3v3"})
    with pytest.raises(ValueError, match="external current monitor"):
        bp2.measure_power(1.0)
    assert pod.sent == []


def test_old_boards_and_firmware_are_refused_before_anything_is_sent():
    old, opod = _bp(False)
    with pytest.raises(ValueError, match="no switchable 3.3 V and 1.8 V"):
        old.power_on("3v3")
    assert set(old.target_status().supplies) == {"5v", "external"}
    bp, pod = _bp(True, features_rev=21)
    with pytest.raises(ValueError):
        bp.power_on("3v3")
    assert pod.sent == [] and not [r for r in opod.sent if r["cmd"] == "target_power"]


def test_status_readers_prefer_supplies_and_fall_back_to_the_legacy_keys():
    legacy = TargetStatus.from_reply({"efuse1": {"enabled": 1, "fault": 0, "valid": 1},
                                      "efuse2": {"enabled": 0, "fault": 1, "valid": 1}})
    assert list(legacy.supplies) == ["5v", "external"]
    assert legacy.supplies["5v"].enabled and legacy.supplies["external"].fault
    with pytest.warns(DeprecationWarning):
        assert legacy.internal.enabled and legacy.efuse(2).fault
    with pytest.raises(ValueError, match="no '3v3' supply"):
        legacy.supply("3v3")
    ps = PowerStatus.from_reply({"internal": {"ok": True, "bus_mv": 5000, "current_ua": 1000},
                                 "external": {"ok": False}})
    assert ps.supplies["5v"].current == pytest.approx(0.001)
    new = PowerStatus.from_reply({"supplies": {"5v": {"ok": True, "bus_mv": 4900, "current_ua": 2000},
                                               "external": {"ok": True, "bus_mv": 12000, "current_ua": 5}},
                                  "internal": {"ok": True, "bus_mv": 1, "current_ua": 1}})
    assert new.supplies["5v"].bus_voltage == pytest.approx(4.9)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert new.rail(2).bus_voltage == pytest.approx(12.0) and new.internal is new.supplies["5v"]
    with pytest.raises(ValueError, match="no current monitor"):
        new.supply("3v3")


def test_flash_target_power_takes_a_supply_and_the_old_numbers_warn():
    bp, _ = _bp(True)
    assert bp._power_arg(None) is None and bp._power_arg("external") == "external"
    with pytest.warns(DeprecationWarning, match="target_power=1/2"):
        assert bp._power_arg(1) == "5v"
    with pytest.raises(ValueError, match="supply must be"):
        bp._power_arg("12v")
