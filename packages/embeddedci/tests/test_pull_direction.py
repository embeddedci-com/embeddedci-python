"""Pull resistors by direction (``set_pull(la, "up"|"down"|"off")``, firmware feature
``la_pull_dir``): what the SDK sends to new firmware, the ``pullup`` fallback for older firmware,
the refusals (BP0023 / BP0024) and the wiring profile's ``pulls`` map (firmware
command_handler_pins.c handle_la, la_pins.c s_pulls_v3)."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import BenchPod, PullDirectionError, PullState, Wiring
from embeddedci.benchpod.constants import PULL_TABLE
from embeddedci.benchpod.errors import firmware_error


class Pod:
    """A pod with today's resistors. ``features_rev`` 3 has la_pull_dir (takes ``pull``), 2 does not
    (takes ``pullup`` only and refuses a query on LA9-LA14, as firmware 3.10.11 does)."""

    def __init__(self, features_rev: int) -> None:
        self.features_rev = features_rev
        self.on: Dict[int, bool] = {}
        self.sent: List[Dict[str, Any]] = []

    def status(self) -> Dict[str, Any]:
        return {"board": "stm32h563", "adc_bits": 16, "version": "3.10.11", "la_voltage": 3.3,
                "features_rev": self.features_rev, "gateware": 58, "fpga_image": "deep",
                "hw": ["nrst_pin"]}

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass

    def command(self, req: dict) -> Any:
        if req.get("cmd") == "status":
            return self.status()
        assert req["cmd"] == "la", req
        self.sent.append(req)
        if "la" not in req:
            mask = sum(1 << (la - 1) for la, on in self.on.items() if on)
            return {"la_pullup_mask": mask, "pullups_available": 1}
        la = req["la"]
        hw = PULL_TABLE.get(la)
        if self.features_rev < 3:
            if "pull" in req:
                raise AssertionError("old firmware got pull")
            if not hw:
                raise firmware_error("no pull-up on this la", cmd="la")
            if "pullup" in req:
                self.on[la] = req["pullup"] == "on"
            return {"la": la, "pullup": int(self.on.get(la, False)), "ohms": hw.ohms, "pull": hw.default,
                    "pullups_available": 1}
        want = req.get("pull")
        if want is None and "pullup" in req:
            want = (hw.default if hw else "none") if req["pullup"] == "on" else "off"
        if want in ("up", "down", "none") and not hw:
            raise firmware_error(f"LA{la} has no pull resistor on this board", cmd="la", code="BP0023",
                                 values={"la": la})
        if want in ("up", "down") and want not in hw.dirs:
            raise firmware_error(f"LA{la} supports pull-{hw.default} only on this board", cmd="la",
                                 code="BP0024", values={"la": la, "want": want, "have": hw.default})
        if want is not None and hw:
            self.on[la] = want != "off"
        on = self.on.get(la, False)
        return {"la": la, "pullup": int(on), "ohms": hw.ohms if hw else "",
                "pull": hw.default if hw else "none", "state": (hw.default if on else "off") if hw else "off",
                "dirs": list(hw.dirs) if hw else [], "pullups_available": 1}


def _bp(features_rev: int = 3) -> "tuple[BenchPod, Pod]":
    pod = Pod(features_rev)
    return BenchPod(transport=pod, lease=False, wiring={"version": 2}), pod


def test_new_firmware_gets_the_direction():
    bp, pod = _bp(3)
    assert bp.supports("la_pull_dir")
    st = bp.set_pull(1, "up")
    assert pod.sent[-1] == {"cmd": "la", "la": 1, "pull": "up"}
    assert isinstance(st, PullState)
    assert st.enabled and st.state == "up" and st.direction == "up" and st.supported == ("up",)
    assert st.ohms == "4.7k"
    st = bp.set_pull(7, "down")
    assert pod.sent[-1] == {"cmd": "la", "la": 7, "pull": "down"} and st.state == "down"
    for off in ("off", None, False):
        st = bp.set_pull(7, off)
        assert pod.sent[-1] == {"cmd": "la", "la": 7, "pull": "off"} and st.state == "off"
        assert not st.enabled and st.direction == "down"
    assert bp.enabled_pulls() == [1]


def test_new_firmware_refusals_carry_the_code():
    bp, pod = _bp(3)
    with pytest.raises(PullDirectionError) as e:
        bp.set_pull(7, "up")
    assert e.value.code == "BP0024" and e.value.la == 7 and e.value.want == "up" and e.value.have == "down"
    assert "LA7 supports pull-down only on this board" in str(e.value)
    assert isinstance(e.value, ValueError)
    with pytest.raises(PullDirectionError) as e:
        bp.set_pull(9, "down")
    assert e.value.code == "BP0023" and e.value.la == 9
    # off is fine everywhere; a query on LA9 answers
    assert bp.set_pull(9, "off").supported == ()
    st = bp.pull_state(12)
    assert st.direction is None and st.ohms == "" and st.state == "off"


def test_true_false_and_enabled_still_work():
    bp, pod = _bp(3)
    assert bp.set_pull(8, True).state == "down"          # True = the channel's own direction
    assert pod.sent[-1] == {"cmd": "la", "la": 8, "pull": "down"}
    assert bp.set_pull(2, enabled=True).state == "up"
    assert bp.set_pull(2, enabled=False).state == "off"
    with pytest.raises(TypeError, match="needs pull"):
        bp.set_pull(2)
    with pytest.raises(ValueError, match="pull"):
        bp.set_pull(2, "sideways")  # type: ignore[arg-type]


def test_shortcuts():
    bp, pod = _bp(3)
    bp.enable_pullup(1, 2)
    bp.enable_pulldown(8)
    assert [r.get("pull") for r in pod.sent] == ["up", "up", "down"]
    with pytest.raises(PullDirectionError, match="LA8 supports pull-down only"):
        bp.disable_pullup(8)
    with pytest.raises(PullDirectionError, match="LA3 supports pull-up only"):
        bp.disable_pulldown(3)
    bp.disable_pulldown(8)
    assert pod.sent[-1] == {"cmd": "la", "la": 8, "pull": "off"}
    with pytest.raises(PullDirectionError, match="LA7 supports pull-down only"):
        bp.enable_pullup(7)


def test_old_firmware_falls_back_to_pullup():
    bp, pod = _bp(2)
    assert not bp.supports("la_pull_dir")
    st = bp.set_pull(3, "up")
    assert pod.sent[-1] == {"cmd": "la", "la": 3, "pullup": "on"}
    assert st.state == "up" and st.supported == ("up",) and st.ohms == "2.2k"
    st = bp.set_pull(3, "off")
    assert pod.sent[-1] == {"cmd": "la", "la": 3, "pullup": "off"} and st.state == "off"
    bp.set_pull(7, "down")
    assert pod.sent[-1] == {"cmd": "la", "la": 7, "pullup": "on"}
    n = len(pod.sent)
    # the SDK refuses what the old firmware would do wrong (pullup:on on LA7 pulls DOWN)
    with pytest.raises(PullDirectionError) as e:
        bp.set_pull(7, "up")
    assert e.value.code == "BP0024" and e.value.have == "down"
    with pytest.raises(PullDirectionError) as e:
        bp.set_pull(10, "up")
    assert e.value.code == "BP0023"
    # LA9-LA14: answered here, old firmware refuses the query
    assert bp.set_pull(10, "off").supported == ()
    assert bp.pull_state(10).state == "off"
    assert len(pod.sent) == n


def test_wiring_pulls_map_and_list():
    w = Wiring.from_dict({"version": 2, "pulls": {"1": "up", "2": "UP", "7": "down"}})
    assert w.pulls == {1: "up", 2: "up", 7: "down"}
    assert w.pull_directions() == {1: "up", 2: "up", 7: "down"}
    # every pull is its channel's own direction: written as the list, which older readers take
    assert w.to_dict()["pulls"] == [1, 2, 7]
    assert Wiring.from_dict(w.to_dict()).pull_directions() == w.pull_directions()
    # an empty map stays a map (it switches every pull off, like [])
    assert Wiring.from_dict({"version": 2, "pulls": {}}).to_dict()["pulls"] == {}
    lst = Wiring.from_dict({"version": 2, "pulls": [8, 1]})
    assert lst.pulls == (1, 8) and lst.pull_directions() == {1: "up", 8: "down"}
    assert lst.to_dict()["pulls"] == [1, 8]
    for bad, msg in (({"7": "up"}, "LA7 supports pull-down only"), ({"9": "up"}, "pulls.9"),
                     ({"1": "sideways"}, '"up" or "down"'), ("1", "map of LA channel")):
        with pytest.raises(ValueError, match=msg):
            Wiring.from_dict({"version": 2, "pulls": bad})


def test_la_pins_reports_the_directions():
    from embeddedci.benchpod.gpio import LaPinState

    new = LaPinState.from_reply({"la": 1, "function": "none", "pull": {"dir": "up", "ohms": "4.7k", "on": False,
                                                                        "dirs": ["up"]}})
    assert new.pull_dirs == ("up",)
    old = LaPinState.from_reply({"la": 7, "function": "none", "pull": {"dir": "down", "ohms": "10k", "on": True}})
    assert old.pull_dirs == ("down",) and old.pull_on
    assert LaPinState.from_reply({"la": 9, "function": "none", "pull": None}).pull_dirs == ()


def test_power_on_applies_the_map():
    bp, pod = _bp(3)
    w = Wiring.from_dict({"version": 2, "la_mv": 3300, "pulls": {"2": "up", "8": "down"}})
    bp._apply_profile_pulls(w)
    sent = {r["la"]: r["pull"] for r in pod.sent}
    assert sent == {1: "off", 2: "up", 3: "off", 4: "off", 5: "off", 6: "off", 7: "off", 8: "down"}
