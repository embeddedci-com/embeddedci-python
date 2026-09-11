"""Tier "usb": the pod's USB console.

    BENCHPOD_E2E_USB=/dev/cu.usbmodem… pytest packages/embeddedci/tests/e2e/test_e2e_usb.py

On the STM32 pod the USB console is a text shell: status, ping, LA voltage and power work; every
other operation must fail at once with a hint to use the network.
"""

from __future__ import annotations

import os
import time

import pytest

from embeddedci.benchpod import BenchPod, FirmwareError, TransportError

# Not marked `hardware`: that gate wants --benchpod-connection, and this tier opens its own
# connection from BENCHPOD_E2E_USB (the `usb` fixture skips without it).


@pytest.fixture(scope="module")
def usb(benchpod_la_voltage):
    device = os.environ.get("BENCHPOD_E2E_USB")
    if not device:
        pytest.skip("set BENCHPOD_E2E_USB to the pod's USB console (a device path or 'usb')")
    with BenchPod(device, la_voltage=benchpod_la_voltage, timeout=10) as pod:
        yield pod


def test_status_is_a_dict(usb):
    status = usb.status()
    assert status["device"] == "benchpod" and status["board"] and status["version"]
    assert usb.capabilities.board == status["board"]


def test_ping_and_la_voltage(usb):
    assert usb.ping()
    assert usb.get_la_voltage().voltage == 3.3


def test_1v8_on_a_v2_board(usb):
    if usb.status().get("board_rev") != "v2":
        pytest.skip("rev3 pods accept 1.8 V")
    try:
        with pytest.raises(FirmwareError, match="v3"):
            usb.set_la_voltage(1.8)
    finally:
        usb.set_la_voltage(3.3)


def test_power_off(usb):
    usb.power_off(1)
    with pytest.raises(TransportError, match="delayed"):
        usb.power_on(1, delay=0.5)


@pytest.mark.parametrize("call", [
    lambda p: p.adc_read("ext"),
    lambda p: p.capture_la(1024),
    lambda p: p.power_status(),
    lambda p: p.transport.dap_start(11, 12),
    lambda p: p.transport.uart_proxy_start(3, 4, 115200),
], ids=["adc_read", "capture_la", "power_status", "dap_start", "uart_proxy"])
def test_network_only_features_fail_fast(usb, call):
    start = time.monotonic()
    with pytest.raises(TransportError, match="network"):
        call(usb)
    assert time.monotonic() - start < 5
    assert usb.status()["device"] == "benchpod"  # the console is still healthy
