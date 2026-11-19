"""What a run records around its results: git state of the tests, tool versions, GitHub run, the
DUT firmware file and the pod (``inputs`` in ``run.json``). Every helper returns empty values
instead of raising: provenance never fails a run."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .schema import empty_pod, sha256_of_json

_GIT_TIMEOUT = 5.0


def _git(cwd: str, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                             timeout=_GIT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def repo_slug(remote_url: str) -> str:
    """``owner/repo`` from a git remote URL (https or ssh); "" when it does not look like one (the
    URL itself is never recorded: it can carry credentials)."""
    m = re.search(r"[:/]([^/:@]+/[^/:@]+?)(?:\.git)?/?$", (remote_url or "").strip())
    return m.group(1) if m else ""


def git_info(cwd: str) -> Dict[str, Any]:
    """``{"repo", "ref", "sha", "dirty"}`` of the git checkout at ``cwd`` (empty when not one)."""
    info: Dict[str, Any] = {"repo": "", "ref": "", "sha": "", "dirty": False}
    sha = _git(cwd, "rev-parse", "HEAD")
    if not sha:
        return info
    info["sha"] = sha
    ref = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or ""
    info["ref"] = "" if ref == "HEAD" else ref
    if not info["ref"]:
        info["ref"] = os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME", "")
    info["repo"] = repo_slug(_git(cwd, "config", "--get", "remote.origin.url") or "")
    if not info["repo"]:
        info["repo"] = os.environ.get("GITHUB_REPOSITORY", "")
    status = _git(cwd, "status", "--porcelain", "--untracked-files=no")
    info["dirty"] = bool(status)
    return info


def tools_info(pytest_version: str = "") -> Dict[str, str]:
    """Tool versions: ``cli`` (from ``EMBEDDEDCI_CLI_VERSION`` when the CLI sets it), ``sdk``,
    ``python``, ``pytest`` and ``os`` (``darwin-arm64``, ``linux-x86_64``)."""
    from ... import __version__ as sdk_version

    machine = platform.machine().lower() or "unknown"
    return {
        "cli": os.environ.get("EMBEDDEDCI_CLI_VERSION", ""),
        "sdk": sdk_version,
        "python": platform.python_version(),
        "pytest": pytest_version,
        "os": f"{sys.platform}-{machine}",
    }


def github_info() -> Dict[str, str]:
    """The GitHub Actions run, from the standard ``GITHUB_*`` variables (empty elsewhere)."""
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return {
        "run_id": run_id,
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "run_url": f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else "",
        "workflow": os.environ.get("GITHUB_WORKFLOW", ""),
        "actor": os.environ.get("GITHUB_ACTOR", ""),
    }


def trigger() -> str:
    """``github`` inside GitHub Actions, ``other`` in another CI, else ``manual``."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "github"
    if os.environ.get("CI", "").lower() in ("1", "true", "yes"):
        return "other"
    return "manual"


def firmware_info(path: Optional[str]) -> Dict[str, Any]:
    """``{"file", "sha256", "size", "build_id", "version"}`` of a DUT firmware file ({} when none)."""
    if not path:
        return {}
    p = Path(path)
    out: Dict[str, Any] = {"file": p.name, "sha256": "", "size": 0, "build_id": "", "version": ""}
    try:
        h = hashlib.sha256()
        with p.open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        out["sha256"] = h.hexdigest()
        out["size"] = p.stat().st_size
    except OSError:
        pass
    return out


def connection_kind(transport: Any) -> str:
    """``lan`` | ``usb_net`` | ``cloud`` | ``serial`` for a transport instance."""
    from ..transport.cloud import CloudTransport
    from ..transport.serial import SerialTransport

    if isinstance(transport, CloudTransport):
        return "cloud"
    if isinstance(transport, SerialTransport):
        return "serial"
    if getattr(transport, "usb_net", False):
        return "usb_net"
    return "lan"


def board_label(status: Mapping[str, Any]) -> str:
    """The board as its silkscreen tag names it (``analog-v3``, ``digital-v3``), from the status
    report: ``board_tag`` when the firmware sends one, else the front end plus ``board_rev``, else
    the firmware's ``board``."""
    tag = str(status.get("board_tag") or "").strip()
    if tag:
        return tag
    rev = str(status.get("board_rev") or "").strip()
    if rev and rev != "unknown":
        kind = "digital" if status.get("analog") is False else "analog"
        return f"{kind}-{rev}"
    return str(status.get("board") or "")


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def pod_info(status: Mapping[str, Any], *, transport: Any = None, name: str = "",
             calibration: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The ``inputs.pod`` block from a status report: name, board, firmware, gateware,
    features_rev, calibration_sha256 (of ``calibration``, "" without one) and connection kind."""
    pod = empty_pod()
    pod["name"] = (name or str(status.get("name") or status.get("hostname") or "")
                   or str(getattr(transport, "device_name", "") or ""))
    pod["board"] = board_label(status)
    pod["firmware"] = str(status.get("version", status.get("firmware_version", "")) or "")
    pod["gateware"] = _int(status.get("gateware"))
    pod["features_rev"] = _int(status.get("features_rev"))
    if calibration:
        pod["calibration_sha256"] = sha256_of_json(dict(calibration))
    pod["connection"] = connection_kind(transport) if transport is not None else ""
    return pod


def wiring_info(wiring: Any) -> Dict[str, Any]:
    """``{"name", "sha256", "snapshot"}`` of a :class:`~embeddedci.benchpod.wiring.Wiring`."""
    if wiring is None:
        return {"name": "", "sha256": "", "snapshot": {}}
    try:
        snapshot = wiring.to_dict()
    except Exception:  # noqa: BLE001
        return {"name": "", "sha256": "", "snapshot": {}}
    return {"name": str(getattr(wiring, "profile_name", "") or ""), "sha256": sha256_of_json(snapshot),
            "snapshot": snapshot}
