"""Check a release tag against the version in the source tree (used by publish.yml).

    python scripts/check_release_tag.py embeddedci-mcp-v2.6.0   # one tag
    python scripts/check_release_tag.py --consistency           # every package, no tag (CI)

A tag publishes the package its prefix names (see publish.yml). The publish fails unless the
tag's version equals that package's pyproject version, so a tag on the wrong commit, or a bump
that missed a file, never reaches PyPI. For embeddedci-mcp the MCP Registry manifest
(packages/embeddedci-mcp/server.json, two version fields) has to match too.
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Tag prefix -> package directory. Longest prefix first: "embeddedci-v" must not match
# "embeddedci-mcp-v2.0.0", and it does not, but keep the order explicit anyway.
PACKAGES = [
    ("embeddedci-py39-shim-v", "embeddedci-py39-shim"),
    ("embeddedci-openhtf-v", "embeddedci-openhtf"),
    ("embeddedci-mcp-v", "embeddedci-mcp"),
    ("embeddedci-v", "embeddedci"),
]

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+([.-]?(a|b|rc|post|dev)\d+)*$")


def pyproject_version(package, root=ROOT):
    """The static [project] version of packages/<package>/pyproject.toml."""
    path = os.path.join(root, "packages", package, "pyproject.toml")
    section = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            m = re.match(r"^\[([^\]]+)\]$", line)
            if m:
                section = m.group(1)
                continue
            if section == "project":
                m = re.match(r'^version\s*=\s*"([^"]+)"', line)
                if m:
                    return m.group(1)
    raise ValueError(f"{path}: no static [project] version")


def server_json_versions(root=ROOT):
    """(top-level version, PyPI package version) of the MCP Registry manifest."""
    path = os.path.join(root, "packages", "embeddedci-mcp", "server.json")
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    pkg = [p for p in doc.get("packages", []) if p.get("identifier") == "embeddedci-mcp"]
    if len(pkg) != 1:
        raise ValueError(f"{path}: expected one package with identifier embeddedci-mcp")
    return doc.get("version"), pkg[0].get("version")


def parse_tag(tag):
    """(package, version) for a release tag, or raise ValueError."""
    tag = tag.removeprefix("refs/tags/")
    for prefix, package in PACKAGES:
        if tag.startswith(prefix):
            version = tag[len(prefix):]
            if not VERSION_RE.match(version):
                raise ValueError(f"tag {tag}: {version!r} is not a version")
            return package, version
    raise ValueError(f"tag {tag}: no package uses this prefix")


def mcp_problems(root=ROOT):
    """Mismatches between embeddedci-mcp's pyproject and server.json."""
    want = pyproject_version("embeddedci-mcp", root)
    top, pkg = server_json_versions(root)
    problems = []
    if top != want:
        problems.append(f"server.json version {top} != pyproject {want}")
    if pkg != want:
        problems.append(f"server.json packages[embeddedci-mcp].version {pkg} != pyproject {want}")
    return problems


def check_tag(tag, root=ROOT):
    """A list of problems; empty when the tag matches the tree."""
    package, version = parse_tag(tag)
    have = pyproject_version(package, root)
    problems = []
    if have != version:
        problems.append(f"tag {tag} says {version}, packages/{package}/pyproject.toml says {have}")
    if package == "embeddedci-mcp":
        problems += mcp_problems(root)
    return problems


def main(argv):
    if argv == ["--consistency"]:
        problems = mcp_problems()
    elif len(argv) == 1:
        try:
            problems = check_tag(argv[0])
        except ValueError as e:
            problems = [str(e)]
    else:
        sys.exit(__doc__)
    for p in problems:
        print(f"check-release-tag: {p}", file=sys.stderr)
    if problems:
        return 1
    print("check-release-tag: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
