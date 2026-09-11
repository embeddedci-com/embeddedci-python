"""Analog stimulus/acquisition helpers for OpenHTF phases.

The BenchPod's analog front end is a DAC output (routed to the 3V3/5V/12V SMA paths) and a
16-bit ADC input (front SMA, internal DAC loopbacks, amps terminal). These helpers and phase
factories wrap the :class:`~embeddedci.benchpod.BenchPod` analog API and turn its results into
OpenHTF measurements.

**Units are volts, seconds and hertz**, exactly as in the ``embeddedci`` SDK: ``amplitude`` /
``offset`` / ``volts`` are volts, ``duration`` / ``settle`` are seconds, ``freq_hz`` /
``sample_rate_hz`` are hertz, and every recorded analog measurement is in volts (units ``"V"``).
Invalid arguments raise :class:`ValueError`.

The ``bench`` argument to every helper is a connected :class:`~embeddedci.benchpod.BenchPod`
*or* a :class:`~embeddedci_openhtf.BenchPodPlug` (the plug proxies the SDK methods), so inside a
phase you can pass the injected ``bench`` straight through::

    @htf.plug(bench=benchpod_plug("192.168.1.50:8080"))
    def stim(test, bench):
        signal_generate(bench, waveform="sine", freq_hz=1000, amplitude=1.0)   # 1 V peak on 5V path

**Routing order.** Starting a waveform (``generate``) or a DC output (``dac_output``) re-applies
that DAC output path, which opens the internal ``cal1``/``cal2`` loopback relays. Route the ADC
source *after* starting the DAC — which is what :func:`loopback_measure_phase` does.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional, Tuple, Union

import openhtf as htf

from embeddedci.benchpod import (
    AdcReading,
    AdcSource,
    AnalogPath,
    AnalogPathState,
    Capture,
    ControlLoopHandle,
    DacHandle,
    DacOutput,
    DacOutputPath,
    DacPath,
    FpgaImage,
    FpgaImageInfo,
    ReplayHandle,
    ReplayMapping,
    Waveshape,
)
from embeddedci.benchpod.control_loop import (
    DEFAULT_K,
    DEFAULT_TICK_DIV,
    DEFAULT_VMAX,
    DEFAULT_VMIN,
)

__all__ = [
    # low-level helpers
    "signal_generate",
    "signal_stop",
    "analog_path",
    "dac_output",
    "adc_read",
    "adc_capture",
    "replay",
    "replay_waveform",
    "control_loop",
    "fpga_image",
    # phase factories
    "signal_generate_phase",
    "dac_output_phase",
    "adc_read_phase",
    "adc_capture_phase",
    "loopback_measure_phase",
    "control_loop_phase",
    "dac_replay_phase",
]

#: ``(low, high)`` inclusive limits, or ``None`` for no limit.
_Range = Optional[Tuple[float, float]]

#: ADC source -> the named analog path that routes it.
_ADC_SOURCE_PATH: Dict[str, str] = {"ext": "adc_ext", "cal1": "cal1", "cal2": "cal2", "amp": "amp"}


# -- low-level helpers (operate on a BenchPod or BenchPodPlug) ---------------

def signal_generate(bench: Any, *, waveform: Waveshape, freq_hz: float, amplitude: float,
                    offset: Optional[float] = None, dac_path: DacPath = "5v",
                    duration: Optional[float] = None,
                    sample_rate_hz: Optional[float] = None) -> DacHandle:
    """Start a parametric DAC waveform (``sine``/``square``/``sawtooth``) on ``dac_path``.

    ``amplitude`` is the peak and ``offset`` the centre, both in **volts** (``offset`` defaults to
    the middle of the path's range). ``duration`` (seconds) stops it by itself; omitted, it runs
    until :func:`signal_stop` or the returned handle's ``stop()``. See
    :meth:`BenchPod.generate <embeddedci.benchpod.BenchPod.generate>`.
    """
    return bench.generate(waveform, freq_hz=freq_hz, amplitude=amplitude, offset=offset,
                          dac_path=dac_path, duration=duration, sample_rate_hz=sample_rate_hz)


def signal_stop(bench: Any) -> None:
    """Stop any DAC output (generator, replay or control loop). Idempotent."""
    bench.dac_stop()


def analog_path(bench: Any, path: AnalogPath) -> AnalogPathState:
    """Apply a named analog path (``dac_5v``, ``adc_ext``, ``cal1``, ...); see
    :meth:`BenchPod.analog_path <embeddedci.benchpod.BenchPod.analog_path>`."""
    return bench.analog_path(path)


def dac_output(bench: Any, path: DacOutputPath, *, volts: Optional[float] = None) -> DacOutput:
    """Route a DAC output path (``3v3``/``5v``/``12v``/``off``) and optionally drive a calibrated
    DC ``volts``. Returns a :class:`~embeddedci.benchpod.DacOutput` (``path``, ``voltage``,
    ``code``)."""
    return bench.dac_output(path, volts=volts)


def adc_read(bench: Any, source: AdcSource = "ext") -> AdcReading:
    """Route an ADC ``source`` (``ext``/``cal1``/``cal2``/``amp``) and return one calibrated
    :class:`~embeddedci.benchpod.AdcReading` (``voltage`` in volts, ``count``). ``ext`` applies
    the front-SMA divider, so ``voltage`` is the true SMA voltage."""
    return bench.adc_read(source)


def adc_capture(bench: Any, samples: int = 4096, *, sample_rate_hz: Optional[float] = None,
                source: Optional[AdcSource] = None) -> Capture:
    """Capture the ADC and return a :class:`~embeddedci.benchpod.Capture` (calibrated ``volts``
    plus raw ``counts``). ``source`` routes that ADC source first; ``None`` leaves routing alone.
    """
    return bench.capture_adc(samples, sample_rate_hz=sample_rate_hz, source=source)


def replay(bench: Any, source: Any, **kwargs: Any) -> ReplayHandle:
    """Replay a :class:`Capture`, a volts sequence or (``are_codes=True``) DAC codes on the DAC
    (see :meth:`BenchPod.replay <embeddedci.benchpod.BenchPod.replay>`). Loops until stopped."""
    return bench.replay(source, **kwargs)


def replay_waveform(bench: Any, waveform: Any, **kwargs: Any) -> ReplayHandle:
    """Load a cloud-stored waveform (id or ``Waveform``) and replay it on the DAC (needs server
    access, e.g. ``BENCHPOD_API_KEY``). Loops until stopped."""
    return bench.replay_waveform(waveform, **kwargs)


def control_loop(bench: Any, **kwargs: Any) -> ControlLoopHandle:
    """Arm the in-fabric closed-loop DAC controller (panel/MPPT emulator); see
    :meth:`BenchPod.control_loop <embeddedci.benchpod.BenchPod.control_loop>`. Returns a
    :class:`~embeddedci.benchpod.ControlLoopHandle` (``probe()``, ``stop()``, context manager)."""
    return bench.control_loop(**kwargs)


def fpga_image(bench: Any, image: Union[FpgaImage, int]) -> FpgaImageInfo:
    """Switch the iCE40 gateware image (``FpgaImage.LOOP`` / ``FpgaImage.DEEP_REPLAY``; takes
    ~2-3 s). Returns a :class:`~embeddedci.benchpod.FpgaImageInfo`."""
    return bench.fpga_image(image)


# -- measurement plumbing ----------------------------------------------------

_VOLT_STATS = ("mean_v", "pp_v", "rms_v", "min_v", "max_v")


def _check_range(name: str, rng: _Range) -> None:
    if rng is not None and (len(rng) != 2 or rng[0] > rng[1]):
        raise ValueError(f"{name} must be a (low, high) tuple with low <= high, got {rng!r}")


def _check_source(source: Optional[str]) -> None:
    if source is not None and source not in _ADC_SOURCE_PATH:
        raise ValueError(f"source must be one of {sorted(_ADC_SOURCE_PATH)} or None, got {source!r}")


def _volts_measures(prefix: str, ranges: Dict[str, _Range]) -> list:
    """Declare the calibrated-volts stat measurements, applying any ranges as limits."""
    out = []
    for suffix in _VOLT_STATS:
        rng = ranges.get(suffix)
        _check_range(f"{suffix[:-2]}_range", rng)
        meas = htf.Measurement(f"{prefix}_{suffix}").with_units("V")
        if rng is not None:
            meas = meas.in_range(rng[0], rng[1])
        out.append(meas)
    return out


def _record_volts(test: Any, cap: Capture, *, prefix: str, attachment: Optional[str]) -> Dict[str, float]:
    """Set ``<prefix>_{mean,pp,rms,min,max}_v`` from a capture and attach its samples."""
    stats = {
        f"{prefix}_mean_v": cap.mean(),
        f"{prefix}_pp_v": cap.peak_to_peak(),
        f"{prefix}_rms_v": cap.rms(),
        f"{prefix}_min_v": cap.min(),
        f"{prefix}_max_v": cap.max(),
    }
    for key, value in stats.items():
        test.measurements[key] = value
    if attachment:
        payload = {"source": cap.source, "sample_rate_hz": cap.sample_rate_hz,
                   "counts": cap.counts, "volts": cap.volts}
        test.attach(attachment, json.dumps(payload).encode("utf-8"), mimetype="application/json")
    return stats


# -- phase factories ---------------------------------------------------------

def signal_generate_phase(plug: type, *, waveform: Waveshape, freq_hz: float, amplitude: float,
                          offset: Optional[float] = None, dac_path: DacPath = "5v",
                          duration: Optional[float] = None,
                          sample_rate_hz: Optional[float] = None,
                          name: str = "signal_generate") -> object:
    """A setup phase that starts a DAC waveform and continues.

    ``amplitude``/``offset`` are volts on ``dac_path``. With ``duration`` (seconds) unset the
    waveform free-runs past the phase — observe it with a later :func:`adc_capture_phase` and
    stop it with :func:`signal_stop` (e.g. in a teardown phase).
    """

    @htf.PhaseOptions(name=name)
    @htf.plug(bench=plug)
    def _gen(test, bench):
        signal_generate(bench, waveform=waveform, freq_hz=freq_hz, amplitude=amplitude,
                        offset=offset, dac_path=dac_path, duration=duration,
                        sample_rate_hz=sample_rate_hz)
        test.logger.info("DAC %s @ %g Hz on %s: amplitude %g V, offset %s", waveform, freq_hz,
                         dac_path, amplitude,
                         "mid-range" if offset is None else f"{offset:g} V")

    return _gen


def dac_output_phase(plug: type, *, path: DacOutputPath, volts: Optional[float] = None,
                     name: str = "dac_output") -> object:
    """A setup phase that routes a DAC output path (``3v3``/``5v``/``12v``/``off``) and, with
    ``volts``, drives that calibrated DC voltage (the switches flip automatically)."""

    @htf.PhaseOptions(name=name)
    @htf.plug(bench=plug)
    def _out(test, bench):
        out = dac_output(bench, path, volts=volts)
        if out.voltage is None:
            test.logger.info("DAC path %s routed", out.path)
        else:
            test.logger.info("DAC %s -> %.3f V (code %d)", out.path, out.voltage, out.code)

    return _out


def adc_read_phase(plug: type, *, source: AdcSource = "ext", v_range: _Range = None,
                   name: str = "adc_read") -> object:
    """A phase that routes an ADC ``source`` and records the calibrated reading as
    ``<source>_v`` (volts). ``source="ext"`` applies the front-SMA divider. Pass
    ``v_range=(low, high)`` for a pass/fail limit."""
    _check_source(source)
    _check_range("v_range", v_range)
    meas_name = f"{source}_v"
    meas = htf.Measurement(meas_name).with_units("V")
    if v_range is not None:
        meas = meas.in_range(v_range[0], v_range[1])

    @htf.PhaseOptions(name=name)
    @htf.measures(meas)
    @htf.plug(bench=plug)
    def _rd(test, bench):
        r = adc_read(bench, source)
        test.measurements[meas_name] = r.voltage
        test.logger.info("ADC %s = %.4f V (count %d, span %d)", r.source, r.voltage, r.count, r.span)

    return _rd


def adc_capture_phase(plug: type, *, samples: int = 4096, sample_rate_hz: Optional[float] = None,
                      source: Optional[AdcSource] = "ext", prefix: str = "adc",
                      mean_range: _Range = None, pp_range: _Range = None,
                      rms_range: _Range = None, min_range: _Range = None,
                      max_range: _Range = None, attachment: Optional[str] = "adc.json",
                      name: str = "adc_capture") -> object:
    """A phase that captures the ADC and records calibrated **volts** statistics.

    Records ``<prefix>_mean_v`` / ``_pp_v`` / ``_rms_v`` / ``_min_v`` / ``_max_v`` (units V); pass
    any of the matching ``*_range`` arguments as ``(low, high)`` volts to make it a limit, e.g.
    ``mean_range=(3.2, 3.4)`` for a 3.3 V rail. ``source`` routes the ADC first (default the front
    SMA, whose calibration the volts use; ``None`` leaves routing alone). The samples (counts and
    volts) are attached as ``attachment`` JSON unless it is ``None``. Works over any transport.
    """
    _check_source(source)
    measures = _volts_measures(prefix, {"mean_v": mean_range, "pp_v": pp_range,
                                        "rms_v": rms_range, "min_v": min_range,
                                        "max_v": max_range})

    @htf.PhaseOptions(name=name)
    @htf.measures(*measures)
    @htf.plug(bench=plug)
    def _cap(test, bench):
        cap = adc_capture(bench, samples, sample_rate_hz=sample_rate_hz, source=source)
        _record_volts(test, cap, prefix=prefix, attachment=attachment)
        test.logger.info("ADC %d samples @ %.0f Hz: mean=%.4f V pp=%.4f V rms=%.4f V",
                         len(cap), cap.sample_rate_hz, cap.mean(), cap.peak_to_peak(), cap.rms())

    return _cap


def loopback_measure_phase(plug: type, *, waveform: Waveshape = "sine", freq_hz: float,
                           amplitude: float, offset: Optional[float] = None,
                           dac_path: DacPath = "5v", samples: int = 4096,
                           sample_rate_hz: Optional[float] = None,
                           source: Optional[AdcSource] = "ext", settle: float = 0.1,
                           prefix: str = "adc", mean_range: _Range = None,
                           pp_range: _Range = None, rms_range: _Range = None,
                           min_range: _Range = None, max_range: _Range = None,
                           attachment: Optional[str] = "adc.json",
                           name: str = "loopback_measure") -> object:
    """A phase that drives a DAC waveform, captures the ADC while it runs, stops the DAC, and
    records calibrated **volts** statistics — the canonical analog signal-path self-test.

    1. starts ``waveform`` at ``freq_hz`` on ``dac_path`` (``amplitude``/``offset`` in volts);
    2. routes the ADC ``source`` (after the DAC, so a loopback relay is not re-opened), waits
       ``settle`` seconds, and captures ``samples`` at ``sample_rate_hz``;
    3. stops the DAC (also when the capture fails) and records ``<prefix>_mean_v`` / ``_pp_v`` /
       ``_rms_v`` / ``_min_v`` / ``_max_v``.

    Wire the DAC output SMA to the front ADC SMA (directly, or through the DUT whose response you
    are checking) and keep ``source="ext"``. E.g. a 1 V-peak sine centred on 2.5 V straight back
    in: ``pp_range=(1.8, 2.2), mean_range=(2.4, 2.6)``.
    """
    _check_source(source)
    if settle < 0:
        raise ValueError(f"settle must be >= 0 seconds, got {settle!r}")
    measures = _volts_measures(prefix, {"mean_v": mean_range, "pp_v": pp_range,
                                        "rms_v": rms_range, "min_v": min_range,
                                        "max_v": max_range})

    @htf.PhaseOptions(name=name)
    @htf.measures(*measures)
    @htf.plug(bench=plug)
    def _meas(test, bench):
        handle = signal_generate(bench, waveform=waveform, freq_hz=freq_hz, amplitude=amplitude,
                                 offset=offset, dac_path=dac_path)
        try:
            if source is not None:
                bench.analog_path(_ADC_SOURCE_PATH[source])
            if settle:
                time.sleep(settle)
            cap = bench.capture_adc(samples, sample_rate_hz=sample_rate_hz)
        finally:
            handle.stop()
        _record_volts(test, cap, prefix=prefix, attachment=attachment)
        test.logger.info("loopback %s @ %g Hz on %s -> %d samples: mean=%.4f V pp=%.4f V",
                         waveform, freq_hz, dac_path, len(cap), cap.mean(), cap.peak_to_peak())

    return _meas


def control_loop_phase(plug: type, *, voc_code: Optional[int] = None, sharpness: float = 4.0,
                       k: int = DEFAULT_K, vmin: int = DEFAULT_VMIN, vmax: int = DEFAULT_VMAX,
                       tick_div: int = DEFAULT_TICK_DIV, probes: int = 8,
                       v_range: _Range = None, i_range: _Range = None,
                       name: str = "control_loop") -> object:
    """A phase that arms the closed-loop DAC controller, lets it settle, and records its
    operating point.

    Arms an in-fabric panel/MPPT emulator (``voc_code`` + ``sharpness`` synthesise the curve),
    polls it ``probes`` times and records the settled ``control_loop_v`` (DAC code) and
    ``control_loop_i`` (ADC code) — raw codes, not volts. Pass ``v_range`` / ``i_range`` as
    ``(low, high)`` codes for pass/fail limits. Stops the loop before returning. Needs the loop
    gateware image (:attr:`Capabilities.dac_control_loop`).
    """
    _check_range("v_range", v_range)
    _check_range("i_range", i_range)
    v_meas = htf.Measurement("control_loop_v")
    i_meas = htf.Measurement("control_loop_i")
    if v_range is not None:
        v_meas = v_meas.in_range(v_range[0], v_range[1])
    if i_range is not None:
        i_meas = i_meas.in_range(i_range[0], i_range[1])

    @htf.PhaseOptions(name=name)
    @htf.plug(bench=plug)
    @htf.measures(v_meas, i_meas)
    def _loop(test, bench):
        with control_loop(bench, voc_code=voc_code, sharpness=sharpness, k=k,
                          vmin=vmin, vmax=vmax, tick_div=tick_div) as loop:
            pt = loop.probe()
            for _ in range(max(1, probes) - 1):
                pt = loop.probe()
            test.measurements.control_loop_v = pt.v
            test.measurements.control_loop_i = pt.i
            test.logger.info("control loop settled: i=%d (ADC) -> v=%d (DAC)", pt.i, pt.v)

    return _loop


def dac_replay_phase(plug: type, *, waveform_id: str, dac_path: DacPath = "5v",
                     mapping: ReplayMapping = "faithful", target_samples: int = 4096,
                     stop_after: bool = False, name: str = "dac_replay") -> object:
    """A phase that loads a cloud-stored waveform and replays it (looping) on the DAC.

    Needs server access (``BENCHPOD_API_KEY``). By default the replay keeps looping after the
    phase (so a later capture phase can observe it); set ``stop_after=True`` to stop it at the end
    of this phase.
    """

    @htf.PhaseOptions(name=name)
    @htf.plug(bench=plug)
    def _replay(test, bench):
        handle = replay_waveform(bench, waveform_id, dac_path=dac_path, mapping=mapping,
                                 target_samples=target_samples)
        test.logger.info("replaying waveform %s on %s (%d samples)",
                         waveform_id, dac_path, handle.samples)
        if stop_after:
            handle.stop()

    return _replay
