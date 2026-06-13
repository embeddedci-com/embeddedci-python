# embeddedci-python

A monorepo of Python packages for driving an **EmbeddedCI BenchPod** — a
hardware-in-the-loop (HIL) device that powers a target board, flashes it over SWD,
captures its UART, and emulates/decodes an I2C sensor.

## Packages

| Package | Path | What it is |
| --- | --- | --- |
| [`embeddedci`](packages/embeddedci) | `packages/embeddedci/` | The BenchPod SDK and pytest plugin. `from embeddedci import benchpod`. |
| [`embeddedci-mcp`](packages/embeddedci-mcp) | `packages/embeddedci-mcp/` | An [MCP](https://modelcontextprotocol.io) server that exposes the SDK as tools, so AI agents can drive the bench. Thin consumer of `embeddedci`. |

The dependency direction is strictly **`embeddedci-mcp` → `embeddedci`** (never the
reverse). Both live here so an SDK change and the matching tool wrapper land in one
commit; each is published to PyPI on its own version tag.

## Layout

```
embeddedci-python/
├── pyproject.toml            # uv workspace root (not published)
├── packages/
│   ├── embeddedci/           # SDK + pytest plugin
│   └── embeddedci-mcp/       # MCP server (console script: embeddedci-mcp)
└── .github/workflows/        # CI for both packages; per-package publish tags
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
