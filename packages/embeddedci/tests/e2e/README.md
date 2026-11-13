# End-to-end tests

The unit suite runs against fakes. These tests run the 2.x SDK against a **real BenchPod** (and a
real board for the DUT tier). Every tier skips cleanly when its prerequisites are missing, so they
are safe in CI.

```bash
# from the repo root — pod + DUT + MCP server + OpenHTF plug
make e2e POD=192.168.1.215 FIRMWARE=../examples/scenario-sensors-stm32/build/scenario-sensors.elf

# a rev3 (v3) pod: REV=v3 makes a misdetected board fail instead of passing as v2
make e2e POD=192.168.1.215 FIRMWARE=… REV=v3

# a digital-only pod (no analog board): BOARD=digital fails the run on an analog pod and the
# reverse, so a front end that went undetected cannot skip the analog tests and pass green
make e2e POD=192.168.1.215 FIRMWARE=… BOARD=digital

# add the USB console and the cloud tiers
make e2e POD=192.168.1.215 FIRMWARE=… USB=/dev/cu.usbmodem357D377F31331
BENCHPOD_API_KEY=eci_… make e2e-cloud CLOUD_DEVICE=benchpod-v2.0.0
```

| Tier | File | Needs | What it proves |
|---|---|---|---|
| pod | `test_e2e_pod.py` | `--benchpod-connection` | status/capabilities/typed state, LA voltage, rev3-only features, argument validation, firmware errors, every analog path, DAC codes vs the SDK volts mapping, DAC→ADC readback, `generate` levels in volts, `route=False` loopbacks, replay (shallow, co-triggered, deep on the deep image, refused on the loop image), `stop_dac_after`, shallow + deep ADC, LA + correlated capture, LA step pulses, the achieved LA rate at rounded and burst-capped rates, control loop (fixed/sweep/input map), gateware image round trip and automatic switching (control loop, deep replay, `switch_image=False`), bias resistors, sensor emulation (library sensors, two on one bus, faults, a stimulus, the NMEA GPS), UART session plumbing, CAN loopback + responder |
| gpio | `test_e2e_gpio.py` | `--benchpod-connection`; firmware with `la_pins`, gateware v35+ for triggers | GPIO output/open-drain read-back, pin ownership (UART refused on a GPIO pin and the reverse, release), bias-resistor conflicts both ways, step trains on GPIO pins, LA-voltage lock (rev3), wiring signal names with active-low, rising/level triggers, trigger timeout, triggered ADC capture, FPGA pulse widths measured from the trigger |
| rev3 | `test_e2e_rev3.py` | `--benchpod-connection` on a rev3 pod; `BENCHPOD_E2E_NRST_LA` for the wire checks | board strap (v3, ~3.0 V), 1.8 V bank read back from the mux status pin, pull-ups released and refused at 1.8 V, GPIO + capture on the 1.8 V bank, reset pin hold/release/pulse, pulses capped at 1 s, and with the jumper: the reset line open-drain to the bank rail at 3.3 V and 1.8 V and the pulse width on the wire; USB-C CC report consistency (and a cable seen with `BENCHPOD_E2E_USB_CC=1`) |
| dut | `test_e2e_dut.py` | + `--benchpod-firmware` and the wired board | SWD flash, boot banner via power cycle, power monitor + eFuse see the board, interactive console (help/status/reset), firmware detects the emulated BMP280, firmware reports a missing sensor, the emulated sensor's bus capture sees the boot probe, a 4 s logic capture of the boot decodes the chip-id read (and every transaction the pod served) and the UART banner; power profiles of a boot, of a delayed power-on and of an unpowered rail; with `BENCHPOD_E2E_NRESET=1` (rev3): flash under reset, a reset pulse reboots the DUT with the rail up, a held reset keeps it quiet until released |
| digital | `test_e2e_digital.py` | `--benchpod-connection` on a digital-only pod (skips on the analog board) | status says `analog: false` and drops the analog capabilities; every analog call (DAC, ADC, 4-20 mA, ADC capture, correlated capture with ADC samples, generate, replay) fails fast with `UnsupportedFeatureError` (`feature="analog"`), raw commands carry the pod's own refusal, `dac_stop` still works and the pod still answers and captures LA data afterwards |
| usb | `test_e2e_usb.py` | `BENCHPOD_E2E_USB` | the text console: status/ping/LA voltage/power, fail-fast hints for everything else |
| cloud | `test_e2e_cloud.py` | `BENCHPOD_E2E_CLOUD_DEVICE` + `BENCHPOD_API_KEY` | lease, command channel, tunnel captures, commands during a UART session, waveform library save → replay → delete |

