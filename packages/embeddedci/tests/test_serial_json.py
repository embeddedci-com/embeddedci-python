"""SerialTransport JSON-mode plumbing, against a fake console port (no hardware).

The fake emulates the firmware's serial console: text commands echo + prompt;
`json` enters JSON mode where lines are dispatched as JSON and replies (plus a
`[debug]` line, to prove it's skipped) come back; `{"cmd":"json_exit"}` leaves.
"""

import json

import pytest

from embeddedci.benchpod.errors import FirmwareError
from embeddedci.benchpod.transport.serial import SerialTransport


class FakeConsolePort:
    def __init__(self):
        self._in = bytearray()   # bytes the transport will read
        self._line = bytearray()  # accumulates bytes written by the transport
        self.json_mode = False
        self.timeout = 0.25

    # pyserial-like surface -------------------------------------------------
    def write(self, data: bytes):
        for b in bytes(data):
            if b in (0x0A, 0x0D):  # newline -> process a line
                self._process(bytes(self._line))
                self._line.clear()
            elif b == 0x08:        # backspace (clear-line prefix) -> ignore
                pass
            else:
                self._line.append(b)
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self._in.clear()

    @property
    def in_waiting(self):
        return len(self._in)

    def read(self, n=1):
        out = bytes(self._in[:n])
        del self._in[:n]
        return out

    def close(self):
        pass

    # firmware emulation ----------------------------------------------------
    def _emit(self, s: str):
        self._in.extend(s.encode())

    def _process(self, line: bytes):
        s = line.decode("utf-8", "replace").strip()
        if not self.json_mode:
            self._emit(s + "\r\n")  # echo the text command
            if s == "json":
                self.json_mode = True
                self._emit('{"status":"ok","data":"json mode"}\n')
            elif s == "status":
                self._emit("firmware : 0.2.0\r\n> ")
            elif s.startswith("target-power"):
                self._emit("eFuse1 ON\r\n> ")
            else:
                self._emit("> ")
            return
        # JSON mode: dispatch the object.
        if not s.startswith("{"):
            self._emit('{"status":"error","message":"json mode"}\n')
            return
        req = json.loads(s)
        cmd = req.get("cmd")
        self._emit(f"[cmd] <- {cmd}\n")  # interleaved debug — must be skipped
        if cmd == "json_exit":
            self.json_mode = False
            self._emit('{"status":"ok","data":"json mode exited"}\n> ')
        elif cmd == "ping":
            self._emit('{"status":"ok","data":"pong"}\n')
        elif cmd == "pullup":
            la = req["la"]
            on = 1 if req.get("state", "on") in ("on", 1, "1") else 0
            self._emit('{"status":"ok","data":{"la":%d,"pullup":%d,"ohms":"4.7k"}}\n'
                       % (la, on))
        elif cmd == "sensor_start":
            self._emit('{"status":"ok","data":{"type":"bmp280","addr":118}}\n')
        elif cmd == "sensor_regs":
            self._emit('{"status":"ok","data":[88,0],"more":true}\n')
            self._emit('{"status":"chunk","data":[1],"more":false}\n')
        else:
            self._emit('{"status":"error","message":"unknown cmd"}\n')


def _transport():
    return SerialTransport(port=FakeConsolePort(), timeout=2)


def test_command_enters_json_and_skips_debug():
    t = _transport()
    assert t.command({"cmd": "ping"}) == "pong"
    assert t._json_mode is True


def test_samples_chunked_over_json():
    t = _transport()
    assert t.samples({"cmd": "sensor_regs", "start": "0xD0", "len": 3}) == [88, 0, 1]


def test_sensor_start_request_roundtrip():
    t = _transport()
    data = t.command({"cmd": "sensor_start", "type": "bmp280", "sda": 7, "scl": 8})
    assert data["addr"] == 118


def test_text_op_auto_exits_json_mode():
    t = _transport()
    t.command({"cmd": "ping"})            # enter json mode
    assert t._json_mode is True
    t.target_power(1, True)               # text op must drop back to console mode
    assert t._json_mode is False


def test_error_reply_raises():
    t = _transport()
    with pytest.raises(FirmwareError):
        t.command({"cmd": "bogus"})


def test_pullup_via_client_over_serial():
    from embeddedci.benchpod.client import BenchPod
    from embeddedci.benchpod.errors import BenchPodError

    bp = BenchPod(transport=_transport())
    d = bp.pullup(1, on=True)
    assert d == {"la": 1, "pullup": 1, "ohms": "4.7k"}
    bp.enable_pullup(1, 2)        # no error
    with pytest.raises(BenchPodError):
        bp.pullup(9)             # LA9 has no pull-up
