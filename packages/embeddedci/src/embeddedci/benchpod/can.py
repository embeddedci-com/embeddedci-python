"""Classic CAN over the pod's FDCAN1 / TCAN1044 transceiver.

The pod is a single CAN node, so ``normal`` mode only works against a second
node on the bus. For self-contained tests on one pod, configure a **loopback**
mode — the node ACKs its own frames, so every :meth:`CanBus.write` comes back on
:meth:`CanBus.read`:

* ``internal`` — looped inside the FDCAN core; the transceiver is not involved
  (works with nothing wired to CAN+/CAN-). Validates the firmware path.
* ``external`` — TX drives the real TCAN1044 pins and loops back to RX.
  Validates the transceiver + PCB. The bus must be otherwise idle.

Typical use::

    with bp.open_can(bitrate=500_000, mode="internal") as can:
        can.write(0x123, [0xDE, 0xAD])
        frame = can.expect(can_id=0x123, timeout=1.0)
        assert frame.data == b"\\xde\\xad"
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, List, Optional, Sequence, Union

from .errors import CanTimeout

if TYPE_CHECKING:  # avoid a circular import at runtime
    from .client import BenchPod

CAN_MODES = ("normal", "internal", "external", "listen")

# A frame matcher for read_until/expect: an int CAN id, or a predicate on a frame.
Match = Union[int, Callable[["CanFrame"], bool]]


@dataclass
class CanFrame:
    """One classic CAN frame."""

    id: int
    data: bytes = b""
    ext: bool = False           # 29-bit extended identifier
    rtr: bool = False           # remote-transmission-request (no data)
    ts: int = 0                 # pod uptime in ms when received (ISR-stamped,
                                # jitter-free) — use for rate/period assertions

    @property
    def dlc(self) -> int:
        return len(self.data)

    @classmethod
    def from_dict(cls, d: dict) -> "CanFrame":
        return cls(
            id=int(d["id"]),
            data=bytes(d.get("data", []) or []),
            ext=bool(d.get("ext", False)),
            rtr=bool(d.get("rtr", False)),
            ts=int(d.get("ts", 0)),
        )

    def __str__(self) -> str:
        kind = "ext" if self.ext else "std"
        body = "RTR" if self.rtr else self.data.hex()
        return f"CAN {kind} id=0x{self.id:X} [{self.dlc}] {body}"


def frames_from_reply(data: Optional[dict]) -> List[CanFrame]:
    """Turn a ``can_read`` reply payload into a list of :class:`CanFrame`."""
    if not data:
        return []
    return [CanFrame.from_dict(f) for f in data.get("frames", [])]


def _matcher(match: Optional[Match]) -> Callable[["CanFrame"], bool]:
    if match is None:
        return lambda _f: True
    if callable(match):
        return match
    want = int(match)
    return lambda f: f.id == want


class CanBus:
    """A configured CAN link on the pod.

    Created by :meth:`BenchPod.open_can`; configures FDCAN on entry and disables
    it on exit (context-manager or :meth:`close`). Frames received between calls
    are buffered on the pod (software RX ring) and pulled in by :meth:`read`.
    """

    def __init__(self, bp: "BenchPod", *, bitrate: int, mode: str,
                 term: bool = False, fd: bool = False) -> None:
        if mode not in CAN_MODES:
            raise ValueError(f"mode must be one of {CAN_MODES}, got {mode!r}")
        self._bp = bp
        self.bitrate = int(bitrate)
        self.mode = mode
        self.term = bool(term)
        self._closed = False
        # Frames pulled off the device ring but not yet consumed by the caller.
        # ``can_read`` is destructive on the pod, so we buffer here — otherwise a
        # matched read would silently drop the other frames it drained.
        self._pending: List[CanFrame] = []
        self._config = bp.can_config(bitrate=bitrate, mode=mode, term=term, fd=fd)

    # -- writing ------------------------------------------------------------
    def write(self, can_id: int, data: Union[bytes, Sequence[int], None] = None,
              *, ext: bool = False, rtr: bool = False) -> dict:
        """Queue one classic frame (0..8 data bytes)."""
        return self._bp.can_write(can_id, data, ext=ext, rtr=rtr)

    # -- reading ------------------------------------------------------------
    def _pull(self) -> None:
        """Drain the pod's RX ring into the local buffer."""
        self._pending.extend(frames_from_reply(self._bp.can_read(max=8)))

    def read(self, max: int = 8) -> List[CanFrame]:
        """Return up to ``max`` buffered/received frames (non-blocking, FIFO)."""
        self._pull()
        out, self._pending = self._pending[:max], self._pending[max:]
        return out

    def read_until(self, match: Optional[Match] = None, *,
                   can_id: Optional[int] = None, timeout: float,
                   poll: float = 0.02) -> Optional[CanFrame]:
        """Poll until a frame matches ``match`` (an id or a predicate) or
        ``timeout`` elapses. Returns the matching :class:`CanFrame` or ``None``.

        Non-matching frames drained in the process are kept buffered, so a later
        :meth:`read`/:meth:`read_until` still sees them. ``can_id`` is a
        convenience alias for matching a specific identifier.
        """
        if can_id is not None and match is None:
            match = can_id
        pred = _matcher(match)
        deadline = time.monotonic() + timeout
        while True:
            for i, f in enumerate(self._pending):
                if pred(f):
                    del self._pending[i]
                    return f
            if time.monotonic() >= deadline:
                return None
            self._pull()
            if not self._pending:
                time.sleep(poll)

    def expect(self, match: Optional[Match] = None, *, can_id: Optional[int] = None,
               timeout: float = 1.0) -> CanFrame:
        """Like :meth:`read_until` but raises :class:`CanTimeout` on timeout.

        ``can_id`` is a convenience alias for matching a specific identifier.
        """
        f = self.read_until(match, can_id=can_id, timeout=timeout)
        if f is None:
            raise CanTimeout(
                f"no matching CAN frame within {timeout:g}s",
                frames=list(self._pending),
            )
        return f

    def collect(self, duration: float, *, match: Optional[Match] = None,
                poll: float = 0.02) -> List[CanFrame]:
        """Read every frame for ``duration`` seconds (optionally only those
        matching ``match``). Useful for observing a DUT's periodic traffic."""
        pred = _matcher(match)
        out: List[CanFrame] = []
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            got = [f for f in self.read() if pred(f)]
            out.extend(got)
            if not got:
                time.sleep(poll)
        return out

    def assert_periodic(self, can_id: int, period: float, *, tol: float = 0.25,
                        min_count: int = 3, duration: Optional[float] = None) -> List[CanFrame]:
        """Assert the DUT broadcasts ``can_id`` roughly every ``period`` seconds.

        Collects frames for ``duration`` (default ``period * (min_count + 2)``),
        then checks at least ``min_count`` arrived and every inter-arrival gap is
        within ``tol`` (fractional) of ``period``. Gaps use the pod's ISR-stamped
        ``ts`` (ms) so host/poll jitter doesn't pollute the measurement. Returns
        the collected frames. Raises ``AssertionError`` on violation."""
        if duration is None:
            duration = period * (min_count + 2)
        frames = self.collect(duration, match=can_id)
        if len(frames) < min_count:
            raise AssertionError(
                f"can_id 0x{can_id:X}: got {len(frames)} frames in {duration:g}s, "
                f"expected >= {min_count} at period {period:g}s")
        gaps = [(b.ts - a.ts) / 1000.0 for a, b in zip(frames, frames[1:])]
        lo, hi = period * (1 - tol), period * (1 + tol)
        bad = [g for g in gaps if not (lo <= g <= hi)]
        if bad:
            raise AssertionError(
                f"can_id 0x{can_id:X}: inter-arrival gaps {[round(g, 4) for g in gaps]}s "
                f"outside [{lo:g}, {hi:g}]s (period {period:g}s ±{tol:.0%})")
        return frames

    # -- autonomous responder (ECU simulation) ------------------------------
    def add_responder(self, match_id: int, reply_id: int,
                      reply_data: Union[bytes, Sequence[int], None] = None, *,
                      match_ext: bool = False, reply_ext: bool = False) -> dict:
        """Have the *firmware* auto-reply to ``match_id`` with ``reply_id`` +
        ``reply_data`` (microsecond latency, no host in the loop). See
        :meth:`BenchPod.can_respond`."""
        return self._bp.can_respond(match_id, reply_id, reply_data,
                                    match_ext=match_ext, reply_ext=reply_ext)

    def clear_responders(self) -> dict:
        """Remove all autonomous-responder rules."""
        return self._bp.can_respond_clear()

    def simulate_ecu(self, rules: dict) -> None:
        """Install a whole responder table at once. ``rules`` maps a request id
        to either a reply-data iterable (reply id defaults to ``request+1``) or a
        ``(reply_id, reply_data)`` tuple::

            can.simulate_ecu({0x7DF: (0x7E8, [0x50, 0x03]), 0x100: [0xAB]})
        """
        self.clear_responders()
        for match_id, spec in rules.items():
            if isinstance(spec, tuple):
                reply_id, reply_data = spec
            else:
                reply_id, reply_data = match_id + 1, spec
            self.add_responder(match_id, reply_id, reply_data)

    # -- state --------------------------------------------------------------
    def status(self) -> dict:
        return self._bp.can_status()

    def set_term(self, on: bool) -> dict:
        self.term = bool(on)
        return self._bp.can_term(on)

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        if not self._closed:
            self._closed = True
            # Clear the responder table FIRST: autonomous-responder rules are firmware state that
            # SURVIVES can_disable, so a bus that installed rules (simulate_ecu / add_responder)
            # leaks them onto the pod for whatever runs next. That is not theoretical — the ECU
            # demo's 0x7DF -> 0x7E8 rule outlived its test and made a later hwe2e
            # TestHW_CAN_Responder read the stale reply payload instead of its own.
            try:
                self.clear_responders()
            except Exception:
                pass
            try:
                self._bp.can_disable()
            except Exception:
                pass

    def __enter__(self) -> "CanBus":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
