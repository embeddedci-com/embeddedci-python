"""Upload a run folder to embeddedci.com: ``POST /api/runs`` with ``run.json``, then its files with
``POST /api/runs/{id}/files?name=<path>``.

What goes up: ``run.json``, ``junit.xml``, the traces of the failed and error cases (the ``failed``
keep policy, whatever the folder kept locally) with the attachments those traces point to, and
``logs/``. The uploaded ``run.json`` says so: its ``keep.traces`` is ``failed`` (or ``none``) and the
cases whose trace stays local have ``trace: ""``. The folder on disk is not changed.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Union

from embeddedci import _http

from ..errors import BenchPodError

_HTTP_TIMEOUT = 60.0
#: The case outcomes whose traces are uploaded.
_UPLOADED_TRACES = frozenset({"failed", "error"})


class RunUploadError(BenchPodError):
    """Uploading a run failed (no credentials, the server refused, the network)."""


def upload_files(run_dir: Path, record: Dict[str, Any]) -> List[str]:
    """The files of ``run_dir`` to upload next to ``record``, as names relative to the folder."""
    names: List[str] = []
    if (run_dir / "junit.xml").is_file():
        names.append("junit.xml")
    for case in record.get("results", {}).get("cases", []) or []:
        trace = case.get("trace") or ""
        if not trace or case.get("outcome") not in _UPLOADED_TRACES:
            continue
        path = run_dir / trace
        if not path.is_file():
            continue
        names.append(trace)
        names.extend(sorted(_attachments(path) - set(names)))
    logs = run_dir / "logs"
    if logs.is_dir():
        names.extend(f"logs/{p.name}" for p in sorted(logs.iterdir()) if p.is_file())
    return [n for n in names if (run_dir / n).is_file()]


def _attachments(trace: Path) -> Set[str]:
    out: Set[str] = set()
    try:
        with trace.open("r", encoding="utf-8") as f:
            for line in f:
                if '"att"' not in line:
                    continue
                try:
                    att = json.loads(line).get("att")
                except ValueError:
                    continue
                if isinstance(att, str) and att.startswith("att/"):
                    out.add(att)
    except OSError:
        pass
    return out


def upload_record(record: Dict[str, Any], *, build_id: Optional[str] = None) -> Dict[str, Any]:
    """The ``run.json`` as uploaded: traces only for failed and error cases, and ``build_id`` (when
    given) in ``inputs.dut_firmware`` so the server links the run to that build."""
    doc = json.loads(json.dumps(record))
    keep = (doc.get("keep") or {}).get("traces", "all")
    if keep == "all":
        doc["keep"] = {"traces": "failed"}
    for case in doc.get("results", {}).get("cases", []) or []:
        if case.get("outcome") not in _UPLOADED_TRACES:
            case["trace"] = ""
    if build_id:
        inputs = doc.setdefault("inputs", {})
        firmware = inputs.get("dut_firmware")
        if not isinstance(firmware, dict):
            firmware = {}
        firmware["build_id"] = str(build_id)
        inputs["dut_firmware"] = firmware
    return doc


def _token(api_base: str) -> str:
    """A credential for the runs API: ``BENCHPOD_API_KEY``, else the ``benchpod login`` session,
    else GitHub Actions OIDC (see :func:`~embeddedci.benchpod.cloud_auth.mint_session_token`)."""
    from ..cloud_auth import mint_session_token

    try:
        return mint_session_token(api_base)[0]
    except BenchPodError as exc:
        raise RunUploadError(f"no credentials to upload the run: {exc}") from exc


def _request(url: str, token: str, body: bytes, content_type: str) -> Dict[str, Any]:
    from ..cloud_auth import USER_AGENT

    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": content_type,
        "Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with _http.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        msg = exc.read().decode("utf-8", "replace")
        try:
            msg = json.loads(msg).get("error", msg)
        except ValueError:
            pass
        raise RunUploadError(f"run upload failed (HTTP {exc.code}): {msg}") from exc
    except Exception as exc:  # noqa: BLE001 - network, TLS
        raise RunUploadError(f"run upload failed: {exc}") from exc
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def upload_run(run_dir: Union[str, "os.PathLike[str]"], *, api_base: Optional[str] = None,
               token: Optional[str] = None, build_id: Optional[str] = None,
               warn: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Upload the run folder ``run_dir``. Returns the server's ``{"id", "url", "build_id"}``.

    ``token`` is the credential (default: ``BENCHPOD_API_KEY``, else the ``benchpod login``
    session, else GitHub Actions OIDC); ``api_base`` the server (default ``BENCHPOD_API_BASE``,
    else ``https://www.embeddedci.com``). A file the server refuses is reported to ``warn`` and
    skipped; a refused ``run.json`` raises :class:`RunUploadError`."""
    from ..cloud_auth import DEFAULT_API_BASE

    folder = Path(run_dir)
    try:
        record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunUploadError(f"{folder}: no readable run.json ({exc})") from exc
    base = (api_base or os.environ.get("BENCHPOD_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    tok = token or _token(base)
    doc = upload_record(record, build_id=build_id)
    created = _request(f"{base}/api/runs", tok, json.dumps(doc).encode("utf-8"), "application/json")
    run_id = str(created.get("id") or "")
    if not run_id:
        raise RunUploadError("run upload: the server returned no run id")
    for name in upload_files(folder, doc):
        url = f"{base}/api/runs/{urllib.parse.quote(run_id, safe='')}/files?name={urllib.parse.quote(name, safe='')}"
        try:
            _request(url, tok, (folder / name).read_bytes(), "application/octet-stream")
        except (RunUploadError, OSError) as exc:
            if warn is not None:
                warn(f"{name}: {exc}")
    return created


def names_valid(names: Iterable[str]) -> bool:
    """Whether every name passes the server's file name rule (``run.json``, ``junit.xml``, or one
    level of ``trace/``, ``att/``, ``logs/`` with ``[A-Za-z0-9._-]`` names not starting with a dot,
    at most 200 characters)."""
    import re

    rule = re.compile(r"^(?:run\.json|junit\.xml|(?:trace|att|logs)/[A-Za-z0-9_-][A-Za-z0-9._-]*)$")
    return all(len(n) <= 200 and rule.match(n) for n in names)
