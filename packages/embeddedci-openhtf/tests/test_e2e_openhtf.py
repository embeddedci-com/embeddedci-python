"""End-to-end: OpenHTF tests built from the plug's phase factories, run against a real BenchPod.

    pytest packages/embeddedci-openhtf/tests/test_e2e_openhtf.py --benchpod-connection=<pod host> \
        [--benchpod-firmware=<scenario-sensors.elf>]

Skips without a connection. The boot phase runs only when a firmware image is given (it expects
the examples/scenario-sensors-stm32 app, UART on BENCHPOD_E2E_UART_RX/TX, default LA3/LA4).
"""

from __future__ import annotations

import os

import pytest

htf = pytest.importorskip("openhtf")
from openhtf.core.test_record import Outcome  # noqa: E402

from embeddedci_openhtf import (  # noqa: E402
    adc_capture_phase,
    adc_read_phase,
    benchpod_plug,
    boot_banner_phase,
    dac_output_phase,
    flash_phase,
    power_phase,
)

pytestmark = pytest.mark.hardware

LA_VOLTAGE = 3.3  # the bench board's I/O voltage — change to 1.8 for a 1V8 board


def _run(*phases):
    records = []
    test = htf.Test(*phases, test_name="embeddedci_openhtf_e2e")
    test.add_output_callbacks(records.append)
    test.execute(test_start=lambda: "E2E-0001")
    assert records, "OpenHTF produced no test record"
    record = records[-1]
    failed = [(p.name, p.outcome, [(m.name, m.outcome, m.measured_value) for m in p.measurements.values()])
              for p in record.phases if str(p.outcome) != "PhaseOutcome.PASS"]
    return record, failed


def test_analog_phases_against_the_pod(benchpod_connection):
    bench = benchpod_plug(benchpod_connection, la_voltage=LA_VOLTAGE)
    record, failed = _run(
        dac_output_phase(bench, path="5v", volts=2.5),
        adc_read_phase(bench, source="cal1", v_range=(2.4, 2.6)),
        dac_output_phase(bench, path="off", name="dac_off"),
        adc_capture_phase(bench, samples=4096, sample_rate_hz=100_000, source="ext"),
    )
    assert record.outcome == Outcome.PASS, failed


def test_flash_and_boot_phases_against_the_dut(benchpod_connection, pytestconfig):
    firmware = pytestconfig.getoption("benchpod_firmware")
    if not firmware:
        pytest.skip("pass --benchpod-firmware to run the DUT phases")
    rx = int(os.environ.get("BENCHPOD_E2E_UART_RX", 3))
    tx = int(os.environ.get("BENCHPOD_E2E_UART_TX", 4))
    bench = benchpod_plug(benchpod_connection, la_voltage=LA_VOLTAGE)
    record, failed = _run(
        flash_phase(bench, file=os.path.abspath(firmware), target="target/stm32f4x.cfg",
                    swclk=11, swdio=12),
        boot_banner_phase(bench, rx=rx, tx=tx, expect="APP_OK", duration=25.0, delay=0.5),
        power_phase(bench, on=False),
    )
    assert record.outcome == Outcome.PASS, failed
