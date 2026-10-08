"""Every public name is documented.

The public surface is ``api_surface.json`` (kept in step with the code by ``test_api_surface.py``)
plus the pytest plugin's options, ini keys, markers, fixtures and environment variables. Each name
must appear inside a code span or code block of the package README (or the repo root README, or a
file under ``docs/``). A name counts wherever it appears, so a method shares a mention with any
other member of the same name; the check catches a name that is documented nowhere, not a wrong
description.

A name that is deliberately left out goes in ``ALLOWLIST`` with the reason.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Set

from embeddedci.benchpod import pytest_plugin

PKG = Path(__file__).resolve().parents[1]
REPO = PKG.parents[1]
SURFACE = PKG / "tests" / "api_surface.json"
SRC = PKG / "src" / "embeddedci"

#: name -> why it is not in the docs.
ALLOWLIST: Dict[str, str] = {
    **{f"PIN{n}": "documented as the range PIN1 … PIN14" for n in range(2, 14)},
}


def _doc_files() -> List[Path]:
    files = [PKG / "README.md", REPO / "README.md"]
    files += sorted((PKG / "docs").glob("*.md"))
    return [f for f in files if f.is_file()]


def _code_text() -> str:
    """The text of every fenced block and inline code span in the docs."""
    parts: List[str] = []
    for path in _doc_files():
        text = path.read_text(encoding="utf-8")
        fenced = re.compile(r"^```.*?^```", re.M | re.S)
        parts += fenced.findall(text)
        parts += re.findall(r"`([^`\n]+)`", fenced.sub("", text))
    return "\n".join(parts)


def _mentioned(name: str, text: str) -> bool:
    return re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text) is not None


def surface_names() -> Set[str]:
    """Every exported name, class member, dataclass field and enum member in the snapshot."""
    surface = json.loads(SURFACE.read_text())
    names: Set[str] = set(surface["__all__"])
    for value in surface.values():
        if not isinstance(value, dict):
            continue
        for member, detail in value.items():
            if member == "__fields__":
                names.update(detail)
            elif member == "__members__":
                names.update(detail)
            elif not member.startswith("_"):
                names.add(member)
    return names


class _Recorder:
    """Stands in for pytest's parser and config to collect what the plugin registers."""

    def __init__(self) -> None:
        self.names: Set[str] = set()

    def getgroup(self, *_a, **_k) -> "_Recorder":
        return self

    def addoption(self, *opts: str, **_k) -> None:
        self.names.update(o for o in opts if o.startswith("--"))

    def addini(self, name: str, *_a, **_k) -> None:
        self.names.add(name)

    def addinivalue_line(self, key: str, line: str) -> None:
        if key == "markers":
            self.names.add(re.match(r"[\w]+", line).group(0))


def _is_fixture(obj: object) -> bool:
    return type(obj).__name__ == "FixtureFunctionDefinition" or hasattr(obj, "_pytestfixturefunction")


def plugin_names() -> Set[str]:
    """The plugin's options, ini keys, markers, public fixtures and the BENCHPOD_* variables."""
    rec = _Recorder()
    pytest_plugin.pytest_addoption(rec)  # type: ignore[arg-type]
    pytest_plugin.pytest_configure(rec)  # type: ignore[arg-type]
    names = set(rec.names)
    names.update(n for n, obj in vars(pytest_plugin).items() if _is_fixture(obj) and not n.startswith("_"))
    for path in SRC.rglob("*.py"):
        names.update(re.findall(r"[\"'](BENCHPOD_[A-Z_]+)[\"']", path.read_text(encoding="utf-8")))
    return names


def _missing(names: Set[str]) -> List[str]:
    text = _code_text()
    return sorted(n for n in names if n not in ALLOWLIST and not _mentioned(n, text))


def test_plugin_names_are_found():
    names = plugin_names()
    assert {"--benchpod-connection", "benchpod_connection", "hardware", "benchpod",
            "benchpod_target", "BENCHPOD_CONNECTION"} <= names


def test_every_public_name_is_documented():
    missing = _missing(surface_names())
    assert not missing, (
        "public names missing from packages/embeddedci/README.md (document them, usually in the "
        "API reference section, or add them to ALLOWLIST with a reason): " + ", ".join(missing))


def test_every_pytest_plugin_name_is_documented():
    missing = _missing(plugin_names())
    assert not missing, (
        "pytest plugin options, fixtures, markers or BENCHPOD_* variables missing from "
        "packages/embeddedci/README.md: " + ", ".join(missing))


def test_allowlist_is_still_needed():
    stale = sorted(n for n in ALLOWLIST if n not in surface_names() | plugin_names())
    assert not stale, "ALLOWLIST names that are no longer public: " + ", ".join(stale)


def test_the_check_notices_a_missing_name():
    assert _missing({"definitely_not_documented_xyz"}) == ["definitely_not_documented_xyz"]
