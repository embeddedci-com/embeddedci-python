# Changelog — `embeddedci-mcp`

## 2.0.0

Built on `embeddedci` 2.0. From this release the tool names, input schemas and annotations are
frozen for 2.x (`tests/tools_surface.json`).

### Behaviour

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

The `benchpod://wiring` resource was corrected: NRST uses the pod's reset pin (not LA3) and
LA7/LA8 carry pull-**down** resistors.
