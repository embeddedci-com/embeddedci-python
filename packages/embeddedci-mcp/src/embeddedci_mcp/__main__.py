"""Command-line entry point for the BenchPod MCP server.

Run over stdio (the default; launched as a subprocess by an MCP client such as
Claude Desktop / Cursor / Claude Code) or as an HTTP server for a remote bench::

    embeddedci-mcp --transport stdio
    embeddedci-mcp --transport http --host 0.0.0.0 --port 8000 --connection serial
"""

from __future__ import annotations

import argparse
from typing import Optional, Sequence

from .server import mcp
from .session import SESSION


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="embeddedci-mcp",
        description="MCP server exposing the EmbeddedCI BenchPod SDK as agent tools.",
    )
    parser.add_argument(
        "--transport", choices=["stdio", "http"], default="stdio",
        help="MCP transport: 'stdio' (default, local subprocess) or 'http' "
             "(streamable HTTP for a remote bench).",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Bind host for --transport http (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port", type=int, default=8000,
        help="Bind port for --transport http (default: 8000).",
    )
    parser.add_argument(
        "--connection", default=None,
        help="Default BenchPod connection used by the `connect` tool when called "
             "with no argument: host[:port], a serial device path, or 'serial'. "
             "Falls back to the BENCHPOD_CONNECTION environment variable.",
    )
    args = parser.parse_args(argv)

    if args.connection:
        SESSION.default_connection = args.connection

    if args.transport == "http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
