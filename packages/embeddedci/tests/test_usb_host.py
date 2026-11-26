"""USB host mode and USB flashing (embeddedci.benchpod.usb_host) against a fake pod."""

from __future__ import annotations

import struct
from typing import Any, Dict, List, Optional

import pytest

from embeddedci.benchpod import BenchPod, usb_host
from embeddedci.benchpod.errors import (
    FirmwareError,
    UnsupportedFeatureError,
    UsbFlashError,
    UsbFlashRefusedError,
    UsbHostError,
    UsbHostPortError,
    UsbHostUnavailableError,
    UsbTimeout,
    XipUnavailableError,
    firmware_error,
)
from embeddedci.benchpod.transport.base import Transport
from embeddedci.benchpod.usb_host import UsbImage, choose_mode, load_image


def iface(cls: int, name: str = "", sub: int = 0, proto: int = 0, n: int = 0) -> Dict[str, Any]:
    return {"number": n, "alt": 0, "class": cls, "class_name": name, "subclass": sub,
            "protocol": proto, "endpoints": 2}


def device(vid: int, pid: int, *ifaces: Dict[str, Any], product: str = "Board") -> Dict[str, Any]:
    return {"vid": vid, "pid": pid, "speed": "full", "usb": "2.00", "release": "1.00", "class": 0,
            "subclass": 0, "protocol": 0, "ep0_size": 64, "configurations": 1,
            "manufacturer": "Acme", "product": product, "serial": "S1", "descriptor_ms": 100,
            "mounted_ms": 200,
            "config": {"value": 1, "max_power_ma": 100, "self_powered": False, "remote_wakeup": False,
                       "interface_count": len(ifaces), "truncated": False, "malformed": False,
                       "interfaces": list(ifaces)}}


PICO = device(0x2E8A, 0x0003, iface(8, "msc"), product="RP2 Boot")
TEENSY = device(0x16C0, 0x0478, iface(3, "hid"), product="Teensy")
STM32_DFU = device(0x0483, 0xDF11, iface(0xFE, "dfu", sub=1, proto=2))
UNO = device(0x2341, 0x0043, iface(2, "cdc"), iface(10, "cdc_data", n=1), product="Uno")
CH340 = device(0x1A86, 0x7523, iface(0xFF, "vendor"))
MKR = device(0x2341, 0x804E, iface(2, "cdc"), iface(10, "cdc_data", n=1), product="MKR1000")
WIO = device(0x2886, 0x802D, iface(2, "cdc"), product="Wio Terminal")
ESP_S3 = device(0x303A, 0x1001, iface(2, "cdc"), iface(10, "cdc_data", n=1))
NUCLEO = device(0x0D28, 0x0204, iface(8, "msc"), iface(2, "cdc", n=1), product="DAPLink")
UNKNOWN = device(0x1234, 0x5678, iface(3, "hid"))


