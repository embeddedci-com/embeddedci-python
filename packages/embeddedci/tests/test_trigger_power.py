"""Triggered captures and power profiles, against fakes that speak the firmware contract."""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

import pytest

from embeddedci.benchpod import (
    BenchPod,
    BenchPodError,
    FirmwareError,
    PowerProfile,
    Signal,
    Trigger,
    TriggerTimeout,
    Wiring,
)


class StreamPod:
    """Answers captures and power profiles with canned chunks; records every request."""

    def __init__(self, caps: Optional[List[str]] = None) -> None:
        self.caps = ["la", "capture_trigger", "power_profile"] if caps is None else caps
        self.requests: List[dict] = []
        self.chunks: Dict[str, List[dict]] = {}
        self.error: Optional[str] = None

    def status(self) -> Dict[str, Any]:
        return {"board": "stm32h563", "adc_bits": 16, "caps": self.caps}

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass

    def command(self, req: dict) -> Any:
        self.requests.append(req)
        return {"started": True} if req.get("action") == "start" else {}

    def stream_chunks(self, req: dict) -> Iterator[Dict[str, Any]]:
        self.requests.append(req)
        if self.error:
            raise FirmwareError(self.error, cmd=req["cmd"])
        if "rate_hz" in req and isinstance(req["rate_hz"], float):
            # The firmware's parser refuses a non-integer here ("rate_hz must be a whole
            # number 0..1000000") — a float used to reach the pod and fail only on hardware.
            raise FirmwareError("power_profile: rate_hz must be a whole number 0..1000000",
                                cmd=req["cmd"])
        key = req["cmd"] if req.get("action") != "stop" else "power_profile"
        yield from self.chunks.get(key, [{"status": "ok", "data": [], "more": False}])


def _bp(pod: Optional[StreamPod] = None, **kwargs: Any):
    pod = pod or StreamPod()
    return BenchPod(transport=pod, lease=False, **kwargs), pod


# -- triggers ---------------------------------------------------------------------------

def test_trigger_validates_itself():
    with pytest.raises(ValueError, match="edge"):
        Trigger(9, "sideways")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="1-12"):
        Trigger(13)
    assert Trigger("READY", "high").la == "READY"


def test_triggered_la_capture_request_and_result():
    bp, pod = _bp()
    pod.chunks["la_capture"] = [{"status": "ok", "la": True, "la_edges": [[0, 256]], "la_upto": 4,
                                 "la_rate_hz": 1e6, "more": False}]
    la = bp.capture_la(4, sample_rate_hz=1e6, trigger=Trigger(9, "falling"), trigger_timeout=2.5)
    req = pod.requests[-1]
    assert req["trigger"] == {"la": 9, "edge": "falling"} and req["trigger_timeout_ms"] == 2500
    assert la.trigger == Trigger(9, "falling") and la.words == [256] * 4


def test_trigger_names_come_from_the_wiring_profile():
    bp, pod = _bp(wiring=Wiring(signals=[Signal("READY", 10)]))
    bp.capture_la(8, trigger=Trigger("ready", "rising"))
    assert pod.requests[-1]["trigger"] == {"la": 10, "edge": "rising"}


def test_triggered_adc_capture_uses_the_streaming_path():
    bp, pod = _bp()
    pod.chunks["capture_dual"] = [{"status": "ok", "data": [1, 2, 3], "adc_rate_hz": 1e5, "more": False}]
    cap = bp.capture_adc(3, trigger=Trigger(2, "low"))
    req = pod.requests[-1]
    assert req["cmd"] == "capture_dual" and req["la_samples"] == 0 and req["trigger"]["edge"] == "low"
    assert cap.counts == [1, 2, 3] and cap.trigger == Trigger(2, "low")


