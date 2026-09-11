"""Bearer-token protection for the streamable-HTTP transport.

The server can power, flash and drive voltages into a real board, so an HTTP endpoint reachable
from the network must not be open. :class:`BearerTokenMiddleware` is a plain ASGI wrapper that
rejects any HTTP request without ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import hmac
from typing import Any, Awaitable, Callable, MutableMapping

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class BearerTokenMiddleware:
    """Reject HTTP requests whose ``Authorization`` header is not ``Bearer <token>``."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        if not token:
            raise ValueError("an empty token would authorize everyone")
        self.app = app
        self._expected = f"Bearer {token}".encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            header = b""
            for name, value in scope.get("headers") or ():
                if name.lower() == b"authorization":
                    header = value
                    break
            if not hmac.compare_digest(header, self._expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"unauthorized\n"})
                return
        await self.app(scope, receive, send)
