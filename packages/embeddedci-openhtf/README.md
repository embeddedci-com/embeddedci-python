# embeddedci-openhtf

> ⚠️ **Experimental.** This package is new and its API may change between
> releases. Pin a version and expect breaking changes before 1.0.

Drive an **EmbeddedCI BenchPod** from [OpenHTF](https://www.openhtf.com/) —
Google's open-source hardware test framework — connecting **directly** to the pod
over a TCP socket or serial port. No EmbeddedCI cloud account, OIDC, or web UI is
required: this package is for teams who want OpenHTF's test sequencing, limits,
records, and station GUI while talking straight to a pod on their own bench.

It's a thin wrapper over the [`embeddedci`](../embeddedci) BenchPod SDK: a single
plug plus a few phase helpers. The dependency direction is strictly
**`embeddedci-openhtf` → `embeddedci`**.

```bash
pip install embeddedci-openhtf      # pulls in embeddedci + openhtf
```

## The plug

`BenchPodPlug` opens a `BenchPod` when a test starts and closes it at teardown.
Bind the connection inline with `benchpod_plug(...)`, or leave it unbound and
supply it through OpenHTF config / the `BENCHPOD_CONNECTION` env var.

```python
import openhtf as htf
from embeddedci_openhtf import benchpod_plug

bench = benchpod_plug("192.168.1.50:8080")   # TCP — or benchpod_plug("/dev/ttyACM0")

@htf.plug(bench=bench)
def power_up(test, bench):
    bench.power_on()                 # methods proxy to the BenchPod SDK client
    bench.pod.capture_uart(...)      # or reach the full client via .pod
```

Connection forms (all **direct**, never cloud):

| Form | Example |
| --- | --- |
| TCP `host[:port]` | `benchpod_plug("192.168.1.50:8080")` |
| Serial device path | `benchpod_plug("/dev/ttyACM0")` / `benchpod_plug("COM5")` |
| `BENCHPOD_CONNECTION` env | `benchpod_plug()` (unbound) |
| OpenHTF config | `htf.conf.load(benchpod_connection="...")` then `@htf.plug(bench=BenchPodPlug)` |

## Phase helpers

Ready-made, fully-decorated phases for the common steps:

```python
from embeddedci_openhtf import benchpod_plug, flash_phase, boot_banner_phase

bench = benchpod_plug("192.168.1.50:8080")

test = htf.Test(
    flash_phase(bench, file="fw.elf", target="target/stm32f4x.cfg",
                swclk=11, swdio=12, nreset=3),      # records flash_ok, attaches openocd.log
    boot_banner_phase(bench, rx=1, tx=2, expect="APP_OK"),   # records boot_ok, attaches uart.txt
)
test.execute(test_start=lambda: "SN-0001")
```

LA channels are 1-12 (the pod has 12 generic logic-analyzer channels and no
fixed-role pins — wire any DUT signal to any channel and name it here).

For anything custom, write a normal phase and use the recorders in
`embeddedci_openhtf.measurements` (`record_flash`, `record_uart`,
`record_samples`) to map SDK results onto measurements and attachments.

`flash_phase` needs `openocd` on PATH (the pod is the CMSIS-DAP probe; OpenOCD
runs the flash algorithm from the `target=` config, so every OpenOCD-supported
MCU works unchanged).

### Analog steps

The pod's DAC output and ADC input are exposed as analog phases (and low-level
helpers). **These are TCP-only** — they need the JSON/sample channel that the
serial console doesn't provide.

```python
from embeddedci_openhtf import (
    benchpod_plug, signal_generate_phase, adc_capture_phase, loopback_measure_phase,
)
bench = benchpod_plug("192.168.1.50:8080")

test = htf.Test(
    # drive the DAC and capture the ADC together (loopback), assert the round trip
    loopback_measure_phase(bench, waveform="sine", freq=10_000, amplitude=120,
                           pp_range=(180, 255), mean_range=(110, 150)),
    # or: free-run a waveform, then snapshot the ADC separately
    signal_generate_phase(bench, waveform="square", freq=1_000, amplitude=100),
    adc_capture_phase(bench, samples=4096, pp_range=(150, 255)),
)
```

Each capture/measure phase records `<prefix>_min` / `_max` / `_mean` / `_pp`
(peak-to-peak) measurements — pass any as `(low, high)` to make it a limit — and
attaches the raw samples as `adc.json`. The low-level helpers `signal_generate`,
`signal_stop`, and `measure` are available for custom phases.

## Station mode (persistent connection)

By default the plug opens a connection per `Test.execute()` and closes it at
teardown. On a station cycling many DUTs back-to-back, pass `persistent=True` to
keep **one** connection open across executions (re-checked with a ping each run,
reconnected if it dropped). Reuse the *same* plug class for every execution, and
close it once at the end:

```python
from embeddedci_openhtf import benchpod_plug, close_persistent_benchpods
from openhtf.plugs import user_input

bench = benchpod_plug("192.168.1.50:8080", persistent=True)
test = htf.Test(power_phase(bench, on=True), boot_banner_phase(bench, rx=1, tx=2, expect="APP_OK"))
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
  smoke test with a parsed measurement, over a direct serial connection.
* [`examples/analog_loopback.py`](examples/analog_loopback.py) — DAC→ADC loopback
  signal-path self-test, over a direct TCP connection.
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
