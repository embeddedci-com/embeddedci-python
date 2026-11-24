"""Flashing chips over their programming pins (embeddedci.benchpod.prog) against a fake pod."""

import json
import struct
from typing import Any, Dict, List, Optional

import pytest

from embeddedci.benchpod import BenchPod, prog
from embeddedci.benchpod.errors import (FlashError, PodBusyError, TargetUnreachableError,
                                        UnsupportedFeatureError)
from embeddedci.benchpod.transport.base import Transport
from embeddedci.benchpod.wiring import Wiring


# -- a fake pod ----------------------------------------------------------------------------------

class FakePod:
    """The pod's ``prog_flash`` command: ``info`` and ``start`` run for ``polls`` status calls, then
    finish with ``result`` (``info`` reports ``chip_id``)."""

    def __init__(self, chip_id: str = "1e9421", result: str = "ok", polls: int = 2,
                 info_result: str = "ok", at_addr: Optional[int] = None) -> None:
        self.chip_id = chip_id
        self.result = result
        self.info_result = info_result
        self.polls = polls
        self.at_addr = at_addr
        self.cmds: List[Dict[str, Any]] = []
        self.staged: List[bytes] = []
        self._left = 0
        self._job: Dict[str, Any] = {}
        self.fail_command: Optional[Exception] = None

    def stage(self, data: bytes) -> int:
        self.staged.append(bytes(data))
        return len(data)

    def command(self, req: Dict[str, Any]) -> Any:
        self.cmds.append(dict(req))
        if req.get("cmd") == "la_voltage":
            return {"mv": self.mv} if getattr(self, "mv", None) else {}
        if req.get("cmd") != "prog_flash":
            return {}
        if self.fail_command is not None and req["op"] in ("info", "start"):
            raise self.fail_command
        op = req["op"]
        if op in ("start", "info"):
            self._job = dict(req)
            self._left = self.polls
            return {"started": True, "op": op, "proto": req["proto"]}
        if op == "abort":
            self._left = 0
            return {"running": False, "result": "aborted"}
        self._left -= 1
        running = self._left > 0
        info = self._job["op"] == "info"
        total = 1 if info else max(1, self._job["len"] // max(1, self._job["page"]))
        res = self.info_result if info else self.result
        st = {"running": running, "op": self._job["op"], "proto": self._job["proto"],
              "phase": "write" if running else "done", "done": 0 if running else total,
              "total": total, "id": self.chip_id, "ms": 10, "protos": ["updi", "pic", "c2", "tpi", "pdi"],
              "result": None if running else res}
        if not running and self.at_addr is not None and res.startswith("verify"):
            st["at_addr"] = self.at_addr
        return st

    def starts(self) -> List[Dict[str, Any]]:
        return [c for c in self.cmds if c.get("cmd") == "prog_flash" and c["op"] == "start"]

    def infos(self) -> List[Dict[str, Any]]:
        return [c for c in self.cmds if c.get("cmd") == "prog_flash" and c["op"] == "info"]


class FakeTransport(Transport):
    def __init__(self, pod: FakePod, features_rev: int = 17) -> None:
        self.pod = pod
        self.features_rev = features_rev
        self.powered: List[Any] = []

    def command(self, req: Dict[str, Any]) -> Any:
        return self.pod.command(req)

    def stage_psram(self, data: bytes) -> int:
        return self.pod.stage(data)

    def close(self) -> None:
        pass

    def status(self) -> Any:
        return {"fpga_image": "deep", "api": 2, "min_client_api": 2, "gateware": 61,
                "features_rev": self.features_rev, "version": "4.0.4"}

    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        self.powered.append((efuse, on))

    def uart_proxy_start(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError


def make_pod(pod: FakePod, signals=(), **wiring_kw) -> BenchPod:
    wiring = {"swd_swclk": 11, "swd_swdio": 12, "signals": list(signals), **wiring_kw}
    return BenchPod(transport=FakeTransport(pod), lease=False, wiring=wiring)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(prog.time, "sleep", lambda s: None)
    monkeypatch.setattr("embeddedci.benchpod.client.time.sleep", lambda s: None)


# -- image builders ------------------------------------------------------------------------------

def ihex(*runs) -> str:
    """Intel HEX text for (address, bytes) runs (extended linear address records as needed)."""
    out, hi = [], None

    def rec(rtype, addr, data=b""):
        raw = bytes([len(data), addr >> 8, addr & 0xFF, rtype]) + bytes(data)
        out.append(":" + (raw + bytes([(-sum(raw)) & 0xFF])).hex().upper())

    for addr, data in runs:
        for i in range(0, len(data), 16):
            a = addr + i
            if (a >> 16) != hi:
                hi = a >> 16
                rec(4, 0, struct.pack(">H", hi))
            rec(0, a & 0xFFFF, data[i:i + 16])
    rec(1, 0)
    return "\n".join(out) + "\n"


def elf32(*segs) -> bytes:
    """A minimal 32-bit little-endian ELF with one PT_LOAD per (paddr, bytes)."""
    phoff = 52
    hdr = bytearray(52)
    hdr[0:4] = b"\x7fELF"
    hdr[4], hdr[5], hdr[6] = 1, 1, 1
    struct.pack_into("<HHIIIIIHHHHHH", hdr, 16, 2, 83, 1, 0, phoff, 0, 0, 52, 32, len(segs), 0, 0, 0)
    off = phoff + 32 * len(segs)
    ph, body = b"", b""
    for paddr, data in segs:
        ph += struct.pack("<IIIIIIII", 1, off + len(body), paddr, paddr, len(data), len(data), 5, 1)
        body += data
    return bytes(hdr) + ph + body


T1616 = "1e9421"


def write(tmp_path, name, content) -> str:
    p = tmp_path / name
    p.write_bytes(content if isinstance(content, bytes) else content.encode())
    return str(p)


# -- the table -----------------------------------------------------------------------------------

def test_table_loads_every_family():
    fams = prog.families()
    assert set(fams) == {"updi", "pic", "c2", "tpi", "pdi"}
    for proto, fam in fams.items():
        assert fam.devices, proto
        assert prog.TARGETS[fam.target] == proto


def test_table_entries_are_consistent():
    for proto, fam in prog.families().items():
        seen = set()
        for d in fam.devices:
            assert d.id and d.id == d.id.lower() and len(d.expect_id) % 2 == 0, d.name
            assert len(d.expect_id) <= 16, d.name
            key = (d.id, tuple(sorted(d.mem)), d.name)
            assert key not in seen, d.name
            seen.add(key)
            assert "flash" in d.mem, d.name
            spans = []
            for name, m in d.mem.items():
                assert name in prog.MEM_ORDER, (d.name, name)
                assert m.size > 0 and m.page > 0 and m.addr >= 0 and m.hex >= 0, (d.name, name)
                assert m.size <= 0x400000, (d.name, name)
                if name == "flash":
                    if proto != "c2":   # C2 flash stops below the lock byte
                        assert m.size % m.page == 0, d.name
                        assert m.align % m.page == 0 or m.page % m.align == 0, d.name
                spans.append((m.hex, m.hex + m.size * m.stride, name))
            spans.sort()
            for (a0, a1, n0), (b0, _b1, n1) in zip(spans, spans[1:]):
                assert a1 <= b0, f"{d.name}: {n0} and {n1} overlap in the image"
            assert d.vmin_mv <= d.vmax_mv or not d.vmax_mv, d.name
        # names and aliases never collide inside a family
        names = [prog._norm(n) for d in fam.devices for n in (d.name, *d.aliases)]
        assert len(names) == len(set(names)), proto


def test_tpi_five_volt_chips_are_marked():
    tpi = {d.name: d for d in prog.families()["tpi"].devices}
    for name in ("ATtiny4", "ATtiny5", "ATtiny9", "ATtiny10", "ATtiny20", "ATtiny40"):
        assert tpi[name].vmin_mv >= 4750, name
    for name in ("ATtiny102", "ATtiny104"):
        assert tpi[name].vmin_mv <= 1800 and tpi[name].opts["flags"] & 8, name
    assert tpi["ATtiny20"].opts["flags"] & 7 == 2 and tpi["ATtiny40"].opts["flags"] & 7 == 4


def test_updi_flash_bases():
    by = {d.name: d for d in prog.families()["updi"].devices}
    assert by["ATtiny1616"].opts["flags"] == 0x8000
    assert by["ATmega4809"].opts["flags"] == 0x4000      # megaAVR 0: the flash is at 0x4000
    assert by["AVR128DA48"].opts["flags"] == 0x800000
    assert by["ATtiny1616"].id == T1616 and by["ATtiny1616"].mem["flash"].size == 16384


def test_id_matching_with_wildcards():
    assert prog.id_matches("1e9421", "1e9421")
    assert prog.id_matches("8014", "80140102")       # a longer ID from the pod: a prefix match
    assert prog.id_matches("30??0a", "3001 0a".replace(" ", ""))
    assert not prog.id_matches("30??0a", "300100")
    assert not prog.id_matches("1e9421", "1e94")
    assert not prog.id_matches("", "1e")


def test_find_device_by_id_name_and_alias():
    dev = prog.find_device("updi", chip_id=T1616)
    assert dev.name == "ATtiny1616" and dev.expect_id == T1616
    assert prog.find_device("updi", name="attiny-1616").name == "ATtiny1616"
    assert prog.find_device("updi", name="ATtiny1616", chip_id=T1616).name == "ATtiny1616"
    xm = prog.find_device("pdi", name="ATxmega16A4U")      # ATxmega16A4 shares its ID
    assert xm.id == "1e9441" and prog.find_device("pdi", name="ATxmega16A4") is xm
    assert prog.find_device("pdi", chip_id="1e9441") is xm
    with pytest.raises(prog.ProgDeviceError, match="not in the AVR UPDI"):
        prog.find_device("updi", chip_id="1effff")
    with pytest.raises(prog.ProgDeviceError, match="device 'ATtiny9999' is not in the AVR UPDI"):
        prog.find_device("updi", name="ATtiny9999")
    with pytest.raises(prog.ProgDeviceError, match="not a ATtiny1616"):
        prog.find_device("updi", name="ATtiny1616", chip_id="1e9220")
    with pytest.raises(prog.ProgDeviceError, match="no chip ID"):
        prog.find_device("updi")


def test_target_aliases():
    for name, proto in (("avr-updi", "updi"), ("updi", "updi"), ("pic", "pic"), ("efm8", "c2"),
                        ("c2", "c2"), ("attiny-tpi", "tpi"), ("TPI", "tpi"), ("xmega", "pdi"),
                        ("pdi", "pdi")):
        assert prog.protocol_for(name) == proto and prog.is_prog_target(name)
    assert not prog.is_prog_target("esp32") and not prog.is_prog_target("target/stm32f4x.cfg")
    with pytest.raises(KeyError):
        prog.protocol_for("esp32")


# -- image files ---------------------------------------------------------------------------------

def test_ihex_parse_joins_runs_and_extended_addresses():
    segs = prog.parse_ihex(ihex((0, b"\x01\x02\x03"), (3, b"\x04"), (0x810000, b"\xaa\xbb")))
    assert segs == [(0, b"\x01\x02\x03\x04"), (0x810000, b"\xaa\xbb")]


def test_ihex_errors():
    good = ihex((0, b"\x01\x02"))
    with pytest.raises(ValueError, match="checksum"):
        prog.parse_ihex(good.replace(":02000000", ":02000000", 1).replace("0102", "0103", 1))
    with pytest.raises(ValueError, match="not an Intel HEX"):
        prog.parse_ihex("hello\n")
    with pytest.raises(ValueError, match="record length"):
        prog.parse_ihex(":0100000000\n")
    with pytest.raises(ValueError, match="not hex"):
        prog.parse_ihex(":zz\n")


def test_elf_parse_uses_load_addresses():
    segs = prog.parse_elf(elf32((0, b"\x10\x20"), (0x810000, b"\x30")))
    assert segs == [(0, b"\x10\x20"), (0x810000, b"\x30")]
    with pytest.raises(ValueError, match="not an ELF"):
        prog.parse_elf(b"nope")
    big = bytearray(elf32((0, b"\x01")))
    big[4] = 2      # ELFCLASS64
    with pytest.raises(ValueError, match="32-bit"):
        prog.parse_elf(bytes(big))
    with pytest.raises(ValueError, match="no loadable"):
        prog.parse_elf(elf32())


def test_load_image_by_content(tmp_path):
    assert prog.load_image(write(tmp_path, "a.hex", ihex((4, b"\x01")))) == [(4, b"\x01")]
    assert prog.load_image(write(tmp_path, "a.elf", elf32((8, b"\x02")))) == [(8, b"\x02")]
    assert prog.load_image(write(tmp_path, "a.bin", b"\x03\x04"), load_address=0x20) == [(0x20, b"\x03\x04")]
    with pytest.raises(ValueError, match="no image bytes"):
        prog.load_image(write(tmp_path, "e.bin", b""))


# -- cutting an image into memories --------------------------------------------------------------

def test_split_updi_flash_eeprom_fuses_user_and_ignored_lock():
    dev = prog.find_device("updi", chip_id=T1616)
    fam = prog.families()["updi"]
    segs = prog.parse_ihex(ihex(
        (0, bytes(range(100))), (0x810000, b"\x11\x22"), (0x820000, b"\x00\x01\x02"),
        (0x830000, b"\xff"), (0x850000, b"\x55")))
    regions, notes = prog.split_regions(segs, dev, ignore=fam.ignore)
    assert [(r.mem, r.addr, len(r.data)) for r in regions] == [
        ("flash", 0, 128), ("eeprom", 0, 2), ("user", 0, 1), ("config", 0, 3)]
    assert regions[0].data[:100] == bytes(range(100)) and regions[0].data[100:] == b"\xff" * 28
    assert regions[0].page == 64 and regions[1].page == 32
    assert any("lock bits" in n for n in notes)


def test_split_aligns_pages_and_joins_neighbours():
    dev = prog.find_device("updi", chip_id=T1616)
    segs = [(10, b"\x01" * 10), (70, b"\x02" * 4), (130, b"\x03")]
    regions, _ = prog.split_regions(segs, dev)
    assert [(r.addr, len(r.data)) for r in regions] == [(0, 192)]     # pages 0, 1 and 2 touch
    segs = [(0, b"\x01"), (1024, b"\x02")]
    regions, _ = prog.split_regions(segs, dev)
    assert [(r.addr, len(r.data)) for r in regions] == [(0, 64), (1024, 64)]  # a gap stays a gap


def test_split_refuses_data_outside_the_chip():
    dev = prog.find_device("updi", chip_id=T1616)
    with pytest.raises(prog.ProgDeviceError, match="0x4000 that is not in any memory.*flash 0x0"):
        prog.split_regions([(0x4000, b"\x01")], dev)
    with pytest.raises(prog.ProgDeviceError, match="not in any memory"):
        prog.split_regions([(0x860000, b"\x01")], dev)
    with pytest.raises(prog.ProgDeviceError, match="no bytes"):
        prog.split_regions([(0x830000, b"\x01")], dev, ignore=prog.families()["updi"].ignore)


def test_split_flash_at_the_end_of_the_chip():
    dev = prog.find_device("updi", chip_id=T1616)
    regions, _ = prog.split_regions([(16383, b"\x07")], dev)
    assert (regions[0].addr, len(regions[0].data)) == (16384 - 64, 64)


def test_split_tpi_config_is_one_word():
    dev = prog.find_device("tpi", name="ATtiny10")
    regions, _ = prog.split_regions([(0, b"\x01\x02"), (0x820000, b"\xf7")], dev,
                                    ignore=prog.families()["tpi"].ignore)
    cfg = [r for r in regions if r.mem == "config"][0]
    assert cfg.addr == 0 and cfg.data == b"\xf7"           # the pod pads it to the chip's word


def test_split_xmega_regions():
    dev = prog.find_device("pdi", name="ATxmega32A4U")
    regions, _ = prog.split_regions(
        [(0x0, b"\x01" * 300), (0x810000, b"\x02" * 40), (0x820000, b"\xff\xff\xff\xff\xff\xf7"),
         (0x850000, b"\x03" * 10)], dev, ignore=prog.families()["pdi"].ignore)
    assert [(r.mem, r.addr, len(r.data), r.page) for r in regions] == [
        ("flash", 0, 512, 256), ("eeprom", 0, 40, 32), ("user", 0, 10, 256), ("config", 0, 6, 1)]


# -- the flow through BenchPod.flash -------------------------------------------------------------

def app_hex(tmp_path, size=100, extra=()):
    return write(tmp_path, "app.hex", ihex((0, bytes(i & 0xFF for i in range(size))), *extra))


def test_flash_updi_full_flow(tmp_path):
    pod = FakePod()
    bp = make_pod(pod)
    res = bp.flash(target="avr-updi", file=app_hex(tmp_path, extra=((0x810000, b"\x01\x02"),)))
    assert res.ok and res.returncode == 0
    assert "ATtiny1616" in res.stdout and "flash at 0x0: 128 bytes" in res.stdout
    assert "eeprom at 0x0: 2 bytes" in res.stdout and "the chip runs its program" in res.stdout
    info = pod.infos()[0]
    assert info == {"cmd": "prog_flash", "op": "info", "proto": "updi", "data": 12}
    s1, s2 = pod.starts()
    assert {k: s1[k] for k in ("proto", "data", "mem", "addr", "len", "page", "erase", "expect_id",
                               "flags", "run")} == {
        "proto": "updi", "data": 12, "mem": "flash", "addr": 0, "len": 128, "page": 64,
        "erase": "chip", "expect_id": T1616, "flags": 0x8000, "run": False}
    assert (s2["mem"], s2["erase"], s2["run"], s2["len"], s2["page"]) == ("eeprom", "none", True, 2, 32)
    assert "clk" not in s1
    assert len(pod.staged) == 2 and len(pod.staged[0]) == 128 and pod.staged[1] == b"\x01\x02"


def test_flash_aliases_and_protocol_names(tmp_path):
    for target, chip, proto, device in (("updi", T1616, "updi", ""), ("tpi", "1e900c", "tpi", "ATtiny102"),
                                        ("pdi", "1e9441", "pdi", "ATxmega16A4U")):
        pod = FakePod(chip_id=chip)
        make_pod(pod).flash(target=target, file=app_hex(tmp_path, 10), device=device)
        assert pod.starts()[0]["proto"] == proto
        assert pod.starts()[0]["data"] == 12


def test_flash_pins_from_arguments_beat_profile_signals(tmp_path):
    pod = FakePod(chip_id="1e9441")
    bp = make_pod(pod, signals=[{"name": "PDI_CLK", "la": 8}, {"name": "PDI_DATA", "la": 9}])
    bp.flash(target="xmega", file=app_hex(tmp_path, 10))
    assert (pod.starts()[0]["clk"], pod.starts()[0]["data"]) == (8, 9)
    bp.flash(target="xmega", file=app_hex(tmp_path, 10), clk=7, data="PDI_DATA")
    assert (pod.starts()[1]["clk"], pod.starts()[1]["data"]) == (7, 9)


def test_flash_updi_pin_from_signal_and_clk_is_refused(tmp_path):
    pod = FakePod()
    bp = make_pod(pod, signals=[{"name": "UPDI", "la": 3}])
    bp.flash(target="avr-updi", file=app_hex(tmp_path, 10))
    assert pod.starts()[0]["data"] == 3
    with pytest.raises(ValueError, match="one pin"):
        bp.flash(target="avr-updi", file=app_hex(tmp_path, 10), clk=4)


def test_flash_missing_pins_say_what_to_set(tmp_path):
    bp = BenchPod(transport=FakeTransport(FakePod()), lease=False, wiring=Wiring())
    with pytest.raises(ValueError, match="data was not given.*swd_swdio"):
        bp.flash(target="avr-updi", file=app_hex(tmp_path, 10))
    bp = BenchPod(transport=FakeTransport(FakePod()), lease=False, wiring=Wiring(swd_swdio=2))
    with pytest.raises(ValueError, match="clk was not given.*ICSPCLK.*swd_swclk"):
        bp.flash(target="pic", file=app_hex(tmp_path, 10))
    with pytest.raises(ValueError, match="different LA pins"):
        make_pod(FakePod()).flash(target="xmega", file=app_hex(tmp_path, 10), clk=3, data=3)


def test_flash_pic_and_tpi_reset_pin(tmp_path):
    pod = FakePod(chip_id="1e900c")
    bp = make_pod(pod)
    bp.flash(target="attiny-tpi", file=app_hex(tmp_path, 10))
    assert pod.infos()[0]["reset"] == "nrst"                      # the pod's reset pin by default
    bp.flash(target="attiny-tpi", file=app_hex(tmp_path, 10), reset_pin=9)
    assert pod.infos()[1]["reset"] == 9 and pod.starts()[1]["reset"] == 9
    bp = make_pod(pod, signals=[{"name": "MCLR", "la": 8}])
    bp.flash(target="attiny-tpi", file=app_hex(tmp_path, 10))
    assert pod.starts()[2]["reset"] == 8
    bp.flash(target="attiny-tpi", file=app_hex(tmp_path, 10), nreset=True)   # the pod's pin wins
    assert pod.starts()[3]["reset"] == "nrst"
    with pytest.raises(ValueError, match="reset_pin must be another"):
        bp.flash(target="attiny-tpi", file=app_hex(tmp_path, 10), reset_pin=11)
    with pytest.raises(ValueError, match="needs the pod's reset pin"):
        make_pod(pod).flash(target="attiny-tpi", file=app_hex(tmp_path, 10), nreset=False)


def test_flash_page_erase_and_none_apply_to_every_region(tmp_path):
    for erase in ("page", "none"):
        pod = FakePod()
        make_pod(pod).flash(target="avr-updi", file=app_hex(tmp_path, extra=((0x810000, b"\x01"),)),
                            erase=erase)
        assert [s["erase"] for s in pod.starts()] == [erase, erase]


def test_flash_chip_erase_only_when_the_image_has_flash(tmp_path):
    pod = FakePod()
    f = write(tmp_path, "ee.hex", ihex((0x810000, b"\x01\x02")))
    res = make_pod(pod).flash(target="avr-updi", file=f)
    assert pod.starts()[0]["erase"] == "page" and "no chip erase" in res.stdout


def test_flash_reset_false_holds_the_chip(tmp_path):
    pod = FakePod()
    res = make_pod(pod).flash(target="avr-updi", file=app_hex(tmp_path), reset=False)
    assert pod.starts()[-1]["run"] is False and "held in reset" in res.stdout


def test_flash_elf_and_bin(tmp_path):
    pod = FakePod()
    bp = make_pod(pod)
    bp.flash(target="avr-updi", file=write(tmp_path, "a.elf", elf32((0, b"\x01" * 70), (0x820000, b"\x02"))))
    assert [(s["mem"], s["len"]) for s in pod.starts()] == [("flash", 128), ("config", 1)]
    pod2 = FakePod()
    make_pod(pod2).flash(target="avr-updi", file=write(tmp_path, "a.bin", b"\x05" * 10), load_address="0x40")
    s = pod2.starts()[0]
    assert (s["addr"], s["len"]) == (64, 64) and pod2.staged[0] == b"\x05" * 10 + b"\xff" * 54


def test_flash_dry_run_only_reads_the_chip(tmp_path):
    pod = FakePod()
    res = make_pod(pod).flash(target="avr-updi", dry_run=True)
    assert res.ok and "ATtiny1616" in res.stdout and "nothing was written" in res.stdout
    assert pod.starts() == [] and pod.staged == [] and len(pod.infos()) == 1
    # with a file too: the file is still not written
    res = make_pod(pod).flash(target="avr-updi", file=app_hex(tmp_path), dry_run=True)
    assert res.ok and pod.starts() == []


def test_flash_power_on_first(tmp_path):
    pod = FakePod()
    bp = make_pod(pod)
    bp.flash(target="avr-updi", file=app_hex(tmp_path), target_power=1)
    assert bp._transport.powered == [(1, True)]


# -- refusals before anything is written ---------------------------------------------------------

def test_flash_requires_a_file_unless_dry_run():
    with pytest.raises(ValueError, match="needs file"):
        make_pod(FakePod()).flash(target="avr-updi")


def test_flash_bad_erase_and_build_are_refused(tmp_path):
    bp = make_pod(FakePod())
    with pytest.raises(ValueError, match="erase must be"):
        bp.flash(target="avr-updi", file=app_hex(tmp_path), erase="all")
    with pytest.raises(ValueError, match="not an uploaded build"):
        bp.flash(target="avr-updi", build="latest")


def test_flash_needs_the_feature():
    pod = FakePod()
    bp = BenchPod(transport=FakeTransport(pod, features_rev=16), lease=False,
                  wiring={"swd_swclk": 11, "swd_swdio": 12})
    with pytest.raises(UnsupportedFeatureError, match="prog_flash.*install-blobs") as exc:
        bp.flash(target="avr-updi", dry_run=True)
    assert exc.value.feature == "prog_flash" and pod.starts() == [] and pod.infos() == []
    assert not bp.supports("prog_flash")
    assert make_pod(FakePod()).supports("prog_flash")


def test_flash_needs_a_stream_connection(tmp_path):
    class Serial(FakeTransport):
        stage_psram = None  # type: ignore[assignment]

    bp = BenchPod(transport=Serial(FakePod()), lease=False, wiring={"swd_swclk": 11, "swd_swdio": 12})
    with pytest.raises(FlashError, match="network, USB or cloud"):
        bp.flash(target="avr-updi", file=app_hex(tmp_path))


def test_flash_unknown_chip_wrong_device_and_ambiguous(tmp_path):
    f = app_hex(tmp_path)
    with pytest.raises(prog.ProgDeviceError, match="1effff.*not in the AVR UPDI"):
        make_pod(FakePod(chip_id="1effff")).flash(target="avr-updi", file=f)
    with pytest.raises(prog.ProgDeviceError, match="not a ATtiny1614"):
        make_pod(FakePod()).flash(target="avr-updi", file=f, device="ATtiny1614")
    with pytest.raises(prog.ProgDeviceError, match="not in the AVR UPDI"):
        make_pod(FakePod()).flash(target="avr-updi", file=f, device="ATtiny0")
    pod = FakePod()
    with pytest.raises(FlashError):
        make_pod(pod).flash(target="avr-updi", file=f, device="ATtiny0")
    assert pod.starts() == []


def test_flash_image_that_does_not_fit_is_refused_before_writing(tmp_path):
    pod = FakePod()
    f = write(tmp_path, "big.hex", ihex((0x7000, b"\x01" * 16)))     # an ATtiny1616 has 16 KB
    with pytest.raises(prog.ProgDeviceError, match="not in any memory"):
        make_pod(pod).flash(target="avr-updi", file=f)
    assert pod.starts() == [] and pod.staged == []


def test_flash_five_volt_chips_fail_at_3v3(tmp_path):
    pod = FakePod(chip_id="1e9003")      # ATtiny10
    pod.mv = 3300
    with pytest.raises(prog.ProgDeviceError, match="ATtiny10.*4.75|only be programmed at 4.75 V") as exc:
        make_pod(pod).flash(target="attiny-tpi", file=app_hex(tmp_path, 10))
    assert exc.value.result == "voltage" and "level shifter" in str(exc.value)
    assert pod.starts() == []
    pod = FakePod(chip_id="1e900c")      # ATtiny102: the datasheet is unclear, so it is allowed
    pod.mv = 3300
    assert make_pod(pod).flash(target="attiny-tpi", file=app_hex(tmp_path, 10)).ok
    pod = FakePod(chip_id="1e9003")
    pod.mv = 1800
    with pytest.raises(prog.ProgDeviceError, match="5 V"):
        make_pod(pod).flash(target="attiny-tpi", file=app_hex(tmp_path, 10), dry_run=True)


def test_flash_voltage_unknown_is_not_checked(tmp_path):
    pod = FakePod(chip_id="1e9003")      # no la_voltage answer: the pod decides
    assert make_pod(pod).flash(target="attiny-tpi", file=app_hex(tmp_path, 10)).ok


def test_check_voltage_limits():
    xm = prog.find_device("pdi", name="ATxmega32A4U")
    prog.check_voltage(xm, 3.3)
    with pytest.raises(prog.ProgDeviceError, match="at most 3.6 V"):
        prog.check_voltage(xm, 5.0)
    dev = prog.ProgDevice(name="X", family="x", proto="pic", id="01", mem={}, vmin_mv=1800,
                          vmax_mv=5500, erase_vmin_mv=2700)
    with pytest.raises(prog.ProgDeviceError, match="chip erase needs the supply above 2.7 V"):
        prog.check_voltage(dev, 1.8, erase="chip")
    prog.check_voltage(dev, 1.8, erase="page")
    prog.check_voltage(dev, 3.3, erase="chip")
    low = prog.ProgDevice(name="Y", family="y", proto="pic", id="01", mem={}, vmin_mv=2000, vmax_mv=5500)
    with pytest.raises(prog.ProgDeviceError, match="at least 2 V"):
        prog.check_voltage(low, 1.8)


# -- the pod's results ---------------------------------------------------------------------------

@pytest.mark.parametrize("result, exc, text", [
    ("no answer (wiring, power or I/O voltage)", prog.ProgNoAnswerError, "Check the wiring"),
    ("wrong chip (the ID is not expect_id)", prog.ProgFlashError, "pass device="),
    ("locked (erase the chip to unlock it)", prog.ProgLockedError, 'erase="chip"'),
    ("verify failed", prog.ProgVerifyError, "at 0x40"),
    ("timeout (the chip stayed busy)", prog.ProgFlashError, "minimum VDD"),
    ("protocol error (an answer the protocol does not allow)", prog.ProgFlashError, "keep the lines short"),
    ("memory, address, length or page size not valid here", prog.ProgFlashError, "pass device="),
    ("could not read the staged image", prog.ProgFlashError, "try again"),
    ("not supported (this chip, memory or protocol)", prog.ProgFlashError, "update the pod"),
    ("aborted", prog.ProgFlashError, "aborted"),
    ("something new the pod says", prog.ProgFlashError, "something new"),
])
def test_flash_maps_every_result(tmp_path, result, exc, text):
    pod = FakePod(result=result, at_addr=0x40)
    with pytest.raises(exc, match=text) as ei:
        make_pod(pod).flash(target="avr-updi", file=app_hex(tmp_path))
    e = ei.value
    assert isinstance(e, FlashError) and e.result == result and e.mem == "flash" and e.addr == 0
    assert e.phase == "done"
    if result.startswith("verify"):
        assert e.at_addr == 0x40
    if result.startswith("no answer"):
        assert isinstance(e, TargetUnreachableError)
        assert "data on LA12" in str(e)


def test_flash_failure_stops_at_the_first_region(tmp_path):
    pod = FakePod(result="verify failed")
    with pytest.raises(prog.ProgVerifyError):
        make_pod(pod).flash(target="avr-updi", file=app_hex(tmp_path, extra=((0x810000, b"\x01"),)))
    assert len(pod.starts()) == 1


def test_flash_check_false_returns_the_failure(tmp_path):
    pod = FakePod(result="no answer (wiring, power or I/O voltage)")
    res = make_pod(pod).flash(target="avr-updi", file=app_hex(tmp_path), check=False)
    assert not res.ok and res.returncode == 1 and res.target_unreachable
    assert "did not answer" in res.stderr
    pod = FakePod(result="locked (erase the chip to unlock it)")
    res = make_pod(pod).flash(target="avr-updi", file=app_hex(tmp_path), check=False)
    assert not res.ok and not res.target_unreachable and "erase" in res.stderr


def test_flash_info_failure_is_reported_before_staging(tmp_path):
    pod = FakePod(info_result="no answer (wiring, power or I/O voltage)")
    with pytest.raises(prog.ProgNoAnswerError):
        make_pod(pod).flash(target="avr-updi", file=app_hex(tmp_path))
    assert pod.staged == [] and pod.starts() == []


def test_flash_refusals_from_the_pod_pass_through(tmp_path):
    pod = FakePod()
    pod.fail_command = PodBusyError("pod busy")
    with pytest.raises(PodBusyError):
        make_pod(pod).flash(target="avr-updi", file=app_hex(tmp_path))


def test_run_times_out_and_aborts(monkeypatch):
    class Stuck(FakePod):
        def command(self, req):
            r = super().command(req)
            if req.get("op") == "status" and self._job["op"] == "start":
                r["running"] = True
                r["result"] = None
                r["phase"] = "erase"
            return r

    pod = Stuck()
    now = [0.0]

    def clock():
        now[0] += 30.0
        return now[0]

    dev = prog.find_device("updi", chip_id=T1616)
    reg = prog.Region(mem="flash", addr=0, data=b"\x00" * 64, page=64)
    with pytest.raises(prog.ProgFlashError, match="did not finish in time.*erase") as ei:
        prog.run(pod.command, pod.stage, "updi", dev, [reg], pins={"data": 3}, clock=clock,
                 sleep=lambda s: None)
    assert ei.value.result == "timeout" and pod.cmds[-1]["op"] == "abort"


def test_read_info_times_out(monkeypatch):
    class Stuck(FakePod):
        def command(self, req):
            r = super().command(req)
            if req.get("op") == "status":
                r["running"] = True
            return r

    pod = Stuck()
    now = [0.0]

    def clock():
        now[0] += 6.0
        return now[0]

    with pytest.raises(prog.ProgNoAnswerError, match="Check the wiring"):
        prog.read_info(pod.command, "updi", {"data": 3}, clock=clock, sleep=lambda s: None,
                       pin_text="data on LA3")
    assert pod.cmds[-1]["op"] == "abort"


def test_run_progress_and_erase_validation():
    pod = FakePod(polls=3)
    dev = prog.find_device("updi", chip_id=T1616)
    reg = prog.Region(mem="flash", addr=0, data=b"\x00" * 128, page=64)
    seen = []
    r = prog.run(pod.command, pod.stage, "updi", dev, [reg], pins={"data": 3},
                 progress=lambda d, t: seen.append((d, t)), sleep=lambda s: None)
    assert r.ok and seen[-1] == (2, 2) and seen[0][0] == 0
    with pytest.raises(ValueError, match="erase must be"):
        prog.run(pod.command, pod.stage, "updi", dev, [reg], pins={"data": 3}, erase="x")
    with pytest.raises(FlashError, match="network, USB or cloud"):
        prog.run(pod.command, None, "updi", dev, [reg], pins={"data": 3})


def test_start_request_options():
    dev = prog.find_device("updi", chip_id=T1616)
    reg = prog.Region(mem="config", addr=0, data=b"\x01", page=1)
    req = prog.start_request("updi", {"data": 3}, region=reg, dev=dev, erase="none", run_after=True)
    assert req == {"cmd": "prog_flash", "op": "start", "proto": "updi", "data": 3, "mem": "config",
                   "addr": 0, "len": 1, "page": 1, "erase": "none", "run": True,
                   "expect_id": T1616, "flags": 0x8000}


def test_table_json_is_valid_and_documented():
    data = prog.load_table()
    assert data["schema"] == 1
    json.dumps(data)
    import pathlib
    readme = pathlib.Path(prog.__file__).with_name("prog_devices.README.md")
    assert readme.exists() and "prog_devices.json" in readme.read_text()


# -- PIC and C2 ------------------------------------------------------------------------------

def test_pic_ids_and_flags():
    pic = prog.families()["pic"]
    d = prog.find_device("pic", chip_id="a4000102")        # PIC16F1847: DEV<8:0> of the packed word, then REV
    assert d.name == "PIC16F1847" and d.expect_id == "a400"
    assert d.opts["flags"] & 4 and (d.opts["flags"] >> 8) & 15 == 7 and d.opts["cp_addr"] == 0x1000E
    f18 = prog.find_device("pic", chip_id="d430")          # PIC16F18446: separate 8-bit set
    assert f18.name == "PIC16F18446" and f18.opts["flags"] == 1 and f18.opts["cp_addr"] == 0x800B * 2
    q = prog.find_device("pic", name="PIC18F27Q43")
    assert q.id == "8074" and q.opts["flags"] == 3 and q.opts["cp_addr"] == 0x300009
    assert q.mem["eeprom"].addr == 0x380000 == q.mem["eeprom"].hex
    q10 = prog.find_device("pic", name="PIC18F26Q10")
    assert q10.opts["flags"] == 3 | 16 and q10.erase_vmin_mv == 1900
    six = prog.find_device("pic", name="PIC16F18313")
    assert six.opts["flags"] & 1 == 0 and six.opts["flags"] & 32       # the 6-bit set, separate REVID
    assert prog.find_device("pic", name="PIC16LF1847").id != d.id      # LF parts have their own IDs
    assert len({x.id for x in pic.devices}) == len(pic.devices)


def test_pic_split_config_user_and_chip_erase_only(tmp_path):
    dev = prog.find_device("pic", name="PIC16F1847")
    regions, _ = prog.split_regions(
        [(0, b"\x01\x02" * 40), (0x1000E, b"\xff\x3f\xff\x3f"), (0x10000, b"\x01\x00")], dev)
    assert [(r.mem, r.addr, len(r.data)) for r in regions] == [
        ("flash", 0, 128), ("user", 0x10000, 2), ("config", 0x1000E, 4)]
    low = prog.find_device("pic", name="PIC16F1827")
    assert low.chip_erase_only
    with pytest.raises(prog.ProgDeviceError, match='erase="chip"'):
        prog.check_erase(low, "page")
    prog.check_erase(low, "chip")
    prog.check_erase(dev, "page")
    pod = FakePod(chip_id="3d01")
    pod.mv = 3300
    f = write(tmp_path, "p.hex", ihex((0, b"\x01" * 4)))
    with pytest.raises(prog.ProgDeviceError, match="wipe their neighbours"):
        make_pod(pod).flash(target="pic", file=f, device="PIC16F1827", erase="page", clk=3, data=4)


def test_pic_flash_flow_sends_flags_and_cp_address(tmp_path):
    pod = FakePod(chip_id="a400")
    pod.mv = 3300
    f = write(tmp_path, "p.hex", ihex((0, b"\x01\x02" * 8), (0x1000E, b"\xff\x3f\xff\x3f")))
    assert make_pod(pod).flash(target="pic", file=f, clk=3, data=4).ok
    s0, s1 = pod.starts()
    assert s0["flags"] == 4 | (7 << 8) and s0["cp_addr"] == 0x1000E and "baud" not in s0 and s0["reset"] == "nrst"
    assert (s0["mem"], s0["page"], s0["erase"]) == ("flash", 64, "chip")
    assert (s1["mem"], s1["addr"], s1["len"], s1["erase"]) == ("config", 0x1000E, 4, "none")


def test_pic_chip_erase_below_brownout_is_refused(tmp_path):
    pod = FakePod(chip_id="8071")      # PIC18F26Q10: programs at 1.8 V, its bulk erase needs 1.9 V
    pod.mv = 1800
    f = write(tmp_path, "p.hex", ihex((0, b"\x01\x02")))
    with pytest.raises(prog.ProgDeviceError, match="chip erase needs the supply above 1.9 V"):
        make_pod(pod).flash(target="pic", file=f, clk=3, data=4)
    assert make_pod(pod).flash(target="pic", file=f, clk=3, data=4, erase="none").ok


def test_c2_family_never_writes_the_lock_byte(tmp_path):
    bb1 = prog.find_device("c2", name="EFM8BB10F8G")
    assert bb1.mem["flash"].size == 0x1FFF
    with pytest.raises(prog.ProgDeviceError, match="not in any memory"):
        prog.split_regions([(0x1FFF, b"\x00")], bb1)
    with pytest.raises(prog.ProgDeviceError, match="differ: pass device="):
        prog.find_device("c2", chip_id="3001")             # BB1 F8G, F4G and F2G share ID 0x30
    regions, _ = prog.split_regions([(0, b"\x01" * 600)], bb1)
    assert [(r.addr, len(r.data), r.page) for r in regions] == [(0, 600, 512)]
    pod = FakePod(chip_id="300201")
    f = write(tmp_path, "c.hex", ihex((0, b"\x01" * 600)))
    assert make_pod(pod).flash(target="efm8", file=f, clk=3, data=4, device="EFM8BB10F8G").ok
    st = pod.starts()[0]
    assert st["proto"] == "c2" and st["expect_id"] == "30" and "flags" not in st and "reset" not in st


def test_direction_conflict_becomes_a_message(tmp_path):
    from embeddedci.benchpod.errors import DirectionConflictError

    for when in ("info", "start"):
        pod = FakePod()
        orig = pod.command

        def command(req, orig=orig, when=when, pod=pod):
            if req.get("cmd") == "prog_flash" and req.get("op") == when:
                raise DirectionConflictError("la_dut_output", cmd="prog_flash", la=12)
            return orig(req)

        pod.command = command
        with pytest.raises(prog.ProgFlashError, match="LA12.*bidirectional.*fix the wiring") as ei:
            make_pod(pod).flash(target="avr-updi", file=app_hex(tmp_path))
        assert isinstance(ei.value.__cause__, DirectionConflictError)
