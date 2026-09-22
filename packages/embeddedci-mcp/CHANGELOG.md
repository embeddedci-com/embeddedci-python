# Changelog — `embeddedci-mcp`

## Unreleased

## 2.1.0

- BenchPod v3 pods now expose LA13/LA14: `la_pins`, `gpio_mode`/`gpio_read`/`gpio_write`,
  `capture_la` and trigger channels accept 1-14 (was 1-12). Built on `embeddedci` 2.1.
- `capabilities` in `connect`/`status` gains `board_rev`, `nrst_pin` and `usb_cc`.
- Fix: `reset_target` accepts `pulse` up to 1 s (was 10). The pod never held it longer.

## 2.0.0

Built on `embeddedci` 2.0. From this release the tool names, input schemas and annotations are
frozen for 2.x (`tests/tools_surface.json`).

### Behaviour

- **The bench's wiring profile drives the defaults.** `wiring` returns the effective profile — a
  12-row pin table (what is wired to each LA channel and its bias resistor), the named signals, the
  target-power rail, the UART baud, the SWD target — and `set_wiring(profile, save)` replaces it
  (`save: true` stores it on embeddedci.com for a cloud device). Arguments that used to be required
  are now optional and fall back to the profile: `power_on`/`power_off` `efuse` (the result says
  which rail was used), `capture_uart`/`power_cycle_and_capture`/`uart_open` `rx`, `tx`, `baud`,
  `enable_i2c_sensor` `sda`, `scl`, `address`, and `flash` `swclk`, `swdio`, `nreset`, `target`.
  Channel arguments also accept the profile's names (`rx: "uart_rx"`, `trigger_la: "READY"`,
  `gpio_mode(la: ["TRIGGER"])`). The `benchpod://wiring` resource now leads with the connected
  device's own profile, followed by the static reference.
- **Triggered captures.** `capture_adc`, `capture_la` and `capture_correlated` take `trigger_la`,
  `trigger_edge` (rising/falling/high/low, default rising) and `trigger_timeout` (seconds, default
  10, max 600); the capture summaries carry `trigger: "LA9 rising"`. Needs the pod's
  `capture_trigger` capability; a condition that never happens fails with `TriggerTimeout: …`.
- **Errors are MCP tool errors.** A failing tool used to return a *successful* result
  `{"ok": false, "error", "error_type"}` (while bad arguments raised); now every failure is an
  `isError` result whose message starts with the cause (`FirmwareError: …`,
  `NotConnectedError: …`, `invalid argument: …`). Negative outcomes of completed operations
  (`flash` `ok: false`, `matched: false`) are still normal results.
- **Structured output.** Every tool returns a typed model with an output schema.
- **Server instructions.** Usage guidance is sent at initialisation instead of only living in the
  `benchpod://help` resource; it now includes the required `set_la_voltage` step.
- **Enums, ranges and annotations** on every tool (read-only / destructive / idempotent / open-world).
- **Non-blocking.** Tools run on worker threads under a device lock; long operations send progress.
  Previously a flash blocked the whole server for its duration.
- **Units are volts, seconds and hertz** throughout.
- **Gateware images switch automatically.** `control_loop`, `replay` and `replay_waveform` take
  `switch_image` (default true), put the pod on the gateware image they need (~3 s) and report it
  as `switched_image`; `switch_image: false` fails instead. The server instructions tell agents
  that a switch resets the FPGA.
- **Cloud pods work out of the box** (`embeddedci[cloud]` is a dependency). `connect` waits at
  most `--lease-wait` (30 s) for a busy pod, and an idle session releases its lease after
  `--idle-timeout` (600 s), reconnecting on the next call.
