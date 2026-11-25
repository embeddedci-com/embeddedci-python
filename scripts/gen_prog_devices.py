#!/usr/bin/env python3
"""Generate the pin programmers' device tables from prog-devices/ (one YAML file per chip).

    python scripts/gen_prog_devices.py                 # write the SDK table
    python scripts/gen_prog_devices.py --check         # fail when a generated copy is stale
    python scripts/gen_prog_devices.py --cli ../benchpod-cli --web ../embeddedci-server/webapp/src/progIds.json

The source is ``prog-devices/<proto>/_protocol.yaml`` (target name, title, image regions to ignore)
and ``prog-devices/<proto>/<family>/<chip>.yaml``; the schema is in ``prog-devices/README.md``.
Outputs, all generated, never edited by hand:

* ``packages/embeddedci/src/embeddedci/benchpod/prog_devices.json`` (the SDK table, schema 2);
* ``<cli>/internal/progdev/prog_devices.json`` and its README (``--cli``), a byte copy of it;
* ``<web>`` (``--web``): the web app's detect table, ``progIds.json``.

Each chip carries ``prog``, the named protocol fields the pod's descriptor takes (firmware 4.0.6+,
feature ``prog_named``), and ``legacy``, the same options as the packed ``flags`` integer (and PIC
``cp_addr``) that older firmware reads. The mapping from the named fields to ``flags`` lives here
and nowhere else; the hosts just send what the table says.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "prog-devices"
SDK_DIR = ROOT / "packages" / "embeddedci" / "src" / "embeddedci" / "benchpod"
SDK_JSON = SDK_DIR / "prog_devices.json"
SDK_README = SDK_DIR / "prog_devices.README.md"
SCHEMA = 2
PROTOS = ("updi", "pic", "c2", "tpi", "pdi", "sbw")
MEMS = ("flash", "eeprom", "user", "config")   # the order the hosts list them in

# The legacy `flags` bits of the firmware's prog_pic.h / prog_avr12.h / prog_c2.h.
PIC_F_SET8, PIC_F_BYTEPC, PIC_F_HAS_EE, PIC_F_CP_HIGH = 1, 2, 4, 8
PIC_F_Q10, PIC_F_ID_SPLIT, PIC_F_KEY_FLIP = 16, 32, 64
TPI_F_PAGE_ERASE = 8
C2_FLAG_NO_SETUP = 0x100
#: the PIC fields that decide how a chip is entered and its ID read (the host probes by them)
PIC_ENTRY_FIELDS = ("cmd_set", "pc", "q10", "id_split", "key_flip")


class SourceError(Exception):
    pass


def _where(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def _check(cond: bool, where: Path, msg: str) -> None:
    if not cond:
        raise SourceError(f"{_where(where)}: {msg}")


def _keys(d: Any, allowed: Tuple[str, ...], where: Path, what: str) -> Dict[str, Any]:
    _check(isinstance(d, dict), where, f"{what} must be a mapping")
    extra = sorted(set(d) - set(allowed))
    _check(not extra, where, f"{what}: unknown key {extra[0] if extra else ''} (allowed: {', '.join(allowed)})")
    return d


def _int(v: Any, where: Path, what: str, lo: int = 0, hi: int = 1 << 32) -> int:
    _check(isinstance(v, int) and not isinstance(v, bool) and lo <= v < hi, where,
           f"{what} must be an integer in {lo}..{hi - 1}, got {v!r}")
    return v


def _bool(v: Any, where: Path, what: str) -> bool:
    _check(isinstance(v, bool), where, f"{what} must be true or false, got {v!r}")
    return v


def _enum(v: Any, allowed: Tuple[str, ...], where: Path, what: str) -> str:
    _check(v in allowed, where, f"{what} must be one of {', '.join(allowed)}, got {v!r}")
    return v


def parse_prog(proto: str, raw: Any, where: Path) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """The chip's ``prog:`` block -> (the named wire fields, the legacy options)."""
    raw = {} if raw is None else raw
    legacy: Dict[str, int] = {}
    prog: Dict[str, Any] = {}
    if proto == "updi":
        r = _keys(raw, ("flash_base",), where, "prog")
        if "flash_base" in r:
            prog["flash_base"] = _int(r["flash_base"], where, "prog.flash_base")
            legacy["flags"] = prog["flash_base"]
    elif proto == "pic":
        r = _keys(raw, ("cmd_set", "pc", "ee_erase", "q10", "id_split", "key_flip", "cp"), where, "prog")
        flags = 0
        prog["cmd_set"] = _enum(r.get("cmd_set", "6bit"), ("6bit", "8bit"), where, "prog.cmd_set")
        if prog["cmd_set"] == "8bit":
            flags |= PIC_F_SET8
        if "pc" in r:
            prog["pc"] = _enum(r["pc"], ("word", "byte"), where, "prog.pc")
            _check(prog["pc"] == "word" or prog["cmd_set"] == "8bit", where, "prog.pc byte needs cmd_set 8bit")
            if prog["pc"] == "byte":
                flags |= PIC_F_BYTEPC
        for key, bit in (("ee_erase", PIC_F_HAS_EE), ("q10", PIC_F_Q10), ("id_split", PIC_F_ID_SPLIT),
                         ("key_flip", PIC_F_KEY_FLIP)):
            if key in r and _bool(r[key], where, "prog." + key):
                prog[key] = True
                flags |= bit
        _check(not prog.get("q10") or prog.get("pc") == "byte", where, "prog.q10 needs prog.pc byte")
        if "cp" in r:
            cp = _keys(r["cp"], ("addr", "bit", "protected_when"), where, "prog.cp")
            addr = _int(cp.get("addr"), where, "prog.cp.addr", 1)
            bit = _int(cp.get("bit", 0), where, "prog.cp.bit", 0, 16)
            when = _int(cp.get("protected_when", 0), where, "prog.cp.protected_when", 0, 2)
            prog["cp_addr"] = addr
            prog["cp_bit"] = bit
            if when:
                prog["cp_high"] = True
                flags |= PIC_F_CP_HIGH
            flags |= bit << 8
            legacy["cp_addr"] = addr
        if flags:
            legacy["flags"] = flags
    elif proto == "tpi":
        r = _keys(raw, ("write_words", "page_erase"), where, "prog")
        flags = 0
        if "write_words" in r:
            w = _int(r["write_words"], where, "prog.write_words", 1, 5)
            _check(w in (1, 2, 4), where, "prog.write_words must be 1, 2 or 4")
            prog["write_words"] = w
            flags |= w if w > 1 else 0   # prog_tpi.c reads 0 and 1 alike
        if "page_erase" in r and _bool(r["page_erase"], where, "prog.page_erase"):
            prog["page_erase"] = True
            flags |= TPI_F_PAGE_ERASE
        if flags:
            legacy["flags"] = flags
    elif proto == "c2":
        r = _keys(raw, ("fpdat", "no_setup"), where, "prog")
        flags = 0
        if "fpdat" in r:
            prog["fpdat"] = _int(r["fpdat"], where, "prog.fpdat", 1, 256)
            flags |= prog["fpdat"]
        if "no_setup" in r and _bool(r["no_setup"], where, "prog.no_setup"):
            prog["no_setup"] = True
            flags |= C2_FLAG_NO_SETUP
        if flags:
            legacy["flags"] = flags
    else:
        _keys(raw, (), where, "prog")
    return prog, legacy


