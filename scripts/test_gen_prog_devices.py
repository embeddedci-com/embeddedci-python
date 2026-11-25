"""Tests of scripts/gen_prog_devices.py: the source schema, the generated tables, the stale check.

Run: python -m pytest scripts/test_gen_prog_devices.py
"""
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import gen_prog_devices as g  # noqa: E402


@pytest.fixture()
def src(tmp_path):
    d = tmp_path / "prog-devices"
    shutil.copytree(g.SRC, d)
    return d


def chip(src, proto, family, name):
    return src / proto / family / (name + ".yaml")


def test_the_repository_tables_are_up_to_date():
    assert g.main(["--check"]) == 0


def test_every_chip_loads_and_names_are_unique():
    fams = g.load_source()
    assert {p: len(f["devices"]) for p, f in fams.items()} == {"updi": 79, "pic": 149, "c2": 14, "tpi": 8, "pdi": 38, "sbw": 1}


def test_natural_order_and_stable_output():
    fams = g.load_source()
    names = [d["name"] for d in fams["updi"]["devices"] if d["family"] == "AVR DA"]
    assert names.index("AVR32DA28") < names.index("AVR64DA28") < names.index("AVR128DA28")
    assert g.render_sdk(fams) == g.render_sdk(g.load_source())
    json.loads(g.render_sdk(fams))
    json.loads(g.render_web(fams))


def test_named_fields_and_legacy_flags_of_known_chips():
    fams = g.load_source()
    by = {d["name"]: d for p in fams.values() for d in p["devices"]}
    assert by["PIC16F18446"]["prog"] == {"cmd_set": "8bit", "cp_addr": 0x10016, "cp_bit": 0}
    assert by["PIC16F18446"]["legacy"] == {"flags": 1, "cp_addr": 0x10016}
    assert by["PIC18F27Q43"]["legacy"]["flags"] == 3
    assert by["PIC16F1847"]["legacy"]["flags"] == 4 | (7 << 8)
    assert by["PIC18F26Q10"]["prog"]["q10"] is True and by["PIC18F26Q10"]["legacy"]["flags"] == 19
    assert by["AVR128DA48"]["prog"] == {"flash_base": 0x800000}
    assert by["ATmega4809"]["legacy"] == {"flags": 0x4000}
    assert by["ATtiny20"]["prog"] == {"write_words": 2} and by["ATtiny20"]["legacy"] == {"flags": 2}
    assert by["ATtiny102"]["legacy"] == {"flags": 8}
    assert "legacy" not in by["ATtiny4"] and "prog" not in by["ATtiny4"]
    assert "prog" not in by["ATxmega16A4U"]


def test_pic_entry_styles():
    styles = g.load_source()["pic"]["entry_styles"]
    assert styles[0] == {"prog": {"cmd_set": "6bit"}, "flags": 0}
    assert [s["flags"] for s in styles] == [0, 1, 3, 19, 32]


def edit(path, old, new):
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


@pytest.mark.parametrize("proto,family,name,old,new,msg", [
    ("pic", "pic16f184xx", "pic16f18446", "cmd_set: 8bit", "cmd_set: 7bit", "prog.cmd_set"),
    ("pic", "pic16f184xx", "pic16f18446", "cmd_set: 8bit", "cmd_set: 8bit\n  speed: 3", "unknown key speed"),
    ("pic", "pic16f184xx", "pic16f18446", "bit: 0", "bit: 16", "prog.cp.bit"),
    ("pic", "pic18-q43", "pic18f27q43", "pc: byte", "pc: word\n  q10: true", "q10 needs"),
    ("pic", "pic16f184xx", "pic16f18446", "id: \"d430\"", "id: \"D430\"", "id must be"),
    ("pic", "pic16f184xx", "pic16f18446", "id: \"d430\"", "id: 1430", "id must be"),
    ("pic", "pic16f184xx", "pic16f18446", "size: 32768", "size: 0", "mem.flash.size"),
    ("pic", "pic16f184xx", "pic16f18446", "voltage: {", "voltagex: {", "unknown key voltagex"),
    ("pic", "pic16f184xx", "pic16f18446", "hex: 0x1000E", "hex: 0x0", "overlap"),
    ("tpi", "attiny-tpi", "attiny20", "write_words: 2", "write_words: 3", "1, 2 or 4"),
    ("updi", "tinyavr-0-1-2", "attiny1616", "flash_base: 0x8000", "flash_base: -1", "flash_base"),
    ("updi", "tinyavr-0-1-2", "attiny1616", "flash_base: 0x8000", "flash_base: true", "flash_base"),
])
def test_source_errors_name_the_file_and_the_field(src, proto, family, name, old, new, msg):
    path = chip(src, proto, family, name)
    if not path.exists():
        pytest.skip("fixture chip not present")
    edit(path, old, new)
    with pytest.raises(g.SourceError) as e:
        g.load_source(src)
    assert path.name in str(e.value)
    assert msg in str(e.value)


def test_a_pdi_chip_takes_no_prog_fields(src):
    path = next((src / "pdi").glob("*/*.yaml"))
    path.write_text(path.read_text().replace("mem:", "prog:\n  flash_base: 1\nmem:"))
    with pytest.raises(g.SourceError, match="unknown key flash_base"):
        g.load_source(src)


def test_duplicate_names_are_refused(src):
    a = next((src / "pdi").glob("*/*.yaml"))
    b = a.with_name("copy.yaml")
    b.write_text(a.read_text())
    with pytest.raises(g.SourceError, match="duplicate chip name"):
        g.load_source(src)


def test_check_detects_a_stale_table_and_write_fixes_it(tmp_path, src, monkeypatch):
    sdk = tmp_path / "prog_devices.json"
    sdk.write_text("{}")
    monkeypatch.setattr(g, "SDK_JSON", sdk)
    assert g.main(["--src", str(src), "--check"]) == 1
    assert sdk.read_text() == "{}"          # --check writes nothing
    assert g.main(["--src", str(src)]) == 0
    assert g.main(["--src", str(src), "--check"]) == 0
    edit(chip(src, "pic", "pic16f184xx", "pic16f18446"), "size: 32768", "size: 16384")
    assert g.main(["--src", str(src), "--check"]) == 1


def test_cli_and_web_copies(tmp_path, src, monkeypatch):
    monkeypatch.setattr(g, "SDK_JSON", tmp_path / "sdk.json")
    cli, web = tmp_path / "cli", tmp_path / "web" / "progIds.json"
    assert g.main(["--src", str(src), "--cli", str(cli), "--web", str(web)]) == 0
    assert (cli / "internal/progdev/prog_devices.json").read_bytes() == (tmp_path / "sdk.json").read_bytes()
    assert (cli / "internal/progdev/prog_devices.README.md").read_text() == g.SDK_README.read_text()
    ids = json.loads(web.read_text())
    assert ids["version"] == 2 and ["AVR32DA28", "1e9534"] in ids["updi"]
    assert ids["pic_entry_styles"][1] == {"prog": {"cmd_set": "8bit"}, "flags": 1}
    assert g.main(["--src", str(src), "--cli", str(cli), "--web", str(web), "--check"]) == 0
    (cli / "internal/progdev/prog_devices.json").write_text("x")
    assert g.main(["--src", str(src), "--cli", str(cli), "--web", str(web), "--check"]) == 1
