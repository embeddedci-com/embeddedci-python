# Changelog — `embeddedci-openhtf`

## 2.0.1

- Docs: LA channels are 1-14 on BenchPod v3 pods with firmware 3.1+ (LA13/LA14), which
  `embeddedci` 2.1 accepts. Now requires `embeddedci>=2.1`, which is where those channels
  became valid.

## 2.0.0 — ported to the frozen `embeddedci` 2.0 API

The version jumps from 0.1.0 to 2.0.0 to track `embeddedci` 2.x, which this release requires
(`embeddedci>=2.0,<3`). Names exported from `embeddedci_openhtf.__all__` now follow semantic
versioning. See the [`embeddedci` changelog](../embeddedci/CHANGELOG.md) for the SDK-side changes.

### Conventions

- **Units are volts, seconds and hertz**, as in the SDK: `freq` → `freq_hz`,
  `sample_rate_mhz` → `sample_rate_hz`, `duration_ms` → `duration` (seconds), millivolt
  measurements → volts. Analog measurements carry units `"V"`.
- **Invalid arguments raise `ValueError`**, from the SDK at run time or from a phase factory
  when you build the phase (bad `source`, a `(low, high)` range with `low > high`,
  negative `settle`).
- Helpers return the SDK's **typed results** (`DacHandle`, `DacOutput`, `AdcReading`,
  `Capture`, `AnalogPathState`, `FpgaImageInfo`) instead of firmware dicts.

### Breaking changes

| 0.1 | 2.0 |
| --- | --- |
| **Dependencies** | |
| `embeddedci>=0.2` | `embeddedci>=2.0,<3` |
| **Signal generation** | |
| `signal_generate(bench, *, waveform, freq, amplitude, offset=128.0, duration_ms, sample_rate_mhz)` — `amplitude` 0-127 / `offset` 0-255 firmware 8-bit codes, raw `generate` JSON | `signal_generate(bench, *, waveform, freq_hz, amplitude, offset=None, dac_path="5v", duration=None, sample_rate_hz=None)` — **volts**, `offset` defaults to mid-range, delegates to `BenchPod.generate`, returns a `DacHandle` |
| `signal_generate_phase(plug, *, waveform, freq, amplitude, offset=128.0, duration_ms, sample_rate_mhz)` | `signal_generate_phase(plug, *, waveform, freq_hz, amplitude, offset=None, dac_path="5v", duration=None, sample_rate_hz=None)` |
| `signal_stop(bench)` returned the raw reply | returns `None` (`BenchPod.dac_stop()`) |
| **Loopback** | |
| `measure(bench, *, waveform, freq, amplitude, offset, samples, sample_rate_mhz)` (raw firmware `measure`, TCP only) | removed — use `loopback_measure_phase`, or `bench.generate(...)` + `bench.capture_adc(...)` |
| `loopback_measure_phase(plug, *, waveform, freq, amplitude, offset=128.0, samples, sample_rate_mhz, prefix, mean_range, pp_range, min_range, max_range)` recorded raw-count `<prefix>_min/_max/_mean/_pp` | `loopback_measure_phase(plug, *, waveform="sine", freq_hz, amplitude, offset=None, dac_path="5v", samples=4096, sample_rate_hz=None, source="ext", settle=0.1, prefix="adc", mean_range, pp_range, rms_range, min_range, max_range, attachment="adc.json")` — starts `generate`, routes `source`, captures while it runs, stops the DAC, records `<prefix>_mean_v/_pp_v/_rms_v/_min_v/_max_v` in volts |
| **ADC capture** | |
| `adc_capture_phase(plug, *, samples, sample_rate_mhz, prefix, mean_range, pp_range, min_range, max_range)` recorded raw-count `<prefix>_min/_max/_mean/_pp` | `adc_capture_phase(plug, *, samples=4096, sample_rate_hz=None, source="ext", prefix="adc", mean_range, pp_range, rms_range, min_range, max_range, attachment="adc.json")` — records calibrated `<prefix>_mean_v/_pp_v/_rms_v/_min_v/_max_v`; `source` routes the ADC (`None` leaves routing alone) |
| `scope_capture_phase(plug, *, samples, sample_rate_mhz, source, prefix="scope", mean_range, pp_range, rms_range)` | removed — `adc_capture_phase(...)` (note the default `prefix` is `"adc"`) |
| `scope_capture(bench, *, samples, sample_rate_mhz, source="ext")` | `adc_capture(bench, samples=4096, *, sample_rate_hz=None, source=None)` → `Capture` |
| `adc.json` attachment was a list of raw counts | an object `{"source", "sample_rate_hz", "counts", "volts"}` |
| **DC output / single reading** | |
| `dac_output(bench, path, volts=None)` → `{"path", "mv", "code"}` | `dac_output(bench, path, *, volts=None)` → `DacOutput(path, voltage, code)` |
| `dac_output_phase` logged `mv` | logs `DacOutput.voltage` (V) and `code` |
| `adc_read(bench, source)` → `{"source", "mv", "count"}` | → `AdcReading(source, voltage, count, span)` |
| `adc_read_phase(plug, *, source, mv_range)` recorded `<source>_mv` (mV) | `adc_read_phase(plug, *, source="ext", v_range)` records `<source>_v` (V) |
| **Other helpers** | |
| `analog_path(bench, path)` → dict | → `AnalogPathState`; canonical path names only (`"dac_5v"`, `"adc_ext"`, ...) |
| `fpga_image(bench, image)` → dict | → `FpgaImageInfo(image, version, features)`; accepts `FpgaImage` |
| `replay_waveform(bench, waveform_id, **kw)` | `replay_waveform(bench, waveform, **kw)` (id or `Waveform`, positional) |
| **Plug** | |
| `BenchPodPlug.pod_kwargs` was a shared mutable `{}` class default | an immutable `MappingProxyType`; `benchpod_plug(...)` stores a read-only copy |

