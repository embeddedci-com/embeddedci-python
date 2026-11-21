"""TcpTransport resolves a host name once (IPv4 first) and again only after a failed connect;
TLS keeps the name as given. Plus a bare pod name (``benchpod-a1b2c3``) in parse_connection."""

import socket

import pytest

from embeddedci.benchpod.connection import parse_connection
from embeddedci.benchpod.discovery import DiscoveredPod, names_pod
from embeddedci.benchpod.errors import TransportError
from embeddedci.benchpod.transport import tcp as tcp_mod
from embeddedci.benchpod.transport.tcp import TcpTransport, resolve_host

NAME = "benchpod-c2f662.local"
#: What the lookups cost on macOS against an IPv4-only pod (measured): a default (AF_UNSPEC)
#: lookup of a .local name waits for an AAAA answer that never comes.
COST = {socket.AF_UNSPEC: 5.0, socket.AF_INET: 0.04}


class _FakeSock:
    """A connected socket that answers every command with ``pong``."""

    def __init__(self, addr):
        self.addr = addr
        self._reply = b'{"status":"ok","data":"pong"}\n'

    def setsockopt(self, *a):
        pass

    def settimeout(self, t):
        pass

    def gettimeout(self):
        return None

    def sendall(self, data):
        pass

    def recv(self, n):
        out, self._reply = self._reply, b""
        return out

    def close(self):
        pass


class _Net:
    """A patched resolver and connect: records each lookup (and its simulated cost) and each
    connect; ``answers`` are the IPv4 answers of successive lookups, ``down`` the addresses that
    refuse a connect."""

    def __init__(self, monkeypatch, answers=(("192.168.1.164",),), v6=(), down=()):
        self.answers = list(answers)
        self.v6 = list(v6)
        self.down = set(down)
        self.lookups = []
        self.connects = []
        self.seconds = 0.0
        monkeypatch.setattr(socket, "getaddrinfo", self.getaddrinfo)
        monkeypatch.setattr(socket, "create_connection", self.create_connection)

    def getaddrinfo(self, host, port, family=0, type=0, *a, **kw):
        self.lookups.append((host, family))
        self.seconds += COST.get(family, 0.0)
        v4 = self.answers[min(len(self.lookups) - 1, len(self.answers) - 1)] if self.answers else ()
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in v4]
        if family == socket.AF_UNSPEC:
            infos += [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port, 0, 0)) for ip in self.v6]
        if not infos:
            raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")
        return infos

    def create_connection(self, addr, timeout=None):
        self.connects.append(addr)
        if addr[0] in self.down:
            raise ConnectionRefusedError("refused")
        return _FakeSock(addr)


def test_a_name_is_resolved_once_for_many_commands(monkeypatch):
    net = _Net(monkeypatch)
    t = TcpTransport(f"{NAME}:8080")
    for _ in range(10):
        assert t.ping() == "pong"
    assert net.lookups == [(NAME, socket.AF_INET)]
    assert net.connects == [("192.168.1.164", 8080)] * 10
    # 10 commands cost one 0.04 s IPv4 lookup; before, each did a 5 s AF_UNSPEC one (50 s).
    assert net.seconds == pytest.approx(0.04)


def test_ipv4_first_and_unspec_only_without_an_ipv4_answer(monkeypatch):
    net = _Net(monkeypatch, answers=(), v6=("fd00::5",))
    assert resolve_host("pod.example", 8443) == ["fd00::5"]
    assert net.lookups == [("pod.example", socket.AF_INET), ("pod.example", socket.AF_UNSPEC)]

    net = _Net(monkeypatch, answers=(("192.168.1.164",),), v6=("fd00::5",))
    assert resolve_host("pod.example", 8443) == ["192.168.1.164"]
    assert net.lookups == [("pod.example", socket.AF_INET)]


def test_a_failed_connect_resolves_again(monkeypatch):
    """The pod got a new DHCP lease: the cached address refuses, the next attempt resolves the
    name again and connects to the new one, which is then reused."""
    net = _Net(monkeypatch, answers=(("192.168.1.164",), ("192.168.1.165",)))
    t = TcpTransport(f"{NAME}:8080")
    assert t.ping() == "pong"
    net.down.add("192.168.1.164")
    assert t.ping() == "pong"
    assert t.ping() == "pong"
    assert [h for h, _ in net.lookups] == [NAME, NAME]
    assert net.connects == [("192.168.1.164", 8080), ("192.168.1.164", 8080),
                            ("192.168.1.165", 8080), ("192.168.1.165", 8080)]


