"""Report a benchpod test run as an *external build* to embeddedci.com.

This is the optional, CI-only counterpart to the pytest hardware test: when a workflow opts in (by
requesting the ``build_report`` fixture) **and** it is running inside GitHub Actions with an OIDC
token available, the run is recorded on embeddedci.com as a GitHub-sourced build — its firmware
artifacts are uploaded and reused later (e.g. flashed from the web UI), and the pytest pass/fail is
captured as the build status.

Nothing here runs unless the fixture is requested *and* the GitHub OIDC token can be minted, so the
same test suite keeps running unchanged locally and in non-GitHub CI: the reporter degrades to a
no-op. The provider-agnostic server API means a GitLab equivalent would only add a sibling reporter,
not change the test.
"""

from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import warnings
from typing import Any, Dict, Iterable, List, Optional

from .cloud_auth import DEFAULT_API_BASE, DEFAULT_AUDIENCE, USER_AGENT, get_session_token
from .errors import CloudAuthError

_HTTP_TIMEOUT = 30.0

# Friendly content types for the firmware formats we upload; everything else falls back to a guess.
_CONTENT_TYPES = {
    ".elf": "application/octet-stream",
    ".bin": "application/octet-stream",
    ".hex": "application/octet-stream",
    ".map": "text/plain",
}


def github_build_metadata() -> Dict[str, str]:
    """Collect the GitHub Actions run metadata from the standard ``GITHUB_*`` env vars."""
    repository = os.environ.get("GITHUB_REPOSITORY", "")  # "owner/repo"
    owner, _, repo = repository.partition("/")
    return {
        "repo_owner": os.environ.get("GITHUB_REPOSITORY_OWNER", owner),
        "repo": repo or repository,
        "ref": os.environ.get("GITHUB_REF_NAME") or os.environ.get("GITHUB_REF", ""),
        "sha": os.environ.get("GITHUB_SHA", ""),
        "event": os.environ.get("GITHUB_EVENT_NAME", ""),
        "workflow": os.environ.get("GITHUB_WORKFLOW", ""),
        "actor": os.environ.get("GITHUB_ACTOR", ""),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
    }


def running_in_github_actions() -> bool:
    """True when executing inside a GitHub Actions runner."""
    return os.environ.get("GITHUB_ACTIONS") == "true"


