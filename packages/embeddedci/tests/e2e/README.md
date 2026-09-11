# End-to-end tests

The unit suite runs against fakes. These tests run the 2.x SDK against a **real BenchPod** (and a
real board for the DUT tier). Every tier skips cleanly when its prerequisites are missing, so they
are safe in CI.

```bash
# from the repo root — pod + DUT + MCP server + OpenHTF plug
make e2e POD=192.168.1.215 FIRMWARE=../examples/scenario-sensors-stm32/build/scenario-sensors.elf

# add the USB console and the cloud tiers
make e2e POD=192.168.1.215 FIRMWARE=… USB=/dev/cu.usbmodem357D377F31331
BENCHPOD_API_KEY=eci_… make e2e-cloud CLOUD_DEVICE=benchpod-v2.0.0
```

| Tier | File | Needs | What it proves |
|---|---|---|---|
| pod | `test_e2e_pod.py` | `--benchpod-connection` | status/capabilities/typed state, LA voltage, rev3-only features, argument validation, firmware errors, every analog path, DAC codes vs the SDK volts mapping, DAC→ADC readback, `generate` levels in volts, `route=False` loopbacks, replay (shallow, co-triggered, deep on the deep image, refused on the loop image), `stop_dac_after`, shallow + deep ADC, LA + correlated capture, LA step pulses, control loop (fixed/sweep/input map), gateware image round trip and automatic switching (control loop, deep replay, `switch_image=False`), bias resistors, I2C sensor emulation, UART session plumbing, CAN loopback + responder |
| gpio | `test_e2e_gpio.py` | `--benchpod-connection`; firmware with `la_pins`, gateware v35+ for triggers | GPIO output/open-drain read-back, pin ownership (UART refused on a GPIO pin and the reverse, release), bias-resistor conflicts both ways, step trains on GPIO pins, LA-voltage lock (rev3), wiring signal names with active-low, rising/level triggers, trigger timeout, triggered ADC capture, FPGA pulse widths measured from the trigger |
| dut | `test_e2e_dut.py` | + `--benchpod-firmware` and the wired board | SWD flash, boot banner via power cycle, power monitor + eFuse see the board, interactive console (help/status/reset), firmware detects the emulated BMP280, firmware reports a missing sensor, `i2c_sensor_capture` sees the boot probe, a 4 s logic capture of the boot decodes the chip-id read (and every transaction the pod served) and the UART banner; power profiles of a boot, of a delayed power-on and of an unpowered rail |
| usb | `test_e2e_usb.py` | `BENCHPOD_E2E_USB` | the text console: status/ping/LA voltage/power, fail-fast hints for everything else |
| cloud | `test_e2e_cloud.py` | `BENCHPOD_E2E_CLOUD_DEVICE` + `BENCHPOD_API_KEY` | lease, command channel, tunnel captures, commands during a UART session, waveform library save → replay → delete |

The MCP server and the OpenHTF plug have their own e2e files, run by `make e2e` too:
`packages/embeddedci-mcp/tests/test_e2e_mcp.py` and
`packages/embeddedci-openhtf/tests/test_e2e_openhtf.py`.

## Bench configuration

The LA voltage (3.3 V) is set once in `tests/conftest.py`. The wiring defaults in `conftest.py`
match the EmbeddedCI bench — a NUCLEO-F446RE running `examples/scenario-sensors-stm32`:

| Signal | Default | Override |
|---|---|---|
| DUT UART TX → pod | LA3 | `BENCHPOD_E2E_UART_RX` |
| DUT UART RX ← pod | LA4 | `BENCHPOD_E2E_UART_TX` |
| I2C SDA / SCL | LA2 / LA1 | `BENCHPOD_E2E_I2C_SDA` / `_I2C_SCL` |
| SWCLK / SWDIO | LA11 / LA12 | `BENCHPOD_E2E_SWCLK` / `_SWDIO` |
| DUT reset on the pod's reset pin | no | `BENCHPOD_E2E_NRESET=1` |
| DUT power rail | eFuse 1 | `BENCHPOD_E2E_EFUSE` |
| OpenOCD target | `target/stm32f4x.cfg` | `BENCHPOD_E2E_TARGET_CFG` |
| Unwired LA channels | 9, 10 | `BENCHPOD_E2E_FREE_LA=9,10` |
| Biased channel nothing uses | LA6 | `BENCHPOD_E2E_FREE_PULL_LA` |
| Highest DAC output voltage | 3.3 V | `BENCHPOD_E2E_DAC_MAX_V` |
| Drive the bipolar 12v output (±1 V) | no | `BENCHPOD_E2E_ALLOW_12V=1` |

## Not covered here

- **Rev3-only hardware** (reset pin, USB-C CC, 1.8 V bank): exercised only on a rev3 pod; on a v2
  pod the tests assert the firmware refuses them.
- **OTA**, provisioning (Wi-Fi/cloud config) and device identity: not in the SDK; see the Go
  `embeddedci-server/hwe2e` suite for OTA.
- **Server-side replay/capture endpoints** beyond the waveform library: `hwe2e` (`TestCloud_*`).
