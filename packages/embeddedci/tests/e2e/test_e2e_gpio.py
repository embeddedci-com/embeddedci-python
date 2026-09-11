"""Tier "pod": LA pin ownership, GPIO, bias-resistor conflicts, capture triggers and wiring names.

    pytest packages/embeddedci/tests/e2e/test_e2e_gpio.py --benchpod-connection=<pod host>

Uses the unwired channels from the ``bench`` fixture (LA9/LA10 by default), the biased channel
``free_pull_la`` (LA6) and LA7's pull-down with nothing attached. Needs firmware with LA pin modes
(``la_pins``); the trigger tests need gateware v35+ (``capture_trigger``).
"""

from __future__ import annotations

import threading
import time

import pytest

from embeddedci.benchpod import (
    PinConflictError,
    PullConflictError,
    Signal,
    Trigger,
    TriggerTimeout,
    Wiring,
)

pytestmark = pytest.mark.hardware


@pytest.fixture
def gpio_pod(pod):
    if not pod.capabilities.la_pins:
        pytest.skip("the pod firmware has no LA pin modes (capability la_pins)")
    pod.release_gpio()
    yield pod
    pod.release_gpio()


@pytest.fixture
def trigger_pod(gpio_pod):
    if not gpio_pod.capabilities.capture_trigger:
        pytest.skip("the pod gateware has no capture triggers (capability capture_trigger)")
    return gpio_pod


def _function(pod, la):
    return next(p for p in pod.la_pins() if p.la == la)


# -- GPIO + ownership ---------------------------------------------------------------------

def test_gpio_output_drives_and_reads_back(gpio_pod, bench):
    out = bench.free_la[0]
    pin = gpio_pod.gpio(out, "output", level=1)
    assert pin.read() == 1 and _function(gpio_pod, out).function == "gpio"
    pin.low()
    assert gpio_pod.pin_levels()[out] == 0
    pin.release()
    state = _function(gpio_pod, out)
    assert state.function == "none" and not state.in_use


def test_open_drain_releases_to_the_pull_up(gpio_pod, bench):
    ch = bench.free_pull_la
    gpio_pod.enable_pullup(ch)
    try:
        pin = gpio_pod.gpio(ch, "open_drain")
        assert pin.read() == 1                      # released: the pod's pull-up holds it high
        pin.low()
        assert pin.read() == 0
    finally:
        gpio_pod.release_gpio(ch)
        gpio_pod.disable_pullup(ch)


def test_pull_down_conflicts_with_open_drain_both_ways(gpio_pod):
    gpio_pod.enable_pulldown(7)
    try:
        with pytest.raises(PullConflictError) as ei:
            gpio_pod.gpio(7, "open_drain")
        assert ei.value.la == 7 and "pull-down" in str(ei.value)
    finally:
        gpio_pod.disable_pulldown(7)
    gpio_pod.gpio(7, "open_drain")
    try:
        with pytest.raises(PullConflictError):
            gpio_pod.enable_pulldown(7)
    finally:
        gpio_pod.release_gpio(7)
    gpio_pod.gpio(7, "output", level=1)                # a push-pull output may sit on a pull-down
    gpio_pod.enable_pulldown(7)
    gpio_pod.disable_pulldown(7)


def test_uart_is_refused_on_a_gpio_pin_until_released(gpio_pod, bench):
    rx, tx = bench.free_la
    gpio_pod.gpio(tx)
    with pytest.raises(PinConflictError) as ei:
        gpio_pod.open_uart(rx=rx, tx=tx)
    assert (ei.value.la, ei.value.function) == (tx, "gpio")
    assert '"mode":"off"' in str(ei.value)
    gpio_pod.release_gpio(tx)
    with gpio_pod.open_uart(rx=rx, tx=tx):
        assert {_function(gpio_pod, rx).function, _function(gpio_pod, tx).function} == {"uart_rx", "uart_tx"}
        with pytest.raises(PinConflictError) as ei:
            gpio_pod.gpio(tx)
        assert ei.value.function == "uart_tx"
    assert not _function(gpio_pod, tx).in_use            # the session released its pins


def test_step_train_on_a_gpio_output_keeps_it_gpio(gpio_pod, bench):
    ch = bench.free_la[0]
    pin = gpio_pod.gpio(ch, "output", level=0)
    pin.pulse(0.002, count=5)
    time.sleep(0.1)
    state = _function(gpio_pod, ch)
    assert state.function == "gpio" and state.level == 0


