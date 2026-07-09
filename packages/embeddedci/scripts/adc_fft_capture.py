#!/usr/bin/env python3
"""Capture BenchPod ADC samples into a CSV for FFT / spectral analysis.

Self-contained and ad-hoc.  By default it drives the pod's own DAC with a sine
and loops it *internally* back to the ADC (analog path ``cal1`` = 5V DAC -> ADC
via relay K1), captures a block of ADC samples at a known rate, writes them to a
CSV (with a metadata header), and runs an FFT (numpy) to report the dominant
frequency -- which, for the loopback, should match the generated sine.  Point it
at an external signal instead with ``--source ext --no-generate``.

Usage:
  python scripts/adc_fft_capture.py                       # 1 kHz sine loopback, 4096 @ auto rate
  python scripts/adc_fft_capture.py --freq 2000 --plot    # + a time+spectrum PNG
  python scripts/adc_fft_capture.py --source ext --no-generate --out signal.csv
  python scripts/adc_fft_capture.py --sweep               # bench check: generate(f) accuracy
  BENCHPOD_HW_ADDR=192.168.1.215 python scripts/adc_fft_capture.py

A DAC-loopback run also asserts the played frequency is recovered within --tol
(default 5%) and exits non-zero otherwise; `--sweep` does this across several tones.

The CSV carries ``#``-comment metadata lines then columns
``index,time_s,count,volts``; a sidecar ``<name>.json`` holds the same metadata
(sample_rate_hz etc.) for easy machine loading.  Analyse it in Python, e.g.:

  import json, numpy as np
  meta = json.load(open("adc_capture_1000hz.json"))
  fs   = meta["sample_rate_hz"]
  ncom = sum(l.startswith("#") for l in open("adc_capture_1000hz.csv"))
  d    = np.genfromtxt("adc_capture_1000hz.csv", delimiter=",", names=True, skip_header=ncom)
  v    = d["volts"] - d["volts"].mean()
  X, f = np.abs(np.fft.rfft(v * np.hanning(len(v)))), np.fft.rfftfreq(len(v), 1/fs)
  print("peak:", f[1 + np.argmax(X[1:])], "Hz")
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Runnable straight from the repo without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from embeddedci import benchpod  # noqa: E402

# A free-running DAC (`generate`, continuous) plus a separate ADC `capture` gives
# a real multi-cycle time series (the DAC plays from FPGA BRAM, the ADC streams to
# PSRAM). On this pod (fw 0.3.0-dev) the ADC scope capture is reliable up to ~4096
# samples; larger requests return "capture failed". 4096 @ ~100 kS/s is ~40 cycles
# of a 1 kHz tone -- plenty of FFT resolution.
ADC_CAP_RELIABLE = 4096
FPGA_HFOSC_HZ = 24_000_000  # iCE40 control clock the ADC/DAC engines divide down
ADC_MIN_DIVIDER = 60        # gateware v12 ADC sample-period floor
ADC_MAX_RATE_HZ = 400_000   # 24 MHz / 60 = 400 kS/s ceiling
# The adc_mcp33131 sequencer's real sample period is (divider + 1) clocks, not
# `divider` (sim-measured: it alternates div / div+2, averaging div+1). Ignoring
# that +1 makes the FFT frequency axis read high by (div+1)/div -- ~+1.7% at the
# 400 kS/s max, ~+0.3% at 100 kS/s. So compute the ADC rate from the true period.
ADC_PERIOD_OVERHEAD = 1


def actual_adc_fs_hz(req_rate_hz: float) -> float:
    """True ADC sample rate for a requested rate: the firmware picks
    div = max(round(HFOSC/req), ADC_MIN_DIVIDER), and the engine's real period is
    div + ADC_PERIOD_OVERHEAD clocks."""
    div = max(round(FPGA_HFOSC_HZ / req_rate_hz), ADC_MIN_DIVIDER)
    return FPGA_HFOSC_HZ / (div + ADC_PERIOD_OVERHEAD)


# The firmware now models the DAC8551 sequencer's per-sample cost (the SPI shift +
# BRAM reads add ~51 FPGA clocks/sample) in compute_divider_and_period, so a plain
# `generate` (auto rate) plays the requested frequency accurately -- no host-side
# rate workaround needed. `--gen-rate-mhz` is left as an optional override for the
# effective DAC update rate.  (Historically the DAC "saturated" -- 1 kHz asked came
# out ~310 Hz at a high nominal play rate -- because that per-sample cost was ignored.)
def counts_to_volts(counts, bits: int, fullscale_mv: int):
    """Scale raw ADC counts to volts using the pod's reported width + fullscale."""
    max_count = float((1 << bits) - 1)
    fs_v = fullscale_mv / 1000.0
    return [c * fs_v / max_count for c in counts]


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--addr", default=os.environ.get("BENCHPOD_HW_ADDR", "192.168.1.215"),
                    help="pod host[:port] (default: $BENCHPOD_HW_ADDR or 192.168.1.215)")
    ap.add_argument("--samples", type=int, default=4096,
                    help="ADC samples (default 4096; loopback/measure caps at 4096, external capture up to 32768)")
    ap.add_argument("--rate-mhz", type=float, default=None, dest="rate_mhz",
                    help="ADC sample rate in MHz. Loopback default auto-picks ~128 samples/period "
                         "(many clean cycles); external-capture default is 0.2 (200 kS/s)")
    ap.add_argument("--freq", type=float, default=1000.0, help="DAC sine frequency in Hz (default 1000)")
    ap.add_argument("--gen-rate-mhz", type=float, default=None, dest="gen_rate_mhz",
                    help="override the DAC effective playback rate in MHz (default: auto — the "
                         "firmware picks a divider that plays --freq accurately)")
    ap.add_argument("--amplitude", type=int, default=127, help="DAC 8-bit amplitude 1..127 (default 127)")
    ap.add_argument("--offset", type=int, default=128, help="DAC 8-bit offset 0..255 (default 128)")
    ap.add_argument("--waveform", default="sine", choices=["sine", "square", "sawtooth"])
    ap.add_argument("--source", default="cal1",
                    help="ADC analog path: cal1 (DAC loopback, default) | ext/sma (SMA input) | amp | cal2")
    ap.add_argument("--no-generate", action="store_true",
                    help="do NOT drive the DAC; capture whatever is present on --source")
    ap.add_argument("--out", default=None, help="CSV output path (default: adc_capture_<freq>hz.csv)")
    ap.add_argument("--settle", type=float, default=0.2,
                    help="seconds to let the DAC/relays settle before capturing (default 0.2)")
    ap.add_argument("--plot", nargs="?", const="__auto__", default=None,
                    help="also write a PNG (time + spectrum) if matplotlib is available; "
                         "optional path, else alongside the CSV")
    ap.add_argument("--tol", type=float, default=0.05,
                    help="frequency-accuracy tolerance as a fraction (default 0.05 = 5%%); "
                         "a generate run exits non-zero if the recovered freq is outside it")
    ap.add_argument("--sweep", nargs="?", const="__default__", default=None,
                    help="bench check: generate several tones and assert each is recovered "
                         "within --tol. Optional comma-separated freqs (default 200,500,1000,2000,5000)")
    return ap.parse_args(argv)


