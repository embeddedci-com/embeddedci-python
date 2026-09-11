"""Typed device-state results: what the pod reports back from a command.

Every value is converted to the public API's units — **volts**, **amps**, **seconds** — so a
test never has to know that the firmware speaks millivolts and microamps. Each object keeps the
untouched firmware reply in ``raw`` (excluded from equality and ``repr``) for fields newer
firmware adds before the SDK names them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from .constants import Efuse, coerce_efuse


def _d(reply: Any) -> Dict[str, Any]:
    return dict(reply) if isinstance(reply, Mapping) else {}


def _opt_int(d: Mapping[str, Any], key: str) -> Optional[int]:
    v = d.get(key)
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def _raw() -> Any:
    return field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class LaVoltage:
    """The LA I/O-bank voltage (``BenchPod.set_la_voltage`` / ``get_la_voltage``)."""

    #: Selected bank voltage in volts (1.8 or 3.3), or ``None`` when none has been chosen yet —
    #: the pod refuses every LA-bank operation until one is.
    voltage: Optional[float]
    #: The measured bank voltage in volts, or ``None`` when the board cannot measure it.
    readback: Optional[float] = None
    raw: Dict[str, Any] = _raw()

    @property
    def is_set(self) -> bool:
        return self.voltage is not None

    @classmethod
    def from_reply(cls, reply: Any) -> "LaVoltage":
        d = _d(reply)
        mv = _opt_int(d, "mv") or 0
        rb = _opt_int(d, "readback_mv")
        # Boards without a readback divider report 0 (or a negative sentinel): that is "unknown",
        # not a bank sitting at 0 V.
        return cls(voltage=mv / 1000.0 if mv else None,
                   readback=rb / 1000.0 if rb is not None and rb > 0 else None, raw=d)


@dataclass(frozen=True)
class EfuseState:
    """One target-power eFuse."""

    enabled: bool
    #: The eFuse tripped (over-current / short on the target rail).
    fault: bool
    #: The state pins were readable (``enabled``/``fault`` are meaningful).
    valid: bool


@dataclass(frozen=True)
class TargetStatus:
    """Both eFuse rails (``BenchPod.target_status``)."""

    internal: EfuseState
    external: EfuseState
    #: False on boards that cannot read the eFuse state back.
    supported: bool = True
    raw: Dict[str, Any] = _raw()

    def efuse(self, efuse: "Efuse | int") -> EfuseState:
        """The state of eFuse ``1`` (internal) or ``2`` (external)."""
        return self.internal if coerce_efuse(efuse) == Efuse.INTERNAL else self.external

    @classmethod
    def from_reply(cls, reply: Any) -> "TargetStatus":
        d = _d(reply)

        def one(key: str) -> EfuseState:
            e = _d(d.get(key))
            return EfuseState(enabled=bool(e.get("enabled", 0)), fault=bool(e.get("fault", 0)),
                              valid=bool(e.get("valid", 0)))

        return cls(internal=one("efuse1"), external=one("efuse2"),
                   supported=bool(d.get("status_supported", True)), raw=d)


@dataclass(frozen=True)
class RailPower:
    """One INA power-monitor rail."""

    #: The monitor answered; ``bus_voltage``/``current`` are meaningful.
    ok: bool
    bus_voltage: float
    #: Current in amps.
    current: float


@dataclass(frozen=True)
class PowerStatus:
    """The INA power monitors on both target-power rails (``BenchPod.power_status``)."""

    internal: RailPower
    external: RailPower
    raw: Dict[str, Any] = _raw()

    def rail(self, efuse: "Efuse | int") -> RailPower:
        """The monitor on eFuse ``1`` (internal) or ``2`` (external)."""
        return self.internal if coerce_efuse(efuse) == Efuse.INTERNAL else self.external

    @classmethod
    def from_reply(cls, reply: Any) -> "PowerStatus":
        d = _d(reply)

        def one(key: str) -> RailPower:
            r = _d(d.get(key))
            return RailPower(ok=bool(r.get("ok", False)),
                             bus_voltage=(_opt_int(r, "bus_mv") or 0) / 1000.0,
                             current=(_opt_int(r, "current_ua") or 0) / 1e6)

        return cls(internal=one("internal"), external=one("external"), raw=d)


@dataclass(frozen=True)
class ResetState:
    """The dedicated target-reset line (``BenchPod.reset_target`` / ``set_reset``)."""

    #: The pod is holding the target in reset right now.
    asserted: bool
    supported: bool = True
    raw: Dict[str, Any] = _raw()

    @classmethod
    def from_reply(cls, reply: Any) -> "ResetState":
        d = _d(reply)
        return cls(asserted=bool(d.get("asserted", False)),
                   supported=bool(d.get("supported", True)), raw=d)


@dataclass(frozen=True)
class UsbCcStatus:
    """The pod's USB-C CC lines (``BenchPod.usb_cc``, rev3 pods)."""

    #: Cable orientation: ``"cc1"``, ``"cc2"`` or ``"none"``.
    orientation: str
    #: What the upstream source advertises: ``"none"``/``"default"``/``"1.5A"``/``"3.0A"``.
    advertised: str
    #: The advertised current in amps.
    advertised_current: float
    cc1_voltage: float
    cc2_voltage: float
    raw: Dict[str, Any] = _raw()

    @classmethod
    def from_reply(cls, reply: Any) -> "UsbCcStatus":
        d = _d(reply)
        return cls(orientation=str(d.get("orientation", "none")),
                   advertised=str(d.get("advertised", "none")),
                   advertised_current=(_opt_int(d, "advertised_ma") or 0) / 1000.0,
                   cc1_voltage=(_opt_int(d, "cc1_mv") or 0) / 1000.0,
                   cc2_voltage=(_opt_int(d, "cc2_mv") or 0) / 1000.0, raw=d)


