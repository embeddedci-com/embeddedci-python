"""Transport abstraction shared by all BenchPod backends.

A :class:`Transport` exposes the high-level operations every BenchPod backend
must provide. The TCP transport additionally offers raw ``command``/``samples``
JSON access (the serial console speaks text commands, not JSON, so those extras
are TCP-only). :class:`RawLink` is the bidirectional byte stream that
``dap_start`` hands to the flash bridge.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class RawLink(Protocol):
    """A raw, bidirectional byte stream (length-framed CMSIS-DAP during a flash).

    ``read`` blocks until at least one byte is available and returns ``b""``
    only when the stream has ended (EOF) or been closed. ``close`` returns the
    pod to a safe state (TCP: closes the socket; serial: sends the ``Q`` quit
    byte) and unblocks any in-flight ``read``.
    """

    def read(self, n: int) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def close(self) -> None: ...


class Transport(ABC):
    """Backend that can talk to a BenchPod."""

    @abstractmethod
    def status(self) -> Any:
        """Return firmware/connection status (dict over TCP, text over serial)."""

    @abstractmethod
    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        """Enable (``on=True``) or disable a target-power eFuse (1 or 2).

        ``delay_ms`` > 0 schedules the change to fire later (pod-side), so the
        connection can move on to e.g. UART capture meanwhile.
        """

    def dap_start(self, swclk: int, swdio: int, nreset: Optional[int]) -> RawLink:
        """Arm the SWD engine and return a raw link carrying length-framed
        CMSIS-DAP packets — the batched flash path that OpenOCD's cmsis-dap TCP
        backend drives (see :mod:`embeddedci.benchpod.flash`). This is the only
        SWD flash path; not every backend implements it."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support CMSIS-DAP (dap_start)"
        )

    def set_la_voltage(self, mv: int) -> Any:
        """Select the LA I/O-bank voltage via the pod's TPS2116 mux; ``mv`` is
        1800 or 3300. Must be set before any LA-bank op (flash/SWD, UART proxy,
        LA capture, pull-ups, I2C-sensor). Returns the pod's ``{"mv","st"}`` state.
        Not every backend implements it."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support set_la_voltage"
        )

    def get_la_voltage(self) -> Any:
        """Report the current LA I/O-bank voltage state (``{"mv","st"}``); ``mv``
        is 0 when not yet set. Not every backend implements it."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support get_la_voltage"
        )

    @abstractmethod
    def uart_proxy_start(self, rx: int, tx: int, baud: int) -> RawLink:
        """Enter transparent UART-proxy mode and return the raw byte link.

        ``rx``/``tx`` are LA channels: ``rx`` is sampled (wire the DUT's TX
        here), ``tx`` is driven (wire the DUT's RX here). The returned link
        carries raw 8N1 bytes; ``close()`` leaves the proxy.
        """

    @abstractmethod
    def close(self) -> None:
        """Release any held resources (sockets, serial port)."""

    def ping(self) -> Any:
        """Lightweight reachability check. Defaults to :meth:`status`."""
        return self.status()

    def __enter__(self) -> "Transport":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
