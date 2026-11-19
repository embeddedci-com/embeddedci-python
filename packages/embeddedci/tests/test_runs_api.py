"""embeddedci.benchpod.runs.api against a fake runs API (urlopen faked, no network)."""

from __future__ import annotations

import urllib.request

import pytest

from embeddedci.benchpod.runs import api
from embeddedci.benchpod.server_api import ServerApi, ServerApiError

from runs_api_fake import RUN, FakeRunsServer


@pytest.fixture
def fake(monkeypatch):
    server = FakeRunsServer()
    monkeypatch.setattr(urllib.request, "urlopen", server.urlopen)
    monkeypatch.setenv("BENCHPOD_API_KEY", "eci_test")
    monkeypatch.setenv("BENCHPOD_API_BASE", "https://staging.example")
    return server


def test_list_runs_filters_and_auth(fake):
    data = api.list_runs(suite="hil", dut="process-node", build="b-1", pod="benchpod-abc123", ref="main",
                         limit=500)
    assert data["runs"][0]["id"] == "r-2" and data["next_before"] and data["org_pods"] == 2
    req = fake.requests[0]
    assert req["base"] == "https://staging.example" and req["auth"] == "ApiKey eci_test"
    assert req["query"] == {"suite": "hil", "dut": "process-node", "build": "b-1",
                            "device": "benchpod-abc123", "ref": "main", "limit": "200"}


def test_get_compare_and_files(fake):
    assert api.get_run("r-2")["id"] == "r-2"
    assert api.compare_runs("r-2")["cases"]["newly_failing"] == ["tests/test_can.py::test_ack"]
    assert fake.requests[-1]["query"] == {"with": "previous"}
    assert api.compare_runs("r-2", "baseline:none")["found"] is False
    assert api.get_run_file("r-2", "trace/t-ack.ndjson").startswith(b"{")
    assert [f["name"] for f in api.RunsApi().list_run_files("r-2")] == ["junit.xml", "trace/t-ack.ndjson"]
    with pytest.raises(ServerApiError, match="404.*run not found"):
        api.get_run("nope")


def test_summarize_run():
    s = api.summarize_run(RUN)
    assert s["outcome"] == "failed" and s["build_id"] == "b-1" and s["pod"]["firmware"] == "3.10.12"
    assert s["failed_cases"] == [{"id": "tests/test_can.py::test_ack", "outcome": "failed",
                                  "message": "ValueError: bad CRC: 6f72", "trace": True}]
    assert s["measurements_out_of_limits"] == [{"case": "tests/test_can.py::test_ack", "name": "settle_ms",
                                                "value": 1200, "unit": "ms", "min": None, "max": 1000}]


def test_case_trace(fake):
    t = api.case_trace("r-2", "test_ack")
    assert t["case"] == "tests/test_can.py::test_ack" and t["total"] == 3 and t["errors"] == 1
    assert t["lines"][0]["resp"].startswith("<") and "bytes" in t["lines"][0]["resp"]   # shortened
    only = api.case_trace(RUN, "tests/test_can.py::test_ack", errors_only=True)
    assert [ln["error"] for ln in only["lines"]] == ["BP0412 no ack"] and not only["truncated"]
    capped = api.case_trace(RUN, "test_ack", limit=1)
    assert capped["shown"] == 1 and capped["truncated"]
    with pytest.raises(LookupError, match="no trace was uploaded"):
        api.case_trace(RUN, "test_ok")
    with pytest.raises(LookupError, match="no case"):
        api.case_trace(RUN, "test_missing")


def test_find_case_ambiguous():
    run = {"results": {"cases": [{"id": "a::test_x"}, {"id": "b::test_x"}]}}
    with pytest.raises(LookupError, match="matches 2 cases"):
        api.find_case(run, "test_x")


def test_runs_api_takes_a_server():
    server = ServerApi(api_base="https://x", token_provider=lambda: "t")
    assert api.RunsApi(server).server is server
