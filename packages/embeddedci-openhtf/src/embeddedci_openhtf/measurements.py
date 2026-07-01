"""Helpers that map BenchPod SDK result objects onto OpenHTF measurements and
attachments.

OpenHTF wants measurements *declared* on a phase (``@htf.measures(...)``) and
*set* inside it (``test.measurements.x = ...``). These helpers give you the
matching declaration/record pair for the common BenchPod results so you don't
hand-roll the boilerplate:

    @htf.measures(flash_ok_measurement())
    @htf.plug(bench=benchpod_plug("192.168.1.50:8080"))
    def flash(test, bench):
        result = bench.flash(file="fw.elf", target="target/stm32f4x.cfg",
                             swclk=11, swdio=12, nreset=3, check=False)
        record_flash(test, result)            # sets flash_ok + attaches the log

The recorders never raise on a bad result — they record the failing value so the
measurement validator (not an exception) decides the phase outcome, and the
OpenOCD / UART output is always attached for triage.
"""

from __future__ import annotations

import json
from typing import List, Optional

import openhtf as htf

from embeddedci.benchpod.flash import FlashResult
from embeddedci.benchpod.uart import UartCapture

__all__ = [
    "flash_ok_measurement",
    "uart_matched_measurement",
    "record_flash",
    "record_uart",
    "record_samples",
]


# -- measurement declarations ------------------------------------------------

def flash_ok_measurement(name: str = "flash_ok") -> htf.Measurement:
    """A boolean measurement that passes only when the flash succeeded."""
    return htf.Measurement(name).equals(True).doc(
        "OpenOCD programmed and verified the target over the pod's SWD probe"
    )


def uart_matched_measurement(name: str = "boot_ok") -> htf.Measurement:
    """A boolean measurement that passes when the UART ``until`` pattern hit."""
    return htf.Measurement(name).equals(True).doc(
        "Expected pattern was seen on the DUT's UART within the capture window"
    )


# -- recorders ---------------------------------------------------------------

def record_flash(test: htf.TestApi, result: FlashResult, *,
                 name: str = "flash_ok", attachment: str = "openocd.log") -> bool:
    """Record a :class:`FlashResult`: set the ``name`` measurement to ``result.ok``
    and attach the OpenOCD output. Returns ``result.ok``."""
    log = (result.stdout or "")
    if getattr(result, "stderr", ""):
        log = f"{log}\n----- stderr -----\n{result.stderr}"
    test.attach(attachment, log.encode("utf-8", "replace"), mimetype="text/plain")
    test.measurements[name] = bool(result.ok)
    return bool(result.ok)


def record_uart(test: htf.TestApi, capture: UartCapture, *,
                name: Optional[str] = "boot_ok",
                attachment: str = "uart.txt") -> bool:
    """Record a :class:`UartCapture`: attach the captured text and, when ``name``
    is given, set that measurement to ``capture.matched``. Returns ``matched``."""
    test.attach(attachment, capture.text.encode("utf-8", "replace"),
                mimetype="text/plain")
    if name is not None:
        test.measurements[name] = bool(capture.matched)
    return bool(capture.matched)


def record_samples(test: htf.TestApi, samples: List[int], *,
                   prefix: str = "adc", attachment: str = "adc.json") -> dict:
    """Attach raw ADC/LA ``samples`` (from ``bench.capture(...)`` / ``measure``)
    as JSON and set ``<prefix>_min`` / ``_max`` / ``_mean`` / ``_pp``
    (peak-to-peak) measurements.

    Declare the matching measurements on the phase (e.g.
    ``htf.Measurement('adc_pp').in_range(...)``) to turn them into limits; only
    declared ones are set. Returns the computed stats dict.
    """
    test.attach(attachment, json.dumps(samples).encode("utf-8"),
                mimetype="application/json")
    lo = min(samples) if samples else 0
    hi = max(samples) if samples else 0
    stats = {
        f"{prefix}_min": lo,
        f"{prefix}_max": hi,
        f"{prefix}_mean": (sum(samples) / len(samples)) if samples else 0.0,
        f"{prefix}_pp": hi - lo,
    }
    for key, value in stats.items():
        # Only set measurements the phase actually declared; ignore the rest so
        # callers can opt into just the stats they care about.
        try:
            test.measurements[key] = value
        except Exception:
            pass
    return stats
