"""The v2 client surface: typed device state, units, validation, connection-time LA voltage."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from embeddedci import benchpod
from embeddedci.benchpod import BenchPod
from embeddedci.benchpod.state import (
    AdcReading,
    AnalogPathState,
    DacOutput,
    LaVoltage,
    PowerStatus,
    PullState,
    ResetState,
    TargetStatus,
    UsbCcStatus,
)


class FakeTransport:
    """Records every command and power call; replies are looked up by ``cmd``."""

    def __init__(self, replies: Dict[str, Any] | None = None) -> None:
        self.commands: List[dict] = []
        self.power: List[tuple] = []
        self.replies = replies or {}
        self.closed = False

    def status(self) -> Any:
        return {"board": "stm32h563", "adc_bits": 16, "adc_fullscale_mv": 4096}

    def ping(self) -> Any:
        return "pong"

    def command(self, req: dict) -> Any:
        self.commands.append(req)
        reply = self.replies.get(req["cmd"], {})
        return reply(req) if callable(reply) else reply

    def samples(self, req: dict) -> List[int]:
        self.commands.append(req)
        return [40000, 40001]

    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        self.power.append((efuse, on, delay_ms))

    def close(self) -> None:
        self.closed = True


def _bp(replies: Dict[str, Any] | None = None, **kwargs: Any) -> "tuple[BenchPod, FakeTransport]":
    t = FakeTransport(replies)
    return BenchPod(transport=t, lease=False, **kwargs), t


# -- LA voltage ----------------------------------------------------------------

def test_la_voltage_is_set_on_connect(monkeypatch):
    monkeypatch.delenv("BENCHPOD_LA_VOLTAGE", raising=False)
    _, t = _bp({"la_voltage": {"mv": 3300, "st": 1}}, la_voltage=3.3)
    assert t.commands == [{"cmd": "la_voltage", "mv": 3300}]


def test_la_voltage_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("BENCHPOD_LA_VOLTAGE", "1.8")
    _, t = _bp({"la_voltage": {"mv": 1800}})
    assert t.commands == [{"cmd": "la_voltage", "mv": 1800}]


def test_a_bad_la_voltage_closes_the_connection(monkeypatch):
    monkeypatch.setenv("BENCHPOD_LA_VOLTAGE", "5")
    t = FakeTransport()
    with pytest.raises(ValueError, match="1.8 or 3.3"):
        BenchPod(transport=t, lease=False)
    assert t.closed and t.commands == []


def test_no_la_voltage_sends_nothing(monkeypatch):
    monkeypatch.delenv("BENCHPOD_LA_VOLTAGE", raising=False)
    _, t = _bp()
    assert t.commands == []


def test_set_la_voltage_takes_volts_only(monkeypatch):
    monkeypatch.delenv("BENCHPOD_LA_VOLTAGE", raising=False)
    bp, t = _bp({"la_voltage": {"mv": 3300, "st": 1, "readback_mv": 3291}})
    state = bp.set_la_voltage(3.3)
    assert state == LaVoltage(voltage=3.3, readback=3.291) and state.is_set
    for bad in (3300, 5.0, 0, 2.5):
        with pytest.raises(ValueError):
            bp.set_la_voltage(bad)
    assert len(t.commands) == 1


def test_get_la_voltage_reports_unset():
    bp, _ = _bp({"la_voltage": {"mv": 0, "st": 0}})
    state = bp.get_la_voltage()
    assert state.voltage is None and not state.is_set


def test_la_voltage_readback_zero_means_unknown():
    # A v2 board has no readback divider and reports readback_mv 0 while the bank is at 3.3 V.
    assert LaVoltage.from_reply({"mv": 3300, "st": 1, "readback_mv": 0}).readback is None


def test_api_base_environment_reaches_the_transport(monkeypatch):
    seen: Dict[str, Any] = {}

    def fake_open(spec, **kwargs):
        seen.update(kwargs)
        return FakeTransport()

    monkeypatch.setenv("BENCHPOD_API_BASE", "https://staging.example")
    monkeypatch.delenv("BENCHPOD_LA_VOLTAGE", raising=False)
    monkeypatch.setattr("embeddedci.benchpod.client.open_transport", fake_open)
    BenchPod("192.168.1.2", lease=False)
    assert seen["api_base"] == "https://staging.example"


# -- power, reset, eFuse state ---------------------------------------------------

def test_power_delay_is_seconds():
    bp, t = _bp()
    bp.power_on(benchpod.EXTERNAL, delay=1.5)
    bp.power_off()
    assert t.power == [(2, True, 1500), (1, False, 0)]
    with pytest.raises(ValueError):
        bp.power_on(delay=-1)
    with pytest.raises(ValueError):
        bp.power_on(3)


def test_target_status_and_power_status_are_typed():
    bp, _ = _bp({
        "target_status": {"efuse1": {"enabled": 1, "fault": 0, "valid": 1},
                          "efuse2": {"enabled": 0, "fault": 1, "valid": 1},
                          "status_supported": True},
        "power_status": {"internal": {"ok": True, "bus_mv": 5012, "current_ua": 123400},
                         "external": {"ok": False, "bus_mv": 0, "current_ua": 0}},
    })
    ts = bp.target_status()
    assert isinstance(ts, TargetStatus)
    assert ts.efuse(1).enabled and not ts.efuse(1).fault and ts.efuse(benchpod.EXTERNAL).fault
    ps = bp.power_status()
    assert isinstance(ps, PowerStatus)
    assert ps.rail(1).bus_voltage == pytest.approx(5.012)
    assert ps.internal.current == pytest.approx(0.1234)
    assert not ps.external.ok


def test_reset_line():
    bp, t = _bp({"nrst": lambda req: {"supported": True, "asserted": bool(req.get("assert"))}})
    assert bp.reset_target(pulse=0.05) == ResetState(asserted=False)
    assert t.commands[-1] == {"cmd": "nrst", "pulse_ms": 50}
    assert bp.set_reset(True).asserted is True
    assert t.commands[-1] == {"cmd": "nrst", "assert": True}
    bp.reset_state()
    assert t.commands[-1] == {"cmd": "nrst"}
    with pytest.raises(ValueError):
        bp.reset_target(pulse=0)


def test_usb_cc():
    bp, _ = _bp({"usb_cc": {"supported": True, "cc1_mv": 420, "cc2_mv": 0, "orientation": "cc1",
                            "advertised": "1.5A", "advertised_ma": 1500}})
    cc = bp.usb_cc()
    assert isinstance(cc, UsbCcStatus)
    assert cc.orientation == "cc1" and cc.advertised_current == 1.5 and cc.cc1_voltage == 0.42


# -- bias resistors ------------------------------------------------------------

def test_pullups_and_pulldowns_are_not_interchangeable():
    bp, t = _bp({"la": lambda req: {"la": req.get("la"), "pullup": 1, "ohms": "10k",
                                    "pull": "down" if req.get("la") in (7, 8) else "up",
                                    "pullups_available": 1}})
    with pytest.raises(ValueError, match="pull-DOWN"):
        bp.enable_pullup(7)
    with pytest.raises(ValueError, match="pull-UP"):
        bp.enable_pulldown(2)
    with pytest.raises(ValueError, match="LA1-LA8"):
        bp.enable_pullup(9)
    assert t.commands == []
    bp.enable_pullup(1, 2)
    bp.enable_pulldown(8)
    assert t.commands == [{"cmd": "la", "la": 1, "pullup": "on"},
                          {"cmd": "la", "la": 2, "pullup": "on"},
                          {"cmd": "la", "la": 8, "pullup": "on"}]
    state = bp.pull_state(8)
    assert isinstance(state, PullState) and state.direction == "down" and state.enabled


def test_enabled_pulls_decodes_the_mask():
    bp, _ = _bp({"la": {"la_pullup_mask": 0b10000101, "pullups_available": 1}})
    assert bp.enabled_pulls() == [1, 3, 8]


# -- analog --------------------------------------------------------------------

def test_analog_paths_take_canonical_names():
    bp, t = _bp({"analog_path": {"path": "dac_5v", "u55": 3, "u58": 0}})
    with pytest.raises(ValueError, match="path"):
        bp.analog_path("5v")  # type: ignore[arg-type]
    state = bp.analog_path("dac_5v")
    assert state == AnalogPathState(path="dac_5v", dac_mux_register=3, cal_relay_register=0)


def test_dac_output_reports_achieved_volts():
    bp, t = _bp({"dac_out": lambda req: {"path": req["path"], "mv": 2498 if "volts" in req else 0,
                                          "code": 127 if "volts" in req else -1}})
    out = bp.dac_output("5v", volts=2.5)
    assert out == DacOutput(path="5v", voltage=2.498, code=127)
    assert t.commands[-1] == {"cmd": "dac_out", "path": "5v", "volts": 2.5}
    routed = bp.dac_output("12v")
    assert routed.voltage is None and routed.code is None
    # The firmware names the analog path (dac_5v); the result uses dac_output's own vocabulary.
    assert DacOutput.from_reply({"path": "dac_5v", "mv": 2502, "code": 128}).path == "5v"
    assert DacOutput.from_reply({"path": "off", "mv": 0, "code": -1}).path == "off"
    with pytest.raises(ValueError):
        bp.dac_output("off", volts=1.0)


def test_adc_read_is_typed():
    bp, t = _bp({"adc_read": {"source": "cal1", "mv": 1234, "count": 40000, "span": 3}})
    reading = bp.adc_read("cal1")
    assert isinstance(reading, AdcReading)
    assert reading.voltage == pytest.approx(1.234) and reading.span == 3
    with pytest.raises(ValueError):
        bp.adc_read("sma")  # type: ignore[arg-type]


def test_capture_adc_routes_the_source_first():
    bp, t = _bp({"analog_path": {"path": "cal1"}})
    cap = bp.capture_adc(2, source="cal1", sample_rate_hz=100_000)
    assert t.commands[0] == {"cmd": "analog_path", "path": "cal1"}
    assert t.commands[1] == {"cmd": "capture", "samples": 2, "sample_rate_mhz": 0.1}
    assert cap.source == "cal1" and len(cap) == 2


def test_capture_adc_leaves_routing_alone_by_default():
    bp, t = _bp()
    cap = bp.capture_adc(2)
    assert [c["cmd"] for c in t.commands] == ["capture"] and cap.source == ""


# -- UART, stepper, CAN, low-level ----------------------------------------------

def test_power_cycle_capture_window_must_cover_the_power_on():
    bp, t = _bp()
    with pytest.raises(ValueError, match="exceed delay"):
        bp.power_cycle_and_capture(rx=5, tx=4, delay=2.0, duration=1.0)
    assert t.power == []


def test_la_step_delay_is_seconds():
    bp, t = _bp()
    bp.la_step(3, steps=200, delay=0.0005, dir_la=4, direction=1)
    assert t.commands[-1] == {"cmd": "la", "la": 3, "steps": 200, "delay_us": 500,
                              "dir_la": 4, "direction": 1}
    with pytest.raises(ValueError):
        bp.la_step(3, steps=10, delay=0)


def test_can_read_returns_frames():
    bp, t = _bp({"can_read": {"frames": [{"id": 0x123, "data": [1, 2]}], "overflow": 2}})
    res = bp.can_read(max_frames=4)
    assert t.commands[-1] == {"cmd": "can_read", "max": 4}
    assert res.overflow == 2 and res.frames[0].id == 0x123 and res.frames[0].data == b"\x01\x02"
    with pytest.raises(ValueError):
        bp.can_write(0x10, list(range(9)))
    with pytest.raises(ValueError):
        bp.can_config(mode="loopback")  # type: ignore[arg-type]


def test_lowlevel_controls():
    bp, t = _bp()
    bp.lowlevel.dac_mux(ctrl1="5v", ctrl2="off")
    assert t.commands[-1] == {"cmd": "dac_mux", "ctrl1_en": 1, "ctrl1_sel": 1, "ctrl2_en": 0}
    bp.lowlevel.dac_set(200, divider=240)
    assert t.commands[-1] == {"cmd": "dac_set", "value": 200, "divider": 240}
    with pytest.raises(ValueError):
        bp.lowlevel.cal_switch(cal1=True, cal2=True)
    with pytest.raises(ValueError):
        bp.lowlevel.dac_set(256)


def test_deep_replay_refused_on_an_image_without_it():
    class ImageFake(FakeTransport):
        def __init__(self, caps):
            super().__init__()
            self.caps = caps
            self.uploads = []

        def status(self):
            return {"board": "stm32h563", "adc_bits": 16, "caps": self.caps}

        def load_replay(self, *, data, replay, psram=False):
            self.uploads.append(psram)
            return {"samples": replay["samples"]}

    loop = ImageFake(["dac", "dac_replay", "dac_control_loop"])
    bp = BenchPod(transport=loop, lease=False)
    bp.replay([1.0] * 2048, dac_path="5v")          # shallow: fine on the loop image
    with pytest.raises(benchpod.BenchPodError, match="DEEP_REPLAY"):
        bp.replay([1.0] * 4096, dac_path="5v", switch_image=False)
    assert loop.uploads == [False]                   # nothing uploaded for the refused replay

    deep = ImageFake(["dac", "dac_replay", "dac_deep_replay"])
    BenchPod(transport=deep, lease=False).replay([1.0] * 4096, dac_path="5v")
    assert deep.uploads == [True]


def test_command_needs_a_cmd():
    bp, _ = _bp()
    with pytest.raises(ValueError):
        bp.command({"samples": 4})


def test_decode_rejects_unknown_protocol():
    bp, _ = _bp()
    with pytest.raises(ValueError, match="protocol"):
        bp.decode([0, 1], "can")  # type: ignore[arg-type]
