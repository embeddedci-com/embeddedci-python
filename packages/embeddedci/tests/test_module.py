"""The analog module of a two-board BenchPod (digital v3r3): module(), set_module_id() and where the
calibration is stored (Calibration.stored, firmware 3.10.1+)."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import BenchPod, BenchPodError, ModuleInfo
from embeddedci.benchpod.state import Calibration


class ModulePod:
    """A digital v3r3 pod with an analog module whose EEPROM starts blank."""

    def __init__(self) -> None:
        self.eeprom: Dict[str, Any] = {"eeprom": "blank"}
        self.commands: List[dict] = []

    def status(self) -> Any:
        return {"board": "stm32h563", "version": "3.10.1", "adc_bits": 16, "analog": True,
                "caps": ["calibrate", "cal_dmm", "analog"]}

    def ping(self) -> Any:
        return "pong"

    def _module(self) -> Dict[str, Any]:
        return {"present": True, "analog": True, "power": True, "bridge_power": True, **self.eeprom}

    def command(self, req: dict) -> Any:
        self.commands.append(req)
        if req["cmd"] == "module":
            if req.get("eeprom") == "id":
                cal = {k: self.eeprom.get(k, v) for k, v in (("cal_len", 0), ("cal_ok", False))}
                self.eeprom = {"eeprom": "ok", "type": req["type"], "rev": req["rev"], "serial": req["serial"],
                               "layout": 1, **cal}
            return self._module()
        raise AssertionError(f"unexpected command {req}")

    def close(self) -> None:
        pass


def _pod() -> "tuple[BenchPod, ModulePod]":
    t = ModulePod()
    return BenchPod(transport=t, lease=False), t


def test_blank_module() -> None:
    bp, _ = _pod()
    m = bp.module()
    assert isinstance(m, ModuleInfo)
    assert m.present and m.analog and m.eeprom == "blank"
    assert m.serial is None and m.cal_len == 0 and not m.cal_ok


def test_set_module_id_writes_once() -> None:
    bp, t = _pod()
    m = bp.set_module_id("am-3f9a1c07b2e4", "3.3")
    assert m.eeprom == "ok" and m.serial == "am-3f9a1c07b2e4" and m.rev == "3.3" and m.type == "analog"
    assert t.commands[-1] == {"cmd": "module", "eeprom": "id", "type": "analog", "rev": "3.3",
                              "serial": "am-3f9a1c07b2e4"}
    # The same serial again is fine (a revision fix); another serial needs replace=True.
    assert bp.set_module_id("am-3f9a1c07b2e4", "3.4").rev == "3.4"
    with pytest.raises(BenchPodError, match="already has serial"):
        bp.set_module_id("am-000000000000", "3.3")
    assert bp.set_module_id("am-000000000000", "3.3", replace=True).serial == "am-000000000000"


def test_set_module_id_keeps_the_calibration_fields() -> None:
    bp, t = _pod()
    t.eeprom = {"eeprom": "ok", "type": "analog", "rev": "3.3", "serial": "", "layout": 1, "cal_len": 124,
                "cal_ok": True}
    m = bp.set_module_id("am-3f9a1c07b2e4", "3.3")   # an empty serial is not "another serial"
    assert m.cal_len == 124 and m.cal_ok


@pytest.mark.parametrize("serial", ["", "x" * 17, 'a"b', "a\\b", "a\nb"])
def test_bad_serial(serial: str) -> None:
    bp, _ = _pod()
    with pytest.raises(ValueError):
        bp.set_module_id(serial, "3.3")


@pytest.mark.parametrize("rev", ["3", "v3r3", "3.3.1", ""])
def test_bad_rev(rev: str) -> None:
    bp, _ = _pod()
    with pytest.raises(ValueError):
        bp.set_module_id("am-1", rev)


def test_calibration_stored() -> None:
    assert Calibration.from_reply({"source": "current_in", "calibrated": True, "stored": "module"}).stored == "module"
    assert Calibration.from_reply({"source": "current_in", "calibrated": True}).stored is None
