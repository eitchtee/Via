"""Serving the web UI, and security headers for every response."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

WEB_DIR = Path(__file__).resolve().parent / "web"

# The UI loads nothing from other origins and runs no inline code.
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)
_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
]


class SecurityHeaders:
    """Pure ASGI middleware (unlike BaseHTTPMiddleware, it never buffers SSE streams)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Swagger UI (/docs) needs its CDN assets and inline script.
        with_csp = not scope["path"].startswith("/docs")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(_HEADERS)
                if with_csp:
                    headers.append((b"content-security-policy", CSP.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def mount_web_ui(app: FastAPI) -> None:
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
