"""Transport backends for the BenchPod client.

:class:`TcpTransport` talks to a pod over the network; :class:`SerialTransport`
over its USB serial console. Use :func:`open_transport` to build the right one
from a resolved :class:`~embeddedci.benchpod.connection.ConnSpec`.
"""

from __future__ import annotations

from ..connection import ConnSpec
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
) -> Transport:
    """Construct a transport for ``spec``. Imports backends lazily.

    ``api_base``/``token``/``audience`` apply only to the cloud (``embeddedci``) destination.
    """
    if spec.is_wifi():
        from .tcp import TcpTransport

        return TcpTransport(spec.addr, timeout=timeout)
    if spec.is_serial():
        from .serial import SerialTransport

        return SerialTransport(spec.device, timeout=timeout)
    if spec.is_cloud():
        from ..cloud_auth import DEFAULT_API_BASE, DEFAULT_AUDIENCE
        from .cloud import CloudTransport

        return CloudTransport(
            spec.device_name,
            api_base=api_base or DEFAULT_API_BASE,
            token=token,
            audience=audience or DEFAULT_AUDIENCE,
            timeout=timeout,
        )
    raise ConnectionConfigError(f"unknown connection kind {spec.kind!r}")


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
