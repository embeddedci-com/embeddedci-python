"""Connection 'usb' prefers the pod's USB network adapter (firmware 3.9.0+) and falls back to the
console; an explicit device path stays the console. No hardware: the console and the TCP probe are
faked."""

from __future__ import annotations

import pytest

from embeddedci.benchpod import transport as transport_mod
from embeddedci.benchpod.connection import ConnSpec, parse_connection
from embeddedci.benchpod.errors import TransportError
from embeddedci.benchpod.transport import open_transport, usb_net_address
from embeddedci.benchpod.transport import serial as serial_mod
from embeddedci.benchpod.transport.serial import parse_text_status, parse_usb_status_line
from embeddedci.benchpod.transport.tcp import TcpTransport

USB_ON = "net on pod 10.237.8.85 host 10.237.8.86 link up rx 2946 (drop 0) tx 5759 (full 2357)"
USB_WIFI = "net off (Wi-Fi configured) pod 10.237.8.85 host 10.237.8.86 link down rx 0 (drop 0) tx 0 (full 0)"
USB_DOWN = "net on pod 10.237.8.85 host 10.237.8.86 link down rx 0 (drop 0) tx 0 (full 0)"

STATUS = (
    "  device : benchpod\r\n"
    "  ip     : 192.168.1.215\r\n"
    "  board  : stm32h563  fw v3.9.0  rev v3  nrst_pin=yes  usb_cc=yes\r\n"
    "  adc    : 16-bit x1, 4096 mV full-scale\r\n"
)


# -- parsing ------------------------------------------------------------------------------------

def test_parse_usb_line_net_on():
    s = parse_text_status("status\r\n" + STATUS + f"  usb    : {USB_ON}\r\n> ")
    assert s["usb"] == {"net": "on", "link": True, "ip": "10.237.8.85", "host_ip": "10.237.8.86"}


def test_parse_usb_line_wifi_configured():
    assert parse_usb_status_line(USB_WIFI) == {
        "net": "off", "link": False, "ip": "10.237.8.85", "host_ip": "10.237.8.86"}


def test_parse_usb_line_from_older_firmware_is_left_alone():
    assert parse_usb_status_line("cdc acm") is None
    s = parse_text_status(STATUS + "  usb    : cdc acm\r\n")
    assert s["usb"] == "cdc acm"
    assert "usb" not in parse_text_status(STATUS)


@pytest.mark.parametrize("status, want", [
    ({"usb": {"net": "on", "link": True, "ip": "10.237.8.85", "host_ip": "10.237.8.86"}}, "10.237.8.85"),
    ({"usb": {"net": "off", "link": False, "ip": "10.237.8.85"}}, None),
    ({"usb": {"net": "on", "link": False, "ip": "10.237.8.85"}}, None),
    ({"usb": {"net": "on", "link": True, "ip": "0.0.0.0"}}, None),
    ({"usb": {"net": "on", "link": True, "ip": "garbage"}}, None),
    ({"usb": "cdc acm"}, None),
    ({"device": "benchpod"}, None),
    ("not a dict", None),
])
def test_usb_net_address(status, want):
    assert usb_net_address(status) == want


# -- resolution ---------------------------------------------------------------------------------

class FakeConsole:
    """Stands in for SerialTransport: answers status() with a canned dict (or raises)."""

    instances: list = []

    def __init__(self, device="", *, timeout=30.0, status=None, error=None, pod=""):
        self.device = device or "/dev/cu.usbmodemPOD"
        self.pod = pod
        self.timeout = timeout
        self._status = status
        self._error = error
        self.closed = False
        self.status_timeouts: list = []
        FakeConsole.instances.append(self)

    def status(self):
        self.status_timeouts.append(self.timeout)
        if self._error is not None:
            raise self._error
        return self._status

    def close(self):
        self.closed = True


@pytest.fixture
def usb(monkeypatch):
    """Install a fake console answering `status` and a fake TCP probe; returns a setter."""
    FakeConsole.instances = []
    probes: list = []
    cfg = {"status": None, "error": None, "reachable": True}

    def make_console(device="", *, timeout=30.0, pod=""):
        return FakeConsole(device, timeout=timeout, status=cfg["status"], error=cfg["error"],
                           pod=pod)

    def probe(host, port, timeout=transport_mod.USB_NET_PROBE_TIMEOUT):
        probes.append((host, port, timeout))
        r = cfg["reachable"]
        return host in r if isinstance(r, (set, frozenset)) else r

    monkeypatch.setattr(serial_mod, "SerialTransport", make_console)
    monkeypatch.setattr(transport_mod, "_tcp_reachable", probe)
    cfg["probes"] = probes
    return cfg


def _status(usb_line):
    return parse_text_status(STATUS + (f"  usb    : {usb_line}\r\n" if usb_line else ""))


