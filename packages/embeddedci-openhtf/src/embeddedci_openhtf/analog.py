"""Analog stimulus/acquisition helpers for OpenHTF phases.

The BenchPod's analog path is a DAC output and an ADC input on the iCE40 FPGA.
The SDK exposes the acquisition side as ``BenchPod.capture(...)`` and everything
else through raw JSON commands; these helpers wrap the firmware's ``generate``
(DAC waveform), ``measure`` (synchronized DAC+ADC loopback), and ``capture``
(ADC snapshot) commands, and the phase factories turn the captured samples into
OpenHTF measurements via :func:`embeddedci_openhtf.record_samples`.

**TCP only.** These commands need the JSON/sample channel, which the serial
console does not provide — they raise ``BenchPodError`` on a serial connection.

The ``bench`` argument to every helper is a connected :class:`BenchPod` *or* a
:class:`~embeddedci_openhtf.BenchPodPlug` (the plug proxies the SDK methods), so
inside a phase you can pass the injected ``bench`` straight through::

    @htf.plug(bench=benchpod_plug("192.168.1.50:8080"))
    def stim(test, bench):
        signal_generate(bench, waveform="sine", freq=1000, amplitude=100, offset=128)
"""

from __future__ import annotations

from typing import Any, List, Optional, Union

import openhtf as htf

from embeddedci.benchpod.errors import BenchPodError

from .measurements import record_samples

__all__ = [
    "signal_generate",
    "signal_stop",
    "measure",
    "signal_generate_phase",
    "adc_capture_phase",
    "loopback_measure_phase",
]

_Range = Optional[tuple]  # (min, max) inclusive, or None for no limit


# -- low-level helpers (operate on a BenchPod or BenchPodPlug) ---------------

def signal_generate(bench: Any, *, waveform: str, freq: float, amplitude: float,
                    offset: float = 128.0, duration_ms: Optional[int] = None,
                    sample_rate_mhz: Optional[float] = None) -> Any:
    """Start a DAC waveform (``sine``/``square``/``sawtooth``/...).

    ``amplitude`` is 0-127 and ``offset`` 0-255 (firmware DAC units). With
    ``duration_ms`` unset/0 the waveform runs until the next command (e.g.
    :func:`signal_stop` or a ``capture``).
    """
    req: dict = {"cmd": "generate", "waveform": waveform, "freq": freq,
                 "amplitude": amplitude, "offset": offset}
    if duration_ms is not None:
        req["duration_ms"] = duration_ms
    if sample_rate_mhz is not None:
        req["sample_rate_mhz"] = sample_rate_mhz
    return bench.command(req)


def signal_stop(bench: Any) -> Any:
    """Stop a free-running DAC waveform."""
    return bench.command({"cmd": "dac_stop"})


def measure(bench: Any, *, waveform: str, freq: float, amplitude: float,
            offset: float = 128.0, samples: int = 256,
            sample_rate_mhz: Optional[float] = None) -> List[int]:
    """Drive the DAC and capture the ADC in one synchronized command (loopback).

    Returns the reassembled ADC sample array. Use for round-trip checks where the
    DAC output is wired (directly or through a DUT) back to the ADC input.
    """
    # measure returns a (possibly chunked) sample array, so it needs the
    # transport's chunk-reassembling `samples` path, not plain `command`.
    transport = getattr(bench, "transport", None)
    fn = getattr(transport, "samples", None)
    if fn is None:
        raise BenchPodError("measure is only available on the TCP transport")
    req: dict = {"cmd": "measure", "waveform": waveform, "freq": freq,
                 "amplitude": amplitude, "offset": offset, "samples": samples}
    if sample_rate_mhz is not None:
        req["sample_rate_mhz"] = sample_rate_mhz
    return fn(req)


# -- phase factories ---------------------------------------------------------

