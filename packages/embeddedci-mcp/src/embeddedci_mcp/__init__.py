"""embeddedci-mcp — an MCP server that lets AI agents drive an EmbeddedCI BenchPod."""

from importlib.metadata import PackageNotFoundError, version

from .server import mcp
from .session import SESSION, Session

try:
    __version__ = version("embeddedci-mcp")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0+unknown"

__all__ = ["mcp", "SESSION", "Session", "main", "__version__"]


def main(argv=None) -> None:
    """Console-script entry point (see ``__main__.main``)."""
    from .__main__ import main as _main

    _main(argv)
