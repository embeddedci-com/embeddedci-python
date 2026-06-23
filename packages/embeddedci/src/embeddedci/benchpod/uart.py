"""Capture a DUT's UART output through the pod's UART proxy.

After ``transport.uart_proxy_start(...)`` the returned :class:`RawLink` is a raw
8N1 byte stream of the DUT's UART. :func:`capture` reads it for a bounded time
(or until a line matches), decodes to text, and always leaves the proxy on the
way out.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Pattern, Union

from .errors import UartTimeout
from .transport.base import RawLink

# An ``until`` condition: a substring, a compiled regex, or a predicate on the
# accumulated text-so-far.
Until = Union[str, "Pattern[str]", Callable[[str], bool]]


@dataclass
class UartCapture:
    """Decoded UART output collected during a capture window."""

    text: str
    lines: List[str] = field(default_factory=list)
    matched: bool = False  # True if an ``until`` condition was satisfied

    def contains(self, needle: str) -> bool:
        return needle in self.text

    def match(self, pattern: Union[str, "Pattern[str]"]) -> bool:
        rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        return rx.search(self.text) is not None

    def __contains__(self, needle: str) -> bool:
        return needle in self.text


def _search(text: str, pattern: Until):
    """Return a truthy match for ``pattern`` in ``text``, or None.

    A ``str`` is a substring (parity with capture's ``until``); a compiled regex
    returns its :class:`re.Match`; a predicate returns True/None. The non-None
    result is what ``read_until``/``expect`` hand back.
    """
    if callable(pattern) and not hasattr(pattern, "search"):
        return True if pattern(text) else None
    if hasattr(pattern, "search"):  # compiled regex
        return pattern.search(text)  # type: ignore[union-attr]
    needle = str(pattern)
    return needle if needle in text else None


class UartSession:
    """Event-based view of the DUT's UART: a background thread drains the proxy
    link into a buffer so you can start listening *before* an action (e.g. a
    non-delayed eFuse power-on) and read the result afterwards.

        with bp.open_uart(rx=5, tx=4, baud=115200) as uart:
            bp.power_on(bp.INTERNAL)              # immediate — no pod-side delay
            uart.expect("APP_OK", timeout=6)      # banner buffered since power-up

    The reader thread parks on a blocking ``link.read`` when the DUT is quiet, so
    it costs nothing while idle. Construction starts the thread; ``close`` (or the
    context-manager exit) stops it and leaves the proxy. See
    ``docs/event-uart-design.md``.
    """

    def __init__(self, link: RawLink, *, max_buffer: int = 1 << 20,
                 chunk: int = 256) -> None:
        self._link = link
        self._max_buffer = max_buffer
        self._chunk = chunk
        self._buf = bytearray()
        self._consumed = 0          # offset for drain()
        self._closed = False        # link reached EOF / was closed
        self._stop = False
        self.overflowed = False     # buffer hit max_buffer and dropped oldest bytes
        self._cond = threading.Condition(threading.Lock())
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # -- background reader --------------------------------------------------
    def _read_loop(self) -> None:
        while not self._stop:
            data = self._link.read(self._chunk)
            if not data:
                with self._cond:
                    self._closed = True
                    self._cond.notify_all()
                return
            with self._cond:
                self._buf.extend(data)
                if len(self._buf) > self._max_buffer:
                    drop = len(self._buf) - self._max_buffer
                    del self._buf[:drop]
                    self._consumed = max(0, self._consumed - drop)
                    self.overflowed = True
                self._cond.notify_all()

    def _text_locked(self) -> str:
        return self._buf.decode("utf-8", errors="replace")

    # -- reading ------------------------------------------------------------
    def read_until(self, pattern: Until, *, timeout: float):
        """Block until ``pattern`` (substring / compiled regex / ``text->bool``
        predicate) appears in the accumulated text, or ``timeout`` elapses.

        Returns the match (the substring, the :class:`re.Match`, or True) — a
        truthy value — or ``None`` on timeout. Does not consume: :attr:`text`
        keeps growing.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                m = _search(self._text_locked(), pattern)
                if m is not None:
                    return m
                if self._closed:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def expect(self, pattern: Until, *, timeout: float):
        """Like :meth:`read_until` but raises :class:`UartTimeout` (carrying the
        text so far) instead of returning ``None``."""
        m = self.read_until(pattern, timeout=timeout)
        if m is None:
            raise UartTimeout(
                f"timed out after {timeout:g}s waiting for {pattern!r}",
                text=self.text,
            )
        return m

    def read(self, *, timeout: float = 0.0) -> str:
        """Return all text received so far. ``timeout > 0`` first waits up to that
        long for at least one new byte; ``timeout == 0`` is non-blocking."""
        with self._cond:
            if timeout > 0:
                start = len(self._buf)
                deadline = time.monotonic() + timeout
                while len(self._buf) == start and not self._closed:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(remaining)
            return self._text_locked()

    def drain(self) -> str:
        """Return everything received since the last ``drain`` and advance the
        cursor, so a subsequent ``read_until`` only sees new data."""
        with self._cond:
            out = self._buf[self._consumed:].decode("utf-8", errors="replace")
            self._consumed = len(self._buf)
            return out

    @property
    def text(self) -> str:
        """Everything decoded so far."""
        with self._cond:
            return self._text_locked()

    @property
    def lines(self) -> List[str]:
        text = self.text.replace("\r\n", "\n").replace("\r", "\n")
        out = text.split("\n")
        if out and out[-1] == "":
            out.pop()
        return out

    @property
    def closed(self) -> bool:
        """True once the proxy link has ended (EOF or :meth:`close`)."""
        with self._cond:
            return self._closed

    # -- writing (the proxy is bidirectional) -------------------------------
    def write(self, data: Union[bytes, str]) -> None:
        """Send bytes to the DUT's RX (e.g. a console command)."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._link.write(data)

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        """Stop the reader and leave the proxy (returns the pod to a safe state)."""
        self._stop = True
        try:
            self._link.close()  # unblocks the reader's in-flight read()
        except Exception:
            pass
        if self._reader.is_alive() and self._reader is not threading.current_thread():
            self._reader.join(timeout=2)

    def __enter__(self) -> "UartSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _make_predicate(until: Optional[Until]) -> Optional[Callable[[str], bool]]:
    if until is None:
        return None
    if callable(until) and not hasattr(until, "search"):
        return until  # plain predicate
    if hasattr(until, "search"):  # compiled regex
        return lambda text: until.search(text) is not None  # type: ignore[union-attr]
    needle = str(until)
    return lambda text: needle in text


def capture(link: RawLink, *, duration: float, until: Optional[Until] = None,
            chunk: int = 256) -> UartCapture:
    """Read raw bytes from ``link`` until ``duration`` elapses or ``until`` hits.

    ``until`` may be a substring, a compiled regex, or a ``text -> bool``
    predicate evaluated against everything received so far. The ``link`` is
    always closed (leaving the proxy) before returning.

    ``read`` on the link blocks until data arrives or the link is closed, so the
    deadline is enforced by a timer that closes the link — that unblocks an
    in-flight read on a quiet DUT and ends the capture.
    """
    predicate = _make_predicate(until)
    buf = bytearray()
    matched = False
    timer = threading.Timer(duration, link.close)
    timer.start()
    try:
        while True:
            data = link.read(chunk)
            if not data:
                break  # link closed (deadline reached) or EOF
            buf.extend(data)
            if predicate is not None and predicate(
                buf.decode("utf-8", errors="replace")
            ):
                matched = True
                break
    finally:
        timer.cancel()
        link.close()

    text = buf.decode("utf-8", errors="replace")
    lines = [ln for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    # Drop a trailing empty line from a final newline, but keep interior blanks.
    if lines and lines[-1] == "":
        lines.pop()
    return UartCapture(text=text, lines=lines, matched=matched)
