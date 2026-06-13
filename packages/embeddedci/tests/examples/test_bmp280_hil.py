"""BMP280 hardware-in-the-loop example.

Flashes the ``examples/scenario-sensors-stm32`` DUT app, has the pod emulate a
BMP280 on the DUT's I2C bus, power-cycles the DUT while capturing its UART, and
asserts on the boot output. Demonstrates connect + flash + I2C-sensor emulation
+ timed power + UART capture end to end.

Run against hardware:

    pytest --benchpod-connection=192.168.1.213 \
           --benchpod-firmware=/path/to/scenario-sensors.elf \
           tests/examples/test_bmp280_hil.py

Skipped automatically without a connection and a firmware image.

============================ WIRING / PIN MAP =============================
The pod's LA channels (1-12) must be wired to the DUT. Defaults below match
the --benchpod-* options; override them to match your bench.

  DUT (STM32F446, scenario-sensors-stm32)      Pod LA channel (default flag)
  ------------------------------------------   -----------------------------
  SWCLK  (SWD clock)                           --benchpod-swclk   (11)
  SWDIO  (SWD data)                            --benchpod-swdio   (12)
  NRST   (optional reset)                      --benchpod-nreset  (3)
  USART1 TX = PA9   -> pod samples (LA5)       --benchpod-uart-rx (5)
  USART1 RX = PA10  <- pod drives  (LA4)        --benchpod-uart-tx (4)
  I2C1 SDA  = PB9   <-> LA2 (4.7k pull-up)     --benchpod-i2c-sda (2)
  I2C1 SCL  = PB8   <-> LA1 (4.7k pull-up)     --benchpod-i2c-scl (1)
  Target 5V power                              --benchpod-efuse   (1 = internal)

I2C needs pull-ups: LA1-8 have switchable pull-ups (LA1/2 = 4.7k). The test
enables them on the SDA/SCL pins via `enable_pullup(...)` before arming the
emulated sensor, so the open-drain bus idles high.

The DUT app prints (see examples/scenario-sensors-stm32/main.c):
  APP_OK                                  -> booted to menu
  BMP280 init: chip id match=0x58 ...     -> sensor present
  bmp280_detected=yes / =no               -> status line
  BMP280 init FAILED ...                  -> sensor absent
==========================================================================
"""

import re
import time

import pytest

from embeddedci import benchpod as bp
from embeddedci.benchpod import i2c

APP_OK = re.compile(r"APP_OK")
PRESENT = re.compile(r"chip id match=0x58|bmp280_detected=yes")
ABSENT = re.compile(r"bmp280_detected=no|BMP280 init FAILED")

BMP280_CHIP_ID_REG = 0xD0
BMP280_CHIP_ID = 0x58

# `firmware` fixture comes from the plugin (embeddedci.benchpod.pytest_plugin).


def _flash(device, pins, firmware):
    # verify=False: over the bit-bang-SWD-over-serial link the long read-heavy
    # verify phase is unreliable (it can drop the link even though programming
    # finished). Programming itself is solid, so we skip verify for serial; for
    # the wifi/TCP transport you can leave verify on.
    result = device.flash(
        file=firmware,
        target="target/stm32f4x.cfg",
        swclk=pins.swclk,
        swdio=pins.swdio,
        nreset=pins.nreset,
        target_power=pins.efuse,
        verify=False,
    )
    assert result.ok


def test_app_boots_with_bmp280(benchpod_sensor, benchpod_pins, firmware):
    device, pins = benchpod_sensor, benchpod_pins
    _flash(device, pins, firmware)

    # I2C is open-drain: enable the pod's pull-ups on SDA/SCL so the bus idles
    # high, then have the pod pretend to be a BMP280 on those lines.
    device.enable_pullup(pins.i2c_sda, pins.i2c_scl)
    device.enable_i2c_sensor(
        bp.Sensor.BMP280, sda=pins.i2c_sda, scl=pins.i2c_scl,
        address=bp.BMP280_ADDR_PRIMARY, temperature_c=22.5, pressure_pa=101000,
    )

    cap = device.power_cycle_and_capture(
        rx=pins.uart_rx, tx=pins.uart_tx, efuse=pins.efuse,
        delay=1.5, duration=6.0, until=PRESENT,
    )

    assert cap.match(APP_OK), f"no APP_OK banner:\n{cap.text}"
    assert cap.match(PRESENT), f"BMP280 not detected:\n{cap.text}"

    # The DUT actually probed the emulated sensor.
    assert device.i2c_sensor_status().get("transactions", 0) > 0


def test_app_boots_without_bmp280(benchpod, benchpod_pins, firmware):
    device, pins = benchpod, benchpod_pins
    _flash(device, pins, firmware)
    device.disable_i2c_sensor()  # ensure nothing answers on I2C

    cap = device.power_cycle_and_capture(
        rx=pins.uart_rx, tx=pins.uart_tx, efuse=pins.efuse,
        delay=1.5, duration=6.0, until=ABSENT,
    )

    assert cap.match(APP_OK), f"no APP_OK banner:\n{cap.text}"
    assert cap.match(ABSENT), f"expected BMP280 absent:\n{cap.text}"


def test_bmp280_i2c_bus_decode(benchpod_sensor, benchpod_pins, firmware):
    """Prove at the protocol level that the DUT read the chip-id register.

    Decodes the actual I2C waveform the pod sampled while serving the emulated
    BMP280. No UART proxy here, so it's a single clean flow on one connection:
    power on (returns immediately), then a blocking ``sensor_la`` capture runs
    while the DUT boots and probes the sensor a few ms later. We sweep a handful
    of back-to-back capture windows to span the boot-to-probe interval.
    """
    device, pins = benchpod_sensor, benchpod_pins
    _flash(device, pins, firmware)
    device.enable_pullup(pins.i2c_sda, pins.i2c_scl)
    device.enable_i2c_sensor(
        bp.Sensor.BMP280, sda=pins.i2c_sda, scl=pins.i2c_scl,
        address=bp.BMP280_ADDR_PRIMARY, temperature_c=22.5, pressure_pa=101000,
    )

    device.power_off(pins.efuse)
    time.sleep(0.3)
    device.power_on(pins.efuse)  # returns immediately; capture below covers boot

    # ~6 windows of ~33 ms (4096 bytes @ 0.5 MS/s = 5 samples/bit @ 100 kHz)
    # collectively span ~200 ms — enough to catch the one-shot boot probe.
    txns = []
    chip_id = None
    for _ in range(6):
        txns = device.i2c_sensor_la_decoded(samples=4096, sample_rate_mhz=0.5)
        chip_id = i2c.read_register(txns, bp.BMP280_ADDR_PRIMARY, BMP280_CHIP_ID_REG)
        if chip_id is not None:
            break

    print("decoded I2C bus:\n" + i2c.format_transactions(txns))
    assert i2c.addressed(txns, bp.BMP280_ADDR_PRIMARY), \
        "no I2C traffic to the BMP280 address was captured"
    assert chip_id == [BMP280_CHIP_ID], \
        f"expected chip-id read 0x{BMP280_CHIP_ID:02X}, decoded {chip_id}"

    # Cross-check against the pod's own transaction counters.
    assert device.i2c_sensor_status().get("transactions", 0) > 0
