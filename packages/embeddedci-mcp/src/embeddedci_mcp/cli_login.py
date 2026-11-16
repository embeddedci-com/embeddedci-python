"""The ``benchpod login`` session, reused by the ``cloud_*`` tools and cloud connections.

The SDK's :mod:`embeddedci.benchpod.cli_login` reads ``benchpod-cli``'s token file, refreshes the
access token when it has expired and writes the rotated tokens back. This module re-exports it, so
the server and the SDK share one copy and one refresh lock (the server rotates the refresh token,
so two concurrent refreshes would leave one holding a revoked token).
"""

from __future__ import annotations

import urllib.request  # noqa: F401  (tests patch cli_login.urllib.request.urlopen)

from embeddedci.benchpod.cli_login import (  # noqa: F401
    EXPIRY_SKEW,
    _parse_time,
    access_token,
    has_session,
    token_path,
)
