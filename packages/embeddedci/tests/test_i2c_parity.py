"""Go<->Python I2C decode parity.

Verifies the Python ``decode_from_la`` against the SAME vector file the Go server
checks (embeddedci-common/testdata/i2c_decode_vectors.json): raw 12-channel LA words
+ expected decode.  Keeping both decoders locked to one vector file stops them
drifting now that all protocol interpretation lives off the FPGA.

embeddedci-python is published on its own, so the sibling embeddedci-common repo is
NOT available in this package's CI (or to downstream users running its tests).  A
byte-identical copy is therefore vendored under ``tests/testdata/`` and the parity
suite always runs against that copy.  ``test_vendored_vectors_in_sync`` guards
against drift: whenever the source-of-truth sibling IS present (local dev), it
asserts the vendored copy matches, so a regeneration that forgets to re-vendor
fails loudly.

Regenerate the vectors with scratchpad/gen_i2c_vectors.go (writes the sibling), then
re-copy into tests/testdata/i2c_decode_vectors.json.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from embeddedci.benchpod.i2c import decode_from_la

VENDORED_VECTORS = Path(__file__).resolve().parent / "testdata" / "i2c_decode_vectors.json"


def _find_source_vectors() -> Path | None:
    """Locate the source-of-truth sibling vector file, if this is a full checkout."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "embeddedci-common" / "testdata" / "i2c_decode_vectors.json"
        if cand.exists():
            return cand
    return None


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
    doc = json.loads(VENDORED_VECTORS.read_text())
    return doc["cases"]


def test_vendored_vectors_in_sync():
    """The vendored copy must match the source-of-truth sibling when it's present.

    Skips on published-package / CI checkouts where the sibling is absent; there the
    vendored copy is authoritative and nothing to compare against.
    """
    source = _find_source_vectors()
    if source is None:
        pytest.skip("embeddedci-common sibling not present (published/CI checkout)")
    assert json.loads(VENDORED_VECTORS.read_text()) == json.loads(source.read_text()), (
        f"vendored {VENDORED_VECTORS} has drifted from source {source}; "
        "re-copy embeddedci-common/testdata/i2c_decode_vectors.json into tests/testdata/"
    )


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