def parse_chip(proto: str, path: Path) -> Dict[str, Any]:
    raw = yaml.safe_load(path.read_text("utf-8"))
    r = _keys(raw, ("name", "aliases", "id", "family", "voltage", "voltage_note", "chip_erase_only",
                    "notes", "source", "prog", "mem"), path, "chip")
    dev: Dict[str, Any] = {}
    _check(isinstance(r.get("name"), str) and r["name"], path, "name is required")
    dev["name"] = r["name"]
    if "aliases" in r:
        _check(isinstance(r["aliases"], list) and all(isinstance(a, str) for a in r["aliases"]), path,
               "aliases must be a list of names")
        dev["aliases"] = r["aliases"]
    _check(isinstance(r.get("id"), str) and re.fullmatch(r"([0-9a-f]{2}|\?\?)+", r["id"]) is not None, path,
           "id must be a quoted string of lower-case hex bytes (?? for a varying byte)")
    dev["id"] = r["id"]
    _check(isinstance(r.get("family"), str) and r["family"], path, "family is required")
    dev["family"] = r["family"]
    if "voltage" in r:
        v = _keys(r["voltage"], ("min_mv", "max_mv", "erase_min_mv"), path, "voltage")
        for src, dst in (("min_mv", "vmin_mv"), ("max_mv", "vmax_mv"), ("erase_min_mv", "erase_vmin_mv")):
            if src in v:
                dev[dst] = _int(v[src], path, "voltage." + src, 1, 100000)
    for key, dst in (("voltage_note", "vnote"), ("notes", "notes")):
        if key in r:
            _check(isinstance(r[key], str), path, key + " must be text")
            dev[dst] = r[key]
    if "source" in r:
        _check(isinstance(r["source"], str), path, "source must be text")   # provenance, not shipped
    if "chip_erase_only" in r:
        dev["chip_erase_only"] = _bool(r["chip_erase_only"], path, "chip_erase_only")
    prog, legacy = parse_prog(proto, r.get("prog"), path)
    if prog:
        dev["prog"] = prog
    if legacy:
        dev["legacy"] = legacy
    mems = _keys(r.get("mem"), MEMS, path, "mem")
    _check(bool(mems), path, "mem needs at least one memory")
    dev["mem"] = {}
    for name in MEMS:
        if name not in mems:
            continue
        m = _keys(mems[name], ("size", "page", "addr", "hex", "align", "stride"), path, "mem." + name)
        out = {}
        for k in ("size", "page", "addr", "hex"):
            _check(k in m, path, f"mem.{name}.{k} is required")
            out[k] = _int(m[k], path, f"mem.{name}.{k}", 1 if k in ("size", "page") else 0)
        for k in ("align", "stride"):
            if k in m:
                out[k] = _int(m[k], path, f"mem.{name}.{k}", 1)
        dev["mem"][name] = out
    # image windows of one chip must not overlap: a hex byte belongs to one memory
    spans = sorted((m["hex"], m["hex"] + m["size"] * m.get("stride", 1), n) for n, m in dev["mem"].items())
    for (a0, a1, an), (b0, _b1, bn) in zip(spans, spans[1:]):
        _check(b0 >= a1, path, f"mem.{an} and mem.{bn} overlap in the image (hex addresses)")
    return dev


