"""Capture orchestration tests against a fake streaming transport (no hardware)."""

from __future__ import annotations

from typing import Any, Dict, Iterator, List

import pytest

from embeddedci.benchpod import capture as cap_mod
from embeddedci.benchpod.capabilities import Capabilities
from embeddedci.benchpod.errors import BenchPodError
from embeddedci.benchpod.results import Capture, CorrelatedCapture, LaCapture


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


def test_capture_adc_scales_to_volts_and_reads_rate():
    t = FakeCaptureTransport({"capture": [
        {"status": "ok", "data": [0, 128, 255], "adc_rate_hz": 400000.0, "more": True},
        {"status": "ok", "data": [255], "more": False},
    ]})
    cap = cap_mod.capture_adc(t, NAIVE, samples=4, sample_rate_hz=500_000)
    assert isinstance(cap, Capture)
    assert cap.counts == [0, 128, 255, 255]
    assert cap.sample_rate_hz == 400000.0  # achieved rate preferred over requested
    assert cap.volts[0] == pytest.approx(0.0)
    assert cap.volts[2] == pytest.approx(2.55, rel=1e-6)
    # The public API speaks hertz; the wire field stays the firmware's MHz.
    assert t.sent[0] == {"cmd": "capture", "samples": 4, "sample_rate_mhz": 0.5}


def test_capture_adc_falls_back_to_requested_rate():
    t = FakeCaptureTransport({"capture": [{"status": "ok", "data": [1, 2], "more": False}]})
    cap = cap_mod.capture_adc(t, NAIVE, samples=2, sample_rate_hz=250_000)
    assert cap.sample_rate_hz == 250_000
    assert cap.duration == pytest.approx(2 / 250_000)


def test_capture_adc_above_the_shallow_buffer_streams_from_psram():
    n = cap_mod.ADC_SHALLOW_MAX_SAMPLES + 1
    t = FakeCaptureTransport({"capture_dual": [
        {"status": "ok", "data": [7] * n, "adc_rate_hz": 400000.0, "more": False},
    ]})
    cap = cap_mod.capture_adc(t, NAIVE, samples=n, source="ext")
    assert t.sent[0]["cmd"] == "capture_dual"
    assert t.sent[0]["adc_samples"] == n and t.sent[0]["la_samples"] == 0
    assert len(cap) == n and cap.source == "ext"


def _b64(words):
    """Encode like the firmware: unpadded base64url of little-endian uint16."""
    import base64
    import struct
    return base64.urlsafe_b64encode(struct.pack(f"<{len(words)}H", *words)).rstrip(b"=").decode()


B64_CAPS = Capabilities.from_status({"adc_bits": 8, "adc_fullscale_mv": 2550,
                                     "caps": ["signal", "capture_b64"]})


def test_capture_b64_is_requested_only_when_the_pod_offers_it():
    assert B64_CAPS.capture_b64 and not NAIVE.capture_b64
    t = FakeCaptureTransport({})
    cap_mod.capture_adc(t, NAIVE, samples=4)
    assert "enc" not in t.sent[0]  # an older pod never sees the key
    cap_mod.capture_adc(t, B64_CAPS, samples=4)
    assert t.sent[1]["enc"] == "b64"
    # An LA-only capture has no dense ADC region to encode.
    cap_mod.capture_correlated(t, B64_CAPS, adc_samples=0, la_samples=8)
    assert "enc" not in t.sent[2]


def test_capture_adc_decodes_b64_chunks():
    # Chunk lengths that leave every base64 remainder (1, 2, 0 bytes mod 3) and the extremes.
    words = [0, 1, 255, 256, 0x1234, 32767, 32768, 65534, 65535]
    t = FakeCaptureTransport({"capture": [
        {"status": "ok", "bits": 16, "b64": _b64(words[:1]), "adc_rate_hz": 400000.0, "more": True},
        {"status": "chunk", "b64": _b64(words[1:4]), "more": True},
        {"status": "chunk", "b64": _b64(words[4:]), "more": False},
    ]})
    cap = cap_mod.capture_adc(t, B64_CAPS, samples=len(words))
    assert cap.counts == words
    assert cap.sample_rate_hz == 400000.0


def test_capture_correlated_decodes_b64_adc_alongside_rle_la():
    t = FakeCaptureTransport({"capture_dual": [
        {"status": "ok", "bits": 16, "adc_rate_hz": 400000, "la_rate_hz": 1000000,
         "b64": _b64([10, 20, 30]), "more": True},
        {"status": "chunk", "la": True, "la_edges": [[0, 5], [2, 6]], "la_upto": 4, "more": False},
    ]})
    cc = cap_mod.capture_correlated(t, B64_CAPS, adc_samples=3, la_samples=4)
    assert t.sent[0]["enc"] == "b64"
    assert cc.adc.counts == [10, 20, 30]
    assert cc.la.words == [5, 5, 6, 6]


