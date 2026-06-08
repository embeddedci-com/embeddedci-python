"""target_power delay_ms wiring (no hardware)."""

import json
import socket
import threading

from embeddedci import benchpod as bp
from embeddedci.benchpod.client import BenchPod
from embeddedci.benchpod.transport.tcp import TcpTransport


class FakePod:
    def __init__(self):
        self.requests = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.addr = "127.0.0.1:%d" % self._sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while b"\n" not in buf:
                    c = conn.recv(4096)
                    if not c:
                        break
                    buf += c
                if buf:
                    self.requests.append(json.loads(buf.split(b"\n", 1)[0]))
                    conn.sendall(b'{"status":"ok","data":{"efuse":1,"enabled":1,"delay_ms":0}}\n')

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass


def test_target_power_no_delay_omits_field():
    pod = FakePod()
    try:
        TcpTransport(pod.addr, timeout=2).target_power(1, True)
        assert pod.requests[-1] == {"cmd": "target_power", "efuse": 1, "state": 1}
    finally:
        pod.close()


def test_target_power_with_delay_sends_delay_ms():
    pod = FakePod()
    try:
        TcpTransport(pod.addr, timeout=2).target_power(2, True, 2000)
        assert pod.requests[-1] == {
            "cmd": "target_power", "efuse": 2, "state": 1, "delay_ms": 2000
        }
    finally:
        pod.close()


def test_client_power_on_delay_seconds_to_ms():
    pod = FakePod()
    try:
        device = BenchPod(transport=TcpTransport(pod.addr, timeout=2))
        device.power_on(bp.INTERNAL, delay=1.5)
        assert pod.requests[-1]["delay_ms"] == 1500
        assert pod.requests[-1]["state"] == 1
    finally:
        pod.close()