def loopback_rate_mhz(freq: float) -> float:
    """ADC sample rate for the DAC loopback: oversample ~100x so ~40 cycles land
    in a 4096-sample window (fine FFT resolution) while staying under the ADC's
    300 kS/s ceiling and comfortably above Nyquist."""
    return min(max(freq * 100.0, 50_000.0), ADC_MAX_RATE_HZ) / 1e6


def capturable_gen_rate_mhz(freq: float, adc_fs_hz: float) -> float:
    """DAC playback (update) rate for a loopback capture: fast enough for a smooth
    ~32-samples/period waveform, but kept below the ADC's Nyquist so the DAC's
    zero-order-hold images don't alias into the captured band (which would swamp
    the fundamental).  The firmware plays `freq` accurately at whatever rate this
    picks, so this only shapes capture quality, not the output frequency."""
    return min(freq * 32.0, adc_fs_hz * 0.4) / 1e6


def capture(args):
    """Connect, set up the signal, capture, and clean up.  Returns a result dict.

    Loopback (DAC-driven, default): a continuous `generate` sine looped through the
    analog path, sampled by a free-running ADC `capture` (DAC clock and ADC clock
    are independent, so we pick an ADC rate that yields many clean cycles).
    An external source (--no-generate) skips the DAC and just captures --source.
    """
    gen = None
    with benchpod.BenchPod(args.addr) as bp:
        st = bp.status()
        if not isinstance(st, dict):
            raise SystemExit(f"expected a dict status over TCP, got {type(st).__name__}")
        bits = int(st.get("adc_bits") or 16)
        fullscale_mv = int(st.get("adc_fullscale_mv") or 4096)
        board = st.get("board", "?")
        print(f"[pod ] {board}  adc_bits={bits}  fullscale={fullscale_mv} mV  @ {args.addr}")

        n = args.samples
        if n > ADC_CAP_RELIABLE:
            print(f"[note] ADC scope capture on this pod is reliable to {ADC_CAP_RELIABLE} "
                  f"samples; clamping {n}.")
            n = ADC_CAP_RELIABLE
        try:
            bp.analog_path(args.source)
            if not args.no_generate:
                rate = args.rate_mhz if args.rate_mhz else loopback_rate_mhz(args.freq)
                fs_hz = actual_adc_fs_hz(rate * 1e6)
                # The firmware plays `freq` accurately at any divider, but for a clean
                # loopback FFT the DAC's update rate must stay below the ADC's Nyquist
                # (else its zero-order-hold images alias in and swamp the fundamental).
                gen_rate = args.gen_rate_mhz or capturable_gen_rate_mhz(args.freq, fs_hz)
                bp.command({"cmd": "generate", "waveform": args.waveform, "freq": args.freq,
                            "amplitude": args.amplitude, "offset": args.offset,
                            "sample_rate_mhz": gen_rate})
                gen = {"waveform": args.waveform, "freq": args.freq, "rate_hz": gen_rate * 1e6}
                print(f"[dac ] {args.waveform} {args.freq:.1f} Hz amp={args.amplitude} "
                      f"@ {gen_rate*1e3:.0f} kS/s continuous, path={args.source}")
                time.sleep(args.settle)
            else:
                rate = args.rate_mhz if args.rate_mhz else 0.2
                fs_hz = actual_adc_fs_hz(rate * 1e6)
            if gen is not None:
                print(f"[adc ] capturing {n} samples @ {fs_hz/1e3:.1f} kS/s "
                      f"({n/fs_hz*1e3:.2f} ms, ~{n*args.freq/fs_hz:.0f} cycles of {args.freq:.0f} Hz)...")
            else:
                print(f"[adc ] capturing {n} samples @ {fs_hz/1e3:.1f} kS/s "
                      f"({n/fs_hz*1e3:.2f} ms window) from external source '{args.source}'...")
            counts = bp.capture(n, sample_rate_mhz=rate)
        finally:
            # Always leave the pod quiet for whoever uses it next. (`measure`
            # already stops the DAC, but do it explicitly for the capture path.)
            for req in ({"cmd": "dac_stop"}, {"cmd": "analog_path", "path": "off"}):
                try:
                    bp.command(req)
                except Exception:
                    pass

    return {"counts": list(counts), "fs_hz": fs_hz, "bits": bits,
            "fullscale_mv": fullscale_mv, "board": board, "gen": gen}


