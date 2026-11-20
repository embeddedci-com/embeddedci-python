"""LAN pairing (firmware 4.0.1+): this computer's TLS client identity, the console's lan-clients and
lan-tls replies, and BenchPod.lan_clients / pair."""

import hashlib
import os
import ssl
import stat

import pytest

from embeddedci.benchpod import BenchPod, LanClients, PodLockedError, PodNotPairedError
from embeddedci.benchpod.errors import FirmwareError, firmware_error
from embeddedci.benchpod.transport import client_identity as ci
from embeddedci.benchpod.transport import tls as _tls
from embeddedci.benchpod.transport.serial import parse_text_lan_clients, parse_text_lan_tls

KEY_A = "3f0c9a1e" + "0" * 55 + "a"
KEY_B = "9b2e" + "0" * 59 + "b"


@pytest.fixture
def idir(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "identity_dir", tmp_path)
    for v in ("BENCHPOD_CLIENT_KEY_PEM", "BENCHPOD_CLIENT_CERT", "BENCHPOD_CLIENT_KEY"):
        monkeypatch.delenv(v, raising=False)
    ci.reset()
    yield tmp_path
    ci.reset()


def test_identity_made_once_and_kept(idir):
    a = ci.client_key_hash()
    assert len(a) == 64
    assert stat.S_IMODE(os.stat(idir / "client.key").st_mode) == 0o600
    ci.reset()
    assert ci.client_key_hash() == a
    # ssl takes the files as they are
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_cert_chain(*ci.identity_files())


def test_spki_hash_is_the_public_key_hash(idir):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    cert_pem, _ = ci.new_identity_pem("x")
    spki = x509.load_pem_x509_certificate(cert_pem).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert ci.spki_sha256_of_pem(cert_pem) == hashlib.sha256(spki).hexdigest()


def test_identity_from_env_pem(idir, monkeypatch):
    cert_pem, key_pem = ci.new_identity_pem("ci-runner-1")
    monkeypatch.setenv("BENCHPOD_CLIENT_KEY_PEM", (cert_pem + key_pem).decode())
    ci.reset()
    assert ci.client_key_hash() == ci.spki_sha256_of_pem(cert_pem)
    assert list(idir.iterdir()) == []   # nothing written next to known_pods.json


def test_half_the_file_variables_is_no_identity(idir, monkeypatch):
    monkeypatch.setenv("BENCHPOD_CLIENT_CERT", "/nope.crt")
    ci.reset()
    assert ci.identity_files() is None


@pytest.mark.parametrize("raw,want", [
    ("Edward's MacBook Pro", "Edward's-MacBook-Pro"),
    ('a"b\\c', "abc"),
    ("", "computer"),
    ("build-runner-with-a-very-long-name-01", "build-runner-with-a-ver"),
])
def test_sanitize_name(raw, want):
    assert ci.sanitize_name(raw) == want


def test_parse_console_lan_clients():
    line = f"lan-clients add laptop {KEY_A}"
    raw = (f"> {line}\r\n[net] noise\r\nlan-clients added\r\nlan-clients 2 of 32, pairing on\r\n"
           f"  laptop {KEY_A}\r\n  ci-runner-1 {KEY_B}\r\n> ")
    d = parse_text_lan_clients(raw, line)
    assert d["outcome"] == "added" and d["count"] == 2 and d["max"] == 32 and d["pairing"] is True
    assert [c["name"] for c in d["clients"]] == ["laptop", "ci-runner-1"]
    with pytest.raises(FirmwareError, match="out of memory"):
        parse_text_lan_clients("lan-clients clear\r\nlan-clients error out of memory\r\n", "lan-clients clear")


def test_parse_console_lan_tls():
    raw = (f'lan-tls\r\nlan-tls port 8443 https-only on sha256 {KEY_A.upper()} generated "EC P-256" '
           '"CN=benchpod-30a9f1" "benchpod-30a9f1" scpi off\r\n> ')
    d = parse_text_lan_tls(raw)
    assert d == {"sha256": KEY_A, "port": 8443, "https_only": True, "scpi": False}


def test_not_paired_is_a_locked_error():
    e = firmware_error("", cmd="generate", code="BP0081", values={"cmd": "generate"})
    assert isinstance(e, PodNotPairedError) and isinstance(e, PodLockedError)
    assert "not paired" in str(e)


class _FakeTransport:
    """The JSON commands pair and lan_clients send, answered like a 4.0.1 pod over USB."""

    def __init__(self, n_clients=0):
        self.sent = []
        self.clients = [{"name": f"c{i}", "spki_sha256": f"{i:064x}"} for i in range(n_clients)]

    def command(self, req):
        self.sent.append(req)
        if req["cmd"] == "lan_clients":
            if "add" in req:
                self.clients.append({"name": req["add"], "spki_sha256": req["spki_sha256"]})
            off = int(req.get("offset", 0))
            return {"pairing": True, "count": len(self.clients), "max": 32, "offset": off,
                    "clients": self.clients[off:off + 12]}
        if req["cmd"] == "lan_tls":
            return {"port": 8443, "sha256": KEY_B}
        raise AssertionError(req)

    def status(self):
        return {"ip": "192.168.1.150"}

    def close(self):
        pass


def _pod(t):
    bp = BenchPod.__new__(BenchPod)
    bp._transport = t
    bp.command = t.command
    return bp


def test_lan_clients_reads_every_page(idir):
    t = _FakeTransport(n_clients=30)
    got = _pod(t).lan_clients()
    assert isinstance(got, LanClients) and got.count == 30 and len(got.clients) == 30
    assert [r.get("offset", 0) for r in t.sent] == [0, 12, 24]


def test_pair_adds_this_computer_and_pins_the_lan_certificate(idir, monkeypatch):
    pins = {}
    monkeypatch.setattr(_tls, "set_pin", lambda host, sha, port=8443, subject="": pins.update({host: sha}))
    t = _FakeTransport()
    got = _pod(t).pair(name="my laptop")
    mine = ci.client_key_hash()
    assert t.sent[0] == {"cmd": "lan_clients", "add": "my-laptop", "spki_sha256": mine}
    assert got.has(mine) and [c.this for c in got.clients] == [True]
    assert pins == {"192.168.1.150": KEY_B}