class FakePod:
    """The pod's usb_host and usb_flash: host mode starts after `start_polls` reads, a flash runs
    for `polls` status calls and then ends with `result`."""

    def __init__(self, *, board_rev: str = "v4", dev: Optional[Dict[str, Any]] = None,
                 start_polls: int = 1, polls: int = 3, result: str = "ok", phase: str = "write",
                 firmware: str = "4.0.3", features_rev: int = 14, usb_a: Optional[bool] = None,
                 enumeration: str = "done", protocol: str = "") -> None:
        self.board_rev = board_rev
        self.dev = dev
        self.start_polls = start_polls
        self.polls = polls
        self.result = result
        self.phase = phase
        self.firmware = firmware
        self.features_rev = features_rev
        self.usb_a = (board_rev >= "v3r4") if usb_a is None else usb_a
        self.enumeration = enumeration
        self.protocol = protocol
        self.cmds: List[Dict[str, Any]] = []
        self.staged: List[bytes] = []
        self.mode = "device"
        self.port = "none"
        self._starting = 0
        self._job: Dict[str, Any] = {}
        self._left = 0
        self._have = False
        self.remaining = 60
        self.seq: List[str] = []

    def status(self) -> Dict[str, Any]:
        return {"api": 2, "min_client_api": 2, "version": self.firmware, "features_rev": self.features_rev,
                "gateware": 60, "fpga_image": "deep", "board_rev": self.board_rev, "hw": []}

    def host_state(self) -> Dict[str, Any]:
        has = self.mode == "host" and self.dev is not None
        d: Dict[str, Any] = {"mode": self.mode, "port": self.port, "remaining_s": self.remaining,
                             "host_ms": 1000, "enumerations": 1 if has else 0, "attached": has,
                             "enumeration": self.enumeration if has else "none", "connected": has,
                             "line_speed": "full" if has else "none"}
        if has:
            d["device"] = self.dev
        return d

    def command(self, req: Dict[str, Any]) -> Any:
        cmd = req.get("cmd")
        if cmd == "la_dir":      # the pin directions the SDK sends on connect
            return {}
        self.cmds.append(dict(req))
        if cmd == "usb_host":
            if "enabled" in req:
                if not req["enabled"]:
                    self.mode, self.port = "device", "none"
                    return self.host_state()
                port = req.get("port")
                if port == "usb_a" and not self.usb_a:
                    raise firmware_error("", cmd="usb_host", code="BP0077", values={"port": "usb_a"})
                if port is None and not self.usb_a:
                    raise firmware_error("", cmd="usb_host", code="BP0006", values={"arg": "port"})
                self.port = port or "usb_a"
                self.remaining = int(req.get("timeout_s", 60))
                self.mode = "starting"
                self._starting = self.start_polls
                self.seq.append("host_start")
                return {"mode": "starting"}
            if self.mode == "starting":
                self._starting -= 1
                if self._starting <= 0:
                    self.mode = "host"
            return self.host_state()
        if cmd == "usb_flash":
            if req.get("abort"):
                self._left = 0
                self._have = True
                self.result = "aborted"
                return self._flash_state(False)
            if "mode" in req:
                self.seq.append("flash_start")
                self._job = dict(req)
                self._left = self.polls
                self._have = False
                return {"started": True}
            if self._left > 0:
                self._left -= 1
                if self._left == 0:
                    self._have = True
                return self._flash_state(self._left > 0)
            return self._flash_state(False)
        raise AssertionError(f"unexpected command {req}")

    def _flash_state(self, running: bool) -> Dict[str, Any]:
        n = (self._job.get("len", 0) + 511) // 512
        d: Dict[str, Any] = {"running": running, "have_result": self._have, "mode": self._job.get("mode", ""),
                             "name": self._job.get("name", ""), "len": self._job.get("len", 0),
                             "address": self._job.get("address", 0), "protocol": self.protocol,
                             "phase": self.phase if running else ("done" if self.result == "ok" else self.phase),
                             "done": n // 2 if running else (n if self.result == "ok" else n // 2),
                             "total": n}
        if not running:
            d.update(result=self.result, ok=self.result == "ok", ms=1840)
        return d

    def flashes(self) -> List[Dict[str, Any]]:
        return [c for c in self.cmds if c.get("cmd") == "usb_flash" and "mode" in c]


class FakeTransport(Transport):
    def __init__(self, pod: FakePod, stage: bool = True) -> None:
        self.pod = pod
        if not stage:
            self.stage_psram = None   # type: ignore[assignment]

    def command(self, req: Dict[str, Any]) -> Any:
        return self.pod.command(req)

    def stage_psram(self, data: bytes) -> int:
        self.pod.seq.append("stage")
        self.pod.staged.append(bytes(data))
        return len(data)

    def close(self) -> None:
        pass

    def status(self) -> Any:
        return self.pod.status()

    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        pass

    def uart_proxy_start(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError


def make(pod: FakePod, **kw: Any) -> BenchPod:
    return BenchPod(transport=FakeTransport(pod, **kw), lease=False, wiring={})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(usb_host.time, "sleep", lambda s: None)
    from embeddedci.benchpod import client
    monkeypatch.setattr(client.time, "sleep", lambda s: None)


# -- feature gating -----------------------------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda bp: bp.usb_host(),
    lambda bp: bp.usb_host_status(),
    lambda bp: bp.usb_wait_device(),
    lambda bp: bp.usb_flash(b"\x00" * 16, mode="avr"),
    lambda bp: bp.usb_flash_status(),
])
def test_old_firmware_gets_a_clear_error(call):
    pod = FakePod(firmware="4.0.2", features_rev=13)
    with pytest.raises(UnsupportedFeatureError, match="needs firmware 4.0.3") as info:
        call(make(pod))
    assert info.value.feature in ("usb_host_mode", "usb_flash")
    assert pod.cmds == []     # nothing was sent


