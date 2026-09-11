"""The process-wide BenchPod connection and the state MCP tool calls share.

The MCP server process is long-lived, but each tool invocation is independent. :class:`Session`
holds the connected :class:`~embeddedci.benchpod.BenchPod` plus what lives across calls: an open
UART session, an open CAN bus, and the last ADC/LA captures (so decoding or saving does not need
a second capture).

``lock`` serialises device access: tools run on worker threads, and two tool calls must never
interleave commands on the pod.

Over the cloud the device is shared and ``connect`` holds an exclusive lease. An agent chat can sit
idle for hours, so after ``idle_timeout`` seconds without a tool call the session closes the
connection (releasing the lease) and transparently reconnects on the next call.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional, Tuple

from embeddedci.benchpod import BenchPod, CanBus, Capture, LaCapture, UartSession
from embeddedci.benchpod.connection import CLOUD_PREFIX, DISCOVER_KEYWORDS, ENV_VAR, _is_device_path
from embeddedci.benchpod.errors import BenchPodError, ConnectionConfigError

from . import models as m


class NotConnectedError(BenchPodError):
    """A tool needs the device but ``connect`` was never called."""


class SessionStateError(BenchPodError):
    """A tool needs a session (UART, CAN, a previous capture) that is not open."""


def connection_kind(connection: str) -> str:
    """Classify a connection string without resolving it (no mDNS lookup)."""
    s = connection.strip()
    if s.lower().startswith(CLOUD_PREFIX):
        return "embeddedci"
    if s.lower() in ("usb", "serial") or _is_device_path(s):
        return "serial"
    if s.lower() in DISCOVER_KEYWORDS:
        return "discover"
    return "tcp"


class Session:
    """At most one open BenchPod connection, plus the sessions opened on it."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._pod: Optional[BenchPod] = None
        #: Connection used by ``connect`` when called without one (``--connection``); falls back to
        #: ``BENCHPOD_CONNECTION``.
        self.default_connection: Optional[str] = None
        #: LA voltage applied on connect when the tool call does not pass one (``--la-voltage``);
        #: falls back to ``BENCHPOD_LA_VOLTAGE`` inside the SDK.
        self.default_la_voltage: Optional[float] = None
        self.timeout: float = 30.0
        self.lease_wait: float = 30.0
        #: Seconds without a tool call before a leased (cloud) connection is released; 0 = never.
        self.idle_timeout: float = 600.0
        self.connection: Optional[str] = None
        self.kind: Optional[str] = None
        self._reconnect: Optional[Tuple[str, Optional[float], float]] = None
        self._idle_closed = False
        self._last_used = time.monotonic()
        self._idle_timer: Optional[threading.Timer] = None
        self.uart: Optional[UartSession] = None
        self.uart_port: Optional[Tuple[int, int, int]] = None
        self.can: Optional[CanBus] = None
        self.can_config: Optional[Tuple[int, str, bool]] = None
        self.last_adc: Optional[Capture] = None
        self.last_la: Optional[LaCapture] = None

    # -- connection -----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._pod is not None

    @property
    def reconnectable(self) -> bool:
        """The connection was released for inactivity and will reopen on the next call."""
        return self._pod is None and self._idle_closed and self._reconnect is not None

    def _open(self, connection: str, la_voltage: Optional[float], lease_wait: float) -> BenchPod:
        return BenchPod(connection, la_voltage=la_voltage, timeout=self.timeout, lease_wait=lease_wait)

    def connect(self, connection: Optional[str] = None, *, la_voltage: Optional[float] = None,
                lease_wait: Optional[float] = None) -> BenchPod:
        """Open (or re-open) the device, closing any prior connection and sessions first."""
        with self.lock:
            self.disconnect()
            conn = connection or self.default_connection or os.environ.get(ENV_VAR)
            if not conn:
                raise ConnectionConfigError(
                    "no connection given: pass one (host[:port], /dev/tty…, 'usb' or "
                    f"'embeddedci:<device>'), start the server with --connection, or set {ENV_VAR}"
                )
            la = self.default_la_voltage if la_voltage is None else la_voltage
            wait = self.lease_wait if lease_wait is None else lease_wait
            self._pod = self._open(conn, la, wait)
            self.connection, self.kind = conn, connection_kind(conn)
            self._reconnect = (conn, la, wait)
            self._touch()
            return self._pod

    def require(self) -> BenchPod:
        """Return the live pod (reconnecting after an idle release), or raise if never connected."""
        with self.lock:
            if self._pod is None:
                if not self.reconnectable:
                    raise NotConnectedError("not connected to a BenchPod — call the `connect` tool first")
                conn, la, wait = self._reconnect  # type: ignore[misc]
                self._pod = self._open(conn, la, wait)
                self._idle_closed = False
            self._touch()
            return self._pod

    def disconnect(self) -> None:
        """Close sessions and the device (idempotent) and forget captures."""
        with self.lock:
            self._cancel_idle_timer()
            self._close_device()
            self._reconnect = None
            self._idle_closed = False
            self.connection = self.kind = None
            self.last_adc = self.last_la = None

    def _close_device(self) -> None:
        self.close_uart()
        self.close_can()
        if self._pod is not None:
            try:
                self._pod.close()
            except Exception:
                pass
            self._pod = None

    # -- idle release -----------------------------------------------------------

    def _touch(self) -> None:
        self._last_used = time.monotonic()
        if (self._idle_timer is None and self.idle_timeout > 0 and self._pod is not None
                and self._pod.leased):
            self._schedule_idle_check(self.idle_timeout)

    def _schedule_idle_check(self, delay: float) -> None:
        timer = threading.Timer(delay, self._check_idle)
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _check_idle(self) -> None:
        if not self.lock.acquire(blocking=False):  # a tool is running: that counts as activity
            self._schedule_idle_check(min(5.0, self.idle_timeout))
            return
        try:
            self._idle_timer = None
            if self._pod is None:
                return
            idle = time.monotonic() - self._last_used
            if idle < self.idle_timeout:
                self._schedule_idle_check(self.idle_timeout - idle)
                return
            self._close_device()
            self._idle_closed = True
        finally:
            self.lock.release()

    # -- UART / CAN sessions -------------------------------------------------------

    def require_uart(self) -> UartSession:
        if self.uart is None:
            raise SessionStateError("no UART session is open — call uart_open first")
        return self.uart

    def close_uart(self) -> None:
        if self.uart is not None:
            try:
                self.uart.close()
            except Exception:
                pass
        self.uart = None
        self.uart_port = None

    def require_can(self) -> CanBus:
        if self.can is None:
            raise SessionStateError("CAN is not open — call can_open first")
        return self.can

    def close_can(self) -> None:
        if self.can is not None:
            try:
                self.can.close()
            except Exception:
                pass
        self.can = None
        self.can_config = None

    def info(self) -> m.SessionInfo:
        leased = self._pod is not None and self._pod.leased
        return m.SessionInfo(
            uart_open=self.uart is not None, can_open=self.can is not None,
            last_adc_capture_samples=len(self.last_adc) if self.last_adc is not None else None,
            last_la_capture_samples=len(self.last_la) if self.last_la is not None else None,
            idle_disconnect_after=self.idle_timeout if (leased and self.idle_timeout > 0) else None,
        )


#: The one shared session for this server process.
SESSION = Session()
