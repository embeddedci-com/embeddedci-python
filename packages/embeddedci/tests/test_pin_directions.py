"""Pin directions by default: the wiring profile's DUT direction per role and signal (dut_dirs),
sent to the pod with la_dir on connect and on a new profile (firmware 3.10.4+)."""

from __future__ import annotations

import pytest

from embeddedci.benchpod import BenchPod, DirectionConflictError, Wiring
from embeddedci.benchpod.wiring import Signal

from test_i2c_master import BusPod


class OldPod(BusPod):
    def __init__(self, version: str) -> None:
        super().__init__()
        self.version = version

    def status(self):
        st = super().status()
        st["version"] = self.version
        return st


def _sent(fake):
    return [c for c in fake.commands if c["cmd"] == "la_dir"]


# -- the profile ----------------------------------------------------------------------------------

def test_defaults_cover_every_wired_role():
    assert Wiring().pin_dut_dirs() == {5: "dut_output", 4: "dut_input", 1: "bidir", 2: "bidir",
                                       11: "dut_input", 12: "bidir"}


def test_dut_dirs_override_roles_and_signals_case_insensitively():
    w = Wiring(signals=(Signal("READY", 9), Signal("TRIG", 10, "output")),
               dut_dirs={"uart_rx": "bidir", "ready": "dut_input"})
    assert w.dut_dir("uart_rx") == "bidir" and w.dut_dir("READY") == "dut_input"
    assert w.dut_dir("TRIG") == "dut_input" and w.dut_dir("uart_tx") == "dut_input"
    assert w.pin_dut_dirs()[9] == "dut_input" and w.pin_dut_dirs()[5] == "bidir"


@pytest.mark.parametrize("dirs, msg", [
    ({"uart_rx": "output"}, "dut_dirs.uart_rx must be one of dut_input, dut_output, bidir"),
    ({"uart_rx": "unknown"}, "dut_dirs.uart_rx must be one of"),
    ({"NOPE": "bidir"}, "'NOPE' is not a wiring role or a signal name"),
])
def test_dut_dirs_are_validated(dirs, msg):
    with pytest.raises(ValueError, match=msg):
        Wiring(dut_dirs=dirs)


def test_dut_dirs_round_trip_and_old_profiles_stay_valid():
    w = Wiring.from_dict({"signals": [{"name": "READY", "la": 9}], "dut_dirs": {"READY": "bidir"}})
    assert w.to_dict()["dut_dirs"] == {"READY": "bidir"}
    assert Wiring.from_dict(w.to_dict()) == w
    assert Wiring.from_dict({"uart_rx": 5}).dut_dirs == {}
    assert Wiring().to_dict()["dut_dirs"] == {}


def test_describe_shows_the_direction():
    text = Wiring().describe()
    assert "uart_rx" in text and "dut_output" in text.splitlines()[5]


# -- pushed to the pod ------------------------------------------------------------------------------

def test_connect_sends_the_wiring_directions():
    fake = BusPod()
    bp = BenchPod(transport=fake, wiring=Wiring(signals=(Signal("READY", 9),)))
    assert bp.pin_directions_status == "sent"
    assert _sent(fake) == [
        {"cmd": "la_dir", "la": [3, 6, 7, 8, 10, 13, 14], "dir": "unknown"},
        {"cmd": "la_dir", "la": [4, 11], "dir": "dut_input"},
        {"cmd": "la_dir", "la": [1, 2, 12], "dir": "bidir"},
        {"cmd": "la_dir", "la": [5, 9], "dir": "dut_output"},
    ]
    # The protection is on: a push-pull GPIO on the DUT's TX line is refused.
    with pytest.raises(DirectionConflictError):
        bp.gpio(5, "output")
    with pytest.raises(DirectionConflictError):
        bp.signal("READY").configure("output")


@pytest.mark.parametrize("version", ["3.10.3", "3.9.6", ""])
def test_older_firmware_is_skipped_quietly(version):
    fake = OldPod(version)
    bp = BenchPod(transport=fake, wiring=Wiring())
    assert _sent(fake) == []
    assert bp.pin_directions_status.startswith("not sent: this pod's firmware")
    assert "la_dir (3.10.4+)" in bp.pin_directions_status


def test_a_new_profile_is_sent_and_moves_marks():
    fake = BusPod()
    bp = BenchPod(transport=fake, wiring=Wiring())
    fake.commands.clear()
    bp.wiring = Wiring(uart_rx=9, uart_tx=10)
    assert bp.pin_directions_status == "sent"
    assert fake.dirs[5] == "unknown" and fake.dirs[9] == "dut_output" and fake.dirs[10] == "dut_input"


def test_a_driven_pin_does_not_fail_the_connect_or_the_assignment():
    fake = BusPod()
    bp = BenchPod(transport=fake, wiring=Wiring(uart_rx=None))
    bp.gpio(9, "output")  # LA9 unwired: a push-pull GPIO is fine
    bp.wiring = Wiring(uart_rx=9)  # now the profile says LA9 is the DUT's TX
    assert "direction conflict: LA9" in bp.pin_directions_status
    # The rest still went out, DUT outputs last.
    assert fake.dirs[4] == "dut_input" and fake.dirs[1] == "bidir"
    with pytest.raises(DirectionConflictError):
        bp.apply_pin_directions()
