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
The pod has no dedicated SWD/UART/I2C pins — it exposes 14 generic LA channels
(``pins.pin_1`` .. ``pins.pin_14``) and any DUT signal can be on any of them.
The ``wiring`` fixture below is THIS bench's map; edit it for your board.

  DUT (STM32F446, scenario-sensors-stm32)      Pod LA channel (wiring fixture)
  ------------------------------------------   ------------------------------
  SWCLK  (SWD clock)                           LA11   (wiring.swclk)
  SWDIO  (SWD data)                            LA12   (wiring.swdio)
  NRST   (optional reset)                      not wired (wiring.nreset = False;
                                               rev3 pods: J1 pin 22 -> True)
  USART1 TX = PA9   -> pod samples             LA3    (wiring.uart_rx)
  USART1 RX = PA10  <- pod drives              LA4    (wiring.uart_tx)
  I2C1 SDA  = PB9   <-> LA2 (4.7k pull-up)     LA2    (wiring.i2c_sda)
  I2C1 SCL  = PB8   <-> LA1 (4.7k pull-up)     LA1    (wiring.i2c_scl)
  Target 5V power                              --benchpod-efuse (1 = internal)

I2C needs pull-ups, and only LA1-8 have them (LA1/2=4.7k, LA3/4=2.2k, LA5-8=10k);
LA9-12 have none. So the I2C lines must sit on a pull-up-capable channel — here
LA1/LA2. The test enables them via `enable_pullup(...)` before arming the
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
from types import SimpleNamespace

import pytest

from embeddedci import benchpod as bp
from embeddedci.benchpod import i2c


@pytest.fixture
def wiring(pins):
    """This bench's wiring: DUT signal → BenchPod LA channel (see the PIN MAP
    in the module docstring). Bench-specific — any signal can be on any of the 12
    channels, except that I2C SDA/SCL must sit on a pull-up-capable LA (1-8)."""
    return SimpleNamespace(
        swclk=pins.pin_11,
        swdio=pins.pin_12,
        nreset=False,  # no reset wire on this bench (rev3 pods: J1 pin 22 -> True)
        uart_rx=pins.pin_3,    # pod samples the DUT's TX
        uart_tx=pins.pin_4,    # pod drives the DUT's RX
        i2c_sda=pins.pin_2,    # LA2 — 4.7k pull-up
        i2c_scl=pins.pin_1,    # LA1 — 4.7k pull-up
        efuse=pins.efuse,
    )

# Seconds from power-on to APP_OK. The app probes its sensors with retries, so the "no sensor"
# path is the slow one; `until=APP_OK` returns as soon as the banner lands, so a generous budget
# costs nothing when the board is healthy.
BOOT_TIMEOUT = 25.0
# Roughly when the app probes the emulated BMP280, in seconds after power-on. A decode window is
# only ~33 ms wide, so it has to be aimed rather than fired immediately (see the decode test).
BMP280_PROBE_AT = 2.31

APP_OK = re.compile(r"APP_OK")
PRESENT = re.compile(r"chip id match=0x58|bmp280_detected=yes")
ABSENT = re.compile(r"bmp280_detected=no|BMP280 init FAILED")

BMP280_CHIP_ID_REG = 0xD0
BMP280_CHIP_ID = 0x58

# `firmware` fixture comes from the plugin (embeddedci.benchpod.pytest_plugin).


def _flash(device, wiring, firmware):
    # verify=False: over the bit-bang-SWD-over-serial link the long read-heavy
    # verify phase is unreliable (it can drop the link even though programming
    # finished). Programming itself is solid, so we skip verify for serial; for
    # the wifi/TCP transport you can leave verify on.
    result = device.flash(
        file=firmware,
        target="target/stm32f4x.cfg",
        swclk=wiring.swclk,
        swdio=wiring.swdio,
        nreset=wiring.nreset,
        target_power=wiring.efuse,
        verify=False,
    )
    assert result.ok