- **HTTP transport:** `--auth-token` / `EMBEDDEDCI_MCP_TOKEN` bearer auth (mandatory for a
  non-loopback bind), `--allowed-host`. Fixed: a `--host 0.0.0.0` server rejected every remote
  client (FastMCP's loopback-only Host check).

### Tools

| 0.1 | 2.0 |
| --- | --- |
| `ping`, `capabilities`, `get_la_voltage` | `status` (connection, capabilities, LA voltage, sessions, warnings) |
| `target_power(efuse, on)` | `power_on` / `power_off` |
| `target_status`, `power_status` | `power_status` (eFuse state + monitors, volts/amps) |
| — | `reset_target` (pulse / hold / release / status) |
| `scope_capture`, `capture_adc` (raw counts) | `capture_adc` (stats, dominant frequency, envelope; `sample_rate_hz`) |
| `capture_logic` | `capture_la` (per-channel summary) |
| `capture_analog` | `capture_correlated` |
| `logic_decode` (captured every call) | `decode_la` (re-decodes the last capture by default) |
| `measure` | removed — `generate` + `capture_adc` |
| `signal_generate(freq, amplitude, offset)` — firmware codes | `generate(freq_hz, amplitude, offset, dac_path)` — volts |
| `i2c_sensor_la_decoded`, `i2c_read_register` | `i2c_sensor_capture(address?, register?)` |
| `enable_pullup`, `disable_pullup`, `pullup_status` | `set_pull(las, enabled)`, `pull_status` (direction-aware) |
| `dac_mux`, `cal_switch`, `route_dac_to_adc`, `adc_from_sma` | `analog_path` (or `command` for raw access) |
| `replay_waveform(target_samples=4096)` | adds `sample_rate_hz`, `window_start`/`window_len`, `fault`, `route`; `target_samples` defaults to 0 |
| `save_capture_as_recording(name, samples, sample_rate_mhz)` | `save_capture_as_recording(name)` saves the last `capture_adc` |
| `la_step(delay_us)` | `la_step(delay)` (seconds) |
| `fpga_image(0 \| 1)` | `fpga_image("loop" \| "deep_replay")` |
| `control_loop` (preset only) | adds `curve` and `input_map` |
| — | new: `disconnect` returns status, `uart_open`/`uart_write`/`uart_read`/`uart_close`, `replay` (volts or last capture, with `fault`), `can_open`/`can_write`/`can_read`/`can_respond`/`can_status`/`can_close` |
| — | new: `wiring`, `set_wiring` |
| — | new: `la_pins`, `gpio_mode`, `gpio_write`, `gpio_read`, `gpio_wait`, `gpio_pulse`, `gpio_release` |
| — | new: `measure_power`, `power_profile_start`, `power_profile_stop` |

New tool groups:

- **Pins and GPIO.** `la_pins` reports every LA channel's owner (`none`, `gpio`, `uart_rx`,
  `swd_clk`, `i2c_sda`, `step`, …), its GPIO mode and level and its bias resistor, plus live pin
  levels when the gateware can read them. `gpio_mode` claims channels as `input` / `output` /
  `open_drain` (one pod command for the whole list, so nothing is half-applied), `gpio_write` drives
  them, `gpio_read` reads any channel, `gpio_wait` polls for a level, `gpio_pulse` emits FPGA-timed
  pulses and `gpio_release` frees them. A channel already in use is refused with
  `PinConflictError: pin conflict: LA5 is in use by uart_rx; …` and a bias resistor that fights the
  mode with `PullConflictError: …`, both carrying the firmware's own message.
- **`la_timing`.** Re-measures a logic capture without re-capturing it: edge times, pulse widths,
  frequency, duty cycle and the delay between two channels (trigger pin → result pin).
- **Power profiles.** `measure_power(duration, efuse, rate_hz, points)` profiles a target-power rail
  and returns average/minimum/peak current, voltage, energy, charge and average power, plus an
  optional downsampled current/voltage trace. `rate_hz` in the result is the rate actually delivered
  and `adc_rate_hz` the sensor's configured conversion rate (ask for 100-500 Hz; it flattens near
  365 Hz). `power_profile_start` / `power_profile_stop` do the
  same around other tool calls; the running profile is session state (`status` shows
  `power_profile_running`) and is dropped on `disconnect`. Needs the pod's `power_profile`
  capability.
- `status` capabilities now include `la_pins`, `gpio_read`, `capture_trigger` and `power_profile`.

The `benchpod://wiring` resource was corrected: NRST uses the pod's reset pin (not LA3) and
LA7/LA8 carry pull-**down** resistors.
