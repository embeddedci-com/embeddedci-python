#!/usr/bin/env python3
"""Cross-check prog-devices/ against Microchip's device packs, or start a new chip file from one.

The packs are Microchip's ``*.atpack`` files (zip): AVR parts carry an ATDF file (``atdf/<chip>.atdf``),
PIC parts a ``.PIC`` file (``edc/<chip>.PIC``). Download them from https://packs.download.microchip.com/
(``index.idx`` lists the names), for example ``Microchip.ATtiny_DFP.3.4.278.atpack``.

    python scripts/prog_devices_packs.py check  PACK.atpack...             # every chip a pack knows
    python scripts/prog_devices_packs.py new    PACK.atpack... --like ATtiny1616 --name ATtiny1614

``check`` compares, per chip of prog-devices/ found in a pack: the ID, the flash size and page, the
EEPROM, user row (AVR) / user ID (PIC) and fuse/config sizes. It prints every difference and exits 1
when there is one; a chip no pack names is listed as "not in these packs". ``new`` writes a chip file
from a template chip (``--like``: its protocol fields, voltage, notes) with the ID and the memory
sizes and pages the pack gives; check the result by hand (hex addresses, voltage, notes) and the
datasheet before committing it.

What a pack gives and what it does not (see prog-devices/README.md, "Where the numbers come from"):
ID, flash/EEPROM/user row/fuse sizes and pages: from the pack. Memory addresses in the pod's and the
image's address space, entry style, voltage limits, erase quirks, flash base and everything in
``prog:``: from the programming specifications and the bench, not from the packs.
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))
import gen_prog_devices as g  # noqa: E402


def _i(v: Optional[str]) -> int:
    return int(v, 0) if v else 0


def _norm(n: str) -> str:
    return re.sub(r"[^a-z0-9]", "", n.lower())


class Pack:
    """Chip name (normalized) -> a function that parses its device file on demand."""

    def __init__(self, paths: List[Path]) -> None:
        self.files: Dict[str, Tuple[zipfile.ZipFile, str, str]] = {}
        for p in paths:
            z = zipfile.ZipFile(p)
            for n in z.namelist():
                m = re.fullmatch(r"(atdf|edc)/([^/]+)\.(atdf|PIC)", n)
                if not m:
                    continue
                key = _norm(m.group(2))
                # AVR packs ship both an ATDF and an .edc copy: the ATDF is the one; PIC has .PIC only
                if m.group(1) == "edc" and (key in self.files or not m.group(2).upper().startswith("PIC")):
                    continue
                self.files[key] = (z, n, m.group(1))

    def has(self, name: str) -> bool:
        return _norm(name) in self.files

    def read(self, name: str) -> Dict[str, Any]:
        z, n, kind = self.files[_norm(name)]
        root = ET.fromstring(z.read(n))
        return atdf_facts(root) if kind == "atdf" else pic_facts(root)


def atdf_facts(root: ET.Element) -> Dict[str, Any]:
    f: Dict[str, Any] = {"kind": "avr"}
    sig = {p.get("name"): _i(p.get("value")) for p in root.iter("property") if (p.get("name") or "").startswith("SIGNATURE")}
    if len(sig) == 3:
        f["id"] = "%02x%02x%02x" % (sig["SIGNATURE0"], sig["SIGNATURE1"], sig["SIGNATURE2"])
    segs: Dict[str, Tuple[int, int]] = {}
    for s in root.iter("memory-segment"):
        segs.setdefault(s.get("name") or "", (_i(s.get("size")), _i(s.get("pagesize"))))
    if "FLASH" in segs:
        f["flash"] = segs["FLASH"]
    elif "APP_SECTION" in segs:
        f["flash"] = (segs["APP_SECTION"][0] + segs.get("BOOT_SECTION", (0, 0))[0], segs["APP_SECTION"][1])
    for ours, theirs in (("eeprom", "EEPROM"), ("user", "USER_SIGNATURES"), ("config", "FUSES")):
        if theirs in segs:
            f[ours] = segs[theirs]
    return f


def pic_facts(root: ET.Element) -> Dict[str, Any]:
    ns = {"edc": "http://crownking/edc"}
    f: Dict[str, Any] = {"kind": "pic"}

    def secs(tag: str) -> List[ET.Element]:
        return [e for e in root.iter() if e.tag.endswith("}" + tag)]

    def attr(e: ET.Element, k: str) -> str:
        for name, v in e.attrib.items():
            if name.endswith("}" + k):
                return v
        return ""

    ids = [attr(e, "value") for e in secs("DeviceIDSector") if attr(e, "value")]
    if ids:
        f["devid"] = _i(ids[0])
    span = lambda tag: sum(_i(attr(e, "endaddr")) - _i(attr(e, "beginaddr")) for e in secs(tag))  # noqa: E731
    f["flash_units"] = span("CodeSector")
    f["user_units"] = span("UserIDSector")
    f["config_units"] = span("ConfigFuseSector")
    f["eeprom_units"] = span("EEDataSector")
    f["rows"] = {attr(e, "progop"): _i(attr(e, "nzsize")) for e in secs("ProgrammingRowSize")}
    return f


def compare(dev: Dict[str, Any], proto: str, facts: Dict[str, Any]) -> List[str]:
    """Differences between a chip of the table and the pack's facts."""
    out: List[str] = []
    mem = dev["mem"]
    if facts["kind"] == "avr":
        if "id" in facts and not dev["id"].startswith(facts["id"][: len(dev["id"].replace("?", ""))]):
            out.append(f"id {dev['id']} (table) vs {facts['id']} (pack)")
        for m in ("flash", "eeprom", "user", "config"):
            if m in mem and m in facts:
                size, page = facts[m]
                # the table stops flash short on TPI? no: sizes are exact; pages of 1-byte memories vary
                if mem[m]["size"] != size:
                    out.append(f"{m}.size {mem[m]['size']} vs {size}")
                if page and mem[m]["page"] != page and m != "config":
                    out.append(f"{m}.page {mem[m]['page']} vs {page}")
            elif m in facts and m in ("flash",):
                out.append(f"{m} missing in the table")
        return out
    prog = dev.get("prog", {})
    unit = 1 if prog.get("pc") == "byte" else 2     # PIC16 sector addresses count words, PIC18's bytes
    if "devid" in facts:
        want = facts["devid"]
        if prog.get("cmd_set") == "6bit" and not prog.get("id_split"):
            want = want >> 5                       # packed word: the pod reports DEV<8:0>
        exp = "%02x%02x" % (want & 0xFF, (want >> 8) & 0xFF)
        if dev["id"] != exp:
            out.append(f"id {dev['id']} (table) vs {exp} (pack DEVID 0x{facts['devid']:04X})")
    for m, key in (("flash", "flash_units"), ("eeprom", "eeprom_units")):
        if m in mem and facts[key] and mem[m]["size"] != facts[key] * (unit if m == "flash" else 1):
            out.append(f"{m}.size {mem[m]['size']} vs {facts[key] * (unit if m == 'flash' else 1)}")
    return out


