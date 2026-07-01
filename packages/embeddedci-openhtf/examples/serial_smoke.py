#!/usr/bin/env python3
"""OpenHTF test: a no-flash power + UART smoke test over a **direct serial**
connection to the BenchPod (no EmbeddedCI cloud, no OpenOCD).

Useful as a first bring-up: power the target, watch its boot output, assert a
banner, and read back a measured value the firmware prints. Run it with the pod
on a USB-serial port::

    pip install embeddedci-openhtf
    python serial_smoke.py --pod /dev/ttyACM0          # or COM5 on Windows

The custom phase shows the general pattern: declare measurements with
``@htf.measures``, grab the pod with ``@htf.plug``, and call the SDK directly.
"""

import argparse
import os
import re

import openhtf as htf
from openhtf.output.callbacks import console_summary

from embeddedci import benchpod
from embeddedci_openhtf import benchpod_plug, record_uart

UART_RX, UART_TX = 1, 2          # edit for your wiring (LA channels 1-12)
BOOT_BANNER = "APP_OK"


def make_smoke_phase(bench: type):
    @htf.PhaseOptions(name="power_and_read")
    @htf.measures(
        htf.Measurement("boot_ok").equals(True).doc("boot banner seen"),
        htf.Measurement("vbat_mv").in_range(3000, 3600).with_units("mV")
                                  .doc("battery voltage the firmware reports"),
    )
    @htf.plug(bench=bench)
    def _phase(test, bench):
        # power-cycle and capture the boot output in one shot
        cap = bench.power_cycle_and_capture(
            rx=UART_RX, tx=UART_TX, efuse=benchpod.INTERNAL,
            delay=1.0, duration=5.0, until=BOOT_BANNER,
        )
        record_uart(test, cap, name="boot_ok")     # sets boot_ok + attaches uart.txt

        # parse a value the firmware prints, e.g. "VBAT=3301mV"
        m = re.search(r"VBAT=(\d+)mV", cap.text)
        test.measurements.vbat_mv = int(m.group(1)) if m else 0

    return _phase


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pod", default=os.environ.get("BENCHPOD_CONNECTION"),
                    help="serial path (e.g. /dev/ttyACM0 or COM5), or host:port; "
                         "default: $BENCHPOD_CONNECTION")
    ap.add_argument("--sn", default="SN-0001")
    args = ap.parse_args()
    if not args.pod:
        ap.error("no pod connection: pass --pod or set BENCHPOD_CONNECTION")

    bench = benchpod_plug(args.pod)          # direct connection, no cloud
    test = htf.Test(make_smoke_phase(bench), test_name="benchpod_serial_smoke")
    test.add_output_callbacks(console_summary.ConsoleSummary())
    test.execute(test_start=lambda: args.sn)


if __name__ == "__main__":
    main()
