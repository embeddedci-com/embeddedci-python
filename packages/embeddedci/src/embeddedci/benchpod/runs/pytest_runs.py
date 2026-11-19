"""The pytest side of run recording: the ``--embeddedci-*`` options, the ``measure`` fixture and the
hooks that turn pytest's reports into the run folder's cases.

Off unless asked for: ``--embeddedci-run-dir DIR`` or ``EMBEDDEDCI_RUN_DIR`` turns it on. With
``EMBEDDEDCI_RUN_DIR`` and ``EMBEDDEDCI_RUN_STEP`` (set by ``benchpod ci``) the plugin writes
``parts/<step>.json`` and its traces into that folder instead of a whole run. ``measure`` checks its
limits whether a run is being recorded or not.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from . import provenance
from .active import (
    KEEP_TRACES_ENV,
    RUN_DIR_ENV,
    RUN_ID_ENV,
    RUN_STEP_ENV,
    RUN_T0_ENV,
    RunRecording,
    active_run,
    default_run_dir,
    set_active_run,
)
from .schema import (
    KEEP_POLICIES,
    LOG_MAX_BYTES,
    MEASUREMENTS_PER_CASE,
    Case,
    Measurement,
    cap_text,
    empty_pod,
    is_plain_number,
)

PLUGIN_NAME = "embeddedci-runs"
#: ``EMBEDDEDCI_UPLOAD=1`` uploads the recorded run (like ``--embeddedci-upload``).
UPLOAD_ENV = "EMBEDDEDCI_UPLOAD"
#: ``--embeddedci-run-dir auto``: the default folder, ``./embeddedci-runs/<started UTC>-<suite>/``.
AUTO_RUN_DIR = "auto"


def add_options(parser: "pytest.Parser") -> None:
    group = parser.getgroup("embeddedci", "EmbeddedCI test run recording")
    group.addoption(
        "--embeddedci-run-dir", action="store", default=None, dest="embeddedci_run_dir",
        metavar="DIR",
        help="Record this run into the folder DIR (run.json, junit.xml, a pod-call trace per test); "
        "'auto' picks ./embeddedci-runs/<started UTC>-<suite>/. Off by default; EMBEDDEDCI_RUN_DIR "
        "turns it on too.",
    )
    group.addoption(
        "--embeddedci-suite", action="store", default=None, dest="embeddedci_suite",
        help="Suite name recorded in run.json (key.suite; default 'pytest').",
    )
    group.addoption(
        "--embeddedci-dut", action="store", default=None, dest="embeddedci_dut",
        help="Name of the board under test recorded in run.json (key.dut; default '').",
    )
    group.addoption(
        "--embeddedci-upload", action="store_true", default=False, dest="embeddedci_upload",
        help="Upload the recorded run to embeddedci.com at the end (records into "
        "./embeddedci-runs/ when --embeddedci-run-dir is not given). Credentials: BENCHPOD_API_KEY, "
        "else the benchpod login session, else GitHub Actions OIDC. Also the embeddedci_upload ini "
        "option and EMBEDDEDCI_UPLOAD=1.",
    )
    parser.addini("embeddedci_upload", type="bool", default=False,
                  help="Upload the recorded run to embeddedci.com (see --embeddedci-upload).")
    group.addoption(
        "--embeddedci-keep-traces", action="store", default=None, dest="embeddedci_keep_traces",
        choices=list(KEEP_POLICIES),
        help="Which pod-call traces to keep: all (default), failed (failed and error tests only) or "
        "none. Falls back to EMBEDDEDCI_KEEP_TRACES.",
    )


def _ini_bool(config: Any, name: str) -> bool:
    try:
        return bool(config.getini(name))
    except (ValueError, KeyError):
        return False


# -- measure -------------------------------------------------------------------------------------

class Measure:
    """The ``measure`` fixture: ``measure(name, value, unit="", min=None, max=None, drift=None)``.

    Records a named number for the test. ``min``/``max`` are limits: a value outside them fails the
    test (all failures listed, after the test body). ``drift`` (``"20%"`` or ``0.2``) is the
    tolerance a later comparison against a baseline uses; it never fails the test. Numbers only:
    ``bool``, NaN and infinity are refused. Stored in the run when one is being recorded (at most
    200 per test); the limits are checked either way."""

    def __init__(self, case_id: str, run: Optional[RunRecording]) -> None:
        self.case_id = case_id
        self._run = run
        self.items: List[Measurement] = []
        self._names: set = set()
        self._reported = 0

    def __call__(self, name: str, value: Any, unit: str = "", min: Any = None, max: Any = None,
                 drift: Any = None) -> Measurement:
        m = Measurement.make(name, value, unit=unit, min=min, max=max, drift=drift, source="measure")
        if name in self._names:
            raise ValueError(f"measurement {name!r} was already recorded in this test: names are "
                             f"unique per test")
        self._names.add(name)
        self.items.append(m)
        run = self._run
        if run is not None and run.active:
            run.add_measurement(self.case_id, m)
        return m

    @property
    def failures(self) -> List[Measurement]:
        """Every measurement outside its limits so far."""
        return [m for m in self.items if m.outcome == "failed"]

    def take_unreported(self) -> List[Measurement]:
        """The failures not yet raised (each failure fails the test once)."""
        failed = self.failures
        new = failed[self._reported:]
        self._reported = len(failed)
        return new


def _limit_text(failures: List[Measurement]) -> str:
    lines = [f"{len(failures)} measurement(s) outside their limits:"]
    lines += [f"  {m.describe_failure()}" for m in failures]
    return "\n".join(lines)


@pytest.fixture
def measure(request: "pytest.FixtureRequest") -> Iterator[Measure]:
    """Record named numeric values with optional limits:
    ``measure("settle_ms", 840, unit="ms", max=1000)``. A value outside ``min``/``max`` fails the
    test (every failure listed); ``drift`` (``"20%"`` or ``0.2``) is kept for comparisons with a
    baseline. Stored in the run folder when one is recorded."""
    m = Measure(request.node.nodeid, active_run())
    request.node._embeddedci_measure = m
    yield m
    pending = m.take_unreported()
    if pending:
        rep_call = getattr(request.node, "rep_call", None)
        if rep_call is not None and rep_call.failed:
            return  # the test failed already; the measurements are still recorded
        pytest.fail(_limit_text(pending), pytrace=False)


# -- outcomes --------------------------------------------------------------------------------------

def _skip_reason(rep: Any) -> str:
    longrepr = getattr(rep, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        text = str(longrepr[2])
    else:
        text = str(longrepr or "")
    for prefix in ("Skipped: ", "Skipped:"):
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text.strip()


def _failure_text(rep: Any) -> str:
    """A failure's text for ``message``: the crash line first (``ValueError: bad CRC: 6f72``,
    pytest's ``reprcrash.message``), a blank line, then the full report. Just the report when
    pytest has no crash line (``pytest.fail(..., pytrace=False)``, a collection error)."""
    full = (getattr(rep, "longreprtext", "") or "").strip("\n")
    crash = getattr(getattr(rep, "longrepr", None), "reprcrash", None)
    line = str(getattr(crash, "message", "") or "").strip()
    if not line or full in line:   # pytest.fail(..., pytrace=False): the report is the message
        return full
    return f"{line}\n\n{full}" if full else line


def case_outcome(reports: Dict[str, Any]) -> Tuple[str, str, str]:
    """``(outcome, message, skip_reason)`` of a test from its setup/call/teardown reports.

    A failure in setup or teardown is ``error``, one in the test body ``failed``; ``xfailed`` and
    ``xpassed`` come from pytest's xfail marker (a strict xpass is ``failed``, as pytest reports it)."""
    outcome = ""
    message = ""
    reason = ""
    for when in ("setup", "call", "teardown"):
        rep = reports.get(when)
        if rep is None:
            continue
        wasxfail = getattr(rep, "wasxfail", None)
        if wasxfail is not None and rep.skipped:
            outcome, reason = "xfailed", str(wasxfail)
            message = _failure_text(rep)
        elif wasxfail is not None and rep.passed:
            outcome, reason = "xpassed", str(wasxfail)
        elif rep.failed:
            if when == "call":
                outcome, message = "failed", _failure_text(rep)
            elif when == "teardown" and outcome == "failed":
                message = (message + "\n\n" + _failure_text(rep)).strip()
            else:
                outcome, message = "error", _failure_text(rep)
        elif rep.skipped:
            outcome, reason = "skipped", _skip_reason(rep)
        elif rep.passed and when == "call":
            outcome = "passed"
        elif rep.passed and when == "setup" and not outcome:
            outcome = "passed"
    return outcome or "passed", message, reason