def test_an_unreachable_name_still_names_the_host(monkeypatch):
    net = _Net(monkeypatch, down=("192.168.1.164",))
    with pytest.raises(TransportError, match=f"could not connect to {NAME}:8080"):
        TcpTransport(f"{NAME}:8080", dial_timeout=0.05)._dial()
    assert len(net.lookups) >= 2  # every retry resolves again after the refused connect


def test_an_ip_literal_is_not_looked_up(monkeypatch):
    net = _Net(monkeypatch)
    t = TcpTransport("192.168.1.164:8080")
    t.ping()
    t.ping()
    assert net.lookups == []
    assert net.connects == [("192.168.1.164", 8080)] * 2


def test_tls_keeps_the_name_for_the_pin_and_sni(monkeypatch):
    """The connect goes to the resolved address; TLS (the pin key, SNI) gets the name as given."""
    net = _Net(monkeypatch)
    wrapped = []

    def fake_wrap(sock, host, port, ca=None, **kw):
        wrapped.append((sock.addr, host, port))
        return sock

    monkeypatch.setattr(tcp_mod._tls, "wrap", fake_wrap)
    t = TcpTransport(f"{NAME}:8443")
    for _ in range(3):
        assert t.ping() == "pong"
    assert wrapped == [(("192.168.1.164", 8443), NAME, 8443)] * 3
    assert len(net.lookups) == 1


# -- a bare pod name ------------------------------------------------------------------------------


def _pod(addresses=("192.168.1.164",), api=2):
    return DiscoveredPod(name="BenchPod c2f662", hostname="benchpod-c2f662.local",
                         addresses=list(addresses), port=8080, tls_port=8443, api=api)


@pytest.mark.parametrize("raw", ["benchpod-c2f662", "c2f662", "BenchPod-C2F662"])
def test_a_pod_name_is_found_over_mdns(raw, monkeypatch):
    asked = []
    monkeypatch.setattr("embeddedci.benchpod.discovery.find_pod",
                        lambda short, timeout=3.0: asked.append(short) or _pod())
    spec = parse_connection(raw)
    assert asked == ["c2f662"]
    assert spec.kind == "tcp" and spec.addr == "192.168.1.164:8443"


def test_a_pod_name_not_on_the_lan_is_looked_for_on_usb(monkeypatch):
    monkeypatch.setattr("embeddedci.benchpod.discovery.find_pod", lambda short, timeout=3.0: None)
    spec = parse_connection("benchpod-c2f662")
    assert spec.kind == "serial" and spec.device == "" and spec.pod == "c2f662"


def test_a_pod_name_without_zeroconf_is_its_local_host_name(monkeypatch):
    def no_zeroconf(short, timeout=3.0):
        raise RuntimeError("mDNS discovery needs the 'zeroconf' package")

    monkeypatch.setattr("embeddedci.benchpod.discovery.find_pod", no_zeroconf)
    assert parse_connection("benchpod-c2f662").addr == "benchpod-c2f662.local:8443"


def test_a_pod_name_on_firmware_3_is_refused(monkeypatch):
    from embeddedci.benchpod.errors import FirmwareTooOldError

    monkeypatch.setattr("embeddedci.benchpod.discovery.find_pod", lambda short, timeout=3.0: _pod(api=0))
    with pytest.raises(FirmwareTooOldError):
        parse_connection("benchpod-c2f662")


@pytest.mark.parametrize("raw", ["benchpod-c2f662.local", "benchpod-c2f662x", "my-pod", "c2f6621"])
def test_other_names_stay_host_names(raw, monkeypatch):
    monkeypatch.setattr("embeddedci.benchpod.discovery.find_pod",
                        lambda *a, **k: pytest.fail("not a pod name"))
    assert parse_connection(raw).addr == f"{raw}:8443"


def test_names_pod_matches_instance_or_host_name():
    assert names_pod(_pod(), "c2f662")
    assert names_pod(DiscoveredPod(name="x", hostname="benchpod-c2f662.local"), "c2f662")
    assert not names_pod(_pod(), "baea06")

