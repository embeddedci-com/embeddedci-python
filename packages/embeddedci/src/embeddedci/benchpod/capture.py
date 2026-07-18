"""Capture orchestration — ADC scope, raw logic-analyzer, and correlated ADC+LA.

Built on the transport's chunked-stream primitives so the SAME code path serves a LAN/serial
pod and a cloud device over the byte tunnel (which GitHub-OIDC auth can reach). Raw ADC counts
are scaled to volts here using the device :class:`~embeddedci.benchpod.capabilities.Capabilities`
— mirroring the server's ADC affine model — so a test gets calibrated volts regardless of
transport, without needing the server's orchestration endpoints.
"""

from __future__ import annotations

from typing import Any, List, Optional

from .capabilities import Capabilities
from .errors import BenchPodError
from .results import AnalogCapture, Capture, LaCapture


def _rate_hz(chunk_rate: float, requested_mhz: Optional[float]) -> float:
    """Prefer the firmware's achieved rate; fall back to the requested rate."""
    if chunk_rate and chunk_rate > 0:
        return float(chunk_rate)
    if requested_mhz and requested_mhz > 0:
        return float(requested_mhz) * 1e6
    return 0.0


def _stream_or_samples(transport: Any, req: dict):
    """Yield chunk dicts from ``stream_chunks`` if the transport has it, else adapt ``samples``."""
    sc = getattr(transport, "stream_chunks", None)
    if sc is not None:
        yield from sc(req)
        return
    fn = getattr(transport, "samples", None)
    if fn is None:
        raise BenchPodError("this transport does not support captures")
    yield {"status": "ok", "data": fn(req), "more": False}


def scope_capture(transport: Any, caps: Capabilities, *, samples: int = 256,
                  sample_rate_mhz: Optional[float] = None, source: str = "") -> Capture:
    """Capture ``samples`` ADC samples and return a :class:`Capture` with calibrated volts."""
    req: dict = {"cmd": "capture", "samples": int(samples)}
    if sample_rate_mhz is not None:
        req["sample_rate_mhz"] = sample_rate_mhz
    counts: List[int] = []
    rate_hz = 0.0
    for chunk in _stream_or_samples(transport, req):
        if chunk.get("adc_rate_hz"):
            rate_hz = float(chunk["adc_rate_hz"])
        data = chunk.get("data")
        if isinstance(data, list):
            counts.extend(int(x) for x in data)
    volts = [caps.counts_to_volts(c) for c in counts]
    return Capture(counts=counts, volts=volts,
                   sample_rate_hz=_rate_hz(rate_hz, sample_rate_mhz), source=source or "ext")


def capture_la(transport: Any, *, samples: int = 4096,
               sample_rate_mhz: Optional[float] = None) -> LaCapture:
    """Capture ``samples`` raw 12-channel LA words and return a :class:`LaCapture`."""
    req: dict = {"cmd": "la_capture", "samples": int(samples)}
    if sample_rate_mhz is not None:
        req["sample_rate_mhz"] = sample_rate_mhz
    words: List[int] = []
    rate_hz = 0.0
    for chunk in _stream_or_samples(transport, req):
        if chunk.get("la_rate_hz"):
            rate_hz = float(chunk["la_rate_hz"])
        data = chunk.get("data")
        if isinstance(data, list):
            words.extend(int(x) for x in data)
    return LaCapture(words=words, sample_rate_hz=_rate_hz(rate_hz, sample_rate_mhz))


def _expand_la_edges(edges: List[List[int]], upto: int) -> List[int]:
    """Expand RLE LA transitions ``[[sampleIndex, word], ...]`` back to ``upto`` dense words.

    Inverse of the server's ``laEdges``: a word holds from its transition index until the next
    one. Before the first transition (index 0 for a fresh capture) the level is 0.
    """
    if upto <= 0:
        return []
    out = [0] * upto
    if not edges:
        return out
    cur = edges[0][1]
    ei = 0
    n = len(edges)
    for j in range(upto):
        while ei < n and edges[ei][0] == j:
            cur = edges[ei][1]
            ei += 1
        out[j] = cur
    return out


def capture_analog(transport: Any, caps: Capabilities, *, adc_samples: int = 256,
                   adc_rate_mhz: Optional[float] = None, la_samples: int = 256,
                   la_rate_mhz: Optional[float] = None) -> AnalogCapture:
    """Correlated ADC + LA capture from one hardware trigger (aligned timebases).

    Uses the firmware ``capture_dual`` command: the ADC region streams as dense counts and the
    LA region as RLE transition frames, which are reassembled here (mirroring the server). Set
    either count to 0 for a single-stream capture. Requires a streaming transport (TCP/serial or
    the cloud tunnel).
    """
    if adc_samples <= 0 and la_samples <= 0:
        raise BenchPodError("capture_analog needs adc_samples or la_samples > 0")
    if getattr(transport, "stream_chunks", None) is None:
        raise BenchPodError("capture_analog needs a streaming transport (TCP/serial or cloud)")
    req: dict = {"cmd": "capture_dual", "adc_samples": int(adc_samples),
                 "la_samples": int(la_samples)}
    if adc_rate_mhz is not None:
        req["adc_rate_mhz"] = adc_rate_mhz
    if la_rate_mhz is not None:
        req["la_rate_mhz"] = la_rate_mhz

    adc_counts: List[int] = []
    la_edges: List[List[int]] = []
    la_upto = 0
    la_dense: List[int] = []
    adc_rate_hz = la_rate_hz = 0.0
    for chunk in transport.stream_chunks(req):
        if chunk.get("adc_rate_hz"):
            adc_rate_hz = float(chunk["adc_rate_hz"])
        if chunk.get("la_rate_hz"):
            la_rate_hz = float(chunk["la_rate_hz"])
        if chunk.get("la"):
            edges = chunk.get("la_edges") or []
            la_edges.extend([int(e[0]), int(e[1])] for e in edges)
            if int(chunk.get("la_upto", 0)) > la_upto:
                la_upto = int(chunk["la_upto"])
        else:
            data = chunk.get("data")
            if isinstance(data, list):
                # ADC dense region first, then (older firmware) any dense LA overflow.
                if len(adc_counts) < adc_samples:
                    take = adc_samples - len(adc_counts)
                    adc_counts.extend(int(x) for x in data[:take])
                    la_dense.extend(int(x) for x in data[take:])
                else:
                    la_dense.extend(int(x) for x in data)

    if la_edges or la_upto:
        la_words = _expand_la_edges(la_edges, la_upto or la_samples)
    else:
        la_words = la_dense[:la_samples] if la_samples else la_dense

    adc = Capture(counts=adc_counts, volts=[caps.counts_to_volts(c) for c in adc_counts],
                  sample_rate_hz=_rate_hz(adc_rate_hz, adc_rate_mhz), source="ext")
    la = LaCapture(words=la_words, sample_rate_hz=_rate_hz(la_rate_hz, la_rate_mhz))
    return AnalogCapture(adc=adc, la=la)
