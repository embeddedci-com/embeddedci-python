# Changelog — `embeddedci`

## Unreleased

- Cloud commands retry twice (0.5 s, then 1.5 s) when Cloudflare answers 502/503/504/52x
  instead of the server, and when the server says the pod is on another instance. One edge blip
  used to fail a CI job. A pod that is offline or timed out still fails at once, and a CAN
  write, reset pulse or step-pulse train is never repeated.
- ADC captures read back about 6.7x faster on firmware that offers base64 samples
  (`capture_b64` in `status.caps`): `capture_adc(2_000_000)` at 400 kS/s now takes 5 s + 4.5 s
  instead of 5 s + 31 s over the LAN. The SDK asks for it on its own; older firmware still gets
  the decimal request. New `Capabilities.capture_b64`.
- `Capabilities` gains `board_rev`, `nrst_pin` and `usb_cc`: the firmware reported them, the SDK
  dropped them.
- Fix: `benchpod_target` powered eFuse 1 whatever the wiring profile said, while
  `power_cycle_and_capture` and `measure_power` used the profile's rail. Without
  `--benchpod-efuse` it now follows the profile.
- Fix: `reset_target(pulse=)` above 1 s raises `ValueError`. The pod clamps the pulse to 1 s, so
  a longer one was silently shortened. The docstring no longer claims it returns before the pulse
  ends.

## 2.0.0 — the frozen API