def test_usb_picks_the_network_when_link_up_and_port_answers(usb):
    usb["status"] = _status(USB_ON)
    t = open_transport(parse_connection("usb"), timeout=12)
    assert isinstance(t, TcpTransport)
    assert t.addr == "10.237.8.85:8080" and t.timeout == 12
    assert t.usb_console == "/dev/cu.usbmodemPOD"
    console = FakeConsole.instances[0]
    assert console.closed  # the console is released once the network is chosen
    assert console.status_timeouts == [5.0] and console.timeout == 12  # short status read, restored
    assert usb["probes"] == [("10.237.8.85", 8080, 1.5)]


def test_usb_name_selects_the_pod_and_upgrades(usb):
    usb["status"] = _status(USB_ON)
    t = open_transport(parse_connection("usb:BenchPod-BAEA06"))
    assert isinstance(t, TcpTransport) and t.addr == "10.237.8.85:8080"
    assert FakeConsole.instances[0].pod == "baea06"  # the console was auto-detected by name


def test_serial_keyword_also_upgrades(usb):
    usb["status"] = {"device": "benchpod",
                     "usb": {"net": "on", "link": True, "ip": "10.237.8.85", "host_ip": "10.237.8.86"}}
    assert isinstance(open_transport(parse_connection("serial")), TcpTransport)


@pytest.mark.parametrize("line", [USB_WIFI, USB_DOWN])
def test_usb_falls_back_to_the_pods_network_address_on_3_9(usb, line, caplog):
    """3.9.0+ with USB networking off or down: its console has no la-voltage or power any more,
    so the way in is the pod's Ethernet or Wi-Fi address from its status."""
    usb["status"] = _status(line)
    with caplog.at_level("WARNING", logger="embeddedci.benchpod"):
        t = open_transport(parse_connection("usb"))
    assert isinstance(t, TcpTransport) and t.addr == "192.168.1.215:8080"
    assert t.usb_lan_fallback and not t.usb_console
    assert FakeConsole.instances[0].closed
    assert "192.168.1.215:8080" in caplog.text and "clear-wifi" in caplog.text


@pytest.mark.parametrize("line", [USB_WIFI, USB_DOWN])
def test_usb_keeps_the_console_when_no_network_address_answers(usb, line):
    usb["status"] = _status(line)
    usb["reachable"] = False
    t = open_transport(parse_connection("usb"))
    assert isinstance(t, FakeConsole) and not t.closed
    assert [p[0] for p in usb["probes"]] == ["192.168.1.215"]


def test_usb_keeps_the_console_on_firmware_before_3_9(usb):
    """Older firmware has every command on its console: no network fallback, nothing probed."""
    usb["status"] = _status(None)
    t = open_transport(parse_connection("usb"))
    assert isinstance(t, FakeConsole) and not t.closed
    assert usb["probes"] == []


def test_usb_keeps_the_console_when_the_port_does_not_answer(usb):
    usb["status"] = _status(USB_ON)
    usb["reachable"] = False
    t = open_transport(parse_connection("usb"))
    assert isinstance(t, FakeConsole) and not t.closed
    assert [p[0] for p in usb["probes"]] == ["10.237.8.85", "192.168.1.215"]


def test_usb_network_not_answering_falls_back_to_the_lan(usb):
    usb["status"] = _status(USB_ON)
    usb["reachable"] = {"192.168.1.215"}
    t = open_transport(parse_connection("usb"))
    assert isinstance(t, TcpTransport) and t.addr == "192.168.1.215:8080"


def test_usb_keeps_the_console_when_status_fails(usb):
    usb["error"] = TransportError("timed out waiting for console prompt")
    t = open_transport(parse_connection("usb"), timeout=20)
    assert isinstance(t, FakeConsole) and not t.closed and t.timeout == 20


def test_explicit_device_path_never_upgrades(usb):
    usb["status"] = _status(USB_ON)
    t = open_transport(ConnSpec(kind="serial", device="/dev/ttyACM0"))
    assert isinstance(t, FakeConsole) and t.device == "/dev/ttyACM0"
    assert t.status_timeouts == [] and usb["probes"] == []


def test_tcp_reachable_against_a_real_listener():
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert transport_mod._tcp_reachable("127.0.0.1", port, timeout=1.0)
    finally:
        srv.close()
    assert not transport_mod._tcp_reachable("127.0.0.1", port, timeout=0.5)


def test_plain_network_transport_has_no_usb_console():
    assert TcpTransport("192.168.1.220:8080").usb_console == ""


def test_discovered_pod_prefers_the_usb_link():
    from embeddedci.benchpod.discovery import DiscoveredPod

    both = DiscoveredPod(name="BenchPod baea06", hostname="benchpod-baea06.local",
                         addresses=["192.168.1.221", "10.237.8.85"])
    assert both.addr == "10.237.8.85:8080" and both.on_usb
    lan = DiscoveredPod(name="BenchPod baea06", hostname="benchpod-baea06.local",
                        addresses=["192.168.1.221"])
    assert lan.addr == "192.168.1.221:8080" and not lan.on_usb
    none = DiscoveredPod(name="BenchPod baea06", hostname="benchpod-baea06.local")
    assert none.addr == "benchpod-baea06.local:8080"
