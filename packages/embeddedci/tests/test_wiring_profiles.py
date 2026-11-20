"""Wiring profiles stored on the pod (firmware wiring_store.h): loading, saving in pieces, revision
conflicts, the default, power-on with a profile, and the error codes on refusals."""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional

import pytest

from embeddedci.benchpod import BenchPod, Wiring
from embeddedci.benchpod.errors import FirmwareError, WiringError, firmware_error


class ProfilePod:
    """A fake pod transport that stores wiring profiles like the firmware does."""

    def __init__(self, profiles: Optional[Dict[str, dict]] = None, default: str = "") -> None:
        self.profiles: Dict[str, bytes] = {n: json.dumps(p).encode() for n, p in (profiles or {}).items()}
        self.revs: Dict[str, int] = {n: 1 for n in self.profiles}
        self.default = default or next(iter(self.profiles), "")
        self.commands: List[dict] = []
        self.la_mv: Optional[int] = None
        self.rails = {1: False, 2: False}
        self._up = b""
        self._up_name = ""
        self._if_rev: Optional[int] = None

    def status(self) -> Dict[str, Any]:
        return {"board": "stm32h563", "adc_bits": 16, "caps": []}

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass

    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        self.rails[efuse] = on

    def _fail(self, code: str, msg: str, cmd: str, **values: Any) -> None:
        raise firmware_error(msg, cmd=cmd, code=code, values=values)

    def command(self, req: dict) -> Any:
        self.commands.append(req)
        verb, name = req["cmd"], req.get("name", "")
        if verb == "wiring_list":
            return {"default": self.default or None,
                    "profiles": [{"name": n, "rev": self.revs[n], "bytes": len(b)} for n, b in self.profiles.items()],
                    "max_profiles": 16, "max_bytes": 4096, "free_bytes": 10000}
        if verb == "wiring_get":
            if name not in self.profiles:
                self._fail("BP0007", "no such profile", verb, name=name)
            data, off = self.profiles[name], req.get("offset", 0)
            piece = data[off:off + 600]
            return {"name": name, "rev": self.revs[name], "total": len(data), "offset": off,
                    "data_b64": base64.urlsafe_b64encode(piece).decode().rstrip("=")}
        if verb == "wiring_put":
            b64 = req["data_b64"]
            piece = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
            if req["offset"] == 0:
                self._up, self._up_name, self._if_rev = b"", name, req.get("if_revision")
            self._up += piece
            if len(self._up) < req["total"]:
                return {"name": name, "received": len(self._up), "total": req["total"]}
            if self._if_rev is not None and self._if_rev != self.revs.get(name, 0):
                self._fail("BP0009", "profile changed since it was loaded", verb, name=name,
                           rev=self.revs.get(name, 0))
            self.profiles[name] = self._up
            self.revs[name] = self.revs.get(name, 0) + 1
            self.default = self.default or name
            return {"name": name, "rev": self.revs[name], "default": self.default == name}
        if verb == "wiring_default":
            if name not in self.profiles:
                self._fail("BP0007", "no such profile", verb, name=name)
            self.default = name
            return {"default": name}
        if verb == "wiring_delete":
            if name == self.default:
                self._fail("BP0013", "cannot delete the default profile", verb, name=name)
            del self.profiles[name]
            return {"name": name, "deleted": True}
        if verb == "la_pull" and "la" in req:
            on = req.get("pull", "off") != "off"
            d = "down" if req["la"] in (7, 8) else "up"
            return {"la": req["la"], "ohms": "4.7k", "state": d if on else "off", "dirs": [d],
                    "pullups_available": True}
        if verb == "la_voltage":
            if "mv" in req:
                self.la_mv = req["mv"]
            return {"mv": self.la_mv or 0, "st": 1}
        if verb == "target_status":
            return {f"efuse{n}": {"enabled": int(self.rails[n]), "fault": 0, "valid": 1} for n in (1, 2)}
        if verb == "target_power":
            self.rails[req["efuse"]] = bool(req["state"])
            return {"efuse": req["efuse"], "enabled": req["state"], "delay_ms": 0}
        return {}

    def sent(self, verb: str) -> List[dict]:
        return [c for c in self.commands if c["cmd"] == verb]