2.0.0 is the first release with a stability promise. Everything in
`embeddedci.benchpod.__all__` follows [semantic versioning](https://semver.org): within 2.x
names and signatures only grow, never break. `tests/api_surface.json` snapshots that surface and
CI fails on any unreviewed change. `BenchPod.command()`, `BenchPod.transport` and
`BenchPod.lowlevel` are escape hatches **outside** the promise.

### Conventions (apply everywhere)

- **Units are volts, seconds and hertz.** `sample_rate_mhz` → `sample_rate_hz`, `*_us`/`*_ms`
  arguments → seconds, millivolt/microamp readings → volts/amps.
- **Invalid arguments raise `ValueError`.** `BenchPodError` (and subclasses) now means a device,
  transport or server failure only.
- **Device state is typed.** Methods that returned raw firmware dicts return frozen dataclasses
  from `embeddedci.benchpod.state` (each keeps the untouched reply in `.raw`).
- **String options are `Literal` types** (`DacPath`, `AnalogPath`, `AdcSource`, `LoopSource`,
  `Waveshape`, `ReplayMapping`, `DecodeProtocol`, `CanMode`, `FaultType`) and are validated.
- Python **3.10+** (3.9 is end-of-life).

### Migration table

| 1.x / 0.x | 2.0 |
| --- | --- |
| **Connection** | |
| `bp = BenchPod(conn); bp.set_la_voltage(3.3)` | `BenchPod(conn, la_voltage=3.3)` (or `BENCHPOD_LA_VOLTAGE=3.3`) |
| pytest: `--benchpod-la-voltage 3.3` on every run | override the `benchpod_la_voltage` fixture once in `conftest.py` (the flag remains a per-run override) |
| `status()` returned text over serial | `status()` is a dict on every transport |
| `transport.set_la_voltage(mv)` / `get_la_voltage()` | removed — use `BenchPod.set_la_voltage` / `get_la_voltage` |
| **LA voltage** | |
| `set_la_voltage(3300)` (millivolts accepted) | `set_la_voltage(3.3)` — volts only |
| `set_la_voltage(...)` / `get_la_voltage()` → `{"mv", "st"}` | → `LaVoltage(voltage, readback)`; `voltage is None` until selected |
| **Power** | |
| `power_status()` → `{"internal": {"bus_mv", "current_ua"}, …}` | → `PowerStatus`: `.rail(1).bus_voltage` (V), `.current` (A) |
| `command({"cmd": "target_status"})` | `target_status()` → `TargetStatus`: `.efuse(1).fault` |
| — | new: `reset_target(pulse=0.1)`, `set_reset(asserted)`, `reset_state()`, `usb_cc()` |
| **Bias resistors** | |
| `pullup(la, on=True/False)` | `set_pull(la, enabled)` → `PullState` |
| `pullup(la)` (query) | `pull_state(la)` → `PullState(enabled, direction, ohms)` |
| `pullup_status()` → `{"la_pullup_mask"}` | `enabled_pulls()` → `[1, 2]` |
| `enable_pullup(7)` silently engaged a pull-**down** | raises `ValueError`; use `enable_pulldown(7)` / `disable_pulldown(7)` |
| **Flash** | |
| `flash(nreset=PIN3)` — an LA channel wired to the target's NRST | `flash(nreset=True)` — NRST wired to the pod's own reset pin (DUT header J1 pin 22, rev3 pods) |
| **Emulated I2C sensor** | |
| `i2c_sensor_la_decoded(samples, sample_rate_mhz)` | `i2c_sensor_capture(samples, *, sample_rate_hz)` |
| `i2c_sensor_la(...)` (raw packed bytes) | removed — `i2c_sensor_capture` decodes |
| `i2c_sensor_regs(start, length)` | `i2c_sensor_regs(*, start, length)` |
| **Analog** | |
| `analog_path("5v")`, `("ext")`, `("sma")` aliases | canonical names only: `"dac_5v"`, `"adc_ext"`; returns `AnalogPathState` |
| `dac_output(path, volts)` → `{"path", "mv", "code"}` | `dac_output(path, *, volts=)` → `DacOutput(path, voltage, code)` |
| `adc_read(source)` → `{"source", "mv", "count"}` | → `AdcReading(source, voltage, count, span)` |
| `measure_volts(source)` | `adc_read(source).voltage` |
| `route_dac_to_adc("5v")` / `("12v")` | `analog_path("cal1")` / `analog_path("cal2")` |
| `adc_from_sma()` | `analog_path("adc_ext")` |
| `dac_mux`, `dac_mux_status`, `cal_switch`, `cal_switch_status` | `bp.lowlevel.dac_mux(...)` etc. (outside the stability promise) |
| `dac_set(value, channel=)` (`channel` was ignored) | `bp.lowlevel.dac_set(code, divider=)` |
| **Captures** | |
| `capture(samples, sample_rate_mhz=)` → raw `List[int]` | `capture_adc(samples).counts` |
| `scope_capture(samples, *, sample_rate_mhz, source)` | `capture_adc(samples, *, sample_rate_hz, source)` — `source` now actually routes the ADC; default 4096 samples; above 32768 it streams from PSRAM |
| `capture_la(n, *, sample_rate_mhz, stop_dac_after_us)` | `capture_la(n, *, sample_rate_hz, stop_dac_after)` (seconds) |
| `capture_analog(adc_samples, adc_rate_mhz, la_samples, la_rate_mhz, stop_dac_after_us)` | `capture_correlated(adc_samples, adc_sample_rate_hz, la_samples, la_sample_rate_hz, stop_dac_after)` |
| `AnalogCapture` | `CorrelatedCapture` |
| `Capture.duration_s`, `LaCapture.duration_s` | `.duration` |
| `LaCapture.decode_i2c(sda=, scl=)` | `LaCapture.decode("i2c", sda=, scl=)` |
| **DAC** | |
| `generate(w, *, freq, amplitude, offset=0.0, duration_ms, sample_rate_mhz)` — amplitude/offset were firmware 8-bit **codes**, offset defaulted to code 0 | `generate(w, *, freq_hz, amplitude, offset=None, dac_path="5v", duration, sample_rate_hz)` — **volts**; offset defaults to mid-range; routes `dac_path`; returns a `DacHandle` |
| `replay(..., sample_rate_mhz=)` | `replay(..., sample_rate_hz=)` (defaults to a `Capture`'s own rate) |
| `replaying(...)` | `replay(...)` (already a context manager) |
| `replay_waveform(waveform_id="…")` | `replay_waveform("…")` (positional id or `Waveform`) |
| `replay_waveform(..., dac_path="5v")` default | `dac_path=None` → the waveform's own path (else `5v`), used for both routing and scaling |
| `Segment(shape, duration_ms, v_start, v_end)` | `Segment(shape, duration, v_start, v_end)` — `duration` in seconds |
| `dac_stop()` / `ReplayHandle.stop()` returned the reply | return `None` |
| **UART** | |
| `UartSession.read(timeout=)` returned everything received so far | returns the output since the last read and marks it read (everything received is `.text`) |
| `UartSession.drain()` | `read()` |
| `UartSession.read_until` / `expect` searched everything ever received and consumed nothing; both returned the match | both search the unread output and mark it read up to the end of the match; `read_until` returns that output (or `None`), `expect` returns the match (`re.Match` for a regex) |
| **Control loop** | |
| `loop_input(...)` → dict | → `LoopState(source, input_code, step, output_code)` |
| `ControlLoopHandle.set_input(...)` → dict | → `LoopState` |
| `IVPoint.current_code`, `IVPoint.voltage_code` | `IVPoint.i`, `IVPoint.v` |
| `fpga_image(0)` → dict | `fpga_image(FpgaImage.LOOP)` → `FpgaImageInfo(image, version, features)` |
| **CAN** | |
| `can_read(max=8)` → `{"frames", "overflow"}` | `can_read(max_frames=8)` → `CanReadResult(frames: List[CanFrame], overflow)` |
| `CanBus.read(max=8)` | `CanBus.read(max_frames=8)` |
| **Stepper** | |
| `la_step(la, steps, delay_us, dir_la, direction)` | `la_step(la, *, steps, delay, dir_la=None, direction=0)` — `delay` in seconds |
| **Errors** | |
| `coerce_pin` / `coerce_efuse` / decoder and flash argument checks raised `BenchPodError` / `FlashError` | raise `ValueError` |

### Added

- `BenchPod(la_voltage=...)` / `BENCHPOD_LA_VOLTAGE` select the LA bank voltage on connect; the
  `benchpod_la_voltage` pytest fixture sets it once for a test suite from `conftest.py`.
- `target_status()`, `reset_target()`, `set_reset()`, `reset_state()`, `usb_cc()`.
- `enable_pulldown()`, `disable_pulldown()`, `set_pull()`, `pull_state()`, `enabled_pulls()`.
- `LoopInputMap` (`control_loop(input_map=...)`) and `Capabilities.dac_loop_input_map` for
  gateware v30's engineering-units loop input.
- `LaCapture.edges(la)`, `FpgaImage`, `DacHandle`, `CanReadResult`, `CloudAuthError` export,
  the `Literal` option types, and the `embeddedci[pytest]` extra.
- The serial transport probes whether the pod's USB console has a JSON mode. The STM32 pod's does
  not (it is a text shell), so over USB `status()` (parsed from the text report), `ping()`, the LA
  voltage and target power on/off run as text commands, and every other operation raises a
  `TransportError` at once that points at the network/cloud connection (instead of timing out).
  On firmware with a JSON mode, chunked replies now stream too.
- `generate(..., route=False)` / `replay(..., route=False)` keep the current analog switching, so a
  DAC→ADC loopback set up with `analog_path("cal1")` survives starting the output.
- `BenchPod.leased`: whether this client holds a cloud device lease.
- **Automatic gateware image switching.** `control_loop()`, `replay()` and `replay_waveform()` take
  `switch_image=True` and switch the pod to the image they need (loop / deep replay) instead of
  failing, logging a warning; `ControlLoopHandle.switched_image` / `ReplayHandle.switched_image`
  record the switch. `switch_image=False` raises a `BenchPodError` (the control loop used to reach
  the firmware and fail there). The `benchpod_capability` marker switches the image for
  `dac_control_loop` / `dac_deep_replay` instead of skipping. The README has a new
  "Gateware images" section.
- **Wiring profiles.** `Wiring` / `Signal` describe which DUT signal is on which LA channel (UART, I2C,
  SWD, SPI roles, the target-power rail, the LA voltage and named signals) with the same schema the
  embeddedci.com server and web UI store. `BenchPod(wiring=...)`, `bp.wiring` (argument → `BENCHPOD_WIRING`
  file → a cloud device's stored profile → defaults), `bp.save_wiring()`, `bp.signal(name)`, the pytest
  `benchpod_wiring` fixture and `--benchpod-wiring`. Channel arguments of `open_uart`, `capture_uart`,
  `power_cycle_and_capture`, `flash`, `enable_i2c_sensor` and the power methods are now optional and
  fall back to the profile; LA arguments accept role/signal names.
- **GPIO on the LA pins and pin ownership.** `bp.gpio()` → `GpioPin` (`configure`, `set`, `high`/`low`,
  `activate`, `read`, `wait_for`, `pulse`, `release`), `set_gpio`, `read_gpio`, `pin_levels`,
  `wait_for_level`, `release_gpio`, `la_pins()` → `LaPinState`, and `gpio_pins()`/`configure_gpio()` to
  claim several channels as one group (a conflict on any of them claims none). Each channel has one
  function at a time:
  a second one fails with `PinConflictError`, an incompatible bias resistor with `PullConflictError`.
- **Triggered captures.** `Trigger(la, edge)` on `capture_adc`/`capture_la`/`capture_correlated`
  (`trigger_timeout`, `TriggerTimeout`); results carry `.trigger`.
- **Timing helpers.** `LaCapture.edge_times`, `first_edge`, `level_at`, `pulse_widths`, `frequency`,
  `duty_cycle`, `delay`; `Capture.crossing_times`, `first_crossing`; the `Edge` option type.
- **Power profiles.** `bp.measure_power()` and `bp.power_profile()` → `PowerProfile` /
  `PowerProfileSession` (average/min/peak current, voltage, energy, charge, trace) from timestamped
  sampling of the rail monitor, integrated over real time. `rate_hz` is the rate actually delivered
  (measured) and `adc_rate_hz` the sensor's configured conversion rate: ask for 100-500 Hz and the
  pod tracks the request to ~200 Hz, flattening near 365 Hz. The README documents the rails' current
  limits.
- `Capabilities.la_pins`, `gpio_read`, `capture_trigger`, `power_profile`.
- The `hardware` marker now skips a test when no connection is configured, even if the test
  requests no device fixture.

### Fixed

- `generate()` sent `offset=0.0` (8-bit code 0) by default and truncated fractional volts; it now
  converts volts to the generator's codes for the chosen path.
- `BENCHPOD_API_BASE` was ignored by the cloud transport when `BenchPod` was constructed directly.
- `scope_capture(source=...)` only labelled the result; `capture_adc(source=...)` routes it.
- The `benchpod_target` fixture ignored `--benchpod-efuse`.
- Over USB, `target_power` sent a `target-power` verb the STM32 console doesn't have (it is
  `power`); a delayed change over USB now raises instead of being silently ignored.
- `LaVoltage.readback` is `None` (not 0.0 V) on boards that cannot measure the bank.
- `fpga_image` was documented as a ~10 ms warm boot; it reprograms the FPGA (~2-3 s).
- The `12v` DAC path was mapped as 0..12 V; it is bipolar −12..+12 V (0 V at mid-scale), so
  `generate` and `replay` volts on it were wrong — confirmed against the firmware calibration and on
  a pod. `generate`'s default offset on `12v` is now 0 V. (`dsp.volts_to_codes` gained
  `path_min_v`; `dsp.dac_path_range_v` gives each path's range.)
- A replay deeper than 2048 samples on the control-loop gateware image was accepted and then
  produced no output (measured on a pod); it now switches the pod to the deep-replay image first
  (or raises a `BenchPodError` with `switch_image=False`).
- `DacOutput.path` reports the output path you asked for (`5v`), not the firmware's analog-path
  name (`dac_5v`).
- `replay_waveform` scaled a segments waveform for its stored path but routed the `5v` default.
- `capture_adc(source=...)` now waits for the relays to settle before capturing.
- Error messages no longer claim the cloud destination is CI-only or suggest OpenOCD builds that
  lack the `cmsis_dap_tcp` backend.
