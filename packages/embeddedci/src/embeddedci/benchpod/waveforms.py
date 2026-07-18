"""The cloud waveform library — list, save, preview, and delete stored waveforms/recordings.

Waveforms live server-side (metadata in the DB, big recordings in S3), so this is a thin client
over :class:`~embeddedci.benchpod.server_api.ServerApi`. Three ``kind``s exist:

* ``dac_waveform`` — authored 8/16-bit DAC codes stored inline;
* ``recording``    — a raw 16-bit ADC capture stored in S3 (what "save a scope capture" produces);
* ``segments``     — a compact piecewise spec rebuilt to codes at replay.

To actually *drive* a stored waveform onto the DAC, use
:meth:`~embeddedci.benchpod.client.BenchPod.replay_waveform` (which can go through the server's
DSP or fetch the blob and replay it over the device tunnel).
"""

from __future__ import annotations

import base64
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .replay import Segment, normalize_segments
from .server_api import ServerApi


@dataclass
class Waveform:
    """One library entry (metadata; the sample blob is fetched separately)."""

    id: str
    name: str
    kind: str = "dac_waveform"
    sample_count: int = 0
    sample_rate_hz: float = 0.0
    bits: int = 8
    full_scale_v: float = 0.0
    recording_size_bytes: int = 0
    created_at: str = ""
    dac_path: str = ""
    segments: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, m: Dict[str, Any]) -> "Waveform":
        return cls(
            id=str(m.get("id", "")),
            name=str(m.get("name", "")),
            kind=str(m.get("kind", "dac_waveform")),
            sample_count=int(m.get("sample_count", 0) or 0),
            sample_rate_hz=float(m.get("sample_rate_hz", 0) or 0),
            bits=int(m.get("bits", 8) or 8),
            full_scale_v=float(m.get("full_scale_v", 0) or 0),
            recording_size_bytes=int(m.get("recording_size_bytes", 0) or 0),
            created_at=str(m.get("created_at", "")),
            dac_path=str(m.get("dac_path", "") or ""),
            segments=list(m.get("segments") or []),
            raw=dict(m),
        )

    @property
    def is_recording(self) -> bool:
        return self.kind == "recording"


class WaveformLibrary:
    """List / save / preview / delete waveforms in the cloud library."""

    def __init__(self, api: ServerApi) -> None:
        self._api = api

    # -- read ---------------------------------------------------------------

    def list(self) -> List[Waveform]:
        """List every waveform in the org's library (no sample blobs)."""
        _, data = self._api.request("GET", "/benchpod/waveforms")
        return [Waveform.from_json(m) for m in (data or {}).get("waveforms", [])]

    def get(self, waveform_id: str) -> Waveform:
        """Fetch one waveform's metadata (``dac_waveform`` also carries ``samples_b64``)."""
        _, data = self._api.request("GET", f"/benchpod/waveforms/{waveform_id}")
        return Waveform.from_json(data or {})

    def find(self, name: str) -> Optional[Waveform]:
        """Return the most recent waveform with this exact name, or None."""
        matches = [w for w in self.list() if w.name == name]
        return matches[-1] if matches else None

    def samples_b64(self, waveform_id: str) -> str:
        """The inline base64 codes of a ``dac_waveform`` entry."""
        return str(self.get(waveform_id).raw.get("samples_b64", ""))

    def download_recording(self, waveform_id: str) -> bytes:
        """Download a ``recording``'s raw 16-bit-LE sample blob (byte-for-byte)."""
        _, raw = self._api.request(
            "GET", f"/benchpod/waveforms/{waveform_id}/recording", parse_json=False
        )
        return raw if isinstance(raw, (bytes, bytearray)) else bytes(raw)

    def preview(self, waveform_id: str, *, mapping: str = "faithful", dac_path: str = "5v",
                points: int = 4096) -> Dict[str, Any]:
        """Server-downsampled, output-mapped preview of a recording (what the DAC will emit)."""
        _, data = self._api.request(
            "GET", f"/benchpod/waveforms/{waveform_id}/preview",
            query={"mapping": mapping, "dac_path": dac_path, "points": points},
        )
        return data or {}

    # -- write --------------------------------------------------------------

    def save_waveform(self, name: str, samples: Sequence[int], *, sample_rate_hz: float,
                      bits: int = 8, full_scale_v: Optional[float] = None) -> Waveform:
        """Save authored DAC codes as a ``dac_waveform`` entry."""
        if bits <= 8:
            blob = bytes(int(s) & 0xFF for s in samples)
        else:
            import struct
            blob = b"".join(struct.pack("<H", max(0, min(65535, int(s)))) for s in samples)
        body: Dict[str, Any] = {
            "name": name,
            "samples_b64": base64.b64encode(blob).decode("ascii"),
            "sample_rate_hz": sample_rate_hz,
            "bits": bits,
        }
        if full_scale_v is not None:
            body["full_scale_v"] = full_scale_v
        _, data = self._api.request("POST", "/benchpod/waveforms", json_body=body)
        return Waveform.from_json(data or {})

    def save_recording(self, name: str, samples16: bytes, *, sample_rate_hz: float,
                       full_scale_v: float) -> Waveform:
        """Save a raw 16-bit-LE ADC recording to S3 (metadata in the query string).

        ``samples16`` is the raw little-endian 16-bit blob; ``full_scale_v`` is the volts the
        top code (65535) represents (used later to map back to volts at replay).
        """
        if not samples16 or len(samples16) % 2 != 0:
            raise ValueError("samples16 must be a non-empty whole number of 16-bit samples")
        query = {
            "name": name,
            "sample_rate_hz": sample_rate_hz,
            "full_scale_v": full_scale_v,
            "sample_count": len(samples16) // 2,
        }
        qs = urllib.parse.urlencode(query)
        _, data = self._api.request(
            "POST", "/benchpod/waveforms/recording?" + qs,
            raw_body=bytes(samples16), content_type="application/octet-stream",
        )
        return Waveform.from_json(data or {})

    def save_segments(self, name: str, *, dac_path: str, segments: Sequence) -> Waveform:
        """Save a piecewise (``ramp``/``hold``/``step``) waveform spec."""
        _, data = self._api.request("POST", "/benchpod/waveforms/segments", json_body={
            "name": name, "dac_path": dac_path, "segments": normalize_segments(segments),
        })
        return Waveform.from_json(data or {})

    def rename(self, waveform_id: str, name: str) -> Waveform:
        _, data = self._api.request("PATCH", f"/benchpod/waveforms/{waveform_id}",
                                    json_body={"name": name})
        return Waveform.from_json(data or {})

    def delete(self, waveform_id: str) -> None:
        """Delete a waveform (also removes its S3 recording blob)."""
        self._api.request("DELETE", f"/benchpod/waveforms/{waveform_id}")
