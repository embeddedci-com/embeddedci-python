"""A single, process-wide BenchPod connection shared across MCP tool calls.

The MCP server process is long-lived, but each tool invocation is independent.
We hold the connected :class:`~embeddedci.benchpod.BenchPod` in one module-level
:class:`Session` so an agent can ``connect`` once and then ``flash``/``capture``/…
against the same device. Tools call :meth:`Session.require` to get the live pod
(or a clear "connect first" error).
"""

from __future__ import annotations

from typing import Optional

from embeddedci.benchpod import BenchPod
from embeddedci.benchpod.errors import BenchPodError


class NotConnectedError(BenchPodError):
    """Raised when a tool needs the device but ``connect`` was never called."""


class Session:
    """Holds at most one open BenchPod connection."""

    def __init__(self) -> None:
        self._pod: Optional[BenchPod] = None
        #: Default connection used by ``connect`` when called with no argument
        #: (set from the ``--connection`` CLI flag). ``None`` falls back to the
        #: ``BENCHPOD_CONNECTION`` environment variable.
        self.default_connection: Optional[str] = None
        self.timeout: float = 30.0

    @property
    def connected(self) -> bool:
        return self._pod is not None

    def connect(self, connection: Optional[str] = None,
                *, timeout: Optional[float] = None) -> BenchPod:
        """Open (or re-open) the device, closing any prior connection first."""
        self.disconnect()
        conn = connection or self.default_connection
        self._pod = BenchPod(
            conn, timeout=self.timeout if timeout is None else timeout
        )
        return self._pod

    def require(self) -> BenchPod:
        """Return the live pod, or raise if not connected."""
        if self._pod is None:
            raise NotConnectedError(
                "not connected to a BenchPod — call the `connect` tool first"
            )
        return self._pod

    def disconnect(self) -> None:
        """Close the device if one is open (idempotent)."""
        if self._pod is not None:
            try:
                self._pod.close()
            except Exception:
                pass
            self._pod = None


#: The one shared session for this server process.
SESSION = Session()