The MCP server and the OpenHTF plug have their own e2e files, run by `make e2e` too:
`packages/embeddedci-mcp/tests/test_e2e_mcp.py` and
`packages/embeddedci-openhtf/tests/test_e2e_openhtf.py`.

## Digital-only board

The digital-only BenchPod (digital-v3, digital-v3r2) has no analog front end: its firmware reports
`analog: false` and refuses the analog commands. Tests that need the DAC, ADC or 4-20 mA carry
`ANALOG` (`@pytest.mark.benchpod_capability("analog")`, from `e2e_helpers.py`) or call
`require_analog(pod)` when the pod is not the `benchpod` fixture (the cloud tier); they skip there.
Tests that mix analog and digital checks are split, so the digital half still runs. The MCP and
OpenHTF e2e files skip their analog tests the same way. Set `BENCHPOD_EXPECT_BOARD=analog` or
`digital` (`make e2e BOARD=…`) on a bench whose board you know: a pod reporting the other one fails
the run.

## Bench configuration

The LA voltage (3.3 V) is set once in `tests/conftest.py`. The wiring defaults in `conftest.py`
match the EmbeddedCI bench — a NUCLEO-F446RE running `examples/scenario-sensors-stm32`:

| Signal | Default | Override |
|---|---|---|
| DUT UART TX → pod | LA3 | `BENCHPOD_E2E_UART_RX` |
| DUT UART RX ← pod | LA4 | `BENCHPOD_E2E_UART_TX` |
| I2C SDA / SCL | LA2 / LA1 | `BENCHPOD_E2E_I2C_SDA` / `_I2C_SCL` |
| SWCLK / SWDIO | LA11 / LA12 | `BENCHPOD_E2E_SWCLK` / `_SWDIO` |
| PCB revision the pod must report | any | `BENCHPOD_E2E_BOARD_REV=v3` (`make e2e REV=v3`) |
| Board the pod must report (analog or digital-only) | any | `BENCHPOD_EXPECT_BOARD=digital` (`make e2e BOARD=digital`) |
| DUT reset on the pod's reset pin (J1 pin 22) | no | `BENCHPOD_E2E_NRESET=1` |
| LA channel jumpered to the reset pin | none | `BENCHPOD_E2E_NRST_LA` (e.g. 8) |
| Pod's USB-C port plugged into a host | no | `BENCHPOD_E2E_USB_CC=1` |
| DAC 0-5 V output wired to the ADC's front SMA | no | `BENCHPOD_E2E_EXT_LOOP=1` |
| DUT power rail | eFuse 1 | `BENCHPOD_E2E_EFUSE` |
| OpenOCD target | `target/stm32f4x.cfg` | `BENCHPOD_E2E_TARGET_CFG` |
| Unwired LA channels (the tests drive them) | 9, 10 | `BENCHPOD_E2E_FREE_LA=9,10` or `none` |
| LA13/LA14 with nothing driving them | 13, 14 | `BENCHPOD_E2E_TOP_LA=13,14` or `none` |
| Biased channel nothing uses (LA1-LA6, pull-up) | LA6 | `BENCHPOD_E2E_FREE_PULL_LA` or `none` |
| Pulled-down channel nothing is attached to (LA7/LA8) | LA7 | `BENCHPOD_E2E_FREE_PULLDOWN_LA` or `none` |
| Where the emulated GPS transmits (a DUT UART RX line, or a free channel) | first free channel | `BENCHPOD_E2E_UART_SENSOR_TX` |
| Highest DAC output voltage | 3.3 V | `BENCHPOD_E2E_DAC_MAX_V` |
| Drive the bipolar 12v output (±1 V) | no | `BENCHPOD_E2E_ALLOW_12V=1` |
| Something connected to the 4-20 mA input (J8) | no, J8 is open | `BENCHPOD_E2E_J8_WIRED=1` |

