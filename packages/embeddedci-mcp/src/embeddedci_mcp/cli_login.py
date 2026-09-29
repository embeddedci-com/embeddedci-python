"""The ``benchpod login`` session, reused by the ``cloud_*`` tools.

``benchpod-cli`` keeps a logged-in user's tokens in ``~/.config/benchpod-cli/token.json`` (see its
``internal/authstore``). A developer who has run ``benchpod login`` can therefore list the pods on
embeddedci.com without also creating an API key. This module reads that file, refreshes the access
token through ``POST /api/auth/guest/refresh`` when it has expired, and writes the rotated tokens
back in the same shape, so the CLI keeps working afterwards.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from embeddedci.benchpod.cloud_auth import USER_AGENT
from embeddedci.benchpod.errors import CloudAuthError

#: Same margin as the CLI: a token this close to expiry counts as expired.
EXPIRY_SKEW = timedelta(seconds=60)
_HTTP_TIMEOUT = 15.0
_LOGIN_HINT = "run `benchpod login` (or set BENCHPOD_API_KEY)"

# One refresh at a time: the server rotates the refresh token, so two concurrent refreshes would
# leave one of them holding a revoked token.
_lock = threading.Lock()


def token_path() -> Path:
    """Where ``benchpod-cli`` stores its tokens: ``$XDG_CONFIG_HOME`` first, else ``~/.config``."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "benchpod-cli" / "token.json"


def _parse_time(value: Any) -> Optional[datetime]:
    """An RFC 3339 time as Go writes it (up to 9 fractional digits); None for Go's zero time."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)  # Python 3.10 accepts at most microseconds
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    if t.year <= 1:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _expired(token: Any, expires_at: Any, now: datetime) -> bool:
    """Mirrors authstore's AccessExpired/RefreshExpired: no expiry recorded means still valid."""
    if not token:
        return True
    t = _parse_time(expires_at)
    return t is not None and now >= t - EXPIRY_SKEW


def _load(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise CloudAuthError(f"not logged in to embeddedci.com ({path} not found): {_LOGIN_HINT}") from None
    except (OSError, ValueError) as exc:
        raise CloudAuthError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CloudAuthError(f"cannot read {path}: not a JSON object")
    return data


def _save(path: Path, tokens: Dict[str, Any]) -> None:
    """Replace the file atomically with owner-only permissions, as authstore.Save does."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="token-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(tokens, fh, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _refresh(api_base: str, refresh_token: str) -> Dict[str, Any]:
    request = urllib.request.Request(
        api_base.rstrip("/") + "/api/auth/guest/refresh",
        data=json.dumps({"refresh_token": refresh_token}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        msg = exc.read().decode("utf-8", "replace")
        try:
            msg = json.loads(msg).get("error", msg)
        except Exception:
            pass
        raise CloudAuthError(
            f"refreshing the benchpod login failed (HTTP {exc.code}): {msg}; {_LOGIN_HINT}") from exc
    except Exception as exc:
        raise CloudAuthError(f"refreshing the benchpod login failed: {exc}") from exc
    if not body.get("access_token") or not body.get("refresh_token"):
        raise CloudAuthError("refreshing the benchpod login returned no tokens; " + _LOGIN_HINT)
    return body


def access_token(api_base: str, path: Optional[Path] = None) -> str:
    """A valid access token from the ``benchpod login`` session, refreshing (and saving) it first
    when it has expired. Raises :class:`CloudAuthError` when there is no usable session."""
    path = path or token_path()
    with _lock:
        tokens = _load(path)
        now = datetime.now(timezone.utc)
        if not _expired(tokens.get("access_token"), tokens.get("access_expires_at"), now):
            return str(tokens["access_token"])
        if _expired(tokens.get("refresh_token"), tokens.get("refresh_expires_at"), now):
            raise CloudAuthError(f"the benchpod login has expired: {_LOGIN_HINT}")
        resp = _refresh(api_base, str(tokens["refresh_token"]))
        local_now = datetime.now().astimezone()
        tokens.update(
            access_token=resp["access_token"],
            refresh_token=resp["refresh_token"],
            access_expires_at=(local_now + timedelta(seconds=int(resp.get("access_expires_in") or 0))).isoformat(),
            refresh_expires_at=(local_now + timedelta(seconds=int(resp.get("refresh_expires_in") or 0))).isoformat(),
            session_id=resp.get("session_id") or tokens.get("session_id", ""),
            user_id=resp.get("user_id") or tokens.get("user_id", ""),
        )
        _save(path, tokens)
        return str(resp["access_token"])
