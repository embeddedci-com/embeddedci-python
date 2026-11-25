"""The device table's named chip fields (prog_devices.json schema 2) and the wire parity with the
packed ``flags`` of firmware 4.0.5 and older.

``data/prog_wire_golden.json`` holds, for every chip and memory of the table, the ``start`` request
(and for every PIC the ``info`` request) that SDK 4.0.5 built from the schema 1 table. Sending the
legacy fields must reproduce them exactly; the named fields must mean the same to the firmware
(``fw_flags`` below is prog_desc.c, written again independently of the generator).
"""
import json
import pathlib
from typing import Any, Dict

import pytest

from embeddedci.benchpod import prog

from test_prog_flash import FakePod, FakeTransport, make_pod, app_hex

GOLDEN = json.loads((pathlib.Path(__file__).parent / "data" / "prog_wire_golden.json").read_text())


def _pins(proto: str) -> Dict[str, int]:
    return {"clk": 3, "data": 4} if proto != "updi" else {"data": 4}


def _requests(named: bool):
    out = []
    for proto, fam in prog.families().items():
        for dev in fam.devices:
            for mem, m in dev.mem.items():
                r = prog.Region(mem=mem, addr=m.addr, data=b"\0" * m.page, page=m.page, hex=m.hex)
                out.append({"proto": proto, "name": dev.name, "mem": mem,
                            "start": prog.start_request(proto, _pins(proto), region=r, dev=dev,
                                                        erase="chip", run_after=True, named=named)})
            if proto == "pic":
                sent = []

                def cmd(q: Dict[str, Any], dev=dev) -> Dict[str, Any]:
                    sent.append(q)
                    return {"running": False, "result": "ok", "id": dev.expect_id or ""}

                prog.read_info(cmd, proto, _pins(proto), sleep=lambda s: None, fields=dev.fields(named))
                out.append({"proto": proto, "name": dev.name, "mem": "", "info": sent[0]})
    return out


def test_legacy_requests_are_byte_identical_to_sdk_405_for_every_chip():
    assert len(GOLDEN) == 1129
    now = json.loads(json.dumps(_requests(named=False)))
    key = lambda r: json.dumps(r, sort_keys=True)   # noqa: E731
    assert sorted(now, key=key) == sorted(GOLDEN, key=key)


def fw_flags(proto: str, f: Dict[str, Any]) -> int:
    """What prog_desc.c turns the named request fields into (the firmware's ctx->flags)."""
    flags = 0
    if proto == "updi":
        flags = f.get("flash_base", 0)
    elif proto == "pic":
        flags |= 1 if f.get("cmd_set") == "8bit" else 0
        flags |= 2 if f.get("pc") == "byte" else 0
        flags |= 4 if f.get("ee_erase") else 0
        flags |= 8 if f.get("cp_high") else 0
        flags |= 16 if f.get("q10") else 0
        flags |= 32 if f.get("id_split") else 0
        flags |= 64 if f.get("key_flip") else 0
        flags |= (f.get("cp_bit", 0) & 15) << 8
    elif proto == "tpi":
        flags = (f.get("write_words", 0) if f.get("write_words", 1) > 1 else 0) | (8 if f.get("page_erase") else 0)
    elif proto == "c2":
        flags = f.get("fpdat", 0) | (0x100 if f.get("no_setup") else 0)
    return flags


def test_named_fields_mean_the_same_flags_as_the_golden_requests_for_every_chip():
    now = _requests(named=True)
    by = {(g["proto"], g["name"], g["mem"]): g for g in GOLDEN}
    for r in now:
        old = by[(r["proto"], r["name"], r["mem"])]
        o = old.get("start") or old["info"]
        n = r.get("start") or r["info"]
        assert "flags" not in n, r
        assert fw_flags(r["proto"], n) == o.get("flags", 0), (r["name"], n, o)
        # cp_addr keeps its name and value; everything that is not a chip field is untouched
        assert n.get("cp_addr") == o.get("cp_addr"), r["name"]
        chip = {"flags", "cp_addr", "flash_base", "cmd_set", "pc", "ee_erase", "cp_high", "q10", "id_split",
                "key_flip", "cp_bit", "write_words", "page_erase", "fpdat", "no_setup"}
        assert {k: v for k, v in n.items() if k not in chip} == {k: v for k, v in o.items() if k not in chip}


def test_every_chip_has_the_fields_its_flags_need():
    for proto, fam in prog.families().items():
        for dev in fam.devices:
            assert fw_flags(proto, dev.prog) == dev.legacy.get("flags", 0), dev.name
            if proto == "pic":
                assert dev.prog["cp_addr"] == dev.legacy["cp_addr"]


def test_pic_entry_styles_have_both_forms():
    styles = prog.pic_entry_styles()
    assert styles[0] == {"prog": {"cmd_set": "6bit"}, "flags": 0}
    assert [s["flags"] for s in styles] == [0, 1, 3, 19, 32]
    for s in styles:
        assert fw_flags("pic", s["prog"]) == s["flags"]


# -- the pod side: a pod with the prog_named feature gets named fields ---------------------------

def _named_pod(pod: FakePod) -> Any:
    from embeddedci.benchpod import feature_map
    rev = feature_map.revision()
    assert rev >= 21
    from embeddedci.benchpod.client import BenchPod as BP
    return BP(transport=FakeTransport(pod, features_rev=rev), lease=False,
              wiring={"swd_swclk": 11, "swd_swdio": 12, "signals": []})


def test_a_pod_with_prog_named_gets_named_fields_and_an_old_pod_gets_flags(tmp_path):
    for features_rev, key, value in ((21, "flash_base", 0x8000), (20, "flags", 0x8000)):
        pod = FakePod()
        from embeddedci.benchpod.client import BenchPod as BP
        bp = BP(transport=FakeTransport(pod, features_rev=features_rev), lease=False,
                wiring={"swd_swclk": 11, "swd_swdio": 12, "signals": []})
        assert bp.supports("prog_named") == (features_rev >= 21)
        assert bp.flash(target="avr-updi", file=app_hex(tmp_path)).ok
        s1 = pod.starts()[0]
        assert s1[key] == value
        assert ("flags" in s1) == (key == "flags") and ("flash_base" in s1) == (key == "flash_base")


def test_pic_probe_with_named_fields_tries_each_style():
    class Pod(FakePod):
        def command(self, req):
            if req.get("cmd") == "prog_flash" and req.get("op") == "info":
                ok = req.get("cmd_set") == "8bit" and req.get("pc") == "byte"
                self.info_result = "ok" if ok else "no answer (wiring, power or I/O voltage)"
            return super().command(req)

    pod = Pod(chip_id="8074")
    pod.mv = 3300
    assert _named_pod(pod).flash(target="pic", dry_run=True, clk=3, data=4).ok
    seen = [(i.get("cmd_set"), i.get("pc")) for i in pod.infos()]
    assert seen == [("6bit", None), ("8bit", None), ("8bit", "byte")]
