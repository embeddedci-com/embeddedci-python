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


def test_read_until_returns_the_output_through_the_match():
    link = FakeLink()
    with UartSession(link) as uart:
        link.feed(b"SELFTEST: boot\r\nAPP_OK\r\nmore")
        assert uart.read_until("APP_OK", timeout=2) == "SELFTEST: boot\r\nAPP_OK"
        assert uart.read() == "\r\nmore"
        assert "SELFTEST" in uart.text and "APP_OK" in uart.lines  # history keeps everything


def test_event_based_banner_arrives_after_open():
    """The motivating case: listening starts first, the banner arrives later
    (as it would after a non-delayed power-on), and is still caught."""
    link = FakeLink()
    with UartSession(link) as uart:
        threading.Timer(0.1, lambda: link.feed(b"APP_OK\r\n")).start()
        assert uart.expect("APP_OK", timeout=2) == "APP_OK"


def test_expect_timeout_carries_text():
    link = FakeLink()
    with UartSession(link) as uart:
        link.feed(b"some noise ")
        with pytest.raises(UartTimeout) as ei:
            uart.expect("NEVER", timeout=0.2)
        assert "some noise" in ei.value.text


def test_expect_regex_returns_the_match():
    link = FakeLink()
    with UartSession(link) as uart:
        link.feed(b"rx_byte_count=42\r\n")
        m = uart.expect(re.compile(r"rx_byte_count=(\d+)"), timeout=2)
        assert m.group(1) == "42"
        assert uart.read() == "\r\n"


def test_expect_marks_the_match_read_so_a_repeated_prompt_needs_new_output():
    link = FakeLink()
    with UartSession(link) as uart:
        link.feed(b"> ")
        uart.expect("> ", timeout=2)
        with pytest.raises(UartTimeout):
            uart.expect("> ", timeout=0.2)
        link.feed(b"help\r\n> ")
        assert uart.read_until("> ", timeout=2) == "help\r\n> "


def test_read_returns_only_new_output():
    link = FakeLink()
    with UartSession(link) as uart:
        link.feed(b"aaa")
        assert uart.read(timeout=2) == "aaa"
        assert uart.read() == ""
        threading.Timer(0.1, lambda: link.feed(b"bbb")).start()
        assert uart.read(timeout=2) == "bbb"
        assert uart.text == "aaabbb"


def test_read_until_timeout_leaves_the_output_unread():
    link = FakeLink()
    with UartSession(link) as uart:
        link.feed(b"partial")
        assert uart.read_until("END", timeout=0.2) is None
        assert uart.read() == "partial"


def test_predicate_marks_everything_unread_as_read():
    link = FakeLink()
    with UartSession(link) as uart:
        link.feed(b"count=3\r\n")
        assert uart.expect(lambda text: "count=" in text, timeout=2) is True
        assert uart.read() == ""


def test_multibyte_characters_split_across_reads_and_invalid_bytes():
    link = FakeLink()
    with UartSession(link, chunk=1) as uart:
        link.feed("é".encode() + b"\xff" + b"ok")
        assert uart.read_until("ok", timeout=2) == "é�ok"


def test_overflow_keeps_the_newest_output():
    link = FakeLink()
    with UartSession(link, max_buffer=10) as uart:
        link.feed(b"0123456789ABCDEF")
        uart.expect("F", timeout=2)
        assert uart.text == "6789ABCDEF" and uart.overflowed
        assert uart.read() == ""


def test_write_forwards_to_link():
    link = FakeLink()
    with UartSession(link) as uart:
        uart.write("ping\r\n")
        assert bytes(link.written) == b"ping\r\n"


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
