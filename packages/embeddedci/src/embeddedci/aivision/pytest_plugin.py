"""pytest plugin for the AI Vision board: the ``aivision`` fixture and failure evidence.

Registered automatically (entry point ``aivision``). Nothing happens unless a test asks for the
fixture. Configure the board with ``--aivision <connection>`` (or ``AIVISION_CONNECTION``); a test
that needs the board is skipped when none is set.

When a test that used the fixture fails, the plugin saves what the camera saw (the rectified
screen when calibrated, else the camera frame) into ``--aivision-artifacts`` (default
``aivision-artifacts/``) and names the file in the test report.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterator, Optional

import pytest

from .client import ENV_CONNECTION, ENV_TOKEN, AIVision
from .errors import AIVisionError

ARTIFACTS_DEFAULT = "aivision-artifacts"


def pytest_addoption(parser: "pytest.Parser") -> None:
    group = parser.getgroup("aivision", "EmbeddedCI AI Vision board options")
    group.addoption("--aivision", action="store", default=None, dest="aivision",
                    help=f"AI Vision board: host[:port], URL, or 'discover' (mDNS). Falls back to {ENV_CONNECTION}.")
    group.addoption("--aivision-token", action="store", default=None, dest="aivision_token",
                    help=f"API token of the board (falls back to {ENV_TOKEN}).")
    group.addoption("--aivision-lease", action="store", default="pytest", dest="aivision_lease",
                    help="Take the board's lease under this name for the session ('' = no lease).")
    group.addoption("--aivision-artifacts", action="store", default=ARTIFACTS_DEFAULT, dest="aivision_artifacts",
                    help="Directory for the screen images saved when a test fails.")
    parser.addini("aivision", "Default AI Vision connection (like --aivision).", default="")


def _connection(config: "pytest.Config") -> str:
    return str(config.getoption("aivision") or config.getini("aivision")
               or os.environ.get(ENV_CONNECTION) or "").strip()


@pytest.fixture(scope="session")
def aivision_connection(pytestconfig: "pytest.Config") -> str:
    """The configured AI Vision connection, or skip the test if none is set."""
    conn = _connection(pytestconfig)
    if not conn:
        pytest.skip(f"no AI Vision board configured; pass --aivision=... or set {ENV_CONNECTION}")
    return conn


@pytest.fixture(scope="session")
def aivision(pytestconfig: "pytest.Config", aivision_connection: str) -> Iterator[AIVision]:
    """An open :class:`~embeddedci.aivision.AIVision` for the session, holding the board's lease."""
    lease = pytestconfig.getoption("aivision_lease") or None
    with AIVision(aivision_connection, token=pytestconfig.getoption("aivision_token"), lease=lease) as v:
        yield v


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: "pytest.Item", call: "pytest.CallInfo"):  # type: ignore[name-defined]
    outcome = yield
    rep = outcome.get_result()
    if rep.when != "call" or not rep.failed or "aivision" not in getattr(item, "fixturenames", ()):
        return
    funcargs = getattr(item, "funcargs", {})
    v: Optional[AIVision] = funcargs.get("aivision")
    if v is None:
        return
    path = _save_failure_frame(v, Path(item.config.getoption("aivision_artifacts")), item.nodeid)
    if path is not None:
        rep.sections.append(("aivision", f"screen at failure: {path}"))
        item.user_properties.append(("aivision_screen", str(path)))


def _save_failure_frame(v: AIVision, folder: Path, nodeid: str) -> Optional[Path]:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", nodeid).strip("_")[:120] or "test"
    try:
        calibrated = v.screen() is not None
        img = v.frame(space="screen" if calibrated else "camera", res=None if calibrated else "preview")
        folder.mkdir(parents=True, exist_ok=True)
        return img.save(folder / f"{name}.jpg")
    except (AIVisionError, OSError):
        return None
