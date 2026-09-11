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
        link = t.dap_start(1, 2)
        try:
            assert link.read(7) == b"RAWDAP0"
        finally:
            link.close()
    finally:
        pod.close()


def test_client_la_voltage_round_trips_over_tcp():
    seen = []

    def handler(conn, line):
        seen.append(line)
        if b'"mv"' in line:
            conn.sendall(b'{"status":"ok","data":{"mv":3300,"st":1,"readback_mv":3298}}\n')
        else:
            conn.sendall(b'{"status":"ok","data":{"mv":0,"st":1}}\n')

    from embeddedci.benchpod.client import BenchPod

    pod = FakePod(handler)
    try:
        bp = BenchPod(transport=TcpTransport(pod.addr, timeout=2), la_voltage=3.3)
        assert b'"cmd":"la_voltage"' in seen[0] and b'"mv":3300' in seen[0]
        state = bp.get_la_voltage()
        assert b'"mv"' not in seen[-1]  # query form omits mv
        assert state.voltage is None
    finally:
        pod.close()