@dataclass(frozen=True)
class PullState:
    """The fixed bias resistor on one LA channel (LA1-LA8)."""

    la: int
    #: The resistor is engaged.
    enabled: bool
    #: ``"up"`` (LA1-LA6) or ``"down"`` (LA7/LA8).
    direction: str
    #: Nominal resistance, e.g. ``"4.7k"``.
    ohms: str
    #: A pull can be engaged right now (the resistors are 3V3-referenced, so not at 1.8 V).
    available: bool = True
    raw: Dict[str, Any] = _raw()

    @classmethod
    def from_reply(cls, reply: Any, la: int) -> "PullState":
        from .constants import PULL_OHMS, PULLDOWN_CHANNELS

        d = _d(reply)
        default_dir = "down" if la in PULLDOWN_CHANNELS else "up"
        return cls(la=int(d.get("la", la)), enabled=bool(d.get("pullup", 0)),
                   direction=str(d.get("pull", default_dir)),
                   ohms=str(d.get("ohms", PULL_OHMS.get(la, ""))),
                   available=bool(d.get("pullups_available", 1)), raw=d)


@dataclass(frozen=True)
class AnalogPathState:
    """The analog switching state after ``BenchPod.analog_path``."""

    #: The canonical path name the pod applied (aliases resolve to it).
    path: str
    #: Raw DAC-mux (U55) and calibration-relay (U58) register values, for diagnostics.
    dac_mux_register: int = 0
    cal_relay_register: int = 0
    raw: Dict[str, Any] = _raw()

    @classmethod
    def from_reply(cls, reply: Any) -> "AnalogPathState":
        d = _d(reply)
        return cls(path=str(d.get("path", "")), dac_mux_register=_opt_int(d, "u55") or 0,
                   cal_relay_register=_opt_int(d, "u58") or 0, raw=d)


@dataclass(frozen=True)
class DacOutput:
    """A DC DAC output (``BenchPod.dac_output``)."""

    path: str
    #: The voltage the calibrated DAC actually produces (the nearest code to what was asked),
    #: or ``None`` when the call only routed the path.
    voltage: Optional[float]
    #: The DAC code driving it, or ``None`` when only routed.
    code: Optional[int]
    raw: Dict[str, Any] = _raw()

    @classmethod
    def from_reply(cls, reply: Any) -> "DacOutput":
        d = _d(reply)
        code = _opt_int(d, "code")
        routed_only = code is None or code < 0
        # The firmware answers with the analog-path name (dac_5v); report the output-path name the
        # caller used (5v) so the result speaks the same vocabulary as dac_output's argument.
        path = str(d.get("path", ""))
        path = path[len("dac_"):] if path.startswith("dac_") else path
        return cls(path=path,
                   voltage=None if routed_only else (_opt_int(d, "mv") or 0) / 1000.0,
                   code=None if routed_only else code, raw=d)


@dataclass(frozen=True)
class AdcReading:
    """A calibrated single ADC reading (``BenchPod.adc_read``)."""

    source: str
    #: Calibrated voltage at the source (``ext``: the true front-SMA voltage).
    voltage: float
    #: The averaged raw count behind it.
    count: int
    #: Peak-to-peak spread (counts) of the averaged burst — how still the input was.
    span: int = 0
    raw: Dict[str, Any] = _raw()

    @classmethod
    def from_reply(cls, reply: Any) -> "AdcReading":
        d = _d(reply)
        return cls(source=str(d.get("source", "")), voltage=(_opt_int(d, "mv") or 0) / 1000.0,
                   count=_opt_int(d, "count") or 0, span=_opt_int(d, "span") or 0, raw=d)


@dataclass(frozen=True)
class FpgaImageInfo:
    """The gateware image the iCE40 now runs (``BenchPod.fpga_image``)."""

    image: int
    #: Gateware version.
    version: int
    #: Gateware feature byte (bit 0 = control loop).
    features: int
    raw: Dict[str, Any] = _raw()

    @classmethod
    def from_reply(cls, reply: Any) -> "FpgaImageInfo":
        d = _d(reply)
        return cls(image=_opt_int(d, "image") or 0, version=_opt_int(d, "version") or 0,
                   features=_opt_int(d, "features") or 0, raw=d)


@dataclass(frozen=True)
class LoopState:
    """A running control loop's input configuration (``BenchPod.loop_input``)."""

    source: Optional[str]
    input_code: int
    step: int
    #: The DAC code at the instant of the write (poll ``loop_probe`` for the settled value).
    output_code: int
    raw: Dict[str, Any] = _raw()

    @classmethod
    def from_reply(cls, reply: Any) -> "LoopState":
        d = _d(reply)
        return cls(source=d.get("source"), input_code=_opt_int(d, "input") or 0,
                   step=_opt_int(d, "step") or 0, output_code=_opt_int(d, "v") or 0, raw=d)


__all__: List[str] = [
    "LaVoltage", "EfuseState", "TargetStatus", "RailPower", "PowerStatus", "ResetState",
    "UsbCcStatus", "PullState", "AnalogPathState", "DacOutput", "AdcReading", "FpgaImageInfo",
    "LoopState",
]
