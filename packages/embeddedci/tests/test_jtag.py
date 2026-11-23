"""JTAG on the pod's CMSIS-DAP probe: the wiring fields, the dap_start JSON, the automatic
SWD/JTAG choice of flash(), and the OpenOCD arguments. No hardware."""

from __future__ import annotations

import json
import types
from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import BenchPod, flash as flashmod
from embeddedci.benchpod.errors import UnsupportedFeatureError
from embeddedci.benchpod.header import connections
from embeddedci.benchpod.transport.tcp import TcpTransport
from embeddedci.benchpod.wiring import Wiring

from test_flash_bridge import FakePodLink
from test_tcp_transport import FakePod


# -- wiring fields -------------------------------------------------------------

JTAG = {"jtag_tck": 3, "jtag_tms": 4, "jtag_tdi": 5, "jtag_tdo": 6}


def test_jtag_fields_default_to_not_wired_and_round_trip():
    assert Wiring().jtag_tck is None and Wiring().jtag_trst is None
    w = Wiring(la_mv=3300, **JTAG, jtag_trst=7)
    d = w.to_dict()
    assert (d["jtag_tck"], d["jtag_tms"], d["jtag_tdi"], d["jtag_tdo"], d["jtag_trst"]) == (3, 4, 5, 6, 7)
    assert Wiring.from_dict(d) == w
    assert Wiring.from_dict({"version": 2, "jtag_tck": 3}).jtag_tck == 3


@pytest.mark.parametrize("key", ["jtag_tck", "jtag_tms", "jtag_tdi", "jtag_tdo", "jtag_trst"])
@pytest.mark.parametrize("bad", [0, 15, "3", 3.5, True])
def test_jtag_fields_must_be_la_channels(key, bad):
    with pytest.raises(ValueError, match=f"{key} must be an LA channel 1..14"):
        Wiring(**{key: bad})


def test_jtag_pins_must_be_distinct():
    with pytest.raises(ValueError, match="LA3 is used by both jtag_tck and jtag_tdi"):
        Wiring(jtag_tck=3, jtag_tdi=3)
    with pytest.raises(ValueError, match="LA7 is used by both jtag_tdo and jtag_trst"):
        Wiring(jtag_tdo=7, jtag_trst=7)
    with pytest.raises(ValueError, match="LA3 is used by both uart_rx and jtag_tck"):
        Wiring(uart_rx=3, jtag_tck=3)


def test_tck_may_share_swclk_and_tms_may_share_swdio_only():
    w = Wiring(swd_swclk=11, swd_swdio=12, jtag_tck=11, jtag_tms=12, jtag_tdi=5, jtag_tdo=6)
    assert w.pins()[11] in ("swd_swclk", "jtag_tck")
    with pytest.raises(ValueError, match="LA11 is used by both swd_swclk and jtag_tms"):
        Wiring(swd_swclk=11, jtag_tms=11)
    with pytest.raises(ValueError, match="LA12 is used by both swd_swdio and jtag_tck"):
        Wiring(swd_swdio=12, jtag_tck=12)
    with pytest.raises(ValueError, match="LA11 is used by both swd_swclk and jtag_tdi"):
        Wiring(swd_swclk=11, jtag_tdi=11)


def test_jtag_roles_have_dut_directions_and_header_labels():
    w = Wiring(swd_swclk=11, swd_swdio=12, jtag_tck=11, jtag_tms=12, jtag_tdi=5, jtag_tdo=6)
    assert w.dut_dir("jtag_tdo") == "dut_output" and w.dut_dir("jtag_tdi") == "dut_input"
    assert w.pin_dut_dirs()[12] == "bidir"      # shared with SWDIO
    used = {c["name"]: c["used_for"] for c in connections(w)}
    assert used["LA11"] == "SWCLK / TCK" and used["LA5"] == "TDI" and used["LA6"] == "TDO"


# -- dap_start JSON ----------------------------------------------------------------

def _dap_request(**kw: Any) -> Dict[str, Any]:
    seen: List[bytes] = []

    def handler(conn, line):
        seen.append(line)
        conn.sendall(b'{"status":"ok"}\n')

    pod = FakePod(handler)
    try:
        link = TcpTransport(pod.addr, timeout=2).dap_start(**kw)
        link.close()
    finally:
        pod.close()
    req = json.loads(seen[0])
    req.pop("client", None)
    req.pop("client_api", None)
    return req


