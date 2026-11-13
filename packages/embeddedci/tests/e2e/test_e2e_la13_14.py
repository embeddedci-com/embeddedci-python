"""Tier "pod": LA13/LA14, the two channels firmware 3.1 (gateware v36) added.

    pytest packages/embeddedci/tests/e2e/test_e2e_la13_14.py --benchpod-connection=<pod host>

LA13/LA14 must be free of drivers (device INPUTS are fine; override with BENCHPOD_E2E_TOP_LA). Each path that used to stop
at 12 gets one check: the pin table, GPIO drive + read-back, a capture, a trigger, a UART session.
Firmware older than 3.1 only has 12 channels, so these skip there; on 3.1+ a missing channel fails.
On a bench where the DUT drives one of them, BENCHPOD_E2E_TOP_LA=none skips every test that drives
them; the pin table check still runs.
"""

from __future__ import annotations

import threading
import time

import pytest

from embeddedci.benchpod import Trigger

from e2e_bench import TOP_LA

pytestmark = pytest.mark.hardware


def _version(v: str):
    try:
        return tuple(int(x) for x in v.split("-")[0].split(".")[:2])
    except ValueError:
        return (0, 0)


@pytest.fixture
def fourteen(pod):
    """The pod, on firmware with 14 LA channels, with every GPIO released before and after."""
    if not pod.capabilities.la_pins:
        pytest.skip("the pod firmware has no LA pin modes (capability la_pins)")
    count = len(pod.la_pins())
    if count < 14:
        if _version(pod.capabilities.firmware_version) >= (3, 1):
            pytest.fail(f"firmware {pod.capabilities.firmware_version} reports {count} LA channels, want 14")
        pytest.skip(f"firmware {pod.capabilities.firmware_version} has {count} LA channels; LA13/LA14 need 3.1+")
    pod.release_gpio()
    yield pod
    pod.release_gpio()


@pytest.fixture
def top(fourteen, bench):
    """The two top channels the tests drive (LA13, LA14 unless BENCHPOD_E2E_TOP_LA says otherwise)."""
    return bench.top_la


def test_pin_table_lists_all_fourteen_channels(pod, fourteen):
    pins = {p.la: p for p in pod.la_pins()}
    assert sorted(pins) == list(range(1, 15))
    for la in (13, 14):
        assert pins[la].function == "none" and pins[la].pull is None   # LA9-LA14 have no resistor
    assert set(pod.pin_levels()) == set(range(1, 15))


@TOP_LA
def test_gpio_drives_each_top_channel_independently(pod, top):
    la13, la14 = top
    a = pod.gpio(la13, "output", level=1)
    b = pod.gpio(la14, "output", level=0)
    assert (pod.pin_levels()[la13], pod.pin_levels()[la14]) == (1, 0)
    a.low()
    b.high()
    assert (pod.pin_levels()[la13], pod.pin_levels()[la14]) == (0, 1)
    state = {p.la: p for p in pod.la_pins()}
    assert state[la13].gpio == "output" and state[la13].level == 0
    assert state[la14].gpio == "output" and state[la14].level == 1


@TOP_LA
def test_capture_sees_both_top_channels_and_triggers_on_la14(pod, top):
    """LA13 held high, a rising trigger on LA14, then 10 pulses on LA14 in the capture."""
    if not pod.capabilities.capture_trigger:
        pytest.skip("the pod gateware has no capture triggers (capability capture_trigger)")
    la13, la14 = top
    pod.gpio(la13, "output", level=1)
    pin = pod.gpio(la14, "output", level=0)
    result = {}

    def run():
        try:
            result["capture"] = pod.capture_la(100_000, sample_rate_hz=1_000_000,
                                               trigger=Trigger(la14, "rising"), trigger_timeout=5.0)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.5)                                   # the capture waits for the LA14 edge
    pin.pulse(0.002, count=10)                        # 2 ms high, 2 ms low
    thread.join()
    if "error" in result:
        raise result["error"]
    la = result["capture"]
    assert la.channels == 14
    assert la.duty_cycle(la13) == 1.0                 # bit 12 of every word
    assert la.level_at(la14, 0.001) == 1              # t = 0 is the LA14 edge (bit 13)
    widths = la.pulse_widths(la14, level=1)
    assert len(widths) >= 8 and all(abs(w - 0.002) < 0.0002 for w in widths), widths


@TOP_LA
def test_level_trigger_on_la13(pod, top):
    if not pod.capabilities.capture_trigger:
        pytest.skip("the pod gateware has no capture triggers (capability capture_trigger)")
    la13, _ = top
    pod.gpio(la13, "output", level=1)
    start = time.monotonic()
    la = pod.capture_la(4096, sample_rate_hz=1_000_000, trigger=Trigger(la13, "high"), trigger_timeout=5.0)
    assert time.monotonic() - start < 4.0 and la.duty_cycle(la13) == 1.0


@TOP_LA
def test_step_train_on_la14_is_captured(pod, top):
    """An exact count: the 40 ms train starts 0.5 s into a 2 s capture, so none of it is missed.

    LA14 is held low as a GPIO output first (the train then pulses the GPIO pin), so an undriven
    line cannot add an edge: with something wired to LA14 (a device input) it otherwise floats
    and picked up a stray edge now and then."""
    _, la14 = top
    pod.gpio(la14, "output", level=0)
    result = {}

    def run():
        result["capture"] = pod.capture_la(2_000_000, sample_rate_hz=1_000_000)

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.5)                                   # let the capture arm (a LAN round-trip or two)
    pod.la_step(la14, steps=20, delay=0.001)
    thread.join()
    rising = len(result["capture"].edge_times(la14, "rising"))
    assert rising == 20, f"{rising} step pulses on LA{la14}, want 20"


@TOP_LA
def test_uart_session_on_the_top_channels(pod, top):
    la13, la14 = top
    with pod.open_uart(rx=la13, tx=la14, baud=115200) as uart:
        functions = {p.la: p.function for p in pod.la_pins()}
        assert (functions[la13], functions[la14]) == ("uart_rx", "uart_tx")
        assert isinstance(uart.read(timeout=0.2), str)
    functions = {p.la: p.function for p in pod.la_pins()}
    assert (functions[la13], functions[la14]) == ("none", "none")
