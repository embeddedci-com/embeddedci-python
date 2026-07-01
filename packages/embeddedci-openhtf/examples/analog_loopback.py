#!/usr/bin/env python3
"""OpenHTF test: analog stimulus + acquisition over a **direct TCP** connection
to the BenchPod (no EmbeddedCI cloud).

The pod has a DAC output and an ADC input on its FPGA. This test drives a known
waveform and captures the ADC at the same time (``measure`` = synchronized
DAC+ADC loopback), then asserts the round-trip amplitude — the canonical analog
signal-path self-test. Wire the pod's DAC output to its ADC input (directly, or
through a DUT filter/amplifier whose response you want to check).

    pip install embeddedci-openhtf
    python analog_loopback.py --pod 192.168.1.50:8080

Analog commands are TCP-only (they need the JSON/sample channel), so this example
uses a TCP address, not a serial path.
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
)


def build_test(pod: str) -> htf.Test:
    bench = benchpod_plug(pod)        # direct TCP, no cloud
    return htf.Test(
        # 1) DAC+ADC loopback: drive a 10 kHz sine and assert the round trip.
        loopback_measure_phase(
            bench, waveform="sine", freq=10_000, amplitude=120, offset=128,
            samples=4096, pp_range=(180, 255), mean_range=(110, 150),
        ),
        # 2) free-run a waveform, then snapshot the ADC on its own.
        signal_generate_phase(bench, waveform="square", freq=1_000, amplitude=100),
        adc_capture_phase(bench, samples=4096, sample_rate_mhz=0.5,
                          pp_range=(150, 255)),
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
