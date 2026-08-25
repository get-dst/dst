"""Response security headers on every surface the port serves.

The dashboard, the control plane and the MCP door share one origin, and none of
them used to send a framing, sniffing, referrer or content policy. The framing
gap is the one that mattered: a dashboard that can be loaded in an invisible
iframe is a clickjacking target on every control it has, including the
destructive ones.

The content policy is the part that can break a page rather than protect it, so
what it permits is pinned against what the built dashboard actually loads: one
external module script, one external stylesheet, self-hosted fonts, and inline
style ATTRIBUTES throughout the JSX.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from services.api.security_headers import SecurityHeaders
from services.app import app

client = TestClient(app)

# Paths that answer without a database or credentials, one per surface. `/mcp` is
# there because the transport gate answers that one itself, at the ASGI edge and
# before routing — the middleware has to be outside it, or the surface most likely
# to be pointed at a hostile client is the one with no headers.
_PATHS = [
    "/health",
    "/.well-known/oauth-authorization-server",
    "/mgmt/ping",
    "/mcp",
    "/no-such-endpoint",
]


@pytest.mark.parametrize("path", _PATHS)
def test_every_response_carries_the_headers(path: str) -> None:
    """Including the ones that fail. A 401 or a 404 is still a response a browser
    renders, and the headers are not conditional on the status."""
    r = client.get(path)
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert r.headers["cross-origin-opener-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_dashboard_policy_matches_what_the_bundle_loads() -> None:
    """The build emits external JS+CSS and no inline <script>, so scripts stay on
    'self'. `style-src` must keep 'unsafe-inline' — the dashboard sets inline style
    attributes (style={{…}}) throughout, and that directive governs them; dropping
    it silently unstyles the app rather than failing loudly."""
    csp = client.get("/health").headers["content-security-policy"]
    assert "script-src 'self';" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "style-src 'self' 'unsafe-inline'" in csp
    assert "font-src 'self'" in csp
    assert "base-uri 'none'" in csp
    assert "object-src 'none'" in csp


def test_openapi_reference_gets_a_policy_that_lets_it_load() -> None:
    """Swagger UI boots from an inline script and a CDN. A single strict policy
    would leave the API reference a blank page — a page broken by a header nobody
    can see — so it gets a policy describing what it really loads, and is still
    unframeable."""
    r = client.get("/docs")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "https://cdn.jsdelivr.net" in csp
    assert "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY"


def test_hsts_is_absent_over_plain_http() -> None:
    """Browsers ignore HSTS on a non-HTTPS response anyway; sending it there would
    only be noise in a self-hosted HTTP deployment. Production and real HTTPS get
    it — same rule the session cookie uses for its Secure flag."""
    assert "strict-transport-security" not in client.get("/health").headers


def test_a_route_may_declare_its_own_policy() -> None:
    """The escape hatch the Clerk consent page uses: a page whose contents the
    default policy would break declares a policy next to its markup, and the
    middleware must not clobber it. The other headers are still added."""
    own = "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"

    async def page(request: object) -> PlainTextResponse:
        return PlainTextResponse("hi", headers={"Content-Security-Policy": own})

    inner = Starlette(routes=[Route("/p", page)])
    inner.add_middleware(SecurityHeaders)
    r = TestClient(inner).get("/p")
    assert r.headers["content-security-policy"] == own
    assert r.headers["x-frame-options"] == "DENY"


def test_consent_page_is_not_frameable() -> None:
    """The MCP consent screen authorizes a token against the operator's own key.
    Framing it is the classic path to a click the person did not mean to make."""
    r = client.get(
        "/oauth/authorize",
        params={
            "client_id": "dstc_nope",
            "redirect_uri": "http://127.0.0.1/cb",
            "code_challenge": "x" * 43,
        },
        follow_redirects=False,
    )
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
