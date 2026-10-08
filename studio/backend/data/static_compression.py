"""Scope gzip compression to public, same-origin static text assets."""

from __future__ import annotations

from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

_STATIC_TEXT_PREFIXES = ("/js/", "/css/", "/vendor/")
_STATIC_TEXT_SUFFIXES = (".css", ".js", ".mjs", ".json", ".map", ".svg", ".txt", ".xml")


class StaticAssetCompressionMiddleware:
    """Apply gzip only to static text, never API or artifact responses.

    Static files retain their existing ``Cache-Control: no-cache`` policy.
    The wrapped Starlette middleware adds ``Vary: Accept-Encoding`` when it
    actually emits a gzip response and skips already encoded responses.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=512, compresslevel=6)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path") or "")
        if (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and path.startswith(_STATIC_TEXT_PREFIXES)
            and path.endswith(_STATIC_TEXT_SUFFIXES)
        ):
            await self.gzip(scope, receive, send)
            return
        await self.app(scope, receive, send)
