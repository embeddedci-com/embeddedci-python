"""In-fabric DAC control loop — a deterministic, tabulated transfer function.

The iCE40 runs the loop entirely in fabric: each tick it takes an INPUT, looks that value up in
a reloadable 2048-entry curve LUT (``out = curve[in >> 5]``), damps toward that target and clamps
to ``[vmin, vmax]``, then drives the DAC. Any transfer function you can tabulate works; a
solar-panel I-V table is one preset (:func:`build_panel_curve`), not what the feature is.

Where the input comes from is selectable (gateware >= v29,
:attr:`Capabilities.dac_loop_sources`):

``"adc"``
    The live ADC — a real CLOSED loop whose output reacts to the DUT. The default, and the only
    source on older gateware.
``"fixed"``
    A host-held constant — OPEN loop, one point of the curve at a time, with the ADC and the
    whole analog input path out of the picture. This is how the DAC and output stage are
    validated on their own: hold a point, meter the output, compare it with
    :func:`curve_output_at`. Step to the next point with
    :meth:`~embeddedci.benchpod.client.BenchPod.loop_input` — no re-arm, no curve re-upload, so
    consecutive readings are comparable.
``"sweep"``
    The input advances by ``step`` every tick — OPEN loop, a function of TIME that walks the
    whole curve at a deterministic rate, again with no ADC involved.

Only the **loop gateware image** provides the loop at all
(:attr:`Capabilities.dac_control_loop`); use
:meth:`~embeddedci.benchpod.client.BenchPod.fpga_image` to switch a device onto it.

This module holds the transport-independent pieces — the curve builders/encoder (a faithful port
of the web UI's ``controlLoopCurve.ts``) and the :class:`ControlLoopHandle`. The commands
(``dac_control_loop`` / ``dac_loop_input`` / ``dac_loop_probe`` / ``dac_stop``) live on
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

#: Entries in the gateware curve LUT (``dac_loop.v``: the 16-bit input >> 5).
CURVE_LUT_ENTRIES = 2048

#: Largest 16-bit code — full scale on both the input and the output axis.
CODE_MAX = 65535

#: The loop input sources the firmware accepts for the ``source`` field.
LOOP_SOURCES = ("adc", "fixed", "sweep")


def normalise_loop_source(source: Optional[str], step: int) -> Optional[str]:
    """Validate a loop input ``source`` against the firmware's own guard.

    An unknown name is REJECTED rather than falling back to the ADC: a caller that asked for an
    open-loop run and silently got a closed one would be metering a value the loop never
    produced. A ``"sweep"`` with ``step`` 0 is rejected for the same reason — the input would
    never advance, so it is a fixed point wearing a moving name
    (``bench-pod-firmware/stm32h563/src/dac_loop_params.c``). Returns the source unchanged
    (``None`` stays ``None`` = leave it to the device default, the ADC).
    """
    if source is None:
        return None
    if source not in LOOP_SOURCES:
        raise ValueError(f"unknown loop input source {source!r}: use one of {LOOP_SOURCES}")
    if source == "sweep" and int(step) < 1:
        raise ValueError("a sweep source needs a step of at least 1 — a step of 0 never advances")
    return source


def curve_output_at(curve: Sequence[int], input_code: int) -> int:
    """The output the hardware will drive for ``input_code`` — the number to compare a meter
    reading against in the open-loop (fixed-input) test.

    Walks the exact two steps the hardware does, so it cannot drift from it: the firmware
    nearest-neighbour upsamples the uploaded ``len(curve)`` points into the 2048-entry LUT (entry
    j takes source point ``j*N//2048``) and the gateware then reads ``LUT[input >> 5]``. Ignores
    damping and the clamp — this is the curve TARGET, which is where the loop settles.
    """
    if not curve:
        return 0
    code = max(0, min(CODE_MAX, int(input_code)))
    src = ((code >> 5) * len(curve)) // CURVE_LUT_ENTRIES
    return int(curve[min(src, len(curve) - 1)])


def input_percent_to_code(percent: float) -> int:
    """A percentage of the input range (0..100) as the 16-bit input code the loop takes."""
    c = int(round((float(percent) / 100.0) * CODE_MAX))
    return 0 if c < 0 else CODE_MAX if c > CODE_MAX else c


def build_constant_curve(value_code: int, points: int = CURVE_POINTS) -> List[int]:
    """A FLAT curve: the same output for every input. The simplest check that the output stage
    is right — pair it with ``source="fixed"`` and a multimeter."""
    v = max(0, min(CODE_MAX, int(round(value_code))))
    return [v] * max(1, int(points))


def build_linear_curve(max_code: int, rising: bool = True, points: int = CURVE_POINTS) -> List[int]:
    """A straight line across the input range: 0 -> ``max_code`` (``rising``) or the reverse."""
    n = max(1, int(points))
    top = max(0, min(CODE_MAX, int(round(max_code))))
    out: List[int] = []
    for i in range(n):
        x = i / (n - 1) if n > 1 else 0.0
        v = int(round(top * (x if rising else 1.0 - x)))
        out.append(0 if v < 0 else CODE_MAX if v > CODE_MAX else v)
    return out


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
    """One live operating point of the running loop, in raw codes.

    ``i`` is the latest ADC reading and ``v`` the DAC code the loop drove this tick.
    ``input_code`` is what the loop's last tick ACTUALLY indexed the curve with — equal to ``i``
    in a closed-loop run, but in a fixed/sweep run the ADC is not in the path at all and ``i`` is
    a reading of something nothing is using, so plot and assert against ``input_code``.
    ``source`` names the input source the device reports (``None`` on firmware without it).
    """

    i: int
    v: int
    input_code: Optional[int] = None
    source: Optional[str] = None

    @property
    def current_code(self) -> int:
        return self.i

    @property
    def voltage_code(self) -> int:
        return self.v

    @property
    def loop_input(self) -> int:
        """The loop's own input — ``input_code`` when the device reports it, else the ADC (which
        IS the input on firmware predating the selectable source)."""
        return self.i if self.input_code is None else self.input_code


class ControlLoopHandle:
    """A running in-fabric control loop. Poll it with :meth:`probe`; stop with :meth:`stop`.

    Use as a context manager so the loop (and the DAC drive) is stopped on exit::

        curve = benchpod.build_panel_curve(voc_code=52000, sharpness=6)
        with bp.control_loop(curve=curve, vmax=52000) as loop:
            pt = loop.probe()            # IVPoint(i=<ADC>, v=<DAC>)
    """

    def __init__(self, *, probe: Callable[[], IVPoint], stop: Callable[[], Any],
                 data: Optional[Dict[str, Any]] = None,
                 set_input: Optional[Callable[..., Any]] = None) -> None:
        self._probe = probe
        self._stop = stop
        self._set_input = set_input
        self._stopped = False
        d = data or {}
        self.armed = bool(d.get("armed", True))
        self.k = int(d.get("k", DEFAULT_K))
        self.vmin = int(d.get("vmin", DEFAULT_VMIN))
        self.vmax = int(d.get("vmax", DEFAULT_VMAX))
        self.tick_div = int(d.get("tick_div", DEFAULT_TICK_DIV))
        self.curve_pts = int(d.get("curve_pts", 0))
        #: Input source the device armed with (``None`` on firmware without selectable sources).
        self.source = d.get("source")
        self.input_code = int(d.get("input", 0))
        self.step = int(d.get("step", 0))
        self.data = d

    def probe(self) -> IVPoint:
        """Poll the loop once, returning its live :class:`IVPoint`."""
        return self._probe()

    def set_input(self, input_code: Optional[int] = None, *,
                  source: Optional[str] = None, step: Optional[int] = None) -> dict:
        """Re-target the RUNNING loop's input without re-arming or re-uploading the curve.

        The open-loop stepping flow: hold a point, meter the output, move to the next. Omitted
        fields keep their current value. Needs :attr:`Capabilities.dac_loop_sources`.
        """
        if self._set_input is None:
            raise RuntimeError("this handle has no input setter")
        data = self._set_input(input_code=input_code, source=source, step=step)
        if isinstance(data, dict):
            if data.get("source") is not None:
                self.source = data["source"]
            self.input_code = int(data.get("input", self.input_code))
            self.step = int(data.get("step", self.step))
        return data if isinstance(data, dict) else {}

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
                f"curve_pts={self.curve_pts}, source={self.source!r})")
