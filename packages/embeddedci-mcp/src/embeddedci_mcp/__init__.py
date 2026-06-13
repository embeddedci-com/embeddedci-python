"""embeddedci-mcp — an MCP server exposing the EmbeddedCI BenchPod SDK as tools."""

from .server import mcp
from .session import SESSION, Session

__all__ = ["mcp", "SESSION", "Session", "main"]

__version__ = "0.1.0"


def main(argv=None) -> None:
    """Console-script entry point (see ``__main__.main``)."""
    from .__main__ import main as _main

    _main(argv)