def _stat_measures(prefix: str, *, mean_range: _Range, pp_range: _Range,
                   min_range: _Range, max_range: _Range) -> list:
    """Declare min/max/mean/pp measurements, applying any ranges as limits."""
    spec = {"min": min_range, "max": max_range, "mean": mean_range, "pp": pp_range}
    out = []
    for suffix, rng in spec.items():
        meas = htf.Measurement(f"{prefix}_{suffix}")
        if rng is not None:
            meas = meas.in_range(rng[0], rng[1])
        out.append(meas)
    return out


def signal_generate_phase(plug: type, *, waveform: str, freq: float,
                          amplitude: float, offset: float = 128.0,
                          duration_ms: Optional[int] = None,
                          sample_rate_mhz: Optional[float] = None,
                          name: str = "signal_generate") -> object:
    """A setup phase that starts a DAC waveform and continues.

    With ``duration_ms`` unset the waveform free-runs; pair it with a later
    :func:`adc_capture_phase` (sampling a different channel) and stop it with
    ``signal_stop`` in a teardown phase if needed.
    """

    @htf.PhaseOptions(name=name)
    @htf.plug(bench=plug)
    def _gen(test, bench):
        signal_generate(bench, waveform=waveform, freq=freq, amplitude=amplitude,
                        offset=offset, duration_ms=duration_ms,
                        sample_rate_mhz=sample_rate_mhz)
        test.logger.info("DAC %s @ %g Hz, amp %g, offset %g", waveform, freq,
                         amplitude, offset)

    return _gen


def adc_capture_phase(plug: type, *, samples: int = 4096,
                      sample_rate_mhz: Optional[float] = None,
                      prefix: str = "adc", mean_range: _Range = None,
                      pp_range: _Range = None, min_range: _Range = None,
                      max_range: _Range = None, name: str = "adc_capture") -> object:
    """A phase that snapshots the ADC and records min/max/mean/pp.

    Pass any of ``mean_range`` / ``pp_range`` / ``min_range`` / ``max_range`` as
    ``(low, high)`` to turn that stat into a pass/fail limit; the raw samples are
    always attached as ``adc.json``.
    """

    @htf.PhaseOptions(name=name)
    @htf.measures(*_stat_measures(prefix, mean_range=mean_range, pp_range=pp_range,
                                  min_range=min_range, max_range=max_range))
    @htf.plug(bench=plug)
    def _cap(test, bench):
        data = bench.capture(samples, sample_rate_mhz=sample_rate_mhz)
        stats = record_samples(test, data, prefix=prefix)
        test.logger.info("ADC %d samples: min=%s max=%s mean=%.1f pp=%s",
                         len(data), stats[f"{prefix}_min"], stats[f"{prefix}_max"],
                         stats[f"{prefix}_mean"], stats[f"{prefix}_pp"])

    return _cap


def loopback_measure_phase(plug: type, *, waveform: str, freq: float,
                           amplitude: float, offset: float = 128.0,
                           samples: int = 4096,
                           sample_rate_mhz: Optional[float] = None,
                           prefix: str = "adc", mean_range: _Range = None,
                           pp_range: _Range = None, min_range: _Range = None,
                           max_range: _Range = None,
                           name: str = "loopback_measure") -> object:
    """A phase that drives the DAC and captures the ADC together (loopback), then
    records min/max/mean/pp.

    The canonical analog self-test: stimulate with a known waveform and assert
    the round-trip amplitude/offset, e.g. ``pp_range=(180, 255)`` for a healthy
    signal path. Raw samples are attached as ``adc.json``.
    """

    @htf.PhaseOptions(name=name)
    @htf.measures(*_stat_measures(prefix, mean_range=mean_range, pp_range=pp_range,
                                  min_range=min_range, max_range=max_range))
    @htf.plug(bench=plug)
    def _meas(test, bench):
        data = measure(bench, waveform=waveform, freq=freq, amplitude=amplitude,
                       offset=offset, samples=samples,
                       sample_rate_mhz=sample_rate_mhz)
        stats = record_samples(test, data, prefix=prefix)
        test.logger.info("loopback %s @ %g Hz -> %d samples: mean=%.1f pp=%s",
                         waveform, freq, len(data), stats[f"{prefix}_mean"],
                         stats[f"{prefix}_pp"])

    return _meas
