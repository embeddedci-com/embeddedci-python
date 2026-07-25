"""Closed-loop DAC control — panel / MPPT emulator (in-fabric).

The iCE40 runs a control loop entirely in fabric: each tick it reads the ADC (a load
"current"), looks that value up in a reloadable 2048-entry curve LUT (``V = curve[ADC >> 5]``),
damps toward that target and clamps to ``[vmin, vmax]``, then drives the DAC. Loaded with a
solar-panel I-V table it emulates a panel: as the load current rises from open-circuit toward
short-circuit, the panel voltage falls from ``Voc`` toward 0 with a knee near the max-power
point. Only the **loop gateware image** provides it (:attr:`Capabilities.dac_control_loop`); use
:meth:`~embeddedci.benchpod.client.BenchPod.fpga_image` to switch a device onto it.

This module holds the transport-independent pieces — the curve builder/encoder (a faithful port
of the web UI's ``controlLoopCurve.ts``) and the :class:`ControlLoopHandle`. The arming
(``dac_control_loop`` / ``dac_loop_probe`` / ``dac_stop`` commands) lives on
:class:`~embeddedci.benchpod.client.BenchPod`.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

#: Points UPLOADED in a curve command. The gateware LUT is 2048 entries (``ADC >> 5``), but one
#: command is transport-capped (~1.2 KB), so the SDK sends a compact curve and the firmware
#: upsamples it (nearest-neighbour) to fill all 2048 entries. 256 pts × 2 B = 512 → ~684 base64
#: chars, well within the cap and smooth for a panel curve. Mirrors ``CURVE_POINTS`` in the UI.
CURVE_POINTS = 256

#: Max points the firmware curve buffer accepts (2048 LUT entries × 2 bytes = 4096 bytes).
CURVE_MAX_POINTS = 2048

# Firmware defaults for the arm command (bench-pod-firmware handle_dac_control_loop).
DEFAULT_K = 8192          # Q15 damping factor (8192/32768 = 0.25)
DEFAULT_VMIN = 0
DEFAULT_VMAX = 0xFFFF
DEFAULT_TICK_DIV = 64

#: Bounds the gateware actually honours. The fabric multiplies with ``k[14:0]``, so a k above
#: 32767 wraps to a *small* gain (a loop that crawls instead of racing) and k=0 freezes the output
#: at ``vmin``; the pipelined tick needs at least 8 clk48 cycles to retire a pass.
K_MIN, K_MAX = 1, 32767
TICK_DIV_MIN = 8


def normalise_loop_params(k: int, vmin: int, vmax: int, tick_div: int) -> tuple:
    """Validate + normalise the loop's arm parameters, mirroring the firmware's own guard
    (``bench-pod-firmware/stm32h563/src/dac_loop_params.c``).

    ``k`` and ``tick_div`` are CLAMPED into the range the gateware honours. An inverted output
    window (``vmin > vmax``) is REJECTED: the gateware clamps against ``vmin`` first, so it would
    pin the DAC at ``vmin`` and silently ignore the ceiling you asked for — on an emulator, that
    means driving a rail the DUT was supposed to be protected from. Returns the effective
    ``(k, vmin, vmax, tick_div)``.
    """
    k, vmin, vmax, tick_div = int(k), int(vmin), int(vmax), int(tick_div)
    if vmin > vmax:
        raise ValueError(f"vmin ({vmin}) must be <= vmax ({vmax}): an inverted clamp would pin the DAC at vmin")
    for name, val in (("vmin", vmin), ("vmax", vmax)):
        if not 0 <= val <= 0xFFFF:
            raise ValueError(f"{name} ({val}) must be a 16-bit DAC code (0..65535)")
    k = max(K_MIN, min(K_MAX, k))
    tick_div = max(TICK_DIV_MIN, min(0xFFFF, tick_div))
    return k, vmin, vmax, tick_div


def build_panel_curve(voc_code: int, sharpness: float = 4.0,
                      points: int = CURVE_POINTS) -> List[int]:
    """Build a solar-panel I-V curve as ``points`` DAC codes (0..65535), indexed by load current.

    ``voc_code`` is the open-circuit voltage (the code at zero current); ``sharpness`` shapes the
    knee — higher = flatter plateau then a steeper cliff toward short-circuit (a typical panel is
    ~4–8). Faithful port of the web UI's ``buildPanelCurve`` so the SDK and UI drive an identical
    emulator: ``v = voc * (1 - x**sharpness)`` where ``x`` is the load-current fraction 0..1.
    """
    n = int(points)
    if n < 1:
        raise ValueError("points must be >= 1")
    voc = max(0, min(65535, int(round(voc_code))))
    p = max(1.0, float(sharpness))
    out: List[int] = []
    for i in range(n):
        x = i / (n - 1) if n > 1 else 0.0
        v = int(round(voc * (1.0 - x ** p)))
        out.append(0 if v < 0 else 65535 if v > 65535 else v)
    return out


def encode_curve_b64url(codes: Sequence[int]) -> str:
    """Encode 16-bit DAC codes as little-endian base64url (no padding) for the ``curve`` field.

    Mirrors the UI's ``encodeCurveB64Url``; the firmware's ``b64url_decode`` tolerates the missing
    padding. Values are clamped to 0..65535.
    """
    buf = bytearray(len(codes) * 2)
    for i, c in enumerate(codes):
        c = 0 if c < 0 else 65535 if c > 65535 else int(c)
        buf[i * 2] = c & 0xFF
        buf[i * 2 + 1] = (c >> 8) & 0xFF
    return base64.urlsafe_b64encode(bytes(buf)).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class IVPoint:
    """One live operating point of the running loop: ``i`` = latest ADC reading (the loop's input
    "current"), ``v`` = the DAC code it drove this tick (the "voltage"). Both are raw codes."""

    i: int
    v: int

    @property
    def current_code(self) -> int:
        return self.i

    @property
    def voltage_code(self) -> int:
        return self.v


class ControlLoopHandle:
    """A running in-fabric control loop. Poll it with :meth:`probe`; stop with :meth:`stop`.

    Use as a context manager so the loop (and the DAC drive) is stopped on exit::

        curve = benchpod.build_panel_curve(voc_code=52000, sharpness=6)
        with bp.control_loop(curve=curve, vmax=52000) as loop:
            pt = loop.probe()            # IVPoint(i=<ADC>, v=<DAC>)
    """

    def __init__(self, *, probe: Callable[[], IVPoint], stop: Callable[[], Any],
                 data: Optional[Dict[str, Any]] = None) -> None:
        self._probe = probe
        self._stop = stop
        self._stopped = False
        d = data or {}
        self.armed = bool(d.get("armed", True))
        self.k = int(d.get("k", DEFAULT_K))
        self.vmin = int(d.get("vmin", DEFAULT_VMIN))
        self.vmax = int(d.get("vmax", DEFAULT_VMAX))
        self.tick_div = int(d.get("tick_div", DEFAULT_TICK_DIV))
        self.curve_pts = int(d.get("curve_pts", 0))
        self.data = d

    def probe(self) -> IVPoint:
        """Poll the loop once, returning its live :class:`IVPoint`."""
        return self._probe()

    def stop(self) -> Any:
        """Stop the loop (and the DAC drive). Idempotent."""
        if self._stopped:
            return None
        self._stopped = True
        return self._stop()

    def __enter__(self) -> "ControlLoopHandle":
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (f"ControlLoopHandle(armed={self.armed}, k={self.k}, "
                f"vmin={self.vmin}, vmax={self.vmax}, tick_div={self.tick_div}, "
                f"curve_pts={self.curve_pts})")
