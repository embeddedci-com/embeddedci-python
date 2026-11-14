"""HTTP for calls that carry credentials.

``urllib``'s redirect handler copies every header to the new URL, ``Authorization`` included,
even when the redirect leaves the host: an endpoint that redirects to object storage (or a
``BENCHPOD_API_BASE`` that redirects) would hand it the API key or session token. :func:`urlopen`
drops the credentials on a redirect to another host or scheme.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from typing import Any, Optional

#: Headers that hold credentials, dropped on a redirect that leaves the origin.
_SECRET_HEADERS = ("authorization", "x-benchpod-lease", "cookie")


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        old, nxt = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(new.full_url)
        if (old.scheme, old.hostname, old.port) != (nxt.scheme, nxt.hostname, nxt.port):
            for name in list(new.headers):
                if name.lower() in _SECRET_HEADERS:
                    del new.headers[name]
            for name in list(new.unredirected_hdrs):
                if name.lower() in _SECRET_HEADERS:
                    del new.unredirected_hdrs[name]
        return new


_opener = urllib.request.build_opener(_DropAuthOnRedirect)
_STDLIB_URLOPEN = urllib.request.urlopen


def urlopen(req: Any, data: Optional[bytes] = None, timeout: Optional[float] = None, **kw: Any):
    """:func:`urllib.request.urlopen` that keeps credentials on their own origin across redirects.

    A test that replaced ``urllib.request.urlopen`` with a fake gets its fake called."""
    if urllib.request.urlopen is not _STDLIB_URLOPEN:
        args = {**kw}
        if data is not None:
            args["data"] = data
        if timeout is not None:
            args["timeout"] = timeout
        return urllib.request.urlopen(req, **args)
    if timeout is None:
        return _opener.open(req, data, **kw)
    return _opener.open(req, data, timeout=timeout, **kw)
