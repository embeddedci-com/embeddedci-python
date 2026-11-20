"""UnsupportedFeatureError and the can_* capability checks on BenchPod.

The pod refuses an unknown command with BP0002 and an analog command on the digital board with
BP0046; their old texts decide nothing."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import pytest

from embeddedci.benchpod import BenchPod, BenchPodError, FirmwareError, UnsupportedFeatureError
from embeddedci.benchpod.errors import firmware_error
from embeddedci.benchpod.protocol import Reply, raise_for_status

NO_ANALOG = ("this BenchPod has no analog front end (digital board): no DAC, ADC or analog outputs. "
             "Restart the pod after fitting an analog add-on")


class CapsPod:
    """A pod that only answers status, with the given capability names."""

    def __init__(self, caps: List[str], *, analog: Optional[bool] = None, gateware: int = 59,
                 hw: Optional[List[str]] = None) -> None:
        self.caps = caps
        self.analog = analog
        self.gateware = gateware
        self.hw = ["usb_cc"] if hw is None else hw
        self.commands: List[dict] = []

    def status(self) -> Dict[str, Any]:
        st: Dict[str, Any] = {"board": "stm32h563", "adc_bits": 16, "version": "4.0.0", "api": 2,
                              "min_client_api": 2, "features_rev": 10, "gateware": self.gateware,
                              "fpga_image": "loop", "hw": self.hw, "nrst_pin": "nrst_pin" in self.caps}
        if self.analog is not None:
            st["analog"] = self.analog
        return st

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass

    def command(self, req: dict) -> Any:
        if req["cmd"] in ("wiring_list", "la_dir"):   # the connect's own
            return {}
        self.commands.append(req)
        raise AssertionError(f"nothing should reach the pod: {req}")


def test_unknown_command_is_an_unsupported_feature():
    err = firmware_error("", cmd="can_start", code="BP0002", values={"cmd": "can_start"})
    assert type(err) is UnsupportedFeatureError
    assert err.feature == "can_start" and err.cmd == "can_start"
    assert isinstance(err, FirmwareError)
    with pytest.raises(UnsupportedFeatureError):
        raise_for_status(Reply(status="error", code="BP0002", args={"cmd": "sensor_start"}),
                         cmd="sensor_start")


def test_the_digital_board_refusing_analog_is_an_unsupported_feature():
    err = firmware_error("", cmd="capture", code="BP0046", values={"cmd": "capture"})
    assert type(err) is UnsupportedFeatureError and err.feature == "analog"


def test_the_old_texts_are_not_classified():
    for text in ("unknown cmd", NO_ANALOG, "la voltage not set", "unknown target"):
        assert type(firmware_error(text)) is FirmwareError, text


def test_a_missing_capability_raises_before_sending():
    pod = CapsPod(["la"], gateware=44)                 # SPI master needs gateware v45
    bp = BenchPod(transport=pod, lease=False)
    with pytest.raises(UnsupportedFeatureError) as info:
        bp.open_spi(sck=1, mosi=2, miso=3, cs=4)
    err = info.value
    assert err.feature == "spi_master" and err.firmware_version == "4.0.0"
    assert "'spi_master' capability is missing" in str(err)
    assert isinstance(err, BenchPodError)  # what it was before
    assert pod.commands == []


def test_can_properties_follow_the_capabilities():
    full = BenchPod(transport=CapsPod(["la_pins", "capture_trigger", "spi_master", "power_profile",
                                       "calibrate", "current_out", "nrst_pin", "can"]), lease=False)
    assert full.can_gpio and full.can_trigger and full.can_spi and full.can_profile_power
    assert full.can_calibrate and full.can_current_out and full.can_reset_target and full.can_analog
    assert full.supports("can") and full.supports("spi_master")

    bare = BenchPod(transport=CapsPod([], analog=False, gateware=34), lease=False)
    assert not any((bare.can_trigger, bare.can_spi, bare.can_calibrate, bare.can_current_out,
                    bare.can_reset_target))
    assert bare.can_gpio and bare.can_profile_power and bare.supports("can")
    assert not bare.supports("no_such_flag")


def test_can_analog_is_false_only_on_the_digital_board():
    assert BenchPod(transport=CapsPod([], analog=True), lease=False).can_analog
    assert BenchPod(transport=CapsPod([]), lease=False).can_analog  # no analog flag: analog
    assert not BenchPod(transport=CapsPod([], analog=False), lease=False).can_analog


@pytest.mark.parametrize("call", [lambda p: p.current_out(0.012), lambda p: p.calibrate()],
                         ids=["current_out", "calibrate"])
def test_digital_board_names_the_missing_front_end_not_old_firmware(call):
    pod = BenchPod(transport=CapsPod([], analog=False), lease=False)
    with pytest.raises(UnsupportedFeatureError, match="digital-only board") as err:
        call(pod)
    assert err.value.feature == "analog" and "newer" not in str(err.value)
    assert pod._transport.commands == []


@pytest.mark.parametrize("analog,outcome", [(True, "passed"), (None, "passed"), (False, "skipped")],
                         ids=["analog board", "older firmware", "digital board"])
def test_analog_capability_marker_skips_only_on_the_digital_board(pytester, analog, outcome):
    tests_dir = os.path.dirname(__file__)
    pytester.makeconftest(f"""
import sys

import pytest

sys.path.insert(0, {tests_dir!r})
from test_unsupported_feature import CapsPod
from embeddedci.benchpod import BenchPod


@pytest.fixture(scope="session")
def benchpod():
    return BenchPod(transport=CapsPod([], analog={analog!r}), lease=False)
""")
    pytester.makepyfile("""
import pytest


@pytest.mark.benchpod_capability("analog")
def test_needs_the_analog_front_end(benchpod):
    pass
""")
    result = pytester.runpytest("-p", "no:cacheprovider", "-rs")
    result.assert_outcomes(**{outcome: 1})
    if outcome == "skipped":
        result.stdout.fnmatch_lines(["*digital-only board*"])
