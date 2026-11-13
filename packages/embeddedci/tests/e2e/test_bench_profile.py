"""Unit tests (no pod) of the e2e bench profile: e2e_bench.py and the bench_needs skip in conftest.py.

They live next to the e2e tests because they test that directory's own modules; nothing here is
marked ``hardware``, so the unit suite runs them.
"""

from __future__ import annotations

import ast
import os
import shutil

import pytest

from e2e_bench import NEEDS, bench_from_env, bench_needs, missing_reason

HERE = os.path.dirname(os.path.abspath(__file__))

#: The fully wired process-node bench (README.md, "Bench profiles").
WIRED = {
    "BENCHPOD_E2E_FREE_LA": "none",
    "BENCHPOD_E2E_TOP_LA": "none",
    "BENCHPOD_E2E_FREE_PULL_LA": "none",
    "BENCHPOD_E2E_FREE_PULLDOWN_LA": "none",
    "BENCHPOD_E2E_UART_SENSOR_TX": "8",
}


def test_defaults_are_the_free_channel_bench():
    b = bench_from_env({})
    assert (b.free_la, b.top_la, b.free_pull_la, b.free_pulldown_la) == ((9, 10), (13, 14), 6, 7)
    assert b.uart_sensor_tx == 9                   # the GPS falls back to the first free channel
    assert missing_reason(b, list(NEEDS)) is None


def test_the_fully_wired_bench_has_no_free_channels():
    b = bench_from_env(WIRED)
    assert (b.free_la, b.top_la, b.free_pull_la, b.free_pulldown_la) == (None, None, None, None)
    assert b.uart_sensor_tx == 8
    assert missing_reason(b, ["uart_sensor_tx"]) is None


@pytest.mark.parametrize("word", ["none", "NONE", " None ", ""])
def test_none_in_any_case_or_empty_means_no_channel(word):
    b = bench_from_env({"BENCHPOD_E2E_FREE_LA": word, "BENCHPOD_E2E_FREE_PULL_LA": word})
    assert b.free_la is None and b.free_pull_la is None
    assert b.uart_sensor_tx is None                # no free channel and no UART RX line given


def test_overrides_are_parsed():
    b = bench_from_env({"BENCHPOD_E2E_FREE_LA": "5,6", "BENCHPOD_E2E_TOP_LA": "14,13",
                        "BENCHPOD_E2E_FREE_PULL_LA": "3", "BENCHPOD_E2E_FREE_PULLDOWN_LA": "8"})
    assert (b.free_la, b.top_la, b.free_pull_la, b.free_pulldown_la) == ((5, 6), (14, 13), 3, 8)
    assert b.uart_sensor_tx == 5


@pytest.mark.parametrize("name,value", [
    ("FREE_LA", "9"), ("FREE_LA", "9,9"), ("FREE_LA", "0,1"), ("FREE_LA", "9,15"),
    ("FREE_LA", "nine,ten"), ("FREE_LA", "9,10,11"), ("TOP_LA", "13"),
    ("FREE_PULL_LA", "7"),                         # LA7/LA8 pull down: not a pull-up channel
    ("FREE_PULLDOWN_LA", "6"), ("UART_SENSOR_TX", "15"),
])
def test_a_bad_value_names_its_variable(name, value):
    with pytest.raises(ValueError, match=f"BENCHPOD_E2E_{name}="):
        bench_from_env({f"BENCHPOD_E2E_{name}": value})


def test_skip_reason_names_the_variable_and_the_wired_bench():
    b = bench_from_env(WIRED)
    reason = missing_reason(b, ["free_la", "free_pull_la"])
    assert "BENCHPOD_E2E_FREE_LA" in reason and "BENCHPOD_E2E_FREE_PULL_LA" in reason
    assert "wires every LA channel" in reason
    assert missing_reason(b, []) is None


def test_unknown_resource_is_refused():
    with pytest.raises(ValueError, match="unknown bench resources"):
        bench_needs("free_las")


# -- every test that drives a free channel carries the marker -------------------------------

