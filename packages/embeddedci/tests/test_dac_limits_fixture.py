"""The session fixture that lifts the pod's DAC output limits for a hardware run
(``BENCHPOD_LIFT_DAC_LIMITS=1``) and puts the same limits back afterwards, even when tests fail."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import pytest_plugin
from embeddedci.benchpod.errors import FirmwareError

LIMITS = {"enabled": True, "path": "5v", "inverted": True, "min_mv": 300, "max_mv": 4200}


@pytest.fixture
def pod(monkeypatch) -> Dict[str, Any]:
    """A fake BenchPod recording every dac_limits command across all the connections it opens."""
    state: Dict[str, Any] = {"limits": dict(LIMITS), "sent": [], "opened": 0, "refuse": False}

    class FakePod:
        def __init__(self, connection, **kwargs):
            state["opened"] += 1
            state["connection"] = connection
            state["kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def close(self):
            pass

        def command(self, req):
            sent: List[dict] = state["sent"]
            sent.append(dict(req))
            if state["refuse"]:
                raise FirmwareError("unknown command: dac_limits", cmd="dac_limits")
            if len(req) == 1:
                return dict(state["limits"])
            if req.get("enabled") is False:
                state["limits"]["enabled"] = False
            else:
                state["limits"] = dict(req, enabled=True)
                del state["limits"]["cmd"]
            return dict(state["limits"])

    monkeypatch.setattr(pytest_plugin, "BenchPod", FakePod)
    monkeypatch.delenv("BENCHPOD_LA_VOLTAGE", raising=False)
    monkeypatch.delenv(pytest_plugin.ENV_VAR, raising=False)
    return state


FAILING = "def test_fails():\n    assert False\n"


def test_without_the_variable_the_pod_is_not_touched(pytester, pod, monkeypatch):
    monkeypatch.delenv(pytest_plugin.LIFT_DAC_LIMITS_ENV, raising=False)
    pytester.makepyfile("def test_x():\n    pass\n")
    pytester.runpytest("--benchpod-connection=10.0.0.1").assert_outcomes(passed=1)
    assert pod["opened"] == 0


def test_without_a_connection_nothing_happens(pytester, pod, monkeypatch):
    monkeypatch.setenv(pytest_plugin.LIFT_DAC_LIMITS_ENV, "1")
    pytester.makepyfile("def test_x():\n    pass\n")
    pytester.runpytest().assert_outcomes(passed=1)
    assert pod["opened"] == 0


def test_limits_are_cleared_for_the_run_and_restored_after_a_failure(pytester, pod, monkeypatch):
    monkeypatch.setenv(pytest_plugin.LIFT_DAC_LIMITS_ENV, "1")
    pytester.makepyfile(FAILING)
    pytester.runpytest("--benchpod-connection=10.0.0.1").assert_outcomes(failed=1)
    assert pod["sent"][0] == {"cmd": "dac_limits"}
    assert pod["sent"][1] == {"cmd": "dac_limits", "enabled": False}
    assert pod["sent"][-1] == {"cmd": "dac_limits", "path": "5v", "inverted": True,
                               "min_mv": 300, "max_mv": 4200}
    assert pod["limits"] == LIMITS  # the pod ends where it started
    assert pod["connection"] == "10.0.0.1"


def test_tests_see_the_limits_off(pytester, pod, monkeypatch):
    monkeypatch.setenv(pytest_plugin.LIFT_DAC_LIMITS_ENV, "1")
    pytester.makepyfile("""
        from embeddedci.benchpod import pytest_plugin

        def test_limits_are_off():
            with pytest_plugin.BenchPod("10.0.0.1") as pod:
                assert pod.command({"cmd": "dac_limits"})["enabled"] is False
    """)
    pytester.runpytest("--benchpod-connection=10.0.0.1").assert_outcomes(passed=1)
    assert pod["limits"]["enabled"] is True


def test_limits_that_were_off_are_left_alone(pytester, pod, monkeypatch):
    monkeypatch.setenv(pytest_plugin.LIFT_DAC_LIMITS_ENV, "1")
    pod["limits"]["enabled"] = False
    pytester.makepyfile("def test_x():\n    pass\n")
    pytester.runpytest("--benchpod-connection=10.0.0.1").assert_outcomes(passed=1)
    assert pod["sent"] == [{"cmd": "dac_limits"}]


def test_firmware_without_dac_limits_does_not_stop_the_run(pytester, pod, monkeypatch):
    monkeypatch.setenv(pytest_plugin.LIFT_DAC_LIMITS_ENV, "1")
    pod["refuse"] = True
    pytester.makepyfile("def test_x():\n    pass\n")
    result = pytester.runpytest("-s", "--benchpod-connection=10.0.0.1")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*BENCHPOD_LIFT_DAC_LIMITS: could not read/clear DAC limits*"])
    assert pod["sent"] == [{"cmd": "dac_limits"}]  # nothing to restore


def test_the_api_key_option_reaches_its_connection(pytester, pod, monkeypatch):
    """--benchpod-api-key (and the lease options) reach the fixture's own connection, so a cloud
    run authenticated only by the flag can lift and restore the limits too."""
    monkeypatch.setenv(pytest_plugin.LIFT_DAC_LIMITS_ENV, "1")
    monkeypatch.delenv("BENCHPOD_API_KEY", raising=False)
    monkeypatch.delenv("BENCHPOD_API_BASE", raising=False)
    pytester.makepyfile("def test_x():\n    pass\n")
    pytester.runpytest("--benchpod-connection=embeddedci:bench-1", "--benchpod-api-key=eci_flag",
                       "--benchpod-api-base=https://x.test", "--benchpod-lease-wait=5"
                       ).assert_outcomes(passed=1)
    assert pod["connection"] == "embeddedci:bench-1"
    assert pod["kwargs"] == {"api_key": "eci_flag", "api_base": "https://x.test",
                             "lease": True, "lease_wait": 5.0}
    assert pod["sent"][-1]["cmd"] == "dac_limits" and "path" in pod["sent"][-1]


def test_the_benchpod_fixture_takes_the_api_key_option(pytester, monkeypatch):
    seen: Dict[str, Any] = {}

    class FakePod:
        def __init__(self, connection, **kwargs):
            seen.update(kwargs, connection=connection)

        def la_pins(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr(pytest_plugin, "BenchPod", FakePod)
    monkeypatch.delenv(pytest_plugin.LIFT_DAC_LIMITS_ENV, raising=False)
    monkeypatch.setenv("BENCHPOD_API_KEY", "eci_env")
    pytester.makepyfile("def test_x(benchpod):\n    pass\n")
    pytester.runpytest("--benchpod-connection=embeddedci:bench-1", "--benchpod-api-key=eci_flag",
                       "--benchpod-no-lease").assert_outcomes(passed=1)
    assert seen["api_key"] == "eci_flag" and seen["lease"] is False  # the flag wins over the env
