#!/usr/bin/env python3
"""OpenHTF station loop with a **persistent** BenchPod connection over direct TCP
(no EmbeddedCI cloud).

A manufacturing station tests one DUT after another. Opening a fresh connection
per DUT adds latency, so this uses ``benchpod_plug(..., persistent=True)``: the
connection is opened once and reused across every ``test.execute()``, re-checked
with a ping each run and reconnected if it dropped. It is closed once at the end
with ``close_persistent_benchpods()``.

    pip install embeddedci-openhtf
    python station.py --pod 192.168.1.50:8080
    # enter each board's serial number when prompted; Ctrl-C to stop

The pod connection stays up for the whole session; only the per-DUT test phases
repeat.
"""

import argparse
import os

import openhtf as htf
from openhtf.output.callbacks import console_summary
from openhtf.plugs import user_input

from embeddedci_openhtf import (
    benchpod_plug,
    boot_banner_phase,
    close_persistent_benchpods,
    power_phase,
)

UART_RX, UART_TX = 1, 2          # edit for your wiring (LA channels 1-12)
BOOT_BANNER = "APP_OK"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pod", default=os.environ.get("BENCHPOD_CONNECTION"),
                    help="BenchPod address host[:port] or serial path "
                         "(default: $BENCHPOD_CONNECTION)")
    args = ap.parse_args()
    if not args.pod:
        ap.error("no pod connection: pass --pod or set BENCHPOD_CONNECTION")

    # persistent=True -> one connection shared across every DUT this session
    bench = benchpod_plug(args.pod, persistent=True)
    test = htf.Test(
        power_phase(bench, on=True),
        boot_banner_phase(bench, rx=UART_RX, tx=UART_TX, expect=BOOT_BANNER,
                          power_cycle=True, duration=6.0),
        power_phase(bench, on=False, name="power_off"),
        test_name="benchpod_station",
    )
    test.add_output_callbacks(console_summary.ConsoleSummary())

    try:
        # OpenHTF returns from execute() after each DUT; loop for the next one.
        while test.execute(test_start=user_input.prompt_for_test_start()):
            pass
    except KeyboardInterrupt:
        print("\nstation stopped")
    finally:
        close_persistent_benchpods()   # close the shared connection once


if __name__ == "__main__":
    main()
