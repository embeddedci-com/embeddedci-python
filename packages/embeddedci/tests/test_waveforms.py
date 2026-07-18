"""Waveform library + ServerApi tests against a fake HTTP layer (no network)."""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional, Tuple

import pytest

from embeddedci.benchpod.errors import CloudAuthError
from embeddedci.benchpod.server_api import ServerApi, ServerApiError
from embeddedci.benchpod.waveforms import Waveform, WaveformLibrary


class FakeApi:
    """Records requests and replays canned responses keyed by (method, path-prefix)."""

    def __init__(self, responses: Dict[Tuple[str, str], Any]) -> None:
        self._responses = responses
        self.calls: List[dict] = []

    def request(self, method, path, *, query=None, json_body=None, raw_body=None,
                content_type=None, parse_json=True):
        self.calls.append({"method": method, "path": path, "query": query,
                           "json": json_body, "raw": raw_body})
        # Match the most specific (longest) registered prefix for this method.
        best = None
        for (m, prefix), resp in self._responses.items():
            if m == method and path.startswith(prefix):
                if best is None or len(prefix) > best[0]:
                    best = (len(prefix), resp)
        return 200, (best[1] if best else {})

    # WaveformLibrary/replay use these:
    def resolve_device_id(self, name):
        return "dev-123"

    def replay_start(self, payload):
        self.calls.append({"replay": payload})
        return {"status": "started", "samples": 8192}


def test_server_api_requires_credentials():
    with pytest.raises(CloudAuthError):
        ServerApi(api_base="https://x.test")  # no api_key, no token provider


def test_library_list_and_get():
    api = FakeApi({
        ("GET", "/benchpod/waveforms"): {"waveforms": [
            {"id": "w1", "name": "ramp", "kind": "recording", "sample_count": 8000,
             "sample_rate_hz": 400000, "full_scale_v": 4.096},
        ]},
        ("GET", "/benchpod/waveforms/w1"): {"id": "w1", "name": "ramp", "kind": "recording",
                                            "samples_b64": ""},
    })
    lib = WaveformLibrary(api)
    items = lib.list()
    assert len(items) == 1 and items[0].is_recording and items[0].sample_count == 8000
    assert lib.find("ramp").id == "w1"
    assert lib.get("w1").name == "ramp"


def test_save_recording_posts_raw_body_and_query():
    api = FakeApi({("POST", "/benchpod/waveforms/recording"): {
        "id": "rec1", "kind": "recording", "sample_count": 4}})
    lib = WaveformLibrary(api)
    blob = b"\x00\x00\xff\xff\x00\x80\x00\x40"  # 4 samples LE
    wf = lib.save_recording("cap", blob, sample_rate_hz=400000, full_scale_v=4.096)
    assert wf.id == "rec1" and wf.kind == "recording"
    call = api.calls[-1]
    assert call["raw"] == blob
    assert "name=cap" in call["path"] and "sample_count=4" in call["path"]


def test_save_recording_rejects_odd_bytes():
    lib = WaveformLibrary(FakeApi({}))
    with pytest.raises(ValueError):
        lib.save_recording("bad", b"\x00\x00\x00", sample_rate_hz=1, full_scale_v=1)


def test_save_waveform_encodes_codes():
    api = FakeApi({("POST", "/benchpod/waveforms"): {"id": "w9", "kind": "dac_waveform"}})
    lib = WaveformLibrary(api)
    lib.save_waveform("sq", [0, 255, 0, 255], sample_rate_hz=1000, bits=8)
    body = api.calls[-1]["json"]
    assert base64.b64decode(body["samples_b64"]) == bytes([0, 255, 0, 255])
    assert body["bits"] == 8


def test_save_segments_normalizes():
    from embeddedci.benchpod.replay import Segment

    api = FakeApi({("POST", "/benchpod/waveforms/segments"): {"id": "s1", "kind": "segments"}})
    lib = WaveformLibrary(api)
    lib.save_segments("ramp", dac_path="5v", segments=[Segment("ramp", 10, 0.0, 5.0)])
    body = api.calls[-1]["json"]
    assert body["dac_path"] == "5v"
    assert body["segments"][0] == {"shape": "ramp", "duration_ms": 10.0, "v_start": 0.0, "v_end": 5.0}


def test_waveform_from_json_defaults():
    wf = Waveform.from_json({"id": "x", "name": "n"})
    assert wf.kind == "dac_waveform" and wf.bits == 8 and not wf.is_recording