def test_app_boots_with_bmp280(benchpod_sensor, wiring, firmware):
    device = benchpod_sensor
    _flash(device, wiring, firmware)

    # I2C is open-drain: enable the pod's pull-ups on SDA/SCL so the bus idles
    # high, then have the pod pretend to be a BMP280 on those lines.
    device.enable_pullup(wiring.i2c_sda, wiring.i2c_scl)
    device.enable_i2c_sensor(
        bp.Sensor.BMP280, sda=wiring.i2c_sda, scl=wiring.i2c_scl,
        address=bp.BMP280_ADDR_PRIMARY, temperature_c=22.5, pressure_pa=101000,
    )

    # Wait for APP_OK: it is the LAST thing the app prints, so a capture that stops at the
    # earlier BMP280 line would not contain it.
    cap = device.power_cycle_and_capture(
        rx=wiring.uart_rx, tx=wiring.uart_tx, efuse=wiring.efuse,
        delay=1.5, duration=BOOT_TIMEOUT, until=APP_OK,
    )

    assert cap.matched, f"no APP_OK within {BOOT_TIMEOUT}s:\n{cap.text[-2000:]}"
    assert cap.match(PRESENT), f"BMP280 not detected:\n{cap.text}"

    # The DUT actually probed the emulated sensor.
    assert device.i2c_sensor_status().get("transactions", 0) > 0


def test_app_boots_without_bmp280(benchpod, wiring, firmware):
    device = benchpod
    _flash(device, wiring, firmware)
    device.disable_i2c_sensor()  # ensure nothing answers on I2C

    cap = device.power_cycle_and_capture(
        rx=wiring.uart_rx, tx=wiring.uart_tx, efuse=wiring.efuse,
        delay=1.5, duration=BOOT_TIMEOUT, until=APP_OK,
    )

    assert cap.matched, f"no APP_OK within {BOOT_TIMEOUT}s:\n{cap.text[-2000:]}"
    assert cap.match(ABSENT), f"expected BMP280 absent:\n{cap.text}"


def test_bmp280_i2c_bus_decode(benchpod_sensor, wiring, firmware):
    """Prove at the protocol level that the DUT read the chip-id register.

    Decodes the actual I2C waveform the pod sampled while serving the emulated
    BMP280. No UART proxy here, so it's a single clean flow on one connection:
    power on (returns immediately), then a blocking ``sensor_la`` capture runs
    while the DUT boots and probes the sensor.

    Timing is the whole trick: 4096 samples at 500 kS/s is only ~8 ms of bus, while the DUT does
    not probe until ~2.3 s after power-on. Firing windows back-to-back from power-on therefore
    captures nothing but idle. Aim the window instead — sleep the expected lead, capture, and nudge
    the lead on a miss (an idle window was early; a window with other traffic was late).
    """
    device = benchpod_sensor
    _flash(device, wiring, firmware)
    device.enable_pullup(wiring.i2c_sda, wiring.i2c_scl)
    device.enable_i2c_sensor(
        bp.Sensor.BMP280, sda=wiring.i2c_sda, scl=wiring.i2c_scl,
        address=bp.BMP280_ADDR_PRIMARY, temperature_c=22.5, pressure_pa=101000,
    )

    txns, chip_id, lead, misses = [], None, BMP280_PROBE_AT, []
    for _ in range(4):
        device.power_off(wiring.efuse)
        time.sleep(0.5)
        device.power_on(wiring.efuse)      # returns immediately
        time.sleep(lead)                   # let the DUT get as far as its sensor probe
        txns = device.i2c_sensor_capture(4096, sample_rate_hz=500_000)  # 5 samples per 100 kHz bit
        chip_id = i2c.read_register(txns, bp.BMP280_ADDR_PRIMARY, BMP280_CHIP_ID_REG)
        if chip_id is not None:
            break
        misses.append((round(lead, 2), len(txns)))
        lead += 0.02 if not txns else -0.02   # idle window = too early; busy window = too late

    print("decoded I2C bus:\n" + i2c.format_transactions(txns))
    assert i2c.addressed(txns, bp.BMP280_ADDR_PRIMARY), \
        f"no I2C traffic to the BMP280 address; (lead s, transactions) per miss: {misses}"
    assert chip_id == [BMP280_CHIP_ID], \
        f"expected chip-id read 0x{BMP280_CHIP_ID:02X}, decoded {chip_id}"

    # Cross-check against the pod's own transaction counters.
    assert device.i2c_sensor_status().get("transactions", 0) > 0
