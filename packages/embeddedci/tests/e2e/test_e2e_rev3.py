"""Tier "rev3": the hardware only a rev3 (v3) pod has — the board strap, the 1.8 V LA bank, the
dedicated target-reset pin and USB-C CC monitoring.

    BENCHPOD_E2E_BOARD_REV=v3 pytest packages/embeddedci/tests/e2e/test_e2e_rev3.py \
        --benchpod-connection=<pod host>

Set BENCHPOD_E2E_BOARD_REV=v3 on a v3 bench: without it a board whose strap is misread as v2 skips
this file and passes the v2 refusal checks in test_e2e_pod.py. Everything here skips on a v2 pod.

The reset pin (J1 pin 22) is open-drain with a 10 k pull-up to the LA bank rail on the pod. To see
it electrically, jumper it to an LA channel nothing else uses and set BENCHPOD_E2E_NRST_LA to that
channel; without the jumper only the firmware's view of the pin is checked. The DUT-side reset
tests are in test_e2e_dut.py (BENCHPOD_E2E_NRESET=1).
"""

from __future__ import annotations

import os
import time

import pytest

from embeddedci.benchpod import FirmwareError, ResetState, UsbCcStatus

from e2e_helpers import events_during

pytestmark = pytest.mark.hardware


@pytest.fixture
def v3(pod, rev3):
    if not rev3:
        pytest.skip("rev3 hardware (this pod reports no reset pin)")
    yield pod
    try:
        pod.set_reset(False)
    except Exception:
        pass
    pod.set_la_voltage(3.3)


@pytest.fixture
def nrst_la(v3, bench):
    """The LA channel jumpered to the reset pin, with nothing driving or biasing it."""
    if bench.nrst_la is None:
        pytest.skip("jumper J1 pin 22 (reset) to a free LA channel and set BENCHPOD_E2E_NRST_LA")
    ch = bench.nrst_la
    if v3.capabilities.la_pins:
        v3.release_gpio(ch)
    before = v3.pull_state(ch).enabled if ch <= 8 else None
    if before:
        v3.set_pull(ch, False)          # the pod's own 10 k pull-up must be what sets the level
    yield ch
    if before:
        v3.set_pull(ch, True)


def _level(pod, ch: int) -> int:
    """The steady level of an LA channel, from a short capture (works on any gateware)."""
    bits = pod.capture_la(2048, sample_rate_hz=1_000_000).channel(ch)
    assert len(set(bits)) == 1, f"LA{ch} toggled during a 2 ms capture of a static line"
    return bits[0]


# -- identity ----------------------------------------------------------------------------

def test_board_strap_reads_as_v3(v3):
    status = v3.status()
    # R164 1 k over R163 10 k from +3V3: ~3.0 V. Near a bucket edge means a wrong part or a bad joint.
    assert status["board_rev"] == "v3" and 2700 <= status["board_rev_mv"] <= 3250, status
    caps = v3.refresh_capabilities()
    assert caps.board_rev == "v3" and caps.nrst_pin and caps.usb_cc, caps


def test_firmware_and_gateware_are_current(v3):
    """Every rev3 pod runs firmware newer than all of these, so a missing one means the iCE40 kept
    older gateware (flash-self writes only the STM32) and the tests that need it would just skip."""
    caps = v3.refresh_capabilities()
    # both gateware images are v35+, so these hold whichever one is running
    missing = [name for name in ("la_pins", "power_profile", "gpio_read", "capture_trigger")
               if not getattr(caps, name)]
    assert not missing, (f"missing {missing}: reflash the gateware (USB console: flash-ice40) and "
                         f"check the running image")


# -- 1.8 V LA bank -------------------------------------------------------------------------

def test_bank_voltage_reads_back_from_the_mux(v3):
    """The TPS2116 status pin says which rail really feeds the bank, not what we asked for."""
    for volts in (1.8, 3.3, 1.8, 3.3):
        state = v3.set_la_voltage(volts)
        assert state.voltage == volts and state.readback == volts, state
        assert v3.get_la_voltage().readback == volts


