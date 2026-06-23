"""End-to-end: BenchPod facade -> TCP transport -> flash bridge -> fake openocd.

Wires the whole stack together with no real hardware and no real openocd. A fake
pod speaks the JSON protocol (ping/target_power/dap_start) and, after the
dap_start ack, holds the connection open as the raw CMSIS-DAP link; a fake
openocd connects to the bridge and exits with a chosen code.
"""

import json
import os
import socket
import stat
import threading
import time

import pytest

from embeddedci import benchpod as bp
from embeddedci.benchpod.client import BenchPod
from embeddedci.benchpod.errors import TargetUnreachableError
from embeddedci.benchpod.transport.tcp import TcpTransport
from test_flash_bridge import FAKE_OPENOCD


class FakePod:
    """JSON/TCP fake that also bridges raw bytes after dap_start."""

    def __init__(self):
        self.power_calls = []
        self.raw_received = bytearray()
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
            line, _, rest = buf.partition(b"\n")
            req = json.loads(line)
            cmd = req.get("cmd")
            if cmd == "ping":
                conn.sendall(b'{"status":"ok","data":"pong"}\n')
            elif cmd == "target_power":
                self.power_calls.append(req)
                conn.sendall(b'{"status":"ok","data":null}\n')
            elif cmd == "dap_start":
                conn.sendall(b'{"status":"ok","data":"dap ready"}\n')
                # Now act as the raw CMSIS-DAP link: drain bytes until closed.
                conn.settimeout(5)
                try:
                    while True:
                        data = conn.recv(4096)
                        if not data:
                            break
                        self.raw_received.extend(data)
                except OSError:
                    pass
            else:
                conn.sendall(b'{"status":"error","message":"unknown cmd"}\n')

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture
def fake_openocd(tmp_path):
    import sys

    path = tmp_path / "fake-openocd"
    path.write_text(FAKE_OPENOCD.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(path)


def test_full_flash_flow_ok(fake_openocd):
    pod = FakePod()
    try:
        device = BenchPod(transport=TcpTransport(pod.addr, timeout=3))
        assert device.ping() == "pong"
        result = device.flash(
            file="fw.elf", target="target/stm32f1x.cfg",
            swclk=bp.PIN1, swdio=bp.PIN2, nreset=bp.PIN3,
            target_power=bp.INTERNAL, openocd_bin=fake_openocd, timeout=10,
        )
        assert result.ok
        # target was powered first, and openocd's bytes reached the pod.
        assert pod.power_calls and pod.power_calls[0]["efuse"] == 1
        time.sleep(0.1)
        assert b"DAPREQ" in bytes(pod.raw_received)
    finally:
        pod.close()


def test_full_flash_flow_target_unreachable(fake_openocd):
    pod = FakePod()
    old = os.environ.get("FAKE_OPENOCD_STDERR"), os.environ.get("FAKE_OPENOCD_EXIT")
    os.environ["FAKE_OPENOCD_STDERR"] = "Error: cannot read IDR\n"
    os.environ["FAKE_OPENOCD_EXIT"] = "1"
    try:
        device = BenchPod(transport=TcpTransport(pod.addr, timeout=3))
        with pytest.raises(TargetUnreachableError):
            device.flash(
                file="fw.elf", target="target/stm32f1x.cfg",
                swclk=bp.PIN1, swdio=bp.PIN2,
                openocd_bin=fake_openocd, timeout=10,
            )
    finally:
        for k, v in zip(("FAKE_OPENOCD_STDERR", "FAKE_OPENOCD_EXIT"), old):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        pod.close()
