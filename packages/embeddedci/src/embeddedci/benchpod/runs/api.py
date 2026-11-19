"""Read test runs back from embeddedci.com: list them, read one, compare two, fetch a run's files.

::

    from embeddedci.benchpod.runs import api

    runs = api.list_runs(suite="process-node-hil", limit=5)["runs"]
    run = api.get_run(runs[0]["id"])
    print(api.summarize_run(run)["failed_cases"])
    print(api.compare_runs(run["id"], "previous")["cases"]["newly_failing"])
    for line in api.case_trace(run, "tests/test_can.py::test_ack", errors_only=True)["lines"]:
        print(line)

Credentials: ``BENCHPOD_API_KEY``, else the ``benchpod login`` session, else GitHub Actions OIDC.
:class:`RunsApi` takes a :class:`~embeddedci.benchpod.server_api.ServerApi` for anything else.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from ..server_api import ServerApi

#: How many runs one list call returns at most (the server's cap).
LIST_MAX = 200
#: A trace line's ``req``/``resp`` longer than this (as JSON) is shortened in :func:`trace_lines`.
TRACE_FIELD_MAX = 300


def default_server(api_base: Optional[str] = None, api_key: Optional[str] = None) -> ServerApi:
    """A server client: ``api_key`` / ``BENCHPOD_API_KEY``, else the ``benchpod login`` session,
    else (in GitHub Actions) a session token from the job's OIDC token."""
    from ..cloud_auth import DEFAULT_API_BASE, login_session_token, mint_session_token

    base = (api_base or os.environ.get("BENCHPOD_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    key = (api_key or os.environ.get("BENCHPOD_API_KEY") or "").strip()
    if key:
        return ServerApi(api_base=base, api_key=key)
    login = login_session_token(base)
    if login is not None:
        return ServerApi(api_base=base, token_provider=login)
    token = mint_session_token(base)[0]  # GitHub Actions OIDC, else CloudAuthError listing the options
    return ServerApi(api_base=base, token_provider=lambda: token)


class RunsApi:
    """The runs API of an embeddedci server (``/api/runs``)."""

    def __init__(self, server: Optional[ServerApi] = None) -> None:
        self.server = server if server is not None else default_server()

    def list_runs(self, *, suite: Optional[str] = None, dut: Optional[str] = None,
                  build: Optional[str] = None, pod: Optional[str] = None, ref: Optional[str] = None,
                  limit: int = 20, before: Optional[str] = None) -> Dict[str, Any]:
        """Runs newest first: ``{"runs": [...], "next_before": ..., "org_pods": N}``. ``pod`` is a
        pod's name or id; ``before`` the ``next_before`` of the previous page."""
        query = {"suite": suite or None, "dut": dut or None, "build": build or None,
                 "device": pod or None, "ref": ref or None,
                 "limit": max(1, min(int(limit), LIST_MAX)), "before": before or None}
        _, data = self.server.request("GET", "/runs", query=query)
        data = dict(data or {})
        data.setdefault("runs", [])
        return data

    def get_run(self, run_id: str) -> Dict[str, Any]:
        """One run as stored: the run.json document with ``id``, ``url``, ``build_id``, ``files``."""
        _, data = self.server.request("GET", f"/runs/{quote(run_id, safe='')}")
        return dict(data or {})

    def compare_runs(self, run_id: str, with_: str = "previous") -> Dict[str, Any]:
        """The diff of an earlier run (``a``) against ``run_id`` (``b``). ``with_``: ``previous``
        (the newest earlier valid run of the same suite and DUT), a run id, or
        ``baseline:<branch>``. ``found`` is false when there is nothing to compare with."""
        _, data = self.server.request("GET", f"/runs/{quote(run_id, safe='')}/compare",
                                      query={"with": with_ or "previous"})
        return dict(data or {})

    def list_run_files(self, run_id: str) -> List[Dict[str, Any]]:
        """The run's uploaded files: ``name``, ``size_bytes``, ``expires_at``, ``expired``."""
        _, data = self.server.request("GET", f"/runs/{quote(run_id, safe='')}/files")
        return list((data or {}).get("files") or [])

    def get_run_file(self, run_id: str, name: str) -> bytes:
        """One uploaded file of the run (``junit.xml``, ``trace/<case>.ndjson``, ``att/...``)."""
        _, raw = self.server.request("GET", f"/runs/{quote(run_id, safe='')}/files",
                                     query={"name": name}, parse_json=False)
        return bytes(raw or b"")

    def case_trace(self, run: Any, case_id: str, *, errors_only: bool = False,
                   limit: int = 200) -> Dict[str, Any]:
        """The pod-call trace of one case (``run``: a run id or a :meth:`get_run` document), as
        trimmed lines: see :func:`trace_lines`. Raises ``LookupError`` when the run has no such
        case, or no trace was uploaded for it (only failed and error cases' traces are)."""
        doc = self.get_run(run) if isinstance(run, str) else run
        case = find_case(doc, case_id)
        trace = str(case.get("trace") or "")
        if not trace:
            raise LookupError(f"no trace was uploaded for {case['id']} ({case.get('outcome')}): only "
                              "failed and error cases' traces are uploaded")
        data = self.get_run_file(str(doc.get("id") or doc.get("run_id")), trace)
        out = trace_lines(data, errors_only=errors_only, limit=limit)
        out["case"] = case["id"]
        out["trace"] = trace
        return out


# -- helpers on the documents ------------------------------------------------------------------------

def find_case(run: Dict[str, Any], case_id: str) -> Dict[str, Any]:
    """The case ``case_id`` of a run document: an exact id, else the one case whose id ends with it
    (``test_ack`` finds ``tests/test_can.py::test_ack``)."""
    cases = list((run.get("results") or {}).get("cases") or [])
    for c in cases:
        if c.get("id") == case_id:
            return c
    tail = [c for c in cases if str(c.get("id", "")).endswith(case_id)]
    if len(tail) == 1:
        return tail[0]
    if tail:
        raise LookupError(f"{case_id!r} matches {len(tail)} cases: "
                          + ", ".join(str(c.get("id")) for c in tail[:5]))
    raise LookupError(f"the run has no case {case_id!r}")


def first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:500]
    return ""


def summarize_run(run: Dict[str, Any]) -> Dict[str, Any]:
    """The essentials of a run document: who and what, outcome and counts, health, the failed and
    error cases with the first line of their message, and every measurement outside its limits."""
    results = run.get("results") or {}
    inputs = run.get("inputs") or {}
    health = run.get("health") or {}
    failed = []
    out_of_limits = []
    for c in results.get("cases") or []:
        if c.get("outcome") in ("failed", "error"):
            failed.append({"id": c.get("id"), "outcome": c.get("outcome"),
                           "message": first_line(str(c.get("message") or "")),
                           "trace": bool(c.get("trace"))})
        for m in c.get("measurements") or []:
            if m.get("outcome") == "failed":
                out_of_limits.append({"case": c.get("id"), **{k: m.get(k) for k in
                                      ("name", "value", "unit", "min", "max")}})
    pod = inputs.get("pod") or {}
    return {
        "id": run.get("id") or run.get("run_id"),
        "url": run.get("url"),
        "suite": (run.get("key") or {}).get("suite"),
        "dut": (run.get("key") or {}).get("dut"),
        "outcome": results.get("outcome"),
        "counts": results.get("counts") or {},
        "duration_ms": results.get("duration_ms"),
        "started": run.get("started"),
        "trigger": run.get("trigger"),
        "build_id": run.get("build_id") or (inputs.get("dut_firmware") or {}).get("build_id") or "",
        "pod": {k: pod.get(k) for k in ("name", "board", "firmware", "gateware", "connection")},
        "tests": inputs.get("tests") or {},
        "valid": health.get("valid", True),
        "health_reasons": list(health.get("reasons") or []),
        "failed_cases": failed,
        "measurements_out_of_limits": out_of_limits,
    }


def _short(value: Any) -> Any:
    text = json.dumps(value, separators=(",", ":"), default=str)
    if len(text) <= TRACE_FIELD_MAX:
        return value
    return f"<{len(text)} bytes: {text[:TRACE_FIELD_MAX]}...>"


def trace_lines(data: bytes, *, errors_only: bool = False, limit: int = 200) -> Dict[str, Any]:
    """A trace file's lines (NDJSON) as objects, ``req``/``resp`` over 300 characters shortened:
    ``{"lines": [...], "total": N, "errors": N, "shown": N, "truncated": bool}``. ``errors_only``
    keeps the calls that failed; ``limit`` caps how many are returned (the first ones)."""
    lines: List[Dict[str, Any]] = []
    total = 0
    errors = 0
    for raw in data.decode("utf-8", "replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        total += 1
        failed = obj.get("ok") is False
        errors += failed
        if errors_only and not failed:
            continue
        if len(lines) < max(0, int(limit)):
            lines.append({k: (_short(v) if k in ("req", "resp") else v) for k, v in obj.items()})
    shown_pool = errors if errors_only else total
    return {"lines": lines, "total": total, "errors": errors, "shown": len(lines),
            "truncated": len(lines) < shown_pool}


# -- module-level shortcuts --------------------------------------------------------------------------

def list_runs(**kwargs: Any) -> Dict[str, Any]:
    """:meth:`RunsApi.list_runs` with the default credentials."""
    return RunsApi().list_runs(**kwargs)


def get_run(run_id: str) -> Dict[str, Any]:
    """:meth:`RunsApi.get_run` with the default credentials."""
    return RunsApi().get_run(run_id)


def compare_runs(run_id: str, with_: str = "previous") -> Dict[str, Any]:
    """:meth:`RunsApi.compare_runs` with the default credentials."""
    return RunsApi().compare_runs(run_id, with_)


def get_run_file(run_id: str, name: str) -> bytes:
    """:meth:`RunsApi.get_run_file` with the default credentials."""
    return RunsApi().get_run_file(run_id, name)


def case_trace(run: Any, case_id: str, *, errors_only: bool = False, limit: int = 200) -> Dict[str, Any]:
    """:meth:`RunsApi.case_trace` with the default credentials."""
    return RunsApi().case_trace(run, case_id, errors_only=errors_only, limit=limit)