# -- usb_host and its port ----------------------------------------------------------------------

def test_usb_host_picks_usb_a_on_v4_and_waits():
    pod = FakePod(board_rev="v4", dev=PICO, start_polls=3)
    st = make(pod).usb_host(timeout_s=120)
    assert st.hosting and st.port == "usb_a"
    start = [c for c in pod.cmds if c.get("enabled")][0]
    assert start == {"cmd": "usb_host", "enabled": True, "port": "usb_a", "timeout_s": 120}
    assert len([c for c in pod.cmds if c == {"cmd": "usb_host"}]) == 3


def test_usb_host_picks_usb_a_on_v3r4_name():
    # v3r4 = the pre-rename name of v4 (firmware 4.0.x)
    pod = FakePod(board_rev="v3r4", dev=PICO, usb_a=True)
    assert make(pod).usb_host().port == "usb_a"


def test_usb_host_picks_usb_c_on_older_boards():
    pod = FakePod(board_rev="v3r2", dev=PICO)
    assert make(pod).usb_host().port == "usb_c"
    assert [c["port"] for c in pod.cmds if c.get("enabled")] == ["usb_c"]


def test_usb_host_without_board_rev_tries_usb_a_then_usb_c():
    pod = FakePod(board_rev="", dev=PICO, usb_a=False)
    st = make(pod).usb_host()
    assert st.port == "usb_c"
    assert [c["port"] for c in pod.cmds if c.get("enabled")] == ["usb_a", "usb_c"]
    pod = FakePod(board_rev="", dev=PICO, usb_a=True)
    assert make(pod).usb_host().port == "usb_a"


def test_usb_host_explicit_port_is_not_retried():
    pod = FakePod(board_rev="v3r2", usb_a=False)
    with pytest.raises(UsbHostUnavailableError) as info:
        make(pod).usb_host(port="usb_a")
    assert info.value.code == "BP0077" and info.value.port == "usb_a" and info.value.feature == "usb_host"
    assert len([c for c in pod.cmds if c.get("enabled")]) == 1


def test_usb_host_rejects_a_bad_port_and_timeout():
    bp = make(FakePod())
    with pytest.raises(ValueError):
        bp.usb_host(port="usb_b")
    with pytest.raises(ValueError):
        bp.usb_host(timeout_s=0)


def test_usb_host_never_reaching_host_mode_times_out(monkeypatch):
    pod = FakePod(start_polls=10 ** 9)
    ticks = iter(range(0, 10 ** 6))
    monkeypatch.setattr("embeddedci.benchpod.client.time.monotonic", lambda: next(ticks))
    with pytest.raises(UsbTimeout, match="did not reach 'host'"):
        make(pod).usb_host()


def test_usb_host_off():
    pod = FakePod(dev=PICO)
    bp = make(pod)
    bp.usb_host()
    st = bp.usb_host(False)
    assert st.mode == "device" and st.port == "none" and not st.hosting
    assert {"cmd": "usb_host", "enabled": False} in pod.cmds


def test_port_errors_are_typed():
    over_usb = firmware_error("", cmd="usb_host", code="BP0076")
    assert isinstance(over_usb, UsbHostUnavailableError) and over_usb.feature == "usb_host"
    other = firmware_error("", cmd="usb_host", code="BP0079", values={"port": "usb_c"})
    assert isinstance(other, UsbHostPortError) and other.port == "usb_c"
    refused = firmware_error("", cmd="usb_flash", code="BP0084", values={"why": "no drive mounted"})
    assert isinstance(refused, UsbFlashRefusedError) and refused.why == "no drive mounted"
    xip = firmware_error("", cmd="usb_flash", code="BP0083", values={"why": "slot missing"})
    assert isinstance(xip, XipUnavailableError) and xip.why == "slot missing"
    assert all(isinstance(e, FirmwareError) for e in (over_usb, other, refused, xip))


