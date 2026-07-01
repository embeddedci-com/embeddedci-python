"""Run the phase factories through the real OpenHTF executor against the fake
transport, and assert on the resulting TestRecord."""

import openhtf as htf
from openhtf.core import test_record as tr

from embeddedci_openhtf import benchpod_plug, boot_banner_phase, power_phase
from _fake import FakeTransport


def _run(*phases):
    records = []
    test = htf.Test(*phases)
    test.add_output_callbacks(records.append)
    test.execute(test_start=lambda: "SN-TEST")
    return records[0]


def _phase(record, name):
    return next(p for p in record.phases if p.name == name)


def test_power_and_boot_pass():
    tx = FakeTransport(banner=b"reset\r\nAPP_OK build 7\r\n")
    bench = benchpod_plug(transport=tx)
    rec = _run(
        power_phase(bench, efuse=1, on=True),
        boot_banner_phase(bench, rx=1, tx=2, expect="APP_OK",
                          power_cycle=False, duration=2.0),
    )
    assert rec.outcome == tr.Outcome.PASS
    assert tx.power_calls[0] == {"efuse": 1, "on": True, "delay_ms": 0}
    boot = _phase(rec, "boot")
    assert boot.measurements["boot_ok"].measured_value.value is True
    assert "uart.txt" in boot.attachments
    assert tx.closed is True  # plug tearDown ran


def test_boot_fail_when_pattern_absent():
    tx = FakeTransport(banner=b"reset\r\nPANIC\r\n")
    bench = benchpod_plug(transport=tx)
    rec = _run(
        boot_banner_phase(bench, rx=1, tx=2, expect="APP_OK",
                          power_cycle=False, duration=1.0),
    )
    # the measurement validator (equals True) fails -> the whole test fails,
    # but the captured UART is still attached for triage
    assert rec.outcome == tr.Outcome.FAIL
    boot = _phase(rec, "boot")
    assert boot.measurements["boot_ok"].measured_value.value is False
    assert "uart.txt" in boot.attachments


def test_power_cycle_capture_path():
    tx = FakeTransport(banner=b"\r\nAPP_OK\r\n")
    bench = benchpod_plug(transport=tx)
    rec = _run(
        boot_banner_phase(bench, rx=5, tx=4, expect="APP_OK",
                          power_cycle=True, efuse=1, delay=0.2,
                          duration=2.0),
    )
    assert rec.outcome == tr.Outcome.PASS
    # power_cycle_and_capture toggles the eFuse off then schedules it on
    assert [c["on"] for c in tx.power_calls] == [False, True]


def test_power_off_phase():
    tx = FakeTransport()
    bench = benchpod_plug(transport=tx)
    rec = _run(power_phase(bench, efuse=2, on=False))
    assert rec.outcome == tr.Outcome.PASS
    assert tx.power_calls == [{"efuse": 2, "on": False, "delay_ms": 0}]
