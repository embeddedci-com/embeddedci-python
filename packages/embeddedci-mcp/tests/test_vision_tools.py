"""The vision_* tools against a stub AI Vision client (no board, no agent).

``test_vision_live`` at the end runs a short flow against a real agent (its simulator or a board)
when ``AIVISION_CONNECTION`` is set.
"""

from __future__ import annotations

import os

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ImageContent

from embeddedci.aivision import Box, Element, Image, NotFoundError, OCRResult, PressResult, TextBlock, WaitResult
from embeddedci_mcp.server import mcp
from embeddedci_mcp.vision import VISION

from conftest import call

RADIO = Element(kind="button", label="Radio", text="Radio", confidence=0.97, box=Box(60, 139, 562, 233))


class StubVision:
    url = "http://stub"

    def __init__(self) -> None:
        self.calls = []

    def status(self):
        return {"version": "t", "screen": {"calibrated": True, "calibration": {"screen_size": [1920, 720]}},
                "camera": {"locked": False}, "actuators": [{"id": "sim"}], "lease": None}

    def capabilities(self):
        return ["ocr", "find.text"]

    def frame(self, **kw):
        self.calls.append(("frame", kw))
        return Image(b"\xff\xd8img", "image/jpeg", 7)

    def zoom(self, region, **kw):
        self.calls.append(("zoom", region))
        return Image(b"\x89PNGzoom", "image/png", 8)

    def ocr(self, region=None, **kw):
        return OCRResult("Home\nRadio", [TextBlock("Home", Box(1, 2, 3, 4), 0.9, {"box": {"x": 1, "y": 2, "w": 3, "h": 4}})],
                         5, 100, False)

    def find(self, text=None, **kw):
        self.calls.append(("find", text, kw))
        return [RADIO] if text == "Radio" else []

    def wait_for(self, text, **kw):
        self.calls.append(("wait_for", text, kw))
        return WaitResult(text == "Radio", f'text "{text}" visible', 1200, 3, 9, [RADIO] if text == "Radio" else [])

    def press(self, text=None, **kw):
        self.calls.append(("press", text, kw))
        if text != "Radio":
            raise NotFoundError("not on the screen", status=404, detail={"visible_text": ["Home", "Radio"]})
        return PressResult(RADIO, (341.0, 255.5), True, WaitResult(True, 'text "FM" visible', 1, 1, 1, []), "e1")

    def close(self):
        pass


@pytest.fixture
def stub(monkeypatch):
    s = StubVision()
    monkeypatch.setattr(VISION, "client", s)
    yield s
    VISION.client = None


def test_status_warns_about_unlocked_camera(stub):
    r = call("vision_status")
    assert r["calibrated"] and r["screen_size"] == [1920, 720] and r["actuators"] == ["sim"]
    assert any("not locked" in w for w in r["warnings"])


def test_look_returns_an_image_block(stub):
    out = anyio.run(lambda: mcp.call_tool("vision_look", {}))
    blocks = out[0] if isinstance(out, tuple) else out
    img = [b for b in blocks if isinstance(b, ImageContent)]
    assert img and img[0].mimeType == "image/jpeg"
    anyio.run(lambda: mcp.call_tool("vision_look", {"region": [0, 0, 400, 100]}))
    assert ("zoom", [0, 0, 400, 100]) in stub.calls


def test_find_ocr_wait(stub):
    assert call("vision_find", text="Radio")["matches"][0]["center"] == [341.0, 255.5]
    assert call("vision_ocr")["lines"][0]["text"] == "Home"
    w = call("vision_wait_for", text="FM", timeout_s=2)
    assert w["met"] is False and w["elapsed_s"] == 1.2
    assert stub.calls[-1][2]["raise_on_timeout"] is False


def test_press_and_not_found(stub):
    r = call("vision_press", text="Radio", verify_text="FM")
    assert r["verified"] is True and r["tapped_at"] == [341.0, 255.5]
    assert stub.calls[-1][2]["verify"] == {"text": "FM"}
    with pytest.raises(ToolError, match="NotFoundError.*Visible text: Home, Radio"):
        call("vision_press", text="Nope")


def test_not_connected_names_vision_connect(monkeypatch):
    monkeypatch.delenv("AIVISION_CONNECTION", raising=False)
    VISION.client = None
    with pytest.raises(ToolError, match="vision_connect"):
        call("vision_find", text="Radio")


@pytest.mark.skipif(not os.environ.get("AIVISION_CONNECTION"), reason="no AI Vision board (AIVISION_CONNECTION)")
def test_vision_live():
    """Against the agent's simulator (home scene) or a board showing a 'Home' screen."""
    VISION.client = None
    try:
        assert call("vision_connect")["connected"]
        out = anyio.run(lambda: mcp.call_tool("vision_look", {"space": "camera"}))
        blocks = out[0] if isinstance(out, tuple) else out
        assert any(isinstance(b, ImageContent) for b in blocks)
        corners = [[312, 598], [2290, 640], [2276, 1392], [298, 1356]]
        call("vision_calibrate", corners=corners, screen_size=[1920, 720])
        assert call("vision_wait_for", text=os.environ.get("AIVISION_HOME_TEXT", "Home"), timeout_s=10)["met"]
    finally:
        call("vision_disconnect")


def test_cloud_boards_lists_the_organizations_boards(monkeypatch):
    from embeddedci_mcp import vision

    monkeypatch.setattr(vision, "list_boards", lambda: [
        {"id": "d1", "name": "bench-1", "online": True, "connection": "embeddedci:bench-1"},
        {"id": "d2", "name": "bench-2", "online": False},
    ])
    r = call("vision_cloud_boards")
    assert [(b["name"], b["online"], b["connection"]) for b in r["boards"]] == [
        ("bench-1", True, "embeddedci:bench-1"), ("bench-2", False, "embeddedci:bench-2")]


def test_cloud_boards_without_a_sign_in_says_how(monkeypatch):
    from embeddedci.aivision import AuthError
    from embeddedci_mcp import vision

    def no_login():
        raise AuthError("listing boards on embeddedci.com needs an API key")

    monkeypatch.setattr(vision, "list_boards", no_login)
    with pytest.raises(ToolError, match="benchpod login"):
        call("vision_cloud_boards")
