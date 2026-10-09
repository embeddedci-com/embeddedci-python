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
        if cmd == "sensor_stop" or cmd == "gps_stop":
            return b'{"status":"ok","data":null}\n'
        if cmd == "sensor_types":
            return (b'{"status":"ok","data":{"types":[{"type":"sht4x","label":"Sensirion SHT4x",'
                    b'"addr":68,"alt_addr":69,"params":[{"key":"temperature_c","unit":"C",'
                    b'"min":-40,"max":125,"default":25}]}]}}\n')
        if cmd in ("gps_start", "gps_set", "gps_status"):
            return b'{"status":"ok","data":{"active":true,"tx":9,"fix":1}}\n'
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


def test_sensor_start_without_address_leaves_the_model_default():
    pod = FakePod()
    try:
        t = TcpTransport(pod.addr, timeout=2)
        sensor.sensor_start(t, sensor.Sensor.SHT4X, sda=2, scl=1)
        assert pod.requests[-1] == {"cmd": "sensor_start", "type": "sht4x", "sda": 2, "scl": 1}
    finally:
        pod.close()


def test_sensor_set_takes_any_model_parameter():
    pod = FakePod()
    try:
        t = TcpTransport(pod.addr, timeout=2)
        sensor.sensor_set(t, {"accel_x_g": 0.5, "accel_z_g": 1, "gyro_x_dps": None})
        assert pod.requests[-1] == {"cmd": "sensor_set", "accel_x_g": 0.5, "accel_z_g": 1.0}
        sensor.sensor_set(t, {"humidity_pct": 55}, temperature_c=21)
        assert pod.requests[-1] == {"cmd": "sensor_set", "humidity_pct": 55.0, "temperature_c": 21.0}
        with pytest.raises(BenchPodError):
            sensor.sensor_set(t, {"humidity_pct": None})
    finally:
        pod.close()


def test_sensor_types():
    pod = FakePod()
    try:
        t = TcpTransport(pod.addr, timeout=2)
        types = sensor.sensor_types(t)
        assert types[0]["type"] == "sht4x" and types[0]["params"][0]["key"] == "temperature_c"
        assert pod.requests[-1] == {"cmd": "sensor_types"}
    finally:
        pod.close()


def test_gps_requests():
    import datetime as dt
    from embeddedci.benchpod import gps
    pod = FakePod()
    try:
        t = TcpTransport(pod.addr, timeout=2)
        when = dt.datetime(2026, 10, 8, 14, 34, 56, tzinfo=dt.timezone(dt.timedelta(hours=2)))
        gps.gps_start(t, tx=9, sentences=["rmc", "GGA"], utc=when, latitude_deg=50.85, satellites=7.0)
        assert pod.requests[-1] == {"cmd": "gps_start", "tx": 9, "baud": 9600, "rate_hz": 1,
                                    "sentences": "RMC,GGA", "utc": "2026-10-08T12:34:56Z",
                                    "latitude_deg": 50.85, "satellites": 7}
        gps.gps_set(t, speed_kmh=36, fix=0, utc=1791462896)
        assert pod.requests[-1] == {"cmd": "gps_set", "speed_kmh": 36.0, "fix": 0, "utc": 1791462896}
        with pytest.raises(ValueError):
            gps.gps_set(t, latitude=1.0)            # not a field
        with pytest.raises(ValueError):
            gps.gps_start(t, tx=9, sentences="RMC,ZDA")
        with pytest.raises(BenchPodError):
            gps.gps_set(t)
        assert gps.gps_status(t)["active"] is True
        gps.gps_stop(t)
        assert pod.requests[-1] == {"cmd": "gps_stop"}
    finally:
        pod.close()
