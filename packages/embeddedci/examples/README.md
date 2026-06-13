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

The defaults match a typical bench; override any pin with `--benchpod-<name>`:

| DUT pin | Pod LA channel | Flag |
|---|---|---|
| SWCLK | LA11 | `--benchpod-swclk` |
| SWDIO | LA12 | `--benchpod-swdio` |
| NRST | LA3 | `--benchpod-nreset` |
| UART TX (DUT→pod samples) | LA5 | `--benchpod-uart-rx` |
| UART RX (pod→DUT drives) | LA4 | `--benchpod-uart-tx` |
| I2C SDA (LA1–8 have pull-ups) | LA2 | `--benchpod-i2c-sda` |
| I2C SCL (LA1–8 have pull-ups) | LA1 | `--benchpod-i2c-scl` |
| Target 5V eFuse (1=int, 2=ext) | — | `--benchpod-efuse` |

### Notes

- **Serial vs wifi.** Over a serial console, SWD flashing is bit-banged and
  latency-bound — fine, but a ~30KB image takes a couple of minutes. The
  wifi/TCP transport is faster. Either way, `verify=False` keeps flashing snappy
  (programming is reliable; the read-back verify is the slow part).
- The example expects a firmware that prints `APP_OK` and a BMP280 probe line
  like `chip id match=0x58`. See `examples/scenario-sensors-stm32` in the
  firmware repo for a DUT app that does exactly that.
