"""Persistent-connection mode: one BenchPod kept open across test executions."""

import openhtf as htf
from openhtf.core import test_record as tr

from embeddedci_openhtf import (
    benchpod_plug,
    close_persistent_benchpods,
    power_phase,
)
from embeddedci_openhtf.plug import _PERSISTENT_POOL
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
        pod1 = _PERSISTENT_POOL[bench]
        # simulate the link dying: make the health-check ping fail
        def boom():
            raise OSError("link dropped")
        tx.ping = boom
        _run(power_phase(bench, on=True))
        # the dead connection was dropped and a fresh BenchPod opened
        pod2 = _PERSISTENT_POOL[bench]
        assert pod2 is not pod1
    finally:
        close_persistent_benchpods()
