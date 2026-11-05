"""embeddedci.aivision against a real agent: the simulator or a board.

Skipped unless ``--aivision`` / ``AIVISION_CONNECTION`` is set. With the agent's simulator showing
its home scene (ai-vision-agent: ``make sim``, scene ``testdata/scenes/cam-home.png``)::

    AIVISION_CONNECTION=127.0.0.1:8080 pytest tests/test_aivision_live.py

The simulator's scene corners are fixed, so the tests calibrate with them; on a board set
``AIVISION_CORNERS`` (``x,y x,y x,y x,y``) and ``AIVISION_SCREEN`` (``1920x720``) instead, and put
the device on a screen that shows the text in ``AIVISION_HOME_TEXT`` (default ``Home``).
"""

from __future__ import annotations

import os

import pytest

from embeddedci.aivision import NotFoundError, WaitTimeout

SIM_CORNERS = [(312, 598), (2290, 640), (2276, 1392), (298, 1356)]
HOME = os.environ.get("AIVISION_HOME_TEXT", "Home")


def _corners():
    raw = os.environ.get("AIVISION_CORNERS")
    if not raw:
        return SIM_CORNERS
    return [tuple(float(n) for n in p.split(",")) for p in raw.split()]


def _screen():
    w, h = os.environ.get("AIVISION_SCREEN", "1920x720").lower().split("x")
    return int(w), int(h)


@pytest.fixture(scope="module")
def v(aivision):
    aivision.calibrate(corners=_corners(), screen_size=_screen())
    return aivision


def test_status_and_capabilities(v):
    assert v.status()["device"] == "ai-vision-2"
    assert "ocr" in v.capabilities()


def test_read_and_find(v):
    assert HOME in v.ocr().lines
    assert v.find(HOME)[0].box.space == "screen"
    assert v.wait_for(HOME, timeout=10).met


def test_absent_text_times_out(v):
    with pytest.raises(WaitTimeout) as e:
        v.wait_for("no such text 7f3a", timeout=1)
    assert e.value.detail.met is False


def test_compare_with_own_reference(v):
    v.add_reference("sdk-live-test", overwrite=True)
    try:
        assert v.compare("sdk-live-test").score > 0.9
    finally:
        v.delete_reference("sdk-live-test")


def test_frame_is_jpeg(v):
    img = v.frame(space="screen")
    assert img.data[:2] == b"\xff\xd8" and img.frame_id


def test_press_on_simulated_actuator(v):
    v.add_actuator("sdk-live-sim", "simulated")
    try:
        with pytest.raises(NotFoundError):
            v.press("no such text 7f3a", actuator="sdk-live-sim")
        r = v.press(HOME, actuator="sdk-live-sim")
        assert r.tapped_at is not None
    finally:
        v.remove_actuator("sdk-live-sim")
