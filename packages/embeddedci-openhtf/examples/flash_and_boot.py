#!/usr/bin/env python3
"""OpenHTF test: flash a DUT over the BenchPod's SWD probe, then assert its boot
banner — connecting **directly** to the pod over TCP (no EmbeddedCI cloud).

Run it against a pod on your LAN::

    pip install embeddedci-openhtf
    python flash_and_boot.py --pod 192.168.1.50:8080 --firmware fw.elf

Needs `openocd` on PATH (the pod is the CMSIS-DAP probe; OpenOCD drives the flash
algorithm). The bench wiring below — which LA channel carries SWCLK/SWDIO/NRST
and the DUT's UART TX/RX — is specific to your setup; edit the constants.

A JSON test record is written next to this script via OpenHTF's standard
OutputToJSON callback, and a PASS/FAIL summary is printed to the console.
"""

import argparse
import os

import openhtf as htf
from openhtf.output.callbacks import console_summary, json_factory

from embeddedci_openhtf import benchpod_plug, boot_banner_phase, flash_phase

# --- bench wiring (edit for your setup): LA channels 1-12 -------------------
SWCLK, SWDIO, NRESET = 11, 12, 3      # SWD probe -> DUT
UART_RX, UART_TX = 1, 2               # RX = LA channel sampling the DUT's TX
OPENOCD_TARGET = "target/stm32f4x.cfg"
BOOT_BANNER = "APP_OK"               # substring the firmware prints when healthy


def build_test(pod: str, firmware: str) -> htf.Test:
    bench = benchpod_plug(pod)        # direct TCP / serial connection, no cloud
    return htf.Test(
        flash_phase(bench, file=firmware, target=OPENOCD_TARGET,
                    swclk=SWCLK, swdio=SWDIO, nreset=NRESET),
        boot_banner_phase(bench, rx=UART_RX, tx=UART_TX, expect=BOOT_BANNER,
                          power_cycle=True, duration=6.0),
        test_name="benchpod_flash_and_boot",
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pod", default=os.environ.get("BENCHPOD_CONNECTION"),
                    help="BenchPod address: host[:port] or a serial path "
                         "(default: $BENCHPOD_CONNECTION)")
    ap.add_argument("--firmware", required=True, help="firmware image to flash")
    ap.add_argument("--sn", default="SN-0001", help="DUT serial number")
    args = ap.parse_args()
    if not args.pod:
        ap.error("no pod connection: pass --pod or set BENCHPOD_CONNECTION")

    test = build_test(args.pod, args.firmware)
    test.add_output_callbacks(
        json_factory.OutputToJSON("./{dut_id}.{start_time_millis}.json", indent=2),
        console_summary.ConsoleSummary(),
    )
    test.execute(test_start=lambda: args.sn)


if __name__ == "__main__":
    main()