class BuildReporter:
    """Records the current test run as an external build on embeddedci.com.

    The build is created lazily on first use (artifact upload, wiring, or finalize), so a test that
    requests the fixture but never reports anything does not create an empty build. All methods are
    no-ops on the :class:`NoopBuildReporter` returned when reporting is not active.
    """

    def __init__(
        self,
        *,
        api_base: str,
        session_token: str,
        metadata: Dict[str, str],
        target: str = "",
        name: str = "",
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._token = session_token
        self._metadata = metadata
        self._target = target
        self._name = name
        self._build_id: Optional[str] = None
        self._wiring: Optional[Dict[str, Any]] = None
        self._finalized = False

    @property
    def active(self) -> bool:
        return True

    @property
    def build_id(self) -> Optional[str]:
        return self._build_id

    # -- public API used by tests -------------------------------------------------

    def record_wiring(
        self,
        *,
        target: Optional[str] = None,
        swclk: Optional[int] = None,
        swdio: Optional[int] = None,
        nreset: Optional[int] = None,
        efuse: Optional[int] = None,
    ) -> None:
        """Record the flash wiring used, so the web UI flash modal can pre-fill these defaults."""
        wiring: Dict[str, Any] = {}
        if target is not None:
            wiring["target"] = target
        if swclk is not None:
            wiring["swclk"] = int(swclk)
        if swdio is not None:
            wiring["swdio"] = int(swdio)
        if nreset is not None:
            wiring["nreset"] = int(nreset)
        if efuse is not None:
            wiring["efuse"] = int(efuse)
        if wiring:
            self._wiring = {**(self._wiring or {}), **wiring}

    def upload_artifacts(self, paths: Iterable[str]) -> None:
        """Upload one or more firmware artifacts (elf/bin/hex/...) to the build."""
        for path in paths:
            self.upload_artifact(path)

    def upload_artifact(self, path: str) -> None:
        """Upload a single firmware artifact to the build."""
        build_id = self._ensure_build()
        if not build_id:
            return
        name = os.path.basename(path)
        with open(path, "rb") as fh:
            data = fh.read()
        content_type = _CONTENT_TYPES.get(
            os.path.splitext(name)[1].lower()
        ) or (mimetypes.guess_type(name)[0] or "application/octet-stream")
        url = (
            f"{self._api_base}/api/cloud/builds/{build_id}/artifacts"
            f"?name={urllib.parse.quote(name, safe='')}"
            f"&content_type={urllib.parse.quote(content_type, safe='')}"
        )
        self._request("POST", url, body=data, content_type=content_type)

    def set_result(self, success: bool, reason: str = "") -> None:
        """Record the test outcome; sent to the server by :meth:`finalize`."""
        self._result = (bool(success), reason)

    def finalize(self) -> None:
        """Create the build if needed and post the final pytest status. Idempotent."""
        if self._finalized:
            return
        self._finalized = True
        build_id = self._ensure_build()
        if not build_id:
            return
        success, reason = getattr(self, "_result", (True, ""))
        payload: Dict[str, Any] = {"success": success, "reason": reason}
        if self._wiring:
            payload["wiring"] = self._wiring
        try:
            self._request(
                "POST",
                f"{self._api_base}/api/cloud/builds/{build_id}/status",
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        except CloudAuthError as exc:
            warnings.warn(f"embeddedci: failed to report build status: {exc}", stacklevel=2)

    # -- internals ---------------------------------------------------------------

    def _ensure_build(self) -> Optional[str]:
        if self._build_id is not None:
            return self._build_id
        payload: Dict[str, Any] = dict(self._metadata)
        if self._target:
            payload["target"] = self._target
        if self._name:
            payload["name"] = self._name
        if self._wiring:
            payload["wiring"] = self._wiring
        try:
            resp = self._request(
                "POST",
                f"{self._api_base}/api/cloud/builds",
                body=json.dumps(payload).encode("utf-8"),
                content_type="application/json",
            )
        except CloudAuthError as exc:
            warnings.warn(f"embeddedci: failed to create build: {exc}", stacklevel=2)
            return None
        self._build_id = (resp or {}).get("build_id")
        return self._build_id

    def _request(
        self, method: str, url: str, *, body: bytes, content_type: str
    ) -> Optional[dict]:
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": content_type,
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            msg = exc.read().decode("utf-8", "replace")
            try:
                msg = json.loads(msg).get("error", msg)
            except Exception:
                pass
            raise CloudAuthError(f"build API call failed (HTTP {exc.code}): {msg}") from exc
        except Exception as exc:  # network/parse
            raise CloudAuthError(f"build API call failed: {exc}") from exc


class NoopBuildReporter:
    """Inert reporter returned when build reporting is not active (local runs, non-GitHub CI)."""

    active = False
    build_id = None

    def record_wiring(self, **_kwargs: Any) -> None:  # noqa: D401 - no-op
        pass

    def upload_artifacts(self, _paths: Iterable[str]) -> None:
        pass

    def upload_artifact(self, _path: str) -> None:
        pass

    def set_result(self, _success: bool, _reason: str = "") -> None:
        pass

    def finalize(self) -> None:
        pass


def make_build_reporter(
    *,
    api_base: Optional[str] = None,
    audience: Optional[str] = None,
    target: str = "",
    name: str = "",
) -> Any:
    """Build the reporter for the current environment.

    Returns a live :class:`BuildReporter` only inside GitHub Actions with a mintable OIDC token;
    otherwise returns a :class:`NoopBuildReporter` (and warns if a token was expected but failed),
    so requesting the fixture never breaks a local or non-GitHub run.
    """
    if not running_in_github_actions():
        return NoopBuildReporter()
    base = api_base or os.environ.get("BENCHPOD_API_BASE") or DEFAULT_API_BASE
    aud = audience or DEFAULT_AUDIENCE
    try:
        token = get_session_token(base, aud)
    except CloudAuthError as exc:
        warnings.warn(
            f"embeddedci: build reporting requested but no session token available: {exc}",
            stacklevel=2,
        )
        return NoopBuildReporter()
    return BuildReporter(
        api_base=base,
        session_token=token,
        metadata=github_build_metadata(),
        target=target,
        name=name,
    )
