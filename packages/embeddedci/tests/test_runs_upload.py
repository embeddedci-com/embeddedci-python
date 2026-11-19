"""Uploading a run folder (``--embeddedci-upload``) and ``build_report`` linking the run to its
build, against an in-process fake runs API."""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest

from embeddedci.benchpod.runs import case_slug, set_active_run
from embeddedci.benchpod.runs import upload as upload_mod
from embeddedci.benchpod.runs.upload import (
    RunUploadError,
    names_valid,
    upload_files,
    upload_record,
    upload_run,
)

RUN_ENV = ("EMBEDDEDCI_RUN_DIR", "EMBEDDEDCI_RUN_STEP", "EMBEDDEDCI_RUN_ID", "EMBEDDEDCI_RUN_T0",
           "EMBEDDEDCI_KEEP_TRACES", "EMBEDDEDCI_UPLOAD", "GITHUB_ACTIONS", "BENCHPOD_API_BASE")


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    for name in RUN_ENV:
        monkeypatch.delenv(name, raising=False)
    yield
    set_active_run(None)


class RunsAPI:
    """POST /api/runs and /api/runs/{id}/files; ``fail`` answers run creation with HTTP 500."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.fail = False
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                url = urllib.parse.urlsplit(self.path)
                api.requests.append({"path": url.path, "query": urllib.parse.parse_qs(url.query),
                                     "auth": self.headers.get("Authorization"), "body": body})
                if url.path == "/api/runs":
                    if api.fail:
                        self._send(500, {"error": "database down"})
                        return
                    doc = json.loads(body)
                    self._send(201, {"id": "r-1", "url": "https://example.test/run/r-1",
                                     "build_id": doc["inputs"].get("dut_firmware", {}).get("build_id", "")})
                elif url.path == "/api/runs/r-1/files":
                    name = api.requests[-1]["query"]["name"][0]
                    if not names_valid([name]):
                        self._send(400, {"error": "bad name"})
                    else:
                        self._send(201, {"name": name, "size_bytes": len(body)})
                else:
                    self._send(404, {"error": "no route"})

            def _send(self, code: int, obj: Dict[str, Any]) -> None:
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def run_doc(self) -> Dict[str, Any]:
        return json.loads(next(r for r in self.requests if r["path"] == "/api/runs")["body"])

    def file_names(self) -> List[str]:
        return [r["query"]["name"][0] for r in self.requests if r["path"].endswith("/files")]

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def api():
    a = RunsAPI()
    yield a
    a.close()


def _folder(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    (run / "trace").mkdir(parents=True)
    (run / "att").mkdir()
    (run / "logs").mkdir()
    ok_trace, bad_trace = f"trace/{case_slug('t.py::test_ok')}.ndjson", f"trace/{case_slug('t.py::test_bad')}.ndjson"
    (run / ok_trace).write_text('{"t":1,"op":"command","cmd":"big","att":"att/0001-big.json"}\n')
    (run / bad_trace).write_text('{"t":2,"op":"command","cmd":"x","ok":true}\n'
                                 '{"t":3,"op":"samples","cmd":"la","att":"att/0002-la.json"}\n')
    (run / "att" / "0001-big.json").write_text("{}")
    (run / "att" / "0002-la.json").write_text("{}")
    (run / "logs" / "flash.log").write_text("ok")
    (run / "junit.xml").write_text("<testsuites/>")
    record = {"schema_version": 1, "keep": {"traces": "all"},
              "inputs": {"dut_firmware": {}},
              "results": {"cases": [
                  {"id": "t.py::test_ok", "outcome": "passed", "trace": ok_trace},
                  {"id": "t.py::test_bad", "outcome": "failed", "trace": bad_trace},
                  {"id": "t.py::test_skip", "outcome": "skipped", "trace": ""}]}}
    (run / "run.json").write_text(json.dumps(record))
    return run


def test_only_failed_traces_and_their_attachments_go_up(tmp_path):
    run = _folder(tmp_path)
    record = json.loads((run / "run.json").read_text())
    doc = upload_record(record, build_id="b-9")
    assert doc["keep"] == {"traces": "failed"}
    cases = {c["id"]: c for c in doc["results"]["cases"]}
    assert cases["t.py::test_ok"]["trace"] == "" and cases["t.py::test_bad"]["trace"]
    assert doc["inputs"]["dut_firmware"]["build_id"] == "b-9"
    assert record["results"]["cases"][0]["trace"]               # the input is not changed
    names = upload_files(run, doc)
    assert names == ["junit.xml", cases["t.py::test_bad"]["trace"], "att/0002-la.json", "logs/flash.log"]
    assert names_valid(names)


@pytest.mark.parametrize("case_id", [
    "tests/test_can_hil.py::test_ack",
    "tests/test_a.py::TestX::test_param[1.8 V-ünïcode/../x]",
    "x" * 500,
    ".hidden",
    "",
])
def test_trace_names_pass_the_server_rule(case_id):
    assert names_valid([f"trace/{case_slug(case_id)}.ndjson", f"trace/{case_slug(case_id)}-2.ndjson"])


def test_server_rule_refuses_bad_names():
    assert not names_valid(["trace/.x", "trace/a/b", "parts/hil.json", "trace/a b", "x" * 201])
    assert names_valid(["run.json", "junit.xml", "trace/_session.ndjson", "att/0001-la.json.zst"])


def test_upload_run_posts_the_run_then_its_files(tmp_path, api):
    run = _folder(tmp_path)
    got = upload_run(run, api_base=api.base, token="tok")
    assert got == {"id": "r-1", "url": "https://example.test/run/r-1", "build_id": ""}
    assert api.requests[0]["path"] == "/api/runs" and api.requests[0]["auth"] == "Bearer tok"
    assert api.run_doc()["keep"] == {"traces": "failed"}
    assert api.file_names() == upload_files(run, api.run_doc())


def test_upload_run_refused(tmp_path, api):
    api.fail = True
    with pytest.raises(RunUploadError, match="HTTP 500.*database down"):
        upload_run(_folder(tmp_path), api_base=api.base, token="tok")
    with pytest.raises(RunUploadError, match="run.json"):
        upload_run(tmp_path / "missing", api_base=api.base, token="tok")


# -- the pytest plugin -----------------------------------------------------------------------------

SUITE = '''
def test_ok():
    pass


def test_bad():
    assert False
'''


def test_plugin_uploads_with_the_option(pytester, api, monkeypatch):
    monkeypatch.setattr(upload_mod, "_token", lambda base: "tok")
    monkeypatch.setenv("BENCHPOD_API_BASE", api.base)
    pytester.makepyfile(test_suite=SUITE)
    out = pytester.path / "out"
    res = pytester.runpytest(f"--embeddedci-run-dir={out}", "--embeddedci-upload", "-p", "no:cacheprovider")
    res.assert_outcomes(passed=1, failed=1)
    res.stdout.fnmatch_lines(["*embeddedci: run uploaded: https://example.test/run/r-1*"])
    doc = api.run_doc()
    assert len(doc["results"]["cases"]) == 2 and "junit.xml" in api.file_names()
    assert (out / "run.json").exists()


def test_plugin_upload_env_records_into_the_default_folder(pytester, api, monkeypatch):
    monkeypatch.setattr(upload_mod, "_token", lambda base: "tok")
    monkeypatch.setenv("BENCHPOD_API_BASE", api.base)
    monkeypatch.setenv("EMBEDDEDCI_UPLOAD", "1")
    pytester.makepyfile(test_suite=SUITE)
    res = pytester.runpytest("-p", "no:cacheprovider")
    res.assert_outcomes(passed=1, failed=1)
    assert len(list((pytester.path / "embeddedci-runs").iterdir())) == 1
    assert api.run_doc()["schema_version"] == 1


def test_plugin_upload_failure_is_a_warning(pytester, api, monkeypatch):
    monkeypatch.setattr(upload_mod, "_token", lambda base: "tok")
    monkeypatch.setenv("BENCHPOD_API_BASE", api.base)
    api.fail = True
    pytester.makepyfile(test_suite="def test_ok():\n    pass\n")
    res = pytester.runpytest("--embeddedci-upload", "-p", "no:cacheprovider")
    res.assert_outcomes(passed=1)
    assert res.ret == 0
    res.stdout.fnmatch_lines(["*embeddedci: warning: run upload: run upload failed (HTTP 500)*"])


def test_plugin_never_uploads_under_benchpod_ci(pytester, api, monkeypatch):
    monkeypatch.setattr(upload_mod, "_token", lambda base: "tok")
    monkeypatch.setenv("BENCHPOD_API_BASE", api.base)
    monkeypatch.setenv("EMBEDDEDCI_RUN_DIR", str(pytester.path / "ci"))
    monkeypatch.setenv("EMBEDDEDCI_RUN_STEP", "hil")
    pytester.makepyfile(test_suite="def test_ok():\n    pass\n")
    res = pytester.runpytest("--embeddedci-upload", "-p", "no:cacheprovider")
    res.assert_outcomes(passed=1)
    assert api.requests == [] and (pytester.path / "ci" / "parts" / "hil.json").exists()


class _FakeReporter:
    active = True
    build_id = "b-42"

    def __init__(self, base: str) -> None:
        self.api_base = base
        self.session_token = "oidc-tok"
        self.result = None

    def record_wiring(self, **_k: Any) -> None:
        pass

    def upload_logs(self, *_a: Any) -> None:
        pass

    def set_result(self, success: bool, reason: str = "") -> None:
        self.result = (success, reason)

    def finalize(self) -> None:
        pass


def test_build_report_uploads_a_run_linked_to_the_build(pytester, api, monkeypatch):
    from embeddedci.benchpod import ci

    reporters: List[_FakeReporter] = []

    def make(**_k: Any) -> _FakeReporter:
        reporters.append(_FakeReporter(api.base))
        return reporters[-1]

    monkeypatch.setattr(ci, "make_build_reporter", make)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    pytester.makepyfile(test_suite='''
def test_flash(build_report):
    build_report.record_wiring(target="t.cfg")


def test_other():
    assert False
''')
    res = pytester.runpytest("-p", "no:cacheprovider")
    res.assert_outcomes(passed=1, failed=1)
    assert reporters[0].result == (True, "")                     # the old status call still runs
    doc = api.run_doc()
    assert doc["inputs"]["dut_firmware"]["build_id"] == "b-42"
    assert {c["id"].split("::")[-1] for c in doc["results"]["cases"]} == {"test_flash", "test_other"}
    assert next(r for r in api.requests if r["path"] == "/api/runs")["auth"] == "Bearer oidc-tok"
    local = json.loads(next((pytester.path / "embeddedci-runs").iterdir()).joinpath("run.json").read_text())
    assert local["inputs"]["dut_firmware"]["build_id"] == "b-42"
    res.stdout.fnmatch_lines(["*embeddedci: run uploaded: https://example.test/run/r-1*"])


def test_build_report_outside_github_actions_records_nothing(pytester, api):
    pytester.makepyfile(test_suite='''
def test_flash(build_report):
    assert not build_report.active
''')
    res = pytester.runpytest("-p", "no:cacheprovider")
    res.assert_outcomes(passed=1)
    assert not (pytester.path / "embeddedci-runs").exists() and api.requests == []


def test_the_repos_benchpod_ci_file():
    yaml = pytest.importorskip("yaml")
    here = Path(__file__).parent
    cfg = yaml.safe_load((here / "benchpod-ci.yaml").read_text())
    assert cfg["version"] == 1 and cfg["suite"] and cfg["dut"] == "none"
    step = cfg["steps"][0]["pytest"]
    assert (here / step["path"]).is_dir() and (here / step["args"][0]).is_dir()


@pytest.mark.parametrize("what", ["la_capture", "ünïcode_cmd", "温度", ".dot", "a/b c", ""])
def test_attachment_and_part_names_pass_the_server_rule(what):
    from embeddedci.benchpod.runs.active import case_slug_simple

    part = case_slug_simple(what)
    assert names_valid([f"att/0001-{part}.json", f"att/0001-{part}.json.zst", f"logs/{part}.log"])
    assert all(c.isascii() for c in part)


def test_recorded_attachment_names_are_ascii(tmp_path):
    from embeddedci.benchpod.runs import RunRecording

    run = RunRecording(tmp_path / "run")
    run.begin_case("c")
    run.record(t=0.0, op="command", cmd="ünï", req={"cmd": "ünï", "x": "y" * 5000}, ms=1.0, ok=True)
    run.end_case("c", "failed")
    names = [f"att/{p.name}" for p in (tmp_path / "run" / "att").iterdir()]
    assert names and names_valid(names)
