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
        "--embeddedci-keep-traces", action="store", default=None, dest="embeddedci_keep_traces",
        choices=list(KEEP_POLICIES),
        help="Which pod-call traces to keep: all (default), failed (failed and error tests only) or "
        "none. Falls back to EMBEDDEDCI_KEEP_TRACES.",
    )


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
            message = rep.longreprtext or ""
        elif wasxfail is not None and rep.passed:
            outcome, reason = "xpassed", str(wasxfail)
        elif rep.failed:
            if when == "call":
                outcome, message = "failed", rep.longreprtext or ""
            elif when == "teardown" and outcome == "failed":
                message = (message + "\n\n" + (rep.longreprtext or "")).strip()
            else:
                outcome, message = "error", rep.longreprtext or ""
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
        t0_text = os.environ.get(RUN_T0_ENV, "").strip()
        try:
            t0 = float(t0_text) if t0_text else None
        except ValueError:
            raise pytest.UsageError(f"{RUN_T0_ENV} must be unix seconds, got {t0_text!r}") from None
        if env_dir and env_step:
            return {"run_dir": env_dir, "step": env_step, "handover": True, "keep": keep, "t0": t0,
                    "run_id": os.environ.get(RUN_ID_ENV, "").strip()}
        run_dir = opt_dir or env_dir
        if not run_dir:
            return None
        return {"run_dir": run_dir, "step": "pytest", "handover": False, "keep": keep, "t0": t0,
                "run_id": os.environ.get(RUN_ID_ENV, "").strip()}

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self, session: "pytest.Session") -> None:
        settings = self._settings()
        if settings is None:
            return
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

    def pytest_collectreport(self, report: Any) -> None:
        if self.run is None or not report.failed:
            return
        self.cases.append(Case(id=report.nodeid or "<collection>", step=self.run.step, kind="pytest",
                               outcome="error", message=report.longreprtext or "collection failed"))

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
        key = {"suite": self.suite, "dut": self.dut, "definition_sha256": ""}
        self.record = run.write_run(cases=self.cases, key=key, inputs=inputs, suite=self.suite)

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        run = self.run
        if run is None:
            return
        if run.handover:
            terminalreporter.write_line(f"embeddedci: step {run.step} recorded in {run.run_dir}")
        elif self.record is not None:
            outcome = self.record["results"]["outcome"]
            terminalreporter.write_line(f"embeddedci: run {outcome}, recorded in {run.run_dir}")

    def pytest_unconfigure(self, config: "pytest.Config") -> None:
        if self.run is not None and self.run.active:
            self.run.finish()
