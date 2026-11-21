"""Modbus RTU, CAN / CAN FD, PWM, 1-Wire and SMBus/PMBus: Go<->Python parity and own checks.

``tests/testdata/{modbus,can,pwm,onewire,smbus}_decode_vectors.json`` are byte-identical copies
of the EmbeddedCI server's ``testdata/shared`` files: synthetic captures as run-length edges, the
channels and options, and what the Go decoder returns. Python must decode them the same.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from embeddedci.benchpod.can_decode import can_crc, stuff_count
from embeddedci.benchpod.decode import decode
from embeddedci.benchpod.modbus_decode import modbus_crc
from embeddedci.benchpod.onewire_decode import crc8
from embeddedci.benchpod.smbus_decode import linear11, pec8

TESTDATA = Path(__file__).resolve().parent / "testdata"
NAMES = ["modbus", "can", "pwm", "onewire", "smbus"]
#: vector option names -> Python keyword names
KW = {"baud": "baud", "bitrate": "bitrate", "data_bitrate": "data_bitrate", "sample_point": "sample_point",
      "data_sample_point": "data_sample_point", "channels": "channels", "pec": "pec"}


def _cases():
    for n in NAMES:
        for c in json.loads((TESTDATA / f"{n}_decode_vectors.json").read_text())["cases"]:
            yield pytest.param(c, id=f"{n}-{c['name']}")


def _words(case) -> list:
    words = [0] * case["count"]
    edges = case["edges"]
    for k, (at, w) in enumerate(edges):
        end = edges[k + 1][0] if k + 1 < len(edges) else case["count"]
        words[at:end] = [w] * (end - at)
    return words


def _close(a, b, path="") -> None:
    if isinstance(b, dict):
        assert isinstance(a, dict) and set(a) == set(b), f"{path}: keys {sorted(a)} != {sorted(b)}"
        for k in b:
            _close(a[k], b[k], f"{path}.{k}")
    elif isinstance(b, list):
        assert isinstance(a, list) and len(a) == len(b), f"{path}: {len(a) if isinstance(a, list) else a} items, want {len(b)}"
        for k, (x, y) in enumerate(zip(a, b)):
            _close(x, y, f"{path}[{k}]")
    elif isinstance(b, (int, float)) and not isinstance(b, bool) and isinstance(a, (int, float)) and not isinstance(a, bool):
        assert math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9), f"{path}: {a} != {b}"
    else:
        assert a == b, f"{path}: {a!r} != {b!r}"


def _as_go(proto: str, res):
    if proto == "pwm":
        return res.as_dict()
    key = "frames" if proto in ("modbus", "can") else "transactions"
    return {key: [x.as_dict() for x in res]}


@pytest.mark.parametrize("case", list(_cases()))
def test_go_parity(case):
    kw = dict(case["channels"])
    kw.update({KW[k]: v for k, v in case["options"].items()})
    res = decode(_words(case), case["protocol"], sample_rate_hz=case["sample_rate_hz"], **kw)
    got = _as_go(case["protocol"], res)
    exp = dict(case["expect"])
    exp.pop("protocol", None)
    got.pop("protocol", None)
    _close(got, exp)


def test_checksums():
    f = bytes([0x11, 0x03, 0x00, 0x6B, 0x00, 0x03])
    assert modbus_crc(f) == 0x8776
    assert crc8(bytes([0x02, 0x1C, 0xB8, 0x01, 0x00, 0x00, 0x00])) == 0xA2
    assert pec8(b"123456789") == 0xF4
    assert linear11(0xD3E8) == 15.625 and linear11(0xFFFF) == -0.5
    assert can_crc([], 15, 0x4599, 0) == 0 and can_crc([1], 15, 0x4599, 0) == 0x4599
    assert stuff_count(3) == [0, 1, 0, 1]


def test_readable_results():
    cases = {c.values[0]["name"]: c.values[0] for c in _cases()}
    mb = cases["rtu_19200_session"]
    frames = decode(_words(mb), "modbus", sample_rate_hz=mb["sample_rate_hz"], rx=3, baud=19200)
    assert frames[1].summary == "READ_HOLDING_REGISTERS 107: 555 0 100" and frames[1].reply_to == 1
    can = cases["fd_500k_2m_48msps"]
    cf = decode(_words(can), "can", sample_rate_hz=can["sample_rate_hz"], can=4, bitrate=500e3, data_bitrate=2e6)
    assert any(f.fd and f.brs and f.ok and f.len == 64 for f in cf)
    assert any(f.kind == "error" for f in cf)
    ow = cases["standard_and_overdrive_4msps"]
    tx = decode(_words(ow), "onewire", sample_rate_hz=ow["sample_rate_hz"], onewire=6)
    assert any(t.function == "READ_SCRATCHPAD" and t.detail == "21.5 °C" for t in tx)
    pm = cases["pmbus_100k_1msps"]
    st = decode(_words(pm), "pmbus", sample_rate_hz=pm["sample_rate_hz"], sda=1, scl=2)
    assert [t.value for t in st[:2]] == ["linear, exponent -9", "3 V"]


@pytest.mark.parametrize("proto, kw, msg", [
    ("can", {"can": 1, "bitrate": 1e6}, "too low"),
    ("pwm", {"channels": []}, "one or more"),
    ("onewire", {"onewire": 1}, "too low"),
    ("smbus", {"sda": 1, "scl": 2, "pec": "maybe"}, "auto, on or off"),
])
def test_bad_arguments(proto, kw, msg):
    with pytest.raises(ValueError, match=msg):
        decode([1] * 100, proto, sample_rate_hz=100e3, **kw)