def test_dap_start_swd_json_is_unchanged():
    assert _dap_request(swclk=11, swdio=12, packet_size=1, wait_ms=5) == {
        "cmd": "dap_start", "swclk": 11, "swdio": 12, "packet_size": 1, "wait_ms": 5}


def test_dap_start_jtag_only_json():
    req = _dap_request(tck=3, tms=4, tdi=5, tdo=6, trst=7, packet_count=4)
    assert req == {"cmd": "dap_start", "tck": 3, "tms": 4, "tdi": 5, "tdo": 6, "trst": 7,
                   "packet_count": 4}


def test_dap_start_both_sets_carry_the_port():
    req = _dap_request(swclk=11, swdio=12, tck=11, tms=12, tdi=5, tdo=6, port="jtag")
    assert req["swclk"] == 11 and req["tck"] == 11 and req["port"] == "jtag"
    # the port only matters when both are given
    assert "port" not in _dap_request(tck=3, tms=4, tdi=5, tdo=6, port="jtag")


@pytest.mark.parametrize("kw, msg", [
    ({}, "needs swclk/swdio or tck"),
    ({"swclk": 1}, "both swclk and swdio"),
    ({"tck": 1, "tms": 2, "tdi": 3}, "missing tdo"),
    ({"swclk": 1, "swdio": 2, "port": "x"}, "port must be"),
])
def test_dap_start_refuses_incomplete_pins(kw, msg):
    t = TcpTransport("127.0.0.1:9", timeout=1)
    with pytest.raises(ValueError, match=msg):
        t.dap_start(**kw)


# -- the automatic choice ----------------------------------------------------------

@pytest.mark.parametrize("target", ["esp32", "esp32s2", "target/esp32.cfg", "target/esp32s2.cfg",
                                    "gd32vf103", "fe310", "k210", "target/riscv.cfg", "riscv",
                                    "target/riscv-gd32.cfg", "TARGET/ESP32.CFG"])
def test_jtag_only_targets(target):
    assert flashmod.target_is_jtag_only(target)


@pytest.mark.parametrize("target", ["", "target/stm32f4x.cfg", "target/esp32c3.cfg", "esp32c3",
                                    "target/esp32s3.cfg", "target/nrf52.cfg", "target/rp2040.cfg"])
def test_swd_capable_targets(target):
    assert not flashmod.target_is_jtag_only(target)


@pytest.mark.parametrize("swd, jtag, target, want", [
    (True, False, "target/stm32f4x.cfg", "swd"),
    (True, False, "target/esp32.cfg", "swd"),        # no JTAG pins: SWD
    (False, True, "target/stm32f4x.cfg", "jtag"),    # no SWD pins: JTAG
    (True, True, "target/stm32f4x.cfg", "swd"),      # both wired, ARM: SWD
    (True, True, "target/esp32.cfg", "jtag"),        # both wired, JTAG-only target
    (True, True, "target/riscv.cfg", "jtag"),
])
def test_select_port(swd, jtag, target, want):
    assert flashmod.select_port(swd=swd, jtag=jtag, target=target) == want


def test_select_port_needs_some_pins():
    with pytest.raises(ValueError, match="SWD pins .* or JTAG pins"):
        flashmod.select_port(swd=False, jtag=False)


