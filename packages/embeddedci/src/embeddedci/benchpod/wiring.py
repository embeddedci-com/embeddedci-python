"""Wiring profiles — which DUT signal is on which of the pod's 14 LA channels.

The pod has no role-named pins: any DUT signal can be on any of LA1..LA14. A wiring profile writes that
mapping down once — the UART, I2C, SWD and SPI roles, the target-power rail, the LA I/O voltage and
named signals such as ``TRIGGER`` — so tests use names and defaults instead of channel numbers::

    bp = BenchPod("192.168.1.50", wiring="wiring.json")
    with bp.open_uart() as uart:              # rx, tx and baud from the profile
        ...
    bp.signal("TRIGGER").pulse(0.005)         # the LA channel the profile names TRIGGER

The same schema (version 1) is stored per device on embeddedci.com, where the web UI edits it, and a
cloud connection (``embeddedci:<device>``) loads it from there. See :class:`Wiring`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple, Union

from .constants import PULL_OHMS, PULLDOWN_CHANNELS

WIRING_VERSION = 1

#: Profile keys that name the LA channel of a DUT signal role.
ROLE_KEYS: Tuple[str, ...] = ("uart_rx", "uart_tx", "i2c_sda", "i2c_scl", "swd_swclk", "swd_swdio",
                              "spi_sclk", "spi_mosi", "spi_miso", "spi_cs")
#: Valid :attr:`Signal.direction` values.
SIGNAL_DIRECTIONS: Tuple[str, ...] = ("input", "output", "open_drain", "bidir")

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_SIGNAL_KEYS = ("name", "la", "direction", "active_low", "description")
#: Keys other parts of the product keep in the profile; the SDK passes them through untouched.
_PASSTHROUGH_PREFIXES = ("loop_input_",)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _i2c_address(value: Any) -> Optional[int]:
    if _is_int(value):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16) if value.lower().startswith("0x") else int(value)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class Signal:
    """A named DUT signal on one LA channel — e.g. a trigger the pod drives or a "result ready" line
    it watches. ``direction`` describes the DUT pin as seen from the pod: ``output`` and
    ``open_drain`` are driven by the pod, ``input`` is watched, ``bidir`` is both."""

    name: str
    la: int
    direction: str = "input"
    active_low: bool = False
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "la": self.la, "direction": self.direction,
                "active_low": self.active_low, "description": self.description}


@dataclass(frozen=True)
class Wiring:
    """A bench's wiring profile (schema version 1). Construct it, :meth:`load` a file, or let
    :attr:`BenchPod.wiring <embeddedci.benchpod.BenchPod.wiring>` fetch it.

    Every field has the web UI's default, so ``Wiring()`` is the default bench and a profile only needs
    what differs. A role set to ``None`` is not wired. Construction validates the whole profile and
    raises :class:`ValueError` listing every problem — including two roles or signals on one LA
    channel. :meth:`warnings` lists wiring that works but is risky (an I2C bus without a pod pull-up).
    """

    la_mv: int = 3300
    efuse: int = 1
    uart_rx: Optional[int] = 5
    uart_tx: Optional[int] = 4
    uart_baud: int = 115200
    i2c_sda: Optional[int] = 1
    i2c_scl: Optional[int] = 2
    i2c_addr: str = "0x76"
    swd_swclk: Optional[int] = 11
    swd_swdio: Optional[int] = 12
    swd_nreset: bool = False
    swd_target: str = ""
    spi_sclk: Optional[int] = None
    spi_mosi: Optional[int] = None
    spi_miso: Optional[int] = None
    spi_cs: Optional[int] = None
    signals: Tuple[Signal, ...] = ()
    #: Profile keys owned by other features (the control loop's ``loop_input_*``), kept as they are.
    extra: Mapping[str, Any] = field(default_factory=dict)
    #: Where this profile came from: ``"defaults"``, ``"file"``, ``"server"`` or ``"dict"``.
    source: str = field(default="defaults", compare=False)
    version: int = WIRING_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "signals", tuple(self.signals))
        object.__setattr__(self, "extra", dict(self.extra))
        errors = self._errors()
        if errors:
            raise ValueError("invalid wiring: " + "; ".join(errors))

    # -- construction ---------------------------------------------------------

    @classmethod
    def defaults(cls) -> "Wiring":
        """The default bench (the web UI's defaults)."""
        return cls()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, source: str = "dict",
                  strict: bool = True) -> "Wiring":
        """Build a profile from its JSON object. Absent keys take their defaults; ``null`` pins are not
        wired. ``strict=False`` keeps unknown keys in :attr:`extra` instead of rejecting them (for a
        profile written by a newer server)."""
        if not isinstance(data, Mapping):
            raise ValueError("invalid wiring: a wiring profile must be a JSON object")
        scalar = {f.name for f in fields(cls)} - {"signals", "extra", "source"}
        kwargs: Dict[str, Any] = {}
        extra: Dict[str, Any] = {}
        unknown: List[str] = []
        for key, value in data.items():
            if key in scalar:
                kwargs[key] = value
            elif key == "signals":
                kwargs["signals"] = tuple(_parse_signals(value, unknown))
            elif key.startswith(_PASSTHROUGH_PREFIXES) or not strict:
                extra[key] = value
            else:
                unknown.append(key)
        if unknown:
            raise ValueError("invalid wiring: unknown key(s) " + ", ".join(sorted(unknown)))
        return cls(**kwargs, extra=extra, source=source)

    @classmethod
    def coerce(cls, value: Union["Wiring", Mapping[str, Any], str, Path]) -> "Wiring":
        """A profile from a :class:`Wiring`, a dict, or a path to a ``.json``/``.toml`` file."""
        if isinstance(value, Wiring):
            return value
        if isinstance(value, Mapping):
            return cls.from_dict(value)
        if isinstance(value, (str, Path)) or hasattr(value, "__fspath__"):
            return cls.load(value)  # type: ignore[arg-type]
        raise ValueError("wiring must be a Wiring, a dict or a path to a .json/.toml file, "
                         f"got {type(value).__name__}")

    @classmethod
    def load(cls, path: Union[str, Path]) -> "Wiring":
        """Read a profile from a ``.json`` or ``.toml`` file (TOML signals are ``[[signals]]`` tables)."""
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        if p.suffix.lower() == ".toml":
            try:
                import tomllib  # type: ignore[import-not-found]
            except ImportError:  # Python 3.10
                try:
                    import tomli as tomllib  # type: ignore[import-not-found,no-redef]
                except ImportError:
                    raise ValueError(f"reading {p.name} needs Python 3.11+ or `pip install tomli`; "
                                     "or use a .json wiring file") from None
            data = tomllib.loads(text)
        else:
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{p.name} is not valid JSON: {exc}") from None
        return cls.from_dict(data, source="file")

    def with_changes(self, **changes: Any) -> "Wiring":
        """A copy with some fields changed (validated again)."""
        return replace(self, **changes)

    def to_dict(self) -> Dict[str, Any]:
        """The profile's JSON object (every key, defaults included)."""
        out: Dict[str, Any] = {"version": self.version}
        for f in fields(self):
            if f.name in ("signals", "extra", "source", "version"):
                continue
            out[f.name] = getattr(self, f.name)
        out["signals"] = [s.to_dict() for s in self.signals]
        out.update(self.extra)
        return out

    # -- lookups ----------------------------------------------------------------

    @property
    def la_voltage(self) -> float:
        """The LA I/O-bank voltage in volts (``la_mv / 1000``)."""
        return self.la_mv / 1000.0

    @property
    def i2c_address(self) -> int:
        """``i2c_addr`` as a 7-bit integer."""
        return int(_i2c_address(self.i2c_addr))  # type: ignore[arg-type]

    def assignments(self) -> Iterator[Tuple[str, int]]:
        """``(role or signal name, LA channel)`` for everything wired."""
        for key in ROLE_KEYS:
            la = getattr(self, key)
            if _is_int(la):
                yield key, la
        for s in self.signals:
            if _is_int(s.la):
                yield s.name, s.la

    def pins(self) -> Dict[int, str]:
        """LA channel → the role or signal on it (unwired channels are absent)."""
        return {la: name for name, la in self.assignments()}

    def la(self, name: str) -> int:
        """The LA channel of a role (``"uart_rx"``) or a signal (``"TRIGGER"``, case-insensitive)."""
        key = str(name).strip()
        if key.lower() in ROLE_KEYS:
            la = getattr(self, key.lower())
            if la is None:
                raise ValueError(f"the wiring profile has no {key.lower()} (it is not wired)")
            return la
        return self.signal(key).la

    def signal(self, name: str) -> Signal:
        """The named :class:`Signal` (case-insensitive)."""
        for s in self.signals:
            if s.name.lower() == str(name).strip().lower():
                return s
        known = ", ".join(s.name for s in self.signals) or "none"
        raise ValueError(f"{name!r} is not a signal or role in the wiring profile (signals: {known})")

    def warnings(self) -> List[str]:
        """Wiring that is allowed but risky."""
        out: List[str] = []
        for key in ("i2c_sda", "i2c_scl"):
            la = getattr(self, key)
            if _is_int(la) and la >= 9:
                out.append(f"{key} is on LA{la}, which has no pod pull-up; the bus needs external pull-ups")
            elif la in PULLDOWN_CHANNELS:
                out.append(f"{key} is on LA{la}, which has a 10k pull-down; keep it disabled on an "
                           "open-drain bus")
        if self.uart_rx in PULLDOWN_CHANNELS:
            out.append(f"uart_rx is on LA{self.uart_rx}, which has a pull-down; the UART idles high, "
                       "so keep the pull disabled")
        return out

    def describe(self) -> str:
        """A readable pin table: channel, what is wired to it, and its bias resistor."""
        pins = self.pins()
        lines = [f"LA I/O {self.la_voltage:g} V, target power eFuse {self.efuse}"]
        for la in range(1, 15):
            pull = PULL_OHMS.get(la)
            bias = (f"{pull} pull-{'down' if la in PULLDOWN_CHANNELS else 'up'}" if pull else "no pull")
            lines.append(f"LA{la:<3} {pins.get(la, '-'):<12} {bias}")
        return "\n".join(lines + [f"warning: {w}" for w in self.warnings()])

    # -- validation -------------------------------------------------------------

    def _errors(self) -> List[str]:
        errs: List[str] = []
        if self.version != WIRING_VERSION:
            errs.append(f"version must be {WIRING_VERSION}, got {self.version!r}")
        if not _is_int(self.la_mv) or self.la_mv not in (1800, 3300):
            errs.append(f"la_mv must be 1800 or 3300, got {self.la_mv!r}")
        if not _is_int(self.efuse) or self.efuse not in (1, 2):
            errs.append(f"efuse must be 1 (internal) or 2 (external), got {self.efuse!r}")
        if not _is_int(self.uart_baud) or not 300 <= self.uart_baud <= 4_000_000:
            errs.append(f"uart_baud must be 300..4000000, got {self.uart_baud!r}")
        for key in ROLE_KEYS:
            la = getattr(self, key)
            if la is not None and not (_is_int(la) and 1 <= la <= 14):
                errs.append(f"{key} must be an LA channel 1..14 or None, got {la!r}")
        addr = _i2c_address(self.i2c_addr)
        if addr is None or not 0x03 <= addr <= 0x77:
            errs.append(f"i2c_addr must be a 7-bit address 0x03..0x77, got {self.i2c_addr!r}")
        if not isinstance(self.swd_nreset, bool):
            errs.append(f"swd_nreset must be true or false, got {self.swd_nreset!r}")
        if not isinstance(self.swd_target, str) or len(self.swd_target) > 128:
            errs.append("swd_target must be a string of at most 128 characters")
        if len(self.signals) > 14:
            errs.append(f"at most 14 signals, got {len(self.signals)}")
        seen: Dict[str, int] = {}
        for i, s in enumerate(self.signals):
            if not isinstance(s, Signal):
                errs.append(f"signals[{i}] must be a Signal")
                continue
            if not isinstance(s.name, str) or not _NAME.match(s.name):
                errs.append(f"signals[{i}].name {s.name!r} must start with a letter and use letters, "
                            "digits and _ (at most 32)")
            elif s.name.lower() in ROLE_KEYS:
                errs.append(f"signals[{i}].name {s.name!r} is a role name; pick another name")
            elif s.name.lower() in seen:
                errs.append(f"signals[{i}].name {s.name!r} is already used by signals[{seen[s.name.lower()]}]")
            else:
                seen[s.name.lower()] = i
            if not (_is_int(s.la) and 1 <= s.la <= 14):
                errs.append(f"signals[{i}].la must be an LA channel 1..14, got {s.la!r}")
            if s.direction not in SIGNAL_DIRECTIONS:
                errs.append(f"signals[{i}].direction must be one of {', '.join(SIGNAL_DIRECTIONS)}, "
                            f"got {s.direction!r}")
            if not isinstance(s.active_low, bool):
                errs.append(f"signals[{i}].active_low must be true or false")
            if not isinstance(s.description, str) or len(s.description) > 120:
                errs.append(f"signals[{i}].description must be a string of at most 120 characters")
        users: Dict[int, str] = {}
        for name, la in self.assignments():
            if not 1 <= la <= 14:
                continue
            if la in users:
                errs.append(f"LA{la} is used by both {users[la]} and {name} (move one to a free channel, "
                            "or set a role that isn't wired to None)")
            else:
                users[la] = name
        return errs


def _parse_signals(value: Any, unknown: List[str]) -> List[Signal]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("invalid wiring: signals must be a list")
    out: List[Signal] = []
    for i, entry in enumerate(value):
        if isinstance(entry, Signal):
            out.append(entry)
            continue
        if not isinstance(entry, Mapping) or "name" not in entry or "la" not in entry:
            raise ValueError(f"invalid wiring: signals[{i}] needs a name and an la")
        unknown.extend(f"signals[{i}].{k}" for k in sorted(set(entry) - set(_SIGNAL_KEYS)))
        out.append(Signal(name=entry["name"], la=entry["la"],
                          direction=entry.get("direction", "input"),
                          active_low=entry.get("active_low", False),
                          description=entry.get("description", "")))
    return out