def natural(s: str) -> List[Any]:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def load_source(src: Path = SRC) -> Dict[str, Any]:
    fams: Dict[str, Any] = {}
    for proto in PROTOS:
        pdir = src / proto
        pfile = pdir / "_protocol.yaml"
        _check(pfile.is_file(), pfile, "missing")
        p = _keys(yaml.safe_load(pfile.read_text("utf-8")), ("target", "title", "ignore"), pfile, "protocol")
        fam: Dict[str, Any] = {"target": p["target"], "title": p["title"]}
        if p.get("ignore"):
            fam["ignore"] = [dict(_keys(g, ("hex", "size", "name"), pfile, "ignore"))
                             for g in p["ignore"]]
        devs = []
        for chip in sorted(pdir.glob("*/*.yaml"), key=lambda f: (natural(f.parent.name), natural(f.stem))):
            devs.append(parse_chip(proto, chip))
        names = [d["name"].lower().replace(" ", "").replace("-", "") for d in devs]
        dup = sorted({n for n in names if names.count(n) > 1})
        _check(not dup, pfile, f"duplicate chip name {dup[:1]}")
        fam["devices"] = devs
        if proto == "pic":
            fam["entry_styles"] = pic_entry_styles(devs)
        fams[proto] = fam
    return fams


def pic_entry_styles(devs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The distinct ways the table's PICs are entered, the plain 6-bit set first: what a host tries,
    in turn, when no chip is named. ``prog`` holds the named fields, ``flags`` the legacy integer."""
    seen: Dict[str, Dict[str, Any]] = {}

    def put(prog: Dict[str, Any]) -> None:
        legacy = (PIC_F_SET8 if prog["cmd_set"] == "8bit" else 0) | (PIC_F_BYTEPC if prog.get("pc") == "byte" else 0) \
            | (PIC_F_Q10 if prog.get("q10") else 0) | (PIC_F_ID_SPLIT if prog.get("id_split") else 0) \
            | (PIC_F_KEY_FLIP if prog.get("key_flip") else 0)
        seen[json.dumps(prog, sort_keys=True)] = {"prog": prog, "flags": legacy}

    put({"cmd_set": "6bit"})
    for d in devs:
        put({k: d["prog"][k] for k in PIC_ENTRY_FIELDS if k in d["prog"]})
    return sorted(seen.values(), key=lambda s: s["flags"])


ABOUT = ("BenchPod prog_flash device table, generated from prog-devices/ (one YAML file per chip) by "
         "scripts/gen_prog_devices.py: do not edit. See prog_devices.README.md for the schema and the sources.")


def render_sdk(fams: Dict[str, Any]) -> str:
    """One chip per line, as the table has always been written: a diff shows the chip that changed."""
    out = ['{', f'"schema":{SCHEMA},', f'"about":{json.dumps(ABOUT)},', '"families":{']
    protos = []
    for proto, fam in fams.items():
        head = {k: v for k, v in fam.items() if k != "devices"}
        s = [f'{json.dumps(proto)}:{{']
        for k, v in head.items():
            s.append(f'{json.dumps(k)}:{json.dumps(v, separators=(",", ":"))},')
        s.append('"devices":[')
        s.append(",\n".join(json.dumps(d, separators=(",", ":")) for d in fam["devices"]))
        s.append("]}")
        protos.append("\n".join(s))
    out.append(",\n".join(protos))
    out.append("}\n}\n")
    return "\n".join(out)


def render_web(fams: Dict[str, Any]) -> str:
    t: Dict[str, Any] = {
        "_comment": "Programming-pin chip IDs for detect: name and ID (hex) per protocol; PIC entry styles with "
                    "their named fields and the legacy flags integer. Generated by embeddedci-python "
                    "scripts/gen_prog_devices.py from prog-devices/: do not edit.",
        "version": 2}
    for proto, fam in fams.items():
        t[proto] = [[d["name"], d["id"]] for d in fam["devices"]]
    t["pic_entry_styles"] = fams["pic"]["entry_styles"]
    return json.dumps(t, separators=(",", ":")) + "\n"


def outputs(fams: Dict[str, Any], cli: Path | None, web: Path | None) -> Dict[Path, str]:
    sdk = render_sdk(fams)
    res = {SDK_JSON: sdk}
    if cli:
        res[cli / "internal" / "progdev" / "prog_devices.json"] = sdk
        res[cli / "internal" / "progdev" / "prog_devices.README.md"] = SDK_README.read_text("utf-8")
    if web:
        res[web] = render_web(fams)
    return res


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="write nothing; exit 1 when a generated copy differs")
    ap.add_argument("--cli", type=Path, help="a benchpod-cli checkout: its internal/progdev copy")
    ap.add_argument("--web", type=Path, help="the web app's progIds.json")
    ap.add_argument("--src", type=Path, default=SRC)
    a = ap.parse_args(argv)
    try:
        fams = load_source(a.src)
    except SourceError as e:
        print("gen_prog_devices:", e, file=sys.stderr)
        return 2
    stale = []
    for path, text in outputs(fams, a.cli, a.web).items():
        if path.exists() and path.read_text("utf-8") == text:
            continue
        stale.append(path)
        if not a.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, "utf-8")
    n = sum(len(f["devices"]) for f in fams.values())
    if a.check and stale:
        for p in stale:
            print(f"gen_prog_devices: {p} is out of date: run scripts/gen_prog_devices.py", file=sys.stderr)
        return 1
    print(f"gen_prog_devices: {n} chips, {'up to date' if a.check or not stale else 'wrote ' + str(len(stale)) + ' file(s)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
