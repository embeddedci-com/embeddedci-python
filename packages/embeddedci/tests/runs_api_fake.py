"""A fake embeddedci runs API for urllib.request.urlopen (shared by the SDK and MCP tests)."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from typing import Any, Dict, List

TRACE = "\n".join(json.dumps(x) for x in [
    {"t": 1.0, "op": "command", "cmd": "can_open", "req": {"cmd": "can_open"}, "ms": 2.0, "ok": True,
     "resp": {"blob": "z" * 2000}},
    {"t": 1.5, "op": "command", "cmd": "can_write", "req": {"cmd": "can_write"}, "ms": 3.0, "ok": False,
     "error": "BP0412 no ack"},
    {"t": 2.0, "op": "command", "cmd": "can_write", "ms": 3.2, "ok": True},
]) + "\n"

RUN = {
    "schema_version": 1, "id": "r-2", "run_id": "r-2", "url": "https://example.test/run/r-2",
    "build_id": "b-1", "key": {"suite": "hil", "dut": "process-node"}, "trigger": "github",
    "started": "2026-11-18T14:02:11.123Z",
    "inputs": {"pod": {"name": "benchpod-abc123", "board": "analog-v3", "firmware": "3.10.12",
                       "gateware": 58, "connection": "lan"},
               "tests": {"repo": "o/r", "ref": "main", "sha": "abc", "dirty": False}},
    "health": {"valid": True, "reasons": []},
    "results": {"outcome": "failed", "duration_ms": 1000,
                "counts": {"passed": 1, "failed": 1, "error": 0, "skipped": 0, "xfailed": 0, "xpassed": 0},
                "cases": [
                    {"id": "tests/test_can.py::test_ack", "outcome": "failed",
                     "message": "ValueError: bad CRC: 6f72\n\n    def test_ack(bus):", "trace": "trace/t-ack.ndjson",
                     "measurements": [{"name": "settle_ms", "value": 1200, "unit": "ms", "min": None, "max": 1000,
                                       "outcome": "failed"}]},
                    {"id": "tests/test_can.py::test_ok", "outcome": "passed", "trace": "",
                     "measurements": [{"name": "v", "value": 1, "unit": "V", "min": 0, "max": 2,
                                       "outcome": "passed"}]}]},
    "files": [{"name": "junit.xml", "size_bytes": 10, "expired": False},
              {"name": "trace/t-ack.ndjson", "size_bytes": 300, "expired": False}],
}
COMPARE = {"found": True, "a": "r-1", "b": "r-2", "comparable": True, "reasons": [], "flags": [],
           "inputs_changed": [{"field": "pod.firmware", "a": "3.10.11", "b": "3.10.12"}],
           "cases": {"newly_failing": ["tests/test_can.py::test_ack"], "fixed": [], "still_failing": [],
                     "new": [], "removed": [], "newly_skipped": []},
           "measurements": [], "slower": [], "calls_slower": []}


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeRunsServer:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []

    def urlopen(self, req: Any, timeout: Any = None, **_k: Any) -> _Resp:
        url = urllib.parse.urlsplit(req.full_url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        self.requests.append({"method": req.get_method(), "path": url.path, "query": q,
                              "auth": req.get_header("Authorization"), "base": f"{url.scheme}://{url.netloc}"})
        if url.path == "/api/runs":
            return _Resp(json.dumps({"runs": [{"id": "r-2", "suite": "hil", "outcome": "failed"}],
                                     "next_before": "2026-11-18T14:02:11Z", "org_pods": 2}).encode())
        if url.path == "/api/runs/r-2":
            return _Resp(json.dumps(RUN).encode())
        if url.path == "/api/runs/r-2/compare":
            if q.get("with") == "baseline:none":
                return _Resp(json.dumps({"found": False, "a": None, "b": "r-2",
                                         "message": "no earlier valid run"}).encode())
            return _Resp(json.dumps(COMPARE).encode())
        if url.path == "/api/runs/r-2/files":
            if q.get("name") == "trace/t-ack.ndjson":
                return _Resp(TRACE.encode())
            if "name" not in q:
                return _Resp(json.dumps({"files": RUN["files"]}).encode())
        raise urllib.error.HTTPError(req.full_url, 404, "no", {}, io.BytesIO(b'{"error":"run not found"}'))
