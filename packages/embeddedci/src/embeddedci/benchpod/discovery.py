"""mDNS/DNS-SD discovery of BenchPods on the local network.

Browses ``_benchpod._tcp.local.`` and returns every pod it hears — each with
its own addresses, port, and stable Ed25519 id (from the TXT record). This is
the enumeration source: zero pods means nothing answered on the subnet; several
pods means the caller disambiguates (by id-prefix or hostname).

The firmware advertises the same service type from every pod, with a unique
hostname/instance derived from a slice of its Ed25519 public key and the full
public key carried in the TXT ``id=`` item. See bench-pod-firmware
``stm32h563/src/net_server.c`` (mDNS responder).

``zeroconf`` is an optional dependency (``pip install embeddedci[discovery]``);
it is imported lazily so the rest of the package works without it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List

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
    port: int = 8080
    pod_id: str = ""  # full base64url Ed25519 pubkey (TXT id=)

    @property
    def addr(self) -> str:
        """A ``host:port`` string ready for :func:`parse_connection`.

        Prefers a numeric address (avoids depending on the OS mDNS resolver),
        falling back to the ``.local`` hostname if no address was resolved.
        """
        host = self.addresses[0] if self.addresses else self.hostname
        return f"{host}:{self.port}"


def discover(timeout: float = DEFAULT_TIMEOUT) -> List[DiscoveredPod]:
    """Browse the LAN for ``timeout`` seconds; return all pods found.

    Raises :class:`RuntimeError` with an install hint if ``zeroconf`` is not
    installed. Returns an empty list when nothing answers.
    """
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError as exc:  # pragma: no cover - exercised via install extra
        raise RuntimeError(
            "mDNS discovery needs the 'zeroconf' package; install it with "
            "`pip install embeddedci[discovery]`"
        ) from exc

    found: Dict[str, DiscoveredPod] = {}
    zc = Zeroconf()

    def _record(zeroconf, type_: str, name: str) -> None:
        info = zeroconf.get_service_info(type_, name, timeout=int(timeout * 1000))
        if info is None:
            return
        txt = {
            k.decode(errors="replace"): (v or b"").decode(errors="replace")
            for k, v in (info.properties or {}).items()
        }
        found[name] = DiscoveredPod(
            name=name.removesuffix("." + SERVICE_TYPE),
            hostname=(info.server or "").rstrip("."),
            addresses=list(info.parsed_addresses()),
            port=info.port or 8080,
            pod_id=txt.get("id", ""),
        )

    class _Listener:
        def add_service(self, zeroconf, type_, name):
            _record(zeroconf, type_, name)

        def update_service(self, zeroconf, type_, name):
            _record(zeroconf, type_, name)

        def remove_service(self, zeroconf, type_, name):
            found.pop(name, None)

    ServiceBrowser(zc, SERVICE_TYPE, _Listener())
    try:
        time.sleep(timeout)
    finally:
        zc.close()

    return sorted(found.values(), key=lambda p: p.name)
