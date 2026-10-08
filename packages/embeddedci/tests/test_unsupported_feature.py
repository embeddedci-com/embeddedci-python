"""UnsupportedFeatureError and the can_* capability checks on BenchPod.

The pod's texts are the firmware's: ``unknown cmd`` (command_handler.c) and the digital board's
analog refusal (cmd_gate.c cmd_gate_check)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from embeddedci.benchpod import BenchPod, BenchPodError, FirmwareError, UnsupportedFeatureError
from embeddedci.benchpod.errors import firmware_error
from embeddedci.benchpod.protocol import Reply, raise_for_status

NO_ANALOG = ("this BenchPod has no analog front end (digital board): no DAC, ADC or analog outputs. "
             "Restart the pod after fitting an analog add-on")


class CapsPod:
    """A pod that only answers status, with the given capability names."""

    def __init__(self, caps: List[str], *, analog: Optional[bool] = None) -> None:
        self.caps = caps
        self.analog = analog
        self.commands: List[dict] = []

    def status(self) -> Dict[str, Any]:
        st: Dict[str, Any] = {"board": "stm32h563", "adc_bits": 16, "version": "3.1.0", "caps": self.caps}
        if self.analog is not None:
            st["analog"] = self.analog
        return st

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass

    def command(self, req: dict) -> Any:
        self.commands.append(req)
        raise AssertionError(f"nothing should reach the pod: {req}")


def test_unknown_cmd_is_an_unsupported_feature():
    err = firmware_error("unknown cmd", cmd="can_config")
    assert type(err) is UnsupportedFeatureError
    assert err.feature == "can_config" and err.cmd == "can_config"
    # Still what it was before: a FirmwareError with the pod's text.
    assert isinstance(err, FirmwareError) and err.firmware_message == "unknown cmd"
    with pytest.raises(UnsupportedFeatureError):
        raise_for_status(Reply(status="error", message="unknown cmd"), cmd="sensor_start")


def test_the_digital_board_refusing_analog_is_an_unsupported_feature():
    err = firmware_error(NO_ANALOG, cmd="capture")
    assert type(err) is UnsupportedFeatureError and err.feature == "analog"


def test_other_refusals_are_not_unsupported():
    for text in ("la voltage not set", "unknown target", "unknown cmd here"):
        assert not isinstance(firmware_error(text), UnsupportedFeatureError), text


def test_a_missing_capability_raises_before_sending():
    pod = CapsPod(["la"])
    bp = BenchPod(transport=pod, lease=False)
    with pytest.raises(UnsupportedFeatureError) as info:
        bp.gpio(9)
    err = info.value
    assert err.feature == "la_pins" and err.firmware_version == "3.1.0"
    assert "'la_pins' capability is missing" in str(err)
    assert isinstance(err, BenchPodError)  # what it was before
    assert pod.commands == []


def test_can_properties_follow_the_capabilities():
    full = BenchPod(transport=CapsPod(["la_pins", "capture_trigger", "spi_master", "power_profile",
                                       "calibrate", "current_out", "nrst_pin", "can"]), lease=False)
    assert full.can_gpio and full.can_trigger and full.can_spi and full.can_profile_power
    assert full.can_calibrate and full.can_current_out and full.can_reset_target and full.can_analog
    assert full.supports("can") and full.supports("spi_master")

    bare = BenchPod(transport=CapsPod([]), lease=False)
    assert not any((bare.can_gpio, bare.can_trigger, bare.can_spi, bare.can_profile_power,
                    bare.can_calibrate, bare.can_current_out, bare.can_reset_target))
    assert not bare.supports("can") and not bare.supports("no_such_flag")


def test_can_analog_is_false_only_on_the_digital_board():
    assert BenchPod(transport=CapsPod([], analog=True), lease=False).can_analog
    assert BenchPod(transport=CapsPod([]), lease=False).can_analog  # older firmware: analog boards
    assert not BenchPod(transport=CapsPod([], analog=False), lease=False).can_analog
