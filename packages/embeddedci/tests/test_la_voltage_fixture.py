"""The LA I/O voltage is configured once in conftest.py (the ``benchpod_la_voltage`` fixture),
with ``--benchpod-la-voltage`` as a per-run override."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from embeddedci.benchpod import pytest_plugin


@pytest.fixture
def opened(monkeypatch) -> Dict[str, Any]:
    seen: Dict[str, Any] = {}

    class FakePod:
        def __init__(self, connection, **kwargs):
            seen.update(kwargs, connection=connection)

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(pytest_plugin, "BenchPod", FakePod)
    monkeypatch.delenv("BENCHPOD_LA_VOLTAGE", raising=False)
    return seen


CONFTEST = """
import pytest

@pytest.fixture(scope="session")
def benchpod_la_voltage():
    return 1.8  # a 1V8 board
"""


def test_conftest_fixture_sets_the_voltage_once(pytester, opened):
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile("def test_uses_pod(benchpod):\n    pass\n")
    pytester.runpytest("--benchpod-connection=10.0.0.1").assert_outcomes(passed=1)
    assert opened["la_voltage"] == 1.8 and opened["closed"]


def test_command_line_flag_overrides_the_fixture(pytester, opened):
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile("def test_uses_pod(benchpod):\n    pass\n")
    pytester.runpytest("--benchpod-connection=10.0.0.1",
                       "--benchpod-la-voltage=3.3").assert_outcomes(passed=1)
    assert opened["la_voltage"] == 3.3


def test_default_leaves_it_to_the_environment(pytester, opened):
    pytester.makepyfile("def test_uses_pod(benchpod):\n    pass\n")
    pytester.runpytest("--benchpod-connection=10.0.0.1").assert_outcomes(passed=1)
    assert opened["la_voltage"] is None  # BenchPod then reads BENCHPOD_LA_VOLTAGE itself
