"""Capture orchestration tests against a fake streaming transport (no hardware)."""

from __future__ import annotations

from typing import Any, Dict, Iterator, List

import pytest

from embeddedci.benchpod import capture as cap_mod
from embeddedci.benchpod.capabilities import Capabilities
from embeddedci.benchpod.results import AnalogCapture, Capture, LaCapture


class FakeCaptureTransport:
    """Serves canned chunk streams for capture/la_capture/capture_dual."""

    def __init__(self, chunks_by_cmd: Dict[str, List[dict]]) -> None:
        self._chunks = chunks_by_cmd
        self.sent: List[dict] = []

    def stream_chunks(self, req: dict) -> Iterator[Dict[str, Any]]:
        self.sent.append(req)
        for c in self._chunks.get(req["cmd"], [{"status": "ok", "data": [], "more": False}]):
            yield c


NAIVE = Capabilities.from_status({"adc_bits": 8, "adc_fullscale_mv": 2550})  # 10 mV/count


def test_scope_capture_scales_to_volts_and_reads_rate():
    t = FakeCaptureTransport({"capture": [
        {"status": "ok", "data": [0, 128, 255], "adc_rate_hz": 400000.0, "more": True},
        {"status": "ok", "data": [255], "more": False},
    ]})
    cap = cap_mod.scope_capture(t, NAIVE, samples=4, sample_rate_mhz=0.5)
    assert isinstance(cap, Capture)
    assert cap.counts == [0, 128, 255, 255]
    assert cap.sample_rate_hz == 400000.0  # achieved rate preferred over requested
    assert cap.volts[0] == pytest.approx(0.0)
    assert cap.volts[2] == pytest.approx(2.55, rel=1e-6)
    assert t.sent[0] == {"cmd": "capture", "samples": 4, "sample_rate_mhz": 0.5}


def test_capture_la_collects_words():
    t = FakeCaptureTransport({"la_capture": [
        {"status": "ok", "data": [1, 2, 4, 8], "la_rate_hz": 1e6, "more": False},
    ]})
    la = cap_mod.capture_la(t, samples=4, sample_rate_mhz=1)
    assert isinstance(la, LaCapture)
    assert la.words == [1, 2, 4, 8]
    assert la.channel(1) == [1, 0, 0, 0]
    assert la.channel(4) == [0, 0, 0, 1]


def test_capture_analog_expands_rle_la_edges():
    # ADC dense (2 samples) + LA as RLE transitions covering 5 samples.
    t = FakeCaptureTransport({"capture_dual": [
        {"status": "ok", "data": [10, 20], "adc_rate_hz": 400000.0, "la_rate_hz": 1e6, "more": True},
        {"status": "ok", "la": True, "la_edges": [[0, 3], [2, 7]], "la_upto": 5, "more": False},
    ]})
    ac = cap_mod.capture_analog(t, NAIVE, adc_samples=2, adc_rate_mhz=0.4, la_samples=5, la_rate_mhz=1)
    assert isinstance(ac, AnalogCapture)
    assert ac.adc.counts == [10, 20]
    # edges: word 3 from index0, word 7 from index2 -> [3,3,7,7,7]
    assert ac.la.words == [3, 3, 7, 7, 7]
    assert ac.adc.sample_rate_hz == 400000.0
    assert ac.la.sample_rate_hz == 1e6


def test_capture_analog_dense_la_fallback():
    # older firmware: one combined dense array, split at adc_samples.
    t = FakeCaptureTransport({"capture_dual": [
        {"status": "ok", "data": [1, 2, 3, 100, 200, 300], "more": False},
    ]})
    ac = cap_mod.capture_analog(t, NAIVE, adc_samples=3, la_samples=3)
    assert ac.adc.counts == [1, 2, 3]
    assert ac.la.words == [100, 200, 300]


def test_capture_analog_requires_streaming_transport():
    class NoStream:
        pass

    with pytest.raises(Exception):
        cap_mod.capture_analog(NoStream(), NAIVE, adc_samples=2, la_samples=2)
