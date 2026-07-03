"""CAN client + CanBus session wiring (no hardware).

A stateful FakePod emulates the firmware's CAN command surface, including
loopback echo (a frame written in ``internal``/``external`` mode reappears on
``can_read``), so the round-trip logic in :class:`CanBus` is exercised end to
end over the real TCP transport.
"""

import json
import socket
import threading

import pytest

from embeddedci.benchpod.can import CanBus, CanFrame
from embeddedci.benchpod.client import BenchPod
from embeddedci.benchpod.errors import CanTimeout, FirmwareError
from embeddedci.benchpod.transport.tcp import TcpTransport


class FakeCanPod:
    """Minimal CAN pod: one connection per command (matches the firmware)."""

    def __init__(self):
        self.requests = []
        self._enabled = False
        self._mode = "normal"
        self._bitrate = 0
        self._term = False
        self._rx = []          # queued frames for can_read (loopback echo)
        self._overflow = 0
        self._rules = []       # autonomous-responder rules
        self._resp_hits = 0
        self.ts_schedule = None   # list of ts values to hand out, else a counter
        self._ts_i = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.addr = "127.0.0.1:%d" % self._sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _next_ts(self):
        if self.ts_schedule is not None and self._ts_i < len(self.ts_schedule):
            ts = self.ts_schedule[self._ts_i]
        else:
            ts = 1000 + self._ts_i
        self._ts_i += 1
        return ts

    def _reply(self, req):
        cmd = req.get("cmd")
        if cmd == "can_config":
            self._enabled = True
            self._mode = req.get("mode", "normal")
            self._bitrate = int(req.get("bitrate", 0))
            self._term = bool(req.get("term", False))
            self._rx = []
            return {"status": "ok", "data": {
                "bitrate": self._bitrate, "mode": self._mode, "term": self._term}}
        if cmd == "can_write":
            if not self._enabled:
                return {"status": "error", "message": "can not enabled"}
            frame = {"id": int(req["id"]), "ext": bool(req.get("ext", False)),
                     "rtr": bool(req.get("rtr", False)),
                     "data": list(req.get("data", [])), "ts": self._next_ts()}
            frame["dlc"] = len(frame["data"])
            if self._mode in ("internal", "external"):
                self._rx.append(frame)          # loopback: echo back to RX
                # autonomous responder fires on the echoed (received) frame
                for r in self._rules:
                    if r["match_id"] == frame["id"] and r["ext"] == frame["ext"]:
                        self._rx.append({
                            "id": r["reply_id"], "ext": r["reply_ext"], "rtr": False,
                            "data": list(r["reply_data"]),
                            "dlc": len(r["reply_data"]), "ts": self._next_ts()})
                        self._resp_hits += 1
                        break
            return {"status": "ok", "data": {
                "id": frame["id"], "ext": frame["ext"], "dlc": frame["dlc"]}}
        if cmd == "can_respond":
            if req.get("clear"):
                self._rules = []
                self._resp_hits = 0
                return {"status": "ok", "data": {"rules": 0}}
            self._rules.append({
                "match_id": int(req["match_id"]), "ext": bool(req.get("ext", False)),
                "reply_id": int(req["reply_id"]), "reply_ext": bool(req.get("reply_ext", False)),
                "reply_data": list(req.get("reply_data", []))})
            return {"status": "ok", "data": {"rule": len(self._rules) - 1, "rules": len(self._rules)}}
        if cmd == "can_read":
            want = int(req.get("max", 8))
            take, self._rx = self._rx[:want], self._rx[want:]
            return {"status": "ok", "data": {
                "frames": take, "overflow": self._overflow}}
        if cmd == "can_status":
            return {"status": "ok", "data": {
                "enabled": self._enabled, "mode": self._mode,
                "bitrate": self._bitrate, "term": self._term,
                "tec": 0, "rec": 0, "bus_off": False, "error_passive": False,
                "rx_pending": len(self._rx), "rx_overflow": self._overflow,
                "responder_rules": len(self._rules), "responder_hits": self._resp_hits}}
        if cmd == "can_term":
            self._term = bool(req.get("on", False))
            return {"status": "ok", "data": {"term": self._term}}
        if cmd == "can_disable":
            self._enabled = False
            return {"status": "ok", "data": {"enabled": False}}
        return {"status": "error", "message": "unknown cmd"}

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
                if not buf:
                    continue
                req = json.loads(buf.split(b"\n", 1)[0])
                self.requests.append(req)
                conn.sendall(json.dumps(self._reply(req)).encode() + b"\n")

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture()
def pod():
    p = FakeCanPod()
    yield p
    p.close()


@pytest.fixture()
def bp(pod):
    return BenchPod(transport=TcpTransport(pod.addr, timeout=2), lease=False)


# -- CanFrame ---------------------------------------------------------------

def test_canframe_from_dict_roundtrip():
    f = CanFrame.from_dict({"id": 0x123, "data": [1, 2, 3], "ext": True, "ts": 7})
    assert f.id == 0x123 and f.data == b"\x01\x02\x03" and f.dlc == 3 and f.ext
    assert "0x123" in str(f)


# -- command wiring ---------------------------------------------------------

