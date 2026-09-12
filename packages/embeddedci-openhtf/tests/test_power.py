"""Power profiling: the measure_power helper and the phase that records it, run through the real
OpenHTF executor against the fake transport. Units are amps, volts, joules and seconds."""

import json

import openhtf as htf
import pytest
from openhtf.core import test_record as tr

from embeddedci.benchpod import PowerProfile

from embeddedci_openhtf import benchpod_plug, measure_power, measure_power_phase
from _fake import FakeTransport


def _run(*phases):
    records = []
    test = htf.Test(*phases)
    test.add_output_callbacks(records.append)
    test.execute(test_start=lambda: "SN-TEST")
    return records[0]


def _phase(record, name):
    return next(p for p in record.phases if p.name == name)


def _value(phase, name):
    return phase.measurements[name].measured_value.value


def _request(tx, cmd="power_profile"):
    return [c for c in tx.commands if c.get("cmd") == cmd][-1]


# -- the helper --------------------------------------------------------------------

def test_measure_power_returns_si_units():
    tx = FakeTransport()
    plug = benchpod_plug(transport=tx)()
    profile = measure_power(plug, 1.0)
    assert isinstance(profile, PowerProfile)
    assert _request(tx) == {"cmd": "power_profile", "efuse": 1, "rate_hz": 1000.0,
                            "keep_samples": 0, "duration_ms": 1000}
    assert profile.avg_current == pytest.approx(0.052)
    assert profile.peak_current == pytest.approx(0.18)
    assert profile.avg_voltage == pytest.approx(5.01)
    assert profile.energy == pytest.approx(0.2605) and profile.avg_power == pytest.approx(0.2605)
    assert profile.samples == ()


def test_measure_power_passes_its_keywords_through():
    tx = FakeTransport()
    plug = benchpod_plug(transport=tx)()
    profile = measure_power(plug, 0.5, efuse=2, rate_hz=500, keep_samples=2)
    req = _request(tx)
    assert req["efuse"] == 2 and req["rate_hz"] == 500.0 and req["keep_samples"] == 2
    assert req["duration_ms"] == 500
    assert profile.samples == ((0.0, 0.04, 5.01), (0.5, 0.06, 5.0))


def test_measure_power_uses_the_wiring_profile_rail():
    tx = FakeTransport()
    plug = benchpod_plug(transport=tx, wiring={"efuse": 2})()
    measure_power(plug, 0.1)
    assert _request(tx)["efuse"] == 2


# -- the phase ---------------------------------------------------------------------

def test_measure_power_phase_records_current_voltage_and_energy():
    tx = FakeTransport()
    rec = _run(measure_power_phase(benchpod_plug(transport=tx), duration=1.0))
    assert rec.outcome == tr.Outcome.PASS
    phase = _phase(rec, "measure_power")
    assert _value(phase, "power_avg_current_a") == pytest.approx(0.052)
    assert _value(phase, "power_peak_current_a") == pytest.approx(0.18)
    assert _value(phase, "power_avg_voltage_v") == pytest.approx(5.01)
    assert _value(phase, "power_energy_j") == pytest.approx(0.2605)
    assert phase.measurements["power_avg_current_a"].units.suffix == "A"
    assert phase.measurements["power_avg_voltage_v"].units.suffix == "V"
    assert phase.measurements["power_energy_j"].units.suffix == "J"
    assert "power.json" not in phase.attachments  # no samples were kept


def test_measure_power_phase_limits_pass_and_fail():
    tx = FakeTransport()
    rec = _run(measure_power_phase(benchpod_plug(transport=tx), duration=1.0,
                                   avg_current_range=(0, 0.1), peak_current_range=(0, 0.2),
                                   energy_range=(0, 1.0)))
    assert rec.outcome == tr.Outcome.PASS

    tx = FakeTransport()
    rec = _run(measure_power_phase(benchpod_plug(transport=tx), duration=1.0,
                                   avg_current_range=(0, 0.01)))  # a 10 mA budget it blows
    assert rec.outcome == tr.Outcome.FAIL


def test_measure_power_phase_attaches_the_kept_samples():
    tx = FakeTransport()
    rec = _run(measure_power_phase(benchpod_plug(transport=tx), duration=1.0, keep_samples=2,
                                   efuse=2, rate_hz=500, prefix="sleep"))
    phase = _phase(rec, "measure_power")
    assert _value(phase, "sleep_avg_current_a") == pytest.approx(0.052)
    payload = json.loads(phase.attachments["power.json"].data)
    assert payload["efuse"] == 2 and payload["samples"][0] == [0.0, 0.04, 5.01]
    assert _request(tx)["rate_hz"] == 500.0


def test_measure_power_phase_can_skip_the_attachment():
    tx = FakeTransport()
    rec = _run(measure_power_phase(benchpod_plug(transport=tx), duration=1.0, keep_samples=2,
                                   attachment=None))
    assert "power.json" not in _phase(rec, "measure_power").attachments


def test_measure_power_phase_reports_a_fault_and_a_truncated_profile():
    tx = FakeTransport()
    tx.power_stats = dict(tx.power_stats, fault=True, truncated=True)
    rec = _run(measure_power_phase(benchpod_plug(transport=tx), duration=1.0))
    assert rec.outcome == tr.Outcome.PASS  # no limits were given; the log carries the warning
    assert _value(_phase(rec, "measure_power"), "power_peak_current_a") == pytest.approx(0.18)


def test_measure_power_phase_checks_its_arguments_at_build_time():
    plug = benchpod_plug(transport=FakeTransport())
    with pytest.raises(ValueError, match="duration"):
        measure_power_phase(plug, duration=0)
    with pytest.raises(ValueError, match="avg_current_range"):
        measure_power_phase(plug, duration=1.0, avg_current_range=(1.0, 0.0))
    with pytest.raises(ValueError, match="energy_range"):
        measure_power_phase(plug, duration=1.0, energy_range=(5, 1))