def test_trigger_timeout_and_bounds():
    bp, pod = _bp()
    pod.error = "trigger timeout: no rising edge on LA9 within 10000 ms"
    with pytest.raises(TriggerTimeout) as ei:
        bp.capture_la(8, trigger=Trigger(9))
    assert ei.value.la == 9 and ei.value.edge == "rising"
    with pytest.raises(ValueError, match="trigger_timeout"):
        bp.capture_la(8, trigger=Trigger(9), trigger_timeout=0)


def test_triggers_need_firmware_that_has_them():
    bp, pod = _bp(StreamPod(caps=["la"]))
    with pytest.raises(BenchPodError, match="capture_trigger"):
        bp.capture_la(8, trigger=Trigger(9))
    assert pod.requests == []


# -- power profiles ---------------------------------------------------------------------

STATS = {"efuse": 1, "rate_hz": 364.0, "adc_rate_hz": 950.0, "n": 950, "duration_ms": 1000, "avg_ua": 52000,
         "min_ua": 40000, "peak_ua": 180000, "avg_mv": 5010, "min_mv": 4990, "max_mv": 5030,
         "energy_uj": 260500, "charge_uc": 52000, "fault": False, "truncated": False}


def test_measure_power_request_and_units():
    bp, pod = _bp(wiring=Wiring(efuse=2))
    pod.chunks["power_profile"] = [
        {"status": "ok", "t_us": [0, 500000], "current_ua": [50000, 54000], "bus_mv": [5010, 5000],
         "more": True},
        {"status": "ok", "t_us": [], "current_ua": [], "bus_mv": [], "stats": STATS, "more": False},
    ]
    prof = bp.measure_power(1.0, keep_samples=2)
    assert pod.requests[-1] == {"cmd": "power_profile", "efuse": 2, "rate_hz": 500,
                                "keep_samples": 2, "duration_ms": 1000}
    assert isinstance(prof, PowerProfile)
    assert prof.avg_current == pytest.approx(0.052) and prof.peak_current == pytest.approx(0.18)
    assert prof.avg_voltage == pytest.approx(5.01) and prof.energy == pytest.approx(0.2605)
    assert prof.charge == pytest.approx(0.052) and prof.duration == 1.0
    assert prof.avg_power == pytest.approx(0.2605)
    assert prof.samples == ((0.0, 0.05, 5.01), (0.5, 0.054, 5.0))


def test_power_profile_session_start_stop():
    bp, pod = _bp()
    pod.chunks["power_profile"] = [{"status": "ok", "stats": STATS, "more": False}]
    session = bp.power_profile(max_duration=5.0, keep_samples=0)
    with pytest.raises(BenchPodError, match="never started"):
        session.stop()
    with session as prof:
        with pytest.raises(BenchPodError, match="still running"):
            _ = prof.result
    assert pod.requests[0] == {"cmd": "power_profile", "efuse": 1, "rate_hz": 500, "keep_samples": 0,
                               "max_duration_ms": 5000, "action": "start"}
    assert pod.requests[-1] == {"cmd": "power_profile", "action": "stop"}
    assert prof.result.n == 950 and prof.stop() is prof.result


def test_power_profile_without_streaming_uses_one_reply():
    class OneShot(StreamPod):
        stream_chunks = None  # type: ignore[assignment]

        def command(self, req):
            self.requests.append(req)
            return {"stats": STATS}

    bp, _ = _bp(OneShot())
    assert bp.measure_power(0.5).peak_current == pytest.approx(0.18)


def test_power_profile_validation_and_capability():
    bp, pod = _bp()
    for kwargs in ({"rate_hz": 50}, {"keep_samples": 5000}):
        with pytest.raises(ValueError):
            bp.measure_power(1.0, **kwargs)
    with pytest.raises(ValueError, match="duration"):
        bp.measure_power(0)
    assert pod.requests == []
    with pytest.raises(BenchPodError, match="power_profile"):
        _bp(StreamPod(caps=["la"]))[0].measure_power(1.0)
    with pytest.raises(BenchPodError, match="no statistics"):
        PowerProfile.from_chunks([{"current_ua": [1]}])
