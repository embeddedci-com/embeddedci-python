"""UART proxy ack handling + capture() behaviour (no hardware)."""

import socket
import threading
import time

from embeddedci.benchpod import uart
from embeddedci.benchpod.transport.tcp import TcpTransport


class _MemLink:
    """In-memory RawLink: yields preloaded chunks, then blocks until closed."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._closed = False
        self.close_calls = 0

    def read(self, n):
        if self._chunks:
            return self._chunks.pop(0)
        while not self._closed:
            time.sleep(0.01)
        return b""

    def write(self, data):
        return len(data)

    def close(self):
        self.close_calls += 1
        self._closed = True


def test_capture_stops_on_until_substring():
    link = _MemLink([b"boot\r\n", b"APP_OK\r\n", b"more\r\n"])
    cap = uart.capture(link, duration=5, until="APP_OK")
    assert cap.matched
    assert "APP_OK" in cap.text
    assert "APP_OK" in cap  # __contains__
    assert "boot" in cap.lines
    assert link.close_calls >= 1  # closed (idempotent in real transports)


def test_capture_stops_on_duration_without_match():
    link = _MemLink([b"hello\n"])
    start = time.monotonic()
    cap = uart.capture(link, duration=0.3, until="NEVER")
    assert not cap.matched
    assert 0.25 <= time.monotonic() - start < 2.0
    assert cap.lines == ["hello"]
    assert link.close_calls >= 1  # closed (idempotent in real transports)


def test_capture_regex_and_helpers():
    link = _MemLink([b"chip id match=0x58 at addr=0x76\n"])
    cap = uart.capture(link, duration=2, until=r"match=0x58")
    assert cap.matched
    assert cap.match(r"match=0x[0-9a-f]{2}")
    assert cap.contains("addr=0x76")


# --- uart_proxy_start ack reader (must not swallow DUT bytes) --------------

class _FakeUartPod:
    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.addr = "127.0.0.1:%d" % self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self._sock.accept()
        with conn:
            buf = b""
            while b"\n" not in buf:
                buf += conn.recv(256)
            assert b'"cmd":"uart_proxy_start"' in buf
            # ack line immediately followed by raw DUT bytes
            conn.sendall(b'{"status":"ok","data":"uart ready"}\nAPP_OK\r\n')
            time.sleep(0.5)

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


def test_uart_proxy_ack_does_not_swallow_bytes():
    pod = _FakeUartPod()
    try:
        t = TcpTransport(pod.addr, timeout=2)
        link = t.uart_proxy_start(5, 6, 115200)
        try:
            assert link.read(8) == b"APP_OK\r\n"
        finally:
            link.close()
    finally:
        pod.close()
