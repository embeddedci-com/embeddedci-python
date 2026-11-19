"""The pod's own lease on the LAN (feature ``lease``), against an in-process fake pod that keeps
one lease and gates commands per connection the way the firmware does."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Any, Dict, List, Optional

import pytest

from embeddedci.benchpod import BenchPod, LocalLease, PodLeasedError, PodLeaseLostError
from embeddedci.benchpod import pod_lease
from embeddedci.benchpod.lease import DeviceLease
from embeddedci.benchpod.runs import RunRecording, set_active_run
from embeddedci.benchpod.transport.tcp import TcpTransport

LIGHT = {"status", "ping", "la_voltage"}


class LeasePod:
    """A JSON/TCP pod with one RAM lease. Every connection reads lines until the client closes."""

    def __init__(self, features_rev: int = 4, knows_lease: bool = True) -> None:
        self.features_rev = features_rev
        self.knows_lease = knows_lease
        self.lease: Optional[Dict[str, Any]] = None   # id, holder, purpose, deadline
        self.lost_by = ""                            # set: owners' next command gets BP0026
        self.lost_conns: set = set()
        self.owners: set = set()
        self.log: List[Dict[str, Any]] = []          # every request, with its connection number
        self._lock = threading.Lock()
        self._n = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.addr = "127.0.0.1:%d" % self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    # -- test helpers --------------------------------------------------------------------------

    def hold(self, lease_id: str, holder: str, purpose: str, ttl: float = 100.0) -> None:
        with self._lock:
            self.lease = {"id": lease_id, "holder": holder, "purpose": purpose,
                          "deadline": time.monotonic() + ttl}

    def stop_lease(self, by: str) -> None:
        with self._lock:
            self.lost_conns = set(self.owners)
            self.lost_by = by
            self.lease = None
            self.owners.clear()

    def cmds(self) -> List[str]:
        return [r["cmd"] for r in self.log]

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    # -- server --------------------------------------------------------------------------------

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with self._lock:
                self._n += 1
                n = self._n
            threading.Thread(target=self._conn, args=(conn, n), daemon=True).start()

    def _conn(self, conn: socket.socket, n: int) -> None:
        buf = b""
        with conn:
            while True:
                while b"\n" not in buf:
                    try:
                        chunk = conn.recv(4096)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        with self._lock:
                            self.owners.discard(n)
                        return
                    buf += chunk
                line, _, buf = buf.partition(b"\n")
                req = json.loads(line)
                with self._lock:
                    self.log.append({**req, "conn": n})
                    reply = self._handle(n, req)
                conn.sendall((json.dumps(reply) + "\n").encode())

    def _live(self) -> bool:
        if self.lease and time.monotonic() > self.lease["deadline"]:
            self.lease = None
            self.owners.clear()
        return self.lease is not None

    def _view(self, n: int) -> Dict[str, Any]:
        lease = self.lease or {}
        return {"id": lease.get("id"), "holder": lease.get("holder"), "purpose": lease.get("purpose"),
                "left_s": int(lease.get("deadline", 0) - time.monotonic() + 0.999), "source": "local"}

    def _busy(self) -> Dict[str, Any]:
        v = self._view(0)
        return {"status": "error", "code": "BP0025",
                "args": {"holder": v["holder"], "purpose": v["purpose"], "left_s": v["left_s"]},
                "message": f"busy: {v['holder']} holds this pod ({v['purpose']}, {v['left_s']} s left)"}

    def _handle(self, n: int, req: Dict[str, Any]) -> Dict[str, Any]:
        cmd = req.get("cmd")
        if n in self.lost_conns:
            self.lost_conns.discard(n)
            return {"status": "error", "code": "BP0026", "args": {"by": self.lost_by},
                    "message": f"lease lost: stopped by {self.lost_by}"}
        live = self._live()
        if cmd == "status":
            data: Dict[str, Any] = {"version": "3.10.13", "board": "stm32h563", "gateware": 58,
                                    "features_rev": self.features_rev}
            if req.get("caps") is False and self.knows_lease:
                data["local_lease"] = ({"held": True, "holder": self.lease["holder"],
                                        "purpose": self.lease["purpose"],
                                        "left_s": self._view(n)["left_s"], "mine": n in self.owners}
                                       if live else {"held": False})
            return {"status": "ok", "data": data}
        if cmd and cmd.startswith("lease_") and not self.knows_lease:
            return {"status": "error", "code": "BP0002", "args": {"cmd": cmd}, "message": "unknown cmd"}
        if cmd == "lease_acquire":
            if live and self.lease["id"] != req["id"]:
                return self._busy()
            if not live:
                self.lease = {"id": req["id"], "holder": req.get("holder", "unnamed"),
                              "purpose": req.get("purpose", ""), "deadline": 0}
                self.owners.clear()
            self.lease["deadline"] = time.monotonic() + req.get("ttl_s", 120)
            self.owners.add(n)
            return {"status": "ok", "data": {"lease": self._view(n)}}
        if cmd == "lease_renew":
            if not live:
                return {"status": "error", "code": "BP0027", "args": {"id": req["id"]},
                        "message": "lease: no lease with this id is held"}
            if self.lease["id"] != req["id"]:
                return self._busy()
            self.lease["deadline"] = time.monotonic() + req.get("ttl_s", 120)
            return {"status": "ok", "data": {"lease": self._view(n)}}
        if cmd == "lease_release":
            if live and self.lease["id"] != req["id"]:
                return self._busy()
            self.lease = None
            self.owners.clear()
            return {"status": "ok", "data": {"released": live}}
        if live and cmd not in LIGHT and n not in self.owners:
            return self._busy()
        return {"status": "ok", "data": {}}


@pytest.fixture
def pod():
    p = LeasePod()
    yield p
    p.close()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("BENCHPOD_LEASE", raising=False)
    monkeypatch.setattr(pod_lease, "POLL_S", 0.05)
    yield
    pod_lease._registry.clear()
    set_active_run(None)


def _open(p: LeasePod, **kw: Any) -> BenchPod:
    return BenchPod(transport=TcpTransport(p.addr, timeout=2, tls="off"), api_key="", api_base="", **kw)


def test_takes_joins_per_connection_and_releases(pod):
    bp = _open(pod, lease_purpose="process-node-hil")
    acquire = [r for r in pod.log if r["cmd"] == "lease_acquire"][0]
    assert acquire["id"].startswith("L-") and len(acquire["id"]) == 38
    assert acquire["holder"].startswith("SDK: ") and len(acquire["holder"]) <= 40
    assert acquire["purpose"] == "process-node-hil" and acquire["ttl_s"] == 120
    assert bp.leased
    before = len(pod.log)
    bp.command({"cmd": "gpio", "la": 3})
    sent = pod.log[before:]
    # one connection: lease_acquire first (an owner), then the command
    assert [r["cmd"] for r in sent] == ["lease_acquire", "gpio"] and sent[0]["conn"] == sent[1]["conn"]
    assert sent[0]["id"] == acquire["id"]
    state = bp.local_lease()
    assert state == LocalLease(held=True, holder=acquire["holder"], purpose="process-node-hil",
                               left_s=state.left_s, mine=True, reported=True)
    bp.close()
    assert pod.cmds()[-1] == "lease_release" and pod.log[-1]["id"] == acquire["id"]
    assert pod.lease is None and not bp.leased


def test_busy_waits_then_raises_with_the_holder(pod):
    pod.hold("L-other", "benchpod ci: ward", "process-node-hil")
    with pytest.raises(PodLeasedError) as info:
        _open(pod, lease_wait=0.2)
    exc = info.value
    assert exc.holder == "benchpod ci: ward" and exc.purpose == "process-node-hil"
    assert isinstance(exc.left_s, int) and exc.left_s > 0
    assert "benchpod ci: ward" in str(exc) and "waited" in str(exc)
    assert pod.cmds().count("lease_acquire") >= 2              # polled


def test_busy_waits_until_it_frees(pod):
    pod.hold("L-other", "someone", "x")
    threading.Timer(0.2, lambda: pod.hold("L-other", "someone", "x", ttl=-1)).start()
    bp = _open(pod, lease_wait=5)
    assert bp.leased and pod.lease["id"] != "L-other"
    bp.close()


def test_heartbeat_renews_and_stops_on_close(pod, monkeypatch):
    monkeypatch.setattr(pod_lease, "HEARTBEAT_S", 0.05)
    bp = _open(pod)
    time.sleep(0.3)
    assert pod.cmds().count("lease_renew") >= 2
    bp.close()
    n = pod.cmds().count("lease_renew")
    time.sleep(0.2)
    assert pod.cmds().count("lease_renew") == n


def test_heartbeat_takes_it_again_when_it_ran_out(pod, monkeypatch):
    monkeypatch.setattr(pod_lease, "HEARTBEAT_S", 0.05)
    bp = _open(pod)
    lease_id = pod.lease["id"]
    pod.lease = None                                           # the pod restarted
    time.sleep(0.3)
    assert pod.lease is not None and pod.lease["id"] == lease_id
    bp.close()


def test_runner_lease_is_joined_never_renewed_or_released(pod, monkeypatch):
    monkeypatch.setattr(pod_lease, "HEARTBEAT_S", 0.05)
    monkeypatch.setenv("BENCHPOD_LEASE", "L-runner")
    pod.hold("L-runner", "benchpod ci: ward", "hil")
    bp = _open(pod)
    assert bp.leased
    bp.command({"cmd": "gpio"})                                # an owner through the join
    time.sleep(0.2)
    bp.close()
    cmds = pod.cmds()
    assert "lease_renew" not in cmds and "lease_release" not in cmds
    assert all(r["id"] == "L-runner" for r in pod.log if r["cmd"] == "lease_acquire")
    assert pod.lease is not None and pod.lease["holder"] == "benchpod ci: ward"


def test_two_sessions_of_one_process_share_the_lease(pod):
    a = _open(pod)
    b = _open(pod)                                             # would wait forever otherwise
    ids = {r["id"] for r in pod.log if r["cmd"] == "lease_acquire"}
    assert len(ids) == 1
    a.close()
    assert "lease_release" not in pod.cmds()
    b.command({"cmd": "gpio"})
    b.close()
    assert pod.cmds()[-1] == "lease_release"


def test_lease_stopped_from_the_web_app(pod, tmp_path):
    bp = _open(pod)
    pod.stop_lease("ward")
    # the pod tells an owner once; this client's connections are all new, so mimic the firmware:
    # the next connection's join is answered with BP0026
    pod.lost_conns = {pod._n + 1}
    run = RunRecording(tmp_path / "run")
    run.attach(bp.transport)
    run.begin_case("c")
    with pytest.raises(PodLeaseLostError) as info:
        bp.command({"cmd": "gpio"})
    assert info.value.by == "ward" and info.value.code == "BP0026"
    assert run.health["lease_lost"] is True and run.health["valid"] is False
    before = len(pod.log)
    bp.command({"cmd": "gpio"})                                # runs without a lease now
    assert [r["cmd"] for r in pod.log[before:]] == ["gpio"]
    assert not bp.leased
    bp.close()
    assert "lease_release" not in pod.cmds()
    run.finish()


def test_no_lease_feature_takes_none():
    p = LeasePod(features_rev=3)
    try:
        bp = _open(p)
        assert not bp.leased and "lease_acquire" not in p.cmds()
        bp.close()
        assert "lease_release" not in p.cmds()
    finally:
        p.close()


def test_no_lease_feature_warns_when_a_runner_lease_was_expected(monkeypatch, caplog):
    monkeypatch.setenv("BENCHPOD_LEASE", "L-runner")
    p = LeasePod(features_rev=3)
    try:
        with caplog.at_level(logging.WARNING, logger="embeddedci.benchpod"):
            bp = _open(p)
        assert any("BENCHPOD_LEASE" in r.message for r in caplog.records)
        assert "lease_acquire" not in p.cmds()
        bp.close()
    finally:
        p.close()


def test_pod_that_does_not_know_the_command_takes_none():
    p = LeasePod(knows_lease=False)
    try:
        bp = _open(p)
        assert not bp.leased
        bp.command({"cmd": "gpio"})
        assert p.cmds().count("lease_acquire") == 1             # the one try, nothing joined
        bp.close()
        assert bp.local_lease() == LocalLease()
    finally:
        p.close()


def test_lease_false_skips_it(pod):
    bp = _open(pod, lease=False)
    assert "lease_acquire" not in pod.cmds() and not bp.leased
    bp.close()


def test_purpose_comes_from_the_recorded_run(pod, tmp_path):
    run = RunRecording(tmp_path / "run")
    run.suite = "process-node-hil"
    set_active_run(run)
    bp = _open(pod)
    assert [r for r in pod.log if r["cmd"] == "lease_acquire"][0]["purpose"] == "process-node-hil"
    bp.close()
    run.finish()


def test_local_lease_from_status():
    assert LocalLease.from_status({}) == LocalLease()
    got = LocalLease.from_status({"local_lease": {"held": True, "holder": "h", "purpose": "p",
                                                  "left_s": 30, "mine": False}})
    assert got == LocalLease(held=True, holder="h", purpose="p", left_s=30, mine=False, reported=True)
    assert LocalLease.from_status({"local_lease": {"held": False}}).reported


def test_cloud_lease_reuses_the_runner_id(monkeypatch):
    posts: List[Any] = []

    def fake_post(self, path, *, renewed=False):
        posts.append((path, self.lease_id))
        return None, {}

    monkeypatch.setattr(DeviceLease, "_post", fake_post)
    lease = DeviceLease(api_base="https://x", token_provider=lambda: "t", device_name="pod",
                        lease_id="L-runner", joined=True)
    lease.acquire(wait_timeout=0)
    assert lease.lease_id == "L-runner" and lease.held and lease._thread is None
    lease.release()
    assert posts == [("lease", "L-runner")] and not lease.held
    own = DeviceLease(api_base="https://x", token_provider=lambda: "t", device_name="pod")
    assert own.lease_id.startswith("lease-")
