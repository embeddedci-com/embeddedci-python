# embeddedci — BenchPod SDK and pytest plugin

A Python SDK and pytest plugin for the [EmbeddedCI](https://embeddedci.com) **BenchPod**, a
hardware-in-the-loop instrument that sits next to your device under test (DUT). From a test you
can:

* connect to a pod over the **network**, **USB** or the **cloud** (`embeddedci:<device-name>`)
* switch and monitor **target power**, and pulse the target's **reset**
* **flash** firmware over SWD and assert it worked
* capture the DUT's **UART**, or hold an interactive UART session
* **emulate an I2C sensor** (BMP280) and decode the bus traffic
* capture **ADC** (calibrated volts), a 12-channel **logic analyzer**, or both from one trigger
* drive the **DAC**: DC levels, generated waveforms, arbitrary replay, fault injection, and an
  in-fabric **control loop**
* talk **CAN**, including an autonomous ECU simulator

The same test runs against a pod on your desk or a remote pod in CI.

```python
from embeddedci.benchpod import INTERNAL, PIN4, PIN5, PIN11, PIN12, BenchPod

with BenchPod("192.168.1.213", la_voltage=3.3) as bp:        # or "usb", or "embeddedci:my-bench"
    bp.flash(file="build/app.elf", target="target/stm32f4x.cfg",
             swclk=PIN11, swdio=PIN12, nreset=True, target_power=INTERNAL)
    boot = bp.power_cycle_and_capture(rx=PIN5, tx=PIN4, delay=1.0, duration=5.0, until="APP_OK")
    assert boot.matched, boot.text
```

**Contents:** [Install](#install) · [Quick start](#quick-start) ·
[API conventions](#api-conventions-and-stability) · [pytest plugin](#pytest-plugin) ·
[Power and reset](#power-reset-and-power-monitoring) · [Flashing](#flashing) · [UART](#uart) ·
[Bias resistors](#la-bias-resistors) · [I2C sensor](#emulated-i2c-sensor) ·
[Analog paths](#analog-paths-dc-output-and-single-readings) · [Captures](#captures) ·
[DAC](#dac-generator-replay-and-faults) · [Control loop](#in-fabric-dac-control-loop) ·
[CAN](#can) · [Cloud](#cloud-embeddedcidevice-name) · [Build reporting](#build-reporting) ·
[Errors](#errors) · [Escape hatches](#escape-hatches) · [Releasing](#releasing-maintainers)

## Install

Requires **Python 3.10+**.

```bash
pip install embeddedci
pip install "embeddedci[cloud,analysis]"     # with extras
```

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
`cloud_token`, `cloud_audience`, `lease`, `lease_wait`, `lease_ttl` (see [Cloud](#cloud-embeddedcidevice-name))
and `transport` (inject a custom backend).

### Connection strings

| Form | Transport |
|---|---|
| `192.168.1.213`, `host:8080`, `[fe80::1]:8080` | network (JSON over TCP, port 8080 by default) |
| `/dev/ttyACM0`, `COM3`, `\\.\COM10` | USB console, explicit device path |
| `usb` | USB console, auto-detected by probing the serial ports (`serial` also accepted) |
| `discover` (or `mdns`, `auto`) | find exactly one pod on the LAN via mDNS (needs `[discovery]`); errors on zero or several |
| `embeddedci:<device-name>` | a named device through embeddedci.com (needs `[cloud]`; an API key anywhere, or GitHub Actions OIDC) |

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
| `BENCHPOD_STALL_TIMEOUT` | flashing | seconds with no SWD traffic before a flash attempt is aborted and retried (default 60) |

## API conventions and stability

2.0.0 froze the API. The conventions hold everywhere:

* **Units are volts, seconds and hertz** — `sample_rate_hz`, `delay`, `duration`, `stop_dac_after`,
  `pulse`; readings come back in volts and amps. (Raw DAC/ADC *codes* are named as such.)
* **Invalid arguments raise `ValueError`**. A `BenchPodError` subclass always means a device,
  transport or server failure — see [Errors](#errors).
* **Device state is typed.** Methods return frozen dataclasses (`LaVoltage`, `TargetStatus`,
  `PowerStatus`, `PullState`, `DacOutput`, `AdcReading`, …) from `embeddedci.benchpod.state`; each
  keeps the untouched firmware reply in `.raw`.
* **String options are `Literal` types** and are validated: `DacPath`, `DacOutputPath`,
  `AnalogPath`, `AdcSource`, `LoopSource`, `Waveshape`, `ReplayMapping`, `DecodeProtocol`,
  `CanMode`, `FaultType`.
* **Named constants instead of magic numbers**:

  | Concept | Constants | Wire value |
  |---|---|---|
  | Target-power eFuse | `INTERNAL`, `EXTERNAL` (`Efuse`) | 1, 2 |
  | LA channels | `PIN1` … `PIN12` (`Pin`) | 1 … 12 |
  | Emulated sensor | `Sensor.BMP280` | `"bmp280"` |
  | BMP280 addresses | `BMP280_ADDR_PRIMARY`, `BMP280_ADDR_SECONDARY` | 0x76, 0x77 |
  | Gateware image | `FpgaImage.LOOP`, `FpgaImage.DEEP_REPLAY` | 0, 1 |

  Plain ints work too (`efuse=1`, `swclk=11`); they are validated. The target's reset line is not an
  LA channel — it has a dedicated pin (see [Flashing](#flashing)).

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

Then point pytest at a pod and use the fixtures:

```bash
pytest --benchpod-connection=192.168.1.213
# or: export BENCHPOD_CONNECTION=usb
```

```python
import pytest
from embeddedci.benchpod import INTERNAL, PIN4, PIN5, PIN11, PIN12


@pytest.mark.hardware
def test_firmware_boots(benchpod, firmware):
    benchpod.flash(file=firmware, target="target/stm32f4x.cfg",
                   swclk=PIN11, swdio=PIN12, nreset=True, target_power=INTERNAL)
    boot = benchpod.power_cycle_and_capture(rx=PIN5, tx=PIN4, efuse=INTERNAL,
                                            delay=1.0, duration=5.0, until="APP_OK")
    assert boot.matched, boot.text


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
| `--benchpod-efuse` | — | `1` | target-power rail for `benchpod_target` and `pins.efuse` (1 internal, 2 external) |
| `--benchpod-firmware` | — | — | firmware image for the `firmware` fixture |
| `--benchpod-discover` | — | off | when no connection is configured, find one pod via mDNS (needs `[discovery]`) |
| `--benchpod-api-key` | `BENCHPOD_API_KEY` | — | API key for the cloud destination and the waveform library |
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
| `benchpod` | session | a connected `BenchPod` with the options above applied; closed at session end |
| `benchpod_connection` | session | the resolved connection string (skips when none) |
| `benchpod_target` | function | `benchpod` with the `--benchpod-efuse` rail powered on for the test, off at teardown |
| `benchpod_sensor` | function | `benchpod`; disarms the emulated I2C sensor at teardown |
| `benchpod_dac` | function | `benchpod`; stops any DAC output (generate, replay, control loop) at teardown |
| `benchpod_capabilities` | session | `benchpod.capabilities` |
| `benchpod_waveforms` | function | the cloud `WaveformLibrary`; deletes waveforms saved through it during the test; skips without server access |
| `benchpod_pins` / `pins` | session | `pin_1` … `pin_12`, `efuse`, and `has_pullup()`, `has_pulldown()`, `pull_ohms()`, `pullup_ohms()`, `pull_direction()` |
| `firmware` | function | the `--benchpod-firmware` path (skips when unset) |
| `build_report` | function | a build reporter — see [Build reporting](#build-reporting) |

`benchpod` is shared by the whole session, so state a test leaves behind (a running DAC, engaged
pull-ups, an armed sensor) carries into the next one — use the teardown fixtures.

### Markers

* `@pytest.mark.hardware` — labels tests that need a real pod, for selection
  (`pytest -m "not hardware"`). Skipping comes from the fixtures, not the marker.
* `@pytest.mark.benchpod_capability("dac_deep_replay", ...)` — skips the test unless the connected
  device advertises every named `Capabilities` flag (`scope`, `analyzer`, `dac_replay`,
  `dac_deep_replay`, `dac_control_loop`, `dac_loop_sources`, `dac_cotrig`, …). For the
  image-bound `dac_control_loop` and `dac_deep_replay` it switches the pod's gateware image instead
  of skipping, when the pod carries both images (see [Gateware images](#gateware-images)).

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
state back.

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
| LA9–LA12 | none | — |

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
| `"amp"` | read the amps terminal |
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
routes. `adc_read` routes the source (`"ext"`, `"cal1"`, `"cal2"`, `"amp"`) and returns one averaged,
calibrated reading; the pod refuses it while the input is still moving (e.g. a DAC left running).

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

* `capture_adc(samples=4096, *, sample_rate_hz=None, source=None)` — omitted rate = the device
  maximum; the **achieved** rate is on the result. `source` routes the ADC first (omitted, the
  current routing is left alone). `volts` use the front-SMA calibration; for the other sources
  compare `counts` or use `adc_read`.
* `capture_la(samples=4096, *, sample_rate_hz=None, stop_dac_after=None)` — 12-bit words; bit *n* is
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
channel, `delay` seconds apart, generated by the FPGA; it returns immediately.

## Cloud (`embeddedci:<device-name>`)

The `embeddedci:<device-name>` destination drives a pod that lives somewhere else — behind NAT, in a
lab — through embeddedci.com. The server bridges a raw byte tunnel to the device, so **the full API
works**, including flashing and UART/ADC/LA captures, and the same test you run locally runs
unchanged. Install the `[cloud]` extra.

**Authentication** — the SDK exchanges one of these for a short-lived session token scoped to the
devices you may drive:

1. **An API key** (`eci_…`) via `api_key=`, `--benchpod-api-key` or `BENCHPOD_API_KEY`. Works
   anywhere: your desk, any CI system.
2. **GitHub Actions OIDC** when no API key is set. No secret is stored — the workflow proves which
   repo it is, like PyPI Trusted Publishing. Only works inside a GitHub Actions job.

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
| `FirmwareError` | the pod replied with an error (e.g. "la voltage not set") | `firmware_message`, `cmd` |
| `FlashError` | OpenOCD failed, is missing, or lacks the TCP backend | |
| `TargetUnreachableError` (a `FlashError`) | the probe worked but no target answered on SWD | |
| `DeviceBusyError` | a cloud device lease was not granted within `lease_wait` | |
| `CloudAuthError` | no session token: API key rejected, OIDC unavailable, exchange failed | |
| `ServerApiError` | an embeddedci server API call failed | `status` (HTTP) |
| `UartTimeout` | `UartSession.expect` timed out | `text` |
| `CanTimeout` | `CanBus.expect` timed out | `frames` |

## Escape hatches

These sit below the stable API and are **not** covered by the stability promise:

* `bp.command({"cmd": "status"})` — send one raw JSON firmware command and get its `data` back
  (raises `FirmwareError` when the pod refuses). Works over TCP, USB and the cloud.
* `bp.transport` — the underlying transport object.
* `bp.lowlevel` — individual analog switches and raw DAC codes for bring-up and diagnostics:
  `dac_mux(ctrl1=, ctrl2=)`, `dac_mux_status()`, `cal_switch(cal1=, cal2=, amp_measure=, cal_path=)`,
  `cal_switch_status()`, `dac_set(code, divider=)`. Prefer the named paths — these can leave the
  front end in a state no named path describes.

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