def test_a_coded_refusal_from_the_pod_is_classified():
    from embeddedci.benchpod import errors
    pod = FakePod()
    real = pod.command

    def refuse(req):
        if req.get("enabled"):
            raise errors._attach_code(FirmwareError("", cmd="usb_host"), "BP0076", {})
        return real(req)

    pod.command = refuse   # type: ignore[assignment]
    with pytest.raises(UsbHostUnavailableError):
        make(pod).usb_host(port="usb_c")


# -- status and waiting -------------------------------------------------------------------------

def test_status_is_typed():
    pod = FakePod(dev=PICO)
    bp = make(pod)
    assert bp.usb_host_status().mode == "device" and bp.usb_host_status().device is None
    bp.usb_host()
    st = bp.usb_host_status()
    assert st.hosting and st.mounted and st.attached and st.enumeration == "done"
    assert st.connected and st.line_speed == "full" and st.enumerations == 1
    d = st.device
    assert d is not None and (d.vid, d.pid, d.vid_pid) == (0x2E8A, 0x0003, "2e8a:0003")
    assert d.product == "RP2 Boot" and d.speed == "full" and d.mounted_ms == 200
    assert d.config is not None and d.config.max_power_ma == 100 and not d.config.truncated
    assert d.has_interface(8) and not d.has_interface(2) and "2e8a:0003" in str(d)
    assert d.interfaces[0].class_name == "msc"


def test_wait_device_matches_ids():
    pod = FakePod(dev=TEENSY)
    bp = make(pod)
    bp.usb_host()
    assert bp.usb_wait_device().vid == 0x16C0
    assert bp.usb_wait_device(vid=0x16C0, pid=0x0478).pid == 0x0478
    assert bp.usb_wait_device(vid="16c0", pid="0x478").pid == 0x0478


def test_wait_device_waits_for_enumeration():
    states = [{"mode": "host", "port": "usb_a", "attached": False, "enumeration": "none", "connected": True},
              {"mode": "host", "port": "usb_a", "attached": True, "enumeration": "running",
               "connected": True, "device": PICO},
              {"mode": "host", "port": "usb_a", "attached": True, "enumeration": "done",
               "connected": True, "device": PICO}]
    seen = []

    def read():
        seen.append(1)
        return usb_host.UsbHostState.from_reply(states[min(len(seen) - 1, 2)])

    dev = usb_host.wait_device(read, timeout=5, sleep=lambda s: None)
    assert dev.pid == 3 and len(seen) == 3


@pytest.mark.parametrize("state, hint", [
    ({"mode": "host", "connected": False, "line_speed": "none"}, "no device pull-up"),
    ({"mode": "host", "connected": True, "line_speed": "low"}, "swapped"),
    ({"mode": "host", "connected": True, "attached": True, "enumeration": "stuck"}, "stuck"),
    ({"mode": "host", "connected": True, "attached": True, "enumeration": "done", "device": PICO},
     "2e8a:0003 is mounted, not 16c0:0478"),
])
def test_wait_device_timeout_says_how_far_it_got(state, hint):
    t = iter(range(100))
    with pytest.raises(UsbTimeout, match=hint):
        usb_host.wait_device(lambda: usb_host.UsbHostState.from_reply(state), vid=0x16C0, pid=0x0478,
                             timeout=3, sleep=lambda s: None, clock=lambda: next(t))


def test_wait_device_with_host_mode_off():
    with pytest.raises(UsbHostError, match="usb_host"):
        make(FakePod(dev=PICO)).usb_wait_device()
    assert not issubclass(UsbHostError, TimeoutError) and issubclass(UsbTimeout, TimeoutError)


# -- files --------------------------------------------------------------------------------------

def ihex(addr: int, data: bytes, ext: int = 0) -> str:
    def rec(typ: int, a: int, payload: bytes) -> str:
        body = bytes([len(payload), a >> 8 & 0xFF, a & 0xFF, typ]) + payload
        return ":" + (body + bytes([(-sum(body)) & 0xFF])).hex().upper()

    lines = []
    if ext:
        lines.append(rec(4, 0, ext.to_bytes(2, "big")))
    for i in range(0, len(data), 16):
        lines.append(rec(0, addr + i, data[i:i + 16]))
    lines.append(rec(1, 0, b""))
    return "\n".join(lines) + "\n"


