# embeddedci — BenchPod SDK and pytest plugin

A Python SDK and pytest plugin for the [EmbeddedCI](https://embeddedci.com) **BenchPod**, a
hardware-in-the-loop instrument that sits next to your device under test (DUT). From a test you
can:

* connect to a pod over the **network**, **USB** or the **cloud** (`embeddedci:<device-name>`)
* switch and monitor **target power**, and pulse the target's **reset**
* **flash** firmware over SWD and assert it worked
* capture the DUT's **UART**, or hold an interactive UART session
* **emulate an I2C sensor** (BMP280) and decode the bus traffic
* capture **ADC** (calibrated volts), a 14-channel **logic analyzer**, or both from one trigger
* drive the **DAC**: DC levels, generated waveforms, arbitrary replay, fault injection, and an
  in-fabric **control loop**
* talk **CAN**, including an autonomous ECU simulator

The same test runs against a pod on your desk or a remote pod in CI.

```python
from embeddedci.benchpod import BenchPod

with BenchPod("192.168.1.213", la_voltage=3.3) as bp:        # or "usb", or "embeddedci:my-bench"
    bp.flash(file="build/app.elf", target="target/stm32f4x.cfg")
    boot = bp.power_cycle_and_capture(duration=5.0, until="APP_OK")
    assert boot.matched, boot.text
```

No pin numbers: the SWD channels, the UART pair, the baud and the power rail come from the bench's
[wiring profile](#wiring-profile). Pass them explicitly when you want to override it.

**Contents:** [Install](#install) · [Quick start](#quick-start) ·
[API conventions](#api-conventions-and-stability) · [pytest plugin](#pytest-plugin) ·
[Power and reset](#power-reset-and-power-monitoring) · [Flashing](#flashing) · [UART](#uart) ·
[Bias resistors](#la-bias-resistors) · [I2C sensor](#emulated-i2c-sensor) ·
[Analog paths](#analog-paths-dc-output-and-single-readings) · [Captures](#captures) ·
[DAC](#dac-generator-replay-and-faults) · [Control loop](#in-fabric-dac-control-loop) ·
[CAN](#can) · [Cloud](#cloud-embeddedcidevice-name) · [Build reporting](#build-reporting) ·
[Errors](#errors) · [Escape hatches](#escape-hatches) · [API reference](#api-reference) ·
[Releasing](#releasing-maintainers)

## Install

Requires **Python 3.10+**.

```bash
pip install embeddedci
pip install "embeddedci[cloud,analysis]"     # with extras
```

On Python 3.10 or newer this gives you the current 2.x release. On **Python 3.9 it cannot** — pip
skips 2.x and installs the 0.2.4 placeholder instead, which fails on import with a message telling
you to upgrade. (Before that placeholder existed, 3.9 quietly resolved to 0.2.3: a completely
different, pre-2.0 API, installed without any warning.)

Pinning the major version is still the most explicit form, and turns a wrong Python into a resolver
error rather than an import error:

```bash
pip install "embeddedci>=2,<3"
```

`python -c "import embeddedci; print(embeddedci.__version__)"` tells you what you actually got.

| Extra | Adds | Needed for |
|---|---|---|
| `cloud` | `websocket-client` | the `embeddedci:<device-name>` destination |
| `discovery` | `zeroconf` | `discover` connection string / `--benchpod-discover` (mDNS) |
| `analysis` | `numpy` | `Capture.fft()` and `Capture.dominant_frequency()` |
| `pytest` | `pytest` | nothing extra — the plugin registers itself; this just installs pytest |
| `dev` | `pytest`, `websocket-client`, `numpy` | working on this package |

Network and USB connections need nothing beyond the base install (`pyserial`). Capture reductions
such as mean, RMS and peak-to-peak are pure Python; only the FFT uses numpy.

### OpenOCD (required for flashing)

Flashing shells out to **OpenOCD**, which must be on your `PATH` (or passed as `openocd_bin=`).
The pod runs a **CMSIS-DAP** probe locally; the library drives it through OpenOCD's `cmsis-dap`
adapter using its **TCP backend** (`cmsis_dap_tcp`), shipping whole DAP transfers rather than
per-bit toggles. That backend is **newer than OpenOCD 0.12.0**. The stock packages
(`apt install openocd`, `brew install open-ocd`) are 0.12.0 and lack it; flashing with them fails
up front with a clear message.

Install a recent build instead — the easiest is **xPack OpenOCD**:

```bash
# macOS / Linux via npm (xpm), or grab a release tarball directly:
npm install -g @xpack-dev-tools/openocd
# or: https://github.com/xpack-dev-tools/openocd-xpack/releases  (extract, add bin/ to PATH)
```

Verify your OpenOCD has the CMSIS-DAP TCP backend:

```bash
openocd -c "adapter driver cmsis-dap" -c "cmsis-dap backend tcp" -c "exit"
# must NOT error on the 'backend tcp' line (if it errors, the build is too old)
```

## Quick start

Most people use this through **pytest** — that is what the plugin is for. Two files and you have a
hardware test that runs on your desk and in CI:

```python
# conftest.py
import pytest

@pytest.fixture(scope="session")
def benchpod_la_voltage():
    return 3.3                               # your DUT's I/O voltage, once for the whole suite
```

```python
# test_bench.py
import pytest


def test_pod_is_alive(benchpod):
    assert benchpod.ping()                   # no wiring, no firmware: the honest first green tick


@pytest.mark.hardware
def test_target_flashes_and_boots(benchpod_target, firmware):
    assert benchpod_target.flash(file=firmware, target="target/stm32f4x.cfg").ok
    boot = benchpod_target.power_cycle_and_capture(duration=5.0, until="APP_OK")
    assert boot.matched, boot.text


@pytest.mark.hardware
def test_boot_stays_inside_its_power_budget(benchpod_target):
    profile = benchpod_target.measure_power(3.0)
    assert profile.peak_current < 0.25       # amps, measured on the rail
    assert not profile.fault                 # the eFuse never tripped
```

```bash
pytest --benchpod-connection=192.168.1.213 --benchpod-firmware=build/app.elf
```

Without a connection the hardware fixtures **skip** rather than fail, so the suite stays green on a
runner with no pod. See [pytest plugin](#pytest-plugin) for every fixture, option and marker.

Driving a pod directly, without pytest:

```python
from embeddedci.benchpod import INTERNAL, BenchPod

with BenchPod("192.168.1.213", la_voltage=3.3) as bp:
    bp.ping()
    print(bp.status())                       # firmware version, board, network, features
    print(bp.capabilities.board, bp.capabilities.dac_deep_replay)
    bp.power_on(INTERNAL)
```

`BenchPod` is a context manager; `close()` is idempotent. `la_voltage` (1.8 or 3.3) selects the
**LA I/O-bank voltage** right after connecting — match it to the DUT's I/O voltage. The pod refuses
every LA-bank operation (flashing, UART, LA capture, pull resistors, I2C-sensor emulation) until one
is selected. You can also call `bp.set_la_voltage(3.3)` later; `bp.get_la_voltage()` returns a
`LaVoltage` whose `voltage` is `None` until one is chosen. 1.8 V needs a rev3 pod.

Other constructor arguments: `timeout` (seconds, default 30), `api_key`, `api_base`,
`cloud_token`, `cloud_audience`, `cloud_user_token`, `lease`, `lease_wait`, `lease_ttl` (see [Cloud](#cloud-embeddedcidevice-name))
and `transport` (inject a custom backend).

### Connection strings

| Form | Transport |
|---|---|
| `192.168.1.213`, `host:8080`, `[fe80::1]:8080` | network (JSON over TCP, port 8080 by default) |
| `/dev/ttyACM0`, `COM3`, `\\.\COM10` | USB console, explicit device path |
| `usb` | USB console, auto-detected by probing the serial ports (`serial` also accepted) |
| `discover` (or `mdns`, `auto`) | find exactly one pod on the LAN via mDNS (needs `[discovery]`); errors on zero or several |
| `embeddedci:<device-name>` | a named device through embeddedci.com (needs `[cloud]`; an API key or a user token anywhere, or GitHub Actions OIDC) |

**Use the network (or the cloud) for testing.** The STM32 pod's USB console is a text shell for
setup and diagnostics with no JSON mode, so over USB the SDK can only read `status()`, `ping()`,
select the LA voltage and switch target power (without `delay`). Everything else — flashing, UART,
analog, captures, I2C-sensor emulation, CAN — raises a `TransportError` right away that points at
the network connection. (On firmware whose console does have a JSON mode, the SDK uses it
automatically.)

| | TCP | USB (STM32 pod) | Cloud |
|---|---|---|---|
| `status`, `ping`, LA voltage, target power on/off | yes | yes | yes |
| Scheduled power changes (`delay=`), flashing, UART, analog, captures, I2C sensor, CAN, control loop | yes | no | yes |
| `replay()` / client-side `replay_waveform()` (streams the waveform to the pod) | yes | no | yes |
| Device lease | — | — | yes |

### Environment variables

| Variable | Used when | Purpose |
|---|---|---|
| `BENCHPOD_CONNECTION` | no `connection` argument / `--benchpod-connection` | connection string |
| `BENCHPOD_LA_VOLTAGE` | no `la_voltage` argument / `--benchpod-la-voltage` | LA bank voltage to select on connect (`1.8` or `3.3`) |
| `BENCHPOD_API_KEY` | no `api_key` argument / `--benchpod-api-key` | EmbeddedCI API key (`eci_…`) for the cloud destination and server features |
| `BENCHPOD_API_BASE` | no `api_base` argument / `--benchpod-api-base` | server base URL (default `https://www.embeddedci.com`) |
| `BENCHPOD_BUILD_TARGET` | no `--benchpod-build-target` | platform id recorded by the `build_report` fixture |
| `BENCHPOD_WIRING` | no `wiring` argument / `--benchpod-wiring` / `benchpod_wiring` fixture | wiring profile file (`.json`/`.toml`), see [Wiring profile](#wiring-profile) |
| `BENCHPOD_LIFT_DAC_LIMITS` | pytest sessions | `1` clears the pod's DAC output limits for the session and restores the same limits at the end, even when tests fail (only with the output stage switched off) |
| `BENCHPOD_STALL_TIMEOUT` | flashing | seconds with no SWD traffic before a flash attempt is aborted and retried (default 60) |

## API conventions and stability

2.0.0 froze the API. The conventions hold everywhere:

* **Units are volts, seconds and hertz** — `sample_rate_hz`, `delay`, `duration`, `stop_dac_after`,
  `pulse`; readings come back in volts and amps. (Raw DAC/ADC *codes* are named as such.)
* **Invalid arguments raise `ValueError`**. A `BenchPodError` subclass always means a device,
  transport or server failure — see [Errors](#errors).
* **Device state is typed.** Methods return frozen dataclasses (`LaVoltage`, `TargetStatus`,
  `PowerStatus`, `PullState`, `DacOutput`, `CurrentOutput`, `AdcReading`, …) from `embeddedci.benchpod.state`; each
  keeps the untouched firmware reply in `.raw`.
* **String options are `Literal` types** and are validated: `DacPath`, `DacOutputPath`,
  `AnalogPath`, `AdcSource`, `LoopSource`, `Waveshape`, `ReplayMapping`, `DecodeProtocol`,
  `CanMode`, `FaultType`, `GpioMode`, `Edge`, `TriggerEdge`.
* **Named constants instead of magic numbers**:

  | Concept | Constants | Wire value |
  |---|---|---|
  | Target-power eFuse | `INTERNAL`, `EXTERNAL` (`Efuse`) | 1, 2 |
  | LA channels | `PIN1` … `PIN14` (`Pin`) | 1 … 14 |
  | Emulated sensor | `Sensor.BMP280` | `"bmp280"` |
  | BMP280 addresses | `BMP280_ADDR_PRIMARY`, `BMP280_ADDR_SECONDARY` | 0x76, 0x77 |
  | Gateware image | `FpgaImage.LOOP`, `FpgaImage.DEEP_REPLAY` | 0, 1 |

  Plain ints work too (`efuse=1`, `swclk=11`); they are validated. The target's reset line is not an
  LA channel — it has a dedicated pin (see [Flashing](#flashing)).

**Feature checks.** Pods differ by board, firmware and gateware. `bp.supports("spi_master")` says
whether the pod has any `Capabilities` flag (`False` for a flag this SDK does not know), and the
properties `can_gpio`, `can_trigger`, `can_spi`, `can_profile_power`, `can_calibrate`,
`can_current_out`, `can_reset_target` and `can_analog` name the common ones (`can_config` …
`can_disable` stay the CAN bus commands). A call the pod cannot serve raises
`UnsupportedFeatureError` (see [Errors](#errors)), before anything is sent when the capabilities
already say so:

```python
import pytest


def test_flash_spi_nor(benchpod):
    if not benchpod.can_spi:
        pytest.skip("this pod has no SPI master")
    benchpod.spi_flash("fw.bin")
```

**Stability promise.** Everything in `embeddedci.benchpod.__all__` follows semantic versioning:
within 2.x names and signatures only grow. `BenchPod.command()`, `BenchPod.transport` and
`BenchPod.lowlevel` are escape hatches **outside** the promise. Migrating from 0.x/1.x? The
[CHANGELOG](https://github.com/embeddedci-com/embeddedci-python/blob/main/packages/embeddedci/CHANGELOG.md)
has a complete old → new table.

## pytest plugin

Installing the package registers a pytest plugin (options, fixtures and markers). Set the board's
I/O voltage **once** for the whole suite in `conftest.py` — the pod refuses flashing, UART, LA
capture, pull resistors and I2C-sensor emulation until an LA bank voltage is selected:

```python
# conftest.py
import pytest


@pytest.fixture(scope="session")
def benchpod_la_voltage():
    return 3.3  # the board's I/O voltage — change to 1.8 for a 1V8 board
```

Write the bench's wiring down once too, so tests use names instead of channel numbers (see
[Wiring profile](#wiring-profile)). A cloud device already has one — edited in the web UI — and
`benchpod.wiring` loads it; for a LAN pod override the fixture (or pass `--benchpod-wiring=wiring.json`):

```python
# conftest.py
from embeddedci.benchpod import Signal, Wiring


@pytest.fixture(scope="session")
def benchpod_wiring():
    return Wiring(uart_rx=3, uart_tx=4, swd_target="target/stm32f4x.cfg",
                  signals=[Signal("TRIGGER", 9, "output"), Signal("READY", 10)])
```

Then point pytest at a pod and use the fixtures:

```bash
pytest --benchpod-connection=192.168.1.213
# or: export BENCHPOD_CONNECTION=usb
```

```python
import pytest
from embeddedci.benchpod import INTERNAL


@pytest.mark.hardware
def test_firmware_boots(benchpod, firmware):
    benchpod.flash(file=firmware, target_power=INTERNAL)     # SWD pins + target from the wiring profile
    boot = benchpod.power_cycle_and_capture(delay=1.0, duration=5.0, until="APP_OK")  # UART from it too
    assert boot.matched, boot.text


@pytest.mark.hardware
def test_result_pin_follows_the_trigger(benchpod):
    trigger, ready = benchpod.signal("TRIGGER"), benchpod.signal("READY")
    trigger.configure()                                      # a GPIO output, starting inactive
    trigger.pulse(0.005)
    assert ready.wait_for(1, timeout=2.0)


def test_rail_is_healthy(benchpod_target, pins):   # target powered for this test only
    assert not benchpod_target.target_status().efuse(pins.efuse).fault
```

The `benchpod` fixture is a `BenchPod` instance, not the module — import constants such as
`INTERNAL` and `PIN1` from `embeddedci.benchpod`. Without a connection configured the fixtures
**skip** rather than fail, so the suite stays green on runners without hardware.

### Options

| Option | Env fallback | Default | Purpose |
|---|---|---|---|
| `--benchpod-connection` | `BENCHPOD_CONNECTION` | — | connection string (also the `benchpod_connection` ini option) |
| `--benchpod-la-voltage` | `BENCHPOD_LA_VOLTAGE` | — | override the `benchpod_la_voltage` fixture for one run (1.8 or 3.3); the flag wins over the fixture, the env var only applies when neither is set |
| `--benchpod-wiring` | `BENCHPOD_WIRING` | — | wiring profile file (`.json`/`.toml`); the flag wins over the `benchpod_wiring` fixture, the env var only applies when neither is set |
| `--benchpod-efuse` | — | the profile's rail | target-power rail for `benchpod_target` and `pins.efuse` (1 internal, 2 external); `benchpod_target` otherwise follows the wiring profile, `pins.efuse` is 1 |
| `--benchpod-firmware` | — | — | firmware image for the `firmware` fixture |
| `--benchpod-discover` | — | off | when no connection is configured, find one pod via mDNS (needs `[discovery]`) |
| `--benchpod-api-key` | `BENCHPOD_API_KEY` | — | API key for the cloud destination and the waveform library; it, `--benchpod-api-base` and the lease options also reach the short connection that lifts the DAC limits (`BENCHPOD_LIFT_DAC_LIMITS=1`) |
| `--benchpod-api-base` | `BENCHPOD_API_BASE` | `https://www.embeddedci.com` | embeddedci server |
| `--benchpod-lease-wait` | — | `600` | seconds to wait for a busy cloud device |
| `--benchpod-no-lease` | — | off | don't lock the cloud device (only when nothing else can use it) |
| `--benchpod-build-target` | `BENCHPOD_BUILD_TARGET` | — | platform id recorded by `build_report` (e.g. `stm32f4`) |

Connection resolution order: `--benchpod-connection` → `benchpod_connection` ini option →
`BENCHPOD_CONNECTION` → `--benchpod-discover`.

```ini
# pytest.ini
[pytest]
benchpod_connection = 192.168.1.213
```

### Fixtures

| Fixture | Scope | Provides |
|---|---|---|
| `benchpod_la_voltage` | session | the board's I/O voltage selected on connect — **override it in `conftest.py`** (default `None` → `BENCHPOD_LA_VOLTAGE`) |
| `benchpod_wiring` | session | the bench's wiring profile — **override it in `conftest.py`** (a `Wiring`, dict or file path; default `None` → `BENCHPOD_WIRING`, a cloud device's stored profile, the defaults). An explicit profile also supplies the LA voltage when nothing else sets one |
| `benchpod` | session | a connected `BenchPod` with the options above applied; LA channels left in GPIO mode are released when the session starts and ends; closed at session end |
| `benchpod_connection` | session | the resolved connection string (skips when none) |
| `benchpod_target` | function | `benchpod` with the target rail (`--benchpod-efuse`, else the wiring profile's) powered on for the test, off at teardown |
| `benchpod_sensor` | function | `benchpod`; disarms the emulated I2C sensor at teardown |
| `benchpod_dac` | function | `benchpod`; stops any DAC output (generate, replay, control loop) at teardown |
| `benchpod_capabilities` | session | `benchpod.capabilities` |
| `benchpod_waveforms` | function | the cloud `WaveformLibrary`; deletes waveforms saved through it during the test; skips without server access |
| `benchpod_pins` / `pins` | session | `pin_1` … `pin_14`, `efuse`, and `has_pullup()`, `has_pulldown()`, `pull_ohms()`, `pullup_ohms()`, `pull_direction()` |
| `firmware` | function | the `--benchpod-firmware` path (skips when unset) |
| `build_report` | function | a build reporter — see [Build reporting](#build-reporting) |

`benchpod` is shared by the whole session, so state a test leaves behind (a running DAC, engaged
pull-ups, an armed sensor) carries into the next one — use the teardown fixtures.

**DAC output limits.** A pod can carry DAC output limits that protect an external output stage (a
solar simulator module on the 5 V path, say); it then refuses what they forbid. With the stage
switched off, run the suite with `BENCHPOD_LIFT_DAC_LIMITS=1`: the plugin clears the limits once
when the session starts and puts the same limits back at the end, even when tests fail. It opens
its own short connection, so it applies whichever fixture the suite uses. Without the variable
nothing changes.

### Markers

* `@pytest.mark.hardware` — labels tests that need a real pod, for selection
  (`pytest -m "not hardware"`). Skipping comes from the fixtures, not the marker.
* `@pytest.mark.benchpod_capability("dac_deep_replay", ...)` — skips the test unless the connected
  device advertises every named `Capabilities` flag (`scope`, `analyzer`, `dac_replay`,
  `dac_deep_replay`, `dac_control_loop`, `dac_loop_sources`, `dac_cotrig`, …). For the
  image-bound `dac_control_loop` and `dac_deep_replay` it switches the pod's gateware image instead
  of skipping, when the pod carries both images (see [Gateware images](#gateware-images)).

## Wiring profile

The pod has no role-named pins, so the bench's wiring — which DUT signal is on which LA channel —
is written down once in a `Wiring` profile. Methods that take a channel fall back to it, and named
signals replace channel numbers:

```python
from embeddedci.benchpod import BenchPod, Signal, Trigger, Wiring

wiring = Wiring(uart_rx=3, uart_tx=4, swd_target="target/stm32f4x.cfg",
                signals=[Signal("TRIGGER", 9, "output"), Signal("READY", 10)])
bp = BenchPod("192.168.1.50", la_voltage=wiring.la_voltage, wiring=wiring)

with bp.open_uart() as uart:                   # rx=3, tx=4, 115200 baud from the profile
    bp.power_on()                              # the profile's eFuse
    uart.expect("APP_OK", timeout=6)
bp.flash(file="app.elf")                        # swclk/swdio/nreset/target from the profile
bp.enable_i2c_sensor()                          # sda/scl/address from the profile
la = bp.capture_la(100_000, trigger=Trigger("READY"))
print(wiring.describe())                        # a pin table with each channel's bias resistor
```

| Key | Default | Meaning |
|---|---|---|
| `la_mv` | `3300` | LA I/O-bank voltage in mV (1800 or 3300); `wiring.la_voltage` in volts |
| `efuse` | `1` | target-power rail: 1 internal 5 V, 2 external |
| `uart_rx`, `uart_tx`, `uart_baud` | `5`, `4`, `115200` | `rx` samples the DUT's TX; `tx` drives the DUT's RX |
| `i2c_sda`, `i2c_scl`, `i2c_addr` | `1`, `2`, `"0x76"` | emulated sensor bus (put I2C on LA1-LA6, which have pull-ups) |
| `swd_swclk`, `swd_swdio`, `swd_nreset`, `swd_target` | `11`, `12`, `False`, `""` | flashing |
| `spi_sclk`, `spi_mosi`, `spi_miso`, `spi_cs` | not wired | decoder channels |
| `signals` | `[]` | named signals: `Signal(name, la, direction="input"/"output"/"open_drain"/"bidir", active_low=False, description="")` |

A role set to `None` is not wired. Construction validates the whole profile and raises `ValueError`
listing every problem — including two roles or signals on one LA channel; `wiring.warnings()` lists
wiring that works but is risky, such as an I2C bus on a channel without a pull-up.

**Where the profile comes from** (`bp.wiring`, resolved once): the `wiring=` argument (a `Wiring`, a
dict, or a `.json`/`.toml` file) → the `BENCHPOD_WIRING` file → for a cloud device
(`embeddedci:<device>`), the profile stored on embeddedci.com, which the web UI edits → the defaults.
`bp.wiring = ...` swaps it for this connection; `bp.save_wiring(profile)` stores it for a cloud device.
A file holds the same JSON object the server stores:

```json
{"uart_rx": 3, "uart_tx": 4, "swd_target": "target/stm32f4x.cfg",
 "signals": [{"name": "TRIGGER", "la": 9, "direction": "output"}, {"name": "READY", "la": 10}]}
```

`bp.signal(name)` returns a GPIO handle for a signal (see [GPIO on the LA pins](#gpio-on-the-la-pins));
channel arguments accept a role or signal name too (`bp.open_uart(rx="uart_rx", tx=4)`).

## Power, reset and power monitoring

```python
from embeddedci.benchpod import EXTERNAL, INTERNAL

bp.power_on(INTERNAL)                         # internal 5 V eFuse
bp.power_off(INTERNAL, delay=2.0)             # scheduled pod-side; returns immediately
bp.target_power(EXTERNAL, on=True)

rail = bp.target_status().efuse(INTERNAL)     # EfuseState(enabled, fault, valid)
assert rail.enabled and not rail.fault        # fault = tripped (over-current / short)

power = bp.power_status().rail(INTERNAL)      # RailPower(ok, bus_voltage, current)
print(f"{power.bus_voltage:.2f} V  {power.current * 1000:.1f} mA")

bp.reset_target(pulse=0.1)                    # pulse reset without power-cycling
bp.set_reset(True)                            # hold the DUT in reset …
assert bp.reset_state().asserted
bp.set_reset(False)                           # … and release it
print(bp.usb_cc().advertised)                 # USB-C orientation + advertised current
```

`delay` lets a power change land *during* something else (see `power_cycle_and_capture`). The reset
controls drive the pod's dedicated reset pin (DUT header J1 pin 22); they and `usb_cc()` need a rev3
pod (`ResetState.supported`). `TargetStatus.supported` is false on boards that cannot read the eFuse
state back. An omitted `efuse` is the wiring profile's rail (internal by default).

### Power profiles

`power_status()` is one snapshot. A power profile samples the rail's monitor about a thousand times a
second **without gaps** — each sample averages its whole period — so energy and charge are integrated,
and short bursts (an inference, a radio transmit) show up:

```python
prof = bp.measure_power(2.0, keep_samples=1000)          # blocks for 2 s
print(prof.avg_current, prof.peak_current, prof.avg_voltage, prof.energy, prof.charge)

with bp.power_profile(efuse=INTERNAL) as session:        # around a block of code
    bp.power_on(delay=0.2)                               # inrush lands inside the profile
    time.sleep(3)
boot = session.result
assert boot.peak_current < 0.5 and not boot.fault
t, amps, volts = boot.samples[0]                         # bin-averaged trace (keep_samples, ≤ 4096)
```

`PowerProfile`: `efuse`, `rate_hz`, `adc_rate_hz`, `n`, `duration` (s), `avg_current`, `min_current`,
`peak_current` (A), `avg_voltage`, `min_voltage`, `max_voltage` (V), `energy` (J), `charge` (C),
`avg_power` (W), `fault` (the eFuse tripped), `truncated` (stopped at `max_duration`), `samples`.

**Sample rate.** Ask for 100-500 Hz (default 500). The pod takes one reading per firmware pass, so
what you get back is not always what you asked for: it tracks the request to ~200 Hz, then beats
against the pass interval and flattens near 365 Hz. `rate_hz` is what was **actually delivered**
(measured) and `adc_rate_hz` what the current sensor was configured for. Every sample carries its own
timestamp, so the trace and the energy/charge integrals are exact either way — the rate only tells you
how finely a transient was resolved. Measured on a v2 pod:

| Asked | 100 | 200 | 300 | 400 | 450 | 500 |
|---|---|---|---|---|---|---|
| Delivered | 105 | 189 | 288 | 305 | 361 | 364 |

| Rail | Supply | Current limit | Cut-off | Monitor resolution |
|---|---|---|---|---|
| eFuse 1 (internal) | 5 V from the pod's USB-C | ≈ 2.0 A | 5.7 V | 100 µA |
| eFuse 2 (external) | 5-20 V on the external terminal | ≈ 3.0 A | ≈ 21.8 V | 167 µA |

An overload is held at the limit for about 2.8 ms, then the eFuse trips (`TargetStatus` `fault`) and
retries after ≈ 110 ms. A trip shorter than one sample does not show in a profile. The ADC's `current_in`
input is a 4-20 mA loop terminal (249 Ω), not a way to measure the target's supply.

## Flashing

```python
from embeddedci.benchpod import INTERNAL, PIN11, PIN12

bp.flash(
    file="build/app.elf",
    target="target/stm32f4x.cfg",   # any OpenOCD target config
    swclk=PIN11, swdio=PIN12,       # any two LA channels
    nreset=True,                    # the target's NRST is wired to the pod's reset pin (J1 pin 22)
    target_power=INTERNAL,          # optional: power the target first
)
```

By default a failed flash raises `FlashError` (or `TargetUnreachableError` when nothing answers on
SWD) carrying OpenOCD's output. Pass `check=False` to get the `FlashResult` and assert yourself:

```python
result = bp.flash(file="build/app.bin", load_address="0x08000000",
                  target="target/stm32f4x.cfg", swclk=PIN11, swdio=PIN12, check=False)
assert result.ok, result.stderr     # also: returncode, stdout, target_unreachable, stalled
```

* `nreset` is a flag, not a pin. With it, `connect_under_reset` defaults to `True`; without it,
  connect-under-reset is forced off (the pod cannot hold a reset it isn't wired to).
* `verify=True` and `reset=True` map to OpenOCD's `program … verify reset`. `verify=False` is
  noticeably faster.
* `connect_attempts=5` retries the whole run when the target was unreachable or the SWD link stalled
  (see `BENCHPOD_STALL_TIMEOUT`); a flash that ran and failed is not retried. `timeout=300` bounds
  one OpenOCD run.
* `extra_configs` adds `-c` commands and `extra_args` raw OpenOCD arguments; `clear_reset_events=True`
  empties the target config's clock-boost reset events, which otherwise race the SWD link.
* OpenOCD runs on the machine running the test, on every transport including the cloud.

## SPI flash and SPI devices

Pods with gateware v45+ (`bp.capabilities.spi_master`) run an SPI master on any four LA pins, up
to 6 MHz, modes 0 and 3. Wire a 25-series SPI NOR flash (W25Q, MX25, GD25, ...) with its SCK, DI,
DO and CS on LA channels (power it at the LA voltage, tie /WP and /HOLD high) and program it:

```python
res = bp.spi_flash("fw.bin", 0x0, sck=13, mosi=14, miso=6, cs=5, hold_reset=True)
print(res.jedec_id, res.length, res.seconds)       # erased, written and verified
```

Or work with a session (pins from the wiring profile's `spi_sclk`, `spi_mosi`, `spi_miso`,
`spi_cs` when omitted):

```python
with bp.open_spi(hz=6_000_000) as spi:
    print(spi.flash_id())                          # SpiFlashInfo(jedec_id='ef4017', present=True, ...)
    data = spi.flash_read(0x0, 4096)
    spi.flash_erase(0x10000, 65536)
    spi.flash_write(0x10000, payload)              # 768 B per command, each verified
    rx = spi.transfer(b"\x9f\x00\x00\x00")        # raw full-duplex, for any SPI device
```

`hold_reset=True` holds the DUT in reset for the job (rev3 pods), so its own controller does not
drive the same bus. The SPI master shares the SWD engine: an SWD flash cannot run while a session
is open. Expect about 30 KB/s written (verified) over the LAN, less over the cloud.

`spi.stream(data, head=b"...")` sends data of any size in one CS frame, write-only: it is uploaded
into the pod's PSRAM first, then clocked out 512 bytes at a time (an FPGA bitstream into a
slave-SPI configuration port). It needs a LAN or cloud connection and firmware with the
`spi_stream` command (`bp.capabilities.spi_stream`).

## Motor & battery emulator

The BenchPod motor emulator boards (ECP5, up to four on one SPI link) are driven with
`bp.open_motor_emulator()`. The SPI pins come from the wiring profile, as do `PROGRAMN` (open
drain) and `DONE` (input) when it names them:

```python
from embeddedci.benchpod import BatteryModel

with bp.open_motor_emulator() as emu:
    emu.configure("emu.bit")                       # every board at once; waits for DONE
    print(emu.probe(0))                            # BoardInfo(version=7, ...)
    emu.set_protection(0, overcurrent_a=8, overvoltage_on_v=30, overvoltage_off_v=29)
    emu.set_battery(BatteryModel(capacity_ah=2.0, ocv=[25.2, 23.4, 22.2, 18.0], r0_ohm=0.02))
    emu.set_pwm(200_000)
    emu.arm(0)
    emu.set_control(0, pwm=True)
    print(emu.sample(0).phase_currents, emu.battery_state(0).soc)
    emu.start_log(["a", "bus"])
    log = emu.read_log()                           # whole sample sets, sequence-checked
```

Every register of the link protocol is reachable by name with `emu.read("SPEED")` and
`emu.write("KE", 1200, board=1)` (`motor_emulator.REGISTERS`); `broadcast=True` writes every
board. Units use `EmulatorCalibration` (the design values unless you pass a bench calibration).
Needs emulator gateware 0x0007 or later.

## UART

`rx` is the LA channel the pod **samples** (wire the DUT's TX here); `tx` is the channel the pod
**drives** (the DUT's RX). `until` is a substring, a compiled regex, or a `text -> bool` predicate.

```python
import re

from embeddedci.benchpod import INTERNAL, PIN4, PIN5

# A fixed window that stops early on a match.
log = bp.capture_uart(rx=PIN5, tx=PIN4, baud=115200, duration=3.0, until="login:")
print(log.matched, log.lines)
assert "login:" in log

# Catch the boot banner: power off, schedule the power-on pod-side, capture across it.
boot = bp.power_cycle_and_capture(rx=PIN5, tx=PIN4, efuse=INTERNAL,
                                  delay=1.0, duration=6.0, until=re.compile(r"APP_OK"))
assert boot.match(r"chip id match=0x58")
```

`power_cycle_and_capture` powers the eFuse off, waits `off_settle` seconds, schedules the power-on
`delay` seconds out with a **pod-side timer**, then captures for `duration` (which must exceed
`delay`) — so the power-on and the boot output land inside the window. It works on every transport,
including USB, where the UART proxy owns the link for the whole capture.

For an interactive session, `open_uart` starts a background reader that buffers from the moment it
opens, so you can listen *before* acting and write to the DUT's console:

```python
with bp.open_uart(rx=PIN5, tx=PIN4, baud=115200) as uart:
    bp.power_on(INTERNAL)                    # immediate: the session is already buffering
    uart.expect("APP_OK", timeout=6)         # raises UartTimeout (with .text) on timeout
    uart.write("status\r\n")
    uptime = uart.expect(re.compile(r"uptime=(\d+)"), timeout=2).group(1)   # expect returns the match
    reply = uart.read_until("> ", timeout=2)    # the output up to the next prompt; None on timeout
    rest = uart.read()                          # whatever arrived since
```

Reading works like a console. `read(timeout=)` returns the output since the last read (waiting up to
`timeout` when there is none), and `read_until` / `expect` mark the output read up to the end of
their match, so the next call only sees newer output. `text` and `lines` keep everything received;
`closed` says the session ended, and `overflowed` is set when more than `max_buffer` characters
arrived and the oldest were dropped. Running commands such as `power_on` while a session is open needs a TCP or
cloud connection.

## LA bias resistors

Eight LA channels carry a fixed, switchable bias resistor. They are referenced to 3V3, so the pod
won't engage one while the LA bank is at 1.8 V (`PullState.available` is false).

| Channels | Resistor | Value |
|---|---|---|
| LA1, LA2 | pull-up | 4.7k |
| LA3, LA4 | pull-up | 2.2k |
| LA5, LA6 | pull-up | 10k |
| LA7, LA8 | pull-**down** | 10k |
| LA9–LA14 | none | — |

```python
from embeddedci.benchpod import PIN1, PIN2, PIN7

bp.enable_pullup(PIN1, PIN2)       # LA1-LA6 only: ValueError on LA7/LA8
bp.enable_pulldown(PIN7)           # LA7/LA8 only
print(bp.pull_state(PIN1))         # PullState(la=1, enabled=True, direction='up', ohms='4.7k', available=True)
print(bp.enabled_pulls())          # [1, 2, 7]
bp.set_pull(PIN7, False)           # either direction
bp.disable_pullup(PIN1, PIN2)
```

An open-drain bus such as I2C must sit on LA1–LA6.

## GPIO on the LA pins

Any LA channel can be a GPIO: an input, a push-pull output or an open-drain output, driven at the LA
I/O voltage through the pod's 330 Ω series resistor. These are the pod's LA pins, not microcontroller
GPIOs.

```python
from embeddedci.benchpod import PinConflictError

start = bp.gpio(9, "output")                  # push-pull, starts low
start.pulse(0.002, count=3)                   # FPGA-timed pulses; returns at once
irq = bp.gpio(10, "input")
assert irq.wait_for(1, timeout=1.0)           # polled from the host (ms resolution)
bus = bp.gpio(6, "open_drain")                # 0 pulls low, 1 releases (LA6's pull-up holds it high)
print(bp.pin_levels())                        # {1: 0, 2: 1, …} — live levels of every channel

reset = bp.signal("RESET_N")                  # a wiring-profile signal, honouring active_low
reset.configure()
reset.activate()

a, b = bp.gpio_pins([11, 12], "open_drain")   # claimed as a group: a conflict on either claims neither

bp.release_gpio(9, 10)                        # back to high-Z; no arguments releases every GPIO pin
```

**One function per channel.** Each channel has exactly one owner at a time: `none` (the default — high-Z
and watched by captures), `gpio`, or a peripheral that claimed it — `uart_rx`/`uart_tx` (a UART session),
`swd_clk`/`swd_dio` (flashing), `i2c_sda`/`i2c_scl` (sensor emulation), `step`/`step_dir` (a pulse train).
The pod refuses a second function with `PinConflictError` (`.la`, `.function`) instead of letting one
silently override the other, and the message says how to free the channel:

```python
bp.gpio(4)
try:
    bp.open_uart(rx=3, tx=4)
except PinConflictError as exc:  # pin conflict: LA4 is in use by gpio; release it with ...
    bp.release_gpio(exc.la)
```

`bp.la_pins()` lists every channel's `LaPinState` (`function`, `gpio` mode, `level`, `pull`, `pull_on`),
and `bp.configure_gpio(channels, mode)` is the same group claim as `gpio_pins()` returning those states.
Captures observe every channel whatever its function. GPIO channels stay GPIO — also across
disconnects — until released (the pytest `benchpod` fixture releases them at session start and end),
and the LA voltage can't change while any channel is in use.

**Bias resistors must suit the function** — the pod refuses the combination with `PullConflictError`,
whichever you set first:

| Function | Pull-up (LA1-LA6) | Pull-down (LA7, LA8) |
|---|---|---|
| none, gpio input, gpio output, swd_clk, step | ok | ok |
| gpio open_drain | ok (it needs one, or an external pull-up) | refused — a released line would read low |
| uart_rx, uart_tx | ok | refused — the line idles high |
| i2c_sda, i2c_scl | ok (needed) | refused — the bus needs pull-ups |
| swd_dio | ok | refused |

A `pulse()` or `la_step()` on a GPIO output keeps the channel GPIO (its level returns afterwards); on a
free channel the train owns it only while it runs. `pin_levels()` reads the pins directly on gateware
v35+ and falls back to a short capture on older gateware. GPIO needs pod firmware that advertises
`Capabilities.la_pins`.

## Emulated I2C sensor

The pod can **be an I2C sensor** (a BMP280) on two LA channels, so firmware that probes a sensor can
be tested with it present, absent, or reporting chosen values:

```python
from embeddedci.benchpod import BMP280_ADDR_PRIMARY, PIN1, PIN2, Sensor, i2c

bp.enable_pullup(PIN1, PIN2)                      # the open-drain bus must idle high
bp.enable_i2c_sensor(Sensor.BMP280, sda=PIN2, scl=PIN1, address=BMP280_ADDR_PRIMARY,
                     temperature_c=22.5, pressure_pa=101_000)
bp.set_i2c_sensor(temperature_c=40.0)             # change the readings mid-test
print(bp.i2c_sensor_status())                     # activity counters: transactions, reads, writes, …
print(bp.i2c_sensor_regs(start=0xD0, length=1))   # the emulated register image

txns = bp.i2c_sensor_capture(4096, sample_rate_hz=500_000)   # capture the bus and decode it
print(i2c.format_transactions(txns))              # e.g. S 0x76W+ 0xD0+ Sr 0x76R+ 0x58- P
assert i2c.read_register(txns, BMP280_ADDR_PRIMARY, 0xD0) == [0x58]
bp.disable_i2c_sensor()
```

`i2c_sensor_capture` samples fast enough to resolve the bus at around 500 kS/s – 1 MS/s; one call
covers a few tens of milliseconds, so to catch a one-shot boot probe, call it repeatedly right after
powering the DUT on. The `benchpod.i2c` decoder handles START / repeated START / STOP, R/W, per-byte
ACK/NACK and the "write register pointer, then read" pattern (`read_register`, `addressed`). It also
decodes `(scl, sda)` streams (`i2c.decode_samples`) and synthesizes traces (`i2c.synthesize`), so
decode logic can be tested without hardware. For a bus on arbitrary channels, use `capture_la` +
`decode` (below).

## Analog paths, DC output and single readings

A named analog path is one fully specified mux and relay state, defined in the firmware, applied in
one step:

| `analog_path(...)` | Effect |
|---|---|
| `"dac_3v3"`, `"dac_5v"`, `"dac_12v"` | route the DAC to that output (`dac_12v` is the bipolar ±12 V output) |
| `"adc_ext"` | connect the ADC to the front SMA |
| `"cal1"`, `"cal2"` | loop the 5 V / 12 V DAC output back into the ADC |
| `"current_in"` | read the 4-20 mA measurement terminal |
| `"current_out"` | the 4-20 mA output: switch the DAC voltage outputs off |
| `"off"` | park everything |

```python
out = bp.dac_output("5v", volts=2.5)       # DacOutput(path, voltage, code): voltage actually produced
loop = bp.adc_read("cal1")                 # AdcReading(source, voltage, count, span)
print(out.voltage, loop.voltage, loop.span)
bp.dac_output("off")

state = bp.analog_path("adc_ext")          # AnalogPathState(path, dac_mux_register, cal_relay_register)
print(bp.adc_read("ext").voltage)          # the true front-SMA voltage (the ÷12 divider is applied)
```

`dac_output` takes calibrated volts (`"3v3"`, `"5v"`, `"12v"`, or `"off"`); without `volts` it only
routes. `adc_read` routes the source (`"ext"`, `"cal1"`, `"cal2"`, `"current_in"`) and returns one averaged,
calibrated reading; the pod refuses it while the input is still moving (e.g. a DAC left running).

### Calibrating the 4-20 mA input

`"current_in"` reads the 4-20 mA terminal (J8) across a 249 Ω resistor, so 4 mA is 0.996 V and 20 mA is
4.98 V. Each pod is off by a few mV there. The pod can measure that offset itself and keep it in
its flash:

```python
cal = bp.calibrate()                       # disconnect J8 first. Calibration(source, calibrated, offset, a, b, ...)
print(cal.offset)                          # 0.004356: what the open input read, now taken out
print(bp.adc_read("current_in").voltage)   # 0.0 with J8 still open
print(bp.adc_read("current_in").current)   # loop current in amps: 0.004 to 0.020 for a live loop

bp.calibration()                           # what the pod has stored
bp.clear_calibration()                     # back to the built-in fit
```

Calibrate once per pod: it survives a reboot and a firmware update. The pod refuses
(`BenchPodError`) when something is driving J8 (more than 50 mV) and keeps what it had. `a` and `b`
are the fit the pod now uses for `current_in` (`volts = a + b * count`). Only the offset of `current_in` is
calibrated; the other sources use the built-in fits. Needs firmware 3.4.0 (capability
`calibrate`, `Capabilities.calibrate`).

### The 4-20 mA output

Terminal J9 is a two-wire 4-20 mA transmitter. `current_out` holds a current on it, in amps:

```python
out = bp.current_out(0.012)                # CurrentOutput(current, code, min_current, max_current)
print(out.current)                         # 0.012: the current actually held (16-bit, 0.25 µA steps)
print(out.min_current, out.max_current)    # 0.004056 0.020094 on a rev3 pod: what the output can do

bp.current_out(0.004)                      # back to the live zero
print(bp.current_out_range().max_current)  # the range only, nothing moves
```

- **It is loop powered.** J9 pin 1 (plus) goes to the plus of an external floating loop supply, pin 2
  (minus) through your receiver to the supply minus. Use 8 V plus 20 mA times the loop
  resistance, 30 V at most (24 V drives up to 800 Ω).
- **The loop must float.** Pin 2 is not pod ground, and nothing in the loop may touch pod
  ground: not the supply minus, and not a receiver that shares a ground with the pod (through
  SWD or UART wiring to the same target). If it does, the current is no longer regulated.
- **Do not wire J9 straight into J8.** The output cannot regulate that way.
- The pod cannot see the loop. `current_out` succeeds with the supply off or the loop open.
- The output cannot go below its live zero (a little above 4 mA) or above about 20.1 mA: there is no
  0 mA and no 21 mA level. `0.004` gives the live zero. Anything else outside the range raises
  `BenchPodError` with the pod's message. A value of 1 or more raises `ValueError` (amps, not mA).
- **The DAC is shared** with the 3.3 V / 5 V / ±12 V outputs. `current_out` switches those off
  first. `dac_output`, `generate` and `replay` also move the loop current. `dac_stop` leaves the
  loop where it was: call `current_out(0.004)` to go back to 4 mA.
- While DAC limits are set (an output stage), the pod refuses `current_out`.

Waveforms work on it too. `generate`, `replay` and `replay_waveform` take
`dac_path="current_out"`, and their levels are then amps:

```python
with bp.generate("sine", freq_hz=2, amplitude=0.006, offset=0.012, dac_path="current_out"):
    ...                                    # 6 to 18 mA; leaving the block returns the loop to 4 mA

ramp = [0.004 + 0.016 * i / 999 for i in range(1000)]
bp.replay(ramp, dac_path="current_out", sample_rate_hz=1000)   # 4 to 20 mA in 1 s, looping

loop = bp.capture_adc(4096, sample_rate_hz=100_000, source="current_in")
print(max(loop.currents))                  # a capture of J8 carries the loop current in amps
bp.replay(loop, dac_path="current_out")    # play the captured loop back on J9
```

- `generate` builds 8-bit levels: steps of about 63 µA. `replay` is 16-bit.
- Stopping the returned handle returns the loop to 4 mA. A bare `dac_stop()` does not.
- A capture or recording of a voltage has no current to reproduce: on `current_out` it needs
  `mapping="fit"`, which stretches its shape over the output's range. The same holds for a
  current recording (`Waveform.unit == "mA"`) on a voltage output.
- In the cloud library, `save_capture_as_recording` stores a capture of `current_in` as a current
  (`unit="mA"`), and `waveforms.save_segments(..., dac_path="current_out")` takes amps.
- Below 400 kS/s a capture of `current_in` reads about 10 to 20 µA higher than `adc_read`.
- The FPGA timing features work on the 4-20 mA terminals exactly as on the voltage ones, because
  they are the same DAC and ADC: `capture_correlated(..., source="current_in")` captures the loop
  current and the logic channels off one trigger, `on_capture=True` starts a current waveform at
  the capture's t0, `stop_dac_after` freezes it (the loop then holds that current), and
  `trigger=` starts a capture on an LA edge. There is one ADC and one DAC: a capture is the SMA
  voltage or the J8 current, and the output is a voltage or a current, never both at once.

The pod converts with a fit for its board revision (measured on one rev3 pod; the nominal part
values on v2), the same on every pod of that revision: the output has no per-pod calibration.
Expect a few tens of µA between pods. Needs firmware 3.4.0 or later (capability `current_out`, `Capabilities.current_out`).

## Captures

Captures return data with the scaling applied: an ADC `Capture` holds **calibrated volts** (the
same front-end model the web UI uses) next to the raw counts.

```python
from embeddedci.benchpod import decode

cap = bp.capture_adc(4096, sample_rate_hz=1_000_000, source="ext")
assert 3.2 < cap.mean() < 3.4                           # a 3.3 V rail
print(cap.sample_rate_hz, cap.duration, cap.peak_to_peak(), cap.rms_ac())
print(cap.dominant_frequency())                         # needs embeddedci[analysis]

long = bp.capture_adc(800_000, sample_rate_hz=400_000)  # 2 s: above 32768 samples it streams from PSRAM

la = bp.capture_la(8192, sample_rate_hz=1_000_000)
print(la.edges(5), la.channel(5)[:16])
txns = la.decode("i2c", sda=2, scl=1)
frames = bp.decode(la, "uart", rx=5, baud=115200)
print(decode.uart_text(frames))
words = la.decode("spi", sclk=1, mosi=2, miso=3, cs=4)

both = bp.capture_correlated(adc_samples=4096, adc_sample_rate_hz=400_000,
                             la_samples=4096, la_sample_rate_hz=1_000_000)
print(both.adc.duration, both.la.duration)              # one hardware trigger: timebases align
```

* `capture_adc(samples=4096, *, sample_rate_hz=None, source=None, trigger=None, trigger_timeout=10.0)` — omitted rate = the device
  maximum; the **achieved** rate is on the result. `source` routes the ADC first (omitted, the
  current routing is left alone). `volts` use the front-SMA calibration; for the other sources
  compare `counts` or use `adc_read`.
* `capture_la(samples=4096, *, sample_rate_hz=None, stop_dac_after=None, trigger=None, trigger_timeout=10.0)` — 12-bit words; bit *n* is
  channel LA*n+1*.
* `capture_correlated(...)` — ADC + LA from one trigger; set either count to 0 for a single stream.
* `bp.decode(source, protocol, **channels)` / `LaCapture.decode(...)` — off-device decoding of
  `i2c` (`sda`, `scl`), `uart` (`rx`, `baud`, optional `data_bits`, `parity`, `stop_bits`) and `spi`
  (`sclk`, optional `mosi`, `miso`, `cs`, `mode`, `bits`, `msb_first`). Results are
  `I2CTransaction`, `UartFrame` and `SpiFrame` lists.

| `Capture` | `LaCapture` |
|---|---|
| `counts`, `volts`, `sample_rate_hz`, `source`, `duration`, `times()` | `words`, `sample_rate_hz`, `channels`, `duration` |
| `mean()`, `min()`, `max()`, `peak_to_peak()`, `rms()`, `rms_ac()` | `channel(la)` → 0/1 list, `edges(la)` → transition count |
| `fft()` → `(freqs_hz, magnitude)`, `dominant_frequency()` — need `[analysis]` | `decode(protocol, **channels)` |
| `crossing_times(threshold, edge, hysteresis=)`, `first_crossing(...)` | `edge_times(la, edge)`, `first_edge(la, edge, after=)`, `level_at(la, t)`, `pulse_widths(la, level)`, `frequency(la)`, `duty_cycle(la)`, `delay(from_la, to_la, ...)` |

### Timing

Timestamps are seconds from the capture's first sample, with one-sample resolution
(`1 / sample_rate_hz`). `edge` is `"rising"`, `"falling"` or `"both"`.

```python
la = bp.capture_la(1_000_000, sample_rate_hz=1_000_000)          # 1 s at 1 µs resolution

latency = la.delay(9, 10)                  # trigger pin (LA9) rising → "result ready" (LA10) rising
assert latency is not None and latency < 0.050

print(la.edge_times(10, "rising")[:5])     # each result's timestamp
print(la.pulse_widths(10, level=1))        # complete high pulses only
print(la.frequency(11), la.duty_cycle(11)) # a PWM or frame-sync line

both = bp.capture_correlated(adc_samples=100_000, adc_sample_rate_hz=100_000,
                             la_samples=1_000_000, la_sample_rate_hz=1_000_000)
rail_up = both.adc.first_crossing(3.0, "rising", hysteresis=0.1)   # one trigger: same timebase
reset_released = both.la.first_edge(3, "rising")
```

`delay` returns `None` when either edge is missing; `first_edge` and `first_crossing` take `after=` to
skip earlier activity. `crossing_times` counts a crossing only once the signal clears
`threshold ± hysteresis / 2`, so noise on a slow edge counts once.

### Triggered captures

A `Trigger` makes the capture start at an event instead of when the command arrives: `rising` or
`falling` on an LA channel, or while it is `high`/`low`. t = 0 is then the trigger moment (and a DAC
co-trigger or `stop_dac_after` counts from it), so a delay measured from the trigger needs no
alignment:

```python
from embeddedci.benchpod import Trigger, TriggerTimeout

la = bp.capture_la(500_000, sample_rate_hz=1_000_000,
                   trigger=Trigger("TRIGGER", "rising"), trigger_timeout=5.0)
latency = la.first_edge(bp.wiring.la("READY"), "rising")        # seconds after the trigger

adc = bp.capture_adc(100_000, sample_rate_hz=100_000, trigger=Trigger(10, "falling"))
```

`capture_adc`, `capture_la` and `capture_correlated` take `trigger` and `trigger_timeout` (seconds, up
to 600). When the condition never happens the pod aborts the capture and raises `TriggerTimeout`
(`.la`, `.edge`). The trigger channel can have any function — triggers observe. Triggers need gateware
v35+ (`Capabilities.capture_trigger`).

## DAC: generator, replay and faults

Every DAC output keeps running until stopped. The returned `DacHandle` / `ReplayHandle` is a
context manager that stops it on exit; `bp.dac_stop()` stops any output (generator, replay or
control loop). DAC paths are `"3v3"`, `"5v"` and `"12v"`.

```python
from embeddedci.benchpod import Fault

# A parametric waveform, in volts: 0-3.3 V square wave that stops by itself after 5 s.
bp.generate("square", freq_hz=10, amplitude=1.65, offset=1.65, dac_path="3v3", duration=5.0)

golden = bp.capture_adc(16_384, sample_rate_hz=100_000, source="ext")

with bp.replay(golden, dac_path="5v"):                   # loops until the block exits
    la = bp.capture_la(8192, sample_rate_hz=1_000_000)   # runs concurrently with the replay

ramp = [3.3 * i / 1000 for i in range(1000)]             # a list of volts
with bp.replay(ramp, dac_path="3v3", sample_rate_hz=10_000,
               fault=Fault("flatline", start=400, width=100)) as r:
    print(r.samples, r.sample_rate_hz, r.deep)
```

* `generate(waveform, *, freq_hz, amplitude, offset=None, dac_path="5v", duration=None,
  sample_rate_hz=None, on_capture=False)` — `sine`, `square` or `sawtooth`. `amplitude` is the peak
  and `offset` the centre (default: mid-range of the path). The firmware builds the waveform from
  8-bit levels, so volts are quantised to full-scale / 255.
* `replay(source, *, dac_path="5v", mapping="faithful", sample_rate_hz=None, deep=None, fault=None,
  are_codes=False, on_capture=False)` — `source` is a `Capture` (its volts, at its own sample rate
  by default), a sequence of volts, or raw DAC codes with `are_codes=True`. `mapping="faithful"`
  reproduces the voltage (clipping outside the path's range), `"fit"` auto-scales. Above 2048 samples
  the replay streams from PSRAM (`deep`), which needs the deep-replay gateware image: the pod is
  switched to it automatically (see [Gateware images](#gateware-images)), and `switch_image=False`
  raises instead. Replay streams the waveform to the pod, so it needs a **TCP or cloud** connection.
* `Fault(type, start, width, level=None)` — `"flatline"`, `"spike"` or `"stuck"` spliced into the
  replay at sample indices; `level` is an optional raw DAC code.

### DAC ↔ capture co-trigger and auto-stop

For a deterministic stimulus → response run, phase-lock the DAC to the capture instead of racing
them from the host. `on_capture=True` arms the DAC to start on the next capture's hardware t0
(gateware v27+, `capabilities.dac_cotrig`); `stop_dac_after=` on a capture cuts the DAC that many
seconds after the same t0, sample-precise (gateware v21+):

```python
with bp.replay(golden, dac_path="5v", on_capture=True) as r:
    assert r.cotrig                                        # armed, not yet driving
    run = bp.capture_correlated(adc_samples=4096, adc_sample_rate_hz=400_000,
                                la_samples=4096, la_sample_rate_hz=1_000_000,
                                stop_dac_after=0.002)      # DAC starts at t0, stops at 2 ms
```

`generate(..., on_capture=True)` and `capture_la(..., stop_dac_after=...)` work the same way.

### Cloud waveform library

Save captures to the organisation's library and replay them later, on this pod or another:

```python
wf = bp.save_capture_as_recording(golden, "golden-startup")   # raw samples stored server-side

for w in bp.waveforms.list():
    print(w.id, w.name, w.kind, w.sample_count, w.sample_rate_hz)

with bp.replay_waveform(wf, dac_path="5v", mapping="faithful", window_start=0, window_len=8192):
    run = bp.capture_la(8192, sample_rate_hz=1_000_000)

bp.waveforms.delete(wf.id)
```

`bp.waveforms` is a `WaveformLibrary` (`list`, `get`, `find(name)`, `preview`, `save_waveform`,
`save_recording`, `save_segments`, `rename`, `delete`, `download_recording`). `replay_waveform`
takes a waveform id or `Waveform`; on a cloud connection it uses the server's replay DSP by default
(`server_side=`), otherwise it downloads the recording and applies the same DSP client-side before
streaming it over the device connection.

> **Server access.** The library and server-side replay need an embeddedci credential. Over the cloud
> destination the SDK reuses the connection's session token (from an API key or GitHub Actions
> OIDC), so nothing extra is needed. On a LAN or USB connection pass an API key (`api_key=`,
> `--benchpod-api-key` or `BENCHPOD_API_KEY`). Captures and direct `replay(...)` never need one.

## Gateware images

The pod's FPGA runs one of two gateware images, both stored on the pod:

| Image | Adds | Capability flag |
|---|---|---|
| `FpgaImage.LOOP` (0) | the [in-fabric control loop](#in-fabric-dac-control-loop) | `dac_control_loop` |
| `FpgaImage.DEEP_REPLAY` (1) | replays longer than 2048 samples, streamed from PSRAM | `dac_deep_replay` |

Everything else — captures, the generator, DC output, short replays, UART, I2C sensor emulation,
CAN — works on both.

**Switching is automatic.** `control_loop()`, `replay()` and `replay_waveform()` switch the pod to
the image they need (`switch_image=True`, the default), log a warning on the `embeddedci.benchpod`
logger, and record the switch on the returned handle as `switched_image` (an `FpgaImageInfo`, or
`None` when no switch was needed). Pass `switch_image=False` to get a `BenchPodError` instead, for a
test that must not disturb the pod. `replay_waveform` through the server switches only for a
recording that would otherwise be downsampled (longer than the shallow replay depth, with no
`target_samples`), and waits until the server sees the new image. A pod that reports neither flag has
a single image and is never switched.

What a switch does:

* It reprograms the FPGA from the pod's config flash and cold-boots it, which takes **~2-3 s**.
* It **resets the FPGA**, so a running DAC output or control loop, an open UART session and I2C
  sensor emulation stop. Switch first, then start those — for example once in a session fixture:

  ```python
  if not bp.capabilities.dac_deep_replay:
      bp.fpga_image(FpgaImage.DEEP_REPLAY)
  ```

* The selection is written to the config flash, so the pod normally stays on that image after a
  power cycle.
* In pytest, `@pytest.mark.benchpod_capability("dac_control_loop")` (or `"dac_deep_replay"`)
  switches the image for the test instead of skipping it.

## In-fabric DAC control loop

On the **loop gateware image** (switched to automatically — see [Gateware images](#gateware-images))
the iCE40 runs a control loop in fabric: each tick it takes an input,
looks it up in a reloadable curve (`out = curve[input]`), damps toward that target, clamps to
`[vmin, vmax]` and drives the DAC — no host in the loop. Any transfer function you can tabulate
works; a solar-panel I-V curve is one preset. Curves and inputs are 16-bit codes (0–65535).

**Start open-loop, with the ADC out of the path.** `source="fixed"` holds one point of the curve, so
the DAC and output stage can be metered on their own before trusting the loop (gateware v29+,
`capabilities.dac_loop_sources`):

```python
import time

from embeddedci.benchpod import build_panel_curve, curve_output_at, input_percent_to_code

curve = build_panel_curve(voc_code=52_000, sharpness=6)
# On the deep-replay image this first switches the pod to the loop image (~3 s).
with bp.control_loop(curve=curve, vmax=65_535, source="fixed", input_code=0) as loop:
    for pct in (0, 50, 100):
        code = input_percent_to_code(pct)
        loop.set_input(code)                       # no re-arm, no curve re-upload
        time.sleep(0.2)
        pt = loop.probe()                          # IVPoint(i, v, input_code, source)
        print(pct, pt.loop_input, pt.v, "expected", curve_output_at(curve, code))
```

Then `source="sweep"` (with `step=` ≥ 1) walks the curve as a function of time, and the default source
closes the loop around the live ADC:

```python
from embeddedci.benchpod import LoopInputMap

with bp.control_loop(curve=curve, vmax=52_000) as loop:        # input = live ADC
    print(loop.probe().loop_input, loop.probe().v)

# Gateware v30+ (capabilities.dac_loop_input_map): index the curve in engineering units of the
# sense chain instead of raw ADC counts — here mA through a 0.04 Ω shunt into a 50 V/V amplifier.
shunt = LoopInputMap(mv_per_unit=2.0, range_min=0.0, range_max=1000.0)
with bp.control_loop(curve=curve, input_map=shunt) as loop:
    print(loop.probe())
```

* Curves: `build_panel_curve(voc_code, sharpness=4.0, points=256)`, `build_constant_curve(value_code)`,
  `build_linear_curve(max_code, rising=True)`, or any sequence of codes; `curve_output_at(curve, code)`
  is what the hardware will drive for an input, and `input_percent_to_code(pct)` converts a
  percentage of the input range. `control_loop(voc_code=, sharpness=)` builds the panel curve for you.
* Loop parameters: `k` (Q15 damping, clamped to 1–32767), `vmin`/`vmax` (output clamp; `vmin > vmax`
  raises `ValueError`), `tick_div` (≥ 8).
* `bp.loop_input(...)` / `loop.set_input(...)` re-target a running loop and return a `LoopState`;
  `bp.loop_probe()` / `loop.probe()` return an `IVPoint` — assert against `loop_input`, which is what
  indexed the curve (in a fixed or sweep run the ADC reading `i` is not in the path).
* `loop.switched_image` is the `FpgaImageInfo` (`image`, `version`, `features`) of the switch
  `control_loop` made, or `None` if the pod was already on the loop image; `switch_image=False`
  raises instead of switching.

## CAN

The pod has one CAN node (FDCAN + TCAN1044 transceiver). `open_can` configures it and returns a
`CanBus`, which clears autonomous-responder rules and disables CAN on exit.

| `mode` | Behaviour |
|---|---|
| `"normal"` | a regular node; needs another node on the bus to ACK |
| `"internal"` | loopback inside the FDCAN core — self-test with nothing wired |
| `"external"` | loopback through the real transceiver pins — validates the transceiver; the bus must be idle |
| `"listen"` | receive only |

```python
with bp.open_can(bitrate=500_000, mode="internal") as can:
    can.write(0x123, [0xDE, 0xAD])
    frame = can.expect(can_id=0x123, timeout=1.0)          # raises CanTimeout (.frames) on timeout
    assert frame.data == b"\xde\xad"

with bp.open_can(bitrate=500_000, term=True) as can:        # on a real bus, 120 Ω termination in
    can.simulate_ecu({0x7DF: (0x7E8, [0x02, 0x41, 0x00])})  # the firmware replies from its RX ISR
    frames = can.collect(1.0, match=0x100)
    can.assert_periodic(0x100, period=0.1, tol=0.2)         # uses the pod's ISR timestamps
```

`CanBus` also has `read(max_frames=8)`, `read_until(match, timeout=)` (returns `None` on timeout),
`add_responder`, `clear_responders`, `status()` and `set_term(on)`. A match is a CAN id or a
`CanFrame -> bool` predicate. `CanFrame` has `id`, `data` (bytes), `ext`, `rtr`, `dlc` and `ts`
(pod uptime in ms). The command-level methods underneath are on `BenchPod`: `can_config`,
`can_write`, `can_read` (→ `CanReadResult(frames, overflow)`), `can_status`, `can_term`,
`can_respond`, `can_respond_clear`, `can_disable`.

### Step/direction pulse trains

`bp.la_step(PIN3, steps=200, delay=0.001, dir_la=PIN4, direction=1)` emits step pulses on an LA
channel, generated by the FPGA; it returns immediately. `delay` is half the step period: each pulse
is high for `delay`, then low for `delay`, so the example steps every 2 ms (500 steps/s). `steps`
is 1..65535 and `delay` 4 µs..65.535 ms (16-bit counters in the gateware); split a longer move into
several trains.

## Cloud (`embeddedci:<device-name>`)

The `embeddedci:<device-name>` destination drives a pod that lives somewhere else — behind NAT, in a
lab — through embeddedci.com. The server bridges a raw byte tunnel to the device, so **the full API
works**, including flashing and UART/ADC/LA captures, and the same test you run locally runs
unchanged. Install the `[cloud]` extra.

**Authentication** — the SDK exchanges one of these for a short-lived session token scoped to the
devices you may drive:

1. **An API key** (`eci_…`) via `api_key=`, `--benchpod-api-key` or `BENCHPOD_API_KEY`. Works
   anywhere: your desk, any CI system.
2. **A logged-in user's token** via `cloud_user_token=`, a callable returning the access token (the
   MCP server passes the `benchpod login` session this way).
3. **GitHub Actions OIDC** when neither is set. No secret is stored — the workflow proves which
   repo it is, like PyPI Trusted Publishing. Only works inside a GitHub Actions job.

The SDK renews that session token shortly before it expires (and once more if the server rejects
it), so a connection held for hours keeps working. A token you pass yourself as `cloud_token=` is
used as is. An offline pod fails with "BenchPod '…' is offline", a wrong name with "no cloud
BenchPod named '…'", and a busy pod names who holds it and until when.

```python
from embeddedci.benchpod import BenchPod

with BenchPod("embeddedci:my-bench-01", la_voltage=3.3, api_key="eci_…") as bp:
    bp.ping()
```

```bash
BENCHPOD_API_KEY=eci_… pytest --benchpod-connection=embeddedci:my-bench-01
```

One-time setup (in the EmbeddedCI web app):

1. Give the device a stable name on the **BenchPod** page (the editable name;
   letters/digits/`-_.`, unique per org).
2. For OIDC, on **BenchPod → GitHub Actions**, add your repo (click *Look up* to fill the numeric
   ids) and choose **Any device** or the specific device(s) this repo may drive.

### Running in GitHub Actions

In `.github/workflows/hil.yml`:

```yaml
permissions:
  id-token: write          # REQUIRED — lets the job mint a GitHub OIDC token
  contents: read

jobs:
  hil:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install "embeddedci[cloud]"
      # plus an OpenOCD with the cmsis_dap_tcp backend if the tests flash (see Install)
      - run: pytest --benchpod-connection=embeddedci:my-bench-01   # LA voltage comes from conftest.py
```

If the OIDC token can't be minted, the error says exactly why — one of: **not running inside a
GitHub Action**, the job is **missing `id-token: write` permission**, or the **token request itself
failed**. Auth failures raise `CloudAuthError`.

### Device leases

A pod can be driven by one consumer at a time, so a cloud `BenchPod` takes an exclusive **lease** on
the device for its lifetime: acquired on connect, renewed by a background heartbeat, released on
`close()` (or it expires if the process dies). A run that finds the device busy waits up to
`lease_wait` seconds (default 600, `--benchpod-lease-wait`) and then raises `DeviceBusyError`, so
concurrent CI runs queue instead of colliding. `lease=False` / `--benchpod-no-lease` skips locking;
`lease_ttl` (default 120 s) sets the lease length. Local TCP and USB connections never lease.

Over the cloud, ordinary commands go through the server's command channel rather than the byte
tunnel, so they keep working while a UART session holds the tunnel.

| Setting | Default | Purpose |
|---|---|---|
| `api_base=` / `--benchpod-api-base` / `BENCHPOD_API_BASE` | `https://www.embeddedci.com` | embeddedci server |
| `api_key=` / `--benchpod-api-key` / `BENCHPOD_API_KEY` | — | API-key authentication (preferred over OIDC when set) |
| `cloud_token=`, `cloud_audience=` | — | supply a session token / override the OIDC audience |
| `lease=`, `lease_wait=`, `lease_ttl=` | `True`, `600`, `120` | device lease |

## Build reporting

A test run in GitHub Actions can be recorded on embeddedci.com as a **GitHub-sourced build**: the
tested firmware is uploaded (and can later be flashed from the web UI), the flash wiring is saved as
the UI's defaults, and the pytest result and log become the build status. Request the `build_report`
fixture to opt in:

```python
from embeddedci.benchpod import INTERNAL, PIN11, PIN12


def test_boots(benchpod, firmware, build_report):
    build_report.record_wiring(target="target/stm32f4x.cfg", swclk=11, swdio=12,
                               nreset=True, efuse=1)
    build_report.upload_artifacts([firmware])
    benchpod.flash(file=firmware, target="target/stm32f4x.cfg",
                   swclk=PIN11, swdio=PIN12, nreset=True, target_power=INTERNAL)
    # the pass/fail and the captured pytest output are reported automatically at teardown
```

Reporting is active only inside GitHub Actions with a mintable session token (`id-token: write`,
repo trusted in the web app). Everywhere else the fixture yields an inert reporter
(`build_report.active` is false), so the same test runs unchanged. `--benchpod-build-target` /
`BENCHPOD_BUILD_TARGET` records a platform id. Outside pytest, `make_build_reporter()` returns the
same `BuildReporter` / `NoopBuildReporter`.

To publish a build **without a pod**, use the `embeddedci-upload-build` command:

```yaml
permissions:
  id-token: write
  contents: read
steps:
  # … build the firmware …
  - run: pip install embeddedci
  - run: >
      embeddedci-upload-build --firmware build/app.elf --build-target stm32f4
      --openocd-target target/stm32f4x.cfg --swclk 11 --swdio 12 --nreset true --efuse 1
```

It uploads the firmware plus any sibling `.elf`/`.bin`/`.hex`/`.uf2` (add more with `--artifact`),
names the build with `--name`, and writes `build_id=…` to `$GITHUB_OUTPUT`. It exits non-zero
without a session unless `--allow-missing-token` is passed.

## Errors

Invalid arguments raise `ValueError` before anything is sent. Everything else raises a
`BenchPodError` subclass, all importable from `embeddedci.benchpod`:

| Exception | Raised when | Extra attributes |
|---|---|---|
| `BenchPodError` | base class of all of the below | |
| `ConnectionConfigError` | no connection configured, unparsable string, discovery found zero or several pods | |
| `TransportError` | the pod or tunnel could not be reached / talked to | |
| `TransportTimeout` (a `TransportError` and a `TimeoutError`) | the pod or tunnel did not answer in time | |
| `ConnectionClosedError` (a `TransportError`) | the connection ended or reset before the reply | |
| `FirmwareError` | the pod replied with an error (e.g. "la voltage not set") | `firmware_message`, `cmd` |
| `FlashError` | OpenOCD failed, is missing, or lacks the TCP backend | |
| `TargetUnreachableError` (a `FlashError`) | the probe worked but no target answered on SWD | |
| `DeviceBusyError` | a cloud device lease was not granted within `lease_wait` | |
| `CloudAuthError` | no session token: API key rejected, OIDC unavailable, exchange failed | |
| `ServerApiError` | an embeddedci server API call failed | `status` (HTTP) |
| `PodLockedError` (a `FirmwareError`) | the pod's LAN policy keeps the command for the cloud or the USB console | `firmware_message`, `cmd` |
| `PodBusyError` (a `FirmwareError`) | the pod is busy (`busy: …`), e.g. a capture or upload is running | `firmware_message`, `cmd` |
| `PodLeasedError` (a `PodBusyError`, `DeviceBusyError` and `TransportError`) | a cloud job holds the pod (its refusal on the LAN, or the server's HTTP 409) | `holder`, `left_s`, `expires_at`, `status` |
| `PermissionDeniedError` (a `FirmwareError` and `TransportError`) | the pod's `forbidden: …` on a cloud tunnel, or a server HTTP 403 (`ServerPermissionDeniedError`, also a `ServerApiError`, from `ServerApi`) | `status` |
| `UnsupportedFeatureError` (a `FirmwareError`) | the pod lacks the feature: its capabilities say so (raised before anything is sent), it answers `unknown cmd`, or the digital-only board has no analog front end | `feature`, `firmware_version`, `firmware_message`, `cmd` |
| `UartTimeout` | `UartSession.expect` timed out | `text` |
| `UartLinkError` (a `UartTimeout` and `ConnectionClosedError`) | the UART link died with an error while `UartSession` waited | `text`, `cause` |
| `PinConflictError` | an LA channel is already used by another function (a `FirmwareError`) | `la`, `function` |
| `PullConflictError` | an engaged bias resistor can't work with the channel's function (a `FirmwareError`) | `la` |
| `TriggerTimeout` | a triggered capture's condition never happened (a `FirmwareError`) | `la`, `edge` |
| `CanTimeout` | `CanBus.expect` timed out | `frames` |

## Escape hatches

These sit below the stable API and are **not** covered by the stability promise:

* `bp.command({"cmd": "status"})` — send one raw JSON firmware command and get its `data` back
  (raises `FirmwareError` when the pod refuses). Works over TCP, USB and the cloud.
* `bp.transport` — the underlying transport object.
* `bp.lowlevel` — individual analog switches and raw DAC codes for bring-up and diagnostics:
  `dac_mux(ctrl1=, ctrl2=)`, `dac_mux_status()`, `cal_switch(cal1=, cal2=, current_in=, cal_path=)`,
  `cal_switch_status()`, `dac_set(code, divider=)`. Prefer the named paths — these can leave the
  front end in a state no named path describes.

## API reference

Every name in `embeddedci.benchpod.__all__`, in one place. The sections above show how the pieces
fit together; this is the index. `tests/test_docs_coverage.py` fails when a public name is missing
from this README, so the list stays complete.

### `BenchPod`

`BenchPod(connection=None, *, la_voltage=None, timeout=30.0, transport=None, api_base=None,
api_key=None, cloud_token=None, cloud_audience=None, cloud_user_token=None, lease=True,
lease_wait=600.0, lease_ttl=120, wiring=None)`. A context manager; `close()` is idempotent.

| Area | Methods and properties |
|---|---|
| Connection and device | `ping()`, `status()`, `capabilities`, `refresh_capabilities()` (drop the cached capabilities and read them again), `close()`, `leased` (true while this client holds the cloud lease), `wiring`, `save_wiring(wiring=None)` (store a cloud device's profile on embeddedci.com), `signal(name)`, `fpga_image(image)`, `usb_cc()` |
| LA voltage | `set_la_voltage(volts)`, `get_la_voltage()` |
| Power and reset | `power_on(efuse=None, *, delay=None)`, `power_off(efuse=None, *, delay=None)`, `target_power(efuse=None, *, on, delay=None)`, `target_status()`, `power_status()`, `measure_power(duration, *, efuse=None, rate_hz=500.0, keep_samples=0)`, `power_profile(*, efuse=None, rate_hz=500.0, keep_samples=4096, max_duration=60.0)`, `reset_target(*, pulse=0.1)`, `set_reset(asserted)`, `reset_state()` |
| Flashing | `flash(...)`, `spi_flash(image, addr=0, *, erase=True, verify=True, hold_reset=False, sck=, mosi=, miso=, cs=)`, `open_spi(...)`, `open_motor_emulator(...)` |
| UART | `capture_uart(...)`, `power_cycle_and_capture(...)`, `open_uart(...)` |
| Bias resistors | `enable_pullup(*las)`, `disable_pullup(*las)`, `enable_pulldown(*las)`, `disable_pulldown(*las)` (LA7/LA8), `set_pull(la, enabled)`, `pull_state(la)`, `enabled_pulls()` |
| GPIO and pins | `gpio(la, mode="output", *, level=None)`, `gpio_pins(las, mode="output", *, level=None)`, `configure_gpio(las, mode="output", *, level=None)`, `set_gpio(la, level)`, `read_gpio(la)`, `wait_for_level(la, level, *, timeout, poll=0.005)` (`False` when `timeout` passes first), `pin_levels()`, `la_pins()`, `release_gpio(*las)`, `la_step(la, *, steps, delay, dir_la=None, direction=0)` |
| I2C sensor | `enable_i2c_sensor(...)`, `set_i2c_sensor(...)`, `disable_i2c_sensor()`, `i2c_sensor_status()`, `i2c_sensor_regs()`, `i2c_sensor_capture(...)` |
| Analog | `analog_path(path)`, `adc_read(source="ext")`, `dac_output(path, *, volts=None)`, `current_out(current)`, `current_out_range()`, `calibrate(source="current_in")`, `calibration()`, `clear_calibration()` |
| Captures | `capture_adc(...)`, `capture_la(...)`, `capture_correlated(...)`, `decode(source, protocol="i2c", ...)` |
| DAC | `generate(...)`, `replay(...)`, `replay_waveform(...)`, `dac_stop()`, `control_loop(...)`, `loop_input(...)`, `loop_probe()` |
| CAN | `open_can(*, bitrate=500000, mode="normal", term=False, fd=False)`, `can_config(...)`, `can_disable()`, `can_status()`, `can_term(on)`, `can_write(...)`, `can_read(...)`, `can_respond(...)`, `can_respond_clear()` |
| Cloud library and server | `waveforms`, `save_capture_as_recording(...)`, `server_api` (the `ServerApi`; needs an API key or a cloud session) |
| Escape hatches | `command(cmd)`, `transport`, `lowlevel` (see [Escape hatches](#escape-hatches)) |

### Types

Results are frozen dataclasses unless noted. A result built from a firmware reply keeps that reply in
`raw` and has a `from_reply()` classmethod (`from_dict()`, `from_json()` or `from_chunks()` for the
few built from other shapes); `to_dict()` goes the other way where a type is sent back.

| Type | Returned by / used for | Fields and members |
|---|---|---|
| `LaVoltage` | `get_la_voltage`, `set_la_voltage` | `voltage`, `readback`, `is_set` |
| `TargetStatus` | `target_status` | `internal`, `external` (each an `EfuseState`), `supported`, `efuse(n)` |
| `EfuseState` | `TargetStatus.efuse` | `enabled`, `fault`, `valid` |
| `PowerStatus` | `power_status` | `internal`, `external` (each a `RailPower`), `pod`, `total_current`, `rail(n)` |
| `RailPower` | `PowerStatus.rail` | `ok`, `bus_voltage`, `current` |
| `PowerProfile` | `measure_power`, `PowerProfileSession.result` | see [Power profiles](#power-profiles) |
| `PowerProfileSession` | `power_profile` (a context manager) | `start()`, `stop()`, `status()`, `result` |
| `ResetState` | `reset_target`, `set_reset`, `reset_state` | `asserted`, `supported` |
| `UsbCcStatus` | `usb_cc` | `orientation`, `advertised`, `advertised_current` (A), `cc1_voltage`, `cc2_voltage` (V) |
| `PullState` | `pull_state`, `set_pull` | `la`, `enabled`, `direction`, `ohms`, `available` |
| `LaPinState` | `la_pins`, `configure_gpio` | `la`, `function`, `gpio`, `level`, `pull`, `pull_ohms`, `pull_on`, `in_use` |
| `GpioPin` (class) | `gpio`, `gpio_pins`, `signal` | `name`, `configure()`, `set(level)`, `high()`, `low()`, `activate()`, `deactivate()`, `is_active()`, `read()`, `wait_for(level, *, timeout)`, `pulse(width, *, count=1)`, `state()`, `release()` |
| `FlashResult` | `flash` | `ok`, `returncode`, `stdout`, `stderr`, `target_unreachable`, `stalled` |
| `SpiSession` (class) | `open_spi` | `flash_id()`, `flash_read(addr, length)`, `flash_erase(addr, length)`, `flash_chip_erase()` (whole chip; seconds taken), `flash_write(addr, data)`, `flash_program(data, addr=0, *, erase=True, verify=True)` (erase, write and verify a whole image), `transfer(data)`, `stream(data, *, head=b"", hold_cs=False)`, `close()` |
| `SpiFlashInfo` | `SpiSession.flash_id` | `jedec_id`, `present`, `size`, `status` |
| `SpiFlashResult` | `spi_flash`, `SpiSession.flash_program` | `jedec_id`, `addr`, `length`, `erased`, `verified`, `seconds` |
| `SpiStreamResult` | `SpiSession.stream` | `sent`, `seconds` |
| `UartCapture` | `capture_uart`, `power_cycle_and_capture` | `text`, `lines`, `matched`, `match(pattern)`, `contains(needle)` (also `needle in capture`) |
| `UartSession` (class) | `open_uart` (a context manager) | `write(data)`, `read(*, timeout=0.0)`, `read_until(pattern, *, timeout)`, `expect(pattern, *, timeout)`, `text`, `lines`, `closed`, `error`, `close()` |
| `Sensor` (enum) | `enable_i2c_sensor` | `Sensor.BMP280` |
| `I2CTransaction` | `i2c_sensor_capture`, `decode("i2c")` | `messages`, `complete`, `address`, `to(address)` |
| `I2CMessage` | `I2CTransaction.messages` | `address`, `read`, `address_ack`, `data`, `values` |
| `I2CByte` | `I2CMessage.data` | `value`, `ack` |
| `UartFrame` | `decode("uart")` | `index`, `start_us`, `end_us`, `value`, `hex`, `text`, `ok`, `error` |
| `SpiFrame` | `decode("spi")` | `index`, `start_us`, `end_us`, `mosi`, `miso`, `mosi_hex`, `miso_hex` |
| `AnalogPathState` | `analog_path` | `path`, `dac_mux_register`, `cal_relay_register` |
| `AdcReading` | `adc_read` | `source`, `voltage`, `count`, `span`, `offset`, `current` |
| `DacOutput` | `dac_output` | `path`, `voltage`, `code` |
| `CurrentOutput` | `current_out`, `current_out_range` | `current`, `code`, `min_current`, `max_current` |
| `Calibration` | `calibrate`, `calibration`, `clear_calibration` | `source`, `calibrated`, `offset`, `a`, `b`, `count`, `span`, `samples` |
| `Capture` | `capture_adc` | `counts`, `volts`, `currents`, `sample_rate_hz`, `source`, `trigger`, `duration`, `times()`, `mean()`, `min()`, `max()`, `rms()`, `rms_ac()`, `peak_to_peak()`, `first_crossing()`, `crossing_times()`, `fft()`, `dominant_frequency()` |
| `LaCapture` | `capture_la` | `words`, `channels`, `sample_rate_hz`, `trigger`, `duration`, `channel(la)`, `level_at()`, `edges()`, `edge_times()`, `first_edge()`, `delay()`, `frequency()`, `duty_cycle()`, `pulse_widths()`, `decode()` |
| `CorrelatedCapture` | `capture_correlated` | `adc` (a `Capture`), `la` (a `LaCapture`), sharing one hardware trigger |
| `Trigger` | `capture_adc`, `capture_la`, `capture_correlated` | `la`, `edge` |
| `DacHandle` (class) | `generate` (a context manager) | `stop()` |
| `ReplayHandle` (class) | `replay`, `replay_waveform` (a context manager) | a `DacHandle` that also knows what it replays: `stop()` |
| `Fault` | `replay(fault=...)` | `type`, `start`, `width`, `level`, `to_dict()` |
| `Segment` | `WaveformLibrary.save_segments` | `shape` (`ramp`, `hold`, `step`), `duration`, `v_start`, `v_end`, `to_dict()` |
| `ControlLoopHandle` (class) | `control_loop` (a context manager) | `probe()`, `set_input(...)`, `stop()` |
| `IVPoint` | `loop_probe`, `ControlLoopHandle.probe` | `i`, `v`, `input_code`, `source`, `tripped`, `loop_input` |
| `LoopInputMap` | `control_loop(input_map=...)` | `mv_per_unit`, `range_min`, `range_max`, `mv_at_zero`, `trip`, `to_request()` |
| `LoopState` | `loop_input` | `source`, `input_code`, `step`, `output_code` |
| `FpgaImage` (enum) | `fpga_image` | `LOOP`, `DEEP_REPLAY` |
| `FpgaImageInfo` | `fpga_image` | `image`, `version`, `features` |
| `CanBus` (class) | `open_can` | `write()`, `read()`, `read_until()`, `expect()`, `collect()`, `assert_periodic()`, `add_responder()`, `clear_responders()`, `simulate_ecu()`, `set_term()`, `status()`, `close()` |
| `CanFrame` | `CanBus.read`, `CanBus.expect` | `id`, `data`, `ext`, `rtr`, `ts`, `dlc` |
| `CanReadResult` | `can_read` | `frames`, `overflow` |
| `MotorEmulator` (class) | `open_motor_emulator` | `configure()`, `boards()`, `probe()`, `read()`, `read_signed(reg)`, `write()`, `status()`, `trips()`, `clear_trips()`, `arm()`, `set_control()`, `set_pwm()`, `set_duties(a, b, c)`, `set_shape(shape)`, `set_protection()`, `set_battery()`, `battery_state()`, `set_pv_calibration(offset, gain)`, `sample()`, `time()`, `start_log()`, `stop_log()`, `read_log()`, `eeprom_read(addr, length)`, `eeprom_write(addr, data)`, `close()` |
| `BatteryModel` | `MotorEmulator.set_battery` | `capacity_ah`, `ocv`, `r0_ohm`, `r1_ohm`, `tau_s`, `soc`, `v_min`, `v_max`, `invert_current` |
| `EmulatorCalibration` | `open_motor_emulator(calibration=...)` | `codes_per_amp`, `codes_per_volt`, `offsets`, `sample_rate_hz`, `amps()`, `volts()`, `current_code()`, `bus_code()`, `resistance_code()` |
| `Wiring` | `wiring`, `save_wiring` | see [Wiring profile](#wiring-profile); also `defaults()`, `load(path)`, `coerce(value)` (a `Wiring`, dict or file path), `from_dict(data, *, strict=True)` (`strict=False` keeps unknown keys in `extra`), `to_dict()`, `with_changes(**changes)`, `assignments()`, `pins()`, `la(name)`, `signal(name)`, `describe()`, `warnings()`, `la_voltage`, `i2c_address` |
| `Signal` | `Wiring.signals` | `name`, `la`, `direction`, `active_low`, `description`, `to_dict()` |
| `Waveform` | `WaveformLibrary` | `id`, `name`, `kind`, `sample_count`, `sample_rate_hz`, `bits`, `full_scale_v`, `recording_size_bytes`, `created_at`, `dac_path`, `segments`, `unit`, `is_recording` |
| `WaveformLibrary` (class) | `waveforms` | `list()`, `get()`, `find()`, `save_recording()`, `save_waveform()`, `save_segments()`, `rename()`, `delete()`, `preview()`, `download_recording()`, `samples_b64(waveform_id)` |
| `BuildReporter`, `NoopBuildReporter` (classes) | `build_report`, `make_build_reporter` | `active`, `build_id`, `record_wiring()`, `upload_artifact()`, `upload_artifacts()`, `upload_logs()`, `set_result()`, `finalize()` |
| `DeviceLease` (class) | a cloud `BenchPod` holds one | `acquire(*, wait_timeout=600.0, poll_interval=5.0)`, `held`, `lease_id`, `release()` |
| `ConnSpec` | `parse_connection`, `resolve_connection` | `kind`, `addr`, `device`, `device_name`, `is_wifi()`, `is_serial()`, `is_cloud()` |
| `LowLevel` (class) | `lowlevel` | see [Escape hatches](#escape-hatches) |
| `Pin` / `Efuse` (enums) | channel and rail arguments | `PIN1` … `PIN14`, `INTERNAL`, `EXTERNAL` |

### `Capabilities`

`bp.capabilities` (and the `benchpod_capabilities` fixture) parses the pod's `status` reply
(`Capabilities.from_status`) or the server's `cap.*` map for a cloud device
(`Capabilities.from_parameters`); `merge(other)` overlays one on another. A flag that was not
reported keeps its default (`False`, `0` or `""`), so a feature check is just
`if bp.capabilities.spi_master:`. `@pytest.mark.benchpod_capability("name")` takes the same names.

| Group | Fields |
|---|---|
| Identity | `board`, `firmware_version`, `board_rev` (`"v2"`, `"v3"`, `"unknown"`, or `""`), `la_vccio_mv` (the LA bank voltage the pod reports) |
| ADC | `adc_bits`, `adc_fullscale_mv`, `adc_channels`, `adc_offset_counts`, `adc_affine` (the front-end fit), `adc_max_count`, `counts_to_volts(count)` |
| DAC and replay | `dac`, `dac_dc`, `dac_replay`, `dac_deep_replay`, `dac_control_loop`, `dac_loop_sources` (gateware v29+), `dac_loop_input_map` (v30+), `dac_cotrig` (v27+), `dac_bits`, `dac_replay_bits`, `dac_replay_max_samples`, `dac_fullscale_mv`, `dac_channels`, `dac_limits` (output limits on the DAC paths) |
| Features | `scope`, `analyzer`, `serial`, `tunnel`, `command`, `ota`, `la_pins`, `gpio_read` (direct pin reads, v35+), `capture_trigger` (v35+), `power_profile`, `capture_b64` (faster ADC read-back, used automatically), `nrst_pin` (the dedicated reset pin, rev3), `usb_cc` (rev3), `spi_master` (v45+), `spi_stream`, `calibrate`, `current_out`, `can`, `pod_current` (`PowerStatus.pod`), `analog` (`False` on the digital-only board, `None` when not announced) |
| Firmware updates and policies | `flash_kb` (MCU flash in KiB, 0 when not reported), `blob_slots`, `ota_sig`, `sig_policy` (`"audit"`, `"required"`, ...), `sig_keys`, `sig_policy_cmd`, `lan_policy` (`"open"`, `"locked"`, `"off"`), `lan_policy_cmd`, `tunnel_max_tier`, `lease_state` (the pod refuses LAN writes while a cloud job holds it), `cloud_ca`, `cloud_proxy`, `ws_auth_v2` (server only) |
| Boot health | `safe_mode`, `safe_reason`, `last_crash`, `reset_cause`, `boot_warning()` (a one-line warning after safe mode or a crash, else `None`) |
| Source | `raw` (the map it was parsed from) |

### `ServerApi`

A thin client for the embeddedci server's HTTP API (`{api_base}/api/...`), for scripts that need
the server rather than a pod. `bp.server_api` returns one bound to the connection's credentials;
build one yourself with `ServerApi(*, api_base=None, api_key=None, token_provider=None,
timeout=60.0, lease_id=None)`. Errors raise `ServerApiError` (`status`), and an HTTP 403 raises
`ServerPermissionDeniedError`.

| Method | Purpose |
|---|---|
| `list_devices()` | the pods the credentials can see |
| `resolve_device_id(name)` | a device name to its server id (raises when unknown) |
| `device_parameters(name_or_id)` | the server's `cap.*` map for a device (`Capabilities.from_parameters` parses it) |
| `wiring_profile(device_id)` | the stored wiring profile: `{"profile", "stored", "defaults", "warnings"}` |
| `put_wiring(device_id, wiring)` | store a wiring profile (the server validates it; errors name the fields) |
| `scope_capture_start(device_id, *, samples=256, sample_rate_mhz=1.0)` | start a server-side ADC capture; returns its id |
| `dual_capture_start(device_id, *, adc_samples, adc_rate_mhz, la_samples, la_rate_mhz)` | start a server-side ADC + LA capture; returns its id |
| `capture_snapshot(capture_id)` | fetch a server-side capture |
| `replay_start(payload)` | arm a server-side DAC replay (`POST /dac/replay/start`) |
| `request(method, path, *, query=None, json_body=None, raw_body=None, content_type=None, parse_json=True)` | any other endpoint; returns `(status, body)` |

### Functions, constants and modules

| Name | Purpose |
|---|---|
| `parse_connection(raw)` | parse a connection string into a `ConnSpec` (raises `ConnectionConfigError`) |
| `resolve_connection(connection=None)` | the same, falling back to `BENCHPOD_CONNECTION` |
| `make_build_reporter(*, api_base=None, audience=None, target="", name="")` | a `BuildReporter` inside GitHub Actions, else a `NoopBuildReporter` |
| `build_panel_curve(voc_code, sharpness=4.0, points=256)`, `build_linear_curve(max_code, rising=True, points=256)`, `build_constant_curve(value_code, points=256)` | control-loop curves (see [In-fabric DAC control loop](#in-fabric-dac-control-loop)) |
| `curve_output_at(curve, input_code)`, `input_percent_to_code(percent)` | evaluate a curve the way the gateware does; an input percentage as a code |
| `encode_curve_b64url(codes)` | 16-bit DAC codes as little-endian base64url, the wire form of a curve |
| `INTERNAL`, `EXTERNAL`, `PIN1` … `PIN14`, `BMP280_ADDR_PRIMARY`, `BMP280_ADDR_SECONDARY` | named constants (see [API conventions](#api-conventions-and-stability)) |
| `DacPath`, `DacOutputPath`, `AnalogPath`, `AdcSource`, `CalibrateSource` (`"current_in"`), `LoopSource`, `Waveshape`, `ReplayMapping`, `DecodeProtocol`, `CanMode`, `FaultType`, `GpioMode`, `Edge`, `TriggerEdge` | the `Literal` string types |
| `decode`, `i2c`, `can`, `control_loop`, `motor_emulator`, `dsp` | submodules: off-device decoders, I2C helpers, CAN types, curve helpers, the emulator's registers, and `dsp`, a pure-Python mirror of the server's record-to-replay processing |

## Examples

* [`examples/test_bmp280.py`](https://github.com/embeddedci-com/embeddedci-python/blob/main/packages/embeddedci/examples/test_bmp280.py)
  — flash → emulate a BMP280 → power-cycle → assert on UART and the decoded I2C bus, with a wiring
  table and run command
  ([README](https://github.com/embeddedci-com/embeddedci-python/blob/main/packages/embeddedci/examples/README.md)).
* [`tests/examples/`](https://github.com/embeddedci-com/embeddedci-python/tree/main/packages/embeddedci/tests/examples)
  — a multi-case BMP280 HIL suite, CAN loopback and ECU-simulator demos, and a smoke test.

## Releasing (maintainers)

Releases publish to [PyPI](https://pypi.org/project/embeddedci/) automatically via
`.github/workflows/publish.yml` using PyPI **Trusted Publishing** (OIDC) — no API token or secret is
stored in GitHub.

**One-time setup** on PyPI (or first via [TestPyPI](https://test.pypi.org)): project →
*Settings → Publishing → Add a pending publisher* with owner `embeddedci-com`, repo
`embeddedci-python`, workflow `publish.yml`, environment `pypi`.

This is a monorepo, so each package releases on its **own** tag — `publish.yml` triggers on
`embeddedci-v*` for this package (and `embeddedci-mcp-v*` / `embeddedci-openhtf-v*` for the others).
To cut a release:

```bash
# 1. bump the version in pyproject.toml (e.g. 2.0.0 -> 2.1.0), update CHANGELOG.md, commit
# 2. tag and push — the tag is embeddedci-v<version> and must match the version
git tag embeddedci-v2.1.0
git push origin embeddedci-v2.1.0
```

The tag push builds the sdist + wheel, runs `twine check`, and publishes. The git tag is the source
of truth for what shipped; keep `embeddedci-v<version>` equal to the `version` in `pyproject.toml`.

Any change to the public surface fails `tests/test_api_surface.py` on purpose. Additions are fine in
a minor release; removals, renames and signature changes need a new major version. After reviewing,
refresh the snapshot with `UPDATE_API_SURFACE=1 pytest tests/test_api_surface.py`.

Build and verify locally before tagging:

```bash
python -m pip install build twine
python -m build           # -> dist/embeddedci-*.tar.gz and *.whl
twine check dist/*
```
