"""Target detection (embeddedci.benchpod.detect and BenchPod.detect) against a fake pod."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from embeddedci.benchpod import BenchPod, detect, prog
from embeddedci.benchpod.errors import FirmwareError, TransportError
from embeddedci.benchpod.transport.base import Transport
from embeddedci.benchpod.usb_host import UsbHostState


# -- the table ----------------------------------------------------------------------------------

def test_table_loads_and_is_consistent():
    t = detect.load_table()
    assert t["version"] == 1
    for reg in detect.id_registers():
        assert reg.parts and reg.mask
        for key, name in reg.parts.items():
            assert key & reg.mask == key, (reg.what, hex(key))
            assert name
    for section in ("jtag", "dpidr"):
        for e in t[section]:
            assert int(e["value"], 16) & int(e["mask"], 16) == int(e["value"], 16), e["name"]
    for key in t["usb"]:
        vid, _, pid = key.partition(":")
        assert len(vid) == 4 and len(pid) == 4 and key == key.lower()


def test_table_ships_with_the_package():
    path = Path(detect.__file__).with_name("detect_ids.json")
    assert json.loads(path.read_text())["version"] == 1


def test_no_stm32_dev_id_is_listed_twice():
    seen: Dict[int, str] = {}
    for reg in detect.id_registers():
        if reg.stm32:
            for key, name in reg.parts.items():
                assert key not in seen, f"DEV_ID 0x{key:03x}: {seen.get(key)} and {name}"
                seen[key] = name


# -- pure mapping ---------------------------------------------------------------------------------

def test_core_from_cpuid():
    assert detect.core_from_cpuid("0x410fc241") == "Cortex-M4"
    assert detect.core_from_cpuid("0x410cc601") == "Cortex-M0+"
    assert detect.core_from_cpuid("0x411fd210") == "Cortex-M33"
    assert detect.core_from_cpuid("0x510fc241") is None
    assert detect.core_from_cpuid(None) is None
    assert detect.core_from_cpuid("0x410fffff") == "Arm part 0xfff"


def test_candidate_addresses_follow_the_core():
    assert detect.candidate_addresses("Cortex-M4") == ["0xe0042000", "0x10000100"]
    assert "0x40015800" in detect.candidate_addresses("Cortex-M0+")
    assert "0xe0042000" not in detect.candidate_addresses("Cortex-M0+")
    assert "0x44024000" in detect.candidate_addresses("Cortex-M33")
    everything = detect.candidate_addresses(None)
    assert len(everything) == len(set(everything)) <= detect.MAX_READS


def test_lookup_part():
    reg, name = detect.lookup_part("0xe0042000", "0x10006421")
    assert name == "STM32F446" and reg.vendor == "STMicroelectronics"
    assert detect.lookup_part("0xe0042000", "0x10006999") is None
    assert detect.lookup_part("0x10000100", "0x00052840")[1] == "nRF52840"
    assert detect.lookup_part("0x12345678", "0x10006421") is None


def test_swd_stm32f446():
    reply = {"port": "swd", "found": True, "dpidr": "0x2ba01477", "targetid": None, "apidr": "0x24770011",
             "cpuid": "0x410fc241", "reads": [{"addr": "0xe0042000", "value": "0x10006421"},
                                              {"addr": "0x10000100", "value": None}]}
    f = detect.finding_from_swd(reply, pins={"swclk": 11, "swdio": 12})
    assert (f.method, f.chip, f.vendor, f.core) == ("swd", "STM32F446", "STMicroelectronics", "Cortex-M4")
    assert (f.id, f.confidence, f.invasive) == ("0x10006421", "high", False)
    assert f.pins == {"swclk": 11, "swdio": 12} and f.raw["apidr"] == "0x24770011"
    assert "STM32F446" in f.summary and f.to_dict()["chip"] == "STM32F446"


def test_swd_nrf52840():
    reply = {"found": True, "dpidr": "0x2ba01477", "cpuid": "0x410fc241",
             "reads": [{"addr": "0xe0042000", "value": None}, {"addr": "0x10000100", "value": "0x00052840"}]}
    f = detect.finding_from_swd(reply)
    assert (f.chip, f.vendor, f.confidence) == ("nRF52840", "Nordic Semiconductor", "high")


def test_swd_unknown_st_part_is_medium():
    reply = {"found": True, "dpidr": "0x2ba01477", "cpuid": "0x410fc241",
             "reads": [{"addr": "0xe0042000", "value": "0x10006999"}]}
    f = detect.finding_from_swd(reply)
    assert (f.chip, f.vendor, f.core, f.confidence) == (None, "STMicroelectronics", "Cortex-M4", "medium")
    assert f.id == "0x10006999"


def test_swd_garbage_register_is_not_an_unknown_st_part():
    for value in ("0x00000000", "0xffffffff", "0x00000999"):
        reply = {"found": True, "dpidr": "0x2ba01477", "cpuid": "0x410fc241",
                 "reads": [{"addr": "0xe0042000", "value": value}]}
        f = detect.finding_from_swd(reply)
        assert f.confidence == "low" and f.vendor is None, value


def test_swd_debug_port_alone_is_low():
    f = detect.finding_from_swd({"found": True, "dpidr": "0x2ba01477", "apidr": None, "cpuid": None})
    assert (f.confidence, f.vendor, f.core, f.id) == ("low", None, None, "0x2ba01477")
    assert "Cortex-M3/M4" in f.chip
    unknown = detect.finding_from_swd({"found": True, "dpidr": "0x0badc0de"})
    assert unknown.chip is None and unknown.confidence == "low"


def test_swd_st_targetid_without_a_register_is_medium():
    # designer 0x020 (bits 11:1), DEV_ID 0x449 in bits 23:12
    targetid = (0x449 << 12) | (0x020 << 1) | 1
    f = detect.finding_from_swd({"found": True, "dpidr": "0x6ba02477", "targetid": hex(targetid),
                                 "cpuid": "0x411fc272"})
    assert (f.chip, f.vendor, f.confidence) == ("STM32F74x/F75x", "STMicroelectronics", "medium")


def test_swd_nothing_found():
    assert detect.finding_from_swd({"port": "swd", "found": False}) is None


def test_jtag_chain_arm_dp_and_stm32f446():
    reply = {"port": "jtag", "found": True, "idcodes": ["0x4ba00477", "0x06421041"]}
    fs = detect.findings_from_jtag(reply, pins={"tck": 11, "tms": 12, "tdi": 9, "tdo": 10})
    assert [f.method for f in fs] == ["jtag", "jtag"]
    arm, st = fs
    assert "Arm JTAG-DP" in arm.chip and arm.vendor == "Arm" and arm.confidence == "low"
    assert arm.id == "0x4ba00477"
    assert (st.chip, st.vendor, st.id, st.confidence) == ("STM32F446", "STMicroelectronics", "0x06421041", "high")
    assert st.pins["tdo"] == 10 and st.raw["index"] == 1


def test_jtag_unknown_ids():
    unknown_st = (0x999 << 12) | (0x020 << 1) | 1
    other = (0x123 << 12) | (0x015 << 1) | 1     # NXP
    fs = detect.findings_from_jtag({"found": True, "idcodes": [hex(unknown_st), hex(other), "0x0"]})
    assert [f.confidence for f in fs] == ["medium", "low"]
    assert fs[0].vendor == "STMicroelectronics" and fs[0].chip is None
    assert fs[1].vendor == "NXP"
    esp = detect.findings_from_jtag({"found": True, "idcodes": ["0x00005c25"]})[0]
    assert (esp.chip, esp.vendor, esp.confidence) == ("ESP32-C3", "Espressif", "high")
    assert detect.findings_from_jtag({"found": False}) == []


class _Dev:
    def __init__(self, vid, pid, product="", manufacturer="", ifaces=()):
        self.vid, self.pid, self.product, self.manufacturer, self.speed = vid, pid, product, manufacturer, "full"
        self._ifaces = ifaces

    def has_interface(self, klass, sub=None):
        return any(c == klass and (sub is None or s == sub) for c, s in self._ifaces)


def test_usb_known_loaders():
    f = detect.finding_from_usb(_Dev(0x16C0, 0x0478, "Teensy", "PJRC"), port="usb_a")
    assert (f.method, f.chip, f.id, f.confidence) == ("usb", "Teensy (HalfKay bootloader)", "16c0:0478", "high")
    assert f.vendor == "PJRC" and f.pins == {"port": "usb_a"}
    assert detect.usb_name(_Dev(0x2E8A, 0x0003)) == ("RP2040 (BOOTSEL)", "high")


def test_usb_classifier_fallbacks():
    assert detect.usb_name(_Dev(0x1234, 0x5678, ifaces=[(0xFE, 1)])) == ("USB device in a DFU bootloader", "medium")
    assert detect.usb_name(_Dev(0x10C4, 0xEEEE))[0] == "CP210x USB-serial chip"
    assert detect.usb_name(_Dev(0x303A, 0x4001))[0] == "Espressif USB device"
    assert detect.usb_name(_Dev(0x2341, 0x0010))[0].startswith("Arduino")
    assert detect.usb_name(_Dev(0xAAAA, 0xBBBB)) == (None, "low")


# -- wiring -> pins -------------------------------------------------------------------------------

def _wiring(**kw):
    from embeddedci.benchpod.wiring import Wiring

    return Wiring.from_dict({"version": 2, **kw}) if "version" not in kw else Wiring.from_dict(kw)


def sig(name, la):
    return {"name": name, "la": la}


def test_prog_pins_need_a_signal_named_for_the_protocol():
    w = _wiring(swd_swclk=11, swd_swdio=12, swd_nreset=True)
    assert detect.prog_pins(w, "pic") == (None, "", True)       # the SWD pins are not a fallback
    w = _wiring(signals=[sig("ICSPCLK", 3), sig("ICSPDAT", 4)], swd_nreset=True)
    got, _, _ = detect.prog_pins(w, "pic")
    assert got.pins == {"data": 4, "clk": 3, "reset": "nrst"}
    assert "reset on the pod's reset pin" in got.text


def test_prog_pic_reset_choice():
    base = [sig("PGC", 3), sig("PGD", 4)]
    # no MCLR signal and no swd_nreset: skipped with a reason, not guessed
    got, reason, silent = detect.prog_pins(_wiring(signals=base), "pic")
    assert got is None and not silent and "MCLR" in reason
    # a MCLR signal wins over the pod's reset pin
    got, _, _ = detect.prog_pins(_wiring(signals=base + [sig("MCLR", 5)], swd_nreset=True), "pic")
    assert got.pins["reset"] == 5
    # UPDI is one wire and needs no reset
    got, _, _ = detect.prog_pins(_wiring(signals=[sig("UPDI", 6)]), "updi")
    assert got.pins == {"data": 6}
    # half a pair is reported
    got, reason, silent = detect.prog_pins(_wiring(signals=[sig("C2CK", 6)]), "c2")
    assert got is None and not silent and "C2D" in reason


def test_esp_pins():
    w = _wiring(uart_rx=3, uart_tx=4, signals=[sig("IO0", 6)], swd_nreset=True)
    assert detect.esp_pins(w)[0] == {"rx": 3, "tx": 4, "boot": 6, "en": "nrst"}
    w = _wiring(uart_rx=3, uart_tx=4, signals=[sig("BOOT", 6), sig("EN", 7)])
    assert detect.esp_pins(w)[0]["en"] == 7
    pins, reason, silent = detect.esp_pins(_wiring(uart_rx=3, uart_tx=4, signals=[sig("BOOT", 6)]))
    assert pins is None and not silent and "EN" in reason
    assert detect.esp_pins(_wiring(uart_rx=3, uart_tx=4)) == (None, "", True)


# -- a fake pod -------------------------------------------------------------------------------------

STM32_SWD = {"port": "swd", "found": True, "dpidr": "0x2ba01477", "targetid": None, "apidr": "0x24770011",
             "cpuid": "0x410fc241", "reads": []}


class FakePod:
    def __init__(self) -> None:
        self.cmds: List[Dict[str, Any]] = []
        self.rails = {"efuse1": {"enabled": 1, "fault": 0, "valid": 1}, "efuse2": {"enabled": 0, "fault": 0, "valid": 1}}
        self.mv: Optional[int] = 3300
        self.owners: Dict[int, str] = {}
        self.dap: Dict[str, Any] = {"swd": STM32_SWD, "regs": {0xE0042000: 0x10006421}}
        self.dap_error: Optional[Exception] = None
        self.jtag: Dict[str, Any] = {"port": "jtag", "found": False}
        self.prog_id = "1e9421"
        self.prog_result = "ok"
        self.esp_chip = "ESP32-S3"
        self.usb: Dict[str, Any] = {"mode": "device"}
        self._job: Dict[str, Any] = {}

    def command(self, req: Dict[str, Any]) -> Any:
        self.cmds.append(dict(req))
        fn = getattr(self, "_" + req["cmd"], None)
        return fn(req) if fn else {}

    def sent(self, cmd: str, **kw: Any) -> List[Dict[str, Any]]:
        return [c for c in self.cmds if c["cmd"] == cmd and all(c.get(k) == v for k, v in kw.items())]

    def _target_status(self, req):
        return dict(self.rails, status_supported=True)

    def _la_voltage(self, req):
        return {"mv": self.mv} if self.mv else {}

    def _la_pins(self, req):
        pins = [{"la": n, "function": self.owners.get(n, "none")} for n in range(1, 15)]
        return {"pins": pins}

    def _fpga_image(self, req):
        return {}

    def _usb_host(self, req):
        return self.usb

    def _dap_start(self, req):
        if self.dap_error:
            raise self.dap_error
        if "tck" in req:
            return self.jtag
        reply = dict(self.dap["swd"])
        if req.get("read"):
            reply["reads"] = [{"addr": a, "value": ("0x%08x" % self.dap["regs"][int(a, 16)])
                               if int(a, 16) in self.dap["regs"] else None} for a in req["read"]]
        return reply

    def _prog_flash(self, req):
        if req["op"] in ("info", "start"):
            self._job = dict(req)
            return {"started": True}
        if req["op"] == "status":
            ok = self.prog_result == "ok"
            return {"running": False, "id": self.prog_id if ok else "", "result": self.prog_result}
        return {}

    def _esp_flash(self, req):
        if req["op"] == "status":
            if self.esp_chip:
                return {"running": False, "chip": self.esp_chip, "result": "ok"}
            return {"running": False, "result": "no answer from the ROM loader"}
        return {"started": True}


class FakeTransport(Transport):
    def __init__(self, pod: FakePod, features_rev: int = 17) -> None:
        self.pod, self.features_rev = pod, features_rev

    def command(self, req):
        return self.pod.command(req)

    def close(self):
        pass

    def status(self):
        return {"fpga_image": "deep", "api": 2, "min_client_api": 2, "gateware": 61,
                "features_rev": self.features_rev, "version": "4.0.5"}

    def target_power(self, efuse, on, delay_ms=0):
        pass

    def uart_proxy_start(self, *a, **k):
        raise NotImplementedError


BENCH = {"swd_swclk": 11, "swd_swdio": 12, "efuse": 1, "la_mv": 3300, "signals": []}


def make(pod: FakePod, dap_id: bool = True, **wiring_kw) -> BenchPod:
    bp = BenchPod(transport=FakeTransport(pod), lease=False, wiring={**BENCH, **wiring_kw})
    if dap_id:
        real = bp.supports
        bp.supports = lambda feature: True if feature == "dap_id" else real(feature)  # type: ignore[method-assign]
    return bp


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(prog.time, "sleep", lambda s: None)
    monkeypatch.setattr(detect.time, "sleep", lambda s: None)


# -- BenchPod.detect ----------------------------------------------------------------------------------

def test_detect_swd_two_calls_and_stm32f446():
    pod = FakePod()
    res = make(pod).detect()
    assert res.powered and not res.skipped
    (f,) = res.findings
    assert (f.chip, f.core, f.confidence, f.id) == ("STM32F446", "Cortex-M4", "high", "0x10006421")
    first, second = pod.sent("dap_start")
    assert first == {"cmd": "dap_start", "op": "id", "swclk": 11, "swdio": 12, "wait_ms": 3000}
    assert second["read"] == ["0xe0042000", "0x10000100"] and second["swclk"] == 11
    assert "wait_ms" not in second
    d = res.to_dict()
    assert d["powered"] is True and d["findings"][0]["chip"] == "STM32F446" and d["skipped"] == []
    assert json.dumps(d)


def test_detect_power_off_probes_nothing():
    pod = FakePod()
    pod.rails["efuse1"]["enabled"] = 0
    res = make(pod).detect()
    assert res.powered is False and res.findings == []
    assert "power" in res.notes[0] and "efuse 1" in res.notes[0]
    assert pod.sent("dap_start") == [] and pod.sent("la_pins") == []


def test_detect_without_a_rail_in_the_profile_takes_any_rail():
    pod = FakePod()
    res = make(pod, efuse=None).detect()
    assert res.powered is True
    pod.rails["efuse1"]["enabled"] = 0
    pod.rails["efuse2"]["enabled"] = 1
    assert make(pod, efuse=None).detect().powered is True
    pod.rails["efuse2"]["enabled"] = 0
    assert make(pod, efuse=None).detect().powered is False


def test_detect_tripped_rail_is_not_power():
    pod = FakePod()
    pod.rails["efuse1"].update(enabled=1, fault=1)
    res = make(pod).detect()
    assert res.powered is False and "tripped" in res.notes[0]


def test_detect_pin_owned_by_another_user_is_skipped_with_the_owner():
    pod = FakePod()
    pod.owners = {11: "uart_rx"}
    res = make(pod).detect()
    assert res.findings == [] and pod.sent("dap_start") == []
    assert res.skipped[0][0] == "swd" and "LA11" in res.skipped[0][1] and "uart_rx" in res.skipped[0][1]


def test_detect_without_dap_id_skips_swd_and_jtag():
    pod = FakePod()
    res = make(pod, dap_id=False, jtag_tck=11, jtag_tms=12, jtag_tdi=9, jtag_tdo=10).detect()
    assert res.findings == [] and pod.sent("dap_start") == []
    assert res.skipped == [("swd", "needs pod firmware with dap_id (update the pod)"),
                           ("jtag", "needs pod firmware with dap_id (update the pod)")]


def test_detect_jtag_chain():
    pod = FakePod()
    pod.dap["swd"] = {"port": "swd", "found": False}
    pod.jtag = {"port": "jtag", "found": True, "idcodes": ["0x4ba00477", "0x06421041"]}
    bp = make(pod, jtag_tck=11, jtag_tms=12, jtag_tdi=9, jtag_tdo=10)
    real = bp.supports
    bp.supports = lambda f: True if f in ("dap_id", "jtag") else real(f)  # type: ignore[method-assign]
    res = bp.detect()
    assert [f.chip for f in res.findings][1] == "STM32F446"
    assert pod.sent("dap_start", tck=11)[0]["tdo"] == 10
    assert pod.sent("dap_start", tck=11)[0]["wait_ms"] == 3000
    assert [m for m, _ in res.skipped] == ["swd"]      # nothing answered on SWD


def test_detect_swd_no_answer_is_skipped_not_raised():
    pod = FakePod()
    pod.dap["swd"] = {"port": "swd", "found": False}
    res = make(pod).detect()
    assert res.findings == [] and res.skipped[0][0] == "swd" and "nothing answered" in res.skipped[0][1]


def test_detect_probe_error_is_a_skip_but_connection_errors_raise():
    pod = FakePod()
    pod.dap_error = FirmwareError("pin conflict: LA11 is in use by gpio", cmd="dap_start")
    res = make(pod).detect()
    assert res.findings == [] and "pin conflict" in res.skipped[0][1]
    pod.dap_error = TransportError("connection lost")
    with pytest.raises(TransportError):
        make(pod).detect()


def test_detect_second_call_failing_keeps_the_debug_port_answer():
    pod = FakePod()
    calls = []
    real = pod._dap_start

    def flaky(req):
        calls.append(req)
        if req.get("read"):
            raise FirmwareError("busy", cmd="dap_start")
        return real(req)

    pod._dap_start = flaky
    res = make(pod).detect()
    assert res.findings[0].confidence == "low" and any("reading the ID registers failed" in n for n in res.notes)


def test_detect_needs_the_la_voltage():
    pod = FakePod()
    pod.mv = None
    res = make(pod, la_mv=None).detect()
    assert res.findings == [] and pod.sent("dap_start") == []
    assert any("LA I/O voltage is not set" in n for n in res.notes)
    assert res.skipped == [("swd", "the LA voltage is not set")]


PIC = {"signals": [sig("ICSPCLK", 7), sig("ICSPDAT", 8), sig("MCLR", 9)]}


def test_detect_pic_through_the_profile_signals():
    pod = FakePod()
    pod.prog_id = "3d8000"            # whatever the table says for some PIC; found below
    dev = prog.families()["pic"].devices[0]
    pod.prog_id = dev.id.replace("?", "0")
    res = make(pod, **PIC).detect(methods=["pic"])
    (f,) = res.findings
    assert f.method == "pic" and f.chip == dev.name and f.invasive and f.confidence == "high"
    assert f.pins == {"data": 8, "clk": 7, "reset": 9} and f.vendor == "Microchip"
    info = pod.sent("prog_flash", op="info")[0]
    assert (info["proto"], info["clk"], info["data"], info["reset"]) == ("pic", 7, 8, 9)
    assert pod.sent("dap_start") == []


def test_detect_prog_is_not_tried_on_the_swd_pins():
    pod = FakePod()
    res = make(pod).detect()
    assert pod.sent("prog_flash") == []
    assert any("not tried" in n and "pic" in n and "esp32" in n for n in res.notes)
    assert res.skipped == []


def test_detect_non_invasive_leaves_out_the_resetting_probes():
    pod = FakePod()
    res = make(pod, **PIC).detect(invasive=False)
    assert pod.sent("prog_flash") == []
    assert [m for m, _ in res.skipped] == ["pic"] and "invasive" in res.skipped[0][1]
    assert res.findings[0].method == "swd"


def test_detect_prog_no_answer_and_unknown_chip():
    pod = FakePod()
    pod.prog_result = "no answer"
    res = make(pod, **PIC).detect(methods=["pic"])
    assert res.findings == [] and res.skipped and res.skipped[0][0] == "pic"
    pod = FakePod()
    pod.prog_result = "ok"
    pod.prog_id = "ffffff"
    pod2 = make(pod, signals=[sig("UPDI", 6)])
    res = pod2.detect(methods=["updi"])
    (f,) = res.findings
    assert f.chip is None and f.confidence == "medium" and f.id == "0xffffff" and f.vendor


def test_detect_named_method_that_is_not_wired_is_skipped():
    res = make(FakePod()).detect(methods=["jtag", "esp32"])
    assert [m for m, _ in res.skipped] == ["jtag", "esp32"]
    with pytest.raises(ValueError, match="unknown detect method"):
        make(FakePod()).detect(methods=["spi"])


def test_detect_esp32():
    pod = FakePod()
    bp = make(pod, uart_rx=3, uart_tx=4, signals=[sig("BOOT", 6)], swd_nreset=True)
    res = bp.detect(methods=["esp32"])
    (f,) = res.findings
    assert (f.chip, f.vendor, f.confidence, f.invasive) == ("ESP32-S3", "Espressif", "high", True)
    sync = pod.sent("esp_flash", op="sync")[0]
    assert (sync["rx"], sync["tx"], sync["boot"], sync["en"]) == (3, 4, 6, "nrst")
    assert any("ROM loader" in n for n in res.notes)
    pod.esp_chip = ""
    res = bp.detect(methods=["esp32"])
    assert res.findings == [] and "ROM loader" in res.skipped[0][1]


def test_detect_usb_only_when_host_mode_is_on_and_a_device_is_mounted(monkeypatch):
    pod = FakePod()
    bp = make(pod)
    monkeypatch.setattr(bp, "usb_host_status", lambda: UsbHostState.from_reply(pod.usb))
    res = bp.detect(methods=["usb"])
    assert res.findings == [] and "never starts it" in res.skipped[0][1]
    assert pod.sent("usb_host", enabled=True) == []
    pod.usb = {"mode": "host", "port": "usb_a", "attached": True, "enumeration": "done", "connected": True,
               "device": {"vid": 0x2E8A, "pid": 0x000F, "manufacturer": "Raspberry Pi", "product": "RP2350 Boot"}}
    res = bp.detect(methods=["usb"])
    (f,) = res.findings
    assert (f.method, f.chip, f.id, f.confidence) == ("usb", "RP2350 (BOOTSEL)", "2e8a:000f", "high")
    # with the default methods, the unmounted/off case is a note, not a skipped entry
    pod.usb = {"mode": "device"}
    res = bp.detect()
    assert [m for m, _ in res.skipped] == [] and any("usb" in n for n in res.notes)


# -- sbw (MSP430 Spy-Bi-Wire) ------------------------------------------------------------------------

SBW = dict(signals=[sig("SBWTCK", 6), sig("SBWTDIO", 7)])


def make_sbw(pod: FakePod, named: bool = True, **kw) -> BenchPod:
    bp = make(pod, **{**SBW, **kw})
    real = bp.supports
    bp.supports = lambda f: named if f == "prog_named" else real(f)  # type: ignore[method-assign]
    return bp


def test_detect_sbw_methods_and_pins():
    assert "sbw" in detect.METHODS and "sbw" in detect.INVASIVE_METHODS
    got, _, _ = detect.prog_pins(_wiring(signals=[sig("TEST", 3), sig("RST", 4)]), "sbw")
    assert got.pins == {"data": 4, "clk": 3}  # no reset pin
    got, reason, silent = detect.prog_pins(_wiring(signals=[sig("SBWTCK", 3)]), "sbw")
    assert got is None and not silent and "SBWTDIO" in reason


def test_detect_sbw_finds_the_msp430fr2433():
    pod = FakePod()
    pod.prog_id = "984082"
    res = make_sbw(pod).detect(methods=["sbw"])
    (f,) = res.findings
    assert (f.chip, f.vendor, f.core, f.confidence, f.invasive) == (
        "MSP430FR2433", "Texas Instruments", "MSP430", "high", True)
    start = pod.sent("prog_flash", op="info")[0]
    assert start["proto"] == "sbw" and start["clk"] == 6 and start["data"] == 7 and "reset" not in start


def test_detect_sbw_jtag_locked_chip_is_low_confidence():
    pod = FakePod()
    pod.prog_id = "98"
    res = make_sbw(pod).detect(methods=["sbw"])
    (f,) = res.findings
    assert (f.chip, f.confidence, f.id) == ("MSP430 (JTAG-locked)", "low", "0x98")
    assert any("JTAG-locked" in n for n in res.notes)


def test_detect_sbw_skipped_on_old_firmware_and_without_invasive():
    pod = FakePod()
    res = make_sbw(pod, named=False).detect(methods=["sbw"])
    assert res.findings == [] and "4.0.6" in res.skipped[0][1] and pod.sent("prog_flash") == []
    res = make_sbw(FakePod()).detect(methods=["sbw"], invasive=False)
    assert "invasive" in res.skipped[0][1]