### Added

- **Wiring profile.** `benchpod_plug(..., wiring=<dict | "bench.json" | Wiring>)` passes through to
  `BenchPod(wiring=...)`, so the bench's map of DUT signal → LA channel supplies the channels, baud,
  power rail and SWD target a helper or phase leaves out, and its names work wherever a channel
  number does (`gpio(bench, "TRIGGER")`, `la_delay_phase(from_la="TRIGGER", to_la="READY")`).
- **GPIO on the LA pins** (`embeddedci_openhtf.pins`): helpers `gpio(bench, la, mode="output",
  level=None)`, `set_gpio`, `read_gpio`, `release_gpio`, and the phase
  `gpio_phase(plug, *, la, mode="output", level=None, name="gpio")`. Each LA channel has one owner
  at a time, so claiming one another function holds raises `PinConflictError` naming the owner —
  release a GPIO channel before a UART session, a flash or sensor emulation uses it.
- **Timing between two channels**: `la_delay(bench, from_la, to_la, *, samples, sample_rate_hz,
  from_edge="rising", to_edge="rising", trigger=None)` and
  `la_delay_phase(plug, *, from_la, to_la, samples, sample_rate_hz, from_edge, to_edge, trigger,
  delay_range, name="la_delay")`, recording `la_delay_s` (units `"s"`). `trigger` (an SDK
  `Trigger`) starts the capture on an LA edge or level.
- **Power profiles** (`embeddedci_openhtf.power`): `measure_power(bench, duration, **kwargs)` and
  `measure_power_phase(plug, *, duration, efuse=None, rate_hz=500.0, keep_samples=0,
  avg_current_range=None, peak_current_range=None, energy_range=None, prefix="power",
  attachment="power.json", name="measure_power")`, recording `<prefix>_avg_current_a` and
  `_peak_current_a` (A), `_avg_voltage_v` (V) and `_energy_j` (J), with the kept `(t, amps, volts)`
  samples attached as JSON.
- OpenHTF conf key **`benchpod_la_voltage`** (default `None`, falling back to
  `BENCHPOD_LA_VOLTAGE` via the SDK), passed as `BenchPod(la_voltage=...)`. `benchpod_plug(...,
  la_voltage=3.3)` binds it per plug and wins over the conf key. The pod refuses flashing, UART,
  LA capture, pull resistors and I2C-sensor emulation until an LA voltage is selected.
- Top-level exports for every analog helper and phase: `analog_path`, `dac_output`, `adc_read`,
  `adc_capture`, `control_loop`, `fpga_image`, `dac_output_phase`, `adc_read_phase`,
  `control_loop_phase` (previously only importable from `embeddedci_openhtf.analog`).
- `rms_range` / `min_range` / `max_range` and `attachment=` on both volts capture phases.
- `control_loop_phase(..., switch_image=True)` and `dac_replay_phase(..., switch_image=True)`: the
  pod is switched to the gateware image the phase needs (via the SDK's automatic switching), and the
  switch is logged; `switch_image=False` makes the phase fail instead.

### Fixed

- The OpenHTF conf keys `benchpod_connection` and `benchpod_timeout` were never applied:
  `@conf.inject_positional_args` ignores arguments that have defaults. The plug now reads the
  conf directly (explicit argument, then conf, then env).
- `loopback_measure_phase` stops the DAC even when the capture fails.
- The analog helpers are no longer TCP-only: generate, DC output, readings and captures work on
  any transport the SDK supports (replay still needs a TCP or cloud connection).
