"""The pod's I/O header, pin by pin, with a wiring profile on it.

Two headers exist:

* ``"j1"``: the 2x11 J1 of the one-piece boards up to v3-r2, seen from above: pin 1 (the square
  pad) at the bottom right, at the ADC/DAC end; even pins along the board edge.
* ``"front"``: the 2x13 right-angle connector on the front of digital v3r3 and later, seen facing
  the front of the enclosure: pin 1 at the top right, odd pins along the top.

:func:`header_map` writes a profile on a header as plain text (for a terminal, a log or an AI
agent); the web and desktop apps draw the same thing (their Pins pane's Visualize button).
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from .constants import PULL_TABLE
from .wiring import ROLE_KEYS, Wiring

HEADERS: Tuple[str, ...] = ("j1", "front")

_COMMON: List[Tuple[int, str]] = [(n, f"LA{n}") for n in range(1, 15)] + [
    (15, "GND"), (16, "GND"), (17, "+3V3"), (18, "+1V8"), (19, "CAN+"), (20, "CAN-"),
    (21, "LA_REF"), (22, "NRST_CONTROL"),
]

#: Every pin of each header, pin 1 first: ``(pin number, name)``.
HEADER_PINS: Dict[str, List[Tuple[int, str]]] = {
    "j1": _COMMON,
    "front": _COMMON + [(n, "GND") for n in (23, 24, 25, 26)],
}

_ROLE_LABELS = {
    "uart_rx": "UART RX", "uart_tx": "UART TX", "i2c_sda": "I2C SDA", "i2c_scl": "I2C SCL",
    "swd_swclk": "SWCLK", "swd_swdio": "SWDIO", "spi_sclk": "SPI SCLK", "spi_mosi": "SPI MOSI",
    "spi_miso": "SPI MISO", "spi_cs": "SPI CS", "jtag_tck": "TCK", "jtag_tms": "TMS",
    "jtag_tdi": "TDI", "jtag_tdo": "TDO", "jtag_trst": "TRST",
}
_ROLE_CONNECT = {
    "uart_rx": "board TX", "uart_tx": "board RX", "i2c_sda": "SDA", "i2c_scl": "SCL",
    "swd_swclk": "SWCLK", "swd_swdio": "SWDIO", "spi_sclk": "SCLK", "spi_mosi": "MOSI (board SDI)",
    "spi_miso": "MISO (board SDO)", "spi_cs": "CS", "jtag_tck": "TCK", "jtag_tms": "TMS",
    "jtag_tdi": "TDI", "jtag_tdo": "TDO", "jtag_trst": "TRST (nTRST)",
}
_SIGNAL_USE = {"input": "the pod reads it", "output": "the pod drives it", "open_drain": "open drain",
               "bidir": "open drain"}


def header_from_status(status: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The header from a status reply: ``module`` in the full status means digital v3r3 or later
    (``"front"``); board_rev v2 or v3r2 is a one-piece board (``"j1"``). None when it cannot tell
    (the cloud's short status, or board_rev "v3", which both kinds report)."""
    if not status:
        return None
    if status.get("module") is not None:
        return "front"
    if status.get("board_rev") in ("v2", "v3r2"):
        return "j1"
    return None


def _is_la(name: str) -> bool:
    return name.startswith("LA") and name[2:].isdigit()


def _uses(w: Wiring, la: int) -> List[Tuple[str, str]]:
    """``(label, connect to)`` for everything the profile puts on an LA channel."""
    out: List[Tuple[str, str]] = []
    for key in ROLE_KEYS:
        if getattr(w, key) == la:
            out.append((_ROLE_LABELS.get(key, key), _ROLE_CONNECT.get(key, key)))
    for s in w.signals:
        if s.la == la:
            out.append((s.name, f"{s.name} ({_SIGNAL_USE.get(s.direction, 'the pod reads it')})"))
    return out


