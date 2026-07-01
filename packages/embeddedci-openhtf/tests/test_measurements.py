"""Unit-test the result -> measurement/attachment recorders by running them in a
tiny OpenHTF phase (no hardware: FlashResult / samples are built directly)."""

import json

import openhtf as htf
from openhtf.core import test_record as tr

from embeddedci.benchpod.flash import FlashResult

from embeddedci_openhtf import flash_ok_measurement, record_flash, record_samples


def _run(phase):
    records = []
    test = htf.Test(phase)
    test.add_output_callbacks(records.append)
    test.execute(test_start=lambda: "SN-TEST")
    return records[0]


def test_record_flash_ok():
    result = FlashResult(ok=True, returncode=0, stdout="** Programming Finished **",
                         stderr="")

    @htf.measures(flash_ok_measurement())
    def phase(test):
        record_flash(test, result)

    rec = _run(phase)
    assert rec.outcome == tr.Outcome.PASS
    p = rec.phases[-1]
    assert p.measurements["flash_ok"].measured_value.value is True
    assert "openocd.log" in p.attachments


def test_record_flash_failure_attaches_stderr():
    result = FlashResult(ok=False, returncode=1, stdout="probe init",
                         stderr="Error: cannot read IDR", target_unreachable=True)

    @htf.measures(flash_ok_measurement())
    def phase(test):
        record_flash(test, result)

    rec = _run(phase)
    assert rec.outcome == tr.Outcome.FAIL  # flash_ok == False fails the validator
    p = rec.phases[-1]
    assert p.measurements["flash_ok"].measured_value.value is False
    log = p.attachments["openocd.log"].data.decode()
    assert "cannot read IDR" in log  # stderr folded into the attached log


def test_record_samples_stats_and_attachment():
    @htf.measures(
        htf.Measurement("adc_min"),
        htf.Measurement("adc_max"),
        htf.Measurement("adc_mean"),
        htf.Measurement("adc_pp"),
    )
    def phase(test):
        stats = record_samples(test, [10, 20, 30])
        assert stats == {"adc_min": 10, "adc_max": 30, "adc_mean": 20.0,
                         "adc_pp": 20}

    rec = _run(phase)
    assert rec.outcome == tr.Outcome.PASS
    p = rec.phases[-1]
    assert p.measurements["adc_max"].measured_value.value == 30
    assert json.loads(p.attachments["adc.json"].data) == [10, 20, 30]
