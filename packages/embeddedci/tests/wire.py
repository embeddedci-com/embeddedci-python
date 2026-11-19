"""Requests as the fake pods in these tests see them, without the two fields every request
carries (``client_api``, ``client``: test_client_api.py checks those)."""

from __future__ import annotations

import json
from typing import Any, Dict, Union

CLIENT_FIELDS = ("client_api", "client")


def request(line: Union[bytes, str]) -> Dict[str, Any]:
    """One JSON request line, without ``client_api`` and ``client``."""
    req = json.loads(line)
    return {k: v for k, v in req.items() if k not in CLIENT_FIELDS} if isinstance(req, dict) else req
