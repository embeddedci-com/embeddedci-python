"""GPIO on the LA pins and the two-channel timing helper, run through the real OpenHTF executor
against the fake transport."""

import openhtf as htf
import pytest
from openhtf.core import test_record as tr

from embeddedci.benchpod import GpioPin, Trigger
from embeddedci.benchpod.errors import PinConflictError

from embeddedci_openhtf import (
    benchpod_plug,
    gpio,
    gpio_phase,
    la_delay,
    la_delay_phase,
    read_gpio,
    release_gpio,
    set_gpio,
)
from _fake import FakeTransport

WIRING = {"signals": [{"name": "TRIGGER", "la": 9, "direction": "output"},
                      {"name": "READY", "la": 10}]}


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


def _cmds(tx):
    return [c.get("cmd") for c in tx.commands]


def la_words(pattern):
    """Turn ``{la: [0, 1, ...]}`` into packed 12-channel LA words."""
    length = max(len(bits) for bits in pattern.values())
    return [sum(bits[i] << (la - 1) for la, bits in pattern.items() if i < len(bits))
            for i in range(length)]


# -- GPIO helpers ----------------------------------------------------------------

def test_gpio_claims_a_channel_and_drives_it():
    tx = FakeTransport()
    plug = benchpod_plug(transport=tx)()
    pin = gpio(plug, 3, "output", level=1)
    assert isinstance(pin, GpioPin) and pin.la == 3
    assert tx.commands[-1] == {"cmd": "gpio", "la": 3, "mode": "output", "level": 1}
    gpio(plug, 4, "output")
    set_gpio(plug, [3, 4], 0)
    assert tx.commands[-1] == {"cmd": "gpio", "la": [3, 4], "level": 0}
    assert tx.level == {**tx.level, 3: 0, 4: 0}


def test_read_gpio_sees_what_the_dut_drives():
    tx = FakeTransport()
    tx.inputs = 1 << 10  # LA11 is high
    plug = benchpod_plug(transport=tx)()
    assert read_gpio(plug, 11) == 1 and read_gpio(plug, 12) == 0


def test_release_gpio_frees_channels_and_all_of_them():
    tx = FakeTransport()
    plug = benchpod_plug(transport=tx)()
    gpio(plug, 3)
    release_gpio(plug, 3)
    assert tx.commands[-1] == {"cmd": "gpio", "la": 3, "mode": "off"}
    assert tx.function[3] == "none"
    release_gpio(plug)
    assert tx.commands[-1] == {"cmd": "gpio", "la": "all", "mode": "off"}


def test_gpio_resolves_a_wiring_name():
    tx = FakeTransport()
    plug = benchpod_plug(transport=tx, wiring=WIRING)()
    pin = gpio(plug, "TRIGGER")
    assert pin.la == 9 and tx.commands[-1]["la"] == 9


def test_a_channel_another_function_owns_is_refused():
    tx = FakeTransport()
    tx.function[5] = "uart_rx"
    plug = benchpod_plug(transport=tx)()
    with pytest.raises(PinConflictError) as exc:
        gpio(plug, 5)
    assert exc.value.la == 5 and exc.value.function == "uart_rx"


# -- la_delay ---------------------------------------------------------------------

def test_la_delay_measures_between_two_channels():
    # LA1 rises at sample 2, LA2 at sample 7 -> 5 samples = 5 µs at 1 MS/s.
    tx = FakeTransport(la_words=la_words({1: [0, 0, 1, 1, 1, 1, 1, 1, 1, 1],
                                          2: [0, 0, 0, 0, 0, 0, 0, 1, 1, 1]}))
    plug = benchpod_plug(transport=tx)()
    delay = la_delay(plug, 1, 2, samples=10, sample_rate_hz=1e6)
    assert delay == pytest.approx(5e-6)
    assert tx.commands[-1]["cmd"] == "la_capture" and tx.commands[-1]["samples"] == 10


def test_la_delay_is_none_without_the_edges():
    tx = FakeTransport(la_words=[0] * 8)
    plug = benchpod_plug(transport=tx)()
    assert la_delay(plug, 1, 2, samples=8, sample_rate_hz=1e6) is None


def test_la_delay_takes_wiring_names_and_a_trigger():
    tx = FakeTransport(la_words=la_words({9: [0, 1, 1, 1, 1], 10: [0, 0, 0, 1, 1]}))
    plug = benchpod_plug(transport=tx, wiring=WIRING)()
    delay = la_delay(plug, "TRIGGER", "READY", samples=5, sample_rate_hz=1e6,
                     trigger=Trigger("TRIGGER", "rising"))
    assert delay == pytest.approx(2e-6)
    assert tx.commands[-1]["trigger"] == {"la": 9, "edge": "rising"}


# -- phase factories ---------------------------------------------------------------

def test_gpio_phase_claims_the_channel():
    tx = FakeTransport()
    rec = _run(gpio_phase(benchpod_plug(transport=tx), la=3, mode="output", level=1))
    assert rec.outcome == tr.Outcome.PASS
    assert "gpio" in _cmds(tx) and tx.mode[3] == "output" and tx.level[3] == 1


def test_gpio_phase_takes_a_wiring_name():
    tx = FakeTransport()
    rec = _run(gpio_phase(benchpod_plug(transport=tx, wiring=WIRING), la="TRIGGER"))
    assert rec.outcome == tr.Outcome.PASS and tx.mode[9] == "output"


def test_la_delay_phase_records_seconds():
    tx = FakeTransport(la_words=la_words({1: [0, 0, 1, 1, 1, 1, 1, 1],
                                          2: [0, 0, 0, 0, 0, 1, 1, 1]}))
    rec = _run(la_delay_phase(benchpod_plug(transport=tx), from_la=1, to_la=2, samples=8,
                              sample_rate_hz=1e6, delay_range=(1e-6, 1e-5)))
    assert rec.outcome == tr.Outcome.PASS
    phase = _phase(rec, "la_delay")
    assert _value(phase, "la_delay_s") == pytest.approx(3e-6)
    assert phase.measurements["la_delay_s"].units.suffix == "s"


def test_la_delay_phase_fails_outside_its_limit():
    tx = FakeTransport(la_words=la_words({1: [0, 1, 1, 1], 2: [0, 0, 0, 1]}))
    rec = _run(la_delay_phase(benchpod_plug(transport=tx), from_la=1, to_la=2, samples=4,
                              sample_rate_hz=1e6, delay_range=(0, 1e-6)))
    assert rec.outcome == tr.Outcome.FAIL


def test_la_delay_phase_fails_when_an_edge_is_missing():
    tx = FakeTransport(la_words=[0] * 4)
    rec = _run(la_delay_phase(benchpod_plug(transport=tx), from_la=1, to_la=2, samples=4,
                              sample_rate_hz=1e6, delay_range=(0, 1e-3)))
    assert rec.outcome == tr.Outcome.FAIL  # the measurement was never set


def test_la_delay_phase_rejects_a_bad_range_at_build_time():
    with pytest.raises(ValueError, match="delay_range"):
        la_delay_phase(benchpod_plug(transport=FakeTransport()), from_la=1, to_la=2, samples=4,
                       sample_rate_hz=1e6, delay_range=(1e-3, 0))
