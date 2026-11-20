"""The client API level: every JSON request carries ``client_api`` and ``client``; a pod that
announces a higher ``api`` than :data:`CLIENT_API`, or refuses with ``BP0030``, raises
:class:`ClientTooOldError`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import BenchPod, ClientTooOldError, FirmwareTooOldError, protocol
from embeddedci.benchpod.errors import firmware_error
from embeddedci.benchpod import feature_map as _feature_map
from embeddedci.benchpod.feature_map import CLIENT_API, table_api
from embeddedci.benchpod.transport.cloud import CloudTransport
from embeddedci.benchpod.transport.tcp import TcpTransport

from test_cloud import _scripted_urlopen
from test_tcp_transport import FakePod


def test_client_api_is_the_bundled_tables():
    """A features-table sync that raises ``api`` fails here: raise CLIENT_API by hand, with the
    code that handles the new level."""
    bundled = json.loads(Path(_feature_map.__file__).with_name("feature_map.json").read_text("utf-8"))
    assert CLIENT_API == bundled["api"] == table_api() == 2
    assert bundled["min_client_api"] <= bundled["api"]


# -- every transport's request encoding ---------------------------------------------------------

def test_encode_request_carries_the_level_and_the_name():
    req = json.loads(protocol.encode_request({"cmd": "gpio", "la": 4}))
    assert req["cmd"] == "gpio" and req["la"] == 4
    assert req["client_api"] == CLIENT_API
    assert req["client"].startswith("sdk ") and len(req["client"]) <= protocol.CLIENT_NAME_MAX


def test_a_request_keeps_its_own_fields():
    req = json.loads(protocol.encode_request({"cmd": "x", "client_api": 0, "client": "mine"}))
    assert req["client_api"] == 0 and req["client"] == "mine"


def test_a_name_the_pod_would_drop_is_not_sent(monkeypatch):
    monkeypatch.setattr(protocol, "_client_name", "x" * 33)
    assert "client" not in json.loads(protocol.encode_request({"cmd": "x"}))
    monkeypatch.setattr(protocol, "_client_name", "mcp 3.0.6\n")
    assert "client" not in json.loads(protocol.encode_request({"cmd": "x"}))
    protocol.set_client_name("mcp 3.0.6")   # monkeypatch puts the name back afterwards
    assert json.loads(protocol.encode_request({"cmd": "x"}))["client"] == "mcp 3.0.6"


def test_tcp_requests_carry_it():
    seen: List[Dict[str, Any]] = []

    def handler(conn, line):
        seen.append(json.loads(line))
        conn.sendall(b'{"status":"ok","data":null}\n')

    pod = FakePod(handler)
    try:
        t = TcpTransport(pod.addr, timeout=2)
        t.command({"cmd": "gpio", "la": 4, "mode": "off"})
        t.command_sequence([{"cmd": "ping"}])
    finally:
        pod.close()
    assert len(seen) == 2
    assert all(r["client_api"] == CLIENT_API and r["client"].startswith("sdk ") for r in seen)


def test_cloud_command_channel_carries_it(monkeypatch):
    t = CloudTransport("dev-a", api_base="https://example.test", token="x")
    calls = _scripted_urlopen(monkeypatch, [None])
    t.command({"cmd": "status"})
    sent = json.loads(calls[0].data)["command"]
    assert sent["cmd"] == "status" and sent["client_api"] == CLIENT_API
    assert sent["client"].startswith("sdk ")


def test_cloud_tunnel_carries_it():
    """The byte tunnel sends the TCP transport's lines."""
    t = CloudTransport("dev-a", api_base="https://example.test", token="x")
    req = json.loads(t._encode({"cmd": "uart_proxy_start"}))
    assert req["client_api"] == CLIENT_API and req["client"].startswith("sdk ")


def test_the_usb_console_text_protocol_gets_nothing():
    from embeddedci.benchpod.transport.serial import SerialTransport

    lines: List[str] = []
    t = SerialTransport.__new__(SerialTransport)
    t.timeout = 1.0
    t._write_line = lines.append                          # type: ignore[method-assign]
    t._read_until_prompt = lambda deadline: "PING ok\n>"  # type: ignore[method-assign]
    assert t.command({"cmd": "ping"}) == "pong"
    assert lines == ["ping"]


# -- the pod's level on connect -----------------------------------------------------------------