def dfuse(elements) -> bytes:
    body = b""
    for addr, data in elements:
        body += struct.pack("<II", addr, len(data)) + data
    target = b"Target" + b"\x00" + struct.pack("<I", 0) + b"\x00" * 255 + struct.pack("<II", len(body), len(elements))
    target += body
    prefix = b"DfuSe" + b"\x01" + struct.pack("<I", 11 + len(target)) + b"\x01"
    return prefix + target + b"\x00" * 8 + b"UFD\x10" + b"\x00" * 4


def test_intel_hex_conversion(tmp_path):
    payload = bytes(range(40))
    f = tmp_path / "app.hex"
    f.write_text(ihex(0x1000, payload, ext=0x0800))
    img = load_image(str(f))
    assert img.kind == "hex"
    data, addr = img.binary()
    assert addr == 0x08001000 and data == payload


def test_intel_hex_gap_is_filled_and_big_gap_refused(tmp_path):
    f = tmp_path / "gap.hex"
    f.write_text(ihex(0, b"\x01" * 16).replace(":00000001FF\n", "") + ihex(0x20, b"\x02" * 16))
    data, addr = load_image(str(f)).binary()
    assert addr == 0 and data == b"\x01" * 16 + b"\xff" * 16 + b"\x02" * 16
    far = tmp_path / "far.hex"
    far.write_text(ihex(0, b"\x01" * 16).replace(":00000001FF\n", "") + ihex(0, b"\x02" * 16, ext=0x0010))
    with pytest.raises(ValueError, match="gap"):
        load_image(str(far)).binary()


def test_intel_hex_checksum_is_checked(tmp_path):
    f = tmp_path / "bad.hex"
    first = ihex(0, b"\x01" * 4).splitlines()[0]
    f.write_text(first[:-2] + ("00" if not first.endswith("00") else "01") + "\n:00000001FF\n")
    with pytest.raises(ValueError, match="checksum"):
        load_image(str(f)).binary()


def test_dfuse_conversion(tmp_path):
    f = tmp_path / "fw.dfu"
    f.write_bytes(dfuse([(0x08000000, b"\xaa" * 8), (0x08000008, b"\xbb" * 8)]))
    img = load_image(str(f))
    assert img.kind == "dfu"
    data, addr = img.binary()
    assert addr == 0x08000000 and data == b"\xaa" * 8 + b"\xbb" * 8


def test_plain_dfu_file_loses_its_suffix(tmp_path):
    f = tmp_path / "plain.dfu"
    f.write_bytes(b"\x11" * 32 + b"\x00" * 8 + b"UFD\x10" + b"\x00" * 4)
    data, addr = load_image(str(f)).binary()
    assert data == b"\x11" * 32 and addr is None


def test_file_kinds_by_content_and_extension(tmp_path):
    assert load_image(b"UF2\n" + b"\x00" * 508).kind == "uf2"
    assert load_image(b"\xe9\x00\x00").looks_like_esp
    assert load_image(ihex(0, b"\x01" * 4).encode()).kind == "hex"
    assert load_image(b"\x00\x01\x02").kind == "bin"
    with pytest.raises(ValueError, match="empty"):
        load_image(b"")
    with pytest.raises(ValueError, match="UF2"):
        load_image(b"UF2\n").binary()


def test_fat_names():
    assert usb_host.fat_name("/x/firmware.bin") == "FIRMWARE.BIN"
    assert usb_host.fat_name("my long firmware.hex") == "MYLONGFI.HEX"
    assert usb_host.fat_name("a.b.bin") == "A_B.BIN"
    assert usb_host.fat_name("noext") == "NOEXT"


# -- the choice of loader -----------------------------------------------------------------------

BIN = UsbImage(raw=b"\x00\x01" * 8, kind="bin", path="fw.bin")
ESP = UsbImage(raw=b"\xe9" + b"\x00" * 15, kind="bin", path="merged.bin")
UF2 = UsbImage(raw=b"UF2\n" + b"\x00" * 508, kind="uf2", path="fw.uf2")
HEX = load_image(ihex(0x2000, b"\x03" * 16).encode())
DFU_HEX = load_image(ihex(0, b"\x04" * 16, ext=0x0800).encode())