def _pull(w: Wiring, la: int) -> Optional[str]:
    ohms = PULL_TABLE[la].ohms if la in PULL_TABLE else None
    d = w.pull_directions().get(la)
    if not ohms or not d:
        return None
    return f"pull-{d} {ohms} on"


def _cell(w: Wiring, n: int, name: str) -> str:
    """'3 LA3 UART RX' / '22 NRST_CONTROL DUT reset' / '9 LA9 -'."""
    if _is_la(name):
        uses = _uses(w, n)
        return f"{n} {name} " + (" / ".join(u for u, _ in uses) if uses else "-")
    if name == "NRST_CONTROL" and w.swd_nreset:
        return f"{n} {name} DUT reset"
    return f"{n} {name}"


def connections(w: Wiring, header: str = "j1") -> List[Dict[str, Any]]:
    """What to connect, one entry per used header pin plus ground:
    ``[{"pin": 3, "name": "LA3", "used_for": "UART RX", "connect": "board TX", "pull": "pull-up 2.2k on"}, ...]``."""
    out: List[Dict[str, Any]] = []
    for n, name in HEADER_PINS[header]:
        if _is_la(name):
            uses = _uses(w, n)
            if uses:
                out.append({"pin": n, "name": name, "used_for": " / ".join(u for u, _ in uses),
                            "connect": " / ".join(c for _, c in uses), "pull": _pull(w, n)})
        elif name == "NRST_CONTROL" and w.swd_nreset:
            out.append({"pin": n, "name": name, "used_for": "Reset", "connect": "board NRST", "pull": None})
    gnd = [n for n, name in HEADER_PINS[header] if name == "GND"]
    out.append({"pin": gnd, "name": "GND", "used_for": "Ground", "connect": "board GND (any one)", "pull": None})
    return out


def header_map(w: Wiring, header: Optional[str] = None) -> str:
    """The profile on the pod's header as plain text: the pins in their physical places, then what
    to connect to each. ``header`` is ``"j1"`` or ``"front"`` (:meth:`BenchPod.board_header`);
    None writes both (the pod did not say which it has)."""
    if header is None:
        return ("The pod did not say which header it has; both are shown.\n\n"
                + header_map(w, "j1") + "\n\n" + header_map(w, "front"))
    if header not in HEADERS:
        raise ValueError(f"header must be one of {', '.join(HEADERS)}")
    names = dict(HEADER_PINS[header])
    lines: List[str] = []
    if header == "j1":
        lines.append("Pin headers J1 (2x11), looking down at the pod: pin 1 (square pad) at the bottom right, "
                     "the ADC/DAC end at the bottom, even pins along the board edge on the left.")
        rows = [(_cell(w, p + 1, names[p + 1]), _cell(w, p, names[p])) for p in range(21, 0, -2)]
        width = max(len(left) for left, _ in rows)
        lines.append(f"  {'board edge side'.ljust(width)} | other side")
        lines += [f"  {left.ljust(width)} | {right}" for left, right in rows]
    else:
        lines.append("Front connector (2x13), facing the front of the pod: pin 1 (square pad) at the top "
                     "right, odd pins on the top row.")
        cols = [(_cell(w, p, names[p]), _cell(w, p + 1, names[p + 1])) for p in range(25, 0, -2)]
        width = max(len(top) for top, _ in cols)
        lines.append(f"  column  {'top row'.ljust(width)} | bottom row")
        lines += [f"  {i + 1:>2}      {top.ljust(width)} | {bottom}" for i, (top, bottom) in enumerate(cols)]
        lines.append("  (column 1 is the left end as you face the pod)")
    lines.append("Connect:")
    for c in connections(w, header):
        pin = ", ".join(str(n) for n in c["pin"]) if isinstance(c["pin"], list) else str(c["pin"])
        pull = f" [{c['pull']}]" if c["pull"] else ""
        lines.append(f"  pin {pin} ({c['name']}, {c['used_for']}) -> {c['connect']}{pull}")
    return "\n".join(lines)
