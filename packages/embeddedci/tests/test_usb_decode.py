"""USB decode: Go<->Python parity and the decoder's own checks.

``tests/testdata/usb_decode_vectors.json`` is a byte-identical copy of the EmbeddedCI server's
``testdata/shared/usb_decode_vectors.json``: synthetic full- and low-speed enumerations (NRZI, bit
stuffing, jitter, D+/D- skew, clock error) as run-length edges, and what the Go decoder
(``USBFromLA``) makes of them. Python must decode them the same, field for field.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from embeddedci.benchpod.decode import decode
from embeddedci.benchpod.results import LaCapture
from embeddedci.benchpod.usb_decode import (
    crc5_field,
    crc16,
    decode_usb,
    format_transactions,
    format_transfers,
)

VECTORS = Path(__file__).resolve().parent / "testdata" / "usb_decode_vectors.json"


def _cases():
    return json.loads(VECTORS.read_text())["cases"]


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
        assert isinstance(a, list) and len(a) == len(b), f"{path}: {len(a)} items, want {len(b)}"
        for k, (x, y) in enumerate(zip(a, b)):
            _close(x, y, f"{path}[{k}]")
    elif isinstance(b, float) or isinstance(a, float):
        assert math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9), f"{path}: {a} != {b}"
    else:
        assert a == b, f"{path}: {a!r} != {b!r}"


def _go_shape(d: dict) -> dict:
    """The Go JSON leaves out empty optional fields: do the same for transactions/transfers."""
    for t in d["transactions"]:
        for k in ("data_pid", "data_hex", "handshake", "error", "setup"):
            if t.get(k) in ("", None):
                t.pop(k, None)
    for t in d["transfers"]:
        for k in ("data_hex", "detail"):
            if t.get(k) == "":
                t.pop(k)
    return d


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_go_parity(case):
    p = case["params"]
    res = decode_usb(_words(case), dp=p["dp"], dm=p["dm"], sample_rate_hz=case["sample_rate_hz"],
                     speed=p["speed"])
    _close(_go_shape(res.as_dict()), case["expect"])


def test_crc():
    assert crc5_field(0) == 0x02  # SETUP 0/0 is 2D 00 10 on the wire
    assert crc16(b"123456789") == 0xB4C8
    assert crc16(b"") == 0


def test_enumeration_reads_as_a_list():
    case = _cases()[0]
    cap = LaCapture(words=_words(case), sample_rate_hz=case["sample_rate_hz"])
    res = cap.decode("usb", dp=1, dm=2)
    assert res.speed == "full" and res.error_count == 0
    lines = format_transfers(res)
    assert any("SET_ADDRESS 5: OK" in ln for ln in lines)
    assert any("VID 0x0483 PID 0x5740" in ln for ln in lines)
    assert any('"BenchPod"' in ln for ln in lines)
    assert any("class SET_IDLE" in ln and "STALL" in ln for ln in lines)
    assert any("bus reset" in ln for ln in format_transactions(res))


def test_errors_flagged_per_packet():
    case = next(c for c in _cases() if "errors" in c["name"])
    res = decode(_words(case), "usb", sample_rate_hz=case["sample_rate_hz"], dp=1, dm=2, speed="full")
    assert [p.error for p in res.packets[:2]] == ["bad CRC16", "bad PID check"]
    assert res.packets[-1].pid == "NAK" and res.packets[-1].ok


@pytest.mark.parametrize("kwargs, msg", [
    ({"dp": 1, "dm": 1, "sample_rate_hz": 48e6}, "two different channels"),
    ({"dp": 1, "dm": 2, "sample_rate_hz": 12e6, "speed": "full"}, "48 MS/s"),
    ({"dp": 1, "dm": 2, "sample_rate_hz": 48e6, "speed": "high"}, "full, low or auto"),
])
def test_bad_arguments(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        decode_usb([], **kwargs)


def test_se0_only_is_a_reset():
    res = decode_usb([0] * 200, dp=1, dm=2, sample_rate_hz=48e6)
    assert [p.kind for p in res.packets] == ["reset"]
