#!/usr/bin/env python3
"""Print the lowest embeddedci version a package's pyproject.toml allows (its ``>=`` bound),
padded to three parts: ``embeddedci[...]>=2.6,<3`` prints ``2.6.0``.

CI installs exactly that release from PyPI and runs the package's tests against it, so raising
the pin moves the check along with it.

    python scripts/min_sdk_version.py packages/embeddedci-mcp/pyproject.toml
"""

from __future__ import annotations

import re
import sys

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None  # type: ignore[assignment]

_REQ = re.compile(r"^\s*embeddedci(\[[^\]]*\])?\s*(?P<spec>[^;]*)")


def _dependencies(path: str) -> list:
    if tomllib is not None:
        with open(path, "rb") as f:
            return list(tomllib.load(f)["project"]["dependencies"])
    # Without tomllib: the quoted strings inside the dependencies = [...] block.
    text = open(path, encoding="utf-8").read()
    block = re.search(r"^dependencies\s*=\s*\[(.*?)^\]", text, re.S | re.M)
    if not block:
        return []
    body = "\n".join(line.split("#", 1)[0] for line in block.group(1).splitlines())
    return re.findall(r'"([^"]+)"', body)


def min_version(path: str) -> str:
    for dep in _dependencies(path):
        m = _REQ.match(dep)
        if not m:
            continue
        for part in m.group("spec").split(","):
            part = part.strip()
            if part.startswith(">="):
                nums = part[2:].strip().split(".")
                return ".".join((nums + ["0", "0"])[:3])
    raise SystemExit(f"{path}: no embeddedci>=X dependency")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} <pyproject.toml>")
    print(min_version(sys.argv[1]))
