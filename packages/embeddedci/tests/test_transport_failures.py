"""Transport failure paths no hardware run exercises on purpose (T-6).

TCP connects that never succeed and links that reset mid-stream or mid-upload, large cloud tunnel
frames in both directions, stale serial replies before each kind of request, UART link errors on
write and on a real TCP proxy socket, and a blocking ``measure_power`` that outlasts the
transport's own timeout. Every failure must surface as an SDK error, never a raw ``OSError``.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time

import pytest

from embeddedci.benchpod import BenchPod
from embeddedci.benchpod.errors import (
    BenchPodError,
    ConnectionClosedError,
    TransportError,
    TransportTimeout,
    UartLinkError,
    UartTimeout,
)
from embeddedci.benchpod.transport.cloud import (
    TUNNEL_FRAME_MAX,
    TUNNEL_UPLOAD_BYTES_PER_SEC,
    CloudTransport,
    _WsTunnelSocket,
)
from embeddedci.benchpod.transport.serial import SerialTransport
from embeddedci.benchpod.transport.tcp import TcpTransport
from embeddedci.benchpod.uart import UartSession


# -- a fake pod that hands each connection to a handler ----------------------------------------

class _Pod:
    """Accepts connections and passes each socket plus its first request line to ``handler``."""

    def __init__(self, handler):
        self._handler = handler
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.addr = "127.0.0.1:%d" % self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            line = buf.partition(b"\n")[0]
            try:
                self._handler(conn, line)
            finally:
                conn.close()

    def close(self):
        self._sock.close()


def _reset(conn):
    """Close with SO_LINGER 0: the peer sees a TCP RST, not a FIN."""
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    conn.close()


@pytest.fixture
def pod_with():
    pods = []

    def make(handler, **kwargs):
        pod = _Pod(handler)
        pods.append(pod)
        kwargs.setdefault("timeout", 0.5)
        kwargs.setdefault("dial_timeout", 1)
        return TcpTransport(pod.addr, **kwargs)

    yield make
    for pod in pods:
        pod.close()


# -- TCP: dial, resets and timeouts mid-stream -------------------------------------------------

def test_a_refused_connection_gives_up_after_the_dial_budget():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens here now
    t = TcpTransport(f"127.0.0.1:{port}", timeout=1, dial_timeout=0.3)
    start = time.monotonic()
    with pytest.raises(TransportError, match="could not connect") as info:
        t.ping()
    assert time.monotonic() - start < 2
    assert not isinstance(info.value, TransportTimeout)


@pytest.mark.parametrize("addr", ["no-port", "host:notaport"])
def test_a_malformed_address_is_a_transport_error(addr):
    with pytest.raises(TransportError, match="address"):
        TcpTransport(addr).ping()


def test_a_reset_after_the_first_chunk_ends_the_stream_with_connection_closed(pod_with):
    def handler(conn, line):
        conn.sendall(b'{"status":"ok","data":{"n":1},"more":true}\n')
        time.sleep(0.05)
        _reset(conn)

    t = pod_with(handler)
    got = []
    with pytest.raises(ConnectionClosedError):
        for chunk in t.stream_chunks({"cmd": "capture_dual"}):
            got.append(chunk)
    assert got == [{"status": "ok", "data": {"n": 1}, "more": True}]


def test_a_pod_that_stalls_mid_sample_stream_is_a_timeout(pod_with):
    def handler(conn, line):
        conn.sendall(b'{"status":"ok","data":[1,2,3],"more":true}\n')
        time.sleep(1.5)

    t = pod_with(handler, timeout=0.3)
    with pytest.raises(TransportTimeout, match="within 0.3 s"):
        t.samples({"cmd": "capture", "samples": 6})


def test_a_half_line_then_close_is_not_taken_for_a_reply(pod_with):
    t = pod_with(lambda conn, line: conn.sendall(b'{"status":"ok","da'))
    with pytest.raises(BenchPodError):
        t.command({"cmd": "status"})


def test_a_reset_during_an_upload_is_connection_closed(pod_with):
    def handler(conn, line):
        conn.sendall(b'{"status":"ok","data":{"ready":4000000}}\n')
        conn.recv(1024)
        _reset(conn)

    t = pod_with(handler)
    with pytest.raises(ConnectionClosedError):
        t.load_replay(data=b"\x00" * 4_000_000, replay={"cmd": "replay"}, psram=True)


def test_a_reset_on_a_raw_link_ends_it_and_says_why(pod_with):
    def handler(conn, line):
        conn.sendall(b'{"status":"ok"}\nbanner')
        time.sleep(0.1)
        _reset(conn)

    t = pod_with(handler)
    link = t.uart_proxy_start(rx=5, tx=4, baud=115200)
    seen = bytearray()
    while True:
        chunk = link.read(64)
        if not chunk:
            break
        seen.extend(chunk)
    assert bytes(seen) == b"banner"
    assert isinstance(link.error, OSError)
    link.close()


def test_a_uart_session_over_a_reset_tcp_proxy_raises_link_error_with_the_banner(pod_with):
    def handler(conn, line):
        conn.sendall(b'{"status":"ok"}\nboot 1.2\r\n')
        time.sleep(0.1)
        _reset(conn)

    t = pod_with(handler)
    with UartSession(t.uart_proxy_start(rx=5, tx=4, baud=115200)) as uart:
        with pytest.raises(UartLinkError) as info:
            uart.expect("APP_OK", timeout=5)
        assert "boot 1.2" in info.value.text
        assert isinstance(info.value, UartTimeout)


# -- UART reader and writer errors ----------------------------------------------------------------

class _BrokenLink:
    """A raw link whose reads block until closed and whose writes fail like a dead socket."""

    def __init__(self, write_exc):
        self._write_exc = write_exc
        self._done = threading.Event()
        self.error = None

    def read(self, n):
        self._done.wait()
        return b""

    def write(self, data):
        raise self._write_exc

    def close(self):
        self._done.set()


@pytest.mark.parametrize("exc", [BrokenPipeError(32, "Broken pipe"),
                                 ConnectionClosedError("the cloud tunnel closed while sending")])
def test_a_failed_uart_write_is_a_link_error_not_a_raw_oserror(exc):
    with UartSession(_BrokenLink(exc)) as uart:
        with pytest.raises(UartLinkError, match="while writing") as info:
            uart.write("help\n")
        assert info.value.cause is exc
        assert isinstance(info.value, TransportError)


def test_output_received_before_the_reader_died_can_still_be_read():
    class _Link:
        def __init__(self):
            self._chunks = [b"ready> ", OSError("device gone")]

        def read(self, n):
            item = self._chunks.pop(0) if self._chunks else b""
            if isinstance(item, BaseException):
                raise item
            return item

        def write(self, data):
            return len(data)

        def close(self):
            pass

    uart = UartSession(_Link())
    try:
        assert uart.read_until("ready>", timeout=2) == "ready>"
        assert uart.read(timeout=1) == " "  # the rest of what arrived is still served
        with pytest.raises(UartLinkError, match="device gone"):
            uart.read()
    finally:
        uart.close()


def test_a_non_oserror_in_the_reader_is_not_swallowed():
    class _Link:
        error = None

        def read(self, n):
            raise ValueError("bad frame")

        def write(self, data):
            return len(data)

        def close(self):
            pass

    uart = UartSession(_Link())
    try:
        with pytest.raises(UartLinkError, match="bad frame"):
            uart.expect("x", timeout=2)
        assert isinstance(uart.error, ValueError)
    finally:
        uart.close()


# -- cloud tunnel: large frames both ways ------------------------------------------------------

class _WS:
    def __init__(self, frames=(), clock=None, send_cost=0.0):
        self._frames = list(frames)
        self.sent = []
        self._clock = clock
        self._send_cost = send_cost
        self.fail_after = None
        self.fail_with = None

    def recv(self):
        return self._frames.pop(0) if self._frames else b""

    def send_binary(self, data):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise self.fail_with
        self.sent.append(bytes(data))
        if self._clock is not None:
            self._clock[0] += self._send_cost

    def settimeout(self, t):
        self.timeout = t

    def close(self):
        pass


def _tunnel(monkeypatch, ws, clock):
    from embeddedci.benchpod.transport import cloud

    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock[0] += s

    monkeypatch.setattr(cloud._WsTunnelSocket, "_clock", staticmethod(lambda: clock[0]))
    monkeypatch.setattr(cloud._WsTunnelSocket, "_sleep", staticmethod(sleep))
    sock = _WsTunnelSocket.__new__(_WsTunnelSocket)
    sock._ws = ws
    sock._buf = bytearray()
    sock._closed = False
    return sock, sleeps


def test_an_upload_already_behind_its_pace_does_not_sleep(monkeypatch):
    clock = [0.0]
    # Each frame takes longer to send than the pace allows: no extra waiting on top.
    ws = _WS(clock=clock, send_cost=2 * TUNNEL_FRAME_MAX / TUNNEL_UPLOAD_BYTES_PER_SEC)
    sock, sleeps = _tunnel(monkeypatch, ws, clock)
    sock.sendall(b"\x55" * (8 * TUNNEL_FRAME_MAX))
    assert len(ws.sent) == 8 and sleeps == []


def test_a_one_mebibyte_upload_keeps_the_pace_and_the_frame_cap(monkeypatch):
    clock = [0.0]
    ws = _WS(clock=clock)
    sock, _ = _tunnel(monkeypatch, ws, clock)
    payload = bytes(range(256)) * 4096
    sock.sendall(payload)
    assert max(len(f) for f in ws.sent) == TUNNEL_FRAME_MAX
    assert b"".join(ws.sent) == payload
    # The last frame leaves (n - 1) frames after the first: 1 MiB at 128 KiB/s is about 8 s.
    assert clock[0] == pytest.approx((len(payload) - TUNNEL_FRAME_MAX) / TUNNEL_UPLOAD_BYTES_PER_SEC)


def test_a_tunnel_closing_mid_upload_is_connection_closed_through_load_replay(monkeypatch):
    import websocket

    clock = [0.0]
    ws = _WS([b'{"status":"ok","data":{"ready":20000}}\n'], clock=clock)
    ws.fail_after = 5
    ws.fail_with = websocket.WebSocketConnectionClosedException("gone")
    sock, _ = _tunnel(monkeypatch, ws, clock)
    t = CloudTransport("dev-a", api_base="https://example.test", token="x")
    monkeypatch.setattr(t, "_dial", lambda *_a: sock)
    with pytest.raises(ConnectionClosedError, match="closed while sending"):
        t.load_replay(data=b"\x00" * 20000, replay={"cmd": "replay"})
    assert len(ws.sent) == 5  # the load_bin line and four data frames, then nothing


def test_a_tunnel_that_stops_taking_data_is_a_transport_timeout(monkeypatch):
    import websocket

    clock = [0.0]
    ws = _WS([b'{"status":"ok","data":{"ready":5000}}\n'], clock=clock)
    ws.fail_after = 2
    ws.fail_with = websocket.WebSocketTimeoutException("send timed out")
    sock, _ = _tunnel(monkeypatch, ws, clock)
    t = CloudTransport("dev-a", api_base="https://example.test", token="x", timeout=5)
    monkeypatch.setattr(t, "_dial", lambda *_a: sock)
    with pytest.raises(TransportTimeout):
        t.stage_psram(b"\x01" * 5000)


def test_a_large_download_frame_carrying_many_lines_is_split_correctly(monkeypatch):
    """The server may coalesce pod output: one 64 KiB frame with many chunk lines, the last one
    cut across into the next frame."""
    lines = [json.dumps({"status": "ok", "data": list(range(i, i + 500)), "more": True}).encode()
             for i in range(0, 20000, 500)]
    lines.append(b'{"status":"ok","data":[],"more":false}')
    blob = b"\n".join(lines) + b"\n"
    cut = len(blob) - 100
    clock = [0.0]
    sock, _ = _tunnel(monkeypatch, _WS([blob[:cut], blob[cut:]]), clock)
    t = CloudTransport("dev-a", api_base="https://example.test", token="x")
    monkeypatch.setattr(t, "_dial", lambda *_a: sock)
    assert t.samples({"cmd": "capture", "samples": 20000}) == list(range(20000))


def test_a_text_frame_from_the_tunnel_is_treated_as_bytes(monkeypatch):
    clock = [0.0]
    sock, _ = _tunnel(monkeypatch, _WS(['{"status":"ok","data":"pong"}\n']), clock)
    assert sock.recv(4096) == b'{"status":"ok","data":"pong"}\n'


# -- serial: stale replies before every kind of request ----------------------------------------

def _serial():
    from test_serial_json import FakeConsolePort

    t = SerialTransport(port=FakeConsolePort(), timeout=1)
    t.command({"cmd": "ping"})  # enter JSON mode
    return t


def test_a_stale_error_reply_does_not_fail_the_next_request():
    t = _serial()
    t._port._emit('{"status":"error","message":"busy: capture running"}\n')
    assert t.command({"cmd": "ping"}) == "pong"


def test_several_stale_lines_and_a_half_line_are_all_dropped():
    t = _serial()
    t._port._emit('{"status":"ok","data":"old1"}\n[cmd] <- x\n{"status":"ok","data":"old2"}\n{"sta')
    assert t.command({"cmd": "status"})["version"] == "2.0.0"


def test_a_stale_chunk_is_dropped_before_a_sample_stream():
    t = _serial()
    t._port._emit('{"status":"ok","data":[9,9,9],"more":true}\n')
    assert t.samples({"cmd": "sensor_regs"}) == [88, 0, 1]


def test_a_stale_chunk_is_dropped_before_a_streamed_command():
    t = _serial()
    t._port._emit('{"status":"ok","data":[7],"more":false}\n')
    chunks = list(t.stream_chunks({"cmd": "sensor_regs"}))
    assert [c["data"] for c in chunks] == [[88, 0], [1]]


# -- a long blocking measure_power ----------------------------------------------------------------

def test_measure_power_longer_than_the_transport_timeout_completes_over_tcp(pod_with, monkeypatch):
    monkeypatch.delenv("BENCHPOD_LA_VOLTAGE", raising=False)
    seen = {}

    def handler(conn, line):
        req = json.loads(line)
        if req["cmd"] == "status":
            conn.sendall(b'{"status":"ok","data":{"board":"stm32h563","caps":["power_profile"]}}\n')
            return
        seen.update(req)
        time.sleep(req["duration_ms"] / 1000.0 + 0.1)  # the pod samples for the whole duration
        stats = {"n": 400, "duration_ms": req["duration_ms"], "rate_hz": 500,
                 "i_avg_ua": 12000, "i_min_ua": 10000, "i_max_ua": 15000,
                 "v_avg_mv": 5000, "energy_uj": 48000}
        conn.sendall((json.dumps({"status": "ok", "data": {"stats": stats}, "more": False})
                      + "\n").encode())

    t = pod_with(handler, timeout=0.3)
    bp = BenchPod(transport=t, lease=False)
    prof = bp.measure_power(0.8, efuse=1)
    assert seen["duration_ms"] == 800
    assert prof.duration == pytest.approx(0.8)


def test_measure_power_over_the_cloud_tunnel_waits_for_its_duration(monkeypatch):
    t = CloudTransport("dev-a", api_base="https://example.test", token="x", timeout=30)
    ws = _WS([b'{"status":"ok","data":{"stats":{"n":1}},"more":false}\n'])
    sock, _ = _tunnel(monkeypatch, ws, [0.0])
    monkeypatch.setattr(t, "_open_tunnel", lambda: sock)
    list(t.stream_chunks({"cmd": "power_profile", "efuse": 1, "duration_ms": 600_000}))
    assert ws.timeout == pytest.approx(630)


def test_measure_power_over_serial_waits_for_its_duration(monkeypatch):
    from embeddedci.benchpod.transport import base, serial

    t = _serial()
    waited = []
    real = base.request_timeout
    monkeypatch.setattr(serial, "request_timeout", lambda req, b: waited.append(real(req, b)) or 0.5)
    t._port._process = lambda line: t._port._emit('{"status":"ok","data":{"stats":{}},"more":false}\n')
    list(t.stream_chunks({"cmd": "power_profile", "duration_ms": 120_000}))
    assert waited == [pytest.approx(121)]
