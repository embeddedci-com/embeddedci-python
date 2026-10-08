"""The README's tool reference must keep up with the server: every tool has a row in the "Tools"
section, every parameter is named in that row, and no row names a tool the server lacks.

When this fails after adding a tool or parameter, add it to the README table (group, purpose,
parameters with their defaults). Leave something out on purpose only through UNDOCUMENTED."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

import anyio

from embeddedci_mcp.server import mcp

README = Path(__file__).resolve().parent.parent / "README.md"

#: Tool -> parameters deliberately left out of the README table ("*" = the whole tool).
UNDOCUMENTED: Dict[str, List[str]] = {}

_ROW = re.compile(r"^\| `([a-z0-9_]+)` \|(.*)$", re.M)


def _tools_section() -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index("\n## Tools\n")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def _rows() -> Dict[str, str]:
    return {m.group(1): m.group(2) for m in _ROW.finditer(_tools_section())}


def _server_tools() -> Dict[str, List[str]]:
    tools = anyio.run(mcp.list_tools)
    return {t.name: list(t.inputSchema.get("properties", {})) for t in tools}


def test_every_tool_has_a_readme_row():
    rows = _rows()
    missing = [n for n in _server_tools() if n not in rows and UNDOCUMENTED.get(n) != ["*"]]
    assert not missing, f"README.md Tools section has no row for: {', '.join(missing)}"


def test_readme_lists_no_unknown_tools():
    unknown = sorted(set(_rows()) - set(_server_tools()))
    assert not unknown, f"README.md lists tools the server does not have: {', '.join(unknown)}"


def test_every_parameter_is_named_in_its_row():
    rows = _rows()
    gaps = []
    for name, params in _server_tools().items():
        row = rows.get(name)
        if row is None:
            continue  # reported by test_every_tool_has_a_readme_row
        skip = set(UNDOCUMENTED.get(name, []))
        for p in params:
            if p not in skip and not re.search(r"`%s(=[^`]*)?`" % re.escape(p), row):
                gaps.append(f"{name}.{p}")
    assert not gaps, f"README.md does not name these parameters in the tool's row: {', '.join(gaps)}"


def test_allowlist_names_real_tools():
    tools = _server_tools()
    for name, params in UNDOCUMENTED.items():
        assert name in tools, f"UNDOCUMENTED names an unknown tool: {name}"
        unknown = [p for p in params if p != "*" and p not in tools[name]]
        assert not unknown, f"UNDOCUMENTED names unknown parameters of {name}: {unknown}"
