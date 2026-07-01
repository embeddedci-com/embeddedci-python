"""Ready-made OpenHTF phase factories for common BenchPod steps.

Each factory returns a fully-decorated phase (plug + measurements wired up) so a
test is just a list of them::

    import openhtf as htf
    from embeddedci_openhtf import benchpod_plug, flash_phase, boot_banner_phase

    bench = benchpod_plug("192.168.1.50:8080")   # direct TCP, no cloud

    test = htf.Test(
        flash_phase(bench, file="fw.elf", target="target/stm32f4x.cfg",
                    swclk=11, swdio=12, nreset=3),
        boot_banner_phase(bench, rx=1, tx=2, expect="APP_OK"),
    )
    test.execute(test_start=lambda: "SN-0001")

These are conveniences; for anything custom, write a normal phase with
``@htf.plug(bench=benchpod_plug(...))`` and the recorders in
:mod:`embeddedci_openhtf.measurements`.
"""

from __future__ import annotations

from typing import Optional, Union

import openhtf as htf

from embeddedci.benchpod.constants import Efuse, Pin

from .measurements import (
    flash_ok_measurement,
    record_flash,
    record_uart,
    uart_matched_measurement,
)

__all__ = ["power_phase", "flash_phase", "boot_banner_phase"]

_PinT = Union[Pin, int]


def power_phase(plug: type, *, efuse: Union[Efuse, int] = Efuse.INTERNAL,
                on: bool = True, name: Optional[str] = None) -> object:
    """A phase that powers the target eFuse on (or off)."""

    @htf.PhaseOptions(name=name or ("power_on" if on else "power_off"))
    @htf.plug(bench=plug)
    def _power(test, bench):
        (bench.power_on if on else bench.power_off)(efuse)
        test.logger.info("target power %s (efuse %s)", "on" if on else "off",
                         int(efuse))

    return _power


def flash_phase(plug: type, *, file: str, target: str,
                swclk: _PinT, swdio: _PinT, nreset: Optional[_PinT] = None,
                target_power: Optional[Union[Efuse, int]] = Efuse.INTERNAL,
                name: str = "flash", stop_on_fail: bool = True,
                **flash_kwargs) -> object:
    """A phase that flashes the DUT over SWD and records the result.

    ``swclk``/``swdio``/``nreset`` are LA channels (1-12). Records a ``flash_ok``
    measurement and attaches the OpenOCD log. By default a failed flash stops the
    test (``stop_on_fail``) so later phases don't run against an unprogrammed DUT.
    Extra keyword args pass through to ``BenchPod.flash`` (``verify``,
    ``connect_under_reset``, ``extra_configs``, ...).
    """

    @htf.PhaseOptions(name=name)
    @htf.measures(flash_ok_measurement())
    @htf.plug(bench=plug)
    def _flash(test, bench):
        result = bench.flash(
            file=file, target=target, swclk=swclk, swdio=swdio, nreset=nreset,
            target_power=target_power, check=False, **flash_kwargs,
        )
        ok = record_flash(test, result)
        if not ok:
            test.logger.error("flash failed (openocd rc=%s)", result.returncode)
            if stop_on_fail:
                return htf.PhaseResult.STOP
        return htf.PhaseResult.CONTINUE

    return _flash


def boot_banner_phase(plug: type, *, rx: _PinT, tx: _PinT, expect,
                      baud: int = 115200, duration: float = 5.0,
                      power_cycle: bool = True,
                      efuse: Union[Efuse, int] = Efuse.INTERNAL,
                      delay: float = 1.0, name: str = "boot",
                      measurement: str = "boot_ok") -> object:
    """A phase that captures the DUT's UART and checks for ``expect``.

    ``rx`` is the LA channel wired to the DUT's TX; ``tx`` drives the DUT's RX.
    ``expect`` is a substring / compiled regex / ``text->bool`` predicate. When
    ``power_cycle`` is true the target is power-cycled so the boot banner lands
    inside the capture window; otherwise it captures the already-powered DUT.
    Records ``measurement`` (True if ``expect`` was seen) and attaches the text.
    """

    @htf.PhaseOptions(name=name)
    @htf.measures(uart_matched_measurement(measurement))
    @htf.plug(bench=plug)
    def _boot(test, bench):
        if power_cycle:
            cap = bench.power_cycle_and_capture(
                rx=rx, tx=tx, efuse=efuse, delay=delay, duration=duration,
                baud=baud, until=expect,
            )
        else:
            cap = bench.capture_uart(rx=rx, tx=tx, baud=baud, duration=duration,
                                     until=expect)
        matched = record_uart(test, cap, name=measurement)
        if not matched:
            test.logger.error("did not see %r on UART within %.1fs", expect, duration)
        return htf.PhaseResult.CONTINUE

    return _boot
