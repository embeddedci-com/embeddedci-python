"""Test run recording: the run folder formats, the transport recorder, the pytest plugin's run
folder and handover, and the ``measure`` fixture. No hardware: every pod is a fake transport."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import BenchPod
from embeddedci.benchpod.runs import (
    Case,
    Measurement,
    RunRecording,
    active_run,
    case_slug,
    junit_xml,
    parse_drift,
    redact,
    run_outcome,
    set_active_run,
)
from embeddedci.benchpod.runs import active as active_mod
from embeddedci.benchpod.runs import provenance
from embeddedci.benchpod.runs.schema import CallStats, cap_text, sha256_of_json
from embeddedci.benchpod.transport.base import Transport
from runs_fake import CALIBRATION, FakePod

RUN_ENV = ("EMBEDDEDCI_RUN_DIR", "EMBEDDEDCI_RUN_STEP", "EMBEDDEDCI_RUN_ID", "EMBEDDEDCI_RUN_T0",
           "EMBEDDEDCI_KEEP_TRACES")


@pytest.fixture(autouse=True)
def _no_run_env(monkeypatch):
    for name in RUN_ENV:
        monkeypatch.delenv(name, raising=False)
    yield
    set_active_run(None)


def _lines(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# -- schema ----------------------------------------------------------------------------------------

def test_case_slug_unchanged_ids_keep_their_name():
    assert case_slug("boot_ms") == "boot_ms"
    assert case_slug("a-b.c_d") == "a-b.c_d"


def test_case_slug_changed_or_cut_ids_get_a_hash():
    import hashlib

    cid = "tests/test_can_hil.py::test_ack"
    h = hashlib.sha1(cid.encode()).hexdigest()[:8]
    assert case_slug(cid) == f"tests_test_can_hil.py__test_ack-{h}"
    long_id = "a" * 300
    slug = case_slug(long_id)
    assert slug.startswith("a" * 120 + "-") and len(slug) == 129


def test_redact_any_depth_case_insensitive():
    data = {"cmd": "wifi_set", "SSID": "lab", "Password": "hunter2",
            "nested": [{"api_key": "eci_abc"}, {"Authorization": "Bearer x"}],
            "cfg": {"psk": "s", "wifi_pass": "p", "Token": "t", "secret": "z", "passwd": "q",
                    "apikey": "k"},
            "key": "short", "other": {"key": "0123456789abcdefXYZ"}}
    out = redact(data)
    assert out["SSID"] == "lab" and out["Password"] == "<redacted>"
    assert out["nested"] == [{"api_key": "<redacted>"}, {"Authorization": "<redacted>"}]
    assert set(out["cfg"].values()) == {"<redacted>"}
    assert out["key"] == "short"                       # 16 chars or fewer: a key id, kept
    assert out["other"]["key"] == "<redacted>"
    assert data["Password"] == "hunter2"               # the input is not changed


def test_cap_text_head_and_tail():
    text = "a" * 100 + "b" * 100
    head = cap_text(text, 120)
    tail = cap_text(text, 120, keep="tail")
    assert len(head.encode()) <= 120 and head.startswith("aaa") and "bytes cut" in head
    assert len(tail.encode()) <= 120 and tail.endswith("bbb") and "bytes cut" in tail
    assert cap_text("short", 120) == "short"


@pytest.mark.parametrize("given,want", [("20%", 0.2), (" 15 % ", 0.15), (0.2, 0.2), (0, 0.0),
                                        (None, None)])
def test_parse_drift(given, want):
    got = parse_drift(given)
    assert got == pytest.approx(want) if want is not None else got is None


@pytest.mark.parametrize("bad", ["20", "abc%", True, -0.1, float("nan"), [1]])
def test_parse_drift_rejects(bad):
    with pytest.raises((TypeError, ValueError)):
        parse_drift(bad)


def test_measurement_outcomes_and_numbers():
    assert Measurement.make("a", 5).outcome == "recorded"
    assert Measurement.make("a", 5, drift="10%").outcome == "recorded"
    assert Measurement.make("a", 5, min=1, max=10).outcome == "passed"
    assert Measurement.make("a", 11, max=10).outcome == "failed"
    assert Measurement.make("a", 0, min=1).outcome == "failed"
    m = Measurement.make("settle_ms", 840, unit="ms", max=1000, drift="20%")
    assert m.to_dict() == {"name": "settle_ms", "value": 840, "unit": "ms", "min": None, "max": 1000,
                           "drift": 0.2, "outcome": "passed", "source": "measure"}
    for bad in (True, float("nan"), float("inf"), "1", None):
        with pytest.raises((TypeError, ValueError)):
            Measurement.make("a", bad)
    with pytest.raises(ValueError):
        Measurement.make("a", 1, min=5, max=1)
    with pytest.raises(ValueError):
        Measurement.make("", 1)


def test_run_outcome_rules():
    def cases(*outcomes):
        return [Case(id=str(i), outcome=o) for i, o in enumerate(outcomes)]

    assert run_outcome(cases("passed", "skipped", "xfailed", "xpassed")) == "passed"
    assert run_outcome(cases("passed", "failed")) == "failed"
    assert run_outcome(cases("failed", "error")) == "error"
    assert run_outcome(cases("passed"), valid=False) == "invalid"
    assert run_outcome(cases("passed"), step_failed=True) == "failed"
    assert run_outcome([]) == "passed"


def test_call_stats_percentiles():
    s = CallStats()
    for ms in range(1, 101):
        s.add(float(ms), ok=ms != 50)
    d = s.to_dict()
    assert d == {"n": 100, "p50_ms": 50.0, "p95_ms": 95.0, "max_ms": 100.0, "errors": 1}
    assert CallStats().to_dict() == {"n": 0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0, "errors": 0}


def test_junit_xml():
    cases = [
        Case(id="tests/test_a.py::test_ok", duration_ms=12,
             measurements=[Measurement.make("v", 1.5, unit="V")]),
        Case(id="tests/test_a.py::TestX::test_bad[1]", outcome="failed", message="assert 1 == 2"),
        Case(id="tests/test_a.py::test_err", outcome="error", message="fixture broke"),
        Case(id="tests/test_a.py::test_skip", outcome="skipped", skip_reason="no pod"),
        Case(id="tests/test_a.py::test_xf", outcome="xfailed", skip_reason="known"),
        Case(id="tests/test_a.py::test_log", outcome="failed", message="x", log="printed\x01"),
    ]
    root = ET.fromstring(junit_xml("hil", cases, duration_ms=1500))
    suite = root.find("testsuite")
    assert suite.get("name") == "hil" and suite.get("tests") == "6"
    assert (suite.get("failures"), suite.get("errors"), suite.get("skipped")) == ("2", "1", "2")
    tcs = suite.findall("testcase")
    assert tcs[0].get("classname") == "tests.test_a" and tcs[0].get("name") == "test_ok"
    assert tcs[0].find("properties/property").attrib == {"name": "v", "value": "1.5"}
    assert tcs[1].get("classname") == "tests.test_a.TestX" and tcs[1].get("name") == "test_bad[1]"
    assert tcs[1].find("failure").text == "assert 1 == 2"
    assert tcs[2].find("error") is not None
    assert tcs[3].find("skipped").get("message") == "no pod"
    assert tcs[5].find("system-out").text == "printed?"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    from embeddedci.benchpod.runs.schema import write_json_atomic

    write_json_atomic(tmp_path / "run.json", {"a": 1})
    write_json_atomic(tmp_path / "run.json", {"a": 2})
    assert json.loads((tmp_path / "run.json").read_text()) == {"a": 2}
    assert [p.name for p in tmp_path.iterdir()] == ["run.json"]


def test_provenance_helpers(tmp_path):
    assert provenance.repo_slug("git@github.com:embeddedci-com/examples.git") == "embeddedci-com/examples"
    assert provenance.repo_slug("https://user:tok@github.com/o/r.git") == "o/r"
    assert provenance.repo_slug("nonsense") == ""
    assert provenance.board_label({"board": "stm32h563", "board_rev": "v3", "analog": False}) == "digital-v3"
    assert provenance.board_label({"board": "stm32h563", "board_rev": "unknown"}) == "stm32h563"
    assert provenance.board_label({"board_tag": "analog-v3r2"}) == "analog-v3r2"
    fw = tmp_path / "app.elf"
    fw.write_bytes(b"\x7fELF" + b"\0" * 12)
    info = provenance.firmware_info(str(fw))
    assert info["file"] == "app.elf" and info["size"] == 16 and len(info["sha256"]) == 64
    assert provenance.firmware_info(None) == {}
    tools = provenance.tools_info("8.0")
    assert set(tools) == {"cli", "sdk", "python", "pytest", "os"} and tools["pytest"] == "8.0"
    assert provenance.git_info(str(tmp_path)) == {"repo": "", "ref": "", "sha": "", "dirty": False}


# -- recorder --------------------------------------------------------------------------------------

def test_recorder_traces_calls_per_case(tmp_path):
    run = RunRecording(tmp_path / "run")
    pod = FakePod()
    run.attach(pod)
    assert isinstance(pod, FakePod) and isinstance(pod, Transport)
    run.begin_case("tests/t.py::test_a")
    pod.command({"cmd": "can_write", "id": 1, "password": "hunter2"})
    pod.command({"cmd": "can_write", "id": 2})
    with pytest.raises(RuntimeError):
        pod.command({"cmd": "boom"})
    pod.status()                                     # status sends command inside: recorded once
    assert pod.samples({"cmd": "la_capture", "n": 5}) == [0, 1, 2, 3, 4]
    assert [c["data"] for c in pod.stream_chunks({"cmd": "capture_dual"})] == [[1, 2, 3], [4, 5]]
    pod.target_power(1, True)
    pod.command_sequence([{"cmd": "ota_begin"}, {"cmd": "ota_end"}])
    link = pod.dap_start(11, 12)
    link.write(b"abcd")
    assert link.read(100) == b"x" * 10
    link.close()
    trace, calls = run.end_case("tests/t.py::test_a", "failed")
    run.finish()

    assert trace.startswith("trace/tests_t.py__test_a-") and trace.endswith(".ndjson")
    lines = _lines(tmp_path / "run" / trace)
    ops = [(ln["op"], ln.get("cmd")) for ln in lines]
    assert ops == [("command", "can_write"), ("command", "can_write"), ("command", "boom"),
                   ("status", "status"), ("samples", "la_capture"), ("stream", "capture_dual"),
                   ("power", "target_power"), ("sequence", "ota_begin"), ("raw", "dap_start")]
    first = lines[0]
    assert first["req"]["password"] == "<redacted>" and first["ok"] is True
    assert first["resp"]["echo"]["password"] == "<redacted>"
    assert isinstance(first["t"], float) and first["ms"] >= 0
    assert lines[2]["ok"] is False and "BP0412" in lines[2]["error"]
    assert lines[4]["n"] == 5 and lines[4]["resp"] == [0, 1, 2, 3, 4]
    assert lines[5]["n"] == 5 and lines[5]["chunks"] == 2
    raw = lines[8]
    assert raw["mode"] == "dap" and raw["bytes_out"] == 4 and raw["bytes_in"] == 10 and raw["ok"] is True
    assert calls["can_write"].to_dict()["n"] == 2
    assert calls["boom"].to_dict()["errors"] == 1
    assert calls["status"].to_dict()["n"] == 1
    assert "command" not in calls


def test_recorder_offloads_big_payloads(tmp_path):
    run = RunRecording(tmp_path / "run")
    pod = FakePod()
    run.attach(pod)
    run.begin_case("c")
    pod.command({"cmd": "big"})
    pod.samples({"cmd": "la_capture", "n": 5000})
    trace, _ = run.end_case("c", "passed")
    lines = _lines(tmp_path / "run" / trace)
    assert "resp" not in lines[0] and lines[0]["att"].startswith("att/0001-big.json")
    assert lines[1]["att"].startswith("att/0002-la_capture.json") and lines[1]["n"] == 5000
    att = tmp_path / "run" / lines[0]["att"]
    data = att.read_bytes()
    if att.name.endswith(".zst"):
        import zstandard

        data = zstandard.ZstdDecompressor().decompress(data)
    assert json.loads(data)["resp"]["blob"] == "y" * 6000


def test_recorder_zstd_is_optional(tmp_path, monkeypatch):
    monkeypatch.setattr(active_mod, "_zstd", None)
    run = RunRecording(tmp_path / "run")
    pod = FakePod()
    run.attach(pod)
    run.begin_case("c")
    pod.command({"cmd": "big"})
    trace, _ = run.end_case("c", "passed")
    att = _lines(tmp_path / "run" / trace)[0]["att"]
    assert att == "att/0001-big.json"
    assert json.loads((tmp_path / "run" / att).read_text())["resp"]["blob"] == "y" * 6000


def test_keep_failed_drops_passed_traces_but_keeps_calls(tmp_path):
    run = RunRecording(tmp_path / "run", keep="failed")
    pod = FakePod()
    run.attach(pod)
    run.begin_case("ok")
    pod.command({"cmd": "big"})
    trace_ok, calls_ok = run.end_case("ok", "passed")
    run.begin_case("bad")
    pod.command({"cmd": "x"})
    trace_bad, _ = run.end_case("bad", "failed")
    assert trace_ok == "" and calls_ok["big"].to_dict()["n"] == 1
    assert trace_bad and (tmp_path / "run" / trace_bad).exists()
    assert not list((tmp_path / "run" / "att").iterdir())       # the passed case's att went too


def test_keep_none_writes_nothing(tmp_path):
    run = RunRecording(tmp_path / "run", keep="none")
    pod = FakePod()
    run.attach(pod)
    run.begin_case("c")
    pod.command({"cmd": "big"})
    trace, calls = run.end_case("c", "failed")
    assert trace == "" and calls["big"].to_dict()["n"] == 1
    assert not (tmp_path / "run" / "trace").exists() and not (tmp_path / "run" / "att").exists()


def test_trace_cap_truncates_lines(tmp_path):
    run = RunRecording(tmp_path / "run", trace_cap=300)
    pod = FakePod()
    run.attach(pod)
    run.begin_case("c")
    for i in range(10):
        pod.command({"cmd": "can_write", "id": i})
    trace, calls = run.end_case("c", "passed")
    lines = _lines(tmp_path / "run" / trace)
    assert run.health["trace_truncated"] is True
    assert "req" in lines[0]
    assert set(lines[-1]) == {"t", "op", "cmd", "ms", "ok"}
    assert calls["can_write"].to_dict()["n"] == 10


def test_inactive_run_passes_calls_straight_through(tmp_path):
    run = RunRecording(tmp_path / "run")
    pod = FakePod()
    rec = run.attach(pod)
    run.begin_case("c")
    run.finish()
    pod.command({"cmd": "late"})
    assert not (tmp_path / "run" / "trace").exists()
    assert pod.requests[-1] == {"cmd": "late"}
    rec.uninstall()
    assert "command" not in vars(pod)


def test_busy_retries_and_session_calls(tmp_path):
    run = RunRecording(tmp_path / "run")
    pod = FakePod()
    run.attach(pod)
    pod.command({"cmd": "busy_twice"})                # outside any case
    assert run.health["busy_retries"] == 2
    assert run.session_calls()["busy_twice"].to_dict()["n"] == 1
    assert (tmp_path / "run" / "trace" / "_session.ndjson").exists()


def test_uart_proxy_link_and_failed_handshake(tmp_path):
    class Refusing(FakePod):
        def dap_start(self, *a, **k):
            raise RuntimeError("BP0009 no LA voltage")

    run = RunRecording(tmp_path / "run")
    pod = Refusing()
    run.attach(pod)
    run.begin_case("c")
    with pod.uart_proxy_start(3, 4, 115200) as link:
        link.write(b"hi")
        assert link.closed is False                   # other attributes reach the real link
    with pytest.raises(RuntimeError):
        pod.dap_start(11, 12)
    trace, calls = run.end_case("c", "passed")
    lines = _lines(tmp_path / "run" / trace)
    assert lines[0]["op"] == "raw" and lines[0]["mode"] == "uart"
    assert lines[0]["req"]["baud"] == 115200 and lines[0]["bytes_out"] == 2
    assert lines[1]["mode"] == "dap" and lines[1]["ok"] is False and "BP0009" in lines[1]["error"]
    assert calls["dap_start"].to_dict()["errors"] == 1


def test_benchpod_attaches_to_the_active_run(tmp_path):
    run = RunRecording(tmp_path / "run")
    set_active_run(run)
    assert active_run() is run
    run.begin_case("tests/t.py::test_pod")
    pod = FakePod()
    bp = BenchPod(transport=pod, lease=False, api_key="", api_base="")
    bp.command({"cmd": "can_write"})
    trace, calls = run.end_case("tests/t.py::test_pod", "passed")
    run.finish()
    assert active_run() is None
    assert "can_write" in calls
    block = run.pod()
    assert block == {"name": "benchpod-abc123", "board": "analog-v3", "firmware": "3.10.11",
                     "gateware": 58, "features_rev": 0,
                     "calibration_sha256": sha256_of_json(CALIBRATION), "connection": "lan"}
    # the calibration read is provenance, not part of the test's calls
    assert "calibrate" not in calls


def test_benchpod_without_a_run_is_not_wrapped():
    pod = FakePod()
    BenchPod(transport=pod, lease=False, api_key="", api_base="")
    assert "command" not in vars(pod)


# -- pytest plugin ---------------------------------------------------------------------------------

CONFTEST = '''
import pytest
from embeddedci.benchpod import BenchPod
from runs_fake import FakePod


@pytest.fixture
def pod():
    bp = BenchPod(transport=FakePod(), lease=False, api_key="", api_base="")
    yield bp
    bp.close()
'''

TESTS = '''
import pytest


def test_pass(pod, measure):
    pod.command({"cmd": "can_write"})
    measure("settle_ms", 840, unit="ms", max=1000, drift="20%")


def test_fail(pod):
    pod.command({"cmd": "can_write"})
    print("output of the failed test")
    assert 0x12 == 0x13


def test_limit(measure):
    measure("a", 5, max=1)
    measure("b", 0, min=1)
    measure("c", 3, min=1, max=4)


def test_skip():
    pytest.skip("no DUT")


@pytest.mark.xfail(reason="known bug")
def test_xfail():
    assert False


@pytest.mark.xfail(reason="fixed already")
def test_xpass():
    pass


@pytest.fixture
def broken():
    raise RuntimeError("setup broke")


def test_setup_error(broken):
    pass


@pytest.fixture
def bad_teardown():
    yield
    raise RuntimeError("teardown broke")


def test_teardown_error(bad_teardown):
    pass


def test_props(record_property):
    record_property("vbus_v", 5.02)
    record_property("note", "text is ignored")
    record_property("flag", True)
'''


def _write_suite(pytester, tests: str = TESTS) -> None:
    pytester.makepyfile(runs_fake=Path(__file__).with_name("runs_fake.py").read_text())
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(test_suite=tests)


def _run_json(path: Path) -> Dict[str, Any]:
    return json.loads((path / "run.json").read_text())


def _cases(record: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {c["id"].split("::")[-1]: c for c in record["results"]["cases"]}


def test_plugin_writes_a_run_folder(pytester):
    _write_suite(pytester)
    out = pytester.path / "out"
    fw = pytester.path / "app.elf"
    fw.write_bytes(b"firmware")
    res = pytester.runpytest(f"--embeddedci-run-dir={out}", "--embeddedci-suite=hil",
                             "--embeddedci-dut=process-node", f"--benchpod-firmware={fw}", "-p",
                             "no:cacheprovider")
    res.assert_outcomes(passed=3, failed=2, skipped=1, xfailed=1, xpassed=1, errors=2)
    res.stdout.fnmatch_lines(["*embeddedci: run error, recorded in*"])
    record = _run_json(out)
    assert record["schema_version"] == 1 and record["run_id"].startswith("local-")
    assert record["key"] == {"suite": "hil", "dut": "process-node", "definition_sha256": ""}
    assert record["trigger"] in ("manual", "github", "other")
    assert record["started"].endswith("Z") and record["finished"].endswith("Z")
    inputs = record["inputs"]
    assert set(inputs) == {"dut_firmware", "tests", "pod", "wiring", "tools", "github"}
    assert inputs["dut_firmware"]["file"] == "app.elf" and inputs["dut_firmware"]["size"] == 8
    assert inputs["pod"]["firmware"] == "3.10.11" and inputs["pod"]["connection"] == "lan"
    assert set(inputs["tools"]) == {"cli", "sdk", "python", "pytest", "os"}
    assert set(inputs["github"]) == {"run_id", "run_attempt", "run_url", "workflow", "actor"}
    assert set(record["health"]) == {"valid", "reasons", "lost_samples", "rate_misses", "busy_retries",
                                     "reconnects", "lease_lost", "measurements_dropped", "trace_truncated"}
    results = record["results"]
    assert results["outcome"] == "error"
    # pytest counts test_teardown_error as passed and errored; the run counts it once, as error
    assert results["counts"] == {"passed": 2, "failed": 2, "error": 2, "skipped": 1, "xfailed": 1,
                                 "xpassed": 1}
    assert record["keep"] == {"traces": "all"}

    cases = _cases(record)
    assert set(cases["test_pass"]) == {"id", "step", "kind", "outcome", "duration_ms", "message",
                                       "skip_reason", "attempt", "trace", "log", "measurements", "calls"}
    ok = cases["test_pass"]
    assert ok["step"] == "pytest" and ok["kind"] == "pytest" and ok["attempt"] == 1
    assert ok["outcome"] == "passed" and ok["log"] == "" and ok["message"] == ""
    assert ok["measurements"] == [{"name": "settle_ms", "value": 840, "unit": "ms", "min": None,
                                   "max": 1000, "drift": 0.2, "outcome": "passed", "source": "measure"}]
    assert ok["calls"]["can_write"]["n"] == 1 and (out / ok["trace"]).exists()
    fail = cases["test_fail"]
    assert fail["outcome"] == "failed" and "assert" in fail["message"]
    assert "output of the failed test" in fail["log"]
    limit = cases["test_limit"]
    assert limit["outcome"] == "failed"
    assert "2 measurement(s) outside their limits" in limit["message"]
    assert [m["outcome"] for m in limit["measurements"]] == ["failed", "failed", "passed"]
    assert cases["test_skip"]["outcome"] == "skipped" and cases["test_skip"]["skip_reason"] == "no DUT"
    assert cases["test_xfail"]["outcome"] == "xfailed" and cases["test_xfail"]["skip_reason"] == "known bug"
    assert cases["test_xpass"]["outcome"] == "xpassed"
    assert cases["test_setup_error"]["outcome"] == "error" and "setup broke" in cases["test_setup_error"]["message"]
    assert cases["test_teardown_error"]["outcome"] == "error"
    props = cases["test_props"]["measurements"]
    assert props == [{"name": "vbus_v", "value": 5.02, "unit": "", "min": None, "max": None,
                      "drift": None, "outcome": "recorded", "source": "record_property"}]

    junit = ET.parse(out / "junit.xml").getroot().find("testsuite")
    assert junit.get("name") == "hil" and junit.get("tests") == "9"
    assert not (out / "parts").exists()


def test_plugin_keep_failed_and_message_cap(pytester):
    _write_suite(pytester, tests='''
def test_pass(pod):
    pod.command({"cmd": "x"})


def test_fail(pod):
    pod.command({"cmd": "x"})
    assert "a" * 40000 == "b"
''')
    out = pytester.path / "out"
    res = pytester.runpytest(f"--embeddedci-run-dir={out}", "--embeddedci-keep-traces=failed",
                             "-p", "no:cacheprovider")
    res.assert_outcomes(passed=1, failed=1)
    record = _run_json(out)
    cases = _cases(record)
    assert cases["test_pass"]["trace"] == "" and cases["test_pass"]["calls"]["x"]["n"] == 1
    assert (out / cases["test_fail"]["trace"]).exists()
    assert len(cases["test_fail"]["message"].encode()) <= 16 * 1024
    assert record["keep"] == {"traces": "failed"}
    assert len(list((out / "trace").iterdir())) == 1


def test_plugin_handover_writes_a_part(pytester, monkeypatch):
    _write_suite(pytester, tests='''
def test_pass(pod, measure):
    pod.command({"cmd": "x"})
    measure("v", 1.0, unit="V")
''')
    out = pytester.path / "ci-run"
    monkeypatch.setenv("EMBEDDEDCI_RUN_DIR", str(out))
    monkeypatch.setenv("EMBEDDEDCI_RUN_STEP", "hil")
    monkeypatch.setenv("EMBEDDEDCI_RUN_T0", "1000.0")
    monkeypatch.setenv("EMBEDDEDCI_KEEP_TRACES", "all")
    res = pytester.runpytest("-p", "no:cacheprovider")
    res.assert_outcomes(passed=1)
    assert not (out / "run.json").exists() and not (out / "junit.xml").exists()
    part = json.loads((out / "parts" / "hil.json").read_text())
    assert part["schema_version"] == 1 and part["step"] == "hil" and part["kind"] == "pytest"
    assert set(part["health"]) == {"lost_samples", "rate_misses", "busy_retries", "reconnects",
                                   "measurements_dropped", "trace_truncated", "reasons"}
    assert set(part["tools"]) == {"sdk", "python", "pytest"}
    assert part["pod"]["firmware"] == "3.10.11"
    case = part["cases"][0]
    assert case["step"] == "hil" and case["measurements"][0]["name"] == "v"
    line = _lines(out / case["trace"])[0]
    assert line["t"] > 1e9                               # counted from EMBEDDEDCI_RUN_T0


def test_plugin_is_off_by_default(pytester):
    _write_suite(pytester, tests='''
def test_pass(pod, measure):
    pod.command({"cmd": "x"})
    measure("v", 1.0)
''')
    res = pytester.runpytest("-p", "no:cacheprovider")
    res.assert_outcomes(passed=1)
    assert not (pytester.path / "embeddedci-runs").exists()
    assert "embeddedci:" not in res.stdout.str()


def test_plugin_env_run_dir_standalone(pytester, monkeypatch):
    _write_suite(pytester, tests="def test_a():\n    pass\n")
    out = pytester.path / "env-run"
    monkeypatch.setenv("EMBEDDEDCI_RUN_DIR", str(out))
    monkeypatch.setenv("EMBEDDEDCI_RUN_ID", "local-given")
    res = pytester.runpytest("-p", "no:cacheprovider")
    res.assert_outcomes(passed=1)
    record = _run_json(out)
    assert record["run_id"] == "local-given" and record["key"]["suite"] == "pytest"
    assert record["inputs"]["pod"]["name"] == ""        # no pod connected


def test_plugin_auto_run_dir(pytester):
    _write_suite(pytester, tests="def test_a():\n    pass\n")
    res = pytester.runpytest("--embeddedci-run-dir=auto", "--embeddedci-suite=smoke", "-p",
                             "no:cacheprovider")
    res.assert_outcomes(passed=1)
    runs = list((pytester.path / "embeddedci-runs").iterdir())
    assert len(runs) == 1 and runs[0].name.endswith("Z-smoke")
    assert (runs[0] / "run.json").exists()


def test_measure_checks_limits_without_a_run(pytester):
    _write_suite(pytester, tests='''
import pytest


def test_limit(measure):
    measure("a", 5, max=1)


def test_bad_values(measure):
    for bad in (True, float("nan"), float("inf")):
        with pytest.raises((TypeError, ValueError)):
            measure("x", bad)
    with pytest.raises(ValueError):
        measure("d", 1, drift="lots")
    measure("dup", 1)
    with pytest.raises(ValueError, match="already recorded"):
        measure("dup", 2)


def test_after_failure(measure):
    measure("a", 5, max=1)
    assert False, "the body failed first"
''')
    res = pytester.runpytest("-p", "no:cacheprovider", "-rf")
    res.assert_outcomes(passed=1, failed=2)
    res.stdout.fnmatch_lines(["*a = 5 is outside the limits (max 1)*"])
    assert "error" not in res.parseoutcomes()


def test_measure_cap_per_case(pytester):
    _write_suite(pytester, tests='''
def test_many(measure, record_property):
    for i in range(205):
        measure(f"m{i}", i)
    record_property("extra", 1)
''')
    out = pytester.path / "out"
    res = pytester.runpytest(f"--embeddedci-run-dir={out}", "-p", "no:cacheprovider")
    res.assert_outcomes(passed=1)
    record = _run_json(out)
    assert len(record["results"]["cases"][0]["measurements"]) == 200
    assert record["health"]["measurements_dropped"] == 6


def test_build_report_fixture_still_works(pytester):
    _write_suite(pytester, tests='''
def test_a(build_report):
    build_report.record_wiring(target="t.cfg", swclk=11, swdio=12)
''')
    res = pytester.runpytest("-p", "no:cacheprovider")
    res.assert_outcomes(passed=1)


def test_message_starts_with_the_crash_line(pytester):
    _write_suite(pytester, tests='''
import pytest


class Bus:
    pass


@pytest.fixture
def bus():
    return Bus()


def test_registers(bus):
    raise ValueError("bad CRC: 6f72")


def test_huge():
    raise ValueError("x" * 40000)


def test_assert(bus):
    assert 1 + 1 == 3


@pytest.fixture
def broken():
    raise RuntimeError("setup broke")


def test_setup(broken):
    pass


def test_limit(measure):
    measure("a", 5, max=1)


def test_skip():
    pytest.skip("no DUT")
''')
    out = pytester.path / "out"
    res = pytester.runpytest(f"--embeddedci-run-dir={out}", "-p", "no:cacheprovider")
    res.assert_outcomes(failed=4, errors=1, skipped=1)
    cases = _cases(_run_json(out))
    msg = cases["test_registers"]["message"]
    first, _, rest = msg.partition("\n\n")
    assert first == "ValueError: bad CRC: 6f72"
    assert "def test_registers(bus):" in rest                 # the full report follows
    huge = cases["test_huge"]["message"]
    assert huge.startswith("ValueError: xxx") and len(huge.encode()) <= 16 * 1024
    assert cases["test_assert"]["message"].startswith("assert ")
    assert "def test_assert(bus):" in cases["test_assert"]["message"]
    assert cases["test_setup"]["message"].startswith("RuntimeError: setup broke\n\n")
    assert cases["test_limit"]["message"].startswith("1 measurement(s) outside their limits")
    assert cases["test_skip"]["message"] == "" and cases["test_skip"]["skip_reason"] == "no DUT"
