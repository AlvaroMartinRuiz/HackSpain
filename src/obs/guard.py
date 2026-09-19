"""Keep the console to the team.

The console shows names, phone numbers, charts, transcripts and recordings,
and ngrok publishes the whole port, not just the call socket. So everything
except the socket the platform dials and the health check needs a token, or
has to come from this machine directly.

Open it once with ``/?token=<CONSOLE_TOKEN>``: that sets a cookie, and the
page's own requests, audio players and live feed carry it from then on.
"""

from __future__ import annotations

import hmac
from http.cookies import SimpleCookie
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs

from src.config import settings

OPEN_PATHS = frozenset({"/ws", "/health"})
COOKIE = "console_token"
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
# ngrok, like any proxy, forwards from loopback but always adds these.
PROXY_HEADERS = (b"x-forwarded-for", b"x-forwarded-host", b"x-forwarded-proto")

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class ConsoleGuard:
    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or scope.get("path") in OPEN_PATHS:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        if _is_local(scope, headers):
            await self.app(scope, receive, send)
            return

        token = settings.console_token
        from_query = _query_token(scope)
        if token and _matches(token, from_query):
            await self.app(scope, receive, _setting_cookie(send, token, headers))
            return
        if token and (_matches(token, _header(headers, b"x-console-token"))
                      or _matches(token, _cookie(headers))):
            await self.app(scope, receive, send)
            return

        await _reject(scope, receive, send)


def _is_local(scope: Scope, headers: dict[bytes, bytes]) -> bool:
    client = scope.get("client") or ("", 0)
    return client[0] in LOOPBACK and not any(h in headers for h in PROXY_HEADERS)


def _matches(token: str, offered: str) -> bool:
    if not offered or len(token) != len(offered):
        return False
    return hmac.compare_digest(token.encode(), offered.encode())


def _header(headers: dict[bytes, bytes], name: bytes) -> str:
    return headers.get(name, b"").decode("latin-1").strip()


def _cookie(headers: dict[bytes, bytes]) -> str:
    raw = _header(headers, b"cookie")
    if not raw:
        return ""
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return ""
    morsel = jar.get(COOKIE)
    return morsel.value if morsel else ""


def _query_token(scope: Scope) -> str:
    query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
    return (query.get("token") or [""])[0]


def _setting_cookie(send: Send, token: str, headers: dict[bytes, bytes]) -> Send:
    secure = _header(headers, b"x-forwarded-proto") == "https"
    cookie = (f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=172800"
              + ("; Secure" if secure else ""))

    async def wrapped(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            message = {**message, "headers": [*message.get("headers", []),
                                              (b"set-cookie", cookie.encode("latin-1"))]}
        await send(message)

    return wrapped


async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] == "websocket":
        await receive()  # websocket.connect
        await send({"type": "websocket.close", "code": 1008})
        return
    body = b'{"detail":"console token required: open /?token=<CONSOLE_TOKEN>"}'
    await send({"type": "http.response.start", "status": 401,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
