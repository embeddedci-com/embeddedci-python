"""Tests of scripts/prog_devices_packs.py on small made-up packs."""
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import prog_devices_packs as p  # noqa: E402
import gen_prog_devices as g  # noqa: E402

ATDF = """<avr-tools-device-file><devices>
<device name="{name}"><address-spaces>
<address-space id="prog"><memory-segment name="FLASH" size="{flash}" pagesize="0x40"/></address-space>
<address-space id="eeprom"><memory-segment name="EEPROM" size="0x100" pagesize="0x20"/></address-space>
</address-spaces><property-groups><property-group name="SIGNATURES">
<property name="SIGNATURE0" value="0x1E"/><property name="SIGNATURE1" value="0x94"/>
<property name="SIGNATURE2" value="{sig2}"/></property-group></property-groups></device></devices></avr-tools-device-file>"""
PIC = """<edc:PIC xmlns:edc="http://crownking/edc">
<edc:CodeSector edc:beginaddr="0x0" edc:endaddr="0x4000"/>
<edc:DeviceIDSector edc:beginaddr="0x8006" edc:endaddr="0x8007" edc:value="0x30D4"/>
<edc:ProgrammingRowSize edc:nzsize="32" edc:progop="pgm"/></edc:PIC>"""


def make_pack(tmp_path, sig2="0x21", flash="0x4000"):
    path = tmp_path / "x.atpack"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("atdf/ATtiny1616.atdf", ATDF.format(name="ATtiny1616", flash=flash, sig2=sig2))
        z.writestr("edc/ATtiny1616.PIC", PIC)        # an AVR pack's .PIC copy must not win
        z.writestr("edc/PIC16F18446.PIC", PIC)
    return p.Pack([path])


def test_avr_facts_and_the_atdf_wins_over_the_pic_copy(tmp_path):
    f = make_pack(tmp_path).read("ATtiny1616")
    assert f["kind"] == "avr" and f["id"] == "1e9421" and f["flash"] == (0x4000, 0x40) and f["eeprom"] == (0x100, 0x20)


def test_pic_facts(tmp_path):
    f = make_pack(tmp_path).read("PIC16F18446")
    assert f["devid"] == 0x30D4 and f["flash_units"] == 0x4000 and f["rows"] == {"pgm": 32}


def chip(name):
    return next(d for f in g.load_source().values() for d in f["devices"] if d["name"] == name)


def test_compare_finds_nothing_when_the_pack_agrees(tmp_path):
    pack = make_pack(tmp_path)
    assert p.compare(chip("ATtiny1616"), "updi", pack.read("ATtiny1616")) == []
    assert p.compare(chip("PIC16F18446"), "pic", pack.read("PIC16F18446")) == []


def test_compare_reports_id_and_size_differences(tmp_path):
    pack = make_pack(tmp_path, sig2="0x22", flash="0x8000")
    d = p.compare(chip("ATtiny1616"), "updi", pack.read("ATtiny1616"))
    assert any(x.startswith("id ") for x in d) and any(x.startswith("flash.size ") for x in d)


def test_known_differences_are_matched_by_chip_and_field():
    rules = p.known_rules()
    assert p.is_known(rules, "updi", chip("AVR128DA48"), "eeprom.page 32 vs 1")
    assert not p.is_known(rules, "updi", chip("ATtiny1616"), "eeprom.page 32 vs 1")
    assert not p.is_known(rules, "updi", chip("AVR128DA48"), "flash.size 1 vs 2")
