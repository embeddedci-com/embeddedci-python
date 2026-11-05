"""embeddedci.aivision against a fake board (no hardware, no agent).

The fake answers with the shapes the real ai-vision-agent (v1 API) returns; the live tier in
``test_aivision_live.py`` runs the same calls against the simulator or a board.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Tuple

import pytest

from embeddedci import aivision
from embeddedci.aivision import (AIVision, AuthError, BusyError, NotCalibratedError, NotFoundError, TransportError,
                                 WaitTimeout, resolve_connection)
from embeddedci.aivision import client as client_mod

Handler = Callable[[Dict[str, Any]], Tuple[int, Any]]

BOX = {"x": 60, "y": 139, "w": 562, "h": 233, "space": "screen"}
RADIO = {"id": "m1", "class": "button", "label": "Radio", "text": "Radio", "confidence": 0.967, "box": BOX,
         "source": "ui", "score": 1}


class FakeBoard:
    """A tiny HTTP server: routes "METHOD /path" to a handler, records every request."""

    def __init__(self) -> None:
        self.routes: Dict[str, Handler] = {}
        self.requests: List[Dict[str, Any]] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _serve(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                path, _, query = self.path.partition("?")
                req = {"method": self.command, "path": path, "query": query, "headers": dict(self.headers),
                       "raw": raw, "json": None}
                if raw and self.headers.get("Content-Type", "").startswith("application/json"):
                    req["json"] = json.loads(raw)
                fake.requests.append(req)
                fn = fake.routes.get(f"{self.command} {path}")
                status, body = fn(req) if fn else (404, {"error": {"code": "not_found", "message": "no route"}})
                if isinstance(body, bytes):
                    data, ctype = body, "image/jpeg"
                else:
                    data, ctype = json.dumps(body).encode(), "application/json"
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                if ctype == "image/jpeg":
                    self.send_header("X-Frame-Id", "42")
                    self.send_header("X-Space", "screen")
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def addr(self) -> str:
        return f"127.0.0.1:{self.server.server_address[1]}"

    def on(self, route: str, status: int = 200, body: Any = None, fn: Handler = None) -> None:
        self.routes["%s /api/v1%s" % tuple(route.split(" ", 1))] = fn or (lambda req: (status, body))

    def last(self, path: str) -> Dict[str, Any]:
        return [r for r in self.requests if r["path"] == "/api/v1" + path][-1]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def board():
    b = FakeBoard()
    yield b
    b.close()


def test_resolve_connection(monkeypatch):
    assert resolve_connection("ai-vision-2-5dcec6.local") == "http://ai-vision-2-5dcec6.local"
    assert resolve_connection("10.1.0.165:8080") == "http://10.1.0.165:8080"
    assert resolve_connection("https://vision.example/") == "https://vision.example"
    monkeypatch.setenv("AIVISION_CONNECTION", "board.local")
    assert resolve_connection(None) == "http://board.local"
    monkeypatch.delenv("AIVISION_CONNECTION")
    with pytest.raises(TransportError):
        resolve_connection(None)


def test_errors_map_to_exceptions(board):
    board.on("POST /find", 409, {"error": {"code": "not_calibrated", "message": "calibrate first"}})
    board.on("GET /status", 401, {"error": {"code": "unauthorized", "message": "token"}})
    board.on("POST /press", 404, {"error": {"code": "not_found", "message": "nope",
                                            "detail": {"visible_text": ["Home", "Radio"]}}})
    v = AIVision(board.addr)
    with pytest.raises(NotCalibratedError):
        v.find("Radio")
    with pytest.raises(AuthError) as e:
        v.status()
    assert e.value.status == 401
    with pytest.raises(NotFoundError) as nf:
        v.press("Nope")
    assert nf.value.detail["visible_text"] == ["Home", "Radio"]


def test_unreachable_board_is_transport_error():
    with pytest.raises(TransportError):
        AIVision("127.0.0.1:1", timeout=1).status()


def test_token_and_lease_headers(board):
    board.on("POST /lease", body={"lease_id": "L1", "who": "t", "expires": "x", "ttl_s": 30})
    board.on("DELETE /lease/L1", body={"released": True})
    board.on("POST /screen/calibrate", body={"calibrated": True, "calibration": {
        "corners": [[1, 2], [3, 4], [5, 6], [7, 8]], "screen_size": [1920, 720], "method": "corners", "quality": 1}})
    with AIVision(board.addr, token="sekret", lease="t", lease_ttl_s=30) as v:
        cal = v.calibrate(corners=[(1, 2), (3, 4), (5, 6), (7, 8)], screen_size=(1920, 720))
        assert cal.screen_size == (1920, 720)
        h = board.last("/screen/calibrate")["headers"]
        assert h["Authorization"] == "Bearer sekret"
        assert h["X-Lease-Id"] == "L1"
    assert board.last("/lease/L1")["method"] == "DELETE"
    assert v.lease is None


def test_busy(board):
    board.on("POST /lease", 409, {"error": {"code": "busy", "message": "leased",
                                            "detail": {"who": "other", "expires": "x"}}})
    with pytest.raises(BusyError) as e:
        AIVision(board.addr, lease="me")
    assert e.value.detail["who"] == "other"


def test_find_and_ocr_results(board):
    board.on("POST /find", body={"found": True, "matches": [RADIO], "frame_id": 3})
    board.on("POST /ocr", body={"text": "Home\nRadio", "frame_id": 9, "ms": 277, "cached": False,
                                "blocks": [{"text": "Home", "confidence": 0.96, "box": BOX},
                                           {"text": "Radio", "confidence": 0.97, "box": BOX}]})
    v = AIVision(board.addr)
    m = v.find("Radio", match="contains", region=(0, 0, 1920, 400))
    assert m[0].kind == "button" and m[0].center == (341.0, 255.5)
    assert board.last("/find")["json"] == {"text": "Radio", "match": "contains", "region": [0, 0, 1920, 400]}
    o = v.ocr()
    assert o.lines == ["Home", "Radio"] and o.frame_id == 9


def test_wait_met_and_timeout(board):
    met = {"met": True, "condition": "text \"Home\" visible", "elapsed_ms": 1, "evaluations": 1, "frame_id": 5,
           "result": {"count": 1, "matches": [RADIO]}}
    board.on("POST /wait", body=met)
    v = AIVision(board.addr)
    r = v.wait_for("Home", timeout=5)
    assert r and r.match.label == "Radio"
    assert board.last("/wait")["json"] == {"condition": {"find": {"text": "Home"}}, "timeout_ms": 5000,
                                           "error_on_timeout": True}

    board.on("POST /wait", 408, {"error": {"code": "timeout", "message": "timed out", "detail": {
        "met": False, "condition": "c", "elapsed_ms": 1500, "evaluations": 9, "evidence": "e1",
        "result": {"count": 0, "matches": []}}}})
    with pytest.raises(WaitTimeout) as e:
        v.wait_for("FM", timeout=1.5)
    assert e.value.detail.evaluations == 9 and e.value.detail.evidence == "e1"

    board.on("POST /wait", body={**met, "met": False})
    assert not v.wait_until_gone("Home", timeout=1, raise_on_timeout=False)
    assert board.last("/wait")["json"]["condition"] == {"find": {"text": "Home", "absent": True}}


def test_long_wait_is_split(board, monkeypatch):
    monkeypatch.setattr(client_mod, "MAX_WAIT_REQUEST_S", 0.2)
    calls = []

    def wait(req):
        calls.append(req["json"])
        if len(calls) < 3:
            return 200, {"met": False, "elapsed_ms": 200, "evaluations": 2, "result": {}}
        return 200, {"met": True, "elapsed_ms": 10, "evaluations": 1, "result": {"matches": [RADIO]}}

    board.on("POST /wait", fn=wait)
    r = AIVision(board.addr).wait_for("Radio", timeout=10)
    assert r.met and r.elapsed_ms == 410 and r.evaluations == 5
    assert [c["timeout_ms"] for c in calls] == [200, 200, 200]
    assert all("error_on_timeout" not in c for c in calls)  # never the last request


def test_press_and_check(board):
    board.on("POST /press", body={"found": RADIO, "tapped_at": {"x": 341, "y": 255.5}, "verified": True,
                                  "verify": {"met": True, "condition": "c", "result": {"matches": []}},
                                  "evidence": "e2"})
    board.on("POST /check", body={"passed": False, "evidence": "e3", "evidence_zip": "/api/v1/evidence/e3.zip",
                                  "results": [{"name": "fm", "passed": True}, {"name": "clock", "passed": False}]})
    v = AIVision(board.addr)
    p = v.press("Radio", verify={"text": "FM"}, verify_timeout=3)
    assert p.tapped_at == (341.0, 255.5) and p.verified and p.found.label == "Radio"
    assert board.last("/press")["json"] == {"target": {"text": "Radio"}, "verify": {"text": "FM"},
                                            "verify_timeout_ms": 3000}
    c = v.check({"fm": {"text": "FM"}, "clock": {"text": "12:45"}})
    assert not c and c.failed == ["clock"]


def test_frame_and_reference_upload(board, tmp_path):
    board.on("GET /frame.jpg", body=b"\xff\xd8jpeg")
    board.on("POST /references", body={"name": "home", "kind": "screen"})
    v = AIVision(board.addr)
    img = v.frame(space="screen", overlay="text")
    assert img.frame_id == 42 and img.data == b"\xff\xd8jpeg"
    assert board.last("/frame.jpg")["query"] == "space=screen&overlay=text"
    assert img.save(tmp_path / "f.jpg").read_bytes() == b"\xff\xd8jpeg"

    v.add_reference("home", image=b"\x89PNG....", mask=[{"name": "clock", "x": 1, "y": 2, "w": 3, "h": 4}])
    req = board.last("/references")
    assert req["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="image"; filename="image.png"' in req["raw"] and b'"clock"' in req["raw"]


def test_public_names_exist():
    for name in aivision.__all__:
        assert hasattr(aivision, name), name


def test_plugin_saves_screen_on_failure(pytester, board):
    board.on("GET /screen", body={"calibrated": True, "calibration": {"corners": [], "screen_size": [1920, 720]}})
    board.on("GET /frame.jpg", body=b"\xff\xd8shot")
    pytester.makepyfile("""
        def test_fails(aivision):
            assert False
        def test_passes(aivision):
            pass
    """)
    art = pytester.path / "art"
    res = pytester.runpytest(f"--aivision={board.addr}", "--aivision-lease=", f"--aivision-artifacts={art}", "-p",
                             "no:cacheprovider")
    res.assert_outcomes(passed=1, failed=1)
    assert (art / "test_plugin_saves_screen_on_failure.py_test_fails.jpg").read_bytes() == b"\xff\xd8shot"
    res.stdout.fnmatch_lines(["*screen at failure:*"])


def test_plugin_skips_without_board(pytester, monkeypatch):
    monkeypatch.delenv("AIVISION_CONNECTION", raising=False)
    pytester.makepyfile("def test_x(aivision):\n    pass\n")
    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(skipped=1)
