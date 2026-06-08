"""Emulated I2C sensor command shapes + chunked reads (no hardware)."""

import json
import socket
import threading

import pytest

from embeddedci.benchpod import sensor
from embeddedci.benchpod.errors import BenchPodError
from embeddedci.benchpod.transport.tcp import TcpTransport


class FakePod:
    """Dispatches one JSON command per connection; records the last request."""

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
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            req = json.loads(buf.split(b"\n", 1)[0])
            self.requests.append(req)
            conn.sendall(self._reply(req))

    def _reply(self, req):
        cmd = req.get("cmd")
        if cmd == "sensor_start":
            addr = req.get("addr", "0x76")
            a = int(addr, 0) if isinstance(addr, str) else int(addr)
            return (b'{"status":"ok","data":{"type":"%s","addr":%d,"sda":%d,"scl":%d}}\n'
                    % (req["type"].encode(), a, req["sda"], req["scl"]))
        if cmd == "sensor_set":
            return b'{"status":"ok","data":{"type":"bmp280"}}\n'
        if cmd == "sensor_status":
            return (b'{"status":"ok","data":{"active":true,"type":"bmp280",'
                    b'"addr":118,"transactions":5,"writes":3,"last_reg":244,"last_val":1}}\n')
        if cmd == "sensor_regs":
            # two chunks of register bytes
            return (b'{"status":"ok","data":[88,0,0],"more":true}\n'
                    b'{"status":"chunk","data":[1,2,3],"more":false}\n')
        if cmd == "sensor_stop":
            return b'{"status":"ok","data":null}\n'
        return b'{"status":"error","message":"unknown cmd"}\n'

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass


def test_sensor_start_request_shape():
    pod = FakePod()
    try:
        t = TcpTransport(pod.addr, timeout=2)
        data = sensor.sensor_start(t, "bmp280", sda=7, scl=8, address=0x77)
        req = pod.requests[-1]
        assert req == {"cmd": "sensor_start", "type": "bmp280",
                       "addr": "0x77", "sda": 7, "scl": 8}
        assert data["addr"] == 0x77
    finally:
        pod.close()


def test_sensor_set_requires_a_value():
    pod = FakePod()
    try:
        t = TcpTransport(pod.addr, timeout=2)
        with pytest.raises(BenchPodError):
            sensor.sensor_set(t)
        sensor.sensor_set(t, temperature_c=21.5)
        assert pod.requests[-1] == {"cmd": "sensor_set", "temperature_c": 21.5}
    finally:
        pod.close()


def test_sensor_status_and_regs():
    pod = FakePod()
    try:
        t = TcpTransport(pod.addr, timeout=2)
        st = sensor.sensor_status(t)
        assert st["active"] is True and st["transactions"] == 5
        regs = sensor.sensor_regs(t, 0xD0, 6)
        assert regs == [88, 0, 0, 1, 2, 3]   # chunks assembled; 88 == 0x58 chip id
        assert pod.requests[-1] == {"cmd": "sensor_regs", "start": "0xd0", "len": 6}
    finally:
        pod.close()


def test_sensor_rejected_on_serial_transport():
    class FakeSerial:  # no command()/samples()
        pass
    with pytest.raises(BenchPodError):
        sensor.sensor_start(FakeSerial(), "bmp280", sda=1, scl=2)
