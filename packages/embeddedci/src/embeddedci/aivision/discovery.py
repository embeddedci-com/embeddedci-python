"""mDNS discovery of ai-vision-2 boards on the local network.

The board's avahi announces ``_aivision._tcp`` with its hostname (``ai-vision-2-5dcec6``).
``zeroconf`` is an optional dependency (``pip install embeddedci[discovery]``), imported lazily.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List

#: DNS-SD service type the board advertises.
SERVICE_TYPE = "_aivision._tcp.local."

DEFAULT_TIMEOUT = 3.0


@dataclass
class DiscoveredBoard:
    """One ai-vision-2 board heard on the LAN."""

    name: str  # DNS-SD instance name
    hostname: str  # "ai-vision-2-5dcec6.local"
    addresses: List[str] = field(default_factory=list)
    port: int = 80

    @property
    def addr(self) -> str:
        """``host:port`` ready for :class:`~embeddedci.aivision.AIVision` (a numeric address first)."""
        host = (self.addresses or [self.hostname])[0]
        return f"{host}:{self.port}"


def discover(timeout: float = DEFAULT_TIMEOUT) -> List[DiscoveredBoard]:
    """Browse the LAN for ``timeout`` seconds and return every board found (possibly none)."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError("AI Vision discovery needs zeroconf: pip install 'embeddedci[discovery]'") from exc

    found: Dict[str, DiscoveredBoard] = {}

    class _Listener:
        def add_service(self, zc, type_, name):  # noqa: ANN001 - zeroconf callback
            info = zc.get_service_info(type_, name, timeout=int(timeout * 1000))
            if info is None:
                return
            host = (info.server or "").rstrip(".")
            found[name] = DiscoveredBoard(name=name.replace("." + type_, ""), hostname=host,
                                          addresses=list(info.parsed_addresses()), port=info.port or 80)

        def update_service(self, zc, type_, name):  # noqa: ANN001
            self.add_service(zc, type_, name)

        def remove_service(self, zc, type_, name):  # noqa: ANN001
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, SERVICE_TYPE, _Listener())
        time.sleep(timeout)
    finally:
        zc.close()
    return sorted(found.values(), key=lambda b: b.hostname)
