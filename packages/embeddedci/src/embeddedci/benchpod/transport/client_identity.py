"""This computer's TLS client identity for LAN pairing (firmware 4.0.1+), as ``benchpod`` keeps it
(its ``tcpclient/clientid.go``).

The pod asks every client on port 8443 for a certificate and lets in beyond ``status`` only the
ones whose public key is on its list of paired computers. Pairing puts the key there, over the
pod's USB cable (:meth:`~embeddedci.benchpod.BenchPod.pair`, ``benchpod setup``) or from
embeddedci.com.

One key per user, shared with ``benchpod`` and the MCP server: ``client.key`` (EC P-256, PKCS#8)
and ``client.crt`` (self-signed) next to ``known_pods.json`` in ``benchpod-cli``'s config
directory, made on first use. A CI runner passes its own instead:

- ``BENCHPOD_CLIENT_KEY_PEM``: the certificate and key PEM (``benchpod lan-clients add --new-key``
  prints one), for a CI secret;
- ``BENCHPOD_CLIENT_CERT`` and ``BENCHPOD_CLIENT_KEY``: files.
"""

from __future__ import annotations

import atexit
import datetime as _dt
import hashlib
import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from ..cli_config import config_dir

CLIENT_KEY_FILE = "client.key"
CLIENT_CERT_FILE = "client.crt"

_lock = threading.Lock()
_files: Optional[Tuple[str, str]] = None
_tmp_files: list = []

#: Overrides the directory ``client.key`` and ``client.crt`` live in (tests).
identity_dir: Optional[Path] = None


def _dir() -> Path:
    return identity_dir if identity_dir is not None else config_dir()


def reset() -> None:
    """Forget the cached identity (tests, and after the files change)."""
    global _files
    with _lock:
        _files = None


def new_identity_pem(cn: str) -> Tuple[bytes, bytes]:
    """A new EC P-256 key and a self-signed client certificate for it: ``(cert_pem, key_pem)``."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(hours=1))
        .not_valid_after(now + _dt.timedelta(days=365 * 100))  # the pod checks the key, not the dates
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                     key_encipherment=False, data_encipherment=False,
                                     key_agreement=False, key_cert_sign=False, crl_sign=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()))


def _write_exclusive(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(fd, data)
    except BaseException:
        os.close(fd)
        os.unlink(str(path))
        raise
    os.close(fd)


def _from_env_pem(pem: str) -> Tuple[str, str]:
    """``ssl`` loads a certificate chain only from files: the environment's PEM goes to a private
    temporary file for this process."""
    fd, path = tempfile.mkstemp(prefix="benchpod-client-", suffix=".pem")
    with os.fdopen(fd, "w") as f:
        f.write(pem.strip() + "\n")
    os.chmod(path, 0o600)
    _tmp_files.append(path)
    return path, path


@atexit.register
def _cleanup() -> None:
    for p in _tmp_files:
        try:
            os.unlink(p)
        except OSError:
            pass


def _load() -> Tuple[str, str]:
    pem = os.environ.get("BENCHPOD_CLIENT_KEY_PEM", "").strip()
    if pem:
        return _from_env_pem(pem)
    cert_file, key_file = os.environ.get("BENCHPOD_CLIENT_CERT", ""), os.environ.get("BENCHPOD_CLIENT_KEY", "")
    if cert_file or key_file:
        if not (cert_file and key_file):
            raise ValueError("set both BENCHPOD_CLIENT_CERT and BENCHPOD_CLIENT_KEY")
        return cert_file, key_file
    d = _dir()
    cert_path, key_path = d / CLIENT_CERT_FILE, d / CLIENT_KEY_FILE
    if cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)
    cert_pem, key_pem = new_identity_pem("benchpod client " + (socket.gethostname() or "computer"))
    d.mkdir(parents=True, exist_ok=True)
    try:
        # The key first and exclusively: two processes (or this and benchpod) starting at once
        # must not end up with two keys. The loser waits for the winner's certificate.
        _write_exclusive(key_path, key_pem, 0o600)
    except FileExistsError:
        for _ in range(50):
            if cert_path.exists():
                return str(cert_path), str(key_path)
            time.sleep(0.02)
        raise
    _write_exclusive(cert_path, cert_pem, 0o644)
    return str(cert_path), str(key_path)


def identity_files() -> Optional[Tuple[str, str]]:
    """``(certfile, keyfile)`` of this computer's identity, made on first use; ``None`` when it
    cannot be read or made (the pod then treats the connection as unpaired and says so)."""
    global _files
    with _lock:
        if _files is None:
            try:
                _files = _load()
            except (ImportError, OSError, ValueError):
                return None
        return _files


def spki_sha256_of_pem(cert_pem: bytes) -> str:
    """What the pod lists a paired computer by: the SHA-256 (hex) of the certificate's DER
    SubjectPublicKeyInfo."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_pem_x509_certificate(cert_pem)
    spki = cert.public_key().public_bytes(serialization.Encoding.DER,
                                          serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(spki).hexdigest()


def client_key_hash() -> str:
    """:func:`spki_sha256_of_pem` of this computer's identity."""
    files = identity_files()
    if files is None:
        raise RuntimeError("no TLS client identity: install the 'cryptography' package, or set "
                           "BENCHPOD_CLIENT_KEY_PEM")
    return spki_sha256_of_pem(Path(files[0]).read_bytes())


def sanitize_name(s: str) -> str:
    """A name the pod takes: 1-23 printable characters, no spaces (the USB console takes it too)."""
    out = []
    for ch in s:
        if ch in " \t":
            out.append("-")
        elif 0x21 <= ord(ch) <= 0x7E and ch not in "\"\\":
            out.append(ch)
        if len(out) >= 23:
            break
    name = "".join(out).strip("-")[:23]
    return name or "computer"


def pairing_name() -> str:
    """This computer's host name as a pairing name."""
    h = socket.gethostname() or ""
    if h.endswith(".local"):
        h = h[: -len(".local")]
    return sanitize_name(h)
