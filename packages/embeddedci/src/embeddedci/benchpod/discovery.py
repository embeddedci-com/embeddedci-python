"""mDNS/DNS-SD discovery of BenchPods on the local network.

Browses ``_benchpod._tcp.local.`` and returns every pod it hears — each with
its own addresses, port, and stable Ed25519 id (from the TXT record). This is
the enumeration source: zero pods means nothing answered on the subnet; several
pods means the caller disambiguates (by id-prefix or hostname).

The firmware advertises the same service type from every pod, with a unique
hostname/instance derived from a slice of its Ed25519 public key and the full
public key carried in the TXT ``id=`` item; firmware 4.0 adds ``api=2`` (its
client API level), ``tls=8443`` and ``https_only=1``. See bench-pod-firmware
``stm32h563/src/net_server.c`` (mDNS responder).

``zeroconf`` is an optional dependency (``pip install embeddedci[discovery]``);
it is imported lazily so the rest of the package works without it.
"""

from __future__ import annotations

import ipaddress
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: DNS-SD service type the firmware advertises.
SERVICE_TYPE = "_benchpod._tcp.local."

#: Default browse window. mDNS answers arrive within a few hundred ms on a quiet
#: LAN; 3s comfortably covers slow responders without dragging the CLI/fixtures.
DEFAULT_TIMEOUT = 3.0


@dataclass
class DiscoveredPod:
    """One BenchPod heard on the LAN via mDNS."""

    name: str  # DNS-SD instance, e.g. "BenchPod a1b2c3"
    hostname: str  # "benchpod-a1b2c3.local"
    addresses: List[str] = field(default_factory=list)  # all A-record IPs
    port: int = 8080  # the SRV port: plaintext 8080, which the LAN takes only with HTTPS-only off
    pod_id: str = ""  # full base64url Ed25519 pubkey (TXT id=)
    #: The TLS port (TXT ``tls=``, 8443); 0 when the pod does not announce one.
    tls_port: int = 0
    #: The pod's client API level (TXT ``api=``); 0 when the pod does not announce one (firmware
    #: 3.x, which this SDK does not drive).
    api: int = 0
    #: TXT ``https_only=1``: plaintext 8080 is off on the LAN.
    https_only: bool = False

    @property
    def supported(self) -> bool:
        """Whether this SDK drives the pod: it announces client API 2 or later (firmware 4.0)."""
        from .feature_map import MIN_POD_API

        return self.api >= MIN_POD_API

    @property
    def addr(self) -> str:
        """A ``host:port`` string ready for :func:`parse_connection`.

        Prefers a numeric address (avoids depending on the OS mDNS resolver), and of those the
        pod's USB network link when this computer heard it there: a pod advertises itself on each
        of its interfaces. Falls back to the ``.local`` hostname if no address was resolved. The
        port is the plaintext one on the USB link, else the TLS one (TXT ``tls``, 8443).
        """
        usb = [a for a in self.addresses if _on_usb_link(a)]
        host = (usb or self.addresses or [self.hostname])[0]
        port = self.port if usb else (self.tls_port or 8443)
        return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"

    @property
    def on_usb(self) -> bool:
        """True when this computer reaches the pod over its USB network link."""
        return any(_on_usb_link(a) for a in self.addresses)


#: Where a pod's USB network adapter lives: a /30 per pod, derived from its unique ID.
USB_LINK_NET = ipaddress.IPv4Network("10.237.0.0/16")


def _txt_int(txt: Dict[str, str], key: str) -> int:
    v = txt.get(key, "").strip()
    return int(v) if v.isdigit() else 0


def _on_usb_link(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr) in USB_LINK_NET
    except ValueError:
        return False


def discover(timeout: float = DEFAULT_TIMEOUT) -> List[DiscoveredPod]:
    """Browse the LAN for ``timeout`` seconds; return all pods found.

    Raises :class:`RuntimeError` with an install hint if ``zeroconf`` is not
    installed. Returns an empty list when nothing answers.
    """
    return sorted(_browse(timeout).values(), key=lambda p: p.name)


def names_pod(pod: DiscoveredPod, short: str) -> bool:
    """Whether ``pod`` is the pod with the short id ``short`` (``"a1b2c3"``), by its DNS-SD
    instance (``"BenchPod a1b2c3"``) or host name (``"benchpod-a1b2c3.local"``)."""
    from .connection import normalize_pod_name

    return normalize_pod_name(pod.name) == short or normalize_pod_name(pod.hostname) == short


def find_pod(short: str, timeout: float = DEFAULT_TIMEOUT) -> Optional[DiscoveredPod]:
    """Browse the LAN for the pod with the short id ``short`` (``"a1b2c3"``), stopping as soon as
    it answers; None when it does not within ``timeout`` seconds. Raises :class:`RuntimeError`
    without ``zeroconf``, like :func:`discover`."""
    for pod in _browse(timeout, want=short).values():
        if names_pod(pod, short) and pod.addresses:
            return pod
    return None


def _browse(timeout: float, want: str = "") -> Dict[str, DiscoveredPod]:
    """The pods heard within ``timeout`` seconds, by service name; with ``want`` (a short id) the
    browse ends as soon as that pod answers with an address."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError as exc:  # pragma: no cover - exercised via install extra
        raise RuntimeError(
            "mDNS discovery needs the 'zeroconf' package; install it with "
            "`pip install embeddedci[discovery]`"
        ) from exc

    found: Dict[str, DiscoveredPod] = {}
    done = threading.Event()
    zc = Zeroconf()

    def _record(zeroconf, type_: str, name: str) -> None:
        info = zeroconf.get_service_info(type_, name, timeout=int(timeout * 1000))
        if info is None:
            return
        txt = {
            k.decode(errors="replace"): (v or b"").decode(errors="replace")
            for k, v in (info.properties or {}).items()
        }
        # An update may come from another interface: keep every address heard.
        prev = found.get(name)
        addresses = list(prev.addresses) if prev else []
        addresses += [a for a in info.parsed_addresses() if a not in addresses]
        found[name] = DiscoveredPod(
            name=name.removesuffix("." + SERVICE_TYPE),
            hostname=(info.server or "").rstrip("."),
            addresses=addresses,
            port=info.port or 8080,
            pod_id=txt.get("id", ""),
            tls_port=_txt_int(txt, "tls"),
            api=_txt_int(txt, "api"),
            https_only=txt.get("https_only", "") == "1",
        )
        if want and found[name].addresses and names_pod(found[name], want):
            done.set()

    class _Listener:
        def add_service(self, zeroconf, type_, name):
            _record(zeroconf, type_, name)

        def update_service(self, zeroconf, type_, name):
            _record(zeroconf, type_, name)

        def remove_service(self, zeroconf, type_, name):
            found.pop(name, None)

    ServiceBrowser(zc, SERVICE_TYPE, _Listener())
    try:
        done.wait(timeout)
    finally:
        zc.close()

    return dict(found)