def write_csv(path, res, args):
    """Write the capture as a CSV (`#` metadata header + index,time_s,count,volts)
    plus a sidecar `.json` with the same metadata for easy machine loading."""
    counts = res["counts"]
    volts = counts_to_volts(counts, res["bits"], res["fullscale_mv"])
    dt = 1.0 / res["fs_hz"]

    meta = {
        "addr": args.addr, "board": res["board"],
        "sample_rate_hz": res["fs_hz"], "samples": len(counts),
        "adc_bits": res["bits"], "adc_fullscale_mv": res["fullscale_mv"],
        "analog_path": args.source,
    }
    if res["gen"]:
        meta.update({"dac_waveform": res["gen"]["waveform"],
                     "dac_freq_hz": res["gen"]["freq"],
                     "dac_rate_hz": res["gen"]["rate_hz"] or "auto"})

    with open(path, "w") as f:
        f.write("# benchpod ADC capture for FFT analysis\n")
        for k, v in meta.items():
            f.write(f"# {k}={v}\n")
        f.write("index,time_s,count,volts\n")
        for i, (c, v) in enumerate(zip(counts, volts)):
            f.write(f"{i},{i*dt:.9f},{c},{v:.6f}\n")

    json_path = path.rsplit(".", 1)[0] + ".json"
    with open(json_path, "w") as f:
        json.dump(meta, f, indent=2)
    return volts, json_path


def fft_peak(volts, fs_hz):
    """Dominant (non-DC) frequency via a Hann-windowed rFFT + parabolic sub-bin
    refinement. Returns None if numpy is unavailable or the input is too short."""
    try:
        import numpy as np
    except ImportError:
        return None
    n = len(volts)
    if n < 4:
        return None
    x = np.asarray(volts, dtype=float)
    x = x - x.mean()
    mag = np.abs(np.fft.rfft(x * np.hanning(n)))
    k = int(np.argmax(mag[1:])) + 1  # skip DC
    peak = k * (fs_hz / n)
    if 1 < k < len(mag) - 1:
        a, b, c = mag[k - 1], mag[k], mag[k + 1]
        denom = a - 2 * b + c
        if denom != 0:
            peak = (k + 0.5 * (a - c) / denom) * (fs_hz / n)
    return float(peak)


