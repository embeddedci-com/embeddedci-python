"""The 4-20 mA input (J8) on both analog module revisions: 249 ohm up to v3r4, 100 ohm from v3r5
(firmware 3.10.13+), where ``current_in`` takes the low range by itself and the pod reports the
resistance it converts with (``sense_mohm``)."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from embeddedci.benchpod import BenchPod
from embeddedci.benchpod.state import AdcReading, Calibration

from test_adc_range import RangePod


class V3r5Pod(RangePod):
    """A v3r5 module: routing current_in without a range picks the low range, the next relay path
    (or a DAC route) gives it back, as the firmware does (analog_path_auto_range)."""

    def __init__(self, shunt_100r: bool = True) -> None:
        super().__init__(supported=True)
        self.shunt_100r = shunt_100r
        self.auto = False

    def _sense_mohm(self) -> int:
        if not self.shunt_100r:
            return 249000
        return 99995 if self.range == "low" else 99990

    def _route(self, path: str, rng: Any) -> None:
        if rng is not None:
            self.auto = False
            if path in ("off", "cal2"):
                self.range = "normal"
            self._set(rng)
            return
        if path in ("off", "cal2"):
            self.range, self.auto = "normal", False
        elif path == "current_in":
            if self.range == "normal" and self.shunt_100r:
                self.range, self.auto = "low", True
        elif path != "current_out" and self.auto:
            self.range, self.auto = "normal", False

    def command(self, req: dict) -> Any:
        cmd = req["cmd"]
        if cmd == "analog_path":
            self.commands.append(req)
            self._route(req["path"], req.get("range"))
            return {"path": req["path"], "u55": 0, "u58": 0, "range": self.range}
        if cmd == "adc_read":
            self.commands.append(req)
            self._route(req["source"], req.get("range"))
            d: Dict[str, Any] = {"source": req["source"], "mv": 1600, "uv": 1600212, "count": 55938, "span": 5,
                                 "range": self.range, "fullscale_mv": 5462 if self.range == "low" else 32856,
                                 "overrange": False}
            if req["source"] == "current_in":
                mohm = self._sense_mohm()
                d.update({"offset_mv": 0, "ua": round(1600212 * 1000 / mohm), "sense_mohm": mohm})
            return d
        if cmd == "dac_out":
            self.commands.append(req)
            self._route("dac_" + req["path"] if req["path"] != "off" else "off", None)
            return {"path": req["path"], "mv": 0, "code": 0}
        d = super().command(req)
        if cmd == "calibrate":
            d["sense_mohm"] = self._sense_mohm()
        return d


def _pod(shunt_100r: bool = True) -> "tuple[BenchPod, V3r5Pod]":
    t = V3r5Pod(shunt_100r)
    return BenchPod(transport=t, lease=False), t


def test_models_carry_the_sense_resistance():
    r = AdcReading.from_reply({"source": "current_in", "mv": 1600, "uv": 1600212, "count": 1, "ua": 16003,
                               "sense_mohm": 99995, "range": "low"})
    assert r.sense_ohms == pytest.approx(99.995) and r.current == pytest.approx(0.016003)
    c = Calibration.from_reply({"source": "current_in", "calibrated": True, "a_uv": 1, "b_nv": 1,
                                "sense_mohm": 249000})
    assert c.sense_ohms == pytest.approx(249.0)
    # Older firmware does not say: None, and the SDK takes 249 ohm.
    assert AdcReading.from_reply({"source": "current_in", "mv": 996, "ua": 4000}).sense_ohms is None
    assert Calibration.from_reply({"source": "current_in", "calibrated": False}).sense_ohms is None
    assert Calibration.from_reply({"source": "current_in", "sense_mohm": 0}).sense_ohms is None


def test_v3r4_reads_as_before():
    bp, t = _pod(shunt_100r=False)
    r = bp.adc_read("current_in")
    assert r.range == "normal" and r.sense_ohms == pytest.approx(249.0)
    assert r.current == pytest.approx(1.600212 / 249, abs=1e-6)
    cap = bp.capture_adc(2, source="current_in")
    assert cap.currents == pytest.approx([v / 249.0 for v in cap.volts])


def test_v3r5_reads_current_in_in_the_low_range():
    bp, t = _pod()
    bp.capabilities
    r = bp.adc_read("current_in")
    assert t.commands[0] == {"cmd": "adc_read", "source": "current_in"}   # no range sent: the pod picks it
    assert r.range == "low" and r.fullscale == pytest.approx(5.462)
    assert r.sense_ohms == pytest.approx(99.995) and r.current == pytest.approx(0.016003, abs=1e-6)
    assert bp.capabilities.adc_range == "low"          # captures now scale with the low fit
    # Another source gets the normal range back, and the SDK follows.
    assert bp.adc_read("ext").range == "normal" and bp.capabilities.adc_range == "normal"
    # Asked for, the normal range stays on current_in: 100 ohm || 992.5k.
    r = bp.adc_read("current_in", range="normal")
    assert r.range == "normal" and r.sense_ohms == pytest.approx(99.990)


def test_v3r5_capture_is_in_amps_through_100_ohm():
    bp, t = _pod()
    cap = bp.capture_adc(2, source="current_in")
    assert t.commands[0] == {"cmd": "analog_path", "path": "current_in"}
    assert cap.range == "low"
    assert cap.currents and cap.currents == pytest.approx([v / 99.995 for v in cap.volts])
    # 59538 in the low fit is about 1 V: 10 mA through 100 ohm (it was 4 mA through 249 ohm).
    assert cap.currents[1] == pytest.approx(0.010, abs=2e-4)


def test_a_dac_route_after_current_in_brings_the_normal_range_back():
    bp, t = _pod()
    bp.analog_path("current_in")
    assert bp.capabilities.adc_range == "low"
    bp.dac_output("12v", volts=10.0)                   # the pod put K5 back without saying so
    assert t.range == "normal" and bp.capabilities.adc_range == "normal"
    assert t.commands[-1] == {"cmd": "adc_range"}      # the normal fit was re-read


def test_a_low_range_the_user_chose_is_not_given_back():
    bp, t = _pod()
    bp.analog_path("current_in", range="low")
    n = len(t.commands)
    bp.dac_output("5v", volts=1.0)
    assert t.range == "low" and bp.capabilities.adc_range == "low" and len(t.commands) == n + 1
