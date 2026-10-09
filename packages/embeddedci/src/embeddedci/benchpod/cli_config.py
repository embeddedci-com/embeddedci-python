"""Read-only access to ``benchpod-cli``'s saved settings, so the SDK, the MCP server and the CLI
share one default connection.

``benchpod-cli`` keeps its configuration in ``~/.config/benchpod-cli/config.json`` (or under
``$XDG_CONFIG_HOME``; see its ``internal/benchpodconfig``). ``benchpod set-connection <target>``
and ``benchpod discover --save`` store the default connection there as ``connection``; older CLI
versions wrote a TCP address as ``bench_pod_addr``, which is read the same way. The SDK never
writes this file. A missing, unreadable or malformed file means "nothing saved".
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

#: The CLI's settings directory name under the config root.
CLI_DIR = "benchpod-cli"
#: The connection keyword that stands for the CLI's saved connection.
SAVED_KEYWORD = "saved"

_log = logging.getLogger("embeddedci.benchpod")


def config_dir() -> Path:
    """``$XDG_CONFIG_HOME/benchpod-cli`` when set, else ``~/.config/benchpod-cli`` (as the CLI)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / CLI_DIR


def config_path() -> Path:
    """The CLI's ``config.json``."""
    return config_dir() / "config.json"


def saved_connection(path: Optional[Path] = None) -> Optional[str]:
    """The default connection ``benchpod-cli`` saved (``connection``, else the legacy
    ``bench_pod_addr``), or None when there is none or the file cannot be read."""
    path = path or config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _log.warning("ignoring the benchpod-cli config %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        _log.warning("ignoring the benchpod-cli config %s: not a JSON object", path)
        return None
    for key in ("connection", "bench_pod_addr"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            conn = value.strip()
            if conn.lower() == SAVED_KEYWORD:  # would point at itself
                return None
            return conn
    return None
