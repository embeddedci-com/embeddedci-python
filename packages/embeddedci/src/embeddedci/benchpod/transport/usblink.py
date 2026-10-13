"""This computer's ends of BenchPod USB network links (firmware 3.9.0+), found without the console.

The pod's USB network adapter puts this computer on a ``/30`` in ``10.237.0.0/16``: the pod is the
first host, this computer the second. Reading the local interface addresses gives the pod's
address with no access to its console device, which a Linux user outside the ``dialout`` group
cannot open. The addresses come from ``getifaddrs(3)`` (Linux, macOS, BSD); elsewhere, such as
Windows, :func:`local_usb_links` returns nothing and the caller uses the console.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.util
import ipaddress
import json
import socket
import sys
from typing import List, Optional, Tuple

_USB_NET = ipaddress.ip_network("10.237.0.0/16")
_MASK_30 = ipaddress.ip_address("255.255.255.252")


class _IfAddrs(ctypes.Structure):
    pass


_IfAddrs._fields_ = [
    ("ifa_next", ctypes.POINTER(_IfAddrs)),
    ("ifa_name", ctypes.c_char_p),
    ("ifa_flags", ctypes.c_uint),
    ("ifa_addr", ctypes.c_void_p),
    ("ifa_netmask", ctypes.c_void_p),
]


def _ipv4(sa: Optional[int]) -> Optional[ipaddress.IPv4Address]:
    """The IPv4 address in a ``struct sockaddr`` (``None`` for another family)."""
    if not sa:
        return None
    raw = ctypes.string_at(sa, 8)
    # BSD and macOS: sa_len (1 byte) then sa_family (1 byte); Linux: sa_family (2 bytes, native).
    family = raw[1] if sys.platform == "darwin" or "bsd" in sys.platform else int.from_bytes(raw[:2], sys.byteorder)
    if family != socket.AF_INET:
        return None
    return ipaddress.IPv4Address(raw[4:8])


def local_usb_links() -> List[Tuple[str, str]]:
    """``(this computer's address, the pod's address)`` for each local USB network link: an IPv4
    interface address in ``10.237.0.0/16`` with a ``/30`` mask."""
    name = ctypes.util.find_library("c")
    if sys.platform.startswith("win") or not name:
        return []
    try:
        libc = ctypes.CDLL(name)
        head = ctypes.POINTER(_IfAddrs)()
        if libc.getifaddrs(ctypes.byref(head)) != 0:
            return []
    except (OSError, AttributeError):
        return []
    out: List[Tuple[str, str]] = []
    try:
        node = head
        while node:
            ifa = node.contents
            ip, mask = _ipv4(ifa.ifa_addr), _ipv4(ifa.ifa_netmask)
            if ip is not None and mask == _MASK_30 and ip in _USB_NET:
                pod = ipaddress.IPv4Address((int(ip) & ~3) + 1)
                if (str(ip), str(pod)) not in out:
                    out.append((str(ip), str(pod)))
            node = ifa.ifa_next
    finally:
        libc.freeifaddrs(head)
    return out


def _short_id(host: str, port: int, timeout: float) -> str:
    """The pod's name (``baea06``): the first 3 bytes of its identity public key, read over the
    network (``identity_public``); ``""`` when it does not answer."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b'{"cmd":"identity_public"}\n')
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(1024)
                if not chunk:
                    return ""
                buf += chunk
        pub = (json.loads(buf.split(b"\n", 1)[0]).get("data") or {}).get("public", "")
        key = base64.urlsafe_b64decode(pub + "=" * (-len(pub) % 4))
        return key[:3].hex() if len(key) >= 3 else ""
    except (OSError, ValueError, AttributeError):
        return ""


def pick_usb_pod(links: List[Tuple[str, str]], pod: str, port: int, timeout: float) -> Optional[str]:
    """The pod's address among ``links``: the only one, or for ``pod`` (a normalized name,
    ``baea06``) the one whose identity matches. ``None`` when there is none, or several and no
    name."""
    if not pod:
        return links[0][1] if len(links) == 1 else None
    for _, addr in links:
        if _short_id(addr, port, timeout) == pod:
            return addr
    return None
