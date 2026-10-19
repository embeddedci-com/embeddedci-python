"""The LAN API over TLS (transport/tls.py): dial plan, trust on first use, pin mismatch, re-trust,
and the pin file the CLI shares (firmware 3.9.3+)."""

import json
import shutil
import socket
import ssl
import subprocess
import threading

import pytest

from embeddedci.benchpod import TlsPinMismatchError, forget_lan_tls, trust_lan_tls
from embeddedci.benchpod.transport import tls as T
from embeddedci.benchpod.transport.tcp import TcpTransport


@pytest.fixture(autouse=True)
def pin_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("BENCHPOD_TLS", raising=False)
    monkeypatch.delenv("BENCHPOD_TLS_CA", raising=False)
    return tmp_path


def test_plan():
    assert T.plan("192.168.1.5", 8080, "auto") == T.DialPlan(("192.168.1.5", 8443), ("192.168.1.5", 8080), True)
    assert T.plan("192.168.1.5", 8443, "auto") == T.DialPlan(("192.168.1.5", 8443), None, False)
    assert T.plan("192.168.1.5", 9000, "auto") == T.DialPlan(None, ("192.168.1.5", 9000), False)
    assert T.plan("10.237.8.85", 8080, "auto") == T.DialPlan(None, ("10.237.8.85", 8080), False)
    assert T.plan("192.168.1.5", 8443, "off") == T.DialPlan(None, ("192.168.1.5", 8443), False)
    assert T.plan("10.237.8.85", 8080, "on") == T.DialPlan(("10.237.8.85", 8443), None, False)
    assert T.tls_mode("ON") == "on" and T.tls_mode("") == "auto"
    with pytest.raises(ValueError):
        T.tls_mode("maybe")


def _make_cert(tmp_path, name):
    if not shutil.which("openssl"):
        pytest.skip("openssl not installed")
    key, crt = tmp_path / f"{name}.key", tmp_path / f"{name}.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-nodes", "-keyout", str(key), "-out", str(crt), "-days", "1", "-subj", f"/CN={name}"],
                   check=True, capture_output=True)
    return crt, key


class _TlsPod:
    """A TLS JSON server answering every line with an ok reply; its certificate can change."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.n = 0
        self.rotate()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def rotate(self):
        self.n += 1
        crt, key = _make_cert(self.tmp, f"benchpod-test{self.n}")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(crt), str(key))
        self.ctx = ctx
        self.der = ssl.PEM_cert_to_DER_cert(crt.read_text())

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            try:
                t = self.ctx.wrap_socket(c, server_side=True)
                f = t.makefile("rb")
                for _ in f:
                    t.sendall(b'{"status":"ok","data":"pong"}\n')
            except (OSError, ssl.SSLError):
                pass
            finally:
                c.close()

    def close(self):
        self.sock.close()


def test_pin_on_first_use_mismatch_and_trust(tmp_path, pin_dir):
    pod = _TlsPod(tmp_path)
    try:
        t = TcpTransport(f"127.0.0.1:{pod.port}", tls="on", timeout=5, dial_timeout=2)
        assert t.command({"cmd": "ping"}) == "pong"
        assert t.last_tls
        assert T.get_pin("127.0.0.1", pod.port) == T.fingerprint(pod.der)
        # The CLI's file format: version + pods keyed by host:port.
        data = json.loads((pin_dir / "benchpod-cli" / "known_pods.json").read_text())
        assert data["pods"][f"127.0.0.1:{pod.port}"]["sha256"] == T.fingerprint(pod.der)

        assert t.command({"cmd": "ping"}) == "pong"
        old = pod.der
        pod.rotate()
        with pytest.raises(TlsPinMismatchError) as ei:
            t.command({"cmd": "ping"})
        assert ei.value.pinned == T.fingerprint(old)
        assert ei.value.got == T.fingerprint(pod.der)

        with pytest.raises(ValueError):
            trust_lan_tls("127.0.0.1", pod.port, sha256="00" * 32)
        assert trust_lan_tls("127.0.0.1", pod.port, sha256=ei.value.got) == ei.value.got
        assert t.command({"cmd": "ping"}) == "pong"
        assert t.command_sequence([{"cmd": "ping"}, {"cmd": "ping"}]) == ["pong", "pong"]

        assert forget_lan_tls("127.0.0.1", pod.port)
        assert not forget_lan_tls("127.0.0.1", pod.port)
        assert T.get_pin("127.0.0.1", pod.port) is None
    finally:
        pod.close()


def test_pin_key_for_the_default_port_is_the_tls_port():
    T.set_pin("192.168.1.5", "AB" * 32)
    assert T.get_pin("192.168.1.5", 8080) == "ab" * 32
    assert T.get_pin("192.168.1.5", 8443) == "ab" * 32


@pytest.mark.parametrize("probe_error", [ConnectionRefusedError, TimeoutError])
def test_a_pod_without_tls_is_probed_once_per_process(monkeypatch, probe_error):
    """A pod without LAN TLS refuses 8443 or, when it lets the first SYN go unanswered, the probe
    times out: either way it is probed once, not on every command (each one dials anew)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    real = socket.create_connection
    dialed = []

    def fake_create_connection(addr, timeout=None):
        dialed.append((addr[1], timeout))
        if addr[1] == 8443:
            raise probe_error("no TLS here")
        return real(server.getsockname(), timeout=timeout)

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    host = "192.0.2.%d" % (10 + [ConnectionRefusedError, TimeoutError].index(probe_error))
    t = TcpTransport(f"{host}:8080")
    for _ in range(3):
        t._dial().close()
    assert [port for port, _ in dialed] == [8443, 8080, 8080, 8080]
    assert dialed[0][1] >= 1.2, "the probe must outlast the host's 1 s SYN retransmit"
    assert not t.last_tls
    server.close()
