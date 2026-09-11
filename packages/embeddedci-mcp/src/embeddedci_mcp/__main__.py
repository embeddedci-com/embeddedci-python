"""Command-line entry point for the BenchPod MCP server.

Run over stdio (the default; launched as a subprocess by an MCP client such as Claude Desktop,
Cursor or Claude Code) or as a streamable-HTTP server for a remote bench::

    embeddedci-mcp --connection 192.168.1.213 --la-voltage 3.3
    embeddedci-mcp --transport http --host 0.0.0.0 --auth-token "$TOKEN" --connection usb
"""

from __future__ import annotations

import argparse
import ipaddress
import os
from typing import List, Optional, Sequence

from .server import mcp
from .session import SESSION

TOKEN_ENV = "EMBEDDEDCI_MCP_TOKEN"


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return host == "localhost"
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="embeddedci-mcp",
        description="MCP server that lets AI agents drive an EmbeddedCI BenchPod.",
    )
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                        help="'stdio' (default, launched by an MCP client) or 'http' (streamable HTTP).")
    parser.add_argument("--connection", default=None,
                        help="Default connection for the connect tool: host[:port], a serial device, "
                             "'usb', or 'embeddedci:<device>'. Falls back to BENCHPOD_CONNECTION.")
    parser.add_argument("--la-voltage", type=float, choices=[1.8, 3.3], default=None,
                        help="LA I/O voltage applied on connect. Falls back to BENCHPOD_LA_VOLTAGE.")
    parser.add_argument("--lease-wait", type=float, default=30.0,
                        help="Cloud: seconds connect waits for a busy shared device (default 30).")
    parser.add_argument("--idle-timeout", type=float, default=600.0,
                        help="Cloud: release the device lease after this many idle seconds; the next "
                             "tool call reconnects (default 600, 0 = never).")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="Per-command device timeout in seconds (default 30).")
    http = parser.add_argument_group("http transport")
    http.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1).")
    http.add_argument("--port", type=int, default=8000, help="Bind port (default 8000).")
    http.add_argument("--auth-token", default=os.environ.get(TOKEN_ENV),
                      help=f"Require 'Authorization: Bearer <token>' on every request (or set {TOKEN_ENV}). "
                           "Mandatory when binding a non-loopback address.")
    http.add_argument("--allowed-host", action="append", default=[], metavar="HOST[:PORT]",
                      help="Host header value to accept (repeatable), for DNS-rebinding protection on a "
                           "non-loopback bind. Without it, Host checking is off and the token protects.")
    http.add_argument("--allow-unauthenticated", action="store_true",
                      help="Serve a non-loopback address without a token (only on an isolated network).")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    SESSION.default_connection = args.connection
    SESSION.default_la_voltage = args.la_voltage
    SESSION.lease_wait = args.lease_wait
    SESSION.idle_timeout = max(0.0, args.idle_timeout)
    SESSION.timeout = args.timeout

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    loopback = _is_loopback(args.host)
    if not loopback and not args.auth_token and not args.allow_unauthenticated:
        parser.error(f"--host {args.host} exposes the bench to the network: pass --auth-token (or set "
                     f"{TOKEN_ENV}), or --allow-unauthenticated on an isolated network")

    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    mcp.settings.host, mcp.settings.port = args.host, args.port
    if not loopback:
        # FastMCP only accepts loopback Host headers by default, which rejects every real client
        # of a network bind. Accept the operator's hosts, or rely on the bearer token.
        allowed: List[str] = list(args.allowed_host)
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=bool(allowed), allowed_hosts=allowed, allowed_origins=[])
    app = mcp.streamable_http_app()
    if args.auth_token:
        from .http_auth import BearerTokenMiddleware

        app = BearerTokenMiddleware(app, args.auth_token)  # type: ignore[assignment]
    uvicorn.run(app, host=args.host, port=args.port, log_level=mcp.settings.log_level.lower())


if __name__ == "__main__":
    main()
