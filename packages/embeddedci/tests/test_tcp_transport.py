"""TcpTransport tested against an in-process fake firmware server."""

import socket
import threading

import pytest

from embeddedci.benchpod.errors import FirmwareError
from embeddedci.benchpod.transport.tcp import TcpTransport


class FakePod:
    """A minimal JSON/TCP server that mimics the pod, one connection at a time."""

    def __init__(self, handler):
        self._handler = handler
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.addr = "127.0.0.1:%d" % self._sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                line, _, _ = buf.partition(b"\n")
                if line:
                    self._handler(conn, line)

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass


def test_ping_roundtrip():
    def handler(conn, line):
        assert b'"cmd":"ping"' in line
        conn.sendall(b'{"status":"ok","data":"pong"}\n')

    pod = FakePod(handler)
    try:
        t = TcpTransport(pod.addr, timeout=2)
        assert t.ping() == "pong"
    finally:
        pod.close()


def test_target_power_request_shape():
    seen = {}

    def handler(conn, line):
        import json

        seen.update(json.loads(line))
        conn.sendall(b'{"status":"ok","data":null}\n')

    pod = FakePod(handler)
    try:
        t = TcpTransport(pod.addr, timeout=2)
        t.target_power(1, True)
        assert seen == {"cmd": "target_power", "efuse": 1, "state": 1}
    finally:
        pod.close()


def test_firmware_error_raised():
    def handler(conn, line):
        conn.sendall(b'{"status":"error","message":"invalid la channel"}\n')

    pod = FakePod(handler)
    try:
        t = TcpTransport(pod.addr, timeout=2)
        with pytest.raises(FirmwareError):
            t.command({"cmd": "la", "la": 99, "pullup": "on"})
    finally:
        pod.close()


def test_dap_start_ack_does_not_swallow_dap_bytes():
    """The ack reader must leave everything after the newline for the bridge."""

    def handler(conn, line):
        assert b'"cmd":"dap_start"' in line
        # ack line immediately followed by raw framed CMSIS-DAP bytes
        conn.sendall(b'{"status":"ok","data":"dap ready"}\nRAWDAP0')
        # keep the connection open so the link can read the trailing bytes
        import time

        time.sleep(0.5)

    pod = FakePod(handler)
    try:
        t = TcpTransport(pod.addr, timeout=2)
        link = t.dap_start(1, 2, None)
        try:
            assert link.read(7) == b"RAWDAP0"
        finally:
            link.close()
    finally:
        pod.close()


def test_set_la_voltage_sends_mv_and_returns_state():
    seen = {}

    def handler(conn, line):
        seen["line"] = line
        conn.sendall(b'{"status":"ok","data":{"mv":3300,"st":1}}\n')

    pod = FakePod(handler)
    try:
        t = TcpTransport(pod.addr, timeout=2)
        data = t.set_la_voltage(3300)
        assert b'"cmd":"la_voltage"' in seen["line"]
        assert b'"mv":3300' in seen["line"]
        assert data == {"mv": 3300, "st": 1}
    finally:
        pod.close()


def test_get_la_voltage_queries_without_mv():
    seen = {}

    def handler(conn, line):
        seen["line"] = line
        conn.sendall(b'{"status":"ok","data":{"mv":0,"st":1}}\n')

    pod = FakePod(handler)
    try:
        t = TcpTransport(pod.addr, timeout=2)
        data = t.get_la_voltage()
        assert b'"cmd":"la_voltage"' in seen["line"]
        assert b'"mv"' not in seen["line"]  # query form omits mv
        assert data == {"mv": 0, "st": 1}
    finally:
        pod.close()


def test_client_set_la_voltage_normalizes_volts_and_mv():
    """BenchPod.set_la_voltage accepts volts or mV and rejects other levels."""
    from embeddedci.benchpod.client import BenchPod

    class _Xport:
        def __init__(self):
            self.mv = None

        def set_la_voltage(self, mv):
            self.mv = mv
            return {"mv": mv, "st": 1}

    bp = BenchPod.__new__(BenchPod)   # bypass __init__/connect
    bp._transport = _Xport()

    assert bp.set_la_voltage(3.3)["mv"] == 3300 and bp._transport.mv == 3300
    assert bp.set_la_voltage(1.8)["mv"] == 1800 and bp._transport.mv == 1800
    assert bp.set_la_voltage(3300)["mv"] == 3300
    assert bp.set_la_voltage(1800)["mv"] == 1800
    for bad in (5.0, 1200, 0, 2.5):
        with pytest.raises(ValueError):
            bp.set_la_voltage(bad)
