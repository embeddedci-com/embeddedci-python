"""Record → DAC replay DSP, a pure-Python mirror of the server's ``benchpod_recording.go``.

When a cloud recording is replayed through the server (``POST /dac/replay/start``) the server
does this DSP itself. But the SDK also replays **client-side over the byte tunnel** (so it works
identically on a LAN/serial-direct pod and under GitHub-OIDC cloud auth, which cannot reach the
server's replay endpoint). To keep both paths bit-for-bit consistent, this module reproduces the
server's pipeline exactly:

    window  ->  block-average downsample  ->  volts→codes (faithful|fit)  ->  device bit width

Kept dependency-free (no numpy) so it runs anywhere the base SDK installs. The functions are
unit-tested against fixed vectors and, where a shared fixture exists, against the Go output.
"""

from __future__ import annotations

import base64
import math
import struct
from typing import List, Sequence

FAITHFUL = "faithful"
FIT = "fit"

# The shallow DAC waveform-BRAM limit a non-deep replay downsamples to (server
# benchpodReplayMaxSamples).
REPLAY_MAX_SAMPLES = 4096

#: Named DAC output paths → full-scale volts (server benchpodDacPathFullScaleV).
DAC_PATH_FULLSCALE_V = {"3v3": 3.3, "5v": 5.0, "12v": 12.0}


def dac_path_fullscale_v(dac_path: str, fallback: float = 5.0) -> float:
    """Full-scale volts for a named DAC output path (``3v3``/``5v``/``12v``)."""
    return DAC_PATH_FULLSCALE_V.get((dac_path or "").lower().replace(".", ""), fallback)


# -- decode ------------------------------------------------------------------

def decode_recording_volts(raw: bytes, full_scale_v: float) -> List[float]:
    """Turn a 16-bit-LE recording blob into volts (``v = sample/65535 * full_scale_v``)."""
    n = len(raw) // 2
    if n == 0:
        return []
    samples = struct.unpack("<%dH" % n, raw[: n * 2])
    scale = full_scale_v / 65535.0
    return [s * scale for s in samples]


def encode_recording_volts(volts: Sequence[float], full_scale_v: float) -> bytes:
    """Inverse of :func:`decode_recording_volts`: volts → a 16-bit-LE recording blob.

    Used to save a captured ADC waveform (already in volts) to the library as a recording.
    """
    if full_scale_v <= 0:
        full_scale_v = 1.0
    out = bytearray(len(volts) * 2)
    for i, v in enumerate(volts):
        code = int(round(v / full_scale_v * 65535.0))
        code = 0 if code < 0 else (65535 if code > 65535 else code)
        struct.pack_into("<H", out, i * 2, code)
    return bytes(out)


# -- window / downsample -----------------------------------------------------

def window_volts(v: Sequence[float], start: int, length: int) -> List[float]:
    """Select ``[start, start+length)`` of ``v``, clamped. ``length<=0`` means to the end."""
    n = len(v)
    if n == 0:
        return list(v)
    if start < 0:
        start = 0
    if start >= n:
        start = n - 1
    end = n
    if length > 0 and start + length < end:
        end = start + length
    return list(v[start:end])


def block_average_downsample(v: Sequence[float], max_out: int) -> List[float]:
    """Reduce ``v`` to at most ``max_out`` samples by averaging contiguous blocks.

    Anti-aliased decimation; returns ``v`` unchanged when it already fits. Mirrors the
    server's ``blockAverageDownsample``.
    """
    if max_out < 1:
        max_out = 1
    n = len(v)
    if n <= max_out:
        return list(v)
    block = (n + max_out - 1) // max_out
    out: List[float] = []
    for i in range(0, n, block):
        chunk = v[i:i + block]
        out.append(sum(chunk) / len(chunk))
    return out


# -- volts → codes -----------------------------------------------------------

def _clamp(x: float, hi: int) -> int:
    x = int(round(x))
    if x < 0:
        return 0
    return hi if x > hi else x


def volts_to_codes(v: Sequence[float], mapping: str, path_full_scale_v: float,
                   bits: int = 8) -> List[int]:
    """Map volts to DAC codes for ``bits`` resolution (8 or 16).

    ``faithful``: ``code = clamp(round(v/path_full_scale_v * max))`` — reproduce the recorded
    voltage on the DAC (clips outside the output range). ``fit``: auto-scale the recording's own
    ``[min,max]`` across the full code range (shape preserved, absolute amplitude not). Mirrors
    the server's ``voltsToCodes`` / ``voltsToCodes16``.
    """
    hi = (1 << bits) - 1
    mid = 1 << (bits - 1)
    n = len(v)
    if n == 0:
        return []
    if mapping == FIT:
        vmin = min(v)
        vmax = max(v)
        span = vmax - vmin
        if span <= 0:
            return [mid] * n
        return [_clamp((x - vmin) / span * hi, hi) for x in v]
    fs = path_full_scale_v if path_full_scale_v > 0 else 1.0
    return [_clamp(x / fs * hi, hi) for x in v]


