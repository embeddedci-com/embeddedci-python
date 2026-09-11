# BenchPod examples

A runnable, copy-pasteable starting point for hardware-in-the-loop tests.

## `test_bmp280.py` — flash, emulate a sensor, assert on UART and I2C

One self-contained test that does the whole HIL loop:

1. **Flash** your firmware onto the DUT over SWD.
2. **Emulate a BMP280** on the DUT's I2C bus (the pod becomes the sensor), with the
   pod's pull-ups engaged on SDA/SCL.
3. **Power-cycle** the DUT (the power-on is scheduled pod-side, so the boot banner lands
   inside the capture window).
4. **Capture the UART** and assert the app booted (`APP_OK`) and detected the sensor
   (`chip id match=0x58`).
5. **Boot again and decode the I2C bus** with `i2c_sensor_capture`, asserting the DUT
   read the BMP280's chip-id register (`0xD0` → `0x58`).

### Run it

```bash
pip install "embeddedci[pytest]"   # flashing also needs an OpenOCD with the cmsis_dap_tcp backend

pytest examples/test_bmp280.py \
    --benchpod-connection=192.168.1.213 \
    --benchpod-firmware=path/to/your_app.elf
```

`--benchpod-connection` also accepts `usb` (auto-detect the USB console), a serial device
path such as `/dev/ttyACM0`, `discover` (mDNS), or `embeddedci:<device-name>` for a pod
reached through embeddedci.com (see the package README for cloud authentication).

The board's I/O voltage is set once, by the `benchpod_la_voltage` fixture at the top of
`test_bmp280.py` (3.3 V). The pod refuses flashing, UART, LA capture, pull resistors and
I2C-sensor emulation until an LA bank voltage is chosen. **Change it to 1.8 for a 1V8
board** — note the pull-up resistors only work at 3.3 V. In your own project put that fixture
in `conftest.py` so every test file shares it.

Without a connection or a firmware image the test **skips** (so it's safe in CI). The
`benchpod_sensor`, `pins` and `firmware` fixtures are provided by the installed plugin.

### Wiring

The pod has **no dedicated SWD/UART/I2C pins** — it exposes 12 generic LA channels
(`pins.pin_1` … `pins.pin_12`) and any DUT signal can be on any of them. The example
maps its own wiring in a `wiring` fixture at the top of `test_bmp280.py`; the table
below is what that fixture uses — edit it to match your board.

| DUT pin | Pod | `wiring` field |
|---|---|---|
| SWCLK | LA11 | `wiring.swclk` |
| SWDIO | LA12 | `wiring.swdio` |
| NRST | J1 pin 22 (the pod's reset pin, not an LA channel) | `wiring.nreset` (`True`) |
| UART TX (DUT → pod samples) | LA5 | `wiring.uart_rx` |
| UART RX (pod → DUT drives) | LA4 | `wiring.uart_tx` |
| I2C SDA (needs a pull-up) | LA2 (4.7k pull-up) | `wiring.i2c_sda` |
| I2C SCL (needs a pull-up) | LA1 (4.7k pull-up) | `wiring.i2c_scl` |
| Target 5V power | eFuse 1 = internal, 2 = external | `--benchpod-efuse` |

Bias resistors on the LA channels:

| Channels | Resistor | Value |
|---|---|---|
| LA1, LA2 | pull-up | 4.7k |
| LA3, LA4 | pull-up | 2.2k |
| LA5, LA6 | pull-up | 10k |
| LA7, LA8 | pull-**down** | 10k |
| LA9–LA12 | none | — |

The open-drain I2C lines must sit on a pull-up channel (LA1–LA6). The resistors are
referenced to 3V3, so the pod will not engage them while the LA bank is at 1.8 V.

### Notes

- **USB vs network.** Flashing works over every transport, but over the USB console each
  CMSIS-DAP packet round-trips the serial link, so a large image is noticeably slower than
  over TCP. `verify=False` keeps flashing snappy either way (programming is reliable; the
  read-back verify is the slow part).
- The example expects a firmware that prints `APP_OK` and a BMP280 probe line like
  `chip id match=0x58`. See `examples/scenario-sensors-stm32` in the firmware repo for a
  DUT app that does exactly that.
- A multi-case version (sensor present / absent, plus I2C-bus decode as a separate test)
  lives in [`tests/examples/test_bmp280_hil.py`](../tests/examples/test_bmp280_hil.py).
