"""Unit tests (no pod): test_e2e_mcp.py reads the SDK e2e suite's bench profile and skips the tests
that drive channels a fully wired bench does not have."""

from __future__ import annotations

import ast
from pathlib import Path

import test_e2e_mcp as e2e

WIRED = {
    "BENCHPOD_E2E_FREE_LA": "none",
    "BENCHPOD_E2E_TOP_LA": "none",
    "BENCHPOD_E2E_FREE_PULL_LA": "none",
    "BENCHPOD_E2E_FREE_PULLDOWN_LA": "none",
    "BENCHPOD_E2E_UART_SENSOR_TX": "8",
}


def test_the_profile_is_the_sdk_suites_file():
    assert e2e.BENCH_PROFILE.is_file()
    assert Path(e2e.e2e_bench.__file__).resolve() == e2e.BENCH_PROFILE


def _skipif(mark):
    (condition,), kwargs = mark.mark.args, mark.mark.kwargs
    return condition, kwargs["reason"]


def test_the_wired_bench_skips_the_top_channels_and_keeps_the_gps_on_la8():
    wired = e2e.e2e_bench.bench_from_env(WIRED)
    skip, reason = _skipif(e2e.bench_needs("top_la", bench=wired))
    assert skip and "BENCHPOD_E2E_TOP_LA" in reason and "wires every LA channel" in reason
    assert _skipif(e2e.bench_needs("uart_sensor_tx", bench=wired)) == (False, "")
    assert wired.uart_sensor_tx == 8


def test_the_gps_skips_without_a_channel():
    bench = e2e.e2e_bench.bench_from_env({"BENCHPOD_E2E_FREE_LA": "none"})
    skip, reason = _skipif(e2e.bench_needs("uart_sensor_tx", bench=bench))
    assert skip and "BENCHPOD_E2E_UART_SENSOR_TX" in reason


def test_the_default_bench_runs_everything():
    bench = e2e.e2e_bench.bench_from_env({})
    assert (bench.top_la, bench.uart_sensor_tx) == ((13, 14), 9)
    assert not _skipif(e2e.bench_needs("top_la", "uart_sensor_tx", bench=bench))[0]


def test_every_test_that_reads_a_bench_channel_is_marked():
    tree = ast.parse(Path(e2e.__file__).read_text())
    needs = set(e2e.e2e_bench.NEEDS)
    unmarked = []
    for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")):
        read = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name) and n.value.id == "BENCH" and n.attr in needs}
        marked = {arg.value for d in fn.decorator_list if isinstance(d, ast.Call)
                  and getattr(d.func, "id", None) == "bench_needs"
                  for arg in d.args if isinstance(arg, ast.Constant)}
        if read - marked:
            unmarked.append(f"{fn.name}: {sorted(read - marked)}")
    assert not unmarked, f"add @bench_needs(...) to: {unmarked}"