def codes_to_bytes(codes: Sequence[int], bits: int) -> bytes:
    """Pack DAC codes into the device's replay byte stream (8-bit => 1 byte, 16-bit => LE)."""
    if bits <= 8:
        return bytes(min(255, max(0, int(c))) & 0xFF for c in codes)
    out = bytearray(len(codes) * 2)
    for i, c in enumerate(codes):
        c = 0 if c < 0 else (65535 if c > 65535 else int(c))
        struct.pack_into("<H", out, i * 2, c)
    return bytes(out)


# -- fault injection ---------------------------------------------------------

def apply_fault(codes: List[int], fault: "dict | None", bits: int = 8) -> List[int]:
    """Splice a fault into a code array (mirrors the server's ``applyWaveformFault``).

    ``fault`` is ``{"type": "flatline"|"spike"|"stuck", "start": i, "width": w, "level": code}``.
    ``start``/``width`` are sample indices into ``codes``; ``level`` is a raw code (defaults to 0
    for flatline, max for spike). Returns ``codes`` (mutated in place and also returned).
    """
    if not fault:
        return codes
    n = len(codes)
    if n == 0:
        return codes
    hi = (1 << bits) - 1
    ftype = str(fault.get("type", "")).lower()
    start = int(fault.get("start", 0))
    width = int(fault.get("width", 0))
    if start < 0:
        start = 0
    if start >= n:
        return codes
    if width <= 0 or start + width > n:
        width = n - start
    level = fault.get("level")
    if ftype in ("flatline", "stuck"):
        lvl = hi if (ftype == "stuck" and level is None) else (0 if level is None else int(level))
        lvl = 0 if lvl < 0 else (hi if lvl > hi else lvl)
        for i in range(start, start + width):
            codes[i] = lvl
    elif ftype == "spike":
        lvl = hi if level is None else int(level)
        lvl = 0 if lvl < 0 else (hi if lvl > hi else lvl)
        for i in range(start, start + width):
            codes[i] = lvl
    return codes


# -- segments ----------------------------------------------------------------

def segments_to_volts(segments: Sequence[dict], sample_rate_hz: float) -> List[float]:
    """Render a piecewise segment spec into a volts array at ``sample_rate_hz``.

    Each segment is ``{"shape": "ramp"|"hold"|"step", "duration_ms": d, "v_start": a,
    "v_end": b}``. ``hold`` holds ``v_start``; ``step`` jumps to ``v_end``; ``ramp`` linearly
    interpolates ``v_start``→``v_end``. Mirrors the server's segment builder used by
    ``POST /benchpod/waveforms/segments``.
    """
    out: List[float] = []
    if sample_rate_hz <= 0:
        sample_rate_hz = 1.0
    for seg in segments:
        shape = str(seg.get("shape", "hold")).lower()
        dur_ms = float(seg.get("duration_ms", 0) or 0)
        v0 = float(seg.get("v_start", 0) or 0)
        v1 = float(seg.get("v_end", v0) if seg.get("v_end") is not None else v0)
        count = max(1, int(round(dur_ms / 1000.0 * sample_rate_hz)))
        if shape == "ramp":
            if count == 1:
                out.append(v1)
            else:
                for i in range(count):
                    out.append(v0 + (v1 - v0) * (i / (count - 1)))
        elif shape == "step":
            out.extend([v1] * count)
        else:  # hold
            out.extend([v0] * count)
    return out


# -- top-level record → replay codes (client-side replay path) ---------------

def recording_to_replay_codes(
    raw: bytes,
    *,
    src_full_scale_v: float,
    dac_path: str = "5v",
    mapping: str = FAITHFUL,
    window_start: int = 0,
    window_len: int = 0,
    target_samples: int = 0,
    bits: int = 8,
    deep: bool = False,
    max_samples: int = REPLAY_MAX_SAMPLES,
    fault: "dict | None" = None,
) -> "ReplayCodes":
    """Run the full record→replay DSP and return the device-ready code bytes + effective rate.

    ``deep`` keeps the full windowed depth (downsampling only when it exceeds ``max_samples``,
    the device PSRAM depth), matching the server's ``applyRecordingForReplayDeep``; otherwise it
    downsamples to ``min(target_samples, REPLAY_MAX_SAMPLES)`` like ``applyRecordingForReplay``.
    """
    path_fs = dac_path_fullscale_v(dac_path)
    volts = decode_recording_volts(raw, src_full_scale_v)
    win = window_volts(volts, window_start, window_len)
    if deep:
        out = win if len(win) <= max_samples else block_average_downsample(win, max_samples)
    else:
        tgt = target_samples if 0 < target_samples <= REPLAY_MAX_SAMPLES else REPLAY_MAX_SAMPLES
        out = block_average_downsample(win, tgt)
    codes = volts_to_codes(out, mapping, path_fs, bits=bits)
    if fault:
        codes = apply_fault(codes, fault, bits=bits)
    return ReplayCodes(codes=codes, bits=bits, source_samples=len(win))


class ReplayCodes:
    """DAC codes ready to load + replay, with the width they were packed for."""

    __slots__ = ("codes", "bits", "source_samples")

    def __init__(self, codes: List[int], bits: int, source_samples: int) -> None:
        self.codes = codes
        self.bits = bits
        self.source_samples = source_samples

    def to_bytes(self) -> bytes:
        return codes_to_bytes(self.codes, self.bits)

    def to_b64(self) -> str:
        return base64.b64encode(self.to_bytes()).decode("ascii")

    def __len__(self) -> int:
        return len(self.codes)
