"""bp.diag() and the short status over a direct connection (firmware 3.10.9 moved the diagnostics
out of status so its full form fits a connection's 2047-byte send buffer)."""
import json
import logging
import socket
import threading

import pytest

from embeddedci.benchpod.client import BenchPod
from embeddedci.benchpod.transport.tcp import TcpTransport

FULL = {"device": "benchpod", "version": "3.10.9", "board": "stm32h563", "analog": True,
        "board_rev": "v3r2", "adc_bits": 16, "adc_fullscale_mv": 4096,
        "adc_cal_a_uv": 65678177, "adc_cal_b_nv": -1002109, "adc_cal_unwrap": True,
        "ota_sig": True, "heap_free": 128000, "heap_min": 126000, "stack_min": 400,
        "caps": ["gpio", "uart", "la"]}
SHORT = {k: v for k, v in FULL.items() if not k.startswith("adc_cal") and k != "ota_sig"}
SHORT["short_form"] = True
DIAG = {"heap_free": 128000, "heap_min": 126000, "stack_min": 400, "stacks": {"hw": 6992, "IDLE": 400},
        "lwip_mem_max": 12288, "lwip_mem_size": 65536, "pbuf_pool_max": 1, "pbuf_pool_size": 16,
        "malloc_failures": 0, "rx_dropped": 0, "tx_dropped": 0, "board_rev_mv": 2728}


class FakePod:
    """Answers status with the next entry of `statuses` (the last one repeats) and diag with DIAG,
    or "unknown cmd" when has_diag is False."""

    def __init__(self, statuses, has_diag=True):
        self.statuses = list(statuses)
        self.has_diag = has_diag
        self.requests = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.addr = "127.0.0.1:%d" % self._sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _reply(self, req):
        cmd = req.get("cmd")
        if cmd == "status":
            st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return {"status": "ok", "data": st}
        if cmd == "diag" and self.has_diag:
            return {"status": "ok", "data": DIAG}
        if cmd == "ping":
            return {"status": "ok", "data": {}}
        return {"status": "error", "code": "BP0002", "args": {"cmd": cmd}, "message": "unknown cmd"}

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while True:
                    while b"\n" not in buf:
                        c = conn.recv(4096)
                        if not c:
                            break
                        buf += c
                    if b"\n" not in buf:
                        break
                    line, buf = buf.split(b"\n", 1)
                    req = json.loads(line)
                    self.requests.append(req)
                    conn.sendall(json.dumps(self._reply(req)).encode() + b"\n")

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass


def _bp(pod):
    return BenchPod(transport=TcpTransport(pod.addr, timeout=2), lease=False)


def test_diag_reads_the_diag_command():
    pod = FakePod([FULL])
    try:
        d = _bp(pod).diag()
        assert d["stacks"] == {"hw": 6992, "IDLE": 400} and d["lwip_mem_max"] == 12288
        assert pod.requests[-1]["cmd"] == "diag"
    finally:
        pod.close()


def test_short_status_once_is_read_again(caplog):
    """A busy send buffer can make one status the short form: the second read is whole, so the
    calibration is the pod's and nothing is logged."""
    pod = FakePod([SHORT, FULL])
    try:
        with caplog.at_level(logging.WARNING):
            caps = _bp(pod).capabilities
        assert caps.adc_affine is not None and abs(caps.adc_affine.b + 1.002109e-3) < 1e-9
        assert "short status" not in caplog.text
    finally:
        pod.close()


def test_short_status_over_lan_warns(caplog):
    pod = FakePod([SHORT])
    try:
        with caplog.at_level(logging.WARNING):
            _bp(pod).capabilities
        assert "short status over the network" in caplog.text
    finally:
        pod.close()
