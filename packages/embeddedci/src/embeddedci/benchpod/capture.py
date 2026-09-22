"""Capture orchestration — ADC, raw logic-analyzer, and correlated ADC+LA.

Built on the transport's chunked-stream primitives so the SAME code path serves a LAN/serial
pod and a cloud device over the byte tunnel (which GitHub-OIDC auth can reach). Raw ADC counts
are scaled to volts here using the device :class:`~embeddedci.benchpod.capabilities.Capabilities`
— mirroring the server's ADC affine model — so a test gets calibrated volts regardless of
transport, without needing the server's orchestration endpoints.

The public API speaks hertz and seconds; the firmware's ``*_rate_mhz`` / ``*_us`` wire fields are
produced only here.
"""

from __future__ import annotations

import base64
import sys
from array import array
from typing import Any, List, Optional

from .capabilities import Capabilities
from .errors import BenchPodError
from .results import Capture, CorrelatedCapture, LaCapture, Trigger

#: The firmware's single-shot ``capture`` reads into a fixed buffer of this many samples; a larger
#: ADC capture streams from PSRAM through ``capture_dual`` instead.
ADC_SHALLOW_MAX_SAMPLES = 32768

#: Longest a triggered capture waits for its condition (the firmware's cap), seconds.
MAX_TRIGGER_TIMEOUT = 600.0


def _apply_trigger(req: dict, trigger: Optional[Trigger], timeout: float) -> None:
    """Add a resolved :class:`Trigger` (integer channel) and its timeout to a capture request."""
    if trigger is None:
        return
    if not isinstance(trigger, Trigger) or not isinstance(trigger.la, int):
        raise ValueError("trigger must be a Trigger with an LA channel number (names are resolved by BenchPod)")
    if not 0 < timeout <= MAX_TRIGGER_TIMEOUT:
        raise ValueError(f"trigger_timeout must be > 0 and <= {MAX_TRIGGER_TIMEOUT:g} seconds, got {timeout!r}")
    req["trigger"] = {"la": trigger.la, "edge": trigger.edge}
    req["trigger_timeout_ms"] = max(1, int(round(timeout * 1000)))


def rate_mhz(rate_hz: Optional[float]) -> Optional[float]:
    """Hertz → the firmware's ``sample_rate_mhz`` (``None`` stays ``None`` = device default)."""
    if rate_hz is None:
        return None
    if rate_hz <= 0:
        raise ValueError(f"sample rate must be > 0 Hz, got {rate_hz!r}")
    return float(rate_hz) / 1e6


def _us(seconds: Optional[float], name: str) -> int:
    if seconds is None:
        return 0
    if seconds < 0:
        raise ValueError(f"{name} must be >= 0 seconds, got {seconds!r}")
    return int(round(seconds * 1e6))


def _rate_hz(chunk_rate: float, requested_hz: Optional[float]) -> float:
    """Prefer the firmware's achieved rate; fall back to the requested rate."""
    if chunk_rate and chunk_rate > 0:
        return float(chunk_rate)
    if requested_hz and requested_hz > 0:
        return float(requested_hz)
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


def _want_b64(transport: Any, caps: Capabilities, req: dict) -> None:
    """Ask for base64 ADC chunks when the pod offers them and the transport yields whole chunks.

    A pod without ``capture_b64`` never sees the key, and the ``samples`` fallback flattens only
    ``data``, so it is left out there too.
    """
    if caps.capture_b64 and getattr(transport, "stream_chunks", None) is not None:
        req["enc"] = "b64"