K66 = {"version": 2, "la_mv": 1800, "efuse": 2, "uart_rx": 3, "uart_tx": 2}
NRF = {"version": 2, "la_mv": 3300, "efuse": 1, "swd_swclk": 11, "swd_swdio": 12}


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    for var in ("BENCHPOD_WIRING", "BENCHPOD_WIRING_PROFILE", "BENCHPOD_WIRING_SOURCE"):
        monkeypatch.delenv(var, raising=False)


def test_the_pods_default_profile_is_loaded():
    pod = ProfilePod({"k66": K66, "nrf": NRF}, default="nrf")
    w = BenchPod(transport=pod, lease=False).wiring
    assert (w.source, w.profile_name, w.profile_rev, w.swd_swclk, w.uart_rx) == ("pod", "nrf", 1, 11, None)


def test_a_named_profile_is_loaded_and_a_missing_one_raises():
    pod = ProfilePod({"k66": K66, "nrf": NRF}, default="nrf")
    assert BenchPod(transport=pod, lease=False, wiring_profile="k66").wiring.uart_rx == 3
    with pytest.raises(FirmwareError, match="no wiring profile 'nope'"):
        _ = BenchPod(transport=pod, lease=False, wiring_profile="nope").wiring


def test_a_pod_without_profiles_wires_nothing():
    w = BenchPod(transport=ProfilePod(), lease=False).wiring
    assert w == Wiring() and w.source == "pod"


def test_a_file_and_a_profile_name_together_is_an_error(monkeypatch):
    with pytest.raises(ValueError, match="not both"):
        BenchPod(transport=ProfilePod(), lease=False, wiring={"version": 2}, wiring_profile="k66")
    monkeypatch.setenv("BENCHPOD_WIRING", "wiring.json")
    with pytest.raises(ValueError, match="not both"):
        BenchPod(transport=ProfilePod(), lease=False, wiring_profile="k66")


def test_a_big_profile_goes_both_ways_in_pieces():
    sigs = [{"name": f"S{i}", "la": i + 1, "description": "d" * 80} for i in range(14)]
    pod = ProfilePod({"default": {"version": 2}})
    bp = BenchPod(transport=pod, lease=False)
    saved = bp.save_wiring({"version": 2, "signals": sigs}, name="big")
    assert len(pod.sent("wiring_put")) > 1 and saved.profile_name == "big" and saved.profile_rev == 1
    bp2 = BenchPod(transport=pod, lease=False, wiring_profile="big")
    assert len(bp2.wiring.signals) == 14 and len(pod.sent("wiring_get")) > 1


def test_saving_the_loaded_profile_checks_its_revision():
    pod = ProfilePod({"k66": K66})
    bp = BenchPod(transport=pod, lease=False)
    assert bp.wiring.profile_rev == 1                                       # loaded now, at rev 1
    other = BenchPod(transport=pod, lease=False)
    other.save_wiring(other.wiring.with_changes(uart_baud=9600))          # someone else saves k66
    with pytest.raises(FirmwareError) as exc:
        bp.save_wiring(bp.wiring.with_changes(uart_baud=19200))
    err = exc.value
    assert err.code == "BP0009" and err.values == {"name": "k66", "rev": 2}
    assert isinstance(err, WiringError) and err.name == "k66"
    assert "load it again" in str(err) and err.docs_url.endswith("#BP0009")
    bp.load_wiring("k66")
    assert bp.save_wiring(bp.wiring.with_changes(uart_baud=19200)).profile_rev == 3


def test_saving_under_a_name_does_not_change_the_default():
    pod = ProfilePod({"k66": K66})
    bp = BenchPod(transport=pod, lease=False)
    bp.save_wiring(NRF, name="nrf")
    assert pod.default == "k66" and bp.wiring.profile_name == "nrf"
    assert bp.set_default_wiring("nrf") == "nrf" and pod.default == "nrf"
    assert bp.wiring_profiles()["default"] == "nrf"


