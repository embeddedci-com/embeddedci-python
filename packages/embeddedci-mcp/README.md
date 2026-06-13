# embeddedci-mcp

An [MCP](https://modelcontextprotocol.io) server that exposes the
[`embeddedci`](https://github.com/embeddedci-com/embeddedci-python/tree/main/packages/embeddedci)
BenchPod SDK as tools, so an AI agent can drive a real hardware-in-the-loop bench:
power a target board, flash it over SWD, capture its UART, and emulate/decode an
I2C sensor.

It's a thin consumer of the SDK — every tool maps directly to a
`embeddedci.benchpod.BenchPod` method.

## Install

```bash
pip install embeddedci-mcp        # pulls embeddedci from PyPI
# or run without installing:
uvx embeddedci-mcp --help
```

For local development from this repo, see the
[workspace README](https://github.com/embeddedci-com/embeddedci-python#development).

## Run

```bash
# stdio (launched by an MCP client as a subprocess — the usual case):
embeddedci-mcp --transport stdio

# streamable HTTP (for a remote bench):
embeddedci-mcp --transport http --host 0.0.0.0 --port 8000

# preset a default connection so the `connect` tool needs no argument:
embeddedci-mcp --connection /dev/tty.usbserial-0001
embeddedci-mcp --connection 192.168.1.213        # wifi/TCP, default port 8080
```

The connection can also come from the `BENCHPOD_CONNECTION` environment variable.

## Client configuration

### Claude Desktop / Cursor (`mcp.json` / `claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "benchpod": {
      "command": "uvx",
      "args": ["embeddedci-mcp"],
      "env": { "BENCHPOD_CONNECTION": "192.168.1.213" }
    }
  }
}
```

Use `"command": "embeddedci-mcp"` instead if it's installed on `PATH`.

### Claude Code

```bash
claude mcp add benchpod -- uvx embeddedci-mcp
```

## Tools

| Group | Tools |
| --- | --- |
| Lifecycle / status | `connect`, `disconnect`, `ping`, `status` |
| Power | `power_on`, `power_off`, `target_power`, `target_status` |
| Flash | `flash` |
| UART | `capture_uart`, `power_cycle_and_capture` |
| I2C sensor | `enable_i2c_sensor`, `set_i2c_sensor`, `disable_i2c_sensor`, `i2c_sensor_status`, `i2c_sensor_regs`, `i2c_sensor_la_decoded`, `i2c_read_register` |
| Pull-ups | `enable_pullup`, `disable_pullup`, `pullup_status` |
| Low-level | `command`, `gpio_set`, `capture_adc`, `signal_generate`, `measure` |

Device/firmware failures come back as `{"ok": false, "error": ..., "error_type": ...}`
rather than raising, so the agent can reason about them.

### Resources

- `benchpod://wiring` — the default LA channel → DUT signal pin map and eFuse table.
- `benchpod://help` — the canonical HIL workflow order.

## Example agent flow

1. `connect("192.168.1.213")`
2. `flash(swclk=11, swdio=12, nreset=3, target="target/stm32f4x.cfg", file="app.elf", target_power=1)`
3. `enable_pullup([1, 2])` then `enable_i2c_sensor(sda=2, scl=1, temperature_c=22.5, pressure_pa=101000)`
4. `power_cycle_and_capture(rx=5, tx=4, delay=1.5, duration=6.0, until_regex="APP_OK")`
5. `i2c_sensor_status()` / `i2c_read_register(address=0x76, register=0xD0)` to confirm the DUT probed the sensor.

## Publishing to the MCP Registry

`server.json` is starter metadata for the
[Official MCP Registry](https://registry.modelcontextprotocol.io) (currently in
preview). Ship the package to PyPI first (that's what makes `uvx embeddedci-mcp`
work), then publish the registry entry once the name is stable:

```bash
mcp-publisher publish      # GitHub-authenticated; reads server.json
```
