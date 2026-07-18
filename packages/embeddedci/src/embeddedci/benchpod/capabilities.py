"""Device capabilities — what a given BenchPod can do and how to scale its ADC/DAC.

The firmware announces a rich capability set (ADC resolution + front-end calibration, DAC
replay depth, feature flags) so the client can drive captures/replays correctly without
hard-coding a board. Two sources feed a :class:`Capabilities`:

* the device ``status`` command (available over any transport — TCP, serial, or the cloud
  tunnel): carries ``board``, ``adc_bits``, ``adc_fullscale_mv``, ``adc_channels`` and the
  ``la_vccio_mv``, but **not** the affine ADC calibration or DAC-replay depth (those ship
  only in the connect-time capabilities frame the firmware sends the cloud server);
* the server's device-parameter map (``cap.*`` keys from ``GET /api/benchpod/devices``),
  which carries the complete set including ``cap.adc_cal_a`` / ``cap.adc_cal_b`` and
  ``cap.dac_replay_max_samples`` — used when the SDK has server access.

When the affine calibration is not reported (today's firmware does not put it in ``status``),
the client falls back to the same built-in per-board nominal the server uses
(:data:`ADC_AFFINE_DEFAULTS`, mirroring the Go ``benchpodADCAffineDefaults``), so a v2 pod's
inverting front-end still scales counts to the *probe* voltage rather than the ADC-pin
voltage. This keeps client-side capture volts consistent with what the server streams.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

# Built-in nominal front-end calibration per board — the affine fit ``volts = a + b*count``
# for boards whose analog front-end is inverting (so the naive count/full-scale model would
# report the ADC-pin voltage, not the probe voltage). MIRRORS the server's
# ``benchpodADCAffineDefaults`` and the firmware's baked-in ``ADC_CAL_EXT``
# (bench-pod-firmware/stm32h563/src/cal_data.h). ``a`` is in volts, ``b`` in volts/count.
ADC_AFFINE_DEFAULTS: Dict[str, "AffineCal"] = {}

# A 16-bit bipolar ADC count wraps past the top for near-zero / negative inputs; unwrap it
# (count += span when count < threshold) before applying the affine fit. Mirrors the
# firmware's "count_uw = count + 65536 when count < 32768".
ADC_WRAP_THRESHOLD = 32768
ADC_WRAP_SPAN = 65536


@dataclass(frozen=True)
class AffineCal:
    """An inverting-front-end fit ``volts = a + b*count`` (with optional 16-bit unwrap)."""

    a: float
    b: float
    unwrap: bool = False


ADC_AFFINE_DEFAULTS["stm32h563"] = AffineCal(a=65.788900, b=-0.001003762, unwrap=True)


def _as_float(m: Mapping[str, Any], *keys: str) -> Optional[float]:
    for k in keys:
        if k in m and m[k] is not None:
            try:
                return float(m[k])
            except (TypeError, ValueError):
                continue
    return None


def _as_int(m: Mapping[str, Any], *keys: str) -> Optional[int]:
    v = _as_float(m, *keys)
    return int(v) if v is not None else None


def _as_bool(m: Mapping[str, Any], *keys: str) -> Optional[bool]:
    for k in keys:
        if k in m and m[k] is not None:
            v = m[k]
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return bool(v)
            s = str(v).strip().lower()
            if s in ("true", "1", "yes", "on"):
                return True
            if s in ("false", "0", "no", "off"):
                return False
    return None


@dataclass
class Capabilities:
    """Resolved capabilities for a connected device.

    Build one with :meth:`from_status` (device ``status`` reply) or
    :meth:`from_parameters` (server ``cap.*`` map). Fields left at their defaults were not
    reported. :meth:`counts_to_volts` applies the correct (affine or linear) ADC model.
    """

    board: str = ""
    firmware_version: str = ""

    # ADC
    adc_bits: int = 8
    adc_fullscale_mv: float = 3300.0
    adc_channels: int = 1
    adc_offset_counts: float = 0.0
    #: affine front-end fit; None => use the naive linear model.
    adc_affine: Optional[AffineCal] = None

    # DAC / replay
    dac: bool = False
    dac_dc: bool = False
    dac_replay: bool = False
    dac_deep_replay: bool = False
    dac_bits: int = 8
    dac_replay_bits: int = 8
    dac_replay_max_samples: int = 4096
    dac_fullscale_mv: float = 0.0
    dac_channels: int = 1

    # feature flags
    scope: bool = False
    analyzer: bool = False
    serial: bool = False
    tunnel: bool = False
    command: bool = False
    ota: bool = False

    #: LA I/O-bank voltage the pod currently reports (mV), if known.
    la_vccio_mv: int = 0
    #: raw source map this was parsed from (for debugging / passthrough).
    raw: Dict[str, Any] = field(default_factory=dict)

    # -- constructors -------------------------------------------------------

    @classmethod
    def from_status(cls, status: Mapping[str, Any]) -> "Capabilities":
        """Parse the device ``status`` reply (``data`` object).

        ``status`` carries the ADC resolution + full scale + board, but not the affine
        calibration; the per-board nominal from :data:`ADC_AFFINE_DEFAULTS` fills that in.
        """
        c = cls(raw=dict(status))
        c.board = str(status.get("board", "") or "")
        c.firmware_version = str(status.get("version", status.get("firmware_version", "")) or "")
        if (v := _as_int(status, "adc_bits")) is not None:
            c.adc_bits = v
            # `status` doesn't carry the DAC-replay width, but a 16-bit-ADC pod (v2) replays
            # 16-bit; assume that so client-side replay packs the right width without needing
            # the server's cap.* set. Server parameters override this in from_parameters/merge.
            if c.adc_bits >= 16:
                c.dac_bits = 16
                c.dac_replay_bits = 16
        if (v := _as_float(status, "adc_fullscale_mv")) is not None and v > 0:
            c.adc_fullscale_mv = v
        if (v := _as_int(status, "adc_channels")) is not None and v > 0:
            c.adc_channels = v
        if (v := _as_int(status, "la_vccio_mv")) is not None:
            c.la_vccio_mv = v
        caps = status.get("caps")
        if isinstance(caps, (list, tuple)):
            names = {str(x).lower() for x in caps}
            c.scope = c.scope or ("signal" in names or "scope" in names)
            c.analyzer = c.analyzer or ("la" in names or "analyzer" in names)
            c.serial = c.serial or ("uart" in names or "serial" in names)
        c._apply_affine(status)
        return c

    @classmethod
    def from_parameters(cls, params: Mapping[str, Any]) -> "Capabilities":
        """Parse a server ``cap.*`` device-parameter map (complete capability set)."""
        c = cls(raw=dict(params))
        c.board = str(params.get("cap.board", "") or "")
        c.firmware_version = str(params.get("cap.firmware_version", "") or "")
        if (v := _as_int(params, "cap.adc_bits")) is not None and v > 0:
            c.adc_bits = v
        fs = _as_float(params, "cap.adc_fullscale_mv") or _as_float(params, "cap.adc_vref_mv")
        if fs is not None and fs > 0:
            c.adc_fullscale_mv = fs
        if (v := _as_int(params, "cap.adc_channels")) is not None and v > 0:
            c.adc_channels = v
        if (v := _as_float(params, "cap.adc_offset_counts")) is not None:
            c.adc_offset_counts = v
        if (v := _as_int(params, "cap.dac_bits")) is not None and v > 0:
            c.dac_bits = v
        if (v := _as_int(params, "cap.dac_replay_bits")) is not None and v > 0:
            c.dac_replay_bits = v
        if (v := _as_int(params, "cap.dac_replay_max_samples")) is not None and v > 0:
            c.dac_replay_max_samples = v
        if (v := _as_float(params, "cap.dac_fullscale_mv")) is not None and v > 0:
            c.dac_fullscale_mv = v
        if (v := _as_int(params, "cap.dac_channels")) is not None and v > 0:
            c.dac_channels = v
        for attr, key in (
            ("dac", "cap.dac"), ("dac_dc", "cap.dac_dc"), ("dac_replay", "cap.dac_replay"),
            ("dac_deep_replay", "cap.dac_deep_replay"), ("scope", "cap.scope"),
            ("analyzer", "cap.analyzer"), ("serial", "cap.serial"), ("tunnel", "cap.tunnel"),
            ("command", "cap.command"), ("ota", "cap.ota"),
        ):
            b = _as_bool(params, key)
            if b is not None:
                setattr(c, attr, b)
        # affine cal: explicit cap.adc_cal_* first, else per-board default.
        cal_b = _as_float(params, "cap.adc_cal_b")
        if cal_b is not None and cal_b != 0:
            cal_a = _as_float(params, "cap.adc_cal_a") or 0.0
            c.adc_affine = AffineCal(a=cal_a, b=cal_b, unwrap=bool(_as_bool(params, "cap.adc_cal_unwrap")))
        else:
            c._apply_affine(params, board=c.board)
        return c

    def _apply_affine(self, src: Mapping[str, Any], board: Optional[str] = None) -> None:
        """Fill :attr:`adc_affine` from explicit cal fields, else the per-board default.

        Accepts either volts (``adc_cal_a``/``adc_cal_b``) or the firmware's integer
        ``adc_cal_a_uv`` (microvolts) / ``adc_cal_b_nv`` (nanovolts/count).
        """
        a = _as_float(src, "adc_cal_a")
        b = _as_float(src, "adc_cal_b")
        if b is None:
            a_uv = _as_float(src, "adc_cal_a_uv")
            b_nv = _as_float(src, "adc_cal_b_nv")
            if b_nv is not None and b_nv != 0:
                a = (a_uv or 0.0) / 1e6
                b = b_nv / 1e9
        if b is not None and b != 0:
            self.adc_affine = AffineCal(a=a or 0.0, b=b, unwrap=bool(_as_bool(src, "adc_cal_unwrap")))
            return
        default = ADC_AFFINE_DEFAULTS.get(board if board is not None else self.board)
        if default is not None:
            self.adc_affine = default

    # -- scaling ------------------------------------------------------------

    @property
    def adc_max_count(self) -> int:
        if not 1 <= self.adc_bits <= 24:
            return 255
        return (1 << self.adc_bits) - 1

    def counts_to_volts(self, count: float) -> float:
        """Convert one raw ADC count to volts using the resolved model.

        Uses the affine fit ``a + b*count`` (with 16-bit unwrap) when calibrated, else the
        naive linear model ``(count - offset) * fullscale/maxcount``. Mirrors the server's
        ``adcScaling.volts``.
        """
        cal = self.adc_affine
        if cal is not None:
            c = float(count)
            if cal.unwrap and count < ADC_WRAP_THRESHOLD:
                c += ADC_WRAP_SPAN
            return cal.a + cal.b * c
        mc = self.adc_max_count or 255
        fs = (self.adc_fullscale_mv / 1000.0) if self.adc_fullscale_mv > 0 else 3.3
        return (float(count) - self.adc_offset_counts) * (fs / mc)

    def merge(self, other: "Capabilities") -> "Capabilities":
        """Overlay the non-default fields of ``other`` onto a copy of ``self`` (other wins).

        Used to enrich device-``status`` capabilities with the fuller server ``cap.*`` set
        (which carries DAC-replay depth + explicit calibration) when both are available.
        """
        base = Capabilities()
        out = Capabilities(**{f: getattr(self, f) for f in _FIELD_NAMES})
        for f in _FIELD_NAMES:
            ov = getattr(other, f)
            if ov != getattr(base, f):
                setattr(out, f, ov)
        return out


_FIELD_NAMES = tuple(f for f in Capabilities().__dict__.keys())
