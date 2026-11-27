"""``BenchPod.reboot`` (firmware command ``reboot``): what the SDK sends, that a reply or a dropped
connection right after the send both mean success, the wait for the pod to answer again, the
client-side mode check and the ``BP0093`` refusal of a hard reboot on a pod that cannot power-cycle."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from embeddedci.benchpod import REBOOT_MODES, BenchPod
from embeddedci.benchpod import client as client_mod
from embeddedci.benchpod.errors import (
    ConnectionClosedError,
    FirmwareError,
    PermissionDeniedError,
    TransportError,
    TransportTimeout,
    firmware_error,
)


class Pod:
    """A fake transport. ``after_send`` is what the reboot command does: ``reply`` answers it,
    ``drop`` closes the connection, ``timeout`` never answers, ``refuse`` is BP0093.
    After the send the pod stays down for ``down_pings`` pings, then answers again."""

    def __init__(self, after_send: str = "reply", down_pings: int = 0) -> None:
        self.after_send = after_send
        self.down_pings = down_pings
        self.sent: List[Dict[str, Any]] = []
        self.down = 0
        self.pings = 0
        self.uptime = 100

    def status(self) -> Dict[str, Any]:
        return {"board": "stm32h563", "adc_bits": 16, "version": "4.0.8", "la_voltage": 3.3,
                "features_rev": 22, "gateware": 68, "fpga_image": "deep", "hw": ["nrst_pin"],
                "uptime_s": self.uptime}

    def ping(self) -> Any:
        self.pings += 1
        if self.down > 0:
            self.down -= 1
            raise ConnectionClosedError("connection refused")
        return "pong"

    def close(self) -> None:
        pass

    def command(self, req: dict) -> Any:
        if req.get("cmd") == "status":
            return self.status()
        if req["cmd"] != "reboot":
            return {}
        self.sent.append(req)
        if self.after_send == "refuse":
            raise firmware_error("", cmd="reboot", code="BP0093")
        self.down = self.down_pings
        self.uptime = 1
        if self.after_send == "drop":
            raise ConnectionClosedError("reboot: the connection was lost")
        if self.after_send == "timeout":
            raise TransportTimeout("reboot: the pod did not answer within 30 s")
        return {"reboot": req.get("mode")}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Polling waits on a clock; the tests run it without waiting."""
    now = [1000.0]
    monkeypatch.setattr(client_mod.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    return now


def _pod(**kw):
    pod = Pod(**kw)
    return BenchPod(transport=pod, lease=False, wiring={"version": 2}), pod


def test_modes() -> None:
    assert REBOOT_MODES == ("soft", "hard")


def test_default_is_soft_and_returns_the_new_status() -> None:
    bp, pod = _pod()
    status = bp.reboot()
    assert pod.sent == [{"cmd": "reboot", "mode": "soft"}]
    assert status["uptime_s"] == 1


def test_success_via_the_reply_without_waiting() -> None:
    bp, pod = _pod()
    assert bp.reboot("soft", wait=False) == {"reboot": "soft"}
    assert pod.sent == [{"cmd": "reboot", "mode": "soft"}]


@pytest.mark.parametrize("how", ["drop", "timeout"])
def test_a_dropped_connection_after_the_send_is_success(how: str) -> None:
    bp, pod = _pod(after_send=how)
    assert bp.reboot(wait=False) == {"reboot": "soft"}
    assert len(pod.sent) == 1


def test_waits_until_the_pod_answers_again() -> None:
    bp, pod = _pod(after_send="drop", down_pings=4)
    status = bp.reboot(timeout=60)
    assert pod.down == 0
    assert pod.pings >= 1 + 5          # the check before the send, 4 refusals, then the answer
    assert status["uptime_s"] == 1


def test_clears_the_cached_capabilities() -> None:
    bp, pod = _pod()
    assert bp.capabilities.firmware_version == "4.0.8"
    pod.status = lambda: {**Pod.status(pod), "version": "4.0.9"}  # type: ignore[method-assign]
    bp.reboot()
    assert bp.capabilities.firmware_version == "4.0.9"


def test_a_pod_that_does_not_come_back_times_out() -> None:
    bp, _ = _pod(after_send="drop", down_pings=10_000)
    with pytest.raises(TransportTimeout, match="did not answer again within 5 s"):
        bp.reboot(timeout=5)


def test_takes_the_pod_lease_again_after_the_restart() -> None:
    bp, _ = _pod()
    renewed: List[bool] = []
    bp._pod_lease = SimpleNamespace(active=True, _renew=lambda: renewed.append(True))  # type: ignore[assignment]
    bp.reboot()
    assert renewed == [True]


@pytest.mark.parametrize("mode", ["", "fast", "SOFT ", None, 1])
def test_bad_mode_is_a_value_error_and_sends_nothing(mode: Any) -> None:
    bp, pod = _pod()
    if mode == "SOFT ":
        bp.reboot(mode)     # case and spaces are forgiven, as for log levels
        assert pod.sent[0]["mode"] == "soft"
        return
    with pytest.raises(ValueError, match="mode must be one of soft, hard"):
        bp.reboot(mode)
    assert pod.sent == []


def test_hard_on_a_pod_without_the_circuit_is_bp0093() -> None:
    bp, pod = _pod(after_send="refuse")
    with pytest.raises(FirmwareError) as err:
        bp.reboot("hard")
    assert err.value.code == "BP0093"
    assert pod.sent == [{"cmd": "reboot", "mode": "hard"}]


def test_an_unreachable_pod_is_an_error_not_a_reboot() -> None:
    bp, pod = _pod()
    pod.down = 1
    with pytest.raises(ConnectionClosedError):
        bp.reboot()
    assert pod.sent == []


def test_other_firmware_refusals_pass_through() -> None:
    bp, pod = _pod()

    def refuse(req: dict) -> Any:
        raise FirmwareError("unknown cmd", cmd="reboot")

    pod.command = refuse  # type: ignore[method-assign]
    with pytest.raises(FirmwareError, match="unknown cmd"):
        bp.reboot()


def test_a_plain_transport_error_on_the_send_is_not_hidden() -> None:
    bp, pod = _pod()

    def broken(req: dict) -> Any:
        raise TransportError("reboot: no route to host")

    pod.command = broken  # type: ignore[method-assign]
    with pytest.raises(TransportError):
        bp.reboot()


def test_timeout_must_be_positive_when_waiting() -> None:
    bp, pod = _pod()
    with pytest.raises(ValueError, match="timeout"):
        bp.reboot(timeout=0)
    assert pod.sent == []
    bp.reboot(timeout=0, wait=False)


def test_cloud_transport_losing_the_pod_after_the_send_is_success(monkeypatch) -> None:
    from embeddedci.benchpod.transport.cloud import CloudTransport

    class CloudPod(Pod):
        pass

    pod = CloudPod()
    pod.__class__ = type("CloudPod", (Pod, CloudTransport), {})   # isinstance(CloudTransport)
    orig = Pod.command

    def command(req: dict) -> Any:
        if req["cmd"] == "reboot":
            pod.sent.append(req)
            raise TransportError("cloud command 'reboot' failed (HTTP 504): pod went away")
        return orig(pod, req)

    pod.command = command  # type: ignore[method-assign]
    bp = BenchPod(transport=pod, lease=False, wiring={"version": 2})
    assert bp.reboot(wait=False) == {"reboot": "soft"}
    pod.sent.clear()

    def denied(req: dict) -> Any:
        if req["cmd"] == "reboot":
            raise PermissionDeniedError("not allowed", status=403)
        return orig(pod, req)

    pod.command = denied  # type: ignore[method-assign]
    with pytest.raises(PermissionDeniedError):
        bp.reboot()