class _Pod:
    """A pod whose status announces ``api`` (or nothing, like firmware 3.10.13)."""

    def __init__(self, status_extra: Dict[str, Any], version: str = "4.0.0") -> None:
        self.extra = status_extra
        self.version = version
        self.requests: List[dict] = []
        self.timeout = 30.0
        self.refuse_with: Dict[str, Any] = {}

    def status(self) -> Dict[str, Any]:
        return self.command({"cmd": "status"})

    def command(self, req: dict) -> Any:
        self.requests.append(req)
        if req["cmd"] == "status":
            return {"version": self.version, "board": "stm32h563", "features_rev": 10,
                    "gateware": 59, **self.extra}
        if self.refuse_with:
            raise firmware_error("", cmd=req["cmd"], code="BP0030", values=self.refuse_with)
        return {}

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass


def test_a_pod_at_this_level_connects():
    bp = BenchPod(transport=_Pod({"api": 2, "min_client_api": 2}), lease=False, api_key="", api_base="")
    assert bp.capabilities.client_api_level == 2 and bp.capabilities.api == 2


@pytest.mark.firmware_3
@pytest.mark.parametrize("extra,version", [
    ({}, "3.10.13"),                                   # no api: client API 1
    ({"api": 1, "min_client_api": 0}, "3.11.0"),       # development builds after 3.10.13
])
def test_firmware_3_is_refused_on_connect(extra, version):
    pod = _Pod(extra, version)
    with pytest.raises(FirmwareTooOldError) as e:
        BenchPod(transport=pod, lease=False, api_key="", api_base="")
    err = e.value
    assert err.api == 1 and err.firmware_version == version and err.feature == "client_api"
    msg = str(err)
    assert version in msg and "4.0.0" in msg and "benchpod flash-self" in msg
    assert "embeddedci<4" in msg
    # Nothing but the status reads reached the pod.
    assert {r["cmd"] for r in pod.requests} == {"status"}


def test_a_pod_above_this_level_refuses_the_connect():
    pod = _Pod({"api": 3, "min_client_api": 2})
    with pytest.raises(ClientTooOldError) as e:
        BenchPod(transport=pod, lease=False, api_key="", api_base="")
    err = e.value
    assert err.api == 3 and err.client_api == CLIENT_API and err.min_client_api == 2
    assert err.firmware_version == "4.0.0"
    msg = str(err)
    assert "4.0.0" in msg and "client API 3" in msg and f"client API {CLIENT_API}" in msg
    assert "pip install -U embeddedci" in msg


def test_a_pod_that_takes_no_client_this_old_refuses_the_connect():
    with pytest.raises(ClientTooOldError):
        BenchPod(transport=_Pod({"api": 3, "min_client_api": 3}), lease=False, api_key="", api_base="")


@pytest.mark.firmware_3
def test_capabilities_after_an_update_check_again():
    pod = _Pod({"api": 2})
    bp = BenchPod(transport=pod, lease=False, api_key="", api_base="")
    pod.extra = {"api": 3}                         # the pod was updated meanwhile
    with pytest.raises(ClientTooOldError):
        bp.refresh_capabilities()
    pod.extra, pod.version = {}, "3.10.13"         # rolled back to firmware 3.x
    with pytest.raises(FirmwareTooOldError):
        bp.refresh_capabilities()


def test_bp0030_raises_client_too_old():
    pod = _Pod({"api": 2, "min_client_api": 2})
    bp = BenchPod(transport=pod, lease=False, api_key="", api_base="")
    pod.refuse_with = {"client_api": 1, "min_client_api": 3, "api": 3}
    with pytest.raises(ClientTooOldError) as e:
        bp.command({"cmd": "gpio", "la": 4, "mode": "off"})
    assert e.value.code == "BP0030" and e.value.min_client_api == 3 and e.value.client_api == 1
    assert "pip install -U embeddedci" in str(e.value)


@pytest.mark.firmware_3
def test_the_console_decides_by_version():
    """The USB console announces no api: its firmware version decides."""
    from embeddedci.benchpod.transport.serial import SerialTransport

    class _Console(SerialTransport):
        def __init__(self, version: str) -> None:
            self.version, self.timeout, self.device = version, 1.0, "/dev/x"

        def command(self, req: dict) -> Any:
            return {"version": self.version, "board": "stm32h563"} if req["cmd"] == "status" else "pong"

        def close(self) -> None:
            pass

    assert BenchPod(transport=_Console("4.0.0"), lease=False, api_key="", api_base="").capabilities
    with pytest.raises(FirmwareTooOldError):
        BenchPod(transport=_Console("3.10.13"), lease=False, api_key="", api_base="").capabilities


def test_the_cloud_frame_carries_it_too():
    from embeddedci.benchpod.capabilities import Capabilities

    c = Capabilities.from_parameters({"cap.api": "2", "cap.min_client_api": "2"})
    assert c.api == 2 and c.min_client_api == 2 and c.client_api_level == 2
    assert Capabilities.from_parameters({}).client_api_level == 1
