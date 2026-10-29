"""The ADC input range (analog module v3r4, firmware 3.9.5+): adc_range, range= on analog_path /
adc_read / capture_adc / calibrate_adc_gain, and captures scaled with the fit of the range in use."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import BenchPod, BenchPodError
from embeddedci.benchpod.capabilities import Capabilities
from embeddedci.benchpod.results import Capture
from embeddedci.benchpod.state import AdcRangeState, AdcRangeTest, AdcReading, AnalogPathState, Calibration

NORMAL_FIT = {"adc_cal_a_uv": 65708158, "adc_cal_b_nv": -1002644}
LOW_FIT = {"adc_cal_a_uv": 10923784, "adc_cal_b_nv": -166687}


class RangePod:
    """A v3r4 module pod: tracks the range like the firmware (off and cal2 put it back to normal)."""

    def __init__(self, supported: bool = True) -> None:
        self.supported = supported
        self.range = "normal"
        self.commands: List[dict] = []

    def status(self) -> Any:
        caps = ["calibrate", "cal_dmm", "analog"] + (["adc_low_range"] if self.supported else [])
        return {"board": "stm32h563", "version": "3.9.5", "adc_bits": 16, "adc_fullscale_mv": 4096,
                "analog": True, "caps": caps, **NORMAL_FIT, "adc_cal_unwrap": True,
                "adc_range": "normal", "adc_range_fs_mv": 32856}

    def ping(self) -> Any:
        return "pong"

    def _set(self, rng: str) -> None:
        if rng == "low" and not self.supported:
            raise BenchPodError("range: the low range needs analog module v3r4 or later")
        self.range = rng

    def _range_reply(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"range": self.range, "supported": self.supported,
                             "fullscale_mv": 5462 if self.range == "low" else 32856, "normal_fullscale_mv": 32856}
        if self.supported:
            d["low_fullscale_mv"] = 5462
        d.update(LOW_FIT if self.range == "low" else NORMAL_FIT)
        return d

    def command(self, req: dict) -> Any:
        self.commands.append(req)
        cmd = req["cmd"]
        if cmd == "adc_range":
            if req.get("test"):
                self.range = "normal"
                return {"points": [{"set_mv": 1000, "normal_mv": 1001, "low_mv": 999, "ok": True},
                                   {"set_mv": 3500, "normal_mv": 3502, "low_mv": 582, "ok": False}],
                        "pass": False, "range": "normal"}
            if "range" in req:
                self._set(req["range"])
            return self._range_reply()
        if cmd == "analog_path":
            if req["path"] in ("off", "cal2"):
                self.range = "normal"
            if "range" in req:
                self._set(req["range"])
            return {"path": req["path"], "u55": 0, "u58": 32 if self.range == "low" else 0, "range": self.range}
        if cmd == "adc_read":
            if "range" in req:
                self._set(req["range"])
            return {"source": req["source"], "mv": 1203, "count": 58322, "span": 9, "range": self.range,
                    "fullscale_mv": 5462 if self.range == "low" else 32856, "overrange": False}
        if cmd == "calibrate":
            d = {"source": "current_in", "calibrated": True, "offset_mv": 0, "offset_uv": 0,
                 "a_uv": 10923784 if self.range == "low" else 65708158,
                 "b_nv": -166687 if self.range == "low" else -1002644,
                 "range": self.range, "adc_low_gain_ppm": 12000 if "adc" in req else None}
            if req.get("range") == "low":
                self.range = "normal"
            return d
        if cmd == "capture":
            return {"samples": [0x8000, 59538], "adc_rate_hz": 400000}
        return {}

    def samples(self, req: dict) -> List[int]:
        self.commands.append(req)
        return [0x8000, 59538]

    def close(self) -> None:
        pass


def _pod(supported: bool = True) -> "tuple[BenchPod, RangePod]":
    t = RangePod(supported)
    return BenchPod(transport=t, lease=False), t


def test_adc_range_reads_and_switches():
    bp, t = _pod()
    st = bp.adc_range()
    assert t.commands[-1] == {"cmd": "adc_range"}
    assert st.range == "normal" and st.supported and st.fullscale == pytest.approx(32.856)
    assert st.low_fullscale == pytest.approx(5.462)
    st = bp.adc_range("low")
    assert t.commands[-1] == {"cmd": "adc_range", "range": "low"}
    assert st.range == "low" and st.fullscale == pytest.approx(5.462)
    assert st.a == pytest.approx(10.923784) and st.b == pytest.approx(-166.687e-6)
    with pytest.raises(ValueError):
        bp.adc_range("high")  # type: ignore[arg-type]


def test_switching_the_range_rescales_captures():
    bp, t = _pod()
    caps = bp.capabilities
    assert caps.adc_range == "normal" and caps.adc_low_range
    count = 59538  # 6000 counts above 0 V on the inverting front end
    v_normal = caps.counts_to_volts(count)
    bp.adc_range("low")
    assert bp.capabilities.adc_range == "low"
    v_low = bp.capabilities.counts_to_volts(count)
    assert v_normal == pytest.approx(6.0 * 1.0026, rel=0.01)
    assert v_low == pytest.approx(v_normal * 0.16625, rel=0.001)
    cap = bp.capture_adc(2)
    assert cap.range == "low"


def test_off_and_cal2_follow_the_pod_back_to_normal():
    bp, t = _pod()
    bp.capabilities
    bp.analog_path("adc_ext", range="low")
    assert t.commands[-2] == {"cmd": "analog_path", "path": "adc_ext", "range": "low"}
    assert t.commands[-1] == {"cmd": "adc_range"}  # the reply said low: the fit is re-read
    assert bp.capabilities.adc_range == "low"
    n = len(t.commands)
    bp.analog_path("current_in")  # stays low: nothing to re-read
    assert len(t.commands) == n + 1
    state = bp.analog_path("cal2")
    assert state.range == "normal" and bp.capabilities.adc_range == "normal"
    assert t.commands[-1] == {"cmd": "adc_range"}


def test_adc_read_with_a_range():
    bp, t = _pod()
    r = bp.adc_read("ext", range="low")
    assert t.commands[0] == {"cmd": "adc_read", "source": "ext", "range": "low"}
    assert r.range == "low" and r.fullscale == pytest.approx(5.462) and not r.overrange
    r = bp.adc_read("cal1")
    assert t.commands[-1] == {"cmd": "adc_read", "source": "cal1"}


def test_capture_adc_with_a_range_routes_then_sets_it():
    bp, t = _pod()
    cap = bp.capture_adc(2, source="current_in", range="low")
    cmds = [c for c in t.commands if c["cmd"] != "adc_range"]
    assert cmds[0] == {"cmd": "analog_path", "path": "current_in", "range": "low"}
    assert cmds[1] == {"cmd": "calibrate"}  # the current_in fit, read in the low range
    assert cap.range == "low" and cap.overrange and cap.clipped_samples == 1
    # current_in volts use the low fit the calibrate reply gave
    assert cap.volts[1] == pytest.approx(10.923784 - 166.687e-6 * 59538, abs=1e-6)  # about 1.0 V


def test_capture_adc_range_without_a_source():
    bp, t = _pod()
    bp.capture_adc(2, range="low")
    assert t.commands[0] == {"cmd": "adc_range", "range": "low"}


def test_low_range_refused_without_k5():
    bp, t = _pod(supported=False)
    assert not bp.capabilities.adc_low_range
    st = bp.adc_range()
    assert not st.supported and st.low_fullscale is None
    with pytest.raises(BenchPodError, match="v3r4"):
        bp.adc_range("low")


def test_range_self_test():
    bp, t = _pod()
    bp.adc_range("low")
    res = bp.adc_range_test()
    assert t.commands[-2] == {"cmd": "adc_range", "test": True}
    assert not res.passed and len(res.points) == 2
    assert res.points[0].set_voltage == pytest.approx(1.0) and res.points[0].ok
    assert res.points[1].low == pytest.approx(0.582) and not res.points[1].ok
    assert bp.capabilities.adc_range == "normal"


def test_calibrate_the_low_range_gain():
    bp, t = _pod()
    c = bp.calibrate_adc_gain("cal1", range="low")
    assert {"cmd": "calibrate", "adc": "cal1", "range": "low"} in t.commands
    assert c.adc_low_gain_ppm == 12000
    with pytest.raises(ValueError, match="cal1"):
        bp.calibrate_adc_gain("ext", range="low")
    bp.calibrate_adc_gain("cal1")
    assert t.commands[-1] == {"cmd": "calibrate", "adc": "cal1"}


def test_models_from_older_firmware():
    r = AdcReading.from_reply({"source": "adc_ext", "mv": 100, "count": 1, "span": 2})
    assert r.range == "" and r.fullscale is None and not r.overrange
    assert AnalogPathState.from_reply({"path": "cal1", "u55": 3, "u58": 9}).range == ""
    assert Calibration.from_reply({"source": "current_in"}).adc_low_gain_ppm is None
    st = AdcRangeState.from_reply({"range": "normal", "supported": False, "fullscale_mv": 32856})
    assert st.a is None and st.b is None
    assert not AdcRangeTest.from_reply({}).passed
    caps = Capabilities.from_status({"adc_bits": 16})
    assert caps.adc_range == "" and not caps.adc_low_range and caps.adc_range_fullscale == 0.0


def test_capabilities_from_server_parameters():
    caps = Capabilities.from_parameters({"cap.adc_low_range": "true", "cap.adc_range": "low",
                                         "cap.adc_range_fs_mv": "5462"})
    assert caps.adc_low_range and caps.adc_range == "low" and caps.adc_range_fullscale == pytest.approx(5.462)


def test_capture_overrange_counts_rails_only():
    assert Capture(counts=[0x7FFF, 0x8000, 0x7FFE, 0]).clipped_samples == 2
    assert not Capture(counts=[0x8001, 65535]).overrange
