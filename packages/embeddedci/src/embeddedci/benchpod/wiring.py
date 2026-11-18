"""Wiring profiles — which DUT signal is on which of the pod's 14 LA channels.

The pod has no role-named pins: any DUT signal can be on any of LA1..LA14. A wiring profile writes that
mapping down once — the UART, I2C, SWD and SPI roles, the target-power rail, the LA I/O voltage and
named signals such as ``TRIGGER`` — so tests use names and defaults instead of channel numbers::

    bp = BenchPod("192.168.1.50", wiring="wiring.json")
    with bp.open_uart() as uart:              # rx, tx and baud from the profile
        ...
    bp.signal("TRIGGER").pulse(0.005)         # the LA channel the profile names TRIGGER

Profiles live on the pod (firmware with the wiring commands): up to 16 named profiles, one of them
the default, the same over LAN, USB and the cloud. Schema version 2: what a profile does not set is
undefined (that pin is not wired, no I/O voltage, no power rail). A version 1 object (or one without
a version) still means what it did: what it leaves out takes the default pins' values
(:meth:`Wiring.preset`). See :class:`Wiring`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple, Union

from .constants import PULL_TABLE, PULLDOWN_CHANNELS, PULLUP_CHANNELS

PULL_CHANNELS = PULLUP_CHANNELS + PULLDOWN_CHANNELS

WIRING_VERSION = 2
#: The schema versions :meth:`Wiring.from_dict` reads: 1 (missing keys = the default pins) and 2.
WIRING_VERSIONS = (1, 2)
#: The default pins: what the pod assumed before profiles left nothing undefined, and what a
#: version 1 profile fills in. Not the wiring of any particular bench.
DEFAULT_PINS: Mapping[str, Any] = {
    "la_mv": 3300, "efuse": 1, "uart_rx": 5, "uart_tx": 4, "i2c_sda": 1, "i2c_scl": 2,
    "i2c_addr": "0x76", "swd_swclk": 11, "swd_swdio": 12,
}
#: The old name of :data:`DEFAULT_PINS`, kept for older code.
REFERENCE_BENCH = DEFAULT_PINS
#: Presets: complete profiles to start a new one from (:meth:`Wiring.preset`). Copied, never linked.
PRESETS: Mapping[str, Mapping[str, Any]] = {"embeddedci-default": DEFAULT_PINS}
#: Old preset names that still work: name -> the preset it is now.
PRESET_ALIASES: Mapping[str, str] = {"embeddedci-reference": "embeddedci-default"}

#: Profile keys that name the LA channel of a DUT signal role.
ROLE_KEYS: Tuple[str, ...] = ("uart_rx", "uart_tx", "i2c_sda", "i2c_scl", "swd_swclk", "swd_swdio",
                              "spi_sclk", "spi_mosi", "spi_miso", "spi_cs")
#: Valid :attr:`Signal.direction` values.
SIGNAL_DIRECTIONS: Tuple[str, ...] = ("input", "output", "open_drain", "bidir")
#: Valid :attr:`Wiring.dut_dirs` values: which way the DUT's signal goes.
DUT_DIRS: Tuple[str, ...] = ("dut_input", "dut_output", "bidir")
#: The default DUT direction of each role. ``uart_rx`` is the pod's receive line, so the DUT drives
#: it (a DUT output); ``uart_tx`` is the pod's transmit line (a DUT input).
ROLE_DUT_DIRS: Mapping[str, str] = {
    "uart_rx": "dut_output", "uart_tx": "dut_input", "i2c_sda": "bidir", "i2c_scl": "bidir",
    "swd_swclk": "dut_input", "swd_swdio": "bidir",
    "spi_sclk": "dut_input", "spi_mosi": "dut_input", "spi_miso": "dut_output", "spi_cs": "dut_input",
}
#: The default DUT direction of a signal from its :attr:`Signal.direction` (seen from the pod): a
#: signal the pod watches (``input``) is a DUT output, one it drives (``output``) a DUT input.
SIGNAL_DUT_DIRS: Mapping[str, str] = {"input": "dut_output", "output": "dut_input",
                                      "open_drain": "bidir", "bidir": "bidir"}

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_SIGNAL_KEYS = ("name", "la", "direction", "active_low", "description")
#: Keys other parts of the product keep in the profile; the SDK passes them through untouched.
#: ``expansion`` is the GPIO expander's object (BenchPod.expansion reads it).
_PASSTHROUGH_PREFIXES = ("loop_input_", "loop_output_", "expansion")


def _channels(las: Tuple[int, ...]) -> str:
    """``(1, 2, 3, 7, 8)`` -> ``"1..3, 7..8"``."""
    runs: List[List[int]] = []
    for la in sorted(las):
        if runs and la == runs[-1][-1] + 1:
            runs[-1].append(la)
        else:
            runs.append([la])
    return ", ".join(f"{r[0]}..{r[-1]}" if len(r) > 1 else str(r[0]) for r in runs)


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
    """A bench's wiring profile (schema version 2). Construct it, :meth:`load` a file, or let
    :attr:`BenchPod.wiring <embeddedci.benchpod.BenchPod.wiring>` load one from the pod.

    What is not set is undefined: ``Wiring()`` wires nothing, has no I/O voltage (``la_mv`` None) and
    no power rail (``efuse`` None); a call that needs one of them says so. Only plain call settings
    keep a value (``uart_baud`` 115200, ``swd_nreset`` False, ``swd_target`` auto-detect). Start from
    the default pins with ``Wiring.preset("embeddedci-default")``. Construction validates the whole profile and
    raises :class:`ValueError` listing every problem — including two roles or signals on one LA
    channel. :meth:`warnings` lists wiring that works but is risky (an I2C bus without a pod pull-up).
    """

    la_mv: Optional[int] = None
    efuse: Optional[int] = None
    uart_rx: Optional[int] = None
    uart_tx: Optional[int] = None
    uart_baud: int = 115200
    #: The UART frame after the 8 data bits: parity ("none", "even", "odd"), then 1 or 2 stop bits.
    uart_parity: str = "none"
    uart_stop_bits: int = 1
    i2c_sda: Optional[int] = None
    i2c_scl: Optional[int] = None
    i2c_addr: Optional[str] = None
    swd_swclk: Optional[int] = None
    swd_swdio: Optional[int] = None
    swd_nreset: bool = False
    swd_target: str = ""
    spi_sclk: Optional[int] = None
    spi_mosi: Optional[int] = None
    spi_miso: Optional[int] = None
    spi_cs: Optional[int] = None
    signals: Tuple[Signal, ...] = ()
    #: Profile keys owned by other features (the control loop's ``loop_input_*``), kept as they are.
    extra: Mapping[str, Any] = field(default_factory=dict)
    #: Where this profile came from: ``"pod"``, ``"file"``, ``"server"``, ``"dict"``, ``"preset"`` or
    #: ``"undefined"``.
    source: str = field(default="undefined", compare=False)
    #: The pod profile this was loaded from (``"k66"``), "" when it did not come from the pod.
    profile_name: str = field(default="", compare=False)
    #: That profile's revision on the pod when it was loaded (0: not from the pod).
    profile_rev: int = field(default=0, compare=False)
    version: int = WIRING_VERSION
    #: The DUT direction of a role or signal where its default is wrong: role key or signal name
    #: -> ``dut_input``, ``dut_output`` or ``bidir`` (see :meth:`pin_dut_dirs`).
    dut_dirs: Mapping[str, str] = field(default_factory=dict)
    #: The pull resistors :meth:`BenchPod.power_on` switches on (the others off) before the rail
    #: goes on: a map of LA channel -> ``"up"`` or ``"down"`` (``{1: "up", 2: "up"}``), or a list
    #: of channels (``[1, 2]``), which pull their own direction (up on LA1-LA6, down on LA7/LA8 on
    #: today's boards). ``None``: the profile leaves the pull resistors as they are.
    #: :meth:`pull_directions` gives either form as the map.
    pulls: Optional[Union[Tuple[int, ...], Mapping[int, str]]] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "signals", tuple(self.signals))
        if isinstance(self.pulls, (list, tuple)):
            pulls = tuple(self.pulls)
            if all(_is_int(x) for x in pulls):
                pulls = tuple(sorted(set(pulls)))
            object.__setattr__(self, "pulls", pulls)
        elif isinstance(self.pulls, Mapping):
            # JSON keys are strings ("1"); keep anything else as it is for validate() to name
            m = {(int(k) if isinstance(k, str) and k.strip().isdigit() else k):
                 (v.strip().lower() if isinstance(v, str) else v) for k, v in self.pulls.items()}
            object.__setattr__(self, "pulls", dict(sorted(m.items(), key=lambda kv: str(kv[0]).zfill(3))))
        if isinstance(self.dut_dirs, Mapping):
            object.__setattr__(self, "dut_dirs", dict(self.dut_dirs))
        object.__setattr__(self, "extra", dict(self.extra))
        errors = self._errors()
        if errors:
            raise ValueError("invalid wiring: " + "; ".join(errors))

    # -- construction ---------------------------------------------------------

    @classmethod
    def undefined(cls) -> "Wiring":
        """A profile with nothing wired, no I/O voltage and no power rail."""
        return cls(source="undefined")

    @classmethod
    def preset(cls, name: str = "embeddedci-default") -> "Wiring":
        """A complete profile to start from: ``"embeddedci-default"`` is the default pins, what the
        pod assumed before version 2 (UART RX on LA5 and TX on LA4, I2C SDA on LA1 and SCL on LA2,
        SWD on LA11/LA12, 3.3 V, internal 5 V). It is not the wiring of any particular bench: check
        it against yours, then save a copy under your board's name:
        ``bp.save_wiring(Wiring.preset().with_changes(uart_rx=3), name="nucleo")``. The old name
        ``"embeddedci-reference"`` still works."""
        try:
            values = PRESETS[PRESET_ALIASES.get(name, name)]
        except KeyError:
            raise ValueError(f"no wiring preset {name!r} (presets: {', '.join(sorted(PRESETS))})") from None
        return cls(**values, source="preset")

    @classmethod
    def defaults(cls) -> "Wiring":
        """The default pins, what a profile was before version 2 left nothing undefined. Kept for
        older code: use :meth:`preset` (or :meth:`undefined`)."""
        return cls.preset("embeddedci-default")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, source: str = "dict",
                  strict: bool = True) -> "Wiring":
        """Build a profile from its JSON object. Version 2: absent keys are undefined. Version 1, or no
        ``version`` key: absent keys take the default pins' values (:data:`DEFAULT_PINS`), as
        before version 2; the result is a version 2 profile with them written out. ``null`` pins are
        not wired. ``strict=False`` keeps unknown keys in :attr:`extra` instead of rejecting them (for
        a profile written by a newer server)."""
        if not isinstance(data, Mapping):
            raise ValueError("invalid wiring: a wiring profile must be a JSON object")
        version = data.get("version", 1)
        if not _is_int(version) or version not in WIRING_VERSIONS:
            raise ValueError(f"invalid wiring: version must be 1 or 2, got {version!r}")
        if version == 1:
            data = {**REFERENCE_BENCH, **data, "version": WIRING_VERSION}
        scalar = {f.name for f in fields(cls)} - {"signals", "extra", "source", "dut_dirs", "profile_name", "profile_rev"}
        kwargs: Dict[str, Any] = {}
        extra: Dict[str, Any] = {}
        unknown: List[str] = []
        for key, value in data.items():
            if key in scalar:
                kwargs[key] = value
            elif key == "signals":
                kwargs["signals"] = tuple(_parse_signals(value, unknown))
            elif key == "dut_dirs":
                kwargs["dut_dirs"] = value
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
        """The profile's JSON object, version 2: every key, ``null`` for what is not set."""
        out: Dict[str, Any] = {"version": self.version}
        for f in fields(self):
            if f.name in ("signals", "extra", "source", "version", "dut_dirs", "pulls", "profile_name", "profile_rev"):
                continue
            out[f.name] = getattr(self, f.name)
        out["signals"] = [s.to_dict() for s in self.signals]
        out["dut_dirs"] = dict(self.dut_dirs)
        if isinstance(self.pulls, Mapping):
            # the list whenever every pull is its channel's own direction: every reader takes it
            dirs = self.pull_directions()
            if dirs and all(PULL_TABLE.get(la) and PULL_TABLE[la].default == d for la, d in dirs.items()):
                out["pulls"] = sorted(dirs)
            else:
                out["pulls"] = {str(la): d for la, d in self.pulls.items()}
        elif self.pulls is not None:
            out["pulls"] = list(self.pulls)
        out.update(self.extra)
        return out

    # -- lookups ----------------------------------------------------------------

    @property
    def la_voltage(self) -> Optional[float]:
        """The LA I/O-bank voltage in volts (``la_mv / 1000``), None when the profile sets none."""
        return None if self.la_mv is None else self.la_mv / 1000.0

    @property
    def i2c_address(self) -> Optional[int]:
        """``i2c_addr`` as a 7-bit integer, None when the profile sets none."""
        return None if self.i2c_addr is None else int(_i2c_address(self.i2c_addr))  # type: ignore[arg-type]

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

    def dut_dir(self, name: str) -> str:
        """The DUT direction of a role (``"uart_rx"``) or signal: its :attr:`dut_dirs` entry, else
        the default (:data:`ROLE_DUT_DIRS`, or :data:`SIGNAL_DUT_DIRS` from the signal's
        direction)."""
        key = str(name).strip()
        for k, d in self.dut_dirs.items():
            if k.lower() == key.lower():
                return d
        if key.lower() in ROLE_KEYS:
            return ROLE_DUT_DIRS[key.lower()]
        return SIGNAL_DUT_DIRS.get(self.signal(key).direction, "dut_output")

    def pin_dut_dirs(self) -> Dict[int, str]:
        """LA channel -> the DUT direction of what is wired to it (:meth:`dut_dir`), for every
        wired role and signal: ``{5: "dut_output", 4: "dut_input", 1: "bidir", ...}``. This is what
        the pod gets with ``la_dir``; unwired channels are absent."""
        return {la: self.dut_dir(name) for name, la in self.assignments()}

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
            elif _is_int(la) and self._pull_may_be(la, "down"):
                out.append(f"{key} is on LA{la}, which has a {PULL_TABLE[la].ohms} pull-down; keep it "
                           "disabled on an open-drain bus")
        if _is_int(self.uart_rx) and self._pull_may_be(self.uart_rx, "down"):
            out.append(f"uart_rx is on LA{self.uart_rx}, which has a pull-down; the UART idles high, "
                       "so keep the pull disabled")
        if self.la_mv == 1800 and self.pulls:
            out.append("pulls: the pull resistors work at 3.3 V only; at 1.8 V they stay off")
        return out

    def pull_directions(self) -> Dict[int, str]:
        """The pulls :meth:`BenchPod.power_on` switches on: LA channel -> ``"up"`` / ``"down"``. A
        list of channels gives each channel's own direction; ``pulls`` None gives ``{}``."""
        if isinstance(self.pulls, Mapping):
            return {int(la): str(d) for la, d in self.pulls.items()}
        out: Dict[int, str] = {}
        for la in self.pulls or ():
            hw = PULL_TABLE.get(la) if _is_int(la) else None
            if hw:
                out[int(la)] = hw.default
        return out

    def _pull_may_be(self, la: int, direction: str) -> bool:
        """Does LA ``la`` pull ``direction`` with this profile, or may it (``pulls`` None)?"""
        if self.pulls is None:
            hw = PULL_TABLE.get(la)
            return bool(hw and direction in hw.dirs)
        return self.pull_directions().get(la) == direction

    def describe(self) -> str:
        """A readable pin table: channel, what is wired to it, its bias resistor and its DUT
        direction."""
        pins = self.pins()
        dirs = self.pin_dut_dirs()
        io = f"{self.la_voltage:g} V" if self.la_voltage is not None else "not set"
        rail = f"eFuse {self.efuse}" if self.efuse is not None else "not set"
        lines = [f"LA I/O {io}, target power {rail}"]
        for la in range(1, 15):
            hw = PULL_TABLE.get(la)
            bias = f"{hw.ohms} pull-{'/'.join(hw.dirs)}" if hw else "no pull"
            line = f"LA{la:<3} {pins.get(la, '-'):<12} {bias:<16} {dirs.get(la, '')}"
            lines.append(line.rstrip())
        return "\n".join(lines + [f"warning: {w}" for w in self.warnings()])

    # -- validation -------------------------------------------------------------

    def _errors(self) -> List[str]:
        errs: List[str] = []
        if self.version != WIRING_VERSION:
            errs.append(f"version must be {WIRING_VERSION}, got {self.version!r}")
        if self.la_mv is not None and (not _is_int(self.la_mv) or self.la_mv not in (1800, 3300)):
            errs.append(f"la_mv must be 1800 or 3300 (or None: not set), got {self.la_mv!r}")
        if self.efuse is not None and (not _is_int(self.efuse) or self.efuse not in (1, 2)):
            errs.append(f"efuse must be 1 (internal) or 2 (external) (or None: not set), got {self.efuse!r}")
        if not _is_int(self.uart_baud) or not 300 <= self.uart_baud <= 4_000_000:
            errs.append(f"uart_baud must be 300..4000000, got {self.uart_baud!r}")
        if self.uart_parity not in ("none", "even", "odd"):
            errs.append(f"uart_parity must be 'none', 'even' or 'odd', got {self.uart_parity!r}")
        if not _is_int(self.uart_stop_bits) or self.uart_stop_bits not in (1, 2):
            errs.append(f"uart_stop_bits must be 1 or 2, got {self.uart_stop_bits!r}")
        for key in ROLE_KEYS:
            la = getattr(self, key)
            if la is not None and not (_is_int(la) and 1 <= la <= 14):
                errs.append(f"{key} must be an LA channel 1..14 or None, got {la!r}")
        addr = _i2c_address(self.i2c_addr)
        if self.i2c_addr is not None and (addr is None or not 0x03 <= addr <= 0x77):
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
        if isinstance(self.pulls, Mapping):
            for la, d in self.pulls.items():
                hw = PULL_TABLE.get(la) if _is_int(la) else None
                if not hw:
                    errs.append(f"pulls.{la}: must be an LA channel with a pull resistor "
                                f"({_channels(PULL_CHANNELS)}), got {la!r}")
                elif d not in ("up", "down"):
                    errs.append(f"pulls.{la}: must be \"up\" or \"down\", got {d!r}")
                elif d not in hw.dirs:
                    errs.append(f"pulls.{la}: LA{la} supports pull-{hw.default} only on this board")
        elif self.pulls is not None:
            if not isinstance(self.pulls, (list, tuple)):
                errs.append("pulls must be a map of LA channel -> \"up\" or \"down\" "
                            "(e.g. {\"1\": \"up\"}) or a list of channels (e.g. [1, 2])")
            else:
                for i, la in enumerate(self.pulls):
                    if not (_is_int(la) and la in PULL_CHANNELS):
                        errs.append(f"pulls[{i}] must be an LA channel with a pull resistor "
                                    f"({_channels(PULL_CHANNELS)}), got {la!r}")
        if not isinstance(self.dut_dirs, Mapping):
            errs.append("dut_dirs must be an object of role or signal name -> direction")
        else:
            names = {s.name.lower() for s in self.signals if isinstance(s, Signal)
                     and isinstance(s.name, str)}
            for k, d in self.dut_dirs.items():
                if not isinstance(k, str) or (k.lower() not in ROLE_KEYS and k.lower() not in names):
                    errs.append(f"dut_dirs.{k}: {k!r} is not a wiring role or a signal name")
                elif d not in DUT_DIRS:
                    errs.append(f"dut_dirs.{k} must be one of {', '.join(DUT_DIRS)}, got {d!r}")
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