def test_1v8_releases_and_refuses_the_pull_ups(v3, bench):
    ch = bench.free_pull_la
    v3.set_la_voltage(3.3)
    before = v3.pull_state(ch).enabled
    try:
        v3.enable_pullup(ch)
        v3.set_la_voltage(1.8)
        # the pull-ups are referenced to +3V3: left engaged they would lift a 1.8 V line to 3.3 V
        assert not [la for la in v3.enabled_pulls() if la <= 6], v3.enabled_pulls()
        with pytest.raises(FirmwareError, match="1.8"):
            v3.enable_pullup(ch)
    finally:
        v3.set_la_voltage(3.3)
        v3.set_pull(ch, before)


def test_gpio_and_capture_work_on_the_1v8_bank(v3, bench):
    if not v3.capabilities.la_pins:
        pytest.skip("the pod firmware has no LA pin modes (capability la_pins)")
    ch = bench.free_la[0]
    v3.set_la_voltage(1.8)
    try:
        pin = v3.gpio(ch, "output", level=1)
        assert _level(v3, ch) == 1
        pin.low()
        assert _level(v3, ch) == 0
    finally:
        v3.release_gpio(ch)


# -- target-reset pin -----------------------------------------------------------------------

def test_reset_pin_state_follows_hold_and_release(v3):
    assert isinstance(v3.set_reset(True), ResetState)
    assert v3.reset_state().asserted
    assert not v3.set_reset(False).asserted
    assert not v3.reset_state().asserted


def test_reset_pulse_is_held_pod_side(v3):
    start = time.monotonic()
    state = v3.reset_target(pulse=0.2)
    assert time.monotonic() - start >= 0.19, "the reply came before the pulse ended"
    assert not state.asserted


def test_reset_pulse_longer_than_the_firmware_allows_is_refused(v3):
    with pytest.raises(ValueError):
        v3.reset_target(pulse=2.0)


@pytest.mark.parametrize("bank", [3.3, 1.8])
def test_reset_pin_is_open_drain_to_the_bank_rail(nrst_la, v3, bank):
    v3.set_la_voltage(bank)
    assert _level(v3, nrst_la) == 1, "released, the pod's pull-up must hold the reset line high"
    v3.set_reset(True)
    try:
        assert _level(v3, nrst_la) == 0, "asserted, the reset line must be low"
    finally:
        v3.set_reset(False)
    assert _level(v3, nrst_la) == 1


def test_reset_pulse_width_on_the_wire(nrst_la, v3):
    width = 0.02
    la, _ = events_during(lambda: v3.capture_la(2_000_000, sample_rate_hz=1_000_000),
                          lambda k: v3.reset_target(pulse=width), count=3, interval=0.2)
    pulses = la.pulse_widths(nrst_la, level=0)
    assert len(pulses) == 3, f"expected 3 reset pulses on LA{nrst_la}, saw {len(pulses)}"
    assert all(p == pytest.approx(width, abs=0.003) for p in pulses), pulses


# -- USB-C CC -------------------------------------------------------------------------------

def test_usb_cc_report_is_self_consistent(v3):
    cc = v3.usb_cc()
    assert isinstance(cc, UsbCcStatus) and cc.raw.get("supported") is True, cc
    live = max(cc.cc1_voltage, cc.cc2_voltage)
    if live < 0.2:
        assert cc.orientation == "none" and cc.advertised == "none", cc
        return
    assert cc.orientation == ("cc1" if cc.cc1_voltage >= cc.cc2_voltage else "cc2"), cc
    expected = "default" if live < 0.66 else "1.5A" if live < 1.23 else "3.0A"
    assert cc.advertised == expected, cc
    # the other line sits at our own Rd (~0 V) or a cable's Ra, well under the live one
    assert min(cc.cc1_voltage, cc.cc2_voltage) < live / 2, cc


def test_usb_cc_sees_the_attached_cable(v3):
    """Run with the pod's USB-C port plugged into a host or charger (BENCHPOD_E2E_USB_CC=1)."""
    if os.environ.get("BENCHPOD_E2E_USB_CC") != "1":
        pytest.skip("plug the pod's USB-C port into a host or charger and set BENCHPOD_E2E_USB_CC=1")
    cc = v3.usb_cc()
    assert cc.orientation in ("cc1", "cc2") and cc.advertised_current >= 0.5, cc
