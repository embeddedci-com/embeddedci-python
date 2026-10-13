"""Transport backends for the BenchPod client.

:class:`TcpTransport` talks to a pod over the network; :class:`SerialTransport`
over its USB serial console. Use :func:`open_transport` to build the right one
from a resolved :class:`~embeddedci.benchpod.connection.ConnSpec`.

Connection ``usb`` (an auto-detected USB pod) prefers the pod's USB network adapter
(firmware 3.9.0+): the console's ``status`` names the pod's address on that link, and when
the link is up and the API port answers, the result is a :class:`TcpTransport` to it (with
``usb_console`` set to the console's device path). Otherwise it is the console, as before.
An explicit device path always stays the console. Connection ``usb:<name>`` does the same for the
pod with that name when several are plugged in (see :func:`~.serial.autodetect_port`).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any, Callable, Optional

from ..connection import DEFAULT_PORT, ConnSpec
from ..errors import ConnectionConfigError
from .base import RawLink, Transport

__all__ = [
    "Transport",
    "RawLink",
    "open_transport",
    "TcpTransport",
    "SerialTransport",
    "CloudTransport",
]


def open_transport(
    spec: ConnSpec,
    *,
    timeout: float = 30.0,
    api_base: "str | None" = None,
    token: "str | None" = None,
    audience: "str | None" = None,
    api_key: "str | None" = None,
    user_token: "Callable[[], str] | None" = None,
) -> Transport:
    """Construct a transport for ``spec``. Imports backends lazily.

    ``api_base``/``token``/``audience``/``api_key``/``user_token`` apply only to the cloud (``embeddedci``)
    destination.
    """
    if spec.is_wifi():
        from .tcp import TcpTransport

        return TcpTransport(spec.addr, timeout=timeout)
    if spec.is_serial():
        return _open_usb(spec.device, timeout=timeout, pod=spec.pod)
    if spec.is_cloud():
        from ..cloud_auth import DEFAULT_API_BASE, DEFAULT_AUDIENCE
        from .cloud import CloudTransport

        return CloudTransport(
            spec.device_name,
            api_base=api_base or DEFAULT_API_BASE,
            token=token,
            audience=audience or DEFAULT_AUDIENCE,
            timeout=timeout,
            api_key=api_key,
            user_token=user_token,
        )
    raise ConnectionConfigError(f"unknown connection kind {spec.kind!r}")


_log = logging.getLogger("embeddedci.benchpod")

#: Budget for the TCP connect that checks the pod's API answers on its USB network address.
USB_NET_PROBE_TIMEOUT = 1.5
#: Budget for reading the console's ``status`` while choosing between USB network and console.
_USB_STATUS_TIMEOUT = 5.0


def usb_net_address(status: Any) -> Optional[str]:
    """The pod's IPv4 address on its USB network link, from a ``status`` reply (JSON, or the
    console's text status parsed by :func:`~.serial.parse_text_status`), or None when USB
    networking is off, its link is down, or the firmware has no USB network (before 3.9.0)."""
    usb = status.get("usb") if isinstance(status, dict) else None
    if not isinstance(usb, dict) or usb.get("net") != "on" or usb.get("link") is not True:
        return None
    try:
        ip = ipaddress.IPv4Address(str(usb.get("ip", "")))
    except ValueError:
        return None
    return None if ip.is_unspecified else str(ip)


def usb_lan_fallback_address(status: Any) -> Optional[str]:
    """The pod's Ethernet or Wi-Fi address from a ``status`` reply, for a 3.9.0+ pod (one that
    reports a ``usb`` section) whose USB network is off or down; None otherwise. Firmware before
    3.9.0 keeps every command on its console, so it never needs this."""
    if not isinstance(status, dict) or not isinstance(status.get("usb"), dict):
        return None
    try:
        ip = ipaddress.IPv4Address(str(status.get("ip", "")).strip())
    except ValueError:
        return None
    return None if ip.is_unspecified else str(ip)


def _tcp_reachable(host: str, port: int, timeout: float = USB_NET_PROBE_TIMEOUT) -> bool:
    """Whether a TCP connect to ``host:port`` succeeds within ``timeout`` (then closed at once)."""
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def _open_usb(device: str, *, timeout: float, pod: str = "") -> Transport:
    """Open a USB pod: the console at ``device``, or for ``""`` (connection ``usb``) the
    auto-detected pod (``pod``, from ``usb:<name>``, picks one of several by name), reached over
    its USB network adapter when that works, else its console.

    The console's ``status`` names the pod's USB network address; the network is used when it is
    on, its link is up and the API port answers a TCP connect within
    :data:`USB_NET_PROBE_TIMEOUT`. Anything else (firmware before 3.9.0, Wi-Fi configured on the
    pod, link down, the host has not configured the interface) keeps the console.
    """
    from .serial import SerialTransport

    console = SerialTransport(device, timeout=timeout, pod=pod)
    if device:
        return console  # an explicit device path is the console, never upgraded
    try:
        console.timeout = min(timeout, _USB_STATUS_TIMEOUT)
        try:
            status = console.status()
            ip = usb_net_address(status)
        finally:
            console.timeout = timeout
    except Exception as exc:  # noqa: BLE001 - any failure keeps the console, as before
        _log.info("usb: could not read the pod's USB network address on %s (%s); using the "
                  "USB console", console.device, exc)
        return console
    from .tcp import TcpTransport

    if ip is None or not _tcp_reachable(ip, DEFAULT_PORT):
        why = ("the pod's USB network is off (Wi-Fi is configured) or down" if ip is None else
               f"the pod's USB network address {ip}:{DEFAULT_PORT} does not answer")
        # Firmware 3.9.0+ keeps only the physical-presence commands on its console, so without the
        # USB network the way in is the pod's Ethernet or Wi-Fi address when this computer reaches
        # it (the pod's external LAN policy applies there).
        lan = usb_lan_fallback_address(status)
        if lan and _tcp_reachable(lan, DEFAULT_PORT):
            tcp = TcpTransport(f"{lan}:{DEFAULT_PORT}", timeout=timeout)
            tcp.usb_lan_fallback = why
            console.close()
            _log.warning("usb: %s; using the pod's network address %s (its external LAN policy "
                         "applies there; `benchpod clear-wifi` turns USB networking back on)",
                         why, tcp.addr)
            return tcp
        _log.info("usb: %s; using the USB console %s", why, console.device)
        return console

    tcp = TcpTransport(f"{ip}:{DEFAULT_PORT}", timeout=timeout)
    tcp.usb_console = console.device
    console.close()
    _log.info("usb: using the pod's USB network at %s (console %s)", tcp.addr, console.device)
    return tcp


def __getattr__(name: str):
    # Lazy re-exports so importing this package does not pull in pyserial.
    if name == "TcpTransport":
        from .tcp import TcpTransport

        return TcpTransport
    if name == "SerialTransport":
        from .serial import SerialTransport

        return SerialTransport
    if name == "CloudTransport":
        from .cloud import CloudTransport

        return CloudTransport
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
