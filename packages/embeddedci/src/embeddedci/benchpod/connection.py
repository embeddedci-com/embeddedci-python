"""Resolve a connection string into a concrete transport spec.

Mirrors the Go CLI's ``connection.go``, plus a cloud destination:

* ``host`` or ``host:port``        -> TCP/wifi (default port 8080)
* ``/dev/tty*`` / ``COM3`` / ``\\\\.\\COM3`` -> serial device path
* ``usb``                          -> USB pod, auto-detected by probing the ports
                                      (``serial`` is accepted as a legacy spelling); on
                                      firmware 3.9.0+ the transport is the pod's USB network
                                      adapter when it answers, else the console (see
                                      :func:`~embeddedci.benchpod.transport.open_transport`)
* ``usb:<name>``                   -> the USB pod with that name, when several are plugged in
                                      (``usb:baea06``, ``usb:benchpod-baea06``)
* ``discover`` / ``mdns`` / ``auto`` -> find a single pod on the LAN via mDNS
* ``embeddedci:<device-name>``     -> drive a named device through embeddedci.com (an API key
                                      anywhere, or GitHub Actions OIDC)
* ``saved``                        -> the default connection ``benchpod-cli`` saved
                                      (``benchpod set-connection``, ``benchpod discover --save``)

:func:`resolve_connection` applies the precedence: an explicit argument, then the
``BENCHPOD_CONNECTION`` environment variable, then the connection ``benchpod-cli`` saved in
``~/.config/benchpod-cli/config.json``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .cli_config import SAVED_KEYWORD, config_path, saved_connection
from .errors import ConnectionConfigError

DEFAULT_PORT = 8080
ENV_VAR = "BENCHPOD_CONNECTION"
CLOUD_PREFIX = "embeddedci:"
DISCOVER_KEYWORDS = ("discover", "mdns", "auto")
USB_PREFIX = "usb:"

_COM_RE = re.compile(r"^COM[0-9]+$", re.IGNORECASE)


@dataclass
class ConnSpec:
    """A resolved connection target."""

    kind: str  # "tcp", "serial" or "embeddedci"
    addr: str = ""  # "host:port" when kind == "tcp"
    device: str = ""  # device path when kind == "serial"; "" means auto-detect
    device_name: str = ""  # named device when kind == "embeddedci"
    pod: str = ""  # usb:<name>: the pod's identity short id ("baea06") among several on USB

    def is_wifi(self) -> bool:
        return self.kind == "tcp"

    def is_serial(self) -> bool:
        return self.kind == "serial"

    def is_cloud(self) -> bool:
        return self.kind == "embeddedci"


def normalize_pod_name(name: str) -> str:
    """Turn the ways a pod is named into its identity short id: ``"baea06"``,
    ``"benchpod-baea06"`` (its embeddedci.com and mDNS host name), ``"BenchPod baea06"`` (its mDNS
    instance), any case, with an optional ``.local`` suffix, all give ``"baea06"``."""
    s = name.strip().lower()
    for prefix in ("benchpod-", "benchpod "):
        if s.startswith(prefix):
            s = s[len(prefix):]
    if s.endswith(".local"):
        s = s[: -len(".local")]
    return s.strip()


def _is_device_path(s: str) -> bool:
    if s.startswith("/dev/"):
        return True
    if s.startswith("\\\\.\\"):  # Windows \\.\COM10 form
        return True
    if _COM_RE.match(s):
        return True
    return False


def _normalize_addr(s: str) -> str:
    # Bracketed IPv6 literal, optionally with a port.
    if s.startswith("["):
        return s if "]:" in s else f"{s}:{DEFAULT_PORT}"
    # A single colon means host:port; more than one and no brackets means a bare
    # IPv6 address, which needs the default port appended as-is.
    if s.count(":") == 1:
        return s
    return f"{s}:{DEFAULT_PORT}"


def parse_connection(raw: str) -> ConnSpec:
    """Parse a connection string into a :class:`ConnSpec`."""
    s = raw.strip()
    if not s:
        raise ConnectionConfigError("connection string is empty")
    if s.lower().startswith(CLOUD_PREFIX):
        name = s[len(CLOUD_PREFIX):].strip()
        if not name:
            raise ConnectionConfigError(
                "embeddedci destination requires a device name, e.g. 'embeddedci:my-bench'"
            )
        return ConnSpec(kind="embeddedci", device_name=name)
    # "usb" is the documented spelling; "serial" is the older one, still accepted
    # so connection strings written before the rename keep working.
    if s.lower() == SAVED_KEYWORD:
        saved = saved_connection()
        if not saved:
            raise ConnectionConfigError(
                f"connection 'saved' asks for benchpod-cli's saved connection, but {config_path()} "
                "holds none; save one with `benchpod set-connection <address|device|usb>`"
            )
        return parse_connection(saved)
    if s.lower() in ("usb", "serial"):
        return ConnSpec(kind="serial", device="")
    if s.lower().startswith(USB_PREFIX):
        pod = normalize_pod_name(s[len(USB_PREFIX):])
        if not pod:
            raise ConnectionConfigError(
                f"connection {s!r} needs the pod's name, e.g. usb:benchpod-a1b2c3"
            )
        return ConnSpec(kind="serial", device="", pod=pod)
    if s.lower() in DISCOVER_KEYWORDS:
        return _discover_one()
    if _is_device_path(s):
        return ConnSpec(kind="serial", device=s)
    return ConnSpec(kind="tcp", addr=_normalize_addr(s))


def _discover_one() -> ConnSpec:
    """Resolve a single LAN pod via mDNS, or raise a helpful error.

    Zero pods -> nothing answered; more than one -> ambiguous, so list them and
    ask the caller to pick (by address or ``<hostname>.local``).
    """
    from .discovery import discover

    pods = discover()
    if not pods:
        raise ConnectionConfigError(
            "no BenchPod found on the local network via mDNS — check it is "
            "powered and on this subnet, or pass an explicit address"
        )
    if len(pods) > 1:
        listing = "\n".join(
            f"  - {p.name}  {p.addr}  id={p.pod_id[:12]}…" for p in pods
        )
        raise ConnectionConfigError(
            f"{len(pods)} BenchPods found — disambiguate with an address or "
            f"hostname (e.g. {pods[0].hostname}):\n{listing}"
        )
    return ConnSpec(kind="tcp", addr=_normalize_addr(pods[0].addr))


def resolve_connection(connection: "str | None" = None) -> ConnSpec:
    """Resolve a connection: the ``connection`` argument, else ``BENCHPOD_CONNECTION``, else the
    default connection ``benchpod-cli`` saved (see :mod:`~embeddedci.benchpod.cli_config`)."""
    raw = connection if connection is not None else os.environ.get(ENV_VAR)
    if not raw or not str(raw).strip():
        raw = saved_connection()
    if not raw or not str(raw).strip():
        raise ConnectionConfigError(
            "no BenchPod connection configured; pass connection=..., set "
            f"the {ENV_VAR} environment variable "
            "(e.g. '192.168.1.213', '/dev/ttyACM0', 'usb', or 'embeddedci:<device-name>'), "
            "or save a default with `benchpod set-connection <address|device|usb>`"
        )
    return parse_connection(str(raw))
