# Flash → emulate a BMP280 → power-cycle → assert on the DUT's UART and I2C bus, in one test.
#
# This is the "hello world" of EmbeddedCI hardware-in-the-loop testing. The pod
# flashes your firmware onto the DUT, then *pretends to be a BMP280* on the DUT's
# I2C bus, power-cycles the DUT and captures its boot log over UART — so you can
# assert that your firmware booted and detected the sensor, on real hardware.
# Finally it power-cycles once more and decodes the I2C bus itself, to check the
# DUT really read the chip-id register.
#
# Run it:
#
#   pip install "embeddedci[pytest]"   # + an OpenOCD with the cmsis_dap_tcp backend
#   pytest examples/test_bmp280.py \
#       --benchpod-connection=192.168.1.213 \
#       --benchpod-firmware=path/to/your_app.elf
#
# The board's I/O voltage is set once, in the `benchpod_la_voltage` fixture below
# (3.3 V here). Change it to 1.8 for a 1V8 board — in a real project that fixture
# lives in conftest.py so every test file shares it.
#
# (--benchpod-connection also takes "usb", a serial device path, "discover", or
# "embeddedci:<device-name>" for a pod reached through embeddedci.com.)
#
# Wiring — the pod has no dedicated SWD/UART/I2C pins. It exposes 12 generic LA
# channels (pins.pin_1 .. pins.pin_12) and any DUT signal can be on any of them.
# The `wiring` fixture below is THIS bench's map; edit it to match your board:
#
#   DUT pin                         Pod                    wiring fixture
#   ------------------------------  ---------------------  --------------
#   SWCLK                           LA11                   wiring.swclk
#   SWDIO                           LA12                   wiring.swdio
#   NRST                            J1 pin 22 (reset pin)  wiring.nreset (True)
#   UART TX  (DUT -> pod samples)   LA5                    wiring.uart_rx
#   UART RX  (pod -> DUT drives)    LA4                    wiring.uart_tx
#   I2C SDA  (needs a pull-up)      LA2 (4.7k pull-up)     wiring.i2c_sda
#   I2C SCL  (needs a pull-up)      LA1 (4.7k pull-up)     wiring.i2c_scl
#   Target 5V power                 eFuse 1 (internal)     --benchpod-efuse
#
# Switchable pull-ups exist only on LA1-LA6 (LA1/2 = 4.7k, LA3/4 = 2.2k,
# LA5/6 = 10k). LA7/LA8 carry 10k pull-DOWNs and LA9-LA12 nothing, so the
# open-drain I2C lines must sit on LA1-LA6. The resistors are referenced to 3V3,
# which is why this example runs the LA bank at 3.3 V.
#
# The `benchpod_sensor`, `pins` and `firmware` fixtures come from the installed
# plugin; the test skips when no --benchpod-connection or --benchpod-firmware is set.

import re
import time
from types import SimpleNamespace

import pytest

from embeddedci.benchpod import BMP280_ADDR_PRIMARY, Sensor, i2c

APP_OK = re.compile(r"APP_OK")
PRESENT = re.compile(r"chip id match=0x58|bmp280_detected=yes")
BMP280_CHIP_ID_REG = 0xD0
BMP280_CHIP_ID = 0x58


@pytest.fixture(scope="session")
def benchpod_la_voltage():
    """The DUT's I/O voltage, selected on the pod when the session connects."""
    return 3.3  # change to 1.8 for a 1V8 board (the pull-ups then can't be used)


@pytest.fixture
def wiring(pins):
    """This bench's wiring: DUT signal → BenchPod LA channel. Bench-specific —
    any signal can be on any LA channel, except I2C SDA/SCL need a pull-up (LA1-LA6)."""
    return SimpleNamespace(
        swclk=pins.pin_11, swdio=pins.pin_12, nreset=True,
        uart_rx=pins.pin_5, uart_tx=pins.pin_4,
        i2c_sda=pins.pin_2, i2c_scl=pins.pin_1,  # LA1/LA2 — 4.7k pull-ups
        efuse=pins.efuse,
    )


@pytest.mark.hardware
def test_bmp280_sensor_present(benchpod_sensor, wiring, firmware):
    """Flash the DUT, emulate a BMP280 on I2C, power-cycle, assert on UART and the I2C bus."""
    bp = benchpod_sensor  # the plain `benchpod` device; also disarms the sensor at teardown

    try:
        # 1. Flash the firmware onto the DUT over SWD. Raises FlashError (with
        #    OpenOCD's output) if it fails.
        bp.flash(
            file=firmware,
            target="target/stm32f4x.cfg",
            swclk=wiring.swclk, swdio=wiring.swdio,
            nreset=wiring.nreset, target_power=wiring.efuse,
            verify=False,  # programming is reliable; the read-back verify is the slow part
        )

        # 2. Have the pod emulate a BMP280 on the DUT's I2C bus. I2C is open-drain,
        #    so engage the pod's pull-ups first to idle the bus high.
        bp.enable_pullup(wiring.i2c_sda, wiring.i2c_scl)
        bp.enable_i2c_sensor(
            Sensor.BMP280,
            sda=wiring.i2c_sda, scl=wiring.i2c_scl,
            address=BMP280_ADDR_PRIMARY,
            temperature_c=22.5, pressure_pa=101_000,
        )

        # 3. Power-cycle the DUT and capture its boot log over UART. The power-on is
        #    scheduled pod-side (delay=1.5 s) so it lands *inside* the capture window
        #    and the boot banner is not missed. Stop at APP_OK, which the app prints
        #    last (after the BMP280 probe), so the capture contains both markers.
        boot = bp.power_cycle_and_capture(
            rx=wiring.uart_rx, tx=wiring.uart_tx, efuse=wiring.efuse,
            delay=1.5, duration=8.0, until=APP_OK,
        )
        assert boot.match(APP_OK), f"no APP_OK banner:\n{boot.text}"
        assert boot.match(PRESENT), f"BMP280 not detected:\n{boot.text}"
        assert bp.i2c_sensor_status().get("transactions", 0) > 0

        # 4. Boot again and decode the I2C bus. The chip-id probe is a one-shot a few
        #    ms after power-up, so sweep a handful of back-to-back ~33 ms windows
        #    (4096 bytes at 500 kS/s ≈ 5 samples per bit at 100 kHz I2C).
        bp.power_off(wiring.efuse)
        time.sleep(0.3)
        bp.power_on(wiring.efuse)  # returns immediately; the captures below cover the boot
        txns = []
        chip_id = None
        for _ in range(6):
            txns = bp.i2c_sensor_capture(4096, sample_rate_hz=500_000)
            chip_id = i2c.read_register(txns, BMP280_ADDR_PRIMARY, BMP280_CHIP_ID_REG)
            if chip_id is not None:
                break
        print("decoded I2C bus:\n" + i2c.format_transactions(txns))
        assert chip_id == [BMP280_CHIP_ID], f"expected a chip-id read of 0x58, decoded {chip_id}"
    finally:
        # Always leave the DUT powered down, pass or fail, so the bench is in a known state.
        bp.power_off(wiring.efuse)
        bp.disable_pullup(wiring.i2c_sda, wiring.i2c_scl)
