#!/usr/bin/env python3
"""OpenHTF test: a no-flash power + UART smoke test of the DUT's serial console,
over a **direct** connection to the BenchPod (no EmbeddedCI cloud, no OpenOCD).

Useful as a first bring-up: power the target, watch its boot output, assert a
banner, read back a value the firmware prints, and check the target rail with the
pod's power monitor. Run it against the pod's network address::

    pip install embeddedci-openhtf
    python serial_smoke.py --pod 192.168.1.50

(The STM32 pod's own USB console can't proxy the DUT's UART, so use the network
connection — the USB port is for power, status and the LA voltage only.)

The custom phase shows the general pattern: declare measurements with
``@htf.measures``, grab the pod with ``@htf.plug``, and call the SDK directly.
Units are volts and seconds throughout.
"""

import argparse
import os
import re

import openhtf as htf
from openhtf.output.callbacks import console_summary

from embeddedci import benchpod
from embeddedci_openhtf import benchpod_plug, record_uart

LA_VOLTAGE = 3.3                 # DUT I/O voltage — change to 1.8 for a 1V8 board
UART_RX, UART_TX = 1, 2          # edit for your wiring (LA channels 1-14)
BOOT_BANNER = "APP_OK"


def make_smoke_phase(bench: type):
    @htf.PhaseOptions(name="power_and_read")
    @htf.measures(
        htf.Measurement("boot_ok").equals(True).doc("boot banner seen"),
        htf.Measurement("vbat_v").in_range(3.0, 3.6).with_units("V")
                                 .doc("battery voltage the firmware reports"),
        htf.Measurement("target_rail_v").in_range(4.75, 5.25).with_units("V")
                                        .doc("internal target rail, pod power monitor"),
    )
    @htf.plug(bench=bench)
    def _phase(test, bench):
        # power-cycle and capture the boot output in one shot (delay/duration in seconds)
        cap = bench.power_cycle_and_capture(
            rx=UART_RX, tx=UART_TX, efuse=benchpod.INTERNAL,
            delay=1.0, duration=5.0, until=BOOT_BANNER,
        )
        record_uart(test, cap, name="boot_ok")     # sets boot_ok + attaches uart.txt

        # parse a value the DUT firmware prints, e.g. "VBAT=3301mV", into volts
        m = re.search(r"VBAT=(\d+)mV", cap.text)
        test.measurements.vbat_v = int(m.group(1)) / 1000.0 if m else 0.0

        # the SDK returns typed state in volts/amps
        rail = bench.power_status().rail(benchpod.INTERNAL)
        test.measurements.target_rail_v = rail.bus_voltage
        test.logger.info("target rail %.3f V, %.1f mA", rail.bus_voltage, rail.current * 1e3)

    return _phase


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pod", default=os.environ.get("BENCHPOD_CONNECTION"),
                    help="the pod's network address (host[:port]); "
                         "default: $BENCHPOD_CONNECTION")
    ap.add_argument("--sn", default="SN-0001")
    args = ap.parse_args()
    if not args.pod:
        ap.error("no pod connection: pass --pod or set BENCHPOD_CONNECTION")

    bench = benchpod_plug(args.pod, la_voltage=LA_VOLTAGE)   # direct connection, no cloud
    test = htf.Test(make_smoke_phase(bench), test_name="benchpod_serial_smoke")
    test.add_output_callbacks(console_summary.ConsoleSummary())
    test.execute(test_start=lambda: args.sn)


if __name__ == "__main__":
    main()
