"""Capture a DUT's UART output through the pod's UART proxy.

After ``transport.uart_proxy_start(...)`` the returned :class:`RawLink` is a raw
8N1 byte stream of the DUT's UART. :func:`capture` reads it for a bounded time
(or until a line matches), decodes to text, and always leaves the proxy on the
way out.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Pattern, Union

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
