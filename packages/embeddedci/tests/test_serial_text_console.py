"""The STM32 pod's USB console is a text shell with no JSON mode: the serial transport answers
status / ping / LA voltage / power through text commands and fails fast on everything else."""

from __future__ import annotations

import time

import pytest

from embeddedci.benchpod.client import BenchPod
from embeddedci.benchpod.errors import FirmwareError, TransportError
from embeddedci.benchpod.transport.serial import SerialTransport, parse_text_status

STATUS = (
    "  device : benchpod\r\n"
    "  mac    : 02:00:00:42:00:01\r\n"
    "  ip     : 192.168.1.215\r\n"
    "  board  : stm32h563  fw v0.3.0-dev  rev v2  nrst_pin=no  usb_cc=no\r\n"
    "  fpga   : gateware v31 (reachable)  status_reg=0x04\r\n"
    "  adc    : 16-bit x1, 4096 mV full-scale\r\n"
)


class TextConsolePort:
    """Emulates the STM32 console: echo, text replies, '> ' prompt, no JSON mode."""

    def __init__(self, rev: str = "v2") -> None:
        self._in = bytearray()
        self._line = bytearray()
        self.lines: list = []
        self.la_mv = 0
        self.rev = rev
        self.timeout = 0.25

    def write(self, data: bytes):
        for b in bytes(data):
            if b in (0x0A, 0x0D):
                if self._line:
                    self._process(self._line.decode())
                self._line.clear()
            else:
                self._line.append(b)
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self._in.clear()

    def read(self, n=1):
        out = bytes(self._in[:n])
        del self._in[:n]
        return out

    def close(self):
        pass

    def _reply(self, text: str) -> None:
        self._in.extend((text + "> ").encode())

    def _process(self, s: str) -> None:
        self.lines.append(s)
        argv = s.split()
        echo = s + "\r\n"
        if argv[0] == "status":
            self._reply(echo + STATUS)
        elif argv[0] == "ping":
            self._reply(echo + "  PING ok  version=0x1f\r\n")
        elif argv[0] == "la-voltage":
            if len(argv) > 1:
                if argv[1] == "1800" and self.rev == "v2":
                    self._reply(echo + "  1.8 V needs a v3 pod; this board is v2 (its TPS2116 has no 1.8 V setting)\r\n")
                    return
                self.la_mv = int(argv[1])
            state = f"{self.la_mv} mV" if self.la_mv else "UNSET"
            self._reply(echo + f"  LA VCCIO = {state} (st=1)\r\n")
        elif argv[0] == "power" and len(argv) >= 3:
            self._reply(echo + f"  eFuse{argv[1]} {argv[2].upper()}\r\n")
        else:
            self._reply(echo + f"  unknown command '{s}' (try 'help')\r\n")


def _transport(**kw) -> SerialTransport:
    return SerialTransport(port=TextConsolePort(**kw), timeout=2)


def test_parse_text_status():
    s = parse_text_status("status\r\n" + STATUS + "> ")
    assert s["device"] == "benchpod" and s["board"] == "stm32h563"
    assert s["version"] == "0.3.0-dev" and s["board_rev"] == "v2" and s["nrst_pin"] is False
    assert s["adc_bits"] == 16 and s["adc_fullscale_mv"] == 4096 and s["gateware"] == 31


def test_status_ping_and_la_voltage_over_the_text_console():
    t = _transport()
    assert t.status()["board"] == "stm32h563"
    assert t.json_supported is False
    assert t.ping() == "pong"
    bp = BenchPod(transport=t, la_voltage=3.3)
    assert bp.get_la_voltage().voltage == 3.3
    assert bp.capabilities.adc_bits == 16


def test_la_voltage_refusal_is_a_firmware_error():
    bp = BenchPod(transport=_transport(), la_voltage=None)
    with pytest.raises(FirmwareError, match="v3 pod"):
        bp.set_la_voltage(1.8)


def test_power_uses_the_power_verb():
    t = _transport()
    t.target_power(1, True)
    assert t._port.lines[-1] == "power 1 on"
    with pytest.raises(TransportError, match="delayed"):
        t.target_power(1, True, delay_ms=500)


def test_json_only_features_fail_fast_with_a_hint():
    bp = BenchPod(transport=_transport(), la_voltage=None)
    start = time.monotonic()
    with pytest.raises(TransportError, match="network"):
        bp.adc_read("ext")
    with pytest.raises(TransportError, match="network"):
        bp.capture_la(1024)
    assert time.monotonic() - start < 3


def test_flash_and_uart_proxy_fail_fast_with_a_hint():
    t = _transport()
    start = time.monotonic()
    with pytest.raises(TransportError, match="network"):
        t.dap_start(11, 12)
    with pytest.raises(TransportError, match="network"):
        t.uart_proxy_start(5, 4, 115200)
    assert time.monotonic() - start < 3