def test_can_config_request_shape(bp, pod):
    bp.can_config(bitrate=250_000, mode="external", term=True)
    assert pod.requests[-1] == {
        "cmd": "can_config", "bitrate": 250_000, "mode": "external",
        "term": True, "fd": False}


def test_can_write_encodes_bytes(bp, pod):
    bp.can_config(mode="internal")
    bp.can_write(0x321, b"\xaa\xbb", ext=True)
    assert pod.requests[-1] == {
        "cmd": "can_write", "id": 0x321, "ext": True, "rtr": False,
        "data": [0xAA, 0xBB]}


def test_can_write_without_config_is_firmware_error(bp):
    with pytest.raises(FirmwareError):
        bp.can_write(0x1, [1])


def test_can_term_toggle(bp, pod):
    assert bp.can_term(True)["term"] is True
    assert bp.can_status()["term"] is True
    assert bp.can_term(False)["term"] is False


# -- CanBus session (loopback round trip) -----------------------------------

def test_open_can_loopback_roundtrip(bp):
    with bp.open_can(bitrate=500_000, mode="internal") as can:
        can.write(0x123, [0xDE, 0xAD])
        frame = can.expect(can_id=0x123, timeout=1.0)
        assert frame.data == b"\xde\xad"
        assert frame.id == 0x123


def test_open_can_expect_predicate(bp):
    with bp.open_can(mode="internal") as can:
        can.write(0x100, [1])
        can.write(0x200, [2, 2])
        f = can.expect(lambda fr: fr.dlc == 2, timeout=1.0)
        assert f.id == 0x200


def test_open_can_expect_times_out(bp):
    with bp.open_can(mode="internal") as can:
        with pytest.raises(CanTimeout):
            can.expect(can_id=0x7FF, timeout=0.1)


def test_open_can_disables_on_exit(bp, pod):
    with bp.open_can(mode="internal"):
        pass
    assert pod.requests[-1]["cmd"] == "can_disable"
    assert pod._enabled is False


def test_canbus_rejects_bad_mode(bp):
    with pytest.raises(ValueError):
        bp.open_can(mode="bogus")


# -- read_until keeps un-consumed frames (regression) -----------------------

def test_read_until_does_not_drop_other_frames(bp):
    with bp.open_can(mode="internal") as can:
        can.write(0x111, [1])
        can.write(0x222, [2])
        # Ask for the SECOND id first; the first must not be lost.
        assert can.expect(can_id=0x222, timeout=1.0).id == 0x222
        assert can.expect(can_id=0x111, timeout=1.0).id == 0x111


# -- autonomous responder (ECU simulation) ----------------------------------

def test_responder_roundtrip(bp, pod):
    with bp.open_can(mode="internal") as can:
        can.add_responder(0x7DF, 0x7E8, [0x50, 0x03])
        can.write(0x7DF, [0x02, 0x10, 0x03])          # request
        reply = can.expect(can_id=0x7E8, timeout=1.0)  # firmware auto-reply
        assert reply.data == b"\x50\x03"
        assert pod._resp_hits == 1
        assert can.status()["responder_rules"] == 1


def test_simulate_ecu_table(bp):
    with bp.open_can(mode="internal") as can:
        can.simulate_ecu({0x100: (0x200, [0xAB]), 0x300: [0xCD]})  # 0x300 -> 0x301
        can.write(0x100, [0])
        can.write(0x300, [0])
        assert can.expect(can_id=0x200, timeout=1.0).data == b"\xab"
        assert can.expect(can_id=0x301, timeout=1.0).data == b"\xcd"


def test_clear_responders(bp, pod):
    with bp.open_can(mode="internal") as can:
        can.add_responder(0x1, 0x2, [9])
        can.clear_responders()
        assert pod._rules == []
        can.write(0x1, [0])
        with pytest.raises(CanTimeout):
            can.expect(can_id=0x2, timeout=0.1)


# -- timing helpers ---------------------------------------------------------

def test_collect_returns_matching_frames(bp):
    with bp.open_can(mode="internal") as can:
        can.write(0x10, [1])
        can.write(0x20, [2])
        can.write(0x10, [3])
        got = can.collect(0.05, match=0x10)
        assert [f.id for f in got] == [0x10, 0x10]


def test_assert_periodic_passes_for_even_spacing(bp, pod):
    pod.ts_schedule = [1000, 1100, 1200, 1300]   # 100 ms apart
    with bp.open_can(mode="internal") as can:
        for _ in range(4):
            can.write(0x100, [0])
        frames = can.assert_periodic(0x100, period=0.1, tol=0.2,
                                     min_count=4, duration=0.05)
        assert len(frames) == 4


def test_assert_periodic_flags_a_gap(bp, pod):
    pod.ts_schedule = [1000, 1100, 1400, 1500]   # third gap is 300 ms
    with bp.open_can(mode="internal") as can:
        for _ in range(4):
            can.write(0x100, [0])
        with pytest.raises(AssertionError):
            can.assert_periodic(0x100, period=0.1, tol=0.2,
                                min_count=4, duration=0.05)


def test_assert_periodic_flags_too_few(bp, pod):
    with bp.open_can(mode="internal") as can:
        can.write(0x100, [0])
        with pytest.raises(AssertionError):
            can.assert_periodic(0x100, period=0.1, min_count=3, duration=0.05)