def dev(d: Dict[str, Any]) -> usb_host.UsbDevice:
    return usb_host.UsbDevice.from_reply(d)


@pytest.mark.parametrize("image, d, mode, address", [
    (UF2, PICO, "uf2", None),
    (BIN, PICO, "picoboot", 0x10000000),
    (BIN, device(0x2E8A, 0x000F, iface(8, "msc")), "picoboot", 0x10000000),
    (UF2, device(0x2E8A, 0x000F, iface(8, "msc")), "uf2", None),
    (HEX, NUCLEO, "file", None),
    (ESP, device(0x303A, 0x0002, iface(8, "msc")), "esp", 0),
    (BIN, TEENSY, "teensy", 0),
    (BIN, STM32_DFU, "dfu", 0x08000000),
    (DFU_HEX, STM32_DFU, "dfu", 0x08000000),
    (BIN, device(0x1209, 0x0001, iface(0xFE, "dfu", sub=1, proto=2)), "dfu", None),
    (BIN, UNO, "avr", 0),
    (HEX, UNO, "avr", 0x2000),
    (BIN, device(0x2341, 0x8036, iface(2, "cdc")), "avr", 0),
    (BIN, device(0x2A03, 0x0043, iface(2, "cdc")), "avr", 0),
    (BIN, device(0x1B4F, 0x9206, iface(2, "cdc")), "avr", 0),
    (BIN, device(0x239A, 0x800C, iface(2, "cdc")), "avr", 0),
    (BIN, CH340, "avr", 0),
    (BIN, MKR, "samd", 0x2000),
    (HEX, MKR, "samd", 0x2000),
    (BIN, device(0x239A, 0x800B, iface(2, "cdc")), "samd", 0x2000),
    (BIN, WIO, "samd", 0x4000),
    (BIN, ESP_S3, "esp", 0),
    (ESP, CH340, "esp", 0),
    (ESP, device(0x10C4, 0xEA60, iface(0xFF, "vendor")), "esp", 0),
])
def test_choose_mode(image, d, mode, address):
    plan = choose_mode(image, dev(d))
    assert (plan.mode, plan.address) == (mode, address)
    assert plan.reason


def test_teensy_4_hex_address_is_relative_to_the_flash():
    t4 = load_image(ihex(0, b"\x05" * 16, ext=0x6000).encode())
    assert t4.binary()[1] == 0x60000000
    assert choose_mode(t4, dev(TEENSY)).address == 0


def test_choose_mode_overrides_and_errors():
    assert choose_mode(BIN, dev(UNO), mode="samd").mode == "samd"
    assert choose_mode(BIN, dev(UNO), address=0x4000, mode="samd").address == 0x4000
    assert choose_mode(BIN, dev(STM32_DFU), address=0x08010000).address == 0x08010000
    with pytest.raises(ValueError, match="mode must be"):
        choose_mode(BIN, dev(UNO), mode="jtag")
    with pytest.raises(UsbFlashError, match="cannot tell how to flash 1234:5678"):
        choose_mode(BIN, dev(UNKNOWN))
    with pytest.raises(UsbFlashError, match="UF2 drive"):
        choose_mode(UF2, dev(UNO))
    with pytest.raises(UsbFlashError, match="needs a .uf2"):
        choose_mode(BIN, dev(PICO), mode="uf2")
    with pytest.raises(UsbFlashError, match="not a UF2 file"):
        choose_mode(UF2, dev(UNO), mode="avr")


# -- usb_flash ----------------------------------------------------------------------------------

def test_flash_a_uf2_to_a_pico_starts_host_stages_and_polls(tmp_path):
    f = tmp_path / "blink.uf2"
    f.write_bytes(b"UF2\n" + b"\x00" * 1020)
    pod = FakePod(dev=PICO, polls=3)
    seen = []
    res = make(pod).usb_flash(str(f), progress=lambda d, t: seen.append((d, t)))
    assert res.ok and res.mode == "uf2" and res.length == 1024 and res.ms == 1840
    assert "UF2" in res.reason and res.device is not None and res.device.vid == 0x2E8A
    assert pod.staged == [f.read_bytes()]
    flash = pod.flashes()[0]
    assert flash == {"cmd": "usb_flash", "mode": "uf2", "len": 1024}
    # Host mode first (renewed to 300 s), the file staged before the flash starts.
    host = [c for c in pod.cmds if c.get("enabled")][0]
    assert host["port"] == "usb_a" and host["timeout_s"] == 300
    assert pod.seq == ["host_start", "stage", "flash_start"]
    assert seen and all(t == 2 for _d, t in seen) and seen[-1][0] <= 2
    assert "uf2 loader" in str(res)


