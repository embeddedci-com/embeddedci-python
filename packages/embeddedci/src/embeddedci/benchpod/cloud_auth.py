"""GitHub Actions OIDC → embeddedci session-token exchange for the cloud destination.

A workflow running ``pytest`` with an ``embeddedci:<device>`` connection cannot ship a
long-lived secret, so it mints a short-lived GitHub Actions OIDC token (proving *which repo* is
running) and exchanges it with the embeddedci server for a session token scoped to the org and the
devices that repo is allowed to drive.

Minting only works inside GitHub Actions with ``permissions: id-token: write``; outside that the
errors below explain exactly why it could not be done.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .errors import CloudAuthError

DEFAULT_AUDIENCE = "https://embeddedci.com"
DEFAULT_API_BASE = "https://embeddedci.com"

_HTTP_TIMEOUT = 15.0


def mint_oidc_token(audience: str = DEFAULT_AUDIENCE) -> str:
    """Mint a GitHub Actions OIDC token for ``audience`` using the runner's request endpoint.

    Raises :class:`CloudAuthError` with a specific reason when it cannot:
      1. not running inside a GitHub Action,
      2. running in Actions but missing ``id-token: write`` permission,
      3. the token request itself failed (HTTP error / network).
    """
    req_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    req_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"

    if not in_actions and not req_url:
        raise CloudAuthError(
            "cannot mint a GitHub OIDC token: not running inside a GitHub Action. "
            "The 'embeddedci' destination authenticates via GitHub Actions OIDC and only works in CI."
        )
    if not req_url or not req_token:
        raise CloudAuthError(
            "cannot mint a GitHub OIDC token: the id-token request endpoint is unavailable, which "
            "means the job is missing the id-token permission. Add to your workflow/job:\n"
            "    permissions:\n      id-token: write"
        )

    sep = "&" if "?" in req_url else "?"
    url = f"{req_url}{sep}audience={urllib.parse.quote(audience, safe='')}"
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {req_token}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # reason 3
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise CloudAuthError(f"failed to request a GitHub OIDC token: HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # reason 3 (network/parse)
        raise CloudAuthError(f"failed to request a GitHub OIDC token: {exc}") from exc

    value = body.get("value")
    if not value:
        raise CloudAuthError("GitHub OIDC token response did not contain a 'value' field")
    return value


def exchange_token(api_base: str, oidc_token: str) -> dict:
    """Exchange a GitHub OIDC token for an embeddedci session token via POST /api/github/oidc/token."""
    url = api_base.rstrip("/") + "/api/github/oidc/token"
    data = json.dumps({"token": oidc_token}).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        msg = exc.read().decode("utf-8", "replace")
        try:
            msg = json.loads(msg).get("error", msg)
        except Exception:
            pass
        raise CloudAuthError(f"embeddedci token exchange failed (HTTP {exc.code}): {msg}") from exc
    except Exception as exc:
        raise CloudAuthError(f"embeddedci token exchange failed: {exc}") from exc


def get_session_token(api_base: str = DEFAULT_API_BASE, audience: str = DEFAULT_AUDIENCE) -> str:
    """Mint a GitHub OIDC token and exchange it for an embeddedci session token."""
    oidc = mint_oidc_token(audience)
    resp = exchange_token(api_base, oidc)
    token = resp.get("access_token")
    if not token:
        raise CloudAuthError("embeddedci token exchange returned no access_token")
    return token
