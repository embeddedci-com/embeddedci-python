"""Capture a DUT's UART output through the pod's UART proxy.

After ``transport.uart_proxy_start(...)`` the returned :class:`RawLink` is a raw
8N1 byte stream of the DUT's UART. :func:`capture` reads it for a bounded time
(or until a line matches), decodes to text, and always leaves the proxy on the
way out.
"""

from __future__ import annotations

import codecs
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


def _find(text: str, pattern: Until) -> Optional[tuple]:
    """``(match, end)`` for ``pattern`` in ``text``, or None.

    A ``str`` is a substring (parity with capture's ``until``) and matches as itself; a compiled
    regex matches as its :class:`re.Match`; a predicate on the whole text matches as ``True`` and
    ends at the end of the text. ``end`` is the offset just past the match.
    """
    if callable(pattern) and not hasattr(pattern, "search"):
        return (True, len(text)) if pattern(text) else None
    if hasattr(pattern, "search"):  # compiled regex
        m = pattern.search(text)  # type: ignore[union-attr]
        return (m, m.end()) if m else None
    needle = str(pattern)
    i = text.find(needle)
    return (needle, i + len(needle)) if i >= 0 else None


class UartSession:
    """Event-based view of the DUT's UART: a background thread drains the proxy
    link into a buffer so you can start listening *before* an action (e.g. a
    non-delayed eFuse power-on) and read the result afterwards.

        with bp.open_uart(rx=5, tx=4, baud=115200) as uart:
            bp.power_on(bp.INTERNAL)              # immediate — no pod-side delay
            uart.expect("APP_OK", timeout=6)      # banner buffered since power-up

    Reading works like a console: :meth:`read` returns the output since the last read,
    and :meth:`read_until` / :meth:`expect` wait for a pattern in that unread output and
    mark it read up to the end of the match — so the next call only sees newer output.
    :attr:`text` and :attr:`lines` keep everything received.

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
        # Decoded incrementally, so a multi-byte character split across reads survives and the
        # read cursor is a character offset (invalid bytes become U+FFFD).
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._parts: List[str] = []  # received text, joined lazily
        self._size = 0
        self._consumed = 0          # read cursor: text before it has been returned
        self._closed = False        # link reached EOF / was closed
        self._stop = False
        self.overflowed = False     # more than max_buffer characters arrived; the oldest were dropped
        self._cond = threading.Condition(threading.Lock())
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # -- background reader --------------------------------------------------
    def _read_loop(self) -> None:
        while not self._stop:
            data = self._link.read(self._chunk)
            with self._cond:
                self._append_locked(self._decoder.decode(data, final=not data))
                if not data:
                    self._closed = True
                self._cond.notify_all()
            if not data:
                return

    def _append_locked(self, text: str) -> None:
        if not text:
            return
        self._parts.append(text)
        self._size += len(text)
        if self._size > self._max_buffer:
            buf = self._text_locked()
            drop = len(buf) - self._max_buffer
            self._parts = [buf[drop:]]
            self._size = self._max_buffer
            self._consumed = max(0, self._consumed - drop)
            self.overflowed = True

    def _text_locked(self) -> str:
        if len(self._parts) > 1:
            self._parts = ["".join(self._parts)]
        return self._parts[0] if self._parts else ""

    def _wait_for_locked(self, pattern: Until, timeout: float) -> Optional[tuple]:
        """Wait for ``pattern`` in the unread text: ``(match, end)`` relative to the cursor, or None."""
        deadline = time.monotonic() + timeout
        while True:
            found = _find(self._text_locked()[self._consumed:], pattern)
            if found is not None or self._closed:
                return found
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._cond.wait(remaining)

    # -- reading ------------------------------------------------------------
    def read_until(self, pattern: Until, *, timeout: float) -> Optional[str]:
        """Wait until ``pattern`` (a substring, compiled regex, or ``text -> bool`` predicate)
        appears in the unread output; return that output up to and including the match, and
        mark it read.

        Returns ``None`` — leaving the output unread — on timeout or when the link ends first.
        """
        with self._cond:
            found = self._wait_for_locked(pattern, timeout)
            if found is None:
                return None
            start = self._consumed
            self._consumed += found[1]
            return self._text_locked()[start:self._consumed]

    def expect(self, pattern: Until, *, timeout: float):
        """Wait for ``pattern`` like :meth:`read_until` (marking the output read up to the end of
        the match), but return the match itself — the substring, the :class:`re.Match` (use its
        groups), or ``True`` for a predicate — and raise :class:`UartTimeout` (carrying everything
        received) instead of returning ``None``."""
        with self._cond:
            found = self._wait_for_locked(pattern, timeout)
            if found is not None:
                self._consumed += found[1]
                return found[0]
            text = self._text_locked()
        raise UartTimeout(f"timed out after {timeout:g}s waiting for {pattern!r}", text=text)

    def read(self, *, timeout: float = 0.0) -> str:
        """Return the output received since the last read and mark it read.

        When nothing is unread, ``timeout > 0`` first waits up to that long for new output;
        ``timeout == 0`` never blocks. Everything received stays available as :attr:`text`.
        """
        with self._cond:
            if timeout > 0:
                deadline = time.monotonic() + timeout
                while len(self._text_locked()) == self._consumed and not self._closed:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(remaining)
            text = self._text_locked()
            out = text[self._consumed:]
            self._consumed = len(text)
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
