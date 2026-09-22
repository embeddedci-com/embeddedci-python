"""LA-pin helpers for OpenHTF phases: GPIO, and timing between two channels.

The pod's 14 logic-analyzer channels (LA1-LA14) are its only pins, and each has exactly one
function at a time — high-Z "LA mode" by default, or claimed by GPIO, a UART proxy, SWD, the
emulated I2C sensor or a step train. :func:`gpio` claims a channel so the pod can drive or read it;
:func:`release_gpio` gives it back. Claiming a channel another function owns raises
:class:`~embeddedci.benchpod.errors.PinConflictError`, whose message names the owner, so release a
GPIO channel before a UART session, a flash or sensor emulation uses it. Captures observe every
channel whatever owns it, which is what :func:`la_delay` measures with.

``la`` arguments are an LA channel (1-14 or a :class:`~embeddedci.benchpod.Pin`) or a name from the
bench's wiring profile — ``benchpod_plug("192.168.1.50", wiring="bench.json")`` then
``gpio(bench, "TRIGGER")``.

**Units are seconds** (pulse widths, delays), as everywhere in the SDK. Invalid arguments raise
:class:`ValueError`.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence, Union

import openhtf as htf

from embeddedci.benchpod import Edge, GpioMode, GpioPin, LaCapture, Trigger
from embeddedci.benchpod.constants import Pin

from .analog import _Range, _check_range

__all__ = [
    "gpio",
    "set_gpio",
    "read_gpio",
    "release_gpio",
    "la_delay",
    "gpio_phase",
    "la_delay_phase",
]

#: An LA channel: 1-14, a :class:`Pin`, or a wiring-profile name.
_LaT = Union[Pin, int, str]


def _channel(bench: Any, la: _LaT) -> int:
    """``la`` as an LA channel number, resolving a wiring-profile name through the bench."""
    return bench.wiring.la(la) if isinstance(la, str) else int(la)


# -- low-level helpers (operate on a BenchPod or BenchPodPlug) ---------------

def gpio(bench: Any, la: _LaT, mode: GpioMode = "output", *,
         level: Optional[int] = None) -> GpioPin:
    """Claim an LA channel as GPIO and return its :class:`~embeddedci.benchpod.GpioPin`.

    ``mode`` is ``output`` (push-pull, starting at ``level``, default 0), ``open_drain`` (0 pulls
    low, 1 releases; starts released) or ``input`` (high-Z, level readable). The channel stays GPIO
    — across disconnects — until :func:`release_gpio`. See
    :meth:`BenchPod.gpio <embeddedci.benchpod.BenchPod.gpio>`.
    """
    return bench.gpio(la, mode, level=level)


def set_gpio(bench: Any, la: Union[_LaT, Sequence[_LaT]], level: int) -> None:
    """Drive a GPIO output / open-drain channel — or several at once — to ``level`` (0 or 1)."""
    bench.set_gpio(la, level)


def read_gpio(bench: Any, la: _LaT) -> int:
    """The live level (0/1) of an LA channel, whatever function owns it."""
    return bench.read_gpio(la)


def release_gpio(bench: Any, *las: _LaT) -> None:
    """Give GPIO channels back to "LA mode" (high-Z). Without arguments, every GPIO channel."""
    bench.release_gpio(*las)


def la_delay(bench: Any, from_la: _LaT, to_la: _LaT, *, samples: int, sample_rate_hz: float,
             from_edge: Edge = "rising", to_edge: Edge = "rising",
             trigger: Optional[Trigger] = None) -> Optional[float]:
    """Capture the logic channels and return the delay in **seconds** from the first ``from_edge``
    on ``from_la`` to the next ``to_edge`` on ``to_la`` — e.g. a trigger pin to a "result ready"
    pin.

    ``None`` when either edge is missing from the capture. Resolution is one sample, so pick
    ``sample_rate_hz`` well above the delay you expect to resolve. ``trigger`` (a
    :class:`~embeddedci.benchpod.Trigger`, gateware v35+) starts the capture on an edge or level
    instead of immediately, so a rare event can be caught at a high rate.
    """
    cap: LaCapture = bench.capture_la(samples, sample_rate_hz=sample_rate_hz, trigger=trigger)
    return cap.delay(_channel(bench, from_la), _channel(bench, to_la),
                     from_edge=from_edge, to_edge=to_edge)


# -- phase factories ---------------------------------------------------------

def gpio_phase(plug: type, *, la: _LaT, mode: GpioMode = "output", level: Optional[int] = None,
               name: str = "gpio") -> object:
    """A setup phase that claims an LA channel as GPIO (and drives ``level`` on an output).

    The channel stays claimed after the phase — drive it from later phases with :func:`set_gpio`,
    and release it with :func:`release_gpio` in a teardown phase (or before a UART session, flash
    or sensor emulation needs that channel).
    """

    @htf.PhaseOptions(name=name)
    @htf.plug(bench=plug)
    def _gpio(test, bench):
        pin = gpio(bench, la, mode, level=level)
        test.logger.info("LA%d is GPIO %s%s", pin.la, mode,
                         "" if level is None else f" at level {level}")

    return _gpio


def la_delay_phase(plug: type, *, from_la: _LaT, to_la: _LaT, samples: int, sample_rate_hz: float,
                   from_edge: Edge = "rising", to_edge: Edge = "rising",
                   trigger: Optional[Trigger] = None, delay_range: _Range = None,
                   name: str = "la_delay") -> object:
    """A phase that measures the delay between two logic channels and records ``la_delay_s``.

    Captures ``samples`` at ``sample_rate_hz`` and records the seconds from the first ``from_edge``
    on ``from_la`` to the next ``to_edge`` on ``to_la`` (units ``"s"``) — a DUT's response time, for
    example. Pass ``delay_range=(low, high)`` in seconds for a pass/fail limit; the measurement is
    left unset (so the phase fails on a limit) when either edge is missing. ``trigger`` starts the
    capture on an LA edge or level.
    """
    _check_range("delay_range", delay_range)
    meas = htf.Measurement("la_delay_s").with_units("s")
    if delay_range is not None:
        meas = meas.in_range(delay_range[0], delay_range[1])

    @htf.PhaseOptions(name=name)
    @htf.measures(meas)
    @htf.plug(bench=plug)
    def _delay(test, bench):
        delay = la_delay(bench, from_la, to_la, samples=samples, sample_rate_hz=sample_rate_hz,
                         from_edge=from_edge, to_edge=to_edge, trigger=trigger)
        if delay is None:
            test.logger.error("no %s edge on %s followed by a %s edge on %s in the capture",
                              from_edge, from_la, to_edge, to_la)
            return htf.PhaseResult.CONTINUE
        test.measurements.la_delay_s = delay
        test.logger.info("%s %s -> %s %s: %.6f s", from_la, from_edge, to_la, to_edge, delay)
        return htf.PhaseResult.CONTINUE

    return _delay
