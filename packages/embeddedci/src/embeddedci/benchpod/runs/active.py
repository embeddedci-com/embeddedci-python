"""The run being recorded in this process, and its trace files.

There is at most one active :class:`RunRecording` per process (:func:`active_run`). The pytest
plugin starts it; :class:`~embeddedci.benchpod.BenchPod` asks for it after its transport is open and
attaches a :class:`~.recorder.Recorder`, so every pod opened while the run is active is recorded,
including ones a test opens itself.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from . import provenance
from .recorder import Recorder, suppressed
from .schema import (
    INLINE_JSON_MAX_BYTES,
    KEEP_POLICIES,
    MEASUREMENTS_PER_CASE,
    TRACE_MAX_BYTES_PER_RUN,
    CallStats,
    Case,
    Measurement,
    build_part_record,
    build_run_record,
    case_slug,
    compact_json,
    empty_health,
    junit_xml,
    redact,
    write_bytes_atomic,
    write_json_atomic,
)

try:  # optional: compress attachments when the module is there
    import zstandard as _zstd  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - depends on the environment
    _zstd = None

#: Environment handover from ``benchpod ci`` (spec section 4).
RUN_DIR_ENV = "EMBEDDEDCI_RUN_DIR"
RUN_ID_ENV = "EMBEDDEDCI_RUN_ID"
RUN_STEP_ENV = "EMBEDDEDCI_RUN_STEP"
RUN_T0_ENV = "EMBEDDEDCI_RUN_T0"
KEEP_TRACES_ENV = "EMBEDDEDCI_KEEP_TRACES"

#: Case outcomes whose trace the ``failed`` policy keeps.
_KEPT_WHEN_FAILED = frozenset({"failed", "error"})
#: Trace file for pod calls made outside any case.
SESSION_TRACE = "_session"

_active: Optional["RunRecording"] = None
_active_lock = threading.Lock()


def active_run() -> Optional["RunRecording"]:
    """The run being recorded in this process, or None."""
    run = _active
    return run if run is not None and run.active else None


def set_active_run(run: Optional["RunRecording"]) -> None:
    """Make ``run`` the process's active run (None clears it)."""
    global _active
    with _active_lock:
        _active = run


def clear_active_run(run: Optional["RunRecording"] = None) -> None:
    """Clear the active run (only when it is ``run``, if given)."""
    global _active
    with _active_lock:
        if run is None or _active is run:
            _active = None


def utc_stamp(ts: float) -> str:
    """``2026-11-18T14:02:11.123Z``"""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def folder_stamp(ts: float) -> str:
    """``20261118T140211Z``"""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def new_run_id(started: float) -> str:
    """``local-20261118T140211Z-3f9a``: the id a run has until it is uploaded."""
    return f"local-{folder_stamp(started)}-{secrets.token_hex(2)}"


def default_run_dir(started: float, suite: str = "", base: Union[str, "os.PathLike[str]"] = ".") -> Path:
    """``<base>/embeddedci-runs/<started UTC>-<suite or "pytest">``."""
    return Path(base) / "embeddedci-runs" / f"{folder_stamp(started)}-{case_slug_simple(suite or 'pytest')}"


def case_slug_simple(text: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in text)[:80] or "pytest"


@dataclass
class _CaseState:
    case_id: Optional[str]
    calls: Dict[str, CallStats] = field(default_factory=dict)
    measurements: List[Measurement] = field(default_factory=list)
    file: Optional[IO[bytes]] = None
    trace_rel: str = ""
    bytes: int = 0
    att: List[Tuple[str, int]] = field(default_factory=list)


