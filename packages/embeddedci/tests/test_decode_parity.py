"""Go<->Python UART and SPI decode parity (T-3).

The same idea as ``test_i2c_parity.py``: raw 14-channel LA words, the decoder parameters and the
frames the server's Go decoders (``decodeUARTFromLA``, ``decodeSPIFromLA``) produce for them. The
vectors were generated from the Go decoders, so a Python change that decodes differently fails
here. The source of truth is ``embeddedci-common/testdata/{uart,spi}_decode_vectors.json`` (next
to the I2C file); a byte-identical copy is vendored under ``tests/testdata/`` because this
package's CI has no sibling checkout, and ``test_vendored_vectors_in_sync`` compares the two
whenever the sibling is present.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from embeddedci.benchpod.decode import decode, decode_spi, decode_uart

TESTDATA = Path(__file__).resolve().parent / "testdata"
FILES = ["uart_decode_vectors.json", "spi_decode_vectors.json"]


def _source(name: str) -> "Path | None":
    for parent in Path(__file__).resolve().parents:
        cand = parent / "embeddedci-common" / "testdata" / name
        if cand.exists():
            return cand
    return None


def _cases(name: str):
    return json.loads((TESTDATA / name).read_text())["cases"]


def _check(frames, expect):
    got = [dataclasses.asdict(f) for f in frames]
    assert len(got) == len(expect), f"{len(got)} frames, want {len(expect)}: {got}"
    for g, w in zip(got, expect):
        w = dict(w)
        if "error" in g:
            w.setdefault("error", "")  # Go omits an empty UART error
        for key in ("start_us", "end_us"):
            assert g.pop(key) == pytest.approx(w.pop(key), abs=1e-6), key
        assert g == w


@pytest.mark.parametrize("name", FILES)
def test_vendored_vectors_in_sync(name):
    source = _source(name)
    if source is None:
        pytest.skip(f"embeddedci-common/testdata/{name} not present (published/CI checkout)")
    assert json.loads((TESTDATA / name).read_text()) == json.loads(source.read_text()), (
        f"tests/testdata/{name} has drifted from {source}; re-copy it")


@pytest.mark.parametrize("case", _cases("uart_decode_vectors.json"), ids=lambda c: c["name"])
def test_uart_decode_parity(case):
    _check(decode_uart(case["la_words"], sample_rate_hz=case["sample_rate_hz"], **case["params"]),
           case["expect"])


@pytest.mark.parametrize("case", _cases("spi_decode_vectors.json"), ids=lambda c: c["name"])
def test_spi_decode_parity(case):
    _check(decode_spi(case["la_words"], sample_rate_hz=case["sample_rate_hz"], **case["params"]),
           case["expect"])


def test_the_unified_entry_point_decodes_the_same():
    uart = _cases("uart_decode_vectors.json")[0]
    _check(decode(uart["la_words"], "uart", sample_rate_hz=uart["sample_rate_hz"], **uart["params"]),
           uart["expect"])
    spi = _cases("spi_decode_vectors.json")[0]
    _check(decode(spi["la_words"], "spi", sample_rate_hz=spi["sample_rate_hz"], **spi["params"]),
           spi["expect"])


def test_every_vector_file_covers_errors_and_modes():
    """Keep the vectors meaningful: UART parity and framing errors, all four SPI modes."""
    errors = {e.get("error") for c in _cases("uart_decode_vectors.json") for e in c["expect"]}
    assert {"parity", "framing"} <= errors
    assert {c["params"]["mode"] for c in _cases("spi_decode_vectors.json")} == {0, 1, 2, 3}