def test_capture_adc_accepts_decimal_chunks_from_older_firmware():
    # A pod that ignores "enc" answers with "data": the SDK must take either.
    t = FakeCaptureTransport({"capture": [{"status": "ok", "data": [1, 2, 3], "more": False}]})
    assert cap_mod.capture_adc(t, B64_CAPS, samples=3).counts == [1, 2, 3]


def test_capture_adc_deep_needs_streaming():
    class SamplesOnly:
        def samples(self, req):
            return []

    with pytest.raises(BenchPodError, match="PSRAM"):
        cap_mod.capture_adc(SamplesOnly(), NAIVE, samples=cap_mod.ADC_SHALLOW_MAX_SAMPLES + 1)


def test_capture_rejects_bad_counts_and_rates():
    t = FakeCaptureTransport({})
    with pytest.raises(ValueError):
        cap_mod.capture_adc(t, NAIVE, samples=0)
    with pytest.raises(ValueError):
        cap_mod.capture_la(t, samples=8, sample_rate_hz=0)
    with pytest.raises(ValueError):
        cap_mod.capture_la(t, samples=8, stop_dac_after=-1)
    assert t.sent == []


def test_capture_la_collects_words():
    t = FakeCaptureTransport({"la_capture": [
        {"status": "ok", "data": [1, 2, 4, 8], "la_rate_hz": 1e6, "more": False},
    ]})
    la = cap_mod.capture_la(t, samples=4, sample_rate_hz=1e6)
    assert isinstance(la, LaCapture)
    assert la.words == [1, 2, 4, 8]
    assert la.channel(1) == [1, 0, 0, 0]
    assert la.channel(4) == [0, 0, 0, 1]
    assert la.edges(1) == 1
    assert t.sent[0]["sample_rate_mhz"] == 1.0


def test_capture_la_expands_rle_frames():
    t = FakeCaptureTransport({"la_capture": [
        {"status": "ok", "la": True, "la_edges": [[0, 1], [3, 0]], "la_upto": 5, "more": False},
    ]})
    la = cap_mod.capture_la(t, samples=5)
    assert la.words == [1, 1, 1, 0, 0]


def test_capture_correlated_expands_rle_la_edges():
    # ADC dense (2 samples) + LA as RLE transitions covering 5 samples.
    t = FakeCaptureTransport({"capture_dual": [
        {"status": "ok", "data": [10, 20], "adc_rate_hz": 400000.0, "la_rate_hz": 1e6, "more": True},
        {"status": "ok", "la": True, "la_edges": [[0, 3], [2, 7]], "la_upto": 5, "more": False},
    ]})
    ac = cap_mod.capture_correlated(t, NAIVE, adc_samples=2, adc_sample_rate_hz=400_000,
                                    la_samples=5, la_sample_rate_hz=1e6)
    assert isinstance(ac, CorrelatedCapture)
    assert ac.adc.counts == [10, 20]
    # edges: word 3 from index0, word 7 from index2 -> [3,3,7,7,7]
    assert ac.la.words == [3, 3, 7, 7, 7]
    assert ac.adc.sample_rate_hz == 400000.0
    assert ac.la.sample_rate_hz == 1e6
    assert t.sent[0]["adc_rate_mhz"] == pytest.approx(0.4) and t.sent[0]["la_rate_mhz"] == 1.0


def test_capture_correlated_dense_la_fallback():
    # older firmware: one combined dense array, split at adc_samples.
    t = FakeCaptureTransport({"capture_dual": [
        {"status": "ok", "data": [1, 2, 3, 100, 200, 300], "more": False},
    ]})
    ac = cap_mod.capture_correlated(t, NAIVE, adc_samples=3, la_samples=3)
    assert ac.adc.counts == [1, 2, 3]
    assert ac.la.words == [100, 200, 300]


def test_capture_correlated_requires_streaming_transport():
    class NoStream:
        pass

    with pytest.raises(BenchPodError):
        cap_mod.capture_correlated(NoStream(), NAIVE, adc_samples=2, la_samples=2)


def test_capture_correlated_needs_a_stream():
    with pytest.raises(ValueError):
        cap_mod.capture_correlated(FakeCaptureTransport({}), NAIVE, adc_samples=0, la_samples=0)