_MARKS = {"FREE_LA": "free_la", "TOP_LA": "top_la", "FREE_PULL_LA": "free_pull_la",
          "FREE_PULLDOWN_LA": "free_pulldown_la", "UART_SENSOR_TX": "uart_sensor_tx"}


def _needs_read(fn: ast.AST) -> set:
    return {node.attr for node in ast.walk(fn)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "bench" and node.attr in NEEDS}


def _marks(decorators) -> set:
    return {_MARKS[d.id] for d in decorators if isinstance(d, ast.Name) and d.id in _MARKS}


def _module_marks(tree: ast.Module) -> set:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "pytestmark" for t in node.targets):
            value = node.value
            items = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
            return _marks(items)
    return set()


@pytest.mark.parametrize("name", sorted(f for f in os.listdir(HERE)
                                        if f.startswith("test_e2e_") and f.endswith(".py")))
def test_every_test_that_uses_a_free_channel_is_marked(name):
    """A test reading ``bench.free_la`` (or one of the others), directly or through a fixture of
    its module, must carry the matching marker: without it a fully wired bench would get a
    TypeError at best, and the test would drive a DUT line at worst."""
    with open(os.path.join(HERE, name)) as f:
        tree = ast.parse(f.read())
    module = _module_marks(tree)
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    fixtures = {f.name: _needs_read(f) for f in funcs if not f.name.startswith("test_")}
    unmarked = []
    for fn in funcs:
        if not fn.name.startswith("test_"):
            continue
        needed = _needs_read(fn)
        for arg in fn.args.args:
            needed |= fixtures.get(arg.arg, set())
        missing = needed - _marks(fn.decorator_list) - module
        if missing:
            unmarked.append(f"{fn.name}: {sorted(missing)}")
    assert not unmarked, f"{name}: add the bench_needs markers (e2e_bench.py): {unmarked}"


# -- the conftest hook ----------------------------------------------------------------------

@pytest.fixture
def e2e_dir(pytester):
    for module in ("conftest.py", "e2e_bench.py", "e2e_helpers.py"):
        shutil.copy(os.path.join(HERE, module), pytester.path / module)
    pytester.makeini("[pytest]\nmarkers =\n    bench_needs(*names): e2e bench resources\n")
    pytester.makepyfile(test_drives="""
        from e2e_bench import FREE_LA, UART_SENSOR_TX

        @FREE_LA
        def test_drives_a_free_channel(bench):
            assert bench.free_la is not None

        @UART_SENSOR_TX
        def test_gps(bench):
            assert bench.uart_sensor_tx is not None

        def test_needs_nothing(bench):
            pass
    """)
    return pytester


@pytest.fixture
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("BENCHPOD_E2E_"):
            monkeypatch.delenv(key)
    return monkeypatch


def test_hook_runs_everything_on_the_default_bench(e2e_dir, clean_env):
    e2e_dir.runpytest_inprocess("-p", "no:cacheprovider").assert_outcomes(passed=3)


def test_hook_skips_on_the_fully_wired_bench(e2e_dir, clean_env):
    for key, value in WIRED.items():
        clean_env.setenv(key, value)
    clean_env.delenv("BENCHPOD_E2E_UART_SENSOR_TX")
    result = e2e_dir.runpytest_inprocess("-p", "no:cacheprovider", "-rs")
    result.assert_outcomes(passed=1, skipped=2)
    result.stdout.fnmatch_lines(["*needs two LA channels nothing is wired to*BENCHPOD_E2E_FREE_LA*"])

    clean_env.setenv("BENCHPOD_E2E_UART_SENSOR_TX", "8")       # the GPS runs on the DUT's RX line
    e2e_dir.runpytest_inprocess("-p", "no:cacheprovider").assert_outcomes(passed=2, skipped=1)


def test_hook_refuses_a_bad_value(e2e_dir, clean_env):
    clean_env.setenv("BENCHPOD_E2E_FREE_LA", "9")
    result = e2e_dir.runpytest_inprocess("-p", "no:cacheprovider")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*BENCHPOD_E2E_FREE_LA='9'*"])
