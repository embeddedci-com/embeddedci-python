"""GPIO on the LA pins and pin ownership, against a fake pod that speaks the firmware contract."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from embeddedci.benchpod import (
    BenchPod,
    BenchPodError,
    FirmwareError,
    GpioPin,
    LaPinState,
    PinConflictError,
    PullConflictError,
    Signal,
    TriggerTimeout,
    Wiring,
)
from embeddedci.benchpod.errors import classify_firmware_error


class PinPod:
    """Owns 12 pins like the firmware: gpio claims, conflicts, pull rules, live levels."""

    def __init__(self, *, caps: Optional[List[str]] = None) -> None:
        self.caps = ["la", "la_pins", "gpio_read"] if caps is None else caps
        self.commands: List[dict] = []
        self.function = {la: "none" for la in range(1, 13)}
        self.mode: Dict[int, Optional[str]] = {la: None for la in range(1, 13)}
        self.level: Dict[int, Optional[int]] = {la: None for la in range(1, 13)}
        self.pull_on = {la: False for la in range(1, 9)}
        self.inputs = 0  # levels driven by the "DUT" on input pins
        self.uart_starts: List[tuple] = []

    # -- transport surface --
    def status(self) -> Dict[str, Any]:
        return {"board": "stm32h563", "adc_bits": 16, "version": "0.4.0", "caps": self.caps}

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass

    def uart_proxy_start(self, rx: int, tx: int, baud: int):
        for la in (rx, tx):
            if self.function[la] != "none":
                raise FirmwareError(self._conflict(la), cmd="uart_proxy_start")
        self.uart_starts.append((rx, tx, baud))
        raise AssertionError("not needed")

    def command(self, req: dict) -> Any:
        self.commands.append(req)
        cmd = req["cmd"]
        if cmd == "la_pins":
            return {"pins": [self._entry(la) for la in range(1, 13)], "levels": self._levels()}
        if cmd == "gpio":
            return self._gpio(req)
        if cmd == "la":
            return {"la": req["la"], "steps": req.get("steps"), "status": "started"}
        return {}

    # -- behaviour --
    def _conflict(self, la: int) -> str:
        fn = self.function[la]
        how = (f'release it with {{"cmd":"gpio","la":{la},"mode":"off"}}' if fn == "gpio"
               else "stop the uart proxy first")
        return f"pin conflict: LA{la} is in use by {fn}; {how}"

    def _entry(self, la: int) -> Dict[str, Any]:
        pull = None
        if la <= 8:
            pull = {"dir": "down" if la in (7, 8) else "up", "ohms": "10k", "on": self.pull_on[la]}
        return {"la": la, "function": self.function[la], "gpio": self.mode[la], "level": self.level[la],
                "pull": pull}

    def _levels(self) -> int:
        mask = self.inputs
        for la in range(1, 13):
            if self.mode[la] in ("output", "open_drain") and self.level[la] is not None:
                mask = (mask & ~(1 << (la - 1))) | (self.level[la] << (la - 1))
        return mask

    def _gpio(self, req: dict) -> Any:
        if "mode" not in req and "level" not in req:
            return {"levels": self._levels(), "pins": [self._entry(la) for la in range(1, 13)]}
        raw = req["la"]
        las = list(range(1, 13)) if raw == "all" else (raw if isinstance(raw, list) else [raw])
        mode = req.get("mode")
        if mode == "off":
            for la in las:
                if self.function[la] == "gpio":
                    self.function[la], self.mode[la], self.level[la] = "none", None, None
            return {"pins": [self._entry(la) for la in las]}
        if mode is not None:
            for la in las:
                if self.function[la] not in ("none", "gpio"):
                    raise FirmwareError(self._conflict(la), cmd="gpio")
                if mode == "open_drain" and la in (7, 8) and self.pull_on[la]:
                    raise FirmwareError(
                        f"pull conflict: LA{la} has its 10k pull-down engaged, which gpio open_drain can't "
                        "work with (a released line would read low)", cmd="gpio")
            for la in las:
                self.function[la], self.mode[la] = "gpio", mode
                self.level[la] = None if mode == "input" else req.get("level", 1 if mode == "open_drain" else 0)
            return {"pins": [self._entry(la) for la in las]}
        for la in las:
            if self.mode[la] not in ("output", "open_drain"):
                raise FirmwareError(f"LA{la} is not a gpio output (function {self.function[la]})", cmd="gpio")
            self.level[la] = req["level"]
        return {"pins": [self._entry(la) for la in las]}


def _bp(pod: Optional[PinPod] = None, **kwargs: Any):
    pod = pod or PinPod()
    return BenchPod(transport=pod, lease=False, **kwargs), pod


def test_configure_set_read_release():
    bp, pod = _bp()
    pin = bp.gpio(9)
    assert isinstance(pin, GpioPin)
    assert pod.commands[-1] == {"cmd": "gpio", "la": 9, "mode": "output"}
    pin.high()
    assert pod.commands[-1] == {"cmd": "gpio", "la": 9, "level": 1} and pin.read() == 1
    bp.set_gpio([9, 10], 0) if False else None
    bp.gpio(10, "open_drain")
    bp.set_gpio([9, 10], 0)
    assert pod.commands[-1] == {"cmd": "gpio", "la": [9, 10], "level": 0}
    states = {p.la: p for p in bp.la_pins()}
    assert states[9] == LaPinState(la=9, function="gpio", gpio="output", level=0, raw={})
    assert states[10].gpio == "open_drain" and not states[1].in_use and states[7].pull == "down"
    bp.release_gpio(9)
    assert pod.commands[-1] == {"cmd": "gpio", "la": 9, "mode": "off"}
    bp.release_gpio()
    assert pod.commands[-1] == {"cmd": "gpio", "la": "all", "mode": "off"}
    assert not any(p.in_use for p in bp.la_pins())


def test_arguments_are_checked_before_sending():
    bp, pod = _bp()
    with pytest.raises(ValueError, match="mode"):
        bp.gpio(9, "tristate")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="level"):
        bp.gpio(9, "input", level=1)
    with pytest.raises(ValueError, match="level must be 0 or 1"):
        bp.set_gpio(9, 2)
    with pytest.raises(ValueError, match="LA pin 1-12"):
        bp.gpio(13)
    assert pod.commands == []


def test_pin_conflicts_are_their_own_error():
    bp, pod = _bp()
    bp.gpio(4)
    with pytest.raises(PinConflictError) as ei:
        bp.open_uart(rx=3, tx=4)
    assert ei.value.la == 4 and ei.value.function == "gpio"
    assert 'release it with {"cmd":"gpio","la":4,"mode":"off"}' in str(ei.value)
    assert isinstance(ei.value, FirmwareError)


def test_several_pins_are_claimed_as_a_group():
    bp, pod = _bp()
    a, b = bp.gpio_pins([9, 10], "open_drain")
    assert (a.la, b.la) == (9, 10)
    assert pod.commands[-1] == {"cmd": "gpio", "la": [9, 10], "mode": "open_drain"}
    assert [p.gpio for p in bp.configure_gpio([1, 2], "input")] == ["input", "input"]


def test_a_conflict_in_a_group_claims_nothing():
    pod = PinPod()
    pod.function[5] = "uart_rx"  # a UART session already owns LA5
    bp, _ = _bp(pod)
    with pytest.raises(PinConflictError) as ei:
        bp.gpio_pins([9, 5])
    assert ei.value.la == 5 and ei.value.function == "uart_rx"
    # the pod validates the whole group first, so LA9 is untouched — unlike a loop over gpio().
    assert pod.function[9] == "none"


def test_pull_conflicts_are_their_own_error():
    pod = PinPod()
    pod.pull_on[7] = True
    bp, _ = _bp(pod)
    with pytest.raises(PullConflictError) as ei:
        bp.gpio(7, "open_drain")
    assert ei.value.la == 7 and "pull-down" in str(ei.value)


def test_signals_by_name_with_active_low():
    wiring = Wiring(signals=[Signal("RESET_N", 9, "output", active_low=True), Signal("READY", 10)])
    bp, pod = _bp(wiring=wiring)
    reset = bp.signal("reset_n")
    reset.configure()                                   # output, starts inactive = high
    assert pod.commands[-1] == {"cmd": "gpio", "la": 9, "mode": "output", "level": 1}
    reset.activate()
    assert pod.commands[-1]["level"] == 0 and reset.is_active()
    ready = bp.signal("READY")
    ready.configure()
    assert pod.commands[-1] == {"cmd": "gpio", "la": 10, "mode": "input"}
    pod.inputs = 1 << 9
    assert ready.read() == 1 and bp.wait_for_level("READY", 1, timeout=0)
    reset.pulse(0.002, count=3)
    assert pod.commands[-1] == {"cmd": "la", "la": 9, "steps": 3, "delay_us": 2000}


def test_wait_for_level_times_out():
    bp, pod = _bp()
    assert bp.wait_for_level(5, 1, timeout=0.02, poll=0.005) is False
    assert sum(1 for c in pod.commands if c == {"cmd": "gpio"}) >= 2


def test_pin_levels_fall_back_to_a_capture_without_gpio_read():
    pod = PinPod(caps=["la", "la_pins"])
    pod.stream_chunks = lambda req: iter([{"status": "ok", "data": [0, 0b100000000], "la_rate_hz": 1e6,
                                           "more": False}])  # type: ignore[attr-defined]
    bp, _ = _bp(pod)
    levels = bp.pin_levels()
    assert levels[9] == 1 and levels[1] == 0 and not any(c["cmd"] == "gpio" for c in pod.commands)


def test_gpio_needs_firmware_that_has_it():
    bp, pod = _bp(PinPod(caps=["la"]))
    with pytest.raises(BenchPodError, match="la_pins.*firmware 0.4.0"):
        bp.gpio(9)
    assert pod.commands == []


def test_error_classification():
    plain = FirmwareError("la voltage not set", cmd="gpio")
    assert classify_firmware_error(plain) is plain
    trig = classify_firmware_error(FirmwareError("trigger timeout: no rising edge on LA9 within 100 ms"))
    assert isinstance(trig, TriggerTimeout) and (trig.la, trig.edge) == (9, "rising")
    pull = classify_firmware_error(FirmwareError("pull conflict: LA8 is used by uart_rx, which can't ..."))
    assert isinstance(pull, PullConflictError) and pull.la == 8
