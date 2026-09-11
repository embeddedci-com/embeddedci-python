#!/usr/bin/env python3
"""OpenHTF test: analog stimulus + acquisition over a **direct TCP** connection
to the BenchPod (no EmbeddedCI cloud).

The pod has a DAC output and a 16-bit ADC input. This test drives a known
waveform, captures the ADC while it runs, and asserts the round trip in
calibrated **volts** — the canonical analog signal-path self-test. Wire the pod's
5V DAC output SMA to its front ADC SMA (directly, or through a DUT
filter/amplifier whose response you want to check).

    pip install embeddedci-openhtf
    python analog_loopback.py --pod 192.168.1.50:8080

Units follow the embeddedci SDK: amplitude/offset/limits in volts, durations in
seconds, frequencies and sample rates in hertz.
"""

import argparse
import os

import openhtf as htf
from openhtf.output.callbacks import console_summary

from embeddedci_openhtf import (
    adc_capture_phase,
    benchpod_plug,
    loopback_measure_phase,
    signal_generate_phase,
    signal_stop,
)

ADC_RATE_HZ = 50_000       # 4096 samples at 50 kHz = ~82 ms of signal


def build_test(pod: str) -> htf.Test:
    bench = benchpod_plug(pod)        # direct TCP, no cloud

    @htf.PhaseOptions(name="dac_stop")
    @htf.plug(bench=bench)
    def dac_stop(test, bench):
        signal_stop(bench)            # never leave the DAC driving after the test

    return htf.Test(
        htf.PhaseGroup(
            main=[
                # 1) loopback: a 100 Hz, 1 V-peak sine centred on 2.5 V (5V path),
                #    captured while it runs; expect ~2 V peak-to-peak around 2.5 V.
                loopback_measure_phase(
                    bench, waveform="sine", freq_hz=100, amplitude=1.0, offset=2.5,
                    dac_path="5v", samples=4096, sample_rate_hz=ADC_RATE_HZ,
                    pp_range=(1.8, 2.2), mean_range=(2.4, 2.6), prefix="loopback",
                ),
                # 2) free-run a square wave for 2 s, then snapshot the ADC on its own.
                signal_generate_phase(bench, waveform="square", freq_hz=100, amplitude=1.0,
                                      duration=2.0),
                adc_capture_phase(bench, samples=4096, sample_rate_hz=ADC_RATE_HZ,
                                  source="ext", pp_range=(1.8, 2.2)),
            ],
            teardown=[dac_stop],
        ),
        test_name="benchpod_analog_loopback",
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pod", default=os.environ.get("BENCHPOD_CONNECTION"),
                    help="BenchPod TCP address host[:port] (default: $BENCHPOD_CONNECTION)")
    ap.add_argument("--sn", default="SN-0001")
    args = ap.parse_args()
    if not args.pod:
        ap.error("no pod connection: pass --pod or set BENCHPOD_CONNECTION")

    test = build_test(args.pod)
    test.add_output_callbacks(console_summary.ConsoleSummary())
    test.execute(test_start=lambda: args.sn)


if __name__ == "__main__":
    main()
