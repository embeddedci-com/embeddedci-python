"""UartSession tested against a fake streaming link (no hardware).

FakeLink mimics a RawLink: read() blocks until bytes are fed or the link is
closed (returning b"" on close), matching the pod's UART proxy.
"""
import re
import threading

import pytest

from embeddedci.benchpod.uart import UartSession
from embeddedci.benchpod.errors import UartTimeout


class FakeLink:
    def __init__(self) -> None:
        self._q = bytearray()
        self._cond = threading.Condition()
        self._closed = False
        self.written = bytearray()

    def feed(self, data: bytes) -> None:
        with self._cond:
            self._q.extend(data)
            self._cond.notify_all()

    def read(self, n: int) -> bytes:
        with self._cond:
            while not self._q and not self._closed:
                self._cond.wait()
            if self._q:
                out = bytes(self._q[:n])
                del self._q[:n]
                return out
            return b""  # closed -> EOF

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


def test_read_until_substring():
    link = FakeLink()
    with UartSession(link) as uart:
        link.feed(b"SELFTEST: boot\r\nAPP_OK\r\n")
        assert uart.read_until("APP_OK", timeout=2) == "APP_OK"
        assert "SELFTEST" in uart.text
        assert "APP_OK" in uart.lines


def test_event_based_banner_arrives_after_open():
    """The motivating case: listening starts first, the banner arrives later
    (as it would after a non-delayed power-on), and is still caught."""
    link = FakeLink()
    with UartSession(link) as uart:
        threading.Timer(0.1, lambda: link.feed(b"APP_OK\r\n")).start()
        assert uart.expect("APP_OK", timeout=2)


def test_expect_timeout_carries_text():
    link = FakeLink()
    with UartSession(link) as uart:
        link.feed(b"some noise ")
        with pytest.raises(UartTimeout) as ei:
            uart.expect("NEVER", timeout=0.2)
        assert "some noise" in ei.value.text


def test_regex_match_returns_match():
    link = FakeLink()
    with UartSession(link) as uart:
        link.feed(b"rx_byte_count=42\r\n")
        m = uart.read_until(re.compile(r"rx_byte_count=(\d+)"), timeout=2)
        assert m.group(1) == "42"


def test_write_forwards_to_link():
    link = FakeLink()
    with UartSession(link) as uart:
        uart.write("ping\r\n")
        assert bytes(link.written) == b"ping\r\n"


def test_drain_returns_only_new():
    link = FakeLink()
    with UartSession(link) as uart:
        link.feed(b"aaa")
        assert uart.read_until("aaa", timeout=2)
        assert "aaa" in uart.drain()
        link.feed(b"bbb")
        assert uart.read_until("bbb", timeout=2)
        second = uart.drain()
        assert second == "bbb" and "aaa" not in second


def test_close_stops_reader_and_returns_none():
    link = FakeLink()
    uart = UartSession(link)
    uart.close()
    assert link._closed is True
    assert uart.closed is True
    assert uart.read_until("x", timeout=1) is None


def test_read_until_none_when_link_ends():
    link = FakeLink()
    with UartSession(link) as uart:
        link.close()  # EOF with no match
        assert uart.read_until("APP_OK", timeout=2) is None
