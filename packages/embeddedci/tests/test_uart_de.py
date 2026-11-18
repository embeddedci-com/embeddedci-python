"""RS-485 DE driven by the pod (``open_uart(de=...)``, gateware v58, feature ``uart_de``): refused
before anything is sent on pods without it, checked, and sent by the TCP transport only when
asked for (firmware command_handler_uart.c uart_proxy_start)."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import BenchPod, UnsupportedFeatureError
from embeddedci.benchpod.transport.tcp import TcpTransport

from test_uart_frame import _serve_once


class TablePod:
    """A pod after 3.10.10: status carries features_rev and the running gateware, no caps list."""

    def __init__(self, features_rev: int, gateware: int) -> None:
        self.features_rev = features_rev
        self.gateware = gateware
        self.starts: List[Dict[str, Any]] = []

    def status(self) -> Dict[str, Any]:
        return {"board": "stm32h563", "adc_bits": 16, "version": "3.10.11", "la_voltage": 3.3,
                "features_rev": self.features_rev, "gateware": self.gateware, "fpga_image": "deep",
                "hw": ["nrst_pin"]}

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass

    def command(self, req: dict) -> Any:
        if req.get("cmd") == "status":
            return self.status()
        raise AssertionError(f"unexpected command {req}")

    def uart_proxy_start(self, rx: int, tx: int, baud: int, **kw: Any) -> Any:
        self.starts.append({"rx": rx, "tx": tx, "baud": baud, **kw})
        return object()


def _bp(features_rev: int = 2, gateware: int = 58) -> "tuple[BenchPod, TablePod]":
    pod = TablePod(features_rev, gateware)
    return BenchPod(transport=pod, lease=False, wiring={"version": 2}), pod


def test_de_reaches_the_transport():
    bp, pod = _bp()
    assert bp.supports("uart_de")
    bp._uart_link(5, 4, 19200, "even", 1, de=9)
    bp._uart_link(5, 4, 9600, de=6, de_active_low=True)
    assert pod.starts == [
        {"rx": 5, "tx": 4, "baud": 19200, "parity": "even", "stop_bits": 1, "de": 9},
        {"rx": 5, "tx": 4, "baud": 9600, "de": 6, "de_active_low": True},
    ]


def test_without_de_nothing_changes():
    bp, pod = _bp()
    bp._uart_link(5, 4, 19200)
    assert pod.starts == [{"rx": 5, "tx": 4, "baud": 19200}]


@pytest.mark.parametrize("features_rev,gateware", [(1, 58), (2, 57)])
def test_old_pods_are_refused_before_anything_is_sent(features_rev, gateware):
    bp, pod = _bp(features_rev, gateware)
    assert not bp.supports("uart_de")
    with pytest.raises(UnsupportedFeatureError) as info:
        bp.open_uart(rx=5, tx=4, de=9)
    assert info.value.feature == "uart_de" and "gateware v58" in str(info.value)
    with pytest.raises(UnsupportedFeatureError):
        bp.capture_uart(rx=5, tx=4, duration=0.1, de=9)
    assert pod.starts == []


@pytest.mark.parametrize("de", [5, 4])
def test_de_needs_its_own_pin(de):
    bp, pod = _bp()
    with pytest.raises(ValueError, match="pin of its own"):
        bp._uart_link(5, 4, 19200, de=de)
    assert pod.starts == []


def test_tcp_sends_de_only_when_set():
    seen: List[dict] = []
    srv, addr = _serve_once(seen)
    try:
        t = TcpTransport(addr, timeout=2)
        t.uart_proxy_start(5, 4, 19200).close()
        t.uart_proxy_start(5, 4, 19200, parity="even", de=9).close()
        t.uart_proxy_start(5, 4, 19200, de=9, de_active_low=True).close()
    finally:
        srv.close()
    assert seen == [
        {"cmd": "uart_proxy_start", "rx": 5, "tx": 4, "baud": 19200},
        {"cmd": "uart_proxy_start", "rx": 5, "tx": 4, "baud": 19200, "parity": "even", "de": 9},
        {"cmd": "uart_proxy_start", "rx": 5, "tx": 4, "baud": 19200, "de": 9, "de_active_low": True},
    ]