def run_fft(volts, fs_hz, gen, plot_path=None):
    try:
        import numpy as np
    except ImportError:
        print("[fft ] numpy not installed; skipping FFT (CSV already written).")
        return None
    n = len(volts)
    x = np.asarray(volts, dtype=float)
    x = x - x.mean()
    win = np.hanning(n)
    mag = np.abs(np.fft.rfft(x * win))
    freqs = np.fft.rfftfreq(n, 1.0 / fs_hz)
    peak_hz = fft_peak(volts, fs_hz)

    print(f"[fft ] N={n}  Fs={fs_hz:.1f} Hz  resolution={fs_hz/n:.2f} Hz/bin")
    print(f"[fft ] dominant frequency: {peak_hz:.1f} Hz  (measured on the ADC)")
    if gen:
        err = (peak_hz - gen["freq"]) / gen["freq"] * 100.0
        print(f"[fft ] generated {gen['freq']:.1f} Hz -> measured {peak_hz:.1f} Hz ({err:+.1f}%)")
    order = np.argsort(mag[1:])[::-1][:5] + 1
    print("[fft ] top peaks:")
    for kk in sorted(order):
        print(f"         {freqs[kk]:9.1f} Hz   mag {mag[kk]:.4g}")

    if plot_path:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            t = np.arange(n) / fs_hz
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 6))
            show = min(n, int(fs_hz / max(peak_hz, 1) * 5) or n)  # ~5 cycles
            ax1.plot(t[:show] * 1e3, x[:show], lw=0.8)
            ax1.set_xlabel("time (ms)"); ax1.set_ylabel("volts (AC)"); ax1.set_title("ADC capture (time domain)")
            ax2.plot(freqs, mag, lw=0.8)
            ax2.axvline(peak_hz, color="r", ls="--", lw=0.8, label=f"peak {peak_hz:.0f} Hz")
            ax2.set_xlabel("frequency (Hz)"); ax2.set_ylabel("|FFT|"); ax2.set_title("Spectrum (Hann)")
            ax2.set_xlim(0, min(freqs[-1], peak_hz * 8 if peak_hz else freqs[-1])); ax2.legend()
            fig.tight_layout(); fig.savefig(plot_path, dpi=110)
            print(f"[plot] wrote {plot_path}")
        except ImportError:
            print("[plot] matplotlib not installed; skipping PNG.")
    return peak_hz


def run_sweep(args):
    """Bench check: generate a set of tones through the DAC loopback and assert each
    is recovered on the ADC within --tol. Exits non-zero if any tone is off, so it
    doubles as a CI/regression check that generate(freq) plays the requested freq."""
    freqs = ([float(f) for f in str(args.sweep).split(",")]
             if args.sweep not in (None, "__default__") else [200, 500, 1000, 2000, 5000])
    print(f"[sweep] generate-accuracy check over {freqs} Hz  (tol {args.tol*100:.0f}%)")
    all_ok = True
    for f in freqs:
        # A moderate amplitude keeps the cal1 loopback front-end linear (near
        # full-scale it slews/clips at higher tones, burying the fundamental under
        # harmonics), so the recovered peak is the fundamental across the range.
        a = parse_args(["--addr", args.addr, "--freq", str(f), "--samples", str(args.samples),
                        "--source", args.source, "--settle", str(args.settle),
                        "--amplitude", str(min(args.amplitude, 60))])
        res = capture(a)
        peak = fft_peak(counts_to_volts(res["counts"], res["bits"], res["fullscale_mv"]),
                        res["fs_hz"]) if res["counts"] else None
        if peak is None:
            print(f"  {f:8.0f} Hz -> FAIL (no capture / no numpy)")
            all_ok = False
            continue
        err = abs(peak - f) / f
        ok = err <= args.tol
        all_ok = all_ok and ok
        print(f"  {f:8.0f} Hz -> {peak:8.1f} Hz  ({(peak-f)/f*100:+5.1f}%)  {'PASS' if ok else 'FAIL'}")
    print(f"[sweep] {'ALL PASS' if all_ok else 'FAILURES PRESENT'}")
    return 0 if all_ok else 3


def main(argv=None):
    args = parse_args(argv)
    if args.sweep is not None:
        return run_sweep(args)

    out = args.out or f"adc_capture_{int(args.freq)}hz.csv"
    res = capture(args)
    if not res["counts"]:
        print("ERROR: capture returned no samples", file=sys.stderr)
        return 2
    volts, json_path = write_csv(out, res, args)
    print(f"[csv ] wrote {len(res['counts'])} samples -> {out}  (+ metadata {json_path})")

    plot_path = None
    if args.plot is not None:
        plot_path = out.rsplit(".", 1)[0] + ".png" if args.plot == "__auto__" else args.plot
    peak = run_fft(volts, res["fs_hz"], res["gen"], plot_path)

    # When we drive the DAC, assert the played frequency is recovered within --tol.
    if res["gen"] and peak is not None:
        err = abs(peak - args.freq) / args.freq
        ok = err <= args.tol
        print(f"[chk ] generate {args.freq:.0f} Hz -> {peak:.1f} Hz "
              f"({err*100:.1f}%, tol {args.tol*100:.0f}%): {'PASS' if ok else 'FAIL'}")
        if not ok:
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