KNOWN = g.SRC / "pack-differences.yaml"


def known_rules() -> List[Dict[str, Any]]:
    import yaml
    return yaml.safe_load(KNOWN.read_text("utf-8")) if KNOWN.exists() else []


def _as_list(v: Any) -> List[str]:
    return v if isinstance(v, list) else [v]


def is_known(rules: List[Dict[str, Any]], proto: str, dev: Dict[str, Any], diff: str) -> bool:
    field = diff.split(" ", 1)[0]
    for r in rules:
        m = r["match"]
        if m.get("proto") == proto and field in _as_list(m["field"]) \
                and ("name" not in m or dev["name"] in _as_list(m["name"])) \
                and ("family" not in m or dev["family"] in _as_list(m["family"])):
            return True
    return False


def cmd_check(packs: Pack) -> int:
    fams = g.load_source()
    rules = known_rules()
    bad = 0
    seen = missing = known = 0
    for proto, fam in fams.items():
        for dev in fam["devices"]:
            names = [dev["name"], *dev.get("aliases", [])]
            hit = next((n for n in names if packs.has(n)), None)
            if not hit:
                missing += 1
                continue
            seen += 1
            diffs = compare(dev, proto, packs.read(hit))
            known += sum(is_known(rules, proto, dev, d) for d in diffs)
            diffs = [d for d in diffs if not is_known(rules, proto, dev, d)]
            if diffs:
                bad += 1
                print(f"{proto}/{dev['name']}: " + "; ".join(diffs))
    print(f"prog_devices_packs: {seen} chips checked against the packs, {bad} differ, "
          f"{known} known differences (prog-devices/pack-differences.yaml), {missing} not in these packs")
    return 1 if bad else 0


