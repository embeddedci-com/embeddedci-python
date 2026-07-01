# embeddedci-python

A monorepo of Python packages for driving an **EmbeddedCI BenchPod** — a
hardware-in-the-loop (HIL) device that powers a target board, flashes it over SWD,
captures its UART, and emulates/decodes an I2C sensor.

## Packages

| Package | Path | What it is |
| --- | --- | --- |
| [`embeddedci`](packages/embeddedci) | `packages/embeddedci/` | The BenchPod SDK and pytest plugin. `from embeddedci import benchpod`. Connects over wifi, serial, or the **cloud** (`embeddedci:<device-name>`) to drive a remote pod from a GitHub Action via OIDC — see the [package README](packages/embeddedci/README.md#running-in-github-actions-cloud). |
| [`embeddedci-mcp`](packages/embeddedci-mcp) | `packages/embeddedci-mcp/` | An [MCP](https://modelcontextprotocol.io) server that exposes the SDK as tools, so AI agents can drive the bench. Thin consumer of `embeddedci`. |
| [`embeddedci-openhtf`](packages/embeddedci-openhtf) | `packages/embeddedci-openhtf/` | An [OpenHTF](https://www.openhtf.com/) plug + phase helpers for driving a pod **directly over TCP/serial** (no cloud) from an OpenHTF test. Thin consumer of `embeddedci`. |

The dependency direction is strictly **`embeddedci-mcp` → `embeddedci`** and
**`embeddedci-openhtf` → `embeddedci`** (never the reverse). All live here so an SDK
change and the matching wrapper land in one commit; each is published to PyPI on its
own version tag.

## Layout

```
embeddedci-python/
├── pyproject.toml            # uv workspace root (not published)
├── packages/
│   ├── embeddedci/           # SDK + pytest plugin
│   ├── embeddedci-mcp/       # MCP server (console script: embeddedci-mcp)
│   └── embeddedci-openhtf/   # OpenHTF plug (direct TCP/serial, no cloud)
└── .github/workflows/        # CI for all packages; per-package publish tags
```

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e "packages/embeddedci[dev]"
pip install -e "packages/embeddedci-mcp[dev]"
pytest packages/embeddedci packages/embeddedci-mcp
```

`embeddedci-mcp`'s editable install pulls `embeddedci` from the sibling source tree.

## Running the MCP server

```bash
# launched by an MCP client (Claude Desktop / Cursor / Claude Code) over stdio,
# or directly for a remote bench over HTTP:
embeddedci-mcp --transport stdio
embeddedci-mcp --transport http --port 8000
```

See [`packages/embeddedci-mcp/README.md`](packages/embeddedci-mcp/README.md) for client
configuration and the full tool list.