class RunRecording:
    """One run being recorded: its folder, the trace of the current case, call statistics, the
    measurements, and the pods it saw.

    ``keep`` is the trace policy (``all``, ``failed``, ``none``); ``t0`` the run's start in unix
    seconds (trace ``t`` counts from it; ``benchpod ci`` passes its own so child processes share one
    timeline). ``handover`` marks a child of ``benchpod ci``: it writes ``parts/<step>.json`` instead
    of ``run.json`` and ``junit.xml``.
    """

    def __init__(self, run_dir: Union[str, "os.PathLike[str]"], *, step: str = "pytest",
                 kind: str = "pytest", keep: str = "all", t0: Optional[float] = None,
                 handover: bool = False, run_id: str = "",
                 trace_cap: int = TRACE_MAX_BYTES_PER_RUN) -> None:
        if keep not in KEEP_POLICIES:
            raise ValueError(f"keep traces must be one of {', '.join(KEEP_POLICIES)}, got {keep!r}")
        self.run_dir = Path(run_dir).resolve()
        self.step = step
        self.kind = kind
        self.keep = keep
        self.handover = handover
        self.started = time.time()
        self.t0 = float(t0) if t0 is not None else self.started
        self.run_id = run_id or new_run_id(self.started)
        self.trace_cap = trace_cap
        self.health: Dict[str, Any] = empty_health()
        self.active = True
        self._lock = threading.RLock()
        self._current: Optional[_CaseState] = None
        self._session = _CaseState(None)
        self._cases: Dict[str, _CaseState] = {}
        self._bytes = 0
        self._att_n = 0
        self._used_traces: set = set()
        self.recorders: List[Recorder] = []
        self._pods: List[Dict[str, Any]] = []
        self._bench: List["weakref.ReferenceType[Any]"] = []
        self.wiring: Dict[str, Any] = provenance.wiring_info(None)
        self.run_dir.mkdir(parents=True, exist_ok=True)

    # -- time and state ------------------------------------------------------------------------

    def now(self) -> float:
        """Seconds since the run started (trace ``t``)."""
        return round(time.time() - self.t0, 3)

    def wants_payloads(self) -> bool:
        """Whether request and reply bodies are still written (not with ``keep=none``, not past the
        run's trace cap)."""
        return self.keep != "none" and not self.health["trace_truncated"]

    def add_health(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.health[key] = int(self.health.get(key, 0)) + n

    def add_reason(self, reason: str, *, invalid: bool = True) -> None:
        """A note on the run's health; ``invalid`` (default) marks the run as not a valid measurement."""
        with self._lock:
            self.health["reasons"].append(reason)
            if invalid:
                self.health["valid"] = False

    # -- cases ---------------------------------------------------------------------------------

    def begin_case(self, case_id: str) -> None:
        """Pod calls from now on belong to ``case_id`` (until :meth:`end_case`)."""
        with self._lock:
            self._close_file(self._current)
            state = self._cases.get(case_id)
            if state is None:
                state = _CaseState(case_id)
                self._cases[case_id] = state
            self._current = state

    def end_case(self, case_id: str, outcome: str) -> Tuple[str, Dict[str, CallStats]]:
        """Close ``case_id``'s trace and apply the keep policy for ``outcome``: the trace path
        relative to the run folder ("" when none was kept) and its per-command call statistics."""
        with self._lock:
            state = self._cases.get(case_id) or _CaseState(case_id)
            self._close_file(state)
            if self._current is state:
                self._current = None
            if state.trace_rel and self.keep == "failed" and outcome not in _KEPT_WHEN_FAILED:
                self._drop_files(state)
            self._snapshot_wiring()
            return state.trace_rel, dict(state.calls)

    def current_case(self) -> Optional[str]:
        state = self._current
        return state.case_id if state is not None else None

    def add_measurement(self, case_id: str, m: Measurement) -> bool:
        """Store a measurement for ``case_id``; False (and counted in ``health``) past the cap."""
        with self._lock:
            state = self._cases.get(case_id)
            if state is None:
                state = _CaseState(case_id)
                self._cases[case_id] = state
            if len(state.measurements) >= MEASUREMENTS_PER_CASE:
                self.health["measurements_dropped"] += 1
                return False
            state.measurements.append(m)
            return True

    def measurements(self, case_id: str) -> List[Measurement]:
        state = self._cases.get(case_id)
        return list(state.measurements) if state is not None else []

    def session_calls(self) -> Dict[str, CallStats]:
        """Call statistics of pod calls made outside every case."""
        return dict(self._session.calls)

    # -- pods ----------------------------------------------------------------------------------

    def attach(self, transport: Any) -> Recorder:
        """Record ``transport`` (idempotent). A transport recorded for an earlier run moves to this one."""
        with self._lock:
            existing = getattr(transport, "_embeddedci_recorder", None)
            if isinstance(existing, Recorder):
                if existing.run is self:
                    return existing
                existing.uninstall()
            rec = Recorder(self, transport).install()
            self.recorders.append(rec)
            return rec

    def note_pod(self, bench: Any) -> None:
        """Remember a connected :class:`~embeddedci.benchpod.BenchPod` for ``inputs.pod`` and
        ``inputs.wiring``. Reads the status (when no call has yet) and, on a pod that keeps one, the
        stored calibration; these reads are not part of any case's trace. Never raises."""
        try:
            transport = bench.transport
            rec = getattr(transport, "_embeddedci_recorder", None)
            with suppressed():
                status = rec.last_status if rec is not None and rec.last_status else None
                if status is None:
                    try:
                        status = bench.status()
                    except Exception:  # noqa: BLE001
                        status = {}
                calibration = None
                try:
                    if bench.capabilities.calibrate:
                        reply = bench.command({"cmd": "calibrate"})
                        if isinstance(reply, dict) and reply.get("calibrated") is True:
                            calibration = reply
                except Exception:  # noqa: BLE001 - provenance never fails a run
                    calibration = None
            block = provenance.pod_info(status or {}, transport=transport, name=_pod_name(bench, transport),
                                        calibration=calibration)
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            self._pods.append(block)
            self._bench.append(weakref.ref(bench))
            self._snapshot_wiring()

    def pod(self) -> Dict[str, Any]:
        """The first pod this run connected to ({} when none)."""
        return dict(self._pods[0]) if self._pods else {}

    def pods(self) -> List[Dict[str, Any]]:
        return [dict(p) for p in self._pods]

    def _snapshot_wiring(self) -> None:
        for ref in self._bench:
            bench = ref()
            wiring = getattr(bench, "_wiring", None) if bench is not None else None
            if wiring is not None:
                self.wiring = provenance.wiring_info(wiring)
                return

    # -- trace ---------------------------------------------------------------------------------

    def record(self, *, t: float, op: str, cmd: str, req: Any, ms: float, ok: bool, resp: Any = None,
               error: str = "", extra: Optional[Mapping[str, Any]] = None, recorder: Any = None) -> None:
        """One trace line for the current case (spec section 3) and its call statistics."""
        with self._lock:
            state = self._current or self._session
            state.calls.setdefault(cmd or op, CallStats()).add(ms, ok)
            if self.keep == "none":
                return
            ms_r = round(ms, 3)
            if self.health["trace_truncated"] or self._bytes >= self.trace_cap:
                self.health["trace_truncated"] = True
                line: Dict[str, Any] = {"t": t, "op": op, "cmd": cmd, "ms": ms_r, "ok": ok}
            else:
                line = {"t": t, "op": op}
                if cmd:
                    line["cmd"] = cmd
                big: Dict[str, Any] = {}
                req_r = redact(req) if req is not None else {}
                req_text = compact_json(req_r)
                if len(req_text.encode("utf-8")) <= INLINE_JSON_MAX_BYTES:
                    line["req"] = req_r
                else:
                    big["req"] = req_r
                line["ms"] = ms_r
                line["ok"] = ok
                for k, v in (extra or {}).items():
                    line[k] = v
                if ok and resp is not None:
                    resp_r = redact(resp)
                    resp_text = compact_json(resp_r)
                    if len(resp_text.encode("utf-8")) <= INLINE_JSON_MAX_BYTES:
                        line["resp"] = resp_r
                    else:
                        big["resp"] = resp_r
                if error:
                    line["error"] = error
                if big:
                    line["att"] = self._write_att(state, cmd or op, big)
            self._write_line(state, line)

    def _write_att(self, state: _CaseState, what: str, payload: Mapping[str, Any]) -> str:
        data = compact_json(payload).encode("utf-8")
        ext = ".json"
        if _zstd is not None:
            data = _zstd.ZstdCompressor().compress(data)
            ext = ".json.zst"
        att_dir = self.run_dir / "att"
        att_dir.mkdir(parents=True, exist_ok=True)
        safe = case_slug_simple(what)
        while True:
            self._att_n += 1
            rel = f"att/{self._att_n:04d}-{safe}{ext}"
            try:
                with open(self.run_dir / rel, "xb") as f:  # several processes share att/
                    f.write(data)
                break
            except FileExistsError:
                continue
        self._bytes += len(data)
        state.att.append((rel, len(data)))
        return rel

    def _write_line(self, state: _CaseState, line: Mapping[str, Any]) -> None:
        if state.file is None:
            if not self._open_trace(state):
                return
        assert state.file is not None
        data = (compact_json(line) + "\n").encode("utf-8")
        state.file.write(data)
        state.file.flush()
        state.bytes += len(data)
        self._bytes += len(data)

    def _open_trace(self, state: _CaseState) -> bool:
        trace_dir = self.run_dir / "trace"
        trace_dir.mkdir(parents=True, exist_ok=True)
        if state.trace_rel:  # reopened (a case whose trace was closed between phases)
            state.file = open(self.run_dir / state.trace_rel, "ab")
            return True
        stem = case_slug(state.case_id) if state.case_id is not None else SESSION_TRACE
        n = 1
        while True:
            name = stem if n == 1 else f"{stem}-{n}"
            rel = f"trace/{name}.ndjson"
            if rel not in self._used_traces:
                try:
                    state.file = open(self.run_dir / rel, "xb")  # another step may own the name
                    break
                except FileExistsError:
                    pass
            n += 1
        self._used_traces.add(rel)
        state.trace_rel = rel
        return True

    @staticmethod
    def _close_file(state: Optional[_CaseState]) -> None:
        if state is not None and state.file is not None:
            try:
                state.file.close()
            finally:
                state.file = None

    def _drop_files(self, state: _CaseState) -> None:
        for rel, size in [(state.trace_rel, state.bytes)] + state.att:
            try:
                os.unlink(self.run_dir / rel)
                self._bytes -= size
            except OSError:
                pass
        state.trace_rel = ""
        state.bytes = 0
        state.att = []

    # -- end -----------------------------------------------------------------------------------

    def finish(self) -> None:
        """Stop recording: later calls of recorded transports go straight through."""
        with self._lock:
            self.active = False
            self._close_file(self._current)
            self._close_file(self._session)
            self._current = None
            self._snapshot_wiring()
        clear_active_run(self)

    def write_run(self, *, cases: Sequence[Case], key: Mapping[str, Any], inputs: Mapping[str, Any],
                  suite: str, finished: Optional[float] = None) -> Dict[str, Any]:
        """Write ``junit.xml`` and then ``run.json`` (atomically, last). Returns the record."""
        end = finished if finished is not None else time.time()
        duration_ms = int(round((end - self.started) * 1000))
        record = build_run_record(run_id=self.run_id, key=key, trigger=provenance.trigger(),
                                  started=utc_stamp(self.started), finished=utc_stamp(end),
                                  inputs=inputs, health=self.health, cases=cases,
                                  duration_ms=duration_ms, keep=self.keep)
        write_bytes_atomic(self.run_dir / "junit.xml",
                           junit_xml(suite, cases, timestamp=utc_stamp(self.started), duration_ms=duration_ms))
        write_json_atomic(self.run_dir / "run.json", record)
        return record

    def write_part(self, *, cases: Sequence[Case], tools: Mapping[str, Any]) -> Dict[str, Any]:
        """Write ``parts/<step>.json`` for ``benchpod ci`` to merge. Returns the record."""
        record = build_part_record(step=self.step, cases=cases, health=self.health, tools=tools,
                                   pod=self.pod() or None)
        write_json_atomic(self.run_dir / "parts" / f"{case_slug_simple(self.step)}.json", record)
        return record


def _pod_name(bench: Any, transport: Any) -> str:
    name = str(getattr(bench, "_device_name", "") or "")
    if name:
        return name
    addr = str(getattr(transport, "addr", "") or "")
    host = addr.rsplit(":", 1)[0] if addr.count(":") == 1 else addr
    host = host.removesuffix(".local")
    return host if host.startswith("benchpod-") else ""
