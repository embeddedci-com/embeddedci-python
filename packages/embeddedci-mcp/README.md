# embeddedci-mcp

<!-- mcp-name: io.github.embeddedci-com/embeddedci-mcp -->

An [MCP](https://modelcontextprotocol.io) server that lets an AI agent drive an
**EmbeddedCI BenchPod** — a hardware-in-the-loop tester wired to a real board. Ask Claude (or any
MCP client) to flash a build, power-cycle the target, watch its UART, pretend to be the sensor it
expects, probe its I2C/CAN traffic or drive and measure analog signals, and the pod does it.

It is a thin layer over the [`embeddedci`](https://pypi.org/project/embeddedci/) SDK: every tool
maps to SDK calls, so anything an agent discovers interactively can become a pytest test.

## Requirements

- Python 3.10+ and [uv](https://docs.astral.sh/uv/) (for `uvx`), or `pip`.
- A BenchPod reachable over the network or the embeddedci.com cloud. (A USB connection works for
  status, LA voltage and power only: the STM32 pod's USB console has no JSON mode.)
- For `flash`: OpenOCD with the `cmsis_dap_tcp` backend (newer than 0.12.0 — e.g.
  `brew install --HEAD open-ocd`, or the xPack build) on the machine running the server. The
  firmware `file` is read from that machine too.

## Set up your client

### Claude Code

```bash
claude mcp add benchpod \
  -e BENCHPOD_CONNECTION=192.168.1.213 -e BENCHPOD_LA_VOLTAGE=3.3 \
  -- uvx embeddedci-mcp
```

`BENCHPOD_LA_VOLTAGE` is the board's I/O voltage, configured once here — use `1.8` for a 1V8
board. Without it the agent is told to call `set_la_voltage` before touching the LA bank.

### Claude Desktop / Cursor

`claude_desktop_config.json` (Claude Desktop) or `.cursor/mcp.json` (Cursor):

```json
{
  "mcpServers": {
    "benchpod": {
      "command": "uvx",
      "args": ["embeddedci-mcp"],
      "env": {
        "BENCHPOD_CONNECTION": "192.168.1.213",
        "BENCHPOD_LA_VOLTAGE": "3.3"
      }
    }
  }
}
```

### Codex

The Codex CLI keeps its MCP servers in `~/.codex/config.toml`. Add it with the CLI:

```bash
codex mcp add benchpod \
  --env BENCHPOD_CONNECTION=192.168.1.213 --env BENCHPOD_LA_VOLTAGE=3.3 \
  -- uvx embeddedci-mcp
```

…or write the entry yourself (`codex mcp list` shows what is configured):

```toml
# ~/.codex/config.toml
[mcp_servers.benchpod]
command = "uvx"
args = ["embeddedci-mcp"]
env = { BENCHPOD_CONNECTION = "192.168.1.213", BENCHPOD_LA_VOLTAGE = "3.3" }
```

### A pod in the cloud

Use the device name and an [API key](https://www.embeddedci.com/docs/benchpod-mcp) — the cloud
transport is included:

```json
"env": {
  "BENCHPOD_CONNECTION": "embeddedci:my-bench",
  "BENCHPOD_API_KEY": "eci_…",
  "BENCHPOD_LA_VOLTAGE": "3.3"
}
```

Or skip the API key: after `benchpod login`, the server reuses that session
(`~/.config/benchpod-cli/token.json`, refreshed and written back when it has expired) for both
`cloud_list_devices` and `connect("embeddedci:<name>")`. `BENCHPOD_API_KEY` wins when set, and
inside GitHub Actions the SDK uses OIDC instead.

`cloud_list_devices` lists the pods on your account and which are online, without connecting to
one; pass a result's `connection` to `connect`. The session token behind a cloud connection is
renewed before it expires, so a long session keeps working.

A cloud pod is shared, so `connect` takes an exclusive lease (waiting up to `--lease-wait`
seconds if another run holds it). The lease is released by `disconnect`, or after
`--idle-timeout` seconds without a tool call — the next call reconnects transparently, so an idle
chat never blocks CI on that pod.

## Options

| Flag | Environment | Default | |
| --- | --- | --- | --- |
| `--connection` | `BENCHPOD_CONNECTION` | — | host[:port], serial device, `usb`, `discover`, or `embeddedci:<device>` |
| `--la-voltage` | `BENCHPOD_LA_VOLTAGE` | — | LA I/O voltage (1.8 or 3.3) applied on connect |
| — | `BENCHPOD_API_KEY` | — | cloud pods and the waveform library (without it, cloud tools use the `benchpod login` session) |
| — | `BENCHPOD_API_BASE` | `https://www.embeddedci.com` | another embeddedci server |
| `--lease-wait` | — | `30` | cloud: seconds to wait for a busy pod |
| `--idle-timeout` | — | `600` | cloud: release the lease after this many idle seconds (0 = never) |
| `--timeout` | — | `30` | per-command device timeout |
| `--transport` | — | `stdio` | `stdio` or `http` |
| `--host` / `--port` | — | `127.0.0.1` / `8000` | HTTP bind address |
| `--auth-token` | `EMBEDDEDCI_MCP_TOKEN` | — | require `Authorization: Bearer <token>` (mandatory off loopback) |
| `--allowed-host` | — | — | Host header(s) to accept on a network bind (DNS-rebinding protection) |
| `--allow-unauthenticated` | — | off | serve a network address without a token (isolated networks only) |

### Serving a bench over HTTP

Run the server on the machine next to the pod, and point clients at it:

```bash
export EMBEDDEDCI_MCP_TOKEN=$(openssl rand -hex 32)
embeddedci-mcp --transport http --host 0.0.0.0 --connection 192.168.1.213 --la-voltage 3.3
```

```bash
claude mcp add --transport http benchpod http://bench-host:8000/mcp \
  --header "Authorization: Bearer $EMBEDDEDCI_MCP_TOKEN"
```

The server drives real hardware, so it refuses a non-loopback bind without a token. It holds one
pod connection shared by all HTTP clients, and serialises their tool calls.

## Tools

Every tool, grouped as in the server. Parameters in **bold** are required; the others are optional
and show their default when they have one (no default means "from the wiring profile", "keep the
current setting" or "not used"). Units are volts, amps, seconds and hertz. LA channel arguments take
a number (1-14) or a name from the wiring profile. Each tool's input schema carries the full
description, ranges and enums, so an agent sees more than this table.

### Connection

| Tool | Purpose | Parameters |
| --- | --- | --- |
| `connect` | Open a connection (closing any previous one) and report status and capabilities. | `connection`, `la_voltage`, `lease_wait=30` |
| `disconnect` | Close UART/CAN sessions and the connection, releasing a cloud lease. | none |
| `status` | Firmware, capabilities, LA voltage, open sessions and warnings (works when not connected). | none |
| `set_la_voltage` | Select the LA I/O voltage to match the DUT (1.8 V needs a rev3 pod). | **`voltage`** |

### Cloud (embeddedci.com)

| Tool | Purpose | Parameters |
| --- | --- | --- |
| `cloud_list_devices` | The pods on your embeddedci.com organization and whether each is online, without connecting. | none |

### Wiring profile

| Tool | Purpose | Parameters |
| --- | --- | --- |
| `wiring` | Which DUT signal is on which LA channel, plus the power rail, UART baud and SWD target. | none |
| `set_wiring` | Replace the wiring profile for this connection, or store it on embeddedci.com. | **`profile`**, `save=false` |

### Power

| Tool | Purpose | Parameters |
| --- | --- | --- |
| `power_on` | Switch the target's power rail on (optionally scheduled pod-side). | `efuse`, `delay` |
| `power_off` | Switch the target's power rail off. | `efuse`, `delay` |
| `power_status` | Both eFuse rails: on/off, tripped, bus voltage and current, and the pod's own draw. | none |
| `reset_target` | Drive the DUT's reset line from the pod's reset pin. | `action=pulse`, `pulse=0.1` |

### Power profiles

| Tool | Purpose | Parameters |
| --- | --- | --- |
| `measure_power` | Sample supply current and voltage for a window: average, min, peak, energy and charge. | **`duration`**, `efuse`, `rate_hz=500`, `points=0` |
| `power_profile_start` | Start sampling the rail in the background while other tools run. | `efuse`, `rate_hz=500`, `max_duration=60` |
| `power_profile_stop` | Stop the running profile and return its statistics (and a trace). | `points=0` |

### Flash

| Tool | Purpose | Parameters |
| --- | --- | --- |
| `flash` | Program the DUT over SWD through the pod's CMSIS-DAP probe (OpenOCD runs next to the server). | `swclk`, `swdio`, `target`, `file`, `nreset`, `load_address`, `target_power`, `verify=true`, `reset=true`, `connect_under_reset`, `extra_configs`, `extra_args`, `timeout=300`, `connect_attempts=5` |

### SPI flash and SPI devices

| Tool | Purpose | Parameters |
| --- | --- | --- |
| `spi_flash_info` | Read the JEDEC ID and size of the SPI NOR flash on the SPI pins. | `sck`, `mosi`, `miso`, `cs`, `hz=1000000`, `mode=0` |
| `spi_flash_program` | Erase, write and verify an image in the SPI NOR flash. | **`file`**, `addr=0`, `erase=true`, `verify=true`, `hold_reset=false`, `sck`, `mosi`, `miso`, `cs`, `hz=6000000`, `mode=0` |
| `spi_flash_read` | Read flash bytes to a file, or up to 4096 bytes as hex. | **`addr`**, **`length`**, `file`, `sck`, `mosi`, `miso`, `cs`, `hz=6000000`, `mode=0` |
| `spi_transfer` | One full-duplex SPI transaction with any device on the SPI pins. | **`tx_hex`**, `sck`, `mosi`, `miso`, `cs`, `hz=1000000`, `mode=0` |

### UART

| Tool | Purpose | Parameters |
| --- | --- | --- |
| `capture_uart` | Record the DUT's UART for a window, or until a regex matches. | **`duration`**, `rx`, `tx`, `baud`, `until_regex` |
| `power_cycle_and_capture` | Power-cycle the target and capture UART across the boot. | `rx`, `tx`, `efuse`, `delay=1`, `duration=4`, `baud`, `until_regex`, `off_settle=0.3` |
| `uart_open` | Start buffering the DUT's UART in the background. | `rx`, `tx`, `baud` |
| `uart_write` | Send text to the DUT through the open UART session. | **`text`**, `line_ending=lf` |
| `uart_read` | Return output received since the last read, optionally waiting for a match. | `until_regex`, `timeout=2` |
| `uart_close` | Stop the background UART session. | none |

### Emulated I2C sensor

| Tool | Purpose | Parameters |
| --- | --- | --- |
| `enable_i2c_sensor` | Make the pod act as an I2C sensor for the DUT to read (engage pull-ups first): `sensor` is `bmp280`, `bme280`, `sht4x` or `mpu6050`. | `sda`, `scl`, `address`, `temperature_c`, `pressure_pa`, `sensor="bmp280"`, `values` (readings by key, e.g. `{"humidity_pct": 55}`) |
| `set_i2c_sensor` | Change what the emulated sensor reports; the reply lists every reading. | `temperature_c`, `pressure_pa`, `values` |
| `i2c_sensor_types` | The models the pod emulates, with their addresses and readings (key, unit, range, default). | none |
| `disable_i2c_sensor` | Disarm the emulated sensor. | none |
| `i2c_sensor_status` | Sensor state and bus activity counters: did the DUT talk to it? | none |
| `i2c_sensor_regs` | Read the emulated sensor's register image. | `start=0`, `length=256` |
| `i2c_sensor_capture` | Capture and decode the sensor's I2C bus into a transaction trace. | `samples=4096`, `sample_rate_hz=500000`, `address`, `register` |

### Emulated GPS receiver

NMEA sentences (u-blox style: RMC VTG GGA GSA GSV GLL) on one LA channel for the DUT's UART, from
the pod's second UART, so `uart_open` keeps the console. Needs gateware v48 (`gps` capability).

| Tool | Purpose | Parameters |
| --- | --- | --- |
| `enable_gps` | Start the receiver on `tx`, the channel wired to the DUT's RX. The position moves along `course_deg` at `speed_kmh` between fixes. | `tx`, `baud=9600`, `rate_hz=1`, `sentences`, `latitude_deg`, `longitude_deg`, `altitude_m`, `speed_kmh`, `course_deg`, `satellites`, `hdop`, `fix`, `utc` (default now) |
| `set_gps` | Change the fix (any subset); `fix=0` drops the lock. | `latitude_deg`, `longitude_deg`, `altitude_m`, `speed_kmh`, `course_deg`, `satellites`, `hdop`, `fix`, `utc` |
| `disable_gps` | Stop the receiver and release its pin. | none |
| `gps_status` | The session (tx, baud, rate, epochs, overruns) and the fix it prints. | none |

### Pull resistors

| Tool | Purpose | Parameters |
| --- | --- | --- |
| `set_pull` | Engage or release the bias resistors: LA1-LA6 pull up, LA7/LA8 pull down. | **`las`**, **`enabled`** |
| `pull_status` | State, direction and value of the bias resistor on LA1-LA8. | none |

### GPIO on the LA pins

| Tool | Purpose | Parameters |
| --- | --- | --- |
| `la_pins` | What owns each of the 14 LA channels, with GPIO mode, level and bias resistor. | none |
| `gpio_mode` | Claim LA channels as GPIO (input, output or open drain). | **`la`**, `mode=output`, `level` |
| `gpio_write` | Set the level of GPIO output and open-drain channels. | **`la`**, **`level`** |
| `gpio_read` | The live level of LA channels (all 14 when `la` is omitted). | `la` |
| `gpio_wait` | Wait until an LA channel reads a level (`reached: false` on timeout). | **`la`**, `level=1`, `timeout=5` |
| `gpio_pulse` | FPGA-timed pulses on an LA channel: `width` 4 us to 65.535 ms, `count` up to 65535. | **`la`**, **`width`**, `count=1` |
| `gpio_release` | Return channels to high-Z, watched by captures. | `la` |

### Analog

| Tool | Purpose | Parameters |
| --- | --- | --- |
| `analog_path` | Set every analog mux and relay for a named path in one step. | **`path`** |
| `dac_output` | Route a DAC output and drive a calibrated DC voltage on it. | **`path`**, `volts` |
| `current_out` | Hold a current on the 4-20 mA output (J9), in amps, or read its range. | `current` |
| `adc_read` | One calibrated reading: front SMA, DAC loopbacks or the 4-20 mA input (J8). | `source=ext` |
| `calibration` | The stored calibration of the 4-20 mA input and the fit it uses. | none |
| `calibrate` | Measure and store the 4-20 mA input's offset, or clear it. | `source=current_in`, `clear=false` |

### Capture and decode

| Tool | Purpose | Parameters |
| --- | --- | --- |
| `capture_adc` | Capture the ADC: calibrated statistics, dominant frequency and a min/max envelope. | `samples=4096`, `sample_rate_hz`, `source`, `points=200`, `trigger_la`, `trigger_edge=rising`, `trigger_timeout=10` |
| `capture_la` | Capture all 14 LA channels: levels, edge count, first edge and frequency per channel. | `samples=4096`, `sample_rate_hz`, `stop_dac_after`, `trigger_la`, `trigger_edge=rising`, `trigger_timeout=10` |
| `capture_correlated` | ADC and LA from one hardware trigger, on one timebase. | `adc_samples=4096`, `adc_sample_rate_hz`, `la_samples=4096`, `la_sample_rate_hz`, `stop_dac_after`, `points=200`, `trigger_la`, `trigger_edge=rising`, `trigger_timeout=10`, `source` |
| `decode_la` | Decode I2C, UART or SPI from the last LA capture (or a new one). | **`protocol`**, `sda`, `scl`, `rx`, `baud`, `sclk`, `mosi`, `miso`, `cs`, `mode=0`, `capture=last`, `samples=16384`, `sample_rate_hz=1000000`, `max_items=200` |
| `la_timing` | Edge timestamps, pulse widths, frequency, duty cycle and channel-to-channel delay from the last capture. | **`la`**, `edge=rising`, `to_la`, `to_edge=rising`, `after=0`, `max_edges=100` |

### DAC

| Tool | Purpose | Parameters |
| --- | --- | --- |
| `generate` | Drive a sine, square or sawtooth on a DAC output (volts, or amps on `current_out`). | **`waveform`**, **`freq_hz`**, **`amplitude`**, `offset`, `dac_path=5v`, `duration`, `sample_rate_hz`, `on_capture=false`, `route=true` |
| `dac_stop` | Stop any DAC output: generator, replay or control loop. | none |
| `replay` | Loop your own samples, or the last ADC capture, out of the DAC. | `volts`, `from_last_capture=false`, `dac_path=5v`, `mapping=faithful`, `sample_rate_hz`, `fault`, `on_capture=false`, `route=true`, `switch_image=true` |
| `list_waveforms` | The organization's cloud waveform library. | none |
| `replay_waveform` | Loop a cloud-library waveform out of the DAC. | **`waveform_id`**, `dac_path`, `mapping=faithful`, `sample_rate_hz`, `window_start=0`, `window_len=0`, `target_samples=0`, `fault`, `on_capture=false`, `switch_image=true` |
| `save_capture_as_recording` | Save the last ADC capture to the cloud waveform library. | **`name`**, `full_scale_v` |

### Control loop

| Tool | Purpose | Parameters |
| --- | --- | --- |
| `control_loop` | Run a tabulated transfer function in the FPGA (for example a solar panel I-V curve). | `curve`, `voc_code`, `sharpness=4`, `points=256`, `k=8192`, `vmin=0`, `vmax=65535`, `tick_div=64`, `source`, `input_code=0`, `step=0`, `input_map`, `switch_image=true` |
| `loop_input` | Re-target the running loop's input without re-arming. | `input_code`, `source`, `step` |
| `loop_probe` | The running loop's live operating point. | none |
| `fpga_image` | Switch the FPGA to the `loop` or `deep_replay` gateware image (~2-3 s). | **`image`** |

### CAN

| Tool | Purpose | Parameters |
| --- | --- | --- |
| `can_open` | Bring up the pod's CAN interface (normal, loopback or listen-only). | `bitrate=500000`, `mode=normal`, `term=false` |
| `can_write` | Queue one classic CAN frame. | **`can_id`**, `data`, `ext=false`, `rtr=false` |
| `can_read` | Read received frames, or wait for one id. | `timeout=0`, `can_id`, `max_frames=32` |
| `can_respond` | Answer a CAN id from the pod firmware (ECU simulation), or clear all rules. | `match_id`, `reply_id`, `reply_data`, `match_ext=false`, `reply_ext=false`, `clear=false` |
| `can_status` | Link state: mode, bitrate, error counters, bus-off. | none |
| `can_close` | Clear responder rules and stop CAN. | none |

### Other

| Tool | Purpose | Parameters |
| --- | --- | --- |
| `la_step` | Step pulses for step/dir motor drivers: `delay` is half the step period (4 us to 65.535 ms), `steps` up to 65535. | **`la`**, **`steps`**, **`delay`**, `dir_la`, `direction=0` |
| `command` | Raw firmware JSON command, for anything no tool covers. | **`request`** |

Resources: `benchpod://wiring` (the connected bench's own wiring profile, then LA channels, bias
resistors and analog paths) and `benchpod://help` (the server instructions).

## How it behaves

- **Instructions.** The server sends usage instructions at initialisation — session start, typical
  flows, units, error contract — so the agent knows to `connect` and `set_la_voltage` before
  anything else.
- **Typed, structured results.** Every tool has an input schema with enums and ranges (paths,
  sources, LA channels 1-14, eFuse 1/2, …) and an output schema; results come back as structured
  content. Units are volts, seconds and hertz.
- **Errors.** A tool that cannot do what was asked fails with an MCP tool error whose message names
  the cause, e.g. `FirmwareError: la voltage not set` or `NotConnectedError: …`. A completed
  operation with a negative outcome is a normal result: `flash` returns `ok: false` with its logs,
  a UART capture `matched: false`.
- **Refusals.** When the pod or the server refuses a command, the error names the kind and ends
  with a one-line hint on what to do: `PodLockedError` (the pod's LAN policy keeps the command for
  the cloud or USB), `PodLeasedError` (a cloud job holds the pod; the message names who and for
  how long), `PodBusyError` (a capture or upload is running), `PermissionDeniedError` (the
  credential lacks the right, for example an API key without the `benchpod:admin` scope) and
  `TransportTimeout` (the pod did not answer). A dropped link is a `ConnectionClosedError`.
  `UnsupportedFeatureError` means the pod cannot do it at all: its firmware or gateware lacks the
  feature, or it is the digital-only board without the analog front end; the hint says which.
  `status` also warns when a cloud job holds the pod or the LAN policy is locked.
- **Capabilities.** `connect` and `status` report what the pod can do as flags, so an agent can
  check before it calls a tool: for example `la_pins`, `capture_trigger`, `power_profile`,
  `nrst_pin`, `can`, `calibrate`, `current_out`, `pod_current` and `analog` (false on the
  digital-only board), plus the pod's policy and cloud settings (`dac_limits`, `flash_kb`,
  `ota_sig`, `sig_policy`, `lan_policy`, `tunnel_max_tier`, `lease_state`, `cloud_ca`,
  `cloud_proxy`).
- **Wiring profile.** `wiring` is the bench's map of DUT signal → LA channel, plus the target-power
  rail, the UART baud and the SWD target. Omitted channel / baud / rail / SWD arguments come from
  it, and channel arguments also accept its names (`trigger_la: "READY"`, `rx: "uart_rx"`), so an
  agent that read `wiring` once can call `flash()`, `uart_open()` or `power_on()` with no
  pin numbers at all. `set_wiring` replaces it for the connection, or stores it on embeddedci.com
  with `save: true`.
- **Agent-sized captures.** `capture_adc` returns calibrated statistics, the dominant frequency and a
  min/max envelope; `capture_la` returns per-channel levels, edges and frequencies. The last captures
  stay in the session, so `decode_la`, `replay(from_last_capture=true)` and
  `save_capture_as_recording` don't capture again.
- **Triggered captures.** `capture_adc`, `capture_la` and `capture_correlated` take `trigger_la`
  (+ `trigger_edge` rising/falling/high/low and `trigger_timeout`) so sampling starts on an event —
  t = 0 is the trigger — and the summary echoes it as `trigger: "LA9 rising"`. A condition that
  never happens fails with `TriggerTimeout: …`.
- **Pins and GPIO.** Each LA channel has one owner at a time: none, GPIO, a UART proxy, SWD, the
  emulated sensor or a step train. `la_pins` shows the table, `gpio_mode` / `gpio_write` /
  `gpio_read` / `gpio_wait` / `gpio_pulse` drive and watch channels, `gpio_release` frees them. A
  second claim fails with `PinConflictError: pin conflict: LA5 is in use by uart_rx; …` naming the
  owner and how to free it — so the agent releases GPIO before opening a UART session on that
  channel. Captures observe all 14 channels whatever owns them.
- **Power profiles.** `measure_power(duration)` reports average, minimum and peak current, voltage,
  energy and charge. Every sample is timestamped and the integrals run over those timestamps, so
  energy is integrated rather than estimated. `rate_hz` (100-500, default 500) tracks the request to
  ~200 Hz and then flattens near 365 Hz — the result reports the rate actually delivered in
  `rate_hz` and the sensor's configured rate in `adc_rate_hz`. Optional downsampled trace (`points`). `power_profile_start` / `power_profile_stop` bracket other
  tool calls; the running profile lives on the session (`status` reports it) and is dropped on
  `disconnect`.
- **Sessions.** `uart_open` buffers the DUT's console in the background (open it before
  `power_on`, then `uart_read` / `uart_write`); `can_open` keeps a CAN bus open across calls.
- **Gateware images.** The pod's FPGA runs either the `loop` image (`control_loop`) or the
  `deep_replay` image (replays longer than 2048 samples). `control_loop`, `replay` and
  `replay_waveform` switch automatically (~3 s) and report it as `switched_image`;
  `switch_image: false` fails instead. A switch resets the FPGA — a running DAC output, UART session
  or I2C sensor emulation stops — and the server instructions tell the agent to start those after it.
  `fpga_image` switches explicitly.
- **Non-blocking.** Tools run on worker threads under one device lock: a 5-minute flash doesn't
  freeze the server, sends progress notifications, and concurrent calls can't interleave commands.
- **Annotations.** Read-only tools (`status`, `power_status`, `adc_read`, …) are marked so clients
  can auto-approve them; tools that power, flash or drive voltages are marked destructive.

## Example

> Connect to the bench, flash `build/app.elf` to the STM32F4 (SWCLK on LA11, SWDIO on LA12,
> reset wired), then power-cycle it and tell me whether it reaches `APP_OK` on the UART (DUT TX on
> LA5, RX on LA4).

```
connect()                                   # BENCHPOD_CONNECTION + BENCHPOD_LA_VOLTAGE
flash(swclk=11, swdio=12, nreset=true, target="target/stm32f4x.cfg", file="build/app.elf", target_power=1)
power_cycle_and_capture(rx=5, tx=4, delay=1.0, duration=5.0, until_regex="APP_OK")
```

With a wiring profile stored for the bench, the same run needs no pin numbers:

```
connect()
wiring()                                    # SWCLK on LA11, DUT TX on LA5, rail eFuse 1, …
flash(file="build/app.elf")
power_cycle_and_capture(delay=1.0, duration=5.0, until_regex="APP_OK")
measure_power(duration=2.0, points=100)     # what the firmware draws once it is up
```

## Stability

2.x freezes the tool names, input schemas and annotations (`tests/tools_surface.json`; CI fails on
any unreviewed change): tools and optional parameters may be added, nothing is renamed or removed
within a major version. See [CHANGELOG.md](CHANGELOG.md) for migrating from 0.1.

## Publishing to the MCP Registry

`server.json` describes this package for the [MCP Registry](https://registry.modelcontextprotocol.io).
The `embeddedci-mcp-v*` release workflow publishes it after the PyPI upload (which is what makes
`uvx embeddedci-mcp` work), logged in with GitHub OIDC. Bump both `version` fields in `server.json`
together with `pyproject.toml`: the release fails when they differ, and so does CI.

To publish by hand instead:

```bash
mcp-publisher login github
mcp-publisher publish
```

The registry verifies PyPI ownership through the `mcp-name` comment at the top of this README.

## Development

```bash
pip install -e "../embeddedci[dev]" -e ".[dev]"
pytest
```
