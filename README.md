# embeddedci-python

Python packages for driving an **EmbeddedCI BenchPod** — a hardware-in-the-loop (HIL) tester
that powers a target board, flashes it over SWD, talks to its UART, I2C and CAN, and drives and
measures its analog and logic signals — from pytest, from an AI agent, or from OpenHTF.

## Packages

| Package | Path | What it is |
| --- | --- | --- |
| [`embeddedci`](packages/embeddedci) | `packages/embeddedci/` | The BenchPod SDK and pytest plugin: `from embeddedci.benchpod import BenchPod`. Connects over the network, USB or the cloud (`embeddedci:<device-name>`, with an API key or GitHub Actions OIDC). |
| [`embeddedci-mcp`](packages/embeddedci-mcp) | `packages/embeddedci-mcp/` | An [MCP](https://modelcontextprotocol.io) server exposing the SDK as typed tools, so AI agents can drive the bench. |
| [`embeddedci-openhtf`](packages/embeddedci-openhtf) | `packages/embeddedci-openhtf/` | An [OpenHTF](https://www.openhtf.com/) plug and phase helpers for driving a pod directly over TCP or serial. |

The dependency direction is strictly **`embeddedci-mcp` → `embeddedci`** and
**`embeddedci-openhtf` → `embeddedci`**. All three live here so an SDK change and the matching
wrapper land in one commit; each is published to PyPI on its own tag.

## Versioning and stability

All three packages are on the **2.x** line, the first with a stability promise:

- `embeddedci`: everything in `embeddedci.benchpod.__all__` follows semantic versioning. Units are
  volts, seconds and hertz; invalid arguments raise `ValueError`; device state comes back typed.
  `tests/api_surface.json` snapshots the public surface.
- `embeddedci-mcp`: tool names, input schemas and annotations are frozen for 2.x
  (`tests/tools_surface.json`).
- Consumers depend on `embeddedci>=2.0,<3`.

The surface snapshot tests fail on any change. When a change is intended and compatible (an
addition), refresh them deliberately:

```bash
UPDATE_API_SURFACE=1 pytest packages/embeddedci/tests/test_api_surface.py
UPDATE_TOOLS_SURFACE=1 pytest packages/embeddedci-mcp/tests/test_server_runtime.py -k surface
```

Migration notes: [embeddedci](packages/embeddedci/CHANGELOG.md),
[embeddedci-mcp](packages/embeddedci-mcp/CHANGELOG.md),
[embeddedci-openhtf](packages/embeddedci-openhtf/CHANGELOG.md).

## Layout

```
embeddedci-python/
├── pyproject.toml            # uv workspace root (not published)
├── packages/
│   ├── embeddedci/           # SDK + pytest plugin
│   ├── embeddedci-mcp/       # MCP server (console script: embeddedci-mcp)
│   └── embeddedci-openhtf/   # OpenHTF plug (direct TCP/serial)
└── .github/workflows/        # CI for all packages; per-package publish tags
```

## Development

Python 3.10+.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e "packages/embeddedci[dev]" -e "packages/embeddedci-mcp[dev]" -e "packages/embeddedci-openhtf[dev]"
pytest packages/embeddedci packages/embeddedci-mcp packages/embeddedci-openhtf
```

Hardware tests skip without a pod. To run them against one:

```bash
pytest packages/embeddedci --benchpod-connection 192.168.1.213
```

The board's I/O voltage (3.3 V) is set once in `packages/embeddedci/tests/conftest.py`; change it
there for a 1V8 board.

## Running the MCP server

```bash
# launched by an MCP client (Claude Code / Claude Desktop / Cursor) over stdio:
embeddedci-mcp --connection 192.168.1.213

# or served over HTTP for a remote bench (a token is required off loopback):
embeddedci-mcp --transport http --host 0.0.0.0 --auth-token "$TOKEN" --connection usb
```

See [`packages/embeddedci-mcp/README.md`](packages/embeddedci-mcp/README.md) for client
configuration and the tool list.

## Releasing

Each package publishes from its own tag (`embeddedci-v*`, `embeddedci-mcp-v*`,
`embeddedci-openhtf-v*`) via `.github/workflows/publish.yml`; the tag must match the version in
that package's `pyproject.toml`. Release `embeddedci` first — the other two depend on it from PyPI.
