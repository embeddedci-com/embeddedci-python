"""la_timing measures the last logic capture (no hardware, no new capture)."""

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from embeddedci.benchpod.results import LaCapture
from embeddedci_mcp.session import SESSION

from conftest import call

LA1 = [0, 0, 1, 1, 1, 0, 0, 1, 1, 0]
LA2 = [0, 0, 0, 0, 1, 1, 0, 0, 0, 1]


def _keep_capture():
    words = [a | (b << 1) for a, b in zip(LA1, LA2)]
    SESSION.last_la = LaCapture(words=words, sample_rate_hz=1000.0)


def test_la_timing_needs_a_capture(connected):
    with pytest.raises(ToolError, match="capture_la"):
        call("la_timing", la=1)


def test_la_timing_edges_pulses_and_delay(connected):
    _keep_capture()
    res = call("la_timing", la=1, to_la=2)
    assert res["edge_times"] == pytest.approx([0.002, 0.007])
    assert res["frequency_hz"] == pytest.approx(200.0)
    assert res["duty_cycle"] == 0.5
    assert res["high_pulses"]["count"] == 2 and res["high_pulses"]["max"] == pytest.approx(0.003)
    assert res["delay"] == pytest.approx(0.002) and res["resolution"] == pytest.approx(0.001)
    assert "fpga" not in str(connected.requests)  # nothing was sent to the pod


def test_la_timing_after_and_truncation(connected):
    _keep_capture()
    res = call("la_timing", la=1, edge="both", after=0.004, max_edges=1)
    assert res["edge_times"] == pytest.approx([0.005]) and res["truncated"] is True
    assert res["delay"] is None
