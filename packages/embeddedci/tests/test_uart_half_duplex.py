"""One-wire UART (half_duplex) and the pod-timed boot-mode entry (UartEntry): refused before
anything is sent on pods without them, checked, and sent by the TCP transport only when asked
for (firmware command_handler_uart.c uart_proxy_start, gateware v57)."""

from __future__ import annotations

from typing import List

import pytest

from embeddedci.benchpod import UartEntry, UnsupportedFeatureError
from embeddedci.benchpod.transport.tcp import TcpTransport

from test_uart_frame import _bp, _serve_once

HD = ["la_pins", "uart_frame", "uart_hd", "nrst_pin"]


def test_half_duplex_uses_one_pin_and_tx_defaults_to_rx():
    bp, pod = _bp(HD)
    bp._uart_link(3, None, 115200, half_duplex=True)
    bp._uart_link(3, 3, 115200, "odd", 1, half_duplex=True)
    assert pod.starts == [
        {"rx": 3, "tx": 3, "baud": 115200, "half_duplex": True},
        {"rx": 3, "tx": 3, "baud": 115200, "parity": "odd", "stop_bits": 1, "half_duplex": True},
    ]


def test_half_duplex_refuses_two_pins():
    bp, pod = _bp(HD)
    with pytest.raises(ValueError, match="one pin"):
        bp.open_uart(rx=3, tx=4, half_duplex=True)
    assert pod.starts == []


def test_entry_reaches_the_transport():
    bp, pod = _bp(HD)
    e = UartEntry(reset_ms=5, hold_ms=3, idle_ms=2, data=b"\x00")
    bp._uart_link(3, None, 115200, half_duplex=True, entry=e)
    assert pod.starts == [{"rx": 3, "tx": 3, "baud": 115200, "half_duplex": True, "entry": e}]
    assert e.to_request() == {"reset_ms": 5, "tx_low": True, "hold_ms": 3, "idle_ms": 2, "hex": "00"}
    assert UartEntry().to_request() == {"reset_ms": 10, "tx_low": True, "hold_ms": 10, "idle_ms": 10}


def test_old_pods_are_refused_before_anything_is_sent():
    bp, pod = _bp(["la_pins", "uart_frame", "nrst_pin"])
    with pytest.raises(UnsupportedFeatureError) as info:
        bp.open_uart(rx=3, half_duplex=True)
    assert info.value.feature == "uart_hd" and "gateware v57" in str(info.value)
    with pytest.raises(UnsupportedFeatureError):
        bp.capture_uart(rx=1, tx=2, duration=0.1, entry=UartEntry())
    bp, pod2 = _bp(["la_pins", "uart_frame", "uart_hd"])          # no reset pin
    with pytest.raises(UnsupportedFeatureError) as info:
        bp.open_uart(rx=1, tx=2, entry=UartEntry())
    assert info.value.feature == "nrst_pin"
    bp._uart_link(3, None, 115200, half_duplex=True)              # half duplex alone is fine
    assert pod.starts == [] and len(pod2.starts) == 1


@pytest.mark.parametrize("kw", [{"reset_ms": 1001}, {"hold_ms": -1}, {"idle_ms": 1.5},
                                {"reset_ms": True}, {"data": bytes(33)}, {"data": "00"}])
def test_bad_entry_values(kw):
    with pytest.raises(ValueError):
        UartEntry(**kw)


def test_entry_must_be_a_uart_entry():
    bp, pod = _bp(HD)
    with pytest.raises(ValueError, match="UartEntry"):
        bp.open_uart(rx=1, tx=2, entry={"reset_ms": 5})  # type: ignore[arg-type]
    assert pod.starts == []


def test_tcp_sends_half_duplex_and_entry_only_when_set():
    seen: List[dict] = []
    srv, addr = _serve_once(seen)
    try:
        t = TcpTransport(addr, timeout=2)
        t.uart_proxy_start(3, 4, 115200).close()
        t.uart_proxy_start(3, 3, 115200, half_duplex=True).close()
        t.uart_proxy_start(3, 3, 115200, half_duplex=True,
                           entry=UartEntry(tx_low=False, data=b"\x3a\xff")).close()
    finally:
        srv.close()
    assert seen == [
        {"cmd": "uart_proxy_start", "rx": 3, "tx": 4, "baud": 115200},
        {"cmd": "uart_proxy_start", "rx": 3, "tx": 3, "baud": 115200, "half_duplex": True},
        {"cmd": "uart_proxy_start", "rx": 3, "tx": 3, "baud": 115200, "half_duplex": True,
         "entry": {"reset_ms": 10, "tx_low": False, "hold_ms": 10, "idle_ms": 10, "hex": "3aff"}},
    ]