## Bench profiles

Some tests drive LA channels as outputs (step trains, GPIO, triggers, a UART session, the pull
resistors), so they need channels nothing else drives. A test that uses one carries a
`bench_needs` marker (`FREE_LA`, `TOP_LA`, `FREE_PULL_LA`, `FREE_PULLDOWN_LA`, `UART_SENSOR_TX`
from `e2e_bench.py`). Setting the variable to `none` says the bench has no such channel: those
tests skip at collection, before anything talks to the pod, and the skip reason names the variable.
A malformed value (one channel where two are needed, LA7 as a pull-up channel) stops the run with a
usage error instead of driving the wrong pin.

**Free-channel bench** (the defaults; `examples/scenario-sensors-stm32`): LA9/LA10, LA13/LA14 and
the LA6/LA7 resistors have nothing attached. No variables needed.

**Fully wired process-node bench** (`examples/process-node-stm32`, wiring in its README): all 14
channels go to the NUCLEO, and several of them are DUT outputs (LA7 USART3 TX, LA9 SCK, LA10 MOSI,
LA14 CS) or reach a DUT pin through a resistor (LA6). Driving them would fight the DUT, or
back-power it through its I/O pins while its rail is off. Run with:

```bash
BENCHPOD_E2E_FREE_LA=none BENCHPOD_E2E_TOP_LA=none \
BENCHPOD_E2E_FREE_PULL_LA=none BENCHPOD_E2E_FREE_PULLDOWN_LA=none \
BENCHPOD_E2E_UART_SENSOR_TX=8 \
    make e2e POD=<pod host> REV=v3
```

`BENCHPOD_E2E_UART_SENSOR_TX=8` lets the GPS test transmit into the DUT's USART3 RX (PC11, an input
until the firmware opens it), the line the process node's own UART sensor tests use; the capture
reads it back from LA8. What skips there: the LA step-train, achieved-rate, above-12 MS/s,
pre-trigger and host-clock tests in `test_e2e_pod.py`, the pull-up toggle, the UART session on free
channels, every GPIO/ownership/trigger test in `test_e2e_gpio.py`, the 1.8 V pull-up and GPIO tests
in `test_e2e_rev3.py`, the LA13/LA14 drive tests in `test_e2e_la13_14.py`, and the UART-session and
LA13/LA14 tests of the cloud tier. Everything else runs: captures (which only observe all 14
channels), the analog tiers, sensor emulation on the I2C pins, the pull-resistor directions, the
14-channel pin table, the GPS on LA8 and the reset pin. The DUT tier needs `FIRMWARE=`: it flashes
that app onto the NUCLEO, so reflash the process-node firmware afterwards.

The MCP server's e2e file (`packages/embeddedci-mcp/tests/test_e2e_mcp.py`), which `make e2e` also
runs, reads the same profile from `e2e_bench.py`: there `test_top_la_channels` skips and
`test_gps_sensor` transmits on LA8. The OpenHTF e2e file drives only the DUT console.

## Not covered here

- **Rev3-only hardware** is in `test_e2e_rev3.py` and skips on a v2 pod, where `test_e2e_pod.py`
  asserts the firmware refuses it instead. Not covered: the 1.8 V bank measured in volts (the
  test reads the mux's status pin, not the rail) and a DUT that runs at 1.8 V.
- **OTA**, provisioning (Wi-Fi/cloud config) and device identity: not in the SDK; see the Go
  `embeddedci-server/hwe2e` suite for OTA.
- **Server-side replay/capture endpoints** beyond the waveform library: `hwe2e` (`TestCloud_*`).
