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

A cloud pod is shared, so `connect` takes an exclusive lease (waiting up to `--lease-wait`
seconds if another run holds it). The lease is released by `disconnect`, or after
`--idle-timeout` seconds without a tool call — the next call reconnects transparently, so an idle
chat never blocks CI on that pod.

## Options

| Flag | Environment | Default | |
| --- | --- | --- | --- |
| `--connection` | `BENCHPOD_CONNECTION` | — | host[:port], serial device, `usb`, `discover`, or `embeddedci:<device>` |
| `--la-voltage` | `BENCHPOD_LA_VOLTAGE` | — | LA I/O voltage (1.8 or 3.3) applied on connect |
| — | `BENCHPOD_API_KEY` | — | cloud pods and the waveform library |
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

| Group | Tools |
| --- | --- |
| Connection | `connect`, `disconnect`, `status`, `set_la_voltage` |
| Power | `power_on`, `power_off`, `power_status`, `reset_target` |
| Flash | `flash` |
| UART | `capture_uart`, `power_cycle_and_capture`, `uart_open`, `uart_write`, `uart_read`, `uart_close` |
| Emulated I2C sensor | `enable_i2c_sensor`, `set_i2c_sensor`, `disable_i2c_sensor`, `i2c_sensor_status`, `i2c_sensor_regs`, `i2c_sensor_capture` |
| Pull resistors | `set_pull`, `pull_status` |
| Analog | `analog_path`, `dac_output`, `adc_read` |
| Capture + decode | `capture_adc`, `capture_la`, `capture_correlated`, `decode_la` |
| DAC | `generate`, `dac_stop`, `replay`, `list_waveforms`, `replay_waveform`, `save_capture_as_recording` |
| Control loop | `control_loop`, `loop_input`, `loop_probe`, `fpga_image` |
| CAN | `can_open`, `can_write`, `can_read`, `can_respond`, `can_status`, `can_close` |
| Other | `la_step`, `command` (raw firmware escape hatch) |

Resources: `benchpod://wiring` (LA channels, bias resistors, analog paths, an example bench) and
`benchpod://help` (the server instructions).

## How it behaves

- **Instructions.** The server sends usage instructions at initialisation — session start, typical
  flows, units, error contract — so the agent knows to `connect` and `set_la_voltage` before
  anything else.
- **Typed, structured results.** Every tool has an input schema with enums and ranges (paths,
  sources, LA channels 1-12, eFuse 1/2, …) and an output schema; results come back as structured
  content. Units are volts, seconds and hertz.
- **Errors.** A tool that cannot do what was asked fails with an MCP tool error whose message names
  the cause, e.g. `FirmwareError: la voltage not set` or `NotConnectedError: …`. A completed
  operation with a negative outcome is a normal result: `flash` returns `ok: false` with its logs,
  a UART capture `matched: false`.
- **Agent-sized captures.** `capture_adc` returns calibrated statistics, the dominant frequency and a
  min/max envelope; `capture_la` returns per-channel levels, edges and frequencies. The last captures
  stay in the session, so `decode_la`, `replay(from_last_capture=true)` and
  `save_capture_as_recording` don't capture again.
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

## Stability

2.x freezes the tool names, input schemas and annotations (`tests/tools_surface.json`; CI fails on
any unreviewed change): tools and optional parameters may be added, nothing is renamed or removed
within a major version. See [CHANGELOG.md](CHANGELOG.md) for migrating from 0.1.

## Publishing to the MCP Registry

`server.json` describes this package for the [MCP Registry](https://registry.modelcontextprotocol.io).
After the PyPI release (which is what makes `uvx embeddedci-mcp` work):

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
