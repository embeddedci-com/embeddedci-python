"""Tier "usb": the pod's USB console.

    BENCHPOD_E2E_USB=/dev/cu.usbmodem… pytest packages/embeddedci/tests/e2e/test_e2e_usb.py

On firmware 4.0 the USB console is a text shell that answers status and ping (and identity);
every other operation, the LA voltage and target power included, fails at once with the hint to
use the network or connection 'usb' (the USB network adapter). The console announces no client API
level, so the SDK checks the firmware version there: firmware 3.x raises FirmwareTooOldError on
connect.
"""

from __future__ import annotations

import os
import time

import pytest

from embeddedci.benchpod import BenchPod, TransportError

# Not marked `hardware`: that gate wants --benchpod-connection, and this tier opens its own
# connection from BENCHPOD_E2E_USB (the `usb` fixture skips without it).


@pytest.fixture(scope="module")
def usb():
    device = os.environ.get("BENCHPOD_E2E_USB")
    if not device:
        pytest.skip("set BENCHPOD_E2E_USB to the pod's USB console (a device path or 'usb')")
    with BenchPod(device, la_voltage=None, timeout=10) as pod:
        if type(pod.transport).__name__ != "SerialTransport":
            pytest.skip("'usb' reached the pod's USB network adapter, not the console; set "
                        "BENCHPOD_E2E_USB to the console's device path")
        yield pod


def test_status_is_a_dict(usb):
    status = usb.status()
    assert status["device"] == "benchpod" and status["board"] and status["version"]
    assert usb.capabilities.board == status["board"]


def test_ping_works_and_the_la_voltage_is_a_network_command(usb):
    assert usb.ping()
    with pytest.raises(TransportError, match="network command"):
        usb.get_la_voltage()


def test_power_is_a_network_command(usb):
    with pytest.raises(TransportError, match="network command"):
        usb.power_off(1)


@pytest.mark.parametrize("call", [
    lambda p: p.adc_read("ext"),
    lambda p: p.capture_la(1024),
    lambda p: p.power_status(),
    lambda p: p.transport.dap_start(11, 12),
    lambda p: p.transport.uart_proxy_start(3, 4, 115200),
], ids=["adc_read", "capture_la", "power_status", "dap_start", "uart_proxy"])
def test_network_only_features_fail_fast(usb, call):
    start = time.monotonic()
    with pytest.raises(TransportError, match="network command"):
        call(usb)
    assert time.monotonic() - start < 5
    assert usb.status()["device"] == "benchpod"  # the console is still healthy