def _captured_output(reports: Dict[str, Any]) -> str:
    """The captured stdout/stderr/log of a test (pytest's report sections), as one text."""
    best: List[Tuple[str, str]] = []
    for when in ("setup", "call", "teardown"):
        rep = reports.get(when)
        sections = list(getattr(rep, "sections", []) or []) if rep is not None else []
        if len(sections) >= len(best):
            best = sections
    parts = [f"----- {title} -----\n{content.rstrip()}" for title, content in best if content.strip()]
    return cap_text("\n".join(parts), LOG_MAX_BYTES, keep="tail")


# -- the plugin ------------------------------------------------------------------------------------

class RunsPlugin:
    """Registered for every session: checks ``measure`` limits always, and records the run when
    ``--embeddedci-run-dir`` or ``EMBEDDEDCI_RUN_DIR`` asks for it."""

    def __init__(self, config: "pytest.Config") -> None:
        self.config = config
        self.run: Optional[RunRecording] = None
        self.cases: List[Case] = []
        self.suite = "pytest"
        self.dut = ""
        self.record: Optional[Dict[str, Any]] = None
        self._reports: Dict[str, Dict[str, Any]] = {}
        #: Upload the run at the end (--embeddedci-upload, the ini option, EMBEDDEDCI_UPLOAD=1).
        self.upload = False
        #: The build a build_report fixture created: (build id, api base, session token).
        self.build_link: Optional[Tuple[str, str, str]] = None
        #: The upload's result ({"id", "url", "build_id"}) or the warning it ended with.
        self.uploaded: Optional[Dict[str, Any]] = None
        self.upload_warning = ""

    # -- start --------------------------------------------------------------------------------

    def _settings(self) -> Optional[Dict[str, Any]]:
        config = self.config
        if hasattr(config, "workerinput"):  # pytest-xdist worker: the controller records the cases
            return None
        env_dir = os.environ.get(RUN_DIR_ENV, "").strip()
        env_step = os.environ.get(RUN_STEP_ENV, "").strip()
        opt_dir = config.getoption("embeddedci_run_dir", None)
        self.suite = config.getoption("embeddedci_suite", None) or "pytest"
        self.dut = config.getoption("embeddedci_dut", None) or ""
        keep = (config.getoption("embeddedci_keep_traces", None)
                or os.environ.get(KEEP_TRACES_ENV, "").strip().lower() or "all")
        if keep not in KEEP_POLICIES:
            raise pytest.UsageError(f"{KEEP_TRACES_ENV} must be one of {', '.join(KEEP_POLICIES)}, got {keep!r}")
        self.upload = bool(config.getoption("embeddedci_upload", False)
                           or _ini_bool(config, "embeddedci_upload")
                           or os.environ.get(UPLOAD_ENV, "").strip().lower() in ("1", "true", "yes"))
        t0_text = os.environ.get(RUN_T0_ENV, "").strip()
        try:
            t0 = float(t0_text) if t0_text else None
        except ValueError:
            raise pytest.UsageError(f"{RUN_T0_ENV} must be unix seconds, got {t0_text!r}") from None
        if env_dir and env_step:
            self.upload = False  # benchpod ci uploads the whole run itself
            return {"run_dir": env_dir, "step": env_step, "handover": True, "keep": keep, "t0": t0,
                    "run_id": os.environ.get(RUN_ID_ENV, "").strip()}
        run_dir = opt_dir or env_dir or (AUTO_RUN_DIR if self.upload else "")
        if not run_dir:
            return None
        return {"run_dir": run_dir, "step": "pytest", "handover": False, "keep": keep, "t0": t0,
                "run_id": os.environ.get(RUN_ID_ENV, "").strip()}

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self, session: "pytest.Session") -> None:
        settings = self._settings()
        if settings is not None:
            self._start(settings)

    def pytest_collection_finish(self, session: "pytest.Session") -> None:
        """A suite whose tests use ``build_report`` records its run in GitHub Actions (when nothing
        else asked for it), so the run can be uploaded linked to the build the fixture reports."""
        if self.run is not None or hasattr(self.config, "workerinput"):
            return
        if os.environ.get(RUN_STEP_ENV, "").strip():
            return
        from ..ci import running_in_github_actions

        if not running_in_github_actions():
            return
        if not any("build_report" in getattr(item, "fixturenames", ()) for item in session.items):
            return
        self._settings()
        self._start({"run_dir": AUTO_RUN_DIR, "step": "pytest", "handover": False,
                     "keep": os.environ.get(KEEP_TRACES_ENV, "").strip().lower() or "failed",
                     "t0": None, "run_id": ""})

    def link_build(self, build_id: str, api_base: str, token: str) -> None:
        """The ``build_report`` fixture created ``build_id``: link the run to it (the first one) and
        upload it at the end with the fixture's credentials."""
        if build_id and self.build_link is None:
            self.build_link = (build_id, api_base, token)

    def _start(self, settings: Dict[str, Any]) -> None:
        run_dir = settings["run_dir"]
        if run_dir == AUTO_RUN_DIR:
            import time

            run_dir = default_run_dir(time.time(), self.suite, base=str(self.config.invocation_params.dir))
        self.run = RunRecording(run_dir, step=settings["step"], keep=settings["keep"],
                                t0=settings["t0"], handover=settings["handover"],
                                run_id=settings["run_id"])
        self.run.suite = self.suite
        set_active_run(self.run)

    # -- per test -----------------------------------------------------------------------------

    def pytest_runtest_logstart(self, nodeid: str, location: Any) -> None:
        if self.run is not None:
            self.run.begin_case(nodeid)

    def pytest_runtest_logreport(self, report: Any) -> None:
        if self.run is not None:
            self._reports.setdefault(report.nodeid, {})[report.when] = report

    def pytest_runtest_logfinish(self, nodeid: str, location: Any) -> None:
        run = self.run
        if run is None:
            return
        reports = self._reports.pop(nodeid, {})
        outcome, message, reason = case_outcome(reports)
        duration_ms = int(round(sum(float(getattr(r, "duration", 0.0) or 0.0) for r in reports.values()) * 1000))
        last = reports.get("teardown") or reports.get("call") or reports.get("setup")
        for key, value in list(getattr(last, "user_properties", []) or []):
            if is_plain_number(value):
                try:
                    m = Measurement.make(str(key), value, source="record_property")
                except (TypeError, ValueError):
                    continue
                run.add_measurement(nodeid, m)
        trace, calls = run.end_case(nodeid, outcome)
        self.cases.append(Case(
            id=nodeid, step=run.step, kind="pytest", outcome=outcome, duration_ms=duration_ms,
            message=message if outcome in ("failed", "error", "xfailed") else "",
            skip_reason=reason, attempt=1, trace=trace,
            log=_captured_output(reports) if outcome in ("failed", "error") else "",
            measurements=run.measurements(nodeid)[:MEASUREMENTS_PER_CASE], calls=calls))
        self._exit_if_stopped()

    def _exit_if_stopped(self) -> None:
        """The runner's lease was stopped from the web app: end the session now. The cases so far
        and the run's health still go to the run folder (``pytest_sessionfinish``)."""
        run = self.run
        if run is not None and run.stop_reason:
            pytest.exit(run.stop_reason, returncode=pytest.ExitCode.INTERRUPTED)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item: "pytest.Item") -> None:
        self._exit_if_stopped()

    def pytest_collectreport(self, report: Any) -> None:
        if self.run is None or not report.failed:
            return
        self.cases.append(Case(id=report.nodeid or "<collection>", step=self.run.step, kind="pytest",
                               outcome="error", message=_failure_text(report) or "collection failed"))

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_call(self, item: "pytest.Item") -> None:
        """After the test body passed: fail it when a ``measure`` value is outside its limits."""
        m = getattr(item, "_embeddedci_measure", None)
        if m is None:
            return
        pending = m.take_unreported()
        if pending:
            pytest.fail(_limit_text(pending), pytrace=False)

    # -- end ----------------------------------------------------------------------------------

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: "pytest.Session", exitstatus: int) -> None:
        run = self.run
        if run is None:
            return
        run.finish()
        tools = provenance.tools_info(pytest.__version__)
        if run.handover:
            self.record = run.write_part(cases=self.cases, tools=tools)
            return
        inputs = {
            "dut_firmware": provenance.firmware_info(self.config.getoption("benchpod_firmware", None)),
            "tests": provenance.git_info(str(self.config.rootpath)),
            "pod": run.pod() or empty_pod(),
            "wiring": run.wiring,
            "tools": tools,
            "github": provenance.github_info(),
        }
        if self.build_link is not None:
            inputs["dut_firmware"] = {**inputs["dut_firmware"], "build_id": self.build_link[0]}
        key = {"suite": self.suite, "dut": self.dut, "definition_sha256": ""}
        self.record = run.write_run(cases=self.cases, key=key, inputs=inputs, suite=self.suite)
        if self.upload or self.build_link is not None:
            self._upload(run)

    def _upload(self, run: RunRecording) -> None:
        from .upload import RunUploadError, upload_run

        problems: List[str] = []
        api_base = (self.config.getoption("benchpod_api_base", None)
                    or os.environ.get("BENCHPOD_API_BASE") or None)
        token = None
        build_id = None
        if self.build_link is not None:
            build_id, base, token = self.build_link
            api_base = api_base or base
        try:
            self.uploaded = upload_run(run.run_dir, api_base=api_base, token=token,
                                       build_id=build_id, warn=problems.append)
        except RunUploadError as exc:
            self.upload_warning = str(exc)
            return
        if problems:
            self.upload_warning = "some files were not uploaded: " + "; ".join(problems[:5])

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        run = self.run
        if run is None:
            return
        if run.handover:
            terminalreporter.write_line(f"embeddedci: step {run.step} recorded in {run.run_dir}")
        elif self.record is not None:
            outcome = self.record["results"]["outcome"]
            terminalreporter.write_line(f"embeddedci: run {outcome}, recorded in {run.run_dir}")
        if self.uploaded is not None:
            terminalreporter.write_line(f"embeddedci: run uploaded: {self.uploaded.get('url') or self.uploaded.get('id')}")
        if self.upload_warning:
            terminalreporter.write_line(f"embeddedci: warning: run upload: {self.upload_warning}", yellow=True)

    def pytest_unconfigure(self, config: "pytest.Config") -> None:
        if self.run is not None and self.run.active:
            self.run.finish()