def test_flash_does_not_restart_host_mode_that_runs(tmp_path):
    f = tmp_path / "fw.bin"
    f.write_bytes(b"\x01" * 100)
    pod = FakePod(dev=NUCLEO, polls=1)
    bp = make(pod)
    bp.usb_host(port="usb_a", timeout_s=900)
    pod.remaining = 900
    before = len([c for c in pod.cmds if c.get("enabled")])
    res = bp.usb_flash(str(f))
    assert len([c for c in pod.cmds if c.get("enabled")]) == before
    assert res.mode == "file" and res.name == "FW.BIN"
    assert pod.flashes()[0] == {"cmd": "usb_flash", "mode": "file", "len": 100, "name": "FW.BIN"}


def test_flash_renews_host_mode_that_is_about_to_end(tmp_path):
    f = tmp_path / "fw.bin"
    f.write_bytes(b"\x01" * 100)
    pod = FakePod(dev=NUCLEO, polls=1)
    bp = make(pod)
    bp.usb_host(port="usb_a")
    pod.remaining = 20
    bp.usb_flash(str(f), name="APP.BIN")
    renew = [c for c in pod.cmds if c.get("enabled")][-1]
    assert renew["port"] == "usb_a" and renew["timeout_s"] == 300
    assert pod.flashes()[0]["name"] == "APP.BIN"


def test_flash_dfu_from_a_hex_file(tmp_path):
    f = tmp_path / "app.hex"
    f.write_text(ihex(0x4000, b"\x07" * 64, ext=0x0800))
    pod = FakePod(dev=STM32_DFU)
    res = make(pod).usb_flash(str(f), leave=False)
    assert res.mode == "dfu" and res.address == 0x08004000 and res.length == 64
    assert pod.flashes()[0] == {"cmd": "usb_flash", "mode": "dfu", "len": 64, "address": 0x08004000,
                                "leave": False}
    assert pod.staged == [b"\x07" * 64]      # the binary, not the hex text


def test_flash_dfu_file(tmp_path):
    f = tmp_path / "app.dfu"
    f.write_bytes(dfuse([(0x08000000, b"\x09" * 32)]))
    pod = FakePod(dev=STM32_DFU)
    res = make(pod).usb_flash(f)
    assert res.mode == "dfu" and pod.staged == [b"\x09" * 32]
    assert pod.flashes()[0]["address"] == 0x08000000 and "leave" not in pod.flashes()[0]


@pytest.mark.parametrize("d, name, data, mode, address, protocol", [
    (TEENSY, "fw.bin", b"\x01" * 64, "teensy", 0, ""),
    (UNO, "sketch.bin", b"\x02" * 64, "avr", 0, "optiboot"),
    (MKR, "sketch.bin", b"\x03" * 64, "samd", 0x2000, "sam-ba"),
    (ESP_S3, "merged.bin", b"\xe9" * 64, "esp", 0, "ESP32-S3"),
])
def test_flash_each_loader(tmp_path, d, name, data, mode, address, protocol):
    f = tmp_path / name
    f.write_bytes(data)
    pod = FakePod(dev=d, protocol=protocol)
    res = make(pod).usb_flash(str(f), address=None if address == 0 else address)
    assert res.mode == mode and res.protocol == protocol
    flash = pod.flashes()[0]
    assert flash["mode"] == mode and flash["address"] == address and flash["len"] == 64
    assert "name" not in flash


def test_flash_esp_address_from_the_caller(tmp_path):
    f = tmp_path / "app.bin"
    f.write_bytes(b"\xe9" * 64)
    pod = FakePod(dev=ESP_S3)
    make(pod).usb_flash(str(f), address="0x10000")
    assert pod.flashes()[0]["address"] == 0x10000


