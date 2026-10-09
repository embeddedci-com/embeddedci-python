# Changelog — `embeddedci-mcp`

## 2.9.0

- Requires `embeddedci` 2.9.0 (pin raised).
- More emulated sensors: `enable_i2c_sensor` takes `sensor` (`bmp280`, `bme280`, `sht4x`,
  `mpu6050`) and `values` (readings by key); `set_i2c_sensor` takes `values`; new
  `i2c_sensor_types` lists the models and their readings.
- Emulated GPS receiver: `enable_gps`, `set_gps`, `disable_gps`, `gps_status` (gateware v48).
- `connect`/`status` capabilities: `sensor_types`, `gps`.

## 2.8.0

- Requires `embeddedci` 2.8.0 (pin raised).
- `UnsupportedFeatureError` gets a hint too: the pod's firmware or gateware lacks the feature, or
  it is the digital-only board without the analog front end.
- The README has a per-tool reference with every parameter and default, the typed refusals and
  the capability flags; a test fails when a tool or parameter is missing from it.

## 2.7.0

- Requires `embeddedci` 2.7.0 (pin raised), which brings the typed refusals and transport errors
  the hints below use.
- A refused command names its kind and what to do: `PodLockedError` (the pod's LAN policy keeps it
  for the cloud or USB), `PodLeasedError` (a cloud job holds the pod, with who and for how long),
  `PodBusyError`, `PermissionDeniedError` (pod `forbidden:` or a server 403) and
  `TransportTimeout`, each followed by a one-line hint.
- `status` warns when a cloud job holds the pod (over the LAN) and when the LAN policy is locked.
- `connect` and `status` report `analog`, `dac_limits`, `flash_kb`, `ota_sig`, `sig_policy`,
  `lan_policy`, `tunnel_max_tier`, `lease_state`, `cloud_ca` and `cloud_proxy`.
- The SDK's typed transport errors reach the agent: a pod that does not answer is a
  `TransportTimeout`, a dropped connection a `ConnectionClosedError`.
- `la_step`: `delay` is described as half the step period (it was "seconds between step pulses",
  which is twice that). `la_step` `steps` and `gpio_pulse` `count` are capped at 65535 and
  `delay`/`width` at 4 µs..65.535 ms, the pod's 16-bit limits (the schemas allowed 10,000,000
  steps and 10 s, which the pod refused).

## 2.6.0

- Over the cloud, `replay` with your own samples and SPI flash staging work above about 1.5 KB,
  and the session token no longer appears in URLs (both from `embeddedci` 2.6.0).
- `power_status` returns `pod`: the pod's own 5 V draw and the USB input total, on boards that
  measure it; null otherwise. `connect` and `status` report the `pod_current` capability.
  Needs `embeddedci` 2.6.0 (pin raised).

## 2.5.0

- `connect` and `status` report the `can` capability (classic CAN on CAN+/CAN-, the `can_*`
  tools). Firmware 3.5.1 announces it. Needs `embeddedci` 2.5.0.

## 2.4.0

- **Breaking:** the 4-20 mA input (J8) is now `current_in` as an ADC source and analog path,
  following firmware 3.4.0; the old name is gone. `adc_read` on it also returns `current` in amps.
- New `calibrate` and `calibration` tools: the pod measures the offset of its 4-20 mA input
  (`current_in`, J8 disconnected) and stores it; `calibration` reads it back and `calibrate(clear=true)`
  removes it. `adc_read` on `current_in` reports the offset it took out. Needs firmware 3.4.0 and
  the matching `embeddedci` release.
- New `current_out` tool: hold a current on the 4-20 mA output (terminal J9), in amps, or read the
  range the output can do. The pod converts and refuses a current outside its range. The output is
  loop powered (it needs an external floating supply) and shares the DAC with the voltage outputs. Needs firmware 3.4.0 and
  `embeddedci` 2.4.0.
- `generate`, `replay` and `replay_waveform` take `dac_path="current_out"` to play a waveform as a
  current on the 4-20 mA output, with levels in amps.
- `capture_adc(source="current_in")` summarises the loop current in amps (new `unit` field in the
  result), and `capture_correlated` takes `source` for the same next to the logic channels, and that capture replays as a current or saves as a recording in mA. `list_waveforms`
  reports each entry's `unit` and `dac_path`. `status` and `connect` report the `calibrate` and
  `current_out` capabilities.

## 2.3.0

- New SPI tools: `spi_flash_info` (JEDEC ID and size), `spi_flash_program` (erase, write and
  verify an image), `spi_flash_read` and `spi_transfer` (raw full-duplex bytes). They use the
  pod's SPI master on the LA pins (firmware 3.3+). Requires `embeddedci>=2.3`.

## 2.2.0

- New `cloud_list_devices`: the pods on your embeddedci.com account and whether each is online,
  without connecting to one. It authenticates with `BENCHPOD_API_KEY`, else the `benchpod login`
  session in `~/.config/benchpod-cli/token.json` (refreshed and saved back when expired). Tools
  that talk to embeddedci.com rather than a pod are named `cloud_*`.
- `connect("embeddedci:<name>")` also works with just `benchpod login`: without `BENCHPOD_API_KEY`
  (and outside GitHub Actions) it authenticates with that session. Requires `embeddedci>=2.2`,
  which also renews the cloud session token during long sessions and reports offline, unknown and
  busy pods clearly.
- Fix: `connect("discover")` failed with "needs the 'zeroconf' package". The server now installs
  `embeddedci[discovery]`.

## 2.1.1

- The README says LA channels 1-14; 2.1.0 still said 12.
- Requires `embeddedci>=2.1`: with 2.0 installed, LA13/LA14 were refused before reaching the pod.

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
