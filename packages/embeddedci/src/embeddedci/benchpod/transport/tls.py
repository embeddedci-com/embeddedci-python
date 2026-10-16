"""The pod's LAN API over TLS (firmware 3.9.3+), as ``benchpod`` does it (its ``tcpclient/tls.go``).

The pod serves the same API on TCP 8443 with TLS 1.2 and its own certificate (self-signed on first
boot, or one the user installed). How :class:`~.tcp.TcpTransport` connects:

- port 8443: TLS only.
- port 8080 (the default): TLS on 8443 first; when 8443 refuses the connection (firmware without
  LAN TLS) and nothing is pinned for the pod, plaintext 8080 as before. With a pin, a pod that stops
  answering TLS is an error, never a silent downgrade.
- any other port, and the pod's USB network adapter (10.237.0.0/16, a physical link): plaintext.

``tls="on"`` / ``"off"`` (or ``BENCHPOD_TLS``) overrides ``"auto"``.

Trust: the SHA-256 of the pod's certificate (DER) is pinned on first use per address in
``known_pods.json`` next to ``benchpod-cli``'s config (the CLI reads and writes the same file). A
different certificate later raises :class:`~..errors.TlsPinMismatchError`. With a CA file
(``tls_ca`` or ``BENCHPOD_TLS_CA``) the certificate is verified against it and the host name
instead, and nothing is pinned.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import ssl
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, NamedTuple, Optional, Tuple

from ..cli_config import config_dir

PLAIN_PORT = 8080
TLS_PORT = 8443
_USB_LINK = ipaddress.ip_network("10.237.0.0/16")
_lock = threading.Lock()
#: Pods (host:port) that refused 8443 in this process, so each command does not probe again.
_no_tls: set = set()


def tls_mode(value: Optional[str] = None) -> str:
    """``"auto"``, ``"on"`` or ``"off"`` from ``value``, else ``BENCHPOD_TLS``, else ``"auto"``."""
    v = (value if value is not None else os.environ.get("BENCHPOD_TLS", "")).strip().lower()
    if v in ("", "auto"):
        return "auto"
    if v in ("on", "true", "yes", "1"):
        return "on"
    if v in ("off", "false", "no", "0"):
        return "off"
    raise ValueError(f"tls: {v!r} is not auto, on or off")


def is_usb_link(host: str) -> bool:
    """Whether ``host`` is on the pod's USB network adapter."""
    try:
        return ipaddress.ip_address(host) in _USB_LINK
    except ValueError:
        return False


def tls_addr(host: str, port: int) -> Tuple[str, int]:
    """Where a TLS connection to this pod goes, also the key of its pin: ``host:8443`` for the
    default port, else the address as given."""
    return (host, TLS_PORT) if port == PLAIN_PORT else (host, port)


class DialPlan(NamedTuple):
    tls: Optional[Tuple[str, int]]    # None: no TLS attempt
    plain: Optional[Tuple[str, int]]  # None: no plaintext
    fallback: bool                    # plaintext only when TLS is refused and nothing is pinned


def plan(host: str, port: int, mode: str) -> DialPlan:
    """How to reach ``host:port`` in ``mode`` (see the module docstring)."""
    if mode == "off":
        return DialPlan(None, (host, port), False)
    if port == TLS_PORT:
        return DialPlan((host, port), None, False)
    if mode == "on":
        return DialPlan(tls_addr(host, port), None, False)
    if port != PLAIN_PORT or is_usb_link(host):
        return DialPlan(None, (host, port), False)
    return DialPlan((host, TLS_PORT), (host, port), True)


def remember_no_tls(addr: Tuple[str, int]) -> None:
    _no_tls.add(addr)


def known_no_tls(addr: Tuple[str, int]) -> bool:
    return addr in _no_tls


# -- pins ---------------------------------------------------------------------------------------


def pin_store_path() -> Path:
    """``known_pods.json`` in ``benchpod-cli``'s config directory."""
    return config_dir() / "known_pods.json"


def _key(host: str, port: int) -> str:
    h, p = tls_addr(host, port)
    return f"[{h}]:{p}" if ":" in h else f"{h}:{p}"


def _load(path: Path) -> Dict:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"version": 1, "pods": {}}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a JSON object")
    data.setdefault("version", 1)
    if not isinstance(data.get("pods"), dict):
        data["pods"] = {}
    return data


