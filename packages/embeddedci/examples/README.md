# BenchPod examples

A runnable, copy-pasteable starting point for hardware-in-the-loop tests.

## `test_bmp280.py` — flash, emulate a sensor, assert on UART

One self-contained test that does the whole HIL loop:

1. **Flash** your firmware onto the DUT over SWD.
2. **Emulate a BMP280** on the DUT's I2C bus (the pod becomes the sensor).
3. **Power-cycle** the DUT (scheduled power-on, so the boot banner lands in view).
4. **Capture the UART** and assert the app booted (`APP_OK`) and detected the
   sensor (`chip id match=0x58`).

### Run it

```bash
pip install embeddedci          # also needs `openocd` on PATH for flashing

pytest examples/test_bmp280.py \
    --benchpod-connection=/dev/tty.usbserial-0001 \   # or an IP for wifi
    --benchpod-firmware=path/to/your_app.elf
```

Without `--benchpod-connection` the test **skips** (so it's safe in CI). The
`benchpod`, `pins` and `firmware` fixtures are provided by the installed plugin.

### Wiring

The pod has **no dedicated SWD/UART/I2C pins** — it exposes 12 generic LA channels
(`pins.pin_1` … `pins.pin_12`) and any DUT signal can be on any of them. The example
maps its own wiring in a `wiring` fixture at the top of `test_bmp280.py`; the table
below is what that fixture uses — edit it to match your board.

| DUT pin | Pod LA channel | `wiring` field |
|---|---|---|
| SWCLK | LA11 | `wiring.swclk` |
| SWDIO | LA12 | `wiring.swdio` |
| NRST | LA3 | `wiring.nreset` |
| UART TX (DUT→pod samples) | LA5 | `wiring.uart_rx` |
| UART RX (pod→DUT drives) | LA4 | `wiring.uart_tx` |
| I2C SDA (needs a pull-up) | LA2 | `wiring.i2c_sda` |
| I2C SCL (needs a pull-up) | LA1 | `wiring.i2c_scl` |
| Target 5V eFuse (1=int, 2=ext) | — | `--benchpod-efuse` |

Pull-ups exist only on **LA1–8** (LA1/2 = 4.7k, LA3/4 = 2.2k, LA5–8 = 10k); LA9–12
have none, so the open-drain I2C lines must sit on a pull-up-capable channel.

### Notes

- **Serial vs wifi.** Over a serial console, SWD flashing is bit-banged and
  latency-bound — fine, but a ~30KB image takes a couple of minutes. The
  wifi/TCP transport is faster. Either way, `verify=False` keeps flashing snappy
  (programming is reliable; the read-back verify is the slow part).
- The example expects a firmware that prints `APP_OK` and a BMP280 probe line
  like `chip id match=0x58`. See `examples/scenario-sensors-stm32` in the
  firmware repo for a DUT app that does exactly that.