def _dense(chunk: dict) -> List[int]:
    """The dense samples of one chunk: ``"b64"`` (unpadded base64url of little-endian uint16)
    or the decimal ``"data"`` list. Empty when the chunk carries neither."""
    b64 = chunk.get("b64")
    if isinstance(b64, str):
        raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        words = array("H")
        words.frombytes(raw[: len(raw) // 2 * 2])
        if sys.byteorder == "big":
            words.byteswap()
        return words.tolist()
    data = chunk.get("data")
    if isinstance(data, list):
        return [int(x) for x in data]
    return []


def _check_samples(samples: int, name: str = "samples") -> int:
    n = int(samples)
    if n <= 0:
        raise ValueError(f"{name} must be > 0, got {samples!r}")
    return n


def capture_adc(transport: Any, caps: Capabilities, *, samples: int = 4096,
                sample_rate_hz: Optional[float] = None, source: str = "",
                trigger: Optional[Trigger] = None, trigger_timeout: float = 10.0) -> Capture:
    """Capture ``samples`` ADC samples and return a :class:`Capture` with calibrated volts.

    Up to :data:`ADC_SHALLOW_MAX_SAMPLES` this is the firmware's single-shot ``capture``; above it
    — and whenever a ``trigger`` is given — the capture streams from PSRAM via ``capture_dual``
    (ADC only), which needs a streaming transport. ``source`` only labels the result — routing is
    the caller's job.
    """
    n = _check_samples(samples)
    if n > ADC_SHALLOW_MAX_SAMPLES or trigger is not None:
        if getattr(transport, "stream_chunks", None) is None:
            raise BenchPodError(
                "a deep or triggered ADC capture streams from PSRAM and needs a TCP, serial or cloud "
                "connection with chunked streaming"
            )
        adc = capture_correlated(transport, caps, adc_samples=n, adc_sample_rate_hz=sample_rate_hz,
                                 la_samples=0, trigger=trigger, trigger_timeout=trigger_timeout).adc
        adc.source = source
        return adc
    req: dict = {"cmd": "capture", "samples": n}
    mhz = rate_mhz(sample_rate_hz)
    if mhz is not None:
        req["sample_rate_mhz"] = mhz
    _want_b64(transport, caps, req)
    counts: List[int] = []
    rate = 0.0
    for chunk in _stream_or_samples(transport, req):
        if chunk.get("adc_rate_hz"):
            rate = float(chunk["adc_rate_hz"])
        counts.extend(_dense(chunk))
    volts = [caps.counts_to_volts(c) for c in counts]
    return Capture(counts=counts, volts=volts, sample_rate_hz=_rate_hz(rate, sample_rate_hz),
                   source=source)


def capture_la(transport: Any, *, samples: int = 4096, sample_rate_hz: Optional[float] = None,
               stop_dac_after: Optional[float] = None, trigger: Optional[Trigger] = None,
               trigger_timeout: float = 10.0) -> LaCapture:
    """Capture ``samples`` raw 12-channel LA words and return a :class:`LaCapture`.

    ``stop_dac_after`` (seconds) auto-stops a concurrently-running DAC that far into the capture —
    the iCE40 cuts the DAC at exactly that offset from the capture's hardware t0 (sample-precise),
    so the captured window shows the target reacting to the output dropping. No-op on
    gateware < v21.
    """
    req: dict = {"cmd": "la_capture", "samples": _check_samples(samples)}
    mhz = rate_mhz(sample_rate_hz)
    if mhz is not None:
        req["sample_rate_mhz"] = mhz
    stop_us = _us(stop_dac_after, "stop_dac_after")
    if stop_us > 0:
        req["stop_dac_after_us"] = stop_us
    _apply_trigger(req, trigger, trigger_timeout)
    dense: List[int] = []
    edges: List[List[int]] = []
    upto = 0
    rate = 0.0
    for chunk in _stream_or_samples(transport, req):
        if chunk.get("la_rate_hz"):
            rate = float(chunk["la_rate_hz"])
        # The firmware answers la_capture RUN-LENGTH ENCODED — {"la":true,"la_edges":[[i,word],…],
        # "la_upto":N} — exactly as it does for capture_dual, and sends no "data" array at all. RLE
        # wins when present; dense "data" remains the fallback for the server-relayed shape.
        if chunk.get("la"):
            edges.extend([int(e[0]), int(e[1])] for e in (chunk.get("la_edges") or []))
            if int(chunk.get("la_upto", 0)) > upto:
                upto = int(chunk["la_upto"])
            continue
        data = chunk.get("data")
        if isinstance(data, list):
            dense.extend(int(x) for x in data)
    words = _expand_la_edges(edges, upto or int(samples)) if (edges or upto) else dense
    return LaCapture(words=words, sample_rate_hz=_rate_hz(rate, sample_rate_hz), trigger=trigger)


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


def capture_correlated(transport: Any, caps: Capabilities, *, adc_samples: int = 4096,
                       adc_sample_rate_hz: Optional[float] = None, la_samples: int = 4096,
                       la_sample_rate_hz: Optional[float] = None,
                       stop_dac_after: Optional[float] = None, trigger: Optional[Trigger] = None,
                       trigger_timeout: float = 10.0) -> CorrelatedCapture:
    """Correlated ADC + LA capture from one hardware trigger (aligned timebases).

    Uses the firmware ``capture_dual`` command: the ADC region streams as dense counts and the
    LA region as RLE transition frames, which are reassembled here (mirroring the server). Set
    either count to 0 for a single-stream capture — ``la_samples=0`` is also how a deep,
    multi-second ADC capture is taken. Requires a streaming transport.

    ``stop_dac_after`` (seconds) auto-stops a concurrently-running DAC that far into the capture,
    cut by the iCE40 at exactly that offset from the capture's hardware t0 (no-op on gw < v21).
    """
    adc_samples, la_samples = int(adc_samples), int(la_samples)
    if adc_samples < 0 or la_samples < 0:
        raise ValueError("adc_samples and la_samples must be >= 0")
    if adc_samples == 0 and la_samples == 0:
        raise ValueError("capture_correlated needs adc_samples or la_samples > 0")
    if getattr(transport, "stream_chunks", None) is None:
        raise BenchPodError("capture_correlated needs a streaming transport (TCP, serial or cloud)")
    req: dict = {"cmd": "capture_dual", "adc_samples": adc_samples, "la_samples": la_samples}
    adc_mhz = rate_mhz(adc_sample_rate_hz)
    la_mhz = rate_mhz(la_sample_rate_hz)
    if adc_mhz is not None:
        req["adc_rate_mhz"] = adc_mhz
    if la_mhz is not None:
        req["la_rate_mhz"] = la_mhz
    stop_us = _us(stop_dac_after, "stop_dac_after")
    if stop_us > 0:
        req["stop_dac_after_us"] = stop_us
    _apply_trigger(req, trigger, trigger_timeout)
    if adc_samples:
        _want_b64(transport, caps, req)

    adc_counts: List[int] = []
    la_edges: List[List[int]] = []
    la_upto = 0
    la_dense: List[int] = []
    adc_rate = la_rate = 0.0
    for chunk in transport.stream_chunks(req):
        if chunk.get("adc_rate_hz"):
            adc_rate = float(chunk["adc_rate_hz"])
        if chunk.get("la_rate_hz"):
            la_rate = float(chunk["la_rate_hz"])
        if chunk.get("la"):
            edges = chunk.get("la_edges") or []
            la_edges.extend([int(e[0]), int(e[1])] for e in edges)
            if int(chunk.get("la_upto", 0)) > la_upto:
                la_upto = int(chunk["la_upto"])
        else:
            data = _dense(chunk)
            # ADC dense region first, then (older firmware) any dense LA overflow.
            if len(adc_counts) < adc_samples:
                take = adc_samples - len(adc_counts)
                adc_counts.extend(data[:take])
                la_dense.extend(data[take:])
            else:
                la_dense.extend(data)

    if la_edges or la_upto:
        la_words = _expand_la_edges(la_edges, la_upto or la_samples)
    else:
        la_words = la_dense[:la_samples] if la_samples else la_dense

    adc = Capture(counts=adc_counts, volts=[caps.counts_to_volts(c) for c in adc_counts],
                  sample_rate_hz=_rate_hz(adc_rate, adc_sample_rate_hz), trigger=trigger)
    la = LaCapture(words=la_words, sample_rate_hz=_rate_hz(la_rate, la_sample_rate_hz),
                   trigger=trigger)
    return CorrelatedCapture(adc=adc, la=la)