def cmd_new(packs: Pack, like: str, name: str) -> int:
    fams = g.load_source()
    found = [(p, d) for p, f in fams.items() for d in f["devices"] if _norm(d["name"]) == _norm(like)]
    if not found:
        print(f"no chip {like} in prog-devices/", file=sys.stderr)
        return 2
    proto, tmpl = found[0]
    if not packs.has(name):
        print(f"{name} is in none of the packs", file=sys.stderr)
        return 2
    facts = packs.read(name)
    src = next(Path(g.SRC / proto).glob(f"*/{_norm(like)}.yaml"), None) or next(
        p for p in (g.SRC / proto).glob("*/*.yaml") if _norm(p.stem) == _norm(like))
    text = src.read_text("utf-8")
    text = re.sub(r"^name: .*$", f"name: {name}", text, count=1, flags=re.M)
    text = re.sub(r"^aliases: .*\n", "", text, flags=re.M)
    if "id" in facts and facts["kind"] == "avr":
        text = re.sub(r'^id: .*$', f'id: "{facts["id"]}"', text, count=1, flags=re.M)
    if facts["kind"] == "pic" and "devid" in facts:
        want = facts["devid"] >> 5 if tmpl.get("prog", {}).get("cmd_set") == "6bit" and not tmpl["prog"].get("id_split") else facts["devid"]
        text = re.sub(r'^id: .*$', 'id: "%02x%02x"' % (want & 0xFF, want >> 8 & 0xFF), text, count=1, flags=re.M)
    if facts["kind"] == "avr":
        for m in ("flash", "eeprom", "user", "config"):
            if m in facts and m in tmpl["mem"]:
                size, page = facts[m]
                text = re.sub(rf"^(  {m}: \{{size: )\d+(, page: )\d+", rf"\g<1>{size}\g<2>{page or tmpl['mem'][m]['page']}",
                              text, count=1, flags=re.M)
    else:
        unit = 1 if tmpl.get("prog", {}).get("pc") == "byte" else 2
        if facts["flash_units"]:
            text = re.sub(r"^(  flash: \{size: )\d+", rf"\g<1>{facts['flash_units'] * unit}", text, count=1, flags=re.M)
    text = f"# New from {src.name} ({like}) and the pack's device file: check hex addresses, voltage and notes.\n" + text
    out = src.with_name(re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") + ".yaml")
    if out.exists():
        print(f"{out} exists", file=sys.stderr)
        return 2
    out.write_text(text, "utf-8")
    print(f"wrote {out}: now check it and run scripts/gen_prog_devices.py")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("check", "new"))
    ap.add_argument("packs", nargs="+", type=Path)
    ap.add_argument("--like", help="new: the chip whose file is the template")
    ap.add_argument("--name", help="new: the chip to create (a name the pack has)")
    a = ap.parse_args(argv)
    packs = Pack(a.packs)
    if a.action == "check":
        return cmd_check(packs)
    if not (a.like and a.name):
        ap.error("new needs --like and --name")
    return cmd_new(packs, a.like, a.name)


if __name__ == "__main__":
    sys.exit(main())
