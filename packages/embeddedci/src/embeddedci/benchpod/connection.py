"""Resolve a connection string into a concrete transport spec.

Mirrors the Go CLI's ``connection.go``:

* ``host`` or ``host:port``        -> TCP/wifi (default port 8080)
* ``/dev/tty*`` / ``COM3`` / ``\\\\.\\COM3`` -> serial device path
* ``serial`` / ``usb``             -> serial, auto-detect by USB VID 0x2E8A

Precedence is handled by the caller: an explicit argument wins over the
``BENCHPOD_CONNECTION`` environment variable.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .errors import ConnectionConfigError

DEFAULT_PORT = 8080
ENV_VAR = "BENCHPOD_CONNECTION"

_COM_RE = re.compile(r"^COM[0-9]+$", re.IGNORECASE)


@dataclass
class ConnSpec:
    """A resolved connection target."""

    kind: str  # "tcp" or "serial"
    addr: str = ""  # "host:port" when kind == "tcp"
    device: str = ""  # device path when kind == "serial"; "" means auto-detect

    def is_wifi(self) -> bool:
        return self.kind == "tcp"

    def is_serial(self) -> bool:
        return self.kind == "serial"


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
    if s.lower() in ("serial", "usb"):
        return ConnSpec(kind="serial", device="")
    if _is_device_path(s):
        return ConnSpec(kind="serial", device=s)
    return ConnSpec(kind="tcp", addr=_normalize_addr(s))


def resolve_connection(connection: "str | None" = None) -> ConnSpec:
    """Resolve a connection from an explicit value or the environment."""
    raw = connection if connection is not None else os.environ.get(ENV_VAR)
    if not raw or not str(raw).strip():
        raise ConnectionConfigError(
            "no BenchPod connection configured; pass connection=... or set "
            f"the {ENV_VAR} environment variable "
            "(e.g. '192.168.1.213', '/dev/ttyACM0', or 'serial')"
        )
    return parse_connection(str(raw))
