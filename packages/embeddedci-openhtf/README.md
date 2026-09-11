# embeddedci-openhtf

Drive an **EmbeddedCI BenchPod** from [OpenHTF](https://www.openhtf.com/) —
Google's open-source hardware test framework — connecting **directly** to the pod
over a TCP socket or serial port. No EmbeddedCI cloud account, OIDC, or web UI is
required: this package is for teams who want OpenHTF's test sequencing, limits,
records, and station GUI while talking straight to a pod on their own bench.

It's a thin wrapper over the [`embeddedci`](../embeddedci) BenchPod SDK: a single
plug plus phase helpers. The dependency direction is strictly
**`embeddedci-openhtf` → `embeddedci`**, and 2.x requires `embeddedci` 2.x. Upgrading
from 0.1? See the [changelog](CHANGELOG.md).

```bash
pip install embeddedci-openhtf      # pulls in embeddedci + openhtf
```

## Units and conventions

The same as the `embeddedci` SDK:

- **Volts, seconds and hertz everywhere.** `amplitude`, `offset`, `volts` and every
  analog limit are volts; `duration`, `delay`, `settle` are seconds; `freq_hz` and
  `sample_rate_hz` are hertz. Analog measurements are recorded in volts (units `"V"`).
  The SDK converts to the firmware's codes for you.
- **Invalid arguments raise `ValueError`** — from the SDK when the phase runs (the
  phase then ERRORs), or from a phase factory as you build the test.
- SDK calls return **typed results** (`DacOutput`, `AdcReading`, `Capture`, ...), not dicts.

## The plug

`BenchPodPlug` opens a `BenchPod` when a test starts and closes it at teardown.
Bind the connection inline with `benchpod_plug(...)`, or leave it unbound and
supply it through OpenHTF config / environment variables.

```python
import openhtf as htf
from embeddedci_openhtf import benchpod_plug

# TCP — or benchpod_plug("/dev/ttyACM0"); la_voltage is the DUT's I/O voltage in volts
bench = benchpod_plug("192.168.1.50:8080", la_voltage=3.3)

@htf.plug(bench=bench)
def power_up(test, bench):
    bench.power_on()                                  # methods proxy to the BenchPod SDK client
    status = bench.pod.target_status()                # or reach the full client via .pod
    test.logger.info("eFuse tripped: %s", status.efuse(1).fault)
```

Connection forms (all **direct**, never cloud):

| Form | Example |
| --- | --- |
| TCP `host[:port]` | `benchpod_plug("192.168.1.50:8080")` |
| Serial device path | `benchpod_plug("/dev/ttyACM0")` / `benchpod_plug("COM5")` |
| `BENCHPOD_CONNECTION` env | `benchpod_plug()` (unbound) |
| OpenHTF config | `htf.conf.load(benchpod_connection="...")` then `@htf.plug(bench=BenchPodPlug)` |

Extra keyword arguments to `benchpod_plug` go to `BenchPod(...)` (`la_voltage=`,
`timeout=`, or `transport=` to inject a fake backend in tests).

### LA voltage

The pod refuses every LA-bank operation — **flashing, the UART proxy, LA capture,
pull resistors, I2C-sensor emulation** — until the LA I/O-bank voltage is selected.
Set it to your DUT's I/O voltage (`1.8` or `3.3` volts); the plug applies it right
after connecting. Resolution order: `benchpod_plug(..., la_voltage=)`, then the
`benchpod_la_voltage` conf key, then the `BENCHPOD_LA_VOLTAGE` env var. Leave all three
unset for analog-only tests that don't touch the LA bank.

### OpenHTF config keys

| Key | Default | Meaning |
| --- | --- | --- |
| `benchpod_connection` | `None` → `BENCHPOD_CONNECTION` | `host[:port]`, a serial device path, or `serial` |
| `benchpod_timeout` | `30.0` | Transport timeout in seconds |
| `benchpod_la_voltage` | `None` → `BENCHPOD_LA_VOLTAGE` | LA I/O-bank voltage in volts (`1.8` or `3.3`) |

```python
import openhtf as htf
from embeddedci_openhtf import BenchPodPlug

htf.conf.load(benchpod_connection="/dev/ttyACM0", benchpod_la_voltage=3.3)

@htf.plug(bench=BenchPodPlug)
def power_up(test, bench):
    bench.power_on()
```

A value bound with `benchpod_plug(...)` wins over the conf key.

## Phase helpers

Ready-made, fully-decorated phases for the common steps:

```python
import openhtf as htf
from embeddedci_openhtf import benchpod_plug, boot_banner_phase, flash_phase, power_phase

bench = benchpod_plug("192.168.1.50:8080", la_voltage=3.3)

test = htf.Test(
    power_phase(bench, on=True),
    flash_phase(bench, file="fw.elf", target="target/stm32f4x.cfg",
                swclk=11, swdio=12, nreset=True),   # records flash_ok, attaches openocd.log
    boot_banner_phase(bench, rx=1, tx=2, expect="APP_OK",
                      duration=5.0),                # records boot_ok, attaches uart.txt
)
test.execute(test_start=lambda: "SN-0001")
```

LA channels are 1-12 (the pod has 12 generic logic-analyzer channels and no
fixed-role pins — wire any DUT signal to any channel and name it here).

`flash_phase` needs `openocd` on PATH (the pod is the CMSIS-DAP probe; OpenOCD
runs the flash algorithm from the `target=` config, so every OpenOCD-supported
MCU works unchanged).

For anything custom, write a normal phase and use the recorders in
`embeddedci_openhtf.measurements` (`record_flash`, `record_uart`, `record_samples`)
to map SDK results onto measurements and attachments:

```python
import openhtf as htf
from embeddedci_openhtf import benchpod_plug, record_uart

bench = benchpod_plug("/dev/ttyACM0", la_voltage=3.3)

@htf.measures(htf.Measurement("boot_ok").equals(True),
              htf.Measurement("rail_v").in_range(4.75, 5.25).with_units("V"))
@htf.plug(bench=bench)
def boot_and_rail(test, bench):
    cap = bench.power_cycle_and_capture(rx=1, tx=2, delay=1.0, duration=5.0, until="APP_OK")
    record_uart(test, cap, name="boot_ok")
    test.measurements.rail_v = bench.power_status().rail(1).bus_voltage   # volts
```

### Analog steps

The pod's DAC output and ADC input are exposed as phases (and low-level helpers).
Every analog quantity is in **volts**; limits are `(low, high)` volts.

```python
import openhtf as htf
from embeddedci_openhtf import (
    adc_capture_phase, adc_read_phase, benchpod_plug, dac_output_phase,
    loopback_measure_phase, signal_generate_phase,
)

bench = benchpod_plug("192.168.1.50:8080")

test = htf.Test(
    # drive a 1 V-peak sine centred on 2.5 V (5V DAC path), capture the front ADC SMA
    # while it runs, stop the DAC, and assert the round trip in volts
    loopback_measure_phase(bench, waveform="sine", freq_hz=100, amplitude=1.0,
                           samples=4096, sample_rate_hz=50_000,
                           pp_range=(1.8, 2.2), mean_range=(2.4, 2.6)),
    # or: run a waveform for 2 s, then capture the ADC separately
    signal_generate_phase(bench, waveform="square", freq_hz=100, amplitude=1.0, duration=2.0),
    adc_capture_phase(bench, samples=4096, sample_rate_hz=50_000, pp_range=(1.8, 2.2)),
    # a calibrated DC level and a single averaged reading
    dac_output_phase(bench, path="5v", volts=2.5),
    adc_read_phase(bench, source="ext", v_range=(2.4, 2.6)),         # records ext_v
)
```

| Phase | Records |
| --- | --- |
| `adc_capture_phase`, `loopback_measure_phase` | `<prefix>_mean_v`, `_pp_v`, `_rms_v`, `_min_v`, `_max_v` (V); samples attached as `adc.json` (`counts` + `volts`) |
| `adc_read_phase` | `<source>_v` (V) |
| `control_loop_phase` | `control_loop_v` (DAC code), `control_loop_i` (ADC code) |
| `signal_generate_phase`, `dac_output_phase`, `dac_replay_phase` | log only |

Notes:

- `source` routes the ADC: `"ext"` (front SMA — the default, and the input the
  capture volts are calibrated for), `"cal1"` / `"cal2"` (the 5 V / 12 V DAC looped back
  internally), `"amp"`; `source=None` leaves the routing alone.
- Starting a waveform or a DC output re-applies that DAC path, which opens the internal
  `cal1`/`cal2` loopback relays. Route the ADC **after** starting the DAC —
  `loopback_measure_phase` does this for you.
- A waveform started without `duration` keeps running after its phase; stop it with
  `signal_stop(bench)` (e.g. in a teardown phase).
- Replaying waveforms (`replay`, `replay_waveform`, `dac_replay_phase`) needs a TCP or
  cloud connection; everything else works over serial too.

The low-level helpers take a connected `BenchPod` or the injected plug:
`signal_generate`, `signal_stop`, `analog_path`, `dac_output`, `adc_read`,
`adc_capture`, `replay`, `replay_waveform`, `control_loop`, `fpga_image`.

```python
import openhtf as htf
from embeddedci_openhtf import adc_capture, benchpod_plug, signal_generate

@htf.measures(htf.Measurement("ripple_v").in_range(0.0, 0.05).with_units("V"))
@htf.plug(bench=benchpod_plug("192.168.1.50:8080"))
def ripple_under_load(test, bench):
    with signal_generate(bench, waveform="square", freq_hz=10, amplitude=2.0, dac_path="5v"):
        cap = adc_capture(bench, 8192, sample_rate_hz=20_000, source="ext")
    test.measurements.ripple_v = cap.rms_ac()        # the DacHandle stopped the DAC on exit
```

## Station mode (persistent connection)

By default the plug opens a connection per `Test.execute()` and closes it at
teardown. On a station cycling many DUTs back-to-back, pass `persistent=True` to
keep **one** connection open across executions (re-checked with a ping each run,
reconnected if it dropped). Reuse the *same* plug class for every execution, and
close it once at the end:

```python
import openhtf as htf
from openhtf.plugs import user_input
from embeddedci_openhtf import (
    benchpod_plug, boot_banner_phase, close_persistent_benchpods, power_phase,
)

bench = benchpod_plug("192.168.1.50:8080", persistent=True, la_voltage=3.3)
test = htf.Test(power_phase(bench, on=True),
                boot_banner_phase(bench, rx=1, tx=2, expect="APP_OK"))
try:
    while test.execute(test_start=user_input.prompt_for_test_start()):
        pass            # next DUT — same pod connection
finally:
    close_persistent_benchpods()   # also runs automatically at process exit
```

## Examples

* [`examples/flash_and_boot.py`](examples/flash_and_boot.py) — flash over SWD then
  assert the boot banner, over a direct TCP connection.
* [`examples/serial_smoke.py`](examples/serial_smoke.py) — a no-flash power + UART
  smoke test with parsed and measured values in volts, over a direct serial connection.
* [`examples/analog_loopback.py`](examples/analog_loopback.py) — DAC→ADC loopback
  signal-path self-test in volts, over a direct TCP connection.
* [`examples/station.py`](examples/station.py) — a station loop testing many DUTs
  over one **persistent** connection.

## Development

```bash
# from the repo root
pip install -e "packages/embeddedci[dev]"
pip install -e "packages/embeddedci-openhtf[dev]"
pytest packages/embeddedci-openhtf
```

The test suite runs the real OpenHTF executor against an in-memory fake transport
(`tests/_fake.py`), so it needs no pod and no OpenOCD.
