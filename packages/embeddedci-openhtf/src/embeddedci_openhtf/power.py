"""Power-profile helpers for OpenHTF phases: what the DUT actually draws.

The pod samples its target-power rail's INA238 monitor a few hundred times a second (ask for
100-500 Hz; it flattens near 365 Hz and reports the rate it achieved), and every sample is
timestamped, so average, minimum and peak current, bus voltage, energy and charge are integrated
over real time rather than estimated from snapshots — which is what a "does this firmware meet its
sleep budget?" test needs.

**Units are SI**: ``duration`` seconds, currents amps, voltages volts, ``energy`` joules,
``charge`` coulombs, ``rate_hz`` hertz. Recorded measurements carry those units. Invalid arguments
raise :class:`ValueError`.
"""

from __future__ import annotations

import json
from typing import Any, Optional, Union

import openhtf as htf

from embeddedci.benchpod import PowerProfile
from embeddedci.benchpod.constants import Efuse

from .analog import _Range, _check_range

__all__ = ["measure_power", "measure_power_phase"]


# -- low-level helper (operates on a BenchPod or BenchPodPlug) ---------------

def measure_power(bench: Any, duration: float, **kwargs: Any) -> PowerProfile:
    """Profile a target-power rail for ``duration`` seconds and return the
    :class:`~embeddedci.benchpod.PowerProfile`.

    Keyword arguments go to :meth:`BenchPod.measure_power
    <embeddedci.benchpod.BenchPod.measure_power>`: ``efuse`` (default the wiring profile's rail),
    ``rate_hz`` (100-500) and ``keep_samples`` (up to 4096 ``(t, amps, volts)`` points).
    """
    return bench.measure_power(duration, **kwargs)


# -- phase factory -----------------------------------------------------------

def measure_power_phase(plug: type, *, duration: float,
                        efuse: Optional[Union[Efuse, int]] = None, rate_hz: float = 500.0,
                        keep_samples: int = 0, avg_current_range: _Range = None,
                        peak_current_range: _Range = None, energy_range: _Range = None,
                        prefix: str = "power", attachment: Optional[str] = "power.json",
                        name: str = "measure_power") -> object:
    """A phase that profiles the target's supply for ``duration`` seconds and records it.

    Records ``<prefix>_avg_current_a`` and ``<prefix>_peak_current_a`` (units A),
    ``<prefix>_avg_voltage_v`` (V) and ``<prefix>_energy_j`` (J). Pass ``avg_current_range``,
    ``peak_current_range`` (amps) or ``energy_range`` (joules) as ``(low, high)`` for pass/fail
    limits — e.g. ``avg_current_range=(0, 0.02)`` for a 20 mA sleep budget.

    ``efuse`` defaults to the wiring profile's rail, ``rate_hz`` is the sample rate the pod aims
    for (it reports the rate it achieved). With ``keep_samples`` above 0 the profile also carries a
    bin-averaged ``(t, amps, volts)`` trace, attached as ``attachment`` JSON (``None`` to skip).
    Whatever the DUT should be doing while it is measured — booting, sleeping, transmitting — start
    it in an earlier phase.
    """
    if duration <= 0:
        raise ValueError(f"duration must be > 0 seconds, got {duration!r}")
    _check_range("avg_current_range", avg_current_range)
    _check_range("peak_current_range", peak_current_range)
    _check_range("energy_range", energy_range)
    measures = [
        _measure(f"{prefix}_avg_current_a", "A", avg_current_range),
        _measure(f"{prefix}_peak_current_a", "A", peak_current_range),
        _measure(f"{prefix}_avg_voltage_v", "V", None),
        _measure(f"{prefix}_energy_j", "J", energy_range),
    ]

    @htf.PhaseOptions(name=name)
    @htf.measures(*measures)
    @htf.plug(bench=plug)
    def _power(test, bench):
        profile = measure_power(bench, duration, efuse=efuse, rate_hz=rate_hz,
                                keep_samples=keep_samples)
        test.measurements[f"{prefix}_avg_current_a"] = profile.avg_current
        test.measurements[f"{prefix}_peak_current_a"] = profile.peak_current
        test.measurements[f"{prefix}_avg_voltage_v"] = profile.avg_voltage
        test.measurements[f"{prefix}_energy_j"] = profile.energy
        if attachment and profile.samples:
            payload = {"efuse": profile.efuse, "rate_hz": profile.rate_hz,
                       "adc_rate_hz": profile.adc_rate_hz, "n": profile.n,
                       "duration": profile.duration,
                       "samples": [list(s) for s in profile.samples]}
            test.attach(attachment, json.dumps(payload).encode("utf-8"),
                        mimetype="application/json")
        if profile.fault:
            test.logger.error("the eFuse tripped during the profile (over-current or short)")
        if profile.truncated:
            test.logger.warning("the profile stopped at the pod's limit before %g s", duration)
        test.logger.info("power on eFuse %d over %.3f s: avg %.4f A, peak %.4f A, %.4f V, %.4f J",
                         profile.efuse, profile.duration, profile.avg_current,
                         profile.peak_current, profile.avg_voltage, profile.energy)
        return htf.PhaseResult.CONTINUE

    return _power


def _measure(name: str, units: str, rng: _Range):
    meas = htf.Measurement(name).with_units(units)
    return meas if rng is None else meas.in_range(rng[0], rng[1])