class _Transport:
    """Records dap_start calls; its link is quiet."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def dap_start(self, *a: Any, **kw: Any) -> FakePodLink:
        self.calls.append({"args": a, **kw})
        return FakePodLink()

    def target_power(self, *a: Any) -> None:
        pass


@pytest.fixture
def fake_pod(monkeypatch):
    """A BenchPod on a recording transport; the OpenOCD bridge is replaced by a recorder."""
    ports: List[str] = []

    def bridge(openocd_bin, args, pod_link, *, timeout, transport="swd", **kw):
        ports.append(transport)
        return flashmod.FlashResult(ok=True, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(flashmod, "_run_bridge", bridge)
    monkeypatch.setattr(flashmod, "find_openocd", lambda b: "openocd")
    monkeypatch.setattr(flashmod, "supports_cmsis_dap_tcp", lambda b: True)
    monkeypatch.setattr(flashmod.time, "sleep", lambda s: None)

    class Pod:
        def make(self, wiring: Dict[str, Any], jtag: bool = True) -> BenchPod:
            self.t = _Transport()
            bp = BenchPod(transport=self.t, lease=False, wiring=Wiring(la_mv=3300, **wiring))
            monkeypatch.setattr(BenchPod, "supports", lambda s, f: jtag if f == "jtag" else False)
            monkeypatch.setattr(BenchPod, "capabilities",
                                types.SimpleNamespace(firmware_version="3.10.0"))
            return bp

        def flash(self, bp: BenchPod, **kw: Any) -> str:
            kw.setdefault("file", "fw.elf")
            assert bp.flash(**kw).ok
            return ports[-1]

    return Pod()


def test_swd_only_wiring_flashes_over_swd_as_before(fake_pod):
    bp = fake_pod.make({"swd_swclk": 11, "swd_swdio": 12})
    assert fake_pod.flash(bp, target="target/stm32f4x.cfg") == "swd"
    call = fake_pod.t.calls[0]
    assert (call["args"][:2]) == (11, 12) and "tck" not in call and "port" not in call


def test_jtag_only_wiring_flashes_over_jtag(fake_pod):
    bp = fake_pod.make(JTAG)
    assert fake_pod.flash(bp, target="target/stm32f4x.cfg") == "jtag"
    call = fake_pod.t.calls[0]
    assert call["args"][:2] == (None, None)
    assert {k: call[k] for k in ("tck", "tms", "tdi", "tdo", "port")} == {
        "tck": 3, "tms": 4, "tdi": 5, "tdo": 6, "port": "jtag"} and "trst" not in call


def test_both_wired_uses_swd_for_arm_and_jtag_for_jtag_only_targets(fake_pod):
    both = {"swd_swclk": 11, "swd_swdio": 12, "jtag_tck": 11, "jtag_tms": 12, "jtag_tdi": 5,
            "jtag_tdo": 6, "jtag_trst": 7}
    bp = fake_pod.make(both)
    assert fake_pod.flash(bp, target="target/stm32f4x.cfg") == "swd"
    assert "tck" not in fake_pod.t.calls[0]
    assert fake_pod.flash(bp, target="target/esp32.cfg") == "jtag"
    call = fake_pod.t.calls[1]
    assert call["args"][:2] == (11, 12)
    assert (call["tck"], call["tms"], call["tdi"], call["tdo"], call["trst"], call["port"]) == (
        11, 12, 5, 6, 7, "jtag")


def test_explicit_pins_override_the_wiring(fake_pod):
    bp = fake_pod.make({"swd_swclk": 11, "swd_swdio": 12})
    # JTAG pins passed, no SWD pins passed: JTAG, even with SWD wired and an ARM target
    assert fake_pod.flash(bp, target="target/stm32f4x.cfg", tck=1, tms=2, tdi=3, tdo=4) == "jtag"
    assert fake_pod.t.calls[0]["tck"] == 1
    fake_pod.flash(bp, target="target/riscv.cfg", tck=1, tms=2, tdi=3, tdo=4, trst=9)
    call = fake_pod.t.calls[1]
    assert (call["tck"], call["tms"], call["tdi"], call["tdo"], call["trst"]) == (1, 2, 3, 4, 9)
    assert call["args"][:2] == (11, 12)
    # swclk/swdio passed explicitly beat the wiring too
    fake_pod.flash(bp, target="target/stm32f4x.cfg", swclk=8, swdio=9)
    assert fake_pod.t.calls[2]["args"][:2] == (8, 9)


# -- the profile's debug_port and pins passed in the call ------------------------------

BOTH = {"swd_swclk": 11, "swd_swdio": 12, "jtag_tck": 11, "jtag_tms": 12, "jtag_tdi": 5, "jtag_tdo": 6}
ARM = "target/stm32f4x.cfg"


def test_debug_port_field_validation_and_round_trip():
    assert Wiring().debug_port is None and Wiring().to_dict()["debug_port"] is None
    for value in ("swd", "jtag"):
        w = Wiring(debug_port=value)
        assert Wiring.from_dict(w.to_dict()) == w
    assert Wiring.from_dict({"version": 2, "debug_port": None}).debug_port is None
    for bad in ("SWD", "usb", "", 1, True):
        with pytest.raises(ValueError, match="debug_port must be 'swd' or 'jtag'"):
            Wiring(debug_port=bad)


def test_profile_debug_port_jtag_beats_automatic_swd(fake_pod):
    bp = fake_pod.make({**BOTH, "debug_port": "jtag"})
    assert fake_pod.flash(bp, target=ARM) == "jtag"
    call = fake_pod.t.calls[0]
    # both pin sets known: both are sent, shared TCK/TMS
    assert call["args"][:2] == (11, 12) and call["port"] == "jtag" and call["tck"] == 11


def test_profile_debug_port_swd_beats_jtag_only_target(fake_pod):
    bp = fake_pod.make({**BOTH, "debug_port": "swd"})
    assert fake_pod.flash(bp, target="target/esp32.cfg") == "swd"
    assert "tck" not in fake_pod.t.calls[0]


def test_profile_debug_port_swd_ignores_partial_jtag_pins(fake_pod):
    bp = fake_pod.make({"swd_swclk": 11, "swd_swdio": 12, "jtag_tdi": 5, "debug_port": "swd"})
    assert fake_pod.flash(bp, target=ARM) == "swd"


def test_debug_port_jtag_with_incomplete_pins_names_the_profile_field(fake_pod):
    bp = fake_pod.make({"swd_swclk": 11, "swd_swdio": 12, "jtag_tck": 3, "jtag_tms": 4,
                        "jtag_tdi": 5, "debug_port": "jtag"})
    with pytest.raises(ValueError, match="debug_port is jtag but jtag_tdo is not set"):
        bp.flash(target=ARM, file="f")
    partial = fake_pod.make({"swd_swclk": 11, "jtag_tdo": 6, "debug_port": "jtag"})
    with pytest.raises(ValueError, match="debug_port is jtag but jtag_tck, jtag_tms, jtag_tdi is not set"):
        partial.flash(target=ARM, file="f")
    assert fake_pod.t.calls == []


def test_debug_port_swd_with_incomplete_pins_names_the_profile_field(fake_pod):
    bp = fake_pod.make({**JTAG, "swd_swclk": 11, "debug_port": "swd"})
    with pytest.raises(ValueError, match="debug_port is swd but swd_swdio is not set"):
        bp.flash(target=ARM, file="f")
    bp = fake_pod.make({**JTAG, "debug_port": "swd"})
    with pytest.raises(ValueError, match="debug_port is swd but swd_swclk, swd_swdio is not set"):
        bp.flash(target=ARM, file="f")


def test_jtag_pin_in_the_call_beats_debug_port_swd(fake_pod):
    bp = fake_pod.make({**BOTH, "debug_port": "swd"})
    assert fake_pod.flash(bp, target=ARM, tdo=9) == "jtag"       # the rest from the profile
    call = fake_pod.t.calls[0]
    assert (call["tck"], call["tms"], call["tdi"], call["tdo"]) == (11, 12, 5, 9)
    assert fake_pod.flash(bp, target=ARM, trst=7) == "jtag"      # trst alone counts as a JTAG pin
    assert fake_pod.t.calls[1]["trst"] == 7


def test_swd_pin_in_the_call_beats_debug_port_jtag_and_jtag_only_target(fake_pod):
    bp = fake_pod.make({**BOTH, "debug_port": "jtag"})
    assert fake_pod.flash(bp, target=ARM, swclk=8) == "swd"
    assert fake_pod.t.calls[0]["args"][:2] == (8, 12)
    assert fake_pod.flash(bp, target="target/esp32.cfg", swdio=9) == "swd"
    assert "tck" not in fake_pod.t.calls[1]


def test_jtag_pin_in_the_call_with_incomplete_pins_is_the_missing_pins_error(fake_pod):
    bp = fake_pod.make({"swd_swclk": 11, "swd_swdio": 12, "debug_port": "swd"})
    with pytest.raises(ValueError, match="all of tck, tms, tdi and tdo: jtag_tms, jtag_tdi, jtag_tdo"):
        bp.flash(target=ARM, file="f", tck=3)


def test_both_kinds_of_pins_in_the_call_fall_through_to_the_profile(fake_pod):
    bp = fake_pod.make({**BOTH, "debug_port": "jtag"})
    assert fake_pod.flash(bp, target=ARM, swclk=8, swdio=9, tck=1, tms=2, tdi=3, tdo=4) == "jtag"
    bp = fake_pod.make({**BOTH, "debug_port": "swd"})
    assert fake_pod.flash(bp, target="target/esp32.cfg", swclk=11, swdio=12,
                          tck=11, tms=12, tdi=3, tdo=4) == "swd"
    bp = fake_pod.make(BOTH)  # no debug_port: automatic
    assert fake_pod.flash(bp, target=ARM, swclk=11, swdio=12, tck=11, tms=12, tdi=3, tdo=4) == "swd"


def test_no_debug_port_keeps_the_automatic_rule(fake_pod):
    bp = fake_pod.make(BOTH)
    assert fake_pod.flash(bp, target=ARM) == "swd"
    assert fake_pod.flash(bp, target="target/esp32.cfg") == "jtag"


@pytest.mark.parametrize("kw, want", [
    (dict(swd=True, jtag=True, jtag_given=True), "jtag"),
    (dict(swd=True, jtag=True, swd_given=True, target="target/esp32.cfg"), "swd"),
    (dict(swd=True, jtag=True, swd_given=True, jtag_given=True, debug_port="jtag"), "jtag"),
    (dict(swd=True, jtag=True, debug_port="jtag"), "jtag"),
    (dict(swd=True, jtag=True, debug_port="swd", target="target/esp32.cfg"), "swd"),
    (dict(swd=True, jtag=True), "swd"),
])
def test_select_port_order(kw, want):
    assert flashmod.select_port(**kw) == want


def test_jtag_without_the_feature_is_refused_before_arming(fake_pod):
    bp = fake_pod.make(JTAG, jtag=False)
    with pytest.raises(UnsupportedFeatureError) as e:
        bp.flash(target="target/stm32f4x.cfg", file="fw.elf")
    assert e.value.feature == "jtag"
    assert fake_pod.t.calls == []


def test_swd_flash_does_not_need_the_jtag_feature(fake_pod):
    bp = fake_pod.make({"swd_swclk": 11, "swd_swdio": 12}, jtag=False)
    fake_pod.flash(bp, target="target/stm32f4x.cfg")
    assert len(fake_pod.t.calls) == 1


def test_jtag_pin_problems(fake_pod):
    with pytest.raises(ValueError, match="all of tck, tms, tdi and tdo: jtag_tdo"):
        fake_pod.make({"jtag_tck": 3, "jtag_tms": 4, "jtag_tdi": 5}).flash(target="x.cfg", file="f")
    with pytest.raises(ValueError, match="tck and tdi must be different"):
        fake_pod.make(JTAG).flash(target="x.cfg", file="f", tdi=3)
    with pytest.raises(ValueError, match="swclk may only share a pin with tck"):
        fake_pod.make({"swd_swclk": 11, "swd_swdio": 12, **JTAG}).flash(
            target="target/riscv.cfg", file="f", tms=11)
    with pytest.raises(ValueError, match="swclk was not given"):
        fake_pod.make({}).flash(target="x.cfg", file="f")


@pytest.mark.parametrize("port", ["swd", "jtag"])
def test_openocd_gets_the_transport_for_the_port(monkeypatch, port):
    seen: List[List[str]] = []

    class Boom(Exception):
        pass

    def popen(args, **kw):
        seen.append(list(args))
        raise Boom

    monkeypatch.setattr(flashmod.subprocess, "Popen", popen)
    with pytest.raises(Boom):
        flashmod._run_bridge("openocd", ["-f", "target/stm32f4x.cfg"], FakePodLink(), timeout=1,
                             transport=port)
    argv = seen[0]
    assert f"transport select {port}" in argv
    assert f"transport select {'jtag' if port == 'swd' else 'swd'}" not in argv
    assert argv.index(f"transport select {port}") < argv.index("-f")
    assert "adapter driver cmsis-dap" in argv and "cmsis-dap backend tcp" in argv


def test_flash_passes_the_port_to_the_bridge_default_swd(monkeypatch):
    seen: Dict[str, Any] = {}

    def bridge(openocd_bin, args, pod_link, **kw):
        seen.update(kw)
        return flashmod.FlashResult(ok=True, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(flashmod, "_run_bridge", bridge)
    monkeypatch.setattr(flashmod, "find_openocd", lambda b: "openocd")
    monkeypatch.setattr(flashmod, "supports_cmsis_dap_tcp", lambda b: True)
    flashmod.flash(_Transport(), swclk=1, swdio=2, target="t.cfg", file="f")
    assert seen["transport"] == "swd"
    flashmod.flash(_Transport(), tck=1, tms=2, tdi=3, tdo=4, port="jtag", target="t.cfg", file="f")
    assert seen["transport"] == "jtag"
    with pytest.raises(ValueError, match="JTAG needs"):
        flashmod.flash(_Transport(), swclk=1, swdio=2, port="jtag", target="t.cfg", file="f")
    with pytest.raises(ValueError, match="SWD needs"):
        flashmod.flash(_Transport(), tck=1, tms=2, tdi=3, tdo=4, target="t.cfg", file="f")
