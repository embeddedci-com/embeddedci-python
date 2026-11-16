"""UART parity and stop bits: open_uart / capture_uart refuse old firmware before sending, check
their arguments, and pass the frame format to the transport; the TCP transport sends the fields
only for a non-8N1 frame (firmware command_handler_uart.c uart_proxy_start)."""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import BenchPod, UnsupportedFeatureError
from embeddedci.benchpod.transport.tcp import TcpTransport


class UartPod:
    """Answers status with the given caps and records uart_proxy_start calls."""

    def __init__(self, caps: List[str], version: str = "3.10.8") -> None:
        self.caps = caps
        self.version = version
        self.starts: List[Dict[str, Any]] = []

    def status(self) -> Dict[str, Any]:
        return {"board": "stm32h563", "adc_bits": 16, "version": self.version, "caps": self.caps,
                "la_voltage": 3.3, "gateware": 56 if "uart_frame" in self.caps else 55}

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass

    def command(self, req: dict) -> Any:
        raise AssertionError(f"unexpected command {req}")

    def uart_proxy_start(self, rx: int, tx: int, baud: int, **frame: Any) -> Any:
        self.starts.append({"rx": rx, "tx": tx, "baud": baud, **frame})
        return object()


def _bp(caps: List[str], version: str = "3.10.8") -> "tuple[BenchPod, UartPod]":
    pod = UartPod(caps, version)
    return BenchPod(transport=pod, lease=False), pod


def test_8n1_needs_no_capability_and_sends_no_frame_fields():
    bp, pod = _bp(["la_pins"], "3.10.7")
    bp._uart_link(5, 4, 9600)
    bp._uart_link(5, 4, 9600, "none", 1)
    assert pod.starts == [{"rx": 5, "tx": 4, "baud": 9600}] * 2


def test_parity_and_stop_bits_reach_the_transport():
    bp, pod = _bp(["la_pins", "uart_frame"])
    bp._uart_link(5, 4, 19200, "even", 1)
    bp._uart_link(5, 4, 19200, "none", 2)
    bp._uart_link(5, 4, 19200, "odd", 2)
    assert pod.starts == [
        {"rx": 5, "tx": 4, "baud": 19200, "parity": "even", "stop_bits": 1},
        {"rx": 5, "tx": 4, "baud": 19200, "parity": "none", "stop_bits": 2},
        {"rx": 5, "tx": 4, "baud": 19200, "parity": "odd", "stop_bits": 2},
    ]


def test_old_firmware_is_refused_before_anything_is_sent():
    bp, pod = _bp(["la_pins"], "3.10.7")
    with pytest.raises(UnsupportedFeatureError) as info:
        bp.open_uart(rx=5, tx=4, baud=19200, parity="even")
    msg = str(info.value)
    assert "UART parity and stop bits need firmware 3.10.8+" in msg and "3.10.7" in msg
    assert info.value.feature == "uart_frame"
    with pytest.raises(UnsupportedFeatureError):
        bp.capture_uart(rx=5, tx=4, baud=19200, duration=0.1, stop_bits=2)
    assert pod.starts == []


@pytest.mark.parametrize("kw", [{"parity": "mark"}, {"parity": "E"}, {"stop_bits": 3},
                                {"stop_bits": 1.5}, {"stop_bits": True}])
def test_bad_frame_arguments(kw):
    bp, pod = _bp(["la_pins", "uart_frame"])
    with pytest.raises(ValueError):
        bp.open_uart(rx=5, tx=4, **kw)
    assert pod.starts == []


def test_capabilities_read_uart_frame():
    bp, _ = _bp(["la_pins", "uart_frame"])
    assert bp.supports("uart_frame")
    bp, _ = _bp(["la_pins"])
    assert not bp.supports("uart_frame")


def _serve_once(seen: List[dict]):
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)

    def run():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                seen.append(json.loads(buf.partition(b"\n")[0]))
                conn.sendall(b'{"status":"ok","data":"uart ready"}\n')

    threading.Thread(target=run, daemon=True).start()
    return srv, "127.0.0.1:%d" % srv.getsockname()[1]


def test_tcp_sends_the_frame_fields_only_when_not_8n1():
    seen: List[dict] = []
    srv, addr = _serve_once(seen)
    try:
        t = TcpTransport(addr, timeout=2)
        t.uart_proxy_start(5, 4, 115200).close()
        t.uart_proxy_start(5, 4, 9600, parity="even").close()
        t.uart_proxy_start(5, 4, 9600, parity="none", stop_bits=2).close()
    finally:
        srv.close()
    assert seen == [
        {"cmd": "uart_proxy_start", "rx": 5, "tx": 4, "baud": 115200},
        {"cmd": "uart_proxy_start", "rx": 5, "tx": 4, "baud": 9600, "parity": "even"},
        {"cmd": "uart_proxy_start", "rx": 5, "tx": 4, "baud": 9600, "stop_bits": 2},
    ]