def _save(path: Path, data: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".known_pods-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_pin(host: str, port: int = PLAIN_PORT) -> Optional[str]:
    """The pinned SHA-256 (lowercase hex) for the pod at ``host``, if any."""
    with _lock:
        try:
            pod = _load(pin_store_path())["pods"].get(_key(host, port))
        except (OSError, ValueError):
            return None
    return pod.get("sha256") if isinstance(pod, dict) else None


def set_pin(host: str, sha256: str, port: int = PLAIN_PORT, subject: str = "") -> None:
    """Pin ``sha256`` for the pod at ``host`` (what ``benchpod lan-tls trust`` does)."""
    with _lock:
        path = pin_store_path()
        data = _load(path)
        data["pods"][_key(host, port)] = {
            "sha256": sha256.lower(),
            "subject": subject,
            "added": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        _save(path, data)


def delete_pin(host: str, port: int = PLAIN_PORT) -> bool:
    """Forget the pod's pin. ``False`` when there was none."""
    with _lock:
        path = pin_store_path()
        data = _load(path)
        if data["pods"].pop(_key(host, port), None) is None:
            return False
        _save(path, data)
        return True


def fingerprint(der: bytes) -> str:
    """Lowercase hex SHA-256 of a certificate (DER): the value the pod reports as ``sha256``."""
    return hashlib.sha256(der).hexdigest()


def _check_pin(host: str, port: int, der: bytes) -> None:
    from ..errors import TlsPinMismatchError

    got = fingerprint(der)
    with _lock:
        path = pin_store_path()
        data = _load(path)
        key = _key(host, port)
        pod = data["pods"].get(key)
        if isinstance(pod, dict) and pod.get("sha256"):
            if pod["sha256"].lower() != got:
                raise TlsPinMismatchError(key, pod["sha256"].lower(), got)
            return
        data["pods"][key] = {"sha256": got, "subject": "",
                             "added": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        _save(path, data)


# -- the handshake ------------------------------------------------------------------------------


def wrap(sock: socket.socket, host: str, port: int, *, ca: Optional[str] = None,
         pin: bool = True, timeout: float = 10.0) -> ssl.SSLSocket:
    """TLS over a connected socket to the pod at ``host:port``.

    With ``ca`` (a PEM file of CA certificates) the certificate chain and host name are verified.
    Otherwise the certificate is checked against (or, on first use, recorded as) the pin; with
    ``pin=False`` any certificate is accepted (to show or trust it)."""
    is_ip = True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        is_ip = False
    if ca and pin:
        ctx = ssl.create_default_context(cafile=ca)
        check_name = True
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # the pin below is the check
        check_name = False
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    old = sock.gettimeout()
    sock.settimeout(timeout)
    tls = ctx.wrap_socket(sock, server_hostname=host if (check_name or not is_ip) else None)
    try:
        if not ca and pin:
            der = tls.getpeercert(binary_form=True)
            if not der:
                raise ssl.SSLError("the pod sent no certificate")
            _check_pin(host, port, der)
    except BaseException:
        tls.close()
        raise
    tls.settimeout(old)
    return tls


def trust_lan_tls(host: str, port: int = PLAIN_PORT, *, sha256: Optional[str] = None,
                  timeout: float = 10.0) -> str:
    """Pin the certificate the pod at ``host`` presents now (after a deliberate change), like
    ``benchpod lan-tls trust``. With ``sha256`` only that certificate is trusted. Returns the
    pinned SHA-256."""
    der = peer_certificate(host, port, timeout=timeout)
    got = fingerprint(der)
    if sha256 and sha256.strip().lower() != got:
        raise ValueError(f"the pod presents sha256 {got}, not {sha256.strip().lower()}; not trusted")
    set_pin(host, got, port=port)
    return got


def peer_certificate(host: str, port: int = PLAIN_PORT, *, timeout: float = 10.0) -> bytes:
    """The certificate (DER) the pod at ``host`` presents on its TLS port, unchecked."""
    th, tp = tls_addr(host, port)
    sock = socket.create_connection((th, tp), timeout=timeout)
    try:
        tls = wrap(sock, th, tp, pin=False, timeout=timeout)
    except BaseException:
        sock.close()
        raise
    try:
        der = tls.getpeercert(binary_form=True)
    finally:
        tls.close()
    if not der:
        raise ssl.SSLError("the pod sent no certificate")
    return der
