"""The board over embeddedci.com: ``AIVision("embeddedci:<name>")``.

A board paired with embeddedci.com keeps a connection open to it, and the server forwards each
API call of the organization's members to the board:

    ANY https://www.embeddedci.com/api/cloud/aivision/<name>/api/v1/<path>

The caller authenticates with a short-lived cloud session token, minted the same way as for a
cloud BenchPod (:mod:`embeddedci.benchpod.cloud_auth`): from an API key with the "Control AI
Vision" scope (``api_key=`` or ``BENCHPOD_API_KEY``), else the ``benchpod login`` session, else
GitHub Actions OIDC. The lease is the server's device lease (``/api/cloud/devices/lease``), the
same one a cloud BenchPod uses; the board's own LAN lease is not reachable over the cloud.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional
from urllib.parse import quote, urlsplit

from ..benchpod.cloud_auth import DEFAULT_API_BASE, DEFAULT_AUDIENCE, mint_session_token

#: Prefix of a cloud connection string: ``embeddedci:<board name>``.
CLOUD_PREFIX = "embeddedci:"
#: Environment variable with the server's base URL (default https://www.embeddedci.com); the
#: BenchPod SDK's, so one setting points both at a test server.
ENV_API_BASE = "BENCHPOD_API_BASE"
#: Environment variable with an API key (the BenchPod SDK's).
ENV_API_KEY = "BENCHPOD_API_KEY"

#: Renew the session token this long before it expires.
_RENEW_MARGIN = 120.0


def cloud_name(connection: Optional[str]) -> Optional[str]:
    """The board name of an ``embeddedci:<name>`` connection, or None for any other."""
    conn = (connection or "").strip()
    if not conn.lower().startswith(CLOUD_PREFIX):
        return None
    name = conn[len(CLOUD_PREFIX):].strip()
    return name or None


def resolve_api_base(given: Optional[str] = None) -> str:
    """The server's base URL: ``given``, else ``BENCHPOD_API_BASE``, else www.embeddedci.com."""
    return (given or os.environ.get(ENV_API_BASE) or DEFAULT_API_BASE).rstrip("/")


def board_url(base: str, name: str) -> str:
    """The URL the board's ``/api/v1`` hangs under, over the cloud."""
    return f"{base}/api/cloud/aivision/{quote(name, safe='')}"


def login_token(base: str) -> Optional[Callable[[], str]]:
    """The ``benchpod login`` session's access token provider, or None without a session."""
    from ..benchpod import cli_login

    if not cli_login.has_session():
        return None
    return lambda: cli_login.access_token(base)


class CloudSession:
    """The cloud session token: minted on first use, renewed before it expires, and dropped when
    the server refuses it so the next call mints a new one."""

    def __init__(self, base: str, *, api_key: Optional[str] = None, token: Optional[str] = None,
                 user_token: Optional[Callable[[], str]] = None, audience: str = DEFAULT_AUDIENCE) -> None:
        self.api_base = base
        self.audience = audience
        self.api_key = api_key or os.environ.get(ENV_API_KEY) or None
        self.user_token = user_token if user_token is not None or self.api_key else login_token(base)
        self._token = token
        self._renewable = token is None
        self._expires_at: Optional[float] = None
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            due = (self._renewable and self._expires_at is not None
                   and time.time() >= self._expires_at - _RENEW_MARGIN)
            if not self._token or due:
                self._token, self._expires_at = mint_session_token(
                    self.api_base, self.audience, self.api_key, self.user_token)
            return self._token

    def invalidate(self) -> bool:
        """Forget a refused token. False when it came from the caller and cannot be renewed."""
        if not self._renewable:
            return False
        with self._lock:
            self._token, self._expires_at = None, None
        return True


def pairing_server(base: str) -> str:
    """The host the board dials for its cloud connection (``PUT /cloud`` ``server``): the API host
    for the production site (www and the apex sit behind a proxy that does not carry it), else the
    host[:port] of ``base``."""
    host = urlsplit(base).netloc or base
    if host in ("www.embeddedci.com", "embeddedci.com"):
        return "api.embeddedci.com"
    return host


def register_device(base: str, name: str, public_key: str, *, api_key: Optional[str] = None,
                    user_token: Optional[Callable[[], str]] = None, timeout: float = 15.0) -> dict:
    """Register a board in the caller's active organization (``POST /api/aivision/devices``):
    returns the device (``id``, ``name``, ``connection``, ...). Registering the same key again
    returns the same device. Credentials: ``api_key`` (or ``BENCHPOD_API_KEY``), else
    ``user_token``, else the ``benchpod login`` session."""
    import json
    import urllib.error
    import urllib.request

    from ..benchpod.cloud_auth import USER_AGENT
    from . import errors

    key = api_key or os.environ.get(ENV_API_KEY)
    if key:
        auth = "ApiKey " + key.strip()
    else:
        provider = user_token or login_token(base)
        if provider is None:
            raise errors.AuthError("registering a board on embeddedci.com needs an API key with the "
                                   "Control AI Vision scope (api_key= or BENCHPOD_API_KEY) or `benchpod login`")
        auth = "Bearer " + provider()
    req = urllib.request.Request(
        base + "/api/aivision/devices", method="POST",
        data=json.dumps({"name": name, "public_key": public_key}).encode(),
        headers={"Authorization": auth, "Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw or b"null")
        except ValueError:
            parsed = None
        raise errors.from_answer(e.code, parsed, raw.decode("utf-8", "replace")[:200]) from None
    except (urllib.error.URLError, OSError) as e:
        raise errors.TransportError(f"POST {base}/api/aivision/devices: {getattr(e, 'reason', e)}") from None


def list_boards(*, api_base: Optional[str] = None, api_key: Optional[str] = None,
                user_token: Optional[Callable[[], str]] = None) -> list:
    """The AI Vision boards of your active organization on embeddedci.com (``GET
    /api/aivision/devices``): each with ``id``, ``name``, ``online`` and ``connection``
    (``embeddedci:<name>``, what :class:`~.client.AIVision` takes). Credentials as for
    :func:`register_device`."""
    from ..benchpod.server_api import ServerApi
    from . import errors

    base = resolve_api_base(api_base)
    key = api_key or os.environ.get(ENV_API_KEY)
    provider = None if key else (user_token or login_token(base))
    if not key and provider is None:
        raise errors.AuthError("listing boards on embeddedci.com needs an API key with the Control AI Vision "
                               "scope (api_key= or BENCHPOD_API_KEY) or `benchpod login`")
    try:
        api = ServerApi(api_base=base, api_key=key.strip() if key else None, token_provider=provider)
        _, data = api.request("GET", "/aivision/devices")
    except Exception as e:  # ServerApiError, CloudAuthError
        raise errors.AIVisionError(f"embeddedci.com: {e}", status=getattr(e, "status", 0) or 0) from None
    return list((data or {}).get("devices") or [])
