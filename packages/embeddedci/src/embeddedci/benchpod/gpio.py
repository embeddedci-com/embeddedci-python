"""GPIO on the LA pins, and which function owns each pin.

Each of the pod's 14 LA channels has exactly one function at a time:

* ``none`` — the default "LA mode": high-Z, watched by captures;
* ``gpio`` — an input, a push-pull output or an open-drain output you control;
* a peripheral that claimed it: ``uart_rx``/``uart_tx`` (a UART session), ``swd_clk``/``swd_dio``
  (flashing), ``i2c_sda``/``i2c_scl`` (sensor emulation), ``step``/``step_dir`` (a pulse train).

The pod refuses a second function on a pin that is in use
(:class:`~embeddedci.benchpod.errors.PinConflictError`) and a bias resistor that would fight the
function (:class:`~embeddedci.benchpod.errors.PullConflictError`) — so release a GPIO pin before
starting a UART session on it. Captures observe every pin whatever its function.

These are the pod's LA pins on the iCE40, driven at the LA I/O voltage through 330 Ω — not STM32 GPIOs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .client import BenchPod
    from .constants import GpioMode
    from .wiring import Signal


@dataclass(frozen=True)
class LaPinState:
    """One LA channel as the pod reports it (:meth:`BenchPod.la_pins`)."""

    la: int
    #: ``none``, ``gpio``, ``uart_rx``, ``uart_tx``, ``swd_clk``, ``swd_dio``, ``i2c_sda``, ``i2c_scl``,
    #: ``step`` or ``step_dir``.
    function: str
    #: The GPIO mode (``input``/``output``/``open_drain``) when ``function`` is ``gpio``.
    gpio: Optional[str] = None
    #: The commanded level of a GPIO output or open-drain pin.
    level: Optional[int] = None
    #: ``"up"``, ``"down"`` or ``None`` (LA9-LA14 have no bias resistor).
    pull: Optional[str] = None
    pull_ohms: Optional[str] = None
    pull_on: bool = False
    raw: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def in_use(self) -> bool:
        """True when a function other than ``none`` owns the pin."""
        return self.function != "none"

    @classmethod
    def from_reply(cls, entry: Any) -> "LaPinState":
        e = entry if isinstance(entry, dict) else {}
        pull = e.get("pull") if isinstance(e.get("pull"), dict) else {}
        level = e.get("level")
        return cls(la=int(e.get("la", 0) or 0), function=str(e.get("function") or "none"),
                   gpio=e.get("gpio") or None, level=None if level is None else int(level),
                   pull=pull.get("dir"), pull_ohms=pull.get("ohms"), pull_on=bool(pull.get("on", False)),
                   raw=dict(e))


class GpioPin:
    """One LA channel used as GPIO. Get one from :meth:`BenchPod.gpio` (configures it) or
    :meth:`BenchPod.signal` (a named signal from the wiring profile)::

        trigger = bp.signal("TRIGGER")
        trigger.configure()                  # the signal's wiring direction, starting inactive
        trigger.pulse(0.005)                 # FPGA-timed 5 ms pulse
        assert bp.signal("READY").wait_for(1, timeout=2.0)
    """

    def __init__(self, pod: "BenchPod", la: int, *, signal: Optional["Signal"] = None) -> None:
        self._pod = pod
        self.la = la
        #: The wiring profile's signal, when the pin came from :meth:`BenchPod.signal`.
        self.signal = signal

    @property
    def name(self) -> str:
        return self.signal.name if self.signal else f"LA{self.la}"

    def configure(self, mode: Optional["GpioMode"] = None, *, level: Optional[int] = None) -> LaPinState:
        """Put the pin in GPIO ``mode``: ``input``, ``output`` (push-pull) or ``open_drain``.

        Without ``mode`` a signal's wiring direction decides (``output``/``open_drain`` as named,
        anything else ``input``), and a driven pin starts at its inactive level.
        """
        if mode is None:
            direction = self.signal.direction if self.signal else "input"
            mode = direction if direction in ("output", "open_drain") else "input"  # type: ignore[assignment]
            if level is None and mode != "input":
                level = self._level(active=False)
        return self._pod._gpio_configure([self.la], mode, level)[0]  # type: ignore[arg-type]

    def set(self, level: int) -> None:
        """Drive a GPIO output (or, open-drain, pull low with 0 and release with 1)."""
        self._pod.set_gpio(self.la, level)

    def high(self) -> None:
        self.set(1)

    def low(self) -> None:
        self.set(0)

    def activate(self) -> None:
        """Set the pin to its active level (low for an ``active_low`` signal)."""
        self.set(self._level(active=True))

    def deactivate(self) -> None:
        self.set(self._level(active=False))

    def read(self) -> int:
        """The pin's live level (0/1), whatever its function."""
        return self._pod.read_gpio(self.la)

    def is_active(self) -> bool:
        return self.read() == self._level(active=True)

    def wait_for(self, level: int, *, timeout: float, poll: float = 0.005) -> bool:
        """Wait until the pin reads ``level``; ``False`` if ``timeout`` seconds pass first."""
        return self._pod.wait_for_level(self.la, level, timeout=timeout, poll=poll)

    def pulse(self, width: float, *, count: int = 1) -> None:
        """Emit ``count`` high pulses of ``width`` seconds (``width`` low between them), timed by the
        FPGA; returns immediately. The pin must be a GPIO output at level 0 (it goes back to its GPIO
        level afterwards) or unused."""
        self._pod.la_step(self.la, steps=count, delay=width)

    def release(self) -> None:
        """Stop using the pin as GPIO: it goes back to high-Z ("LA mode")."""
        self._pod.release_gpio(self.la)

    def state(self) -> LaPinState:
        """The pin's current :class:`LaPinState`."""
        for pin in self._pod.la_pins():
            if pin.la == self.la:
                return pin
        return LaPinState(la=self.la, function="none")

    def _level(self, *, active: bool) -> int:
        active_low = bool(self.signal and self.signal.active_low)
        return int(active != active_low)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"GpioPin({self.name}, la={self.la})"
