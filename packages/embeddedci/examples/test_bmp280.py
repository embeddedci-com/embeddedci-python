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
# Wiring — the pod has no dedicated SWD/UART/I2C pins. It exposes 12 generic LA
# channels (pins.pin_1 .. pins.pin_12) and any DUT signal can be on any of them.
# The `wiring` fixture below is THIS bench's map; edit it to match your board:
#
#   DUT pin                         Pod LA channel   wiring fixture
#   ------------------------------  --------------   --------------
#   SWCLK                           LA11             wiring.swclk
#   SWDIO                           LA12             wiring.swdio
#   NRST                            LA3              wiring.nreset
#   UART TX  (DUT -> pod samples)   LA5              wiring.uart_rx
#   UART RX  (pod -> DUT drives)    LA4              wiring.uart_tx
#   I2C SDA  (needs a pull-up)      LA2 (4.7k)       wiring.i2c_sda
#   I2C SCL  (needs a pull-up)      LA1 (4.7k)       wiring.i2c_scl
#   5V power eFuse (1=int, 2=ext)   --benchpod-efuse
#
# Pull-ups exist only on LA1-8 (LA1/2=4.7k, LA3/4=2.2k, LA5-8=10k), so the I2C
# lines must sit on one of those. The `benchpod`, `pins` and `firmware` fixtures
# come from the installed plugin; the test skips if no --benchpod-connection is set.

import re
from types import SimpleNamespace

import pytest
from embeddedci import benchpod as bp

APP_OK = re.compile(r"APP_OK")
PRESENT = re.compile(r"chip id match=0x58|bmp280_detected=yes")


@pytest.fixture
def wiring(pins):
    """This bench's wiring: DUT signal → BenchPod LA channel. Bench-specific —
    any signal can be on any LA channel, except I2C SDA/SCL need a pull-up (LA1-8)."""
    return SimpleNamespace(
        swclk=pins.pin_11, swdio=pins.pin_12, nreset=pins.pin_3,
        uart_rx=pins.pin_5, uart_tx=pins.pin_4,
        i2c_sda=pins.pin_2, i2c_scl=pins.pin_1,  # LA1/2 — 4.7k pull-ups
        efuse=pins.efuse,
    )


@pytest.mark.hardware
def test_bmp280_sensor_present(benchpod, wiring, firmware):
    """Flash the DUT, emulate a BMP280 on I2C, power-cycle, assert on UART."""

    try:
        # 1. Flash the firmware onto the DUT over SWD.
        result = benchpod.flash(
            file=firmware,
            target="target/stm32f4x.cfg",
            swclk=wiring.swclk, swdio=wiring.swdio,
            nreset=wiring.nreset, target_power=wiring.efuse,
            verify=False,  # bit-bang-over-serial: program reliably, skip the slow verify
        )
        assert result.ok

        # 2. Have the pod emulate a BMP280 on the DUT's I2C bus.
        #    I2C is open-drain, so enable the pod's pull-ups to idle the bus high.
        benchpod.enable_pullup(wiring.i2c_sda, wiring.i2c_scl)
        benchpod.enable_i2c_sensor(
            bp.Sensor.BMP280,
            sda=wiring.i2c_sda, scl=wiring.i2c_scl,
            address=bp.BMP280_ADDR_PRIMARY,
            temperature_c=22.5, pressure_pa=101000,
        )

        # 3. Power-cycle the DUT and capture its boot log over UART. The power-on is
        #    scheduled (delay=1.5s) so it lands *inside* the capture window — that way
        #    we don't miss the boot banner. Stop at APP_OK, which the app prints last
        #    (after the BMP280 probe), so the capture contains both markers.
        cap = benchpod.power_cycle_and_capture(
            rx=wiring.uart_rx, tx=wiring.uart_tx, efuse=wiring.efuse,
            delay=1.5, duration=8.0, until=APP_OK,
        )

        # 4. Assert the DUT booted and detected the emulated sensor.
        assert cap.match(APP_OK), f"no APP_OK banner:\n{cap.text}"
        assert cap.match(PRESENT), f"BMP280 not detected:\n{cap.text}"
        assert benchpod.i2c_sensor_status().get("transactions", 0) > 0
    finally:
        # Always leave the DUT powered down — cut the eFuse whether the test
        # passed or failed so the bench is left in a safe, known state.
        benchpod.power_off(wiring.efuse)
