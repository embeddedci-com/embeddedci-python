"""cloud_runs_list, cloud_run_get, cloud_run_compare and cloud_run_trace against a fake runs API
(the SDK tests' fake, urlopen patched: no network)."""

from __future__ import annotations

import importlib.util
import urllib.request
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from conftest import call

pytest.importorskip("embeddedci.benchpod.runs.api", reason="an SDK without the runs API")

_FAKE = Path(__file__).resolve().parents[2] / "embeddedci" / "tests" / "runs_api_fake.py"
_spec = importlib.util.spec_from_file_location("runs_api_fake_for_mcp", _FAKE)
fake_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_mod)  # type: ignore[union-attr]


@pytest.fixture
def server(monkeypatch):
    fake = fake_mod.FakeRunsServer()
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    monkeypatch.setenv("BENCHPOD_API_KEY", "eci_test")
    monkeypatch.delenv("BENCHPOD_API_BASE", raising=False)
    return fake


def test_runs_list(server):
    out = call("cloud_runs_list", suite="hil", pod="benchpod-abc123", limit=5)
    assert out["auth"] == "api_key" and out["runs"][0]["id"] == "r-2"
    assert out["next_before"] and out["org_pods"] == 2
    assert server.requests[0]["query"] == {"suite": "hil", "device": "benchpod-abc123", "limit": "5"}
    assert server.requests[0]["auth"] == "ApiKey eci_test"


def test_run_get(server):
    out = call("cloud_run_get", run_id="r-2")
    s = out["summary"]
    assert s["outcome"] == "failed" and s["failed_cases"][0]["message"] == "ValueError: bad CRC: 6f72"
    assert s["measurements_out_of_limits"][0]["name"] == "settle_ms"
    assert out["run"] is None and [f["name"] for f in out["files"]][0] == "junit.xml"
    assert call("cloud_run_get", run_id="r-2", full=True)["run"]["results"]["cases"]


def test_run_compare(server):
    out = call("cloud_run_compare", run_id="r-2")
    assert out["found"] and out["diff"]["cases"]["newly_failing"] == ["tests/test_can.py::test_ack"]
    assert server.requests[-1]["query"] == {"with": "previous"}
    none = call("cloud_run_compare", run_id="r-2", compare_with="baseline:none")
    assert none["found"] is False and "no earlier" in none["message"]
    assert server.requests[-1]["query"] == {"with": "baseline:none"}


def test_run_trace(server):
    out = call("cloud_run_trace", run_id="r-2", case_id="test_ack", errors_only=True)
    assert out["case"] == "tests/test_can.py::test_ack" and out["total"] == 3
    assert [ln["cmd"] for ln in out["lines"]] == ["can_write"] and out["lines"][0]["ok"] is False
    with pytest.raises(ToolError, match="no trace was uploaded"):
        call("cloud_run_trace", run_id="r-2", case_id="test_ok")


def test_unknown_run(server):
    with pytest.raises(ToolError, match="run not found"):
        call("cloud_run_get", run_id="nope")