def test_deleting_the_default_is_refused_with_its_code():
    pod = ProfilePod({"k66": K66, "nrf": NRF}, default="k66")
    bp = BenchPod(transport=pod, lease=False)
    with pytest.raises(FirmwareError) as exc:
        bp.delete_wiring("k66")
    assert exc.value.code == "BP0013" and isinstance(exc.value, WiringError)
    bp.delete_wiring("nrf")
    assert "nrf" not in pod.profiles


def test_power_on_applies_the_profile_first():
    pod = ProfilePod({"k66": K66, "nrf": NRF}, default="nrf")
    bp = BenchPod(transport=pod, lease=False)
    bp.power_on(wiring_profile="k66")
    assert pod.la_mv == 1800 and pod.rails[2] is True
    tp = pod.sent("target_power")[-1]
    assert tp["profile"] == "k66" and tp["efuse"] == 2
    # A powered board never gets its I/O voltage changed by the next power_on.
    with pytest.raises(Exception, match="power it off first"):
        bp.power_on(efuse=2, wiring_profile="nrf")


def test_power_on_with_nothing_wired_needs_a_rail():
    with pytest.raises(ValueError, match="sets no power rail"):
        BenchPod(transport=ProfilePod(), lease=False).power_on()


def test_power_on_sets_the_profiles_pull_resistors():
    pod = ProfilePod({"nrf": {**NRF, "pulls": [2, 1]}}, default="nrf")
    bp = BenchPod(transport=pod, lease=False)
    assert bp.wiring.pulls == (1, 2)
    bp.power_on()
    sets = {c["la"]: c["pull"] for c in pod.sent("la_pull")}
    assert sets == {1: "up", 2: "up", 3: "off", 4: "off", 5: "off", 6: "off", 7: "off", 8: "off"}
    # Sent before the rail goes on.
    order = [c["cmd"] for c in pod.commands]
    assert order.index("target_power") > max(i for i, c in enumerate(order) if c == "la_pull")


def test_power_on_leaves_the_pulls_without_the_key():
    pod = ProfilePod({"nrf": NRF}, default="nrf")
    bp = BenchPod(transport=pod, lease=False)
    bp.power_on()
    assert pod.sent("la_pull") == []


def test_power_on_applies_the_pulls_at_1v8():
    pod = ProfilePod({"k66": {**K66, "pulls": [1]}}, default="k66")
    bp = BenchPod(transport=pod, lease=False)
    bp.power_on()
    sets = {c["la"]: c["pull"] for c in pod.sent("la_pull")}
    assert sets == {1: "up", 2: "off", 3: "off", 4: "off", 5: "off", 6: "off", 7: "off", 8: "off"}


def test_pulls_key():
    w = Wiring.from_dict({"version": 2, "pulls": [3, 1, 3]})
    assert w.pulls == (1, 3) and w.to_dict()["pulls"] == [1, 3]
    assert "pulls" not in Wiring().to_dict()
    with pytest.raises(ValueError, match=r"pulls\[0\] must be an LA channel with a pull resistor"):
        Wiring(pulls=[9])
    assert not Wiring(i2c_sda=7, pulls=[]).warnings()
    assert Wiring(i2c_sda=7, pulls=[7]).warnings()
    assert not Wiring(la_mv=1800, pulls=[1]).warnings()


def test_connect_sets_the_io_voltage_from_the_pods_profile():
    pod = ProfilePod({"nrf": NRF}, default="nrf")
    bp = BenchPod(transport=pod, lease=False)
    assert bp.wiring.source == "pod"   # loaded, as connect does to send the pin directions
    bp._auto_la_voltage()
    assert pod.la_mv == 3300
    # A voltage the pod already has is never changed.
    pod.la_mv = 1800
    bp._auto_la_voltage()
    assert pod.la_mv == 1800


def test_connect_leaves_the_voltage_without_one_in_the_profile_or_for_a_dict_profile():
    pod = ProfilePod({"bare": {"version": 2, "uart_rx": 5}}, default="bare")
    bp = BenchPod(transport=pod, lease=False)
    _ = bp.wiring
    bp._auto_la_voltage()
    assert not pod.la_mv
    bp2 = BenchPod(transport=ProfilePod(), lease=False, wiring={"version": 2, "la_mv": 3300})
    bp2._auto_la_voltage()
    assert bp2.transport.sent("la_voltage") == []   # type: ignore[attr-defined]
