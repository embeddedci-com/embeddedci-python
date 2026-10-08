"""Persistent-connection mode: one BenchPod kept open across test executions."""

import openhtf as htf
from openhtf.core import test_record as tr

from embeddedci_openhtf import (
    benchpod_plug,
    close_persistent_benchpods,
    power_phase,
)
from embeddedci_openhtf.plug import _PERSISTENT_POOL, _pool_key
from _fake import FakeTransport


def _run(*phases):
    records = []
    test = htf.Test(*phases)
    test.add_output_callbacks(records.append)
    test.execute(test_start=lambda: "SN-TEST")
    return records[0]


def test_persistent_reuses_one_connection():
    tx = FakeTransport()
    bench = benchpod_plug(transport=tx, persistent=True)
    try:
        for _ in range(3):
            rec = _run(power_phase(bench, efuse=1, on=True))
            assert rec.outcome == tr.Outcome.PASS
            assert tx.closed is False           # stays open between executions
        # all three executions drove the same transport
        assert len(tx.power_calls) == 3
    finally:
        close_persistent_benchpods()
    assert tx.closed is True                     # closed only on explicit cleanup


def test_non_persistent_closes_each_run():
    tx = FakeTransport()
    bench = benchpod_plug(transport=tx)           # default: not persistent
    _run(power_phase(bench, on=True))
    assert tx.closed is True                       # tearDown closed it


def test_persistent_reconnects_after_drop():
    tx = FakeTransport()
    bench = benchpod_plug(transport=tx, persistent=True)
    try:
        _run(power_phase(bench, on=True))
        key = _pool_key(None, {"transport": tx, "timeout": 30.0})
        pod1 = _PERSISTENT_POOL[key]
        # simulate the link dying: make the health-check ping fail
        def boom():
            raise OSError("link dropped")
        tx.ping = boom
        _run(power_phase(bench, on=True))
        # the dead connection was dropped and a fresh BenchPod opened
        pod2 = _PERSISTENT_POOL[key]
        assert pod2 is not pod1
    finally:
        close_persistent_benchpods()


def test_plugs_with_the_same_settings_share_one_connection():
    tx = FakeTransport()
    first = benchpod_plug(transport=tx, persistent=True, la_voltage=3.3)
    second = benchpod_plug(transport=tx, persistent=True, la_voltage=3.3)
    try:
        _run(power_phase(first, efuse=1, on=True))
        _run(power_phase(second, efuse=1, on=True))
        assert len(_PERSISTENT_POOL) == 1
        assert len(tx.power_calls) == 2
    finally:
        close_persistent_benchpods()


def test_different_settings_get_their_own_connection():
    tx1, tx2 = FakeTransport(), FakeTransport()
    try:
        _run(power_phase(benchpod_plug(transport=tx1, persistent=True), on=True))
        _run(power_phase(benchpod_plug(transport=tx2, persistent=True), on=True))
        _run(power_phase(benchpod_plug(transport=tx1, persistent=True, timeout=5.0), on=True))
        assert len(_PERSISTENT_POOL) == 3
    finally:
        close_persistent_benchpods()
    assert tx1.closed and tx2.closed


def test_pool_key_reads_settings_by_value():
    wiring = {"signals": {"READY": 5}, "uart": {"baud": 115200}}
    assert _pool_key("10.0.0.5:8080", {"wiring": wiring, "la_voltage": 3.3}) == \
        _pool_key("10.0.0.5:8080", {"la_voltage": 3.3, "wiring": dict(wiring)})
    assert _pool_key("10.0.0.5:8080", {}) != _pool_key("10.0.0.6:8080", {})
    assert _pool_key("x", {"la_voltage": 1.8}) != _pool_key("x", {"la_voltage": 3.3})