def test_flash_failure_raises_with_the_pods_result(tmp_path):
    f = tmp_path / "fw.bin"
    f.write_bytes(b"\x01" * 600)
    pod = FakePod(dev=STM32_DFU, result="the image does not fit the loader's flash layout", phase="layout")
    with pytest.raises(UsbFlashError, match="does not fit") as info:
        make(pod).usb_flash(str(f))
    e = info.value
    assert e.result == "the image does not fit the loader's flash layout" and e.mode == "dfu"
    assert e.phase == "layout" and e.state is not None and not e.state.ok


def test_flash_refused_by_the_pod_is_typed(tmp_path):
    f = tmp_path / "fw.bin"
    f.write_bytes(b"\x01" * 600)
    pod = FakePod(dev=STM32_DFU)
    real = pod.command

    def refuse(req):
        if req.get("mode"):
            raise firmware_error("", cmd="usb_flash", code="BP0084", values={"why": "nothing staged"})
        return real(req)

    pod.command = refuse   # type: ignore[assignment]
    with pytest.raises(UsbFlashRefusedError) as info:
        make(pod).usb_flash(str(f))
    assert info.value.why == "nothing staged" and info.value.code == "BP0084"


def test_flash_timeout_aborts():
    pod = FakePod(polls=10 ** 9)
    clock = iter(range(0, 10 ** 6, 10))
    cmds = pod.command
    with pytest.raises(UsbFlashError, match="did not finish within 30 s") as info:
        usb_host.run_flash(cmds, lambda b: None, b"x" * 64, {"cmd": "usb_flash", "mode": "avr", "len": 64},
                           timeout=30, sleep=lambda s: None, clock=lambda: next(clock))
    assert info.value.result == "timeout"
    assert {"cmd": "usb_flash", "abort": True} in pod.cmds


def test_flash_interrupt_aborts_and_reraises():
    pod = FakePod(polls=10)
    n = []

    def progress(d, t):
        n.append(1)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        usb_host.run_flash(pod.command, lambda b: None, b"x" * 2048,
                           {"cmd": "usb_flash", "mode": "avr", "len": 2048}, timeout=100,
                           progress=progress, sleep=lambda s: None)
    assert {"cmd": "usb_flash", "abort": True} in pod.cmds


def test_abort_and_status():
    pod = FakePod(dev=PICO, polls=5)
    bp = make(pod)
    bp.usb_host()
    pod.command({"cmd": "usb_flash", "mode": "uf2", "len": 512})
    assert bp.usb_flash_status().running
    st = bp.usb_flash_abort()
    assert st.result == "aborted" and not st.running and st.have_result


def test_flash_needs_a_staging_transport(tmp_path):
    f = tmp_path / "fw.bin"
    f.write_bytes(b"\x01" * 16)
    from embeddedci.benchpod.errors import FlashError
    with pytest.raises(FlashError, match="network"):
        make(FakePod(dev=PICO), stage=False).usb_flash(str(f))


def test_flash_no_device(tmp_path):
    f = tmp_path / "fw.bin"
    f.write_bytes(b"\x01" * 16)
    pod = FakePod(dev=None)
    t = iter(range(10 ** 6))
    bp = make(pod)
    import embeddedci.benchpod.usb_host as u
    orig = u.wait_device
    u.wait_device = lambda *a, **k: orig(*a, **{**k, "clock": lambda: next(t)})   # type: ignore[assignment]
    try:
        with pytest.raises(UsbTimeout, match="no device pull-up"):
            bp.usb_flash(str(f), device_timeout=3)
    finally:
        u.wait_device = orig
    assert pod.flashes() == [] and pod.staged == []


def test_flash_bytes_input():
    pod = FakePod(dev=UNO)
    res = make(pod).usb_flash(b"\x01" * 33, mode="avr")
    assert res.mode == "avr" and pod.staged == [b"\x01" * 33] and "was requested" in res.reason


def test_flash_timeout_default_scales_with_size():
    assert usb_host.flash_timeout(0) == 60
    assert usb_host.flash_timeout(1 << 20) > 400


def test_flash_state_unit():
    assert usb_host.UsbFlashState.from_reply({"mode": "avr"}).unit == "bytes"
    assert usb_host.UsbFlashState.from_reply({"mode": "uf2"}).unit == "sectors"
    assert usb_host.UsbFlashState.from_reply({}).unit == ""
