"""timed_transactions: the capture view's I2C decode, with timing (the server's Go shape)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from embeddedci.benchpod.i2c import timed_transactions

VECTORS = json.loads((Path(__file__).parent / "testdata" / "i2c_decode_vectors.json").read_text())["cases"]


@pytest.mark.parametrize("case", VECTORS, ids=[c["name"] for c in VECTORS])
def test_same_bus_content_as_the_vectors(case):
    txns = timed_transactions(case["la_words"], case["sda_ch"], case["scl_ch"], case["sample_rate_hz"])
    assert len(txns) == len(case["expect"])
    for got, want in zip(txns, case["expect"]):
        assert got["ok"] == want["ok"]
        assert got.get("error_kind", "") == want["error_kind"]
        addr = [s for s in got["segments"] if s["type"].startswith("address")]
        if want.get("addr") is not None:
            assert addr[0]["value"] == f"0x{want['addr']:02X}"
            assert addr[0]["type"] == ("address-read" if want["read"] else "address-write")
        data = [int(s["value"], 16) for s in got["segments"] if s["type"].startswith("data")]
        assert data[: len(want["data"])] == want["data"]


def test_timing_is_in_microseconds_and_ordered():
    case = VECTORS[0]
    t = timed_transactions(case["la_words"], case["sda_ch"], case["scl_ch"], 1_000_000)[0]
    starts = [s["start_us"] for s in t["segments"]]
    assert starts == sorted(starts)
    assert t["segments"][0]["type"] == "start" and t["segments"][-1]["type"] == "stop"
    assert all(s["end_us"] > s["start_us"] for s in t["segments"])
    assert t["start_us"] == 2.0  # SDA falls with SCL high at sample 2, 1 us per sample
    half = timed_transactions(case["la_words"], case["sda_ch"], case["scl_ch"], 2_000_000)[0]
    assert half["start_us"] == 1.0