def test_la_voltage_cannot_change_while_pins_are_in_use(gpio_pod, bench, rev3):
    if not rev3:
        pytest.skip("switching the LA bank needs a rev3 pod")
    gpio_pod.gpio(bench.free_la[0])
    with pytest.raises(Exception, match="in use"):
        gpio_pod.set_la_voltage(1.8)
    gpio_pod.set_la_voltage(3.3)                          # the current voltage is always fine


def test_wiring_signal_names(gpio_pod, bench):
    out, inp = bench.free_la
    previous = gpio_pod.wiring
    gpio_pod.wiring = Wiring(uart_rx=None, uart_tx=None, i2c_sda=None, i2c_scl=None,
                             swd_swclk=None, swd_swdio=None,
                             signals=[Signal("E2E_OUT", out, "output", active_low=True),
                                      Signal("E2E_IN", inp)])
    try:
        sig = gpio_pod.signal("e2e_out")
        sig.configure()                                   # inactive = high for an active-low output
        assert sig.read() == 1 and not sig.is_active()
        sig.activate()
        assert sig.read() == 0 and gpio_pod.read_gpio("E2E_OUT") == 0
    finally:
        gpio_pod.wiring = previous


# -- triggers ---------------------------------------------------------------------------

def _capture_in_thread(pod, **kwargs):
    result = {}

    def run():
        try:
            result["capture"] = pod.capture_la(**kwargs)
        except BaseException as exc:  # re-raised on the test thread
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_rising_trigger_starts_the_capture_at_the_edge(trigger_pod, bench):
    ch = bench.free_la[0]
    pin = trigger_pod.gpio(ch, "output", level=0)
    thread, result = _capture_in_thread(trigger_pod, samples=20_000, sample_rate_hz=1_000_000,
                                        trigger=Trigger(ch, "rising"), trigger_timeout=5.0)
    time.sleep(0.5)                                       # the capture is waiting for the edge
    pin.high()
    thread.join()
    if "error" in result:
        raise result["error"]
    la = result["capture"]
    assert la.trigger == Trigger(ch, "rising")
    assert la.level_at(ch, 0.002) == 1                    # high from (almost) the first sample
    assert la.first_edge(ch, "falling") is None


def test_level_trigger_fires_at_once(trigger_pod, bench):
    ch = bench.free_la[0]
    trigger_pod.gpio(ch, "output", level=1)
    start = time.monotonic()
    la = trigger_pod.capture_la(4096, sample_rate_hz=1_000_000, trigger=Trigger(ch, "high"),
                                trigger_timeout=5.0)
    assert time.monotonic() - start < 4.0 and la.duty_cycle(ch) == 1.0


def test_trigger_timeout_aborts_and_later_captures_are_untriggered(trigger_pod, bench):
    quiet = bench.free_la[1]
    with pytest.raises(TriggerTimeout) as ei:
        trigger_pod.capture_la(4096, sample_rate_hz=1_000_000, trigger=Trigger(quiet, "rising"),
                               trigger_timeout=0.5)
    assert ei.value.la == quiet and ei.value.edge == "rising"
    start = time.monotonic()
    assert len(trigger_pod.capture_la(4096, sample_rate_hz=1_000_000)) == 4096
    assert time.monotonic() - start < 5.0


def test_triggered_adc_capture(trigger_pod, bench):
    ch = bench.free_la[0]
    pin = trigger_pod.gpio(ch, "output", level=0)
    result = {}

    def run():
        result["capture"] = trigger_pod.capture_adc(8192, sample_rate_hz=100_000,
                                                    trigger=Trigger(ch, "rising"), trigger_timeout=5.0)

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.5)
    pin.high()
    thread.join()
    assert len(result["capture"]) == 8192 and result["capture"].trigger.la == ch


def test_step_train_is_timed_from_the_trigger(trigger_pod, bench):
    ch = bench.free_la[0]
    pin = trigger_pod.gpio(ch, "output", level=0)
    thread, result = _capture_in_thread(trigger_pod, samples=100_000, sample_rate_hz=1_000_000,
                                        trigger=Trigger(ch, "rising"), trigger_timeout=5.0)
    time.sleep(0.5)
    pin.pulse(0.002, count=10)                            # 2 ms high, 2 ms low
    thread.join()
    la = result["capture"]
    widths = la.pulse_widths(ch, level=1)
    assert len(widths) >= 8 and all(abs(w - 0.002) < 0.0002 for w in widths), widths
    assert la.frequency(ch) == pytest.approx(250.0, rel=0.02)
