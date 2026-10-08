"""Tests for check_release_tag.py (run by `make test` and CI)."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_release_tag as crt  # noqa: E402


def write_tree(root, versions, server=("2.6.0", "2.6.0")):
    for package, version in versions.items():
        d = root / "packages" / package
        d.mkdir(parents=True)
        (d / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["hatchling"]\n\n'
            f'[project]\nname = "{package}"\nversion = "{version}"\n\n'
            '[tool.other]\nversion = "9.9.9"\n')
    mcp = root / "packages" / "embeddedci-mcp"
    mcp.mkdir(parents=True, exist_ok=True)
    (mcp / "server.json").write_text(json.dumps({
        "version": server[0],
        "packages": [{"identifier": "embeddedci-mcp", "version": server[1]}],
    }))


@pytest.fixture
def tree(tmp_path):
    write_tree(tmp_path, {"embeddedci": "2.6.0", "embeddedci-mcp": "2.6.0",
                          "embeddedci-openhtf": "2.1.0", "embeddedci-py39-shim": "0.2.4"})
    return tmp_path


@pytest.mark.parametrize("tag,package,version", [
    ("embeddedci-v2.6.0", "embeddedci", "2.6.0"),
    ("refs/tags/embeddedci-mcp-v2.6.0", "embeddedci-mcp", "2.6.0"),
    ("embeddedci-openhtf-v2.1.0rc1", "embeddedci-openhtf", "2.1.0rc1"),
    ("embeddedci-py39-shim-v0.2.4", "embeddedci-py39-shim", "0.2.4"),
])
def test_parse_tag(tag, package, version):
    assert crt.parse_tag(tag) == (package, version)


@pytest.mark.parametrize("tag", ["v0.1.0", "embeddedci-vX", "embeddedci-mcp-v2.6", "stm32-v3.6.0"])
def test_parse_tag_rejects(tag):
    with pytest.raises(ValueError):
        crt.parse_tag(tag)


def test_matching_tags_pass(tree):
    for tag in ("embeddedci-v2.6.0", "embeddedci-mcp-v2.6.0", "embeddedci-openhtf-v2.1.0",
                "embeddedci-py39-shim-v0.2.4"):
        assert crt.check_tag(tag, tree) == []


def test_wrong_version_fails(tree):
    problems = crt.check_tag("embeddedci-v2.7.0", tree)
    assert len(problems) == 1 and "2.7.0" in problems[0]


def test_mcp_server_json_must_match(tmp_path):
    write_tree(tmp_path, {"embeddedci-mcp": "2.7.0"}, server=("2.6.0", "2.7.0"))
    problems = crt.check_tag("embeddedci-mcp-v2.7.0", tmp_path)
    assert len(problems) == 1 and "server.json version 2.6.0" in problems[0]
    write_tree(tmp_path / "b", {"embeddedci-mcp": "2.7.0"}, server=("2.7.0", "2.6.0"))
    assert len(crt.mcp_problems(tmp_path / "b")) == 1


def test_real_tree_is_consistent():
    # The checked-in server.json must always follow the embeddedci-mcp version.
    assert crt.mcp_problems() == []
