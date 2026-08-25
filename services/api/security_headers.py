"""Response security headers for every surface on the port.

dst serves the dashboard, the control plane and the MCP door from one origin, so
one middleware covers all three. Four headers are unconditional, one is
conditional, and the content policy varies by what the path actually serves.

The framing headers are the load-bearing ones: without them the dashboard is
embeddable, and a dashboard that can be put in an invisible iframe is a
clickjacking target on every destructive control it has. `X-Frame-Options` and
CSP `frame-ancestors` say the same thing to old and new browsers.

Written as raw ASGI rather than `BaseHTTPMiddleware` because the MCP surface
streams (server-sent events) and that base class buffers.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from services.config import settings

# Applied to every response.
_ALWAYS: list[tuple[bytes, bytes]] = [
    # No MIME sniffing: an uploaded file echoed back must never be re-interpreted
    # as script because a browser disagreed with the declared type.
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    # Full URLs of a governed dashboard carry lens and org names — send them to
    # other origins as a bare origin, and not at all when leaving HTTPS.
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    # Severs window.opener from anything this origin opens or is opened by.
    (b"cross-origin-opener-policy", b"same-origin"),
]

_HSTS = (b"strict-transport-security", b"max-age=31536000")

# The dashboard build: one external module script, one external stylesheet,
# self-hosted fonts, and no inline <script> — so 'self' is enough for scripts.
# `style-src` must keep 'unsafe-inline': the dashboard sets inline style
# ATTRIBUTES throughout (style={{…}} in JSX), which that directive governs.
# Removing it means removing those, which is a UI refactor, not a header change.
_APP_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "object-src 'none'"
)

# The OpenAPI reference is FastAPI's bundled Swagger UI, which loads its assets
# from a CDN and boots from an inline <script> — neither of which the app policy
# above permits. It gets a policy describing what it genuinely loads instead of an
# exemption, so the page keeps working and still cannot be framed.
_DOCS_HOSTS = "https://cdn.jsdelivr.net"
_DOCS_CSP = (
    "default-src 'self'; "
    f"script-src 'self' 'unsafe-inline' {_DOCS_HOSTS}; "
    f"style-src 'self' 'unsafe-inline' {_DOCS_HOSTS}; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "connect-src 'self'; "
    "worker-src 'self' blob:; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "object-src 'none'"
)
_DOCS_PATHS = frozenset({"/docs", "/redoc", "/docs/oauth2-redirect"})


def csp_for(path: str) -> str:
    return _DOCS_CSP if path in _DOCS_PATHS else _APP_CSP


class SecurityHeaders:
    """Add the response security headers, without overwriting a route's own CSP.

    A route that renders something the default policy would break declares its own
    `Content-Security-Policy` (the MCP consent pages do). Honouring what is already
    set keeps that decision next to the markup it describes.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] != "http.response.start":
                await send(message)
                return
            headers: list[tuple[bytes, bytes]] = list(message.get("headers") or [])
            present = {k.lower() for k, _ in headers}
            headers.extend((k, v) for k, v in _ALWAYS if k not in present)
            if b"content-security-policy" not in present:
                headers.append((b"content-security-policy", csp_for(scope["path"]).encode()))
            # HSTS only where it can mean anything. Browsers ignore it over plain
            # HTTP, and a TLS-terminating proxy that dst is not told to trust makes
            # the observed scheme read 'http' on a live HTTPS site — the same reason
            # the session cookie forces Secure in production.
            if scope.get("scheme") == "https" or settings.environment == "production":
                if _HSTS[0] not in present:
                    headers.append(_HSTS)
            message["headers"] = headers
            await send(message)

        await self._app(scope, receive, send_with_headers)
