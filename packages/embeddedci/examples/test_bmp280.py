# Flash → emulate a BMP280 → power-cycle → assert on the DUT's UART, in one test.
#
# This is the "hello world" of EmbeddedCI hardware-in-the-loop testing. The pod
# flashes your firmware onto the DUT, then *pretends to be a BMP280* on the DUT's
# I2C bus, power-cycles the DUT, and captures its boot log over UART — so you can
# assert that your firmware booted and detected the sensor, on real hardware.
#
# Run it:
#
#   pip install embeddedci
#   pytest examples/test_bmp280.py \
#       --benchpod-connection=/dev/tty.usbserial-0001 \    # or 192.168.1.50 for wifi
#       --benchpod-firmware=path/to/your_app.elf
#
# Wiring (override any with --benchpod-<name>=<LA-channel>):
#
#   DUT pin                         Pod LA channel   flag
#   ------------------------------  --------------   --------------------
#   SWCLK                           LA11             --benchpod-swclk
#   SWDIO                           LA12             --benchpod-swdio
#   NRST                            LA3              --benchpod-nreset
#   UART TX  (DUT -> pod samples)   LA5              --benchpod-uart-rx
#   UART RX  (pod -> DUT drives)    LA4              --benchpod-uart-tx
#   I2C SDA  (has a pull-up)        LA2              --benchpod-i2c-sda
#   I2C SCL  (has a pull-up)        LA1              --benchpod-i2c-scl
#   5V power eFuse (1=int, 2=ext)   --benchpod-efuse
#
# The `benchpod`, `pins` and `firmware` fixtures come from the installed plugin;
# the whole test skips automatically if no --benchpod-connection is given.

import re

import pytest
from embeddedci import benchpod as bp

APP_OK = re.compile(r"APP_OK")
PRESENT = re.compile(r"chip id match=0x58|bmp280_detected=yes")


@pytest.mark.hardware
def test_bmp280_sensor_present(benchpod, pins, firmware):
    """Flash the DUT, emulate a BMP280 on I2C, power-cycle, assert on UART."""

    try:
        # 1. Flash the firmware onto the DUT over SWD.
        result = benchpod.flash(
            file=firmware,
            target="target/stm32f4x.cfg",
            swclk=pins.swclk, swdio=pins.swdio,
            nreset=pins.nreset, target_power=pins.efuse,
            verify=False,  # bit-bang-over-serial: program reliably, skip the slow verify
        )
        assert result.ok

        # 2. Have the pod emulate a BMP280 on the DUT's I2C bus.
        #    I2C is open-drain, so enable the pod's pull-ups to idle the bus high.
        benchpod.enable_pullup(pins.i2c_sda, pins.i2c_scl)
        benchpod.enable_i2c_sensor(
            bp.Sensor.BMP280,
            sda=pins.i2c_sda, scl=pins.i2c_scl,
            address=bp.BMP280_ADDR_PRIMARY,
            temperature_c=22.5, pressure_pa=101000,
        )

        # 3. Power-cycle the DUT and capture its boot log over UART. The power-on is
        #    scheduled (delay=1.5s) so it lands *inside* the capture window — that way
        #    we don't miss the boot banner. Stop at APP_OK, which the app prints last
        #    (after the BMP280 probe), so the capture contains both markers.
        cap = benchpod.power_cycle_and_capture(
            rx=pins.uart_rx, tx=pins.uart_tx, efuse=pins.efuse,
            delay=1.5, duration=8.0, until=APP_OK,
        )

        # 4. Assert the DUT booted and detected the emulated sensor.
        assert cap.match(APP_OK), f"no APP_OK banner:\n{cap.text}"
        assert cap.match(PRESENT), f"BMP280 not detected:\n{cap.text}"
        assert benchpod.i2c_sensor_status().get("transactions", 0) > 0
    finally:
        # Always leave the DUT powered down — cut the eFuse whether the test
        # passed or failed so the bench is left in a safe, known state.
        benchpod.power_off(pins.efuse)
