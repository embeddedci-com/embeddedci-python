"""Digital board v4 (firmware board_caps.h): 5 V I/O and pin 21 (``set_la_voltage(5.0)``,
:attr:`LaVoltage.vref_out`), and pulls with a direction and a strength on every channel. A pod
without the ``vref_out`` fact behaves as before: 5.0 is refused before anything is sent."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import BenchPod, FirmwareError, Wiring
from embeddedci.benchpod.errors import firmware_error


class Pod:
    def __init__(self, v4: bool, features_rev: int = 7, rev: str = "v4") -> None:
        self.v4 = v4
        self.rev = rev
        self.features_rev = features_rev
        self.sent: List[Dict[str, Any]] = []
        self.mv = 0
        self.pin21_mv = 3300
        self.five_v_ok = True
        self.pulls: Dict[int, tuple] = {}
        self.held = ""              # la query: a pull held off ("pin21_down", ...)
        self.subset = True          # firmware with la_pins "la"
        self.too_large = False      # the full la_pins listing does not fit one reply

    def status(self) -> Dict[str, Any]:
        hw = ["nrst_pin"] + (["vref_out"] if self.v4 else [])
        return {"board": "stm32h563", "adc_bits": 16, "version": "3.10.13", "features_rev": self.features_rev,
                "gateware": 58, "fpga_image": "deep", "hw": hw, "board_rev": self.rev if self.v4 else "v3"}

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass

    def _vref(self) -> Dict[str, Any]:
        src = "5v" if self.mv == 5000 else "bank"
        return {"source": src, "set_mv": 5000 if src == "5v" else (self.mv or 3300), "mv": self.pin21_mv,
                "state": "ok", "up": True}

    def command(self, req: dict) -> Any:
        if req.get("cmd") == "status":
            return self.status()
        self.sent.append(req)
        if req["cmd"] == "la_voltage":
            if "mv" in req:
                if req["mv"] == 5000 and not self.v4:
                    raise firmware_error("la voltage must be 1800 or 3300 (mv)", cmd="la_voltage")
                if req["mv"] == 5000 and not self.five_v_ok:
                    raise firmware_error("5 V did not come up on pin 21 (4120 mV); check the USB supply",
                                         cmd="la_voltage", code="BP0028", values={"mv": 4120})
                self.mv = req["mv"]
                self.pin21_mv = 4920 if self.mv == 5000 else self.mv
            out: Dict[str, Any] = {"mv": self.mv, "st": 1, "readback_mv": 3300 if self.mv == 5000 else self.mv}
            if self.v4:
                out["vref_out"] = self._vref()
            return out
        if req["cmd"] == "la_pull":
            la = req["la"]
            want = req.get("pull")   # None: a query
            if want is None:
                pass
            elif want != "off":
                self.pulls[la] = (want, "2.2k" if la in (1, 2) else "10k")
            else:
                self.pulls.pop(la, None)
            d, s = self.pulls.get(la, ("off", "none"))
            ohms = {"2.2k": "2.2k", "10k": "10k", "none": "10k"}[s]
            out = {"la": la, "ohms": ohms, "state": d, "dirs": ["up", "down"], "strength": s,
                   "strengths": ["10k", "2.2k", "both"], "pullups_available": True}
            if self.held and d != "off":
                out.update(pullup=0, state="off", held=self.held)
            return out
        if req["cmd"] == "la_pins":
            want = req.get("la") if self.subset else None
            if want is None and self.too_large:
                raise firmware_error("", cmd="la_pins", code="BP0054", values={"bytes": 1900, "limit": 1791})
            pins = [self._entry(n) for n in range(1, 15) if want is None or n in want]
            return {"pins": pins, "pull_dirs": ["up", "down"], "levels": None}
        if req["cmd"] in ("la_dir", "gpio"):   # the same entries, without pull_dirs
            las = req["la"] if isinstance(req["la"], list) else [req["la"]]
            return {"pins": [self._entry(n) for n in las]}
        raise AssertionError(req)

    @staticmethod
    def _entry(n: int) -> Dict[str, Any]:
        return {"la": n, "function": "none", "gpio": None, "level": None,
                "pull": {"dir": "up", "ohms": "10k", "on": False}}


def _bp(v4: bool, features_rev: int = 7, rev: str = "v4") -> "tuple[BenchPod, Pod]":
    pod = Pod(v4, features_rev, rev)
    return BenchPod(transport=pod, lease=False, wiring={"version": 2}), pod


def test_capabilities_say_v4():
    bp, _ = _bp(True)
    assert bp.capabilities.vref_out
    assert bp.supports("la_5v") and bp.supports("la_pull_strength")
    old, _ = _bp(False)
    assert not old.capabilities.vref_out
    assert not old.supports("la_5v") and not old.supports("la_pull_strength")


def test_board_rev_v3r4_still_counts_as_v4():
    # v3r4 = the pre-rename name of v4, what firmware 4.0.x reports
    bp, _ = _bp(True, rev="v3r4")
    assert bp.capabilities.board_rev == "v3r4"
    assert bp.supports("la_5v") and bp.capabilities.vref_out
    assert bp._has_usb_a("v3r4") and bp._has_usb_a("v4") and not bp._has_usb_a("v3r3")


def test_5v_on_v4():
    bp, pod = _bp(True)
    st = bp.set_la_voltage(5.0)
    assert pod.sent[-1] == {"cmd": "la_voltage", "mv": 5000}
    assert st.voltage == 5.0 and st.readback == 3.3
    assert st.vref_out is not None and st.vref_out.source == "5v" and st.vref_out.ok
    assert st.vref_out.measured == pytest.approx(4.92) and st.vref_out.setting == 5.0


def test_5v_refused_before_sending_on_older_boards():
    bp, pod = _bp(False)
    with pytest.raises(ValueError, match="digital board v4"):
        bp.set_la_voltage(5.0)
    assert not [r for r in pod.sent if r["cmd"] == "la_voltage"]
    # 3.3 V is as before, and there is no pin 21 in the reply
    st = bp.set_la_voltage(3.3)
    assert st.voltage == 3.3 and st.vref_out is None


def test_5v_that_did_not_come_up():
    bp, pod = _bp(True)
    pod.five_v_ok = False
    with pytest.raises(FirmwareError) as e:
        bp.set_la_voltage(5.0)
    assert e.value.code == "BP0028"


def test_other_voltages_name_5v_only_where_it_exists():
    bp, _ = _bp(True)
    with pytest.raises(ValueError, match="or 5.0"):
        bp.set_la_voltage(2.5)
    old, _ = _bp(False)
    with pytest.raises(ValueError) as e:
        old.set_la_voltage(2.5)
    assert "5.0" not in str(e.value)


def test_pull_strength_reported():
    bp, _ = _bp(True)
    st = bp.set_pull(1, "up")
    assert st.state == "up" and st.strength == "2.2k" and st.ohms == "2.2k"
    st = bp.set_pull(9, "down")   # every channel on v4, both ways
    assert st.state == "down" and st.supported == ("up", "down") and st.strength == "10k"
    st = bp.set_pull(9, "off")
    assert st.state == "off" and st.strength == "none"


def test_la_pins_takes_the_directions_said_once():
    bp, _ = _bp(True)
    pins = bp.la_pins()
    assert len(pins) == 14 and all(p.pull_dirs == ("up", "down") for p in pins)


def test_la_pins_subset():
    bp, pod = _bp(True)
    pins = bp.la_pins(la=[9, 3])
    assert pod.sent[-1] == {"cmd": "la_pins", "la": [3, 9]}
    assert [p.la for p in pins] == [3, 9] and pins[0].pull_dirs == ("up", "down")
    # Older firmware ignores "la" and lists all 14: the SDK keeps the ones asked for.
    pod.subset = False
    assert [p.la for p in bp.la_pins(la=5)] == [5]
    # Without la the request is as before.
    bp.la_pins()
    assert pod.sent[-1] == {"cmd": "la_pins"}


def test_la_pins_too_large_reads_in_two_parts():
    bp, pod = _bp(True)
    pod.too_large = True
    pins = bp.la_pins()
    assert [p.la for p in pins] == list(range(1, 15))
    assert pod.sent[-2:] == [{"cmd": "la_pins", "la": [1, 2, 3, 4, 5, 6, 7]},
                             {"cmd": "la_pins", "la": [8, 9, 10, 11, 12, 13, 14]}]


def test_la_dir_and_gpio_entries_pull_both_ways_on_v4():
    bp, _ = _bp(True)
    assert bp.la_dir(9, "dut_input")[0].pull_dirs == ("up", "down")
    assert all(p.pull_dirs == ("up", "down") for p in bp._gpio_configure([4], "input", None))
    # Older boards say dirs per entry; without them an entry keeps its one direction.
    old, _ = _bp(False)
    assert old.la_dir(9, "dut_input")[0].pull_dirs == ("up",)


def test_pull_held_off():
    bp, pod = _bp(True)
    bp.set_pull(2, "up")
    pod.held = "dut_off"
    st = bp.pull_state(2)
    assert st.held == "dut_off" and not st.enabled and st.state == "off" and st.strength == "2.2k"
    pod.held = ""
    st = bp.pull_state(2)
    assert st.held is None and st.enabled


def test_profile_takes_5v():
    assert Wiring(la_mv=5000).la_voltage == 5.0
    with pytest.raises(ValueError, match="1800, 3300 or 5000"):
        Wiring(la_mv=2500)


class PagedPod(Pod):
    """Firmware with la_pins_paged (features rev 12): a reply carries at most ``page`` pins and
    says ``next``; ``from`` continues there, with or without ``la``."""

    def __init__(self, page: int = 5, features_rev: int = 12) -> None:
        super().__init__(True, features_rev)
        self.page = page
        self.bad_next = False

    def command(self, req: dict) -> Any:
        if req.get("cmd") != "la_pins":
            return super().command(req)
        self.sent.append(req)
        want = req.get("la") or list(range(1, 15))
        if self.features_rev < 12:   # older firmware ignores "from"
            start = 1
        else:
            start = req.get("from", 1)
        todo = [n for n in want if n >= start]
        out: Dict[str, Any] = {"pins": [self._entry(n) for n in todo[:self.page]],
                               "pull_dirs": ["up", "down"], "levels": len(self.sent)}
        if len(todo) > self.page:
            out["next"] = 1 if self.bad_next else todo[self.page]
        return out


def test_la_pins_pages_with_from_and_next():
    pod = PagedPod(page=5)
    bp = BenchPod(transport=pod, lease=False, wiring={"version": 2})
    assert bp.supports("la_pins_paged")
    pins = bp.la_pins()
    assert [p.la for p in pins] == list(range(1, 15))
    assert all(p.pull_dirs == ("up", "down") for p in pins)
    assert [r for r in pod.sent if r["cmd"] == "la_pins"] == [
        {"cmd": "la_pins"}, {"cmd": "la_pins", "from": 6}, {"cmd": "la_pins", "from": 11}]


def test_la_pins_pages_a_subset_with_the_same_la():
    pod = PagedPod(page=2)
    bp = BenchPod(transport=pod, lease=False, wiring={"version": 2})
    pins = bp.la_pins(la=[2, 4, 6, 8, 10])
    assert [p.la for p in pins] == [2, 4, 6, 8, 10]
    assert [r for r in pod.sent if r["cmd"] == "la_pins"] == [
        {"cmd": "la_pins", "la": [2, 4, 6, 8, 10]},
        {"cmd": "la_pins", "la": [2, 4, 6, 8, 10], "from": 6},
        {"cmd": "la_pins", "la": [2, 4, 6, 8, 10], "from": 10}]


def test_la_pins_listing_that_fits_is_one_round_trip():
    pod = PagedPod(page=14)
    bp = BenchPod(transport=pod, lease=False, wiring={"version": 2})
    assert len(bp.la_pins()) == 14
    assert [r for r in pod.sent if r["cmd"] == "la_pins"] == [{"cmd": "la_pins"}]


def test_la_pins_stops_when_next_does_not_move_forward():
    pod = PagedPod(page=5)
    pod.bad_next = True
    bp = BenchPod(transport=pod, lease=False, wiring={"version": 2})
    pins = bp.la_pins()
    assert [p.la for p in pins] == [1, 2, 3, 4, 5]
    assert len([r for r in pod.sent if r["cmd"] == "la_pins"]) == 1


def test_la_pins_ignores_next_without_the_feature():
    # Firmware before la_pins_paged never says next; if a reply did, the SDK would not page.
    pod = PagedPod(page=5, features_rev=11)
    bp = BenchPod(transport=pod, lease=False, wiring={"version": 2})
    assert not bp.supports("la_pins_paged")
    assert [p.la for p in bp.la_pins()] == [1, 2, 3, 4, 5]
    assert [r for r in pod.sent if r["cmd"] == "la_pins"] == [{"cmd": "la_pins"}]
