"""Go<->Python I2C decode parity.

Verifies the Python ``decode_from_la`` against the SAME shared vector file the Go
server checks (embeddedci-common/testdata/i2c_decode_vectors.json): raw 12-channel
LA words + expected decode.  Keeping both decoders locked to one vector file stops
them drifting now that all protocol interpretation lives off the FPGA.

Regenerate the vectors with scratchpad/gen_i2c_vectors.go.
"""
import json
from pathlib import Path

import pytest

from embeddedci.benchpod.i2c import decode_from_la


def _find_vectors() -> Path:
    """Walk up to the repo root and locate the shared vector file."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "embeddedci-common" / "testdata" / "i2c_decode_vectors.json"
        if cand.exists():
            return cand
    raise FileNotFoundError("shared i2c_decode_vectors.json not found (need embeddedci-common sibling)")


def _canon(txn) -> dict:
    """Canonical form matching the Go test: {addr, read, data, ok, error_kind}."""
    msg = txn.messages[0]
    if msg.address_ack is False:
        ok, err = False, "address-nack"
    elif (not msg.read) and any(not b.ack for b in msg.data):
        ok, err = False, "data-nack"
    else:
        ok, err = True, ""
    return {
        "addr": msg.address,
        "read": bool(msg.read),
        "data": [b.value for b in msg.data],
        "ok": ok,
        "error_kind": err,
    }


def _load_cases():
    doc = json.loads(_find_vectors().read_text())
    return doc["cases"]


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["name"])
def test_i2c_decode_parity(case):
    txns = decode_from_la(case["la_words"], case["sda_ch"], case["scl_ch"])
    assert len(txns) == len(case["expect"]), f"txn count {len(txns)} != {len(case['expect'])}"
    for got_txn, want in zip(txns, case["expect"]):
        got = _canon(got_txn)
        assert got["addr"] == want["addr"]
        assert got["read"] == want["read"]
        assert got["data"] == want["data"]
        assert got["ok"] == want["ok"]
        assert got["error_kind"] == want["error_kind"]
