"""OAuth AS endpoints for the MCP surface.

Mounted at the app **root** (not under ``/mcp``): RFC 9728 puts the protected-resource
metadata at the host root (``/.well-known/oauth-protected-resource/mcp``), and clients
discover the AS from there. Endpoints:

  GET  /.well-known/oauth-protected-resource[/mcp]  -> where to authenticate
  GET  /.well-known/oauth-authorization-server      -> AS metadata
  POST /oauth/register                              -> dynamic client registration
  GET  /oauth/authorize                             -> server-rendered consent page
  POST /oauth/authorize/complete                    -> credential -> signed auth code
  POST /oauth/token                                 -> code + PKCE -> dsto_ access token

The consent page is **self-contained** (server-rendered HTML, no dashboard SPA, no Clerk
frontend): the operator authenticates with a credential dst already trusts — a caller
key (``dst_``/``dsto_``, the per-person/attributable path), an admin token, or a Clerk
session JWT when Clerk is configured. We mint a ``dsto_`` token bound to a caller row so
every query is attributable. See ``services.auth.oauth``.
"""

from __future__ import annotations

import html
import uuid
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from mcp.shared.auth import OAuthClientMetadata, OAuthMetadata, ProtectedResourceMetadata

from services.auth import clerk, oauth, scopes
from services.auth.deps import resolve_admin_org
from services.auth.tokens import ADMIN_PREFIX, CALLER_PREFIX, OAUTH_PREFIX, hash_token
from services.config import settings
from services.db.session import org_session
from services.governance import credentials, ratelimit

router = APIRouter(tags=["oauth"])


def _base(request: Request) -> str:
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


def _meta(content: dict[str, Any]) -> JSONResponse:
    # Discovery docs are fetched cross-origin by MCP clients — keep them open.
    return JSONResponse(content, headers={"Access-Control-Allow-Origin": "*"})


def _oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status)


@router.get("/.well-known/oauth-protected-resource/mcp")
@router.get("/.well-known/oauth-protected-resource")
async def protected_resource_metadata(request: Request) -> JSONResponse:
    """RFC 9728 protected-resource metadata for the MCP door — what the 401
    challenge points at so a client's native OAuth flow can find the
    authorization server. Served with open CORS: discovery is cross-origin."""
    base = _base(request)
    meta = ProtectedResourceMetadata(
        resource=f"{base}/mcp",  # type: ignore[arg-type]
        authorization_servers=[base],  # type: ignore[list-item]
        resource_name="dst",
        scopes_supported=list(scopes.ALL),
    )
    return _meta(meta.model_dump(mode="json", exclude_none=True))


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata(request: Request) -> JSONResponse:
    """RFC 8414 authorization-server metadata: authorization-code + PKCE S256
    only, public clients, dynamic registration. Served with open CORS."""
    base = _base(request)
    meta = OAuthMetadata(
        issuer=base,  # type: ignore[arg-type]
        authorization_endpoint=f"{base}/oauth/authorize",  # type: ignore[arg-type]
        token_endpoint=f"{base}/oauth/token",  # type: ignore[arg-type]
        registration_endpoint=f"{base}/oauth/register",  # type: ignore[arg-type]
        response_types_supported=["code"],
        grant_types_supported=["authorization_code"],
        code_challenge_methods_supported=["S256"],
        token_endpoint_auth_methods_supported=["none"],
        scopes_supported=list(scopes.ALL),
    )
    return _meta(meta.model_dump(mode="json", exclude_none=True))


# Registration is the one endpoint that writes on behalf of a stranger: it has to
# be anonymous (RFC 7591 — the client is registering precisely because it has no
# credential yet), so the budget is what keeps "anonymous" from meaning "free
# INSERT". Sized for the real flow, which registers once per client per install and
# retries a handful of times at worst.
#
# In-process limiter (services/governance/ratelimit.py): per worker, so the budget
# holds per replica rather than across a fleet, and a restart forgets it. Retention
# in services/auth/oauth.py is the half that does not depend on that — it bounds the
# table whatever slips through here.
_REGISTER_RPM = 12

# What one registration may write. `OAuthClientMetadata` validates shapes, not
# sizes, so without these a single anonymous request can store an arbitrarily large
# row — the same free-write problem measured in bytes instead of rows.
_MAX_REDIRECT_URIS = 12
_MAX_NAME_CHARS = 200
_MAX_BODY_BYTES = 16 * 1024


@router.post("/oauth/register", status_code=201)
async def register(request: Request) -> JSONResponse:
    """Dynamic client registration (RFC 7591). Public PKCE clients — no secret issued.

    Anonymous, and therefore throttled per source address and bounded in what it may
    store; registrations that never reach an authorization request expire (see
    `services.auth.oauth`). The flow itself is unchanged — a client that registers
    and connects sees exactly what it saw before.
    """
    ip = request.client.host if request.client else "unknown"
    # Not X-Forwarded-For: it is caller-controlled, so keying on it would hand every
    # request a fresh budget. Behind a proxy this is one shared budget — deliberate.
    key = f"oauth-register:{ip}"
    if not ratelimit.check(key, _REGISTER_RPM):
        return JSONResponse(
            {
                "error": "temporarily_unavailable",
                "error_description": "too many client registrations — retry shortly",
            },
            status_code=429,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Retry-After": str(ratelimit.retry_after(key)),
            },
        )
    # Refuse on the DECLARED length before reading, so an oversize body is not first
    # buffered in full and then rejected. A request that declares nothing (chunked)
    # still gets read, hence the second check — this bounds what is stored, and the
    # honest limit of what it bounds in transit is the declared case.
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > _MAX_BODY_BYTES:
        return _oauth_error("invalid_client_metadata", "client metadata too large")
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        return _oauth_error("invalid_client_metadata", "client metadata too large")
    try:
        body = OAuthClientMetadata.model_validate_json(raw)
    except Exception:
        return _oauth_error("invalid_client_metadata", "could not parse client metadata")
    redirect_uris = [str(u) for u in (body.redirect_uris or [])]
    if len(redirect_uris) > _MAX_REDIRECT_URIS:
        return _oauth_error(
            "invalid_redirect_uri", f"at most {_MAX_REDIRECT_URIS} redirect_uris per client"
        )
    if body.client_name and len(body.client_name) > _MAX_NAME_CHARS:
        return _oauth_error(
            "invalid_client_metadata", f"client_name exceeds {_MAX_NAME_CHARS} characters"
        )
    client_id = oauth.register_client(redirect_uris, body.client_name)
    return JSONResponse(
        {
            "client_id": client_id,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "client_name": body.client_name,
        },
        status_code=201,
        headers={"Access-Control-Allow-Origin": "*"},
    )


def _resolve_grant_identity(raw: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Map a pasted credential to (org_id, caller_id) for the token we'll mint.

    A caller/OAuth key resolves to that caller (real per-person attribution); an admin
    token to a caller in its org; a Clerk JWT to the signed-in person (when configured).
    Returns None if the credential isn't recognized.
    """
    if raw.startswith((CALLER_PREFIX, OAUTH_PREFIX)):
        ident = credentials.verify_caller_key(raw)
        return (ident.org_id, ident.caller_id) if ident and ident.caller_id else None
    if raw.startswith(ADMIN_PREFIX):
        org_id = resolve_admin_org(raw)
        if org_id is None:
            return None
        with org_session(org_id) as session:
            cid = credentials.caller_id_by_name(session, "admin") or credentials.create_caller(
                session, "admin", "service", []
            )
        return org_id, cid
    clerk_ident = clerk.resolve_identity(raw)
    if clerk_ident is None:
        return None
    with org_session(clerk_ident.org_id) as session:
        cid = credentials.caller_id_by_name(session, clerk_ident.user) or credentials.create_caller(
            session, clerk_ident.user, "user", clerk_ident.groups
        )
    return clerk_ident.org_id, cid


def _grant_summary(scope: str) -> str:
    """Plain English for what the person is about to hand over.

    The consent screen has to state the ACTUAL grant. It previously said nothing
    about scope while the token came back unrestricted regardless of what the
    client requested — a consent screen that overstates or understates the grant is
    worse than none, because it is the artefact the person relies on.
    """
    granted = scope.split()
    if not granted:
        return "This client will get full access to the lenses you can reach."
    parts = {
        scopes.READ: "read governed data (query lenses, read definitions)",
        scopes.WRITE: "file corrections against served answers",
    }
    listed = "; ".join(parts[s] for s in scopes.ALL if s in granted)
    return f"This client will be able to: {html.escape(listed)}. Nothing else."


def _client_line(client_name: str, redirect_uri: str) -> str:
    """Name the client and where the code goes — the only on-page defense against a
    consent-phishing link (attacker registers an arbitrary redirect and sends the
    victim a link on the real dst origin). Show the destination host, and flag it
    when it isn't loopback, so a token bound to your key can't leave to an unfamiliar
    host without you seeing it."""
    who = html.escape(client_name) if client_name else "An unnamed client"
    host = urlsplit(redirect_uri).netloc or redirect_uri
    loopback = host.split(":")[0] in {"localhost", "127.0.0.1", "[::1]", "::1"}
    dest = html.escape(host)
    warn = (
        ""
        if loopback
        else '<br><span class="warn">⚠ not a local address — only continue if you '
        "recognize this destination.</span>"
    )
    return (
        f'<p class="who"><b>{who}</b> wants to connect over MCP and will receive a token '
        f"scoped to your key.<br>Authorization code will be sent to: "
        f"<code>{dest}</code>{warn}</p>"
    )


def _consent_html(params: dict[str, str], error: str = "", *, client_name: str = "") -> str:
    """Minimal self-contained consent page (no SPA, no Clerk frontend needed)."""
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{html.escape(v, quote=True)}">'
        for k, v in params.items()
    )
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    grant = _grant_summary(params.get("scope", ""))
    who = _client_line(client_name, params.get("redirect_uri", ""))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect to dst</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    background:#f5f1e8; color:#2b2824; }}
  body, input, button {{ font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; }}
  .card {{ width:100%; max-width:30rem; margin:1.5rem; background:#fffdf8;
    border:1px solid #e4ddcf; border-radius:8px; padding:1.6rem 1.8rem; }}
  .tag {{ font-size:11px; font-weight:600; letter-spacing:.14em;
    text-transform:uppercase; color:#b9742b; }}
  h1 {{ font-size:17px; margin:.5rem 0 .6rem; }}
  p {{ color:#6b6459; margin:.4rem 0; }}
  code {{ color:#2b2824; }}
  input {{ width:100%; box-sizing:border-box; margin-top:.9rem; padding:.6rem .7rem;
    border:1px solid #e4ddcf; border-radius:6px; background:#fff; }}
  button {{ margin-top:.8rem; width:100%; padding:.6rem; border:0; border-radius:6px;
    cursor:pointer; background:#b9742b; color:#fff; font-weight:600; }}
  .err {{ color:#b3261e; }}
  .warn {{ color:#b3261e; }}
  .who code {{ word-break:break-all; }}
  .grant {{ color:#2b2824; background:#f5f1e8; border-left:2px solid #b9742b;
    padding:.5rem .7rem; }}
</style></head><body><div class="card">
  <div class="tag">Connect MCP client</div>
  <h1>Authorize dst access</h1>
  {who}
  <p>Paste a dst caller key (<code>dst_…</code>) or admin token to authorize — the client
  receives its own token; your key is never stored in its config.</p>
  <p class="grant">{grant}</p>
  {err}
  <form method="post" action="/oauth/authorize/complete">
    {hidden}
    <input type="password" name="credential" placeholder="dst_… or dstadm_…"
      autocomplete="off" autofocus required>
    <button type="submit">Authorize</button>
  </form>
</div></body></html>"""


# Plain template (not an f-string) so the JS/CSS braces stay literal; only __TOKENS__ are
# substituted. Loads Clerk's hosted SDK, renders a real sign-in, and on success submits the
# Clerk session token as the credential — the backend resolves it to the person + org.
_CLERK_CONSENT = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in to dst</title>
<style>
  :root { color-scheme: light; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    background:#f5f1e8; color:#2b2824;
    font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; }
  .card { width:100%; max-width:26rem; margin:1.5rem; background:#fffdf8;
    border:1px solid #e4ddcf; border-radius:8px; padding:1.6rem 1.8rem; text-align:center; }
  .tag { font-size:11px; font-weight:600; letter-spacing:.14em;
    text-transform:uppercase; color:#b9742b; }
  h1 { font-size:17px; margin:.5rem 0 .4rem; }
  p { color:#6b6459; margin:.3rem 0 1rem; }
  #signin { display:flex; justify-content:center; }
</style></head><body><div class="card">
  <div class="tag">Connect MCP client</div>
  <h1>Sign in to authorize</h1>
  <p>Grant __CLIENT__ access to dst as you.</p>
  <div id="signin"><p id="status">Loading sign-in…</p></div>
  <form id="grant" method="post" action="/oauth/authorize/complete">
    __HIDDEN__
    <input type="hidden" name="credential" id="credential">
  </form>
  <noscript>JavaScript is required to sign in.</noscript>
</div>
<script async crossorigin="anonymous" data-clerk-publishable-key="__PK__"
  src="https://__HOST__/npm/@clerk/clerk-js@5/dist/clerk.browser.js"
  onload="boot()"></script>
<script>
async function boot() {
  await window.Clerk.load();
  const grant = async () => {
    const token = await window.Clerk.session.getToken();
    document.getElementById('credential').value = token;
    document.getElementById('grant').submit();   // 302 → back to the client (loopback)
  };
  if (window.Clerk.user) {
    document.getElementById('status').textContent = 'Authorizing…';
    grant();
    return;
  }
  window.Clerk.addListener((res) => { if (res.user) grant(); });
  window.Clerk.mountSignIn(document.getElementById('signin'));
}
</script></body></html>"""


# The Clerk sign-in page restricts framing and nothing else, on purpose.
#
# It boots an SDK from Clerk's CDN which then loads further origins of its own
# choosing; a content policy enumerating them would be a guess, and a guess that is
# wrong presents a blank consent page — an authorization flow broken by a header
# nobody can see. So this pins only the three directives whose correctness does not
# depend on what Clerk loads: it cannot be framed, cannot have a <base> injected,
# and cannot instantiate plugins. Everything else is left to the default the page
# has always had. The credential-paste page, whose contents are entirely ours, gets
# the full default policy instead.
_CLERK_CONSENT_CSP = "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"


def _clerk_consent_html(
    params: dict[str, str], publishable_key: str, frontend_host: str, client_name: str
) -> str:
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{html.escape(v, quote=True)}">'
        for k, v in params.items()
    )
    return (
        _CLERK_CONSENT.replace("__HIDDEN__", hidden)
        .replace("__PK__", html.escape(publishable_key, quote=True))
        .replace("__HOST__", html.escape(frontend_host, quote=True))
        .replace("__CLIENT__", html.escape(client_name or "an MCP client"))
    )


@router.get("/oauth/authorize", response_model=None)
async def authorize(
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    response_type: str = "code",
    code_challenge_method: str = "S256",
    state: str = "",
    scope: str = "",
    resource: str = "",
) -> Response:
    """Validate the request, then render the self-contained consent page."""
    if response_type != "code":
        return _oauth_error("unsupported_response_type", "only response_type=code is supported")
    if code_challenge_method != "S256":
        return _oauth_error("invalid_request", "only PKCE S256 is supported")
    registered = oauth.client_redirect_uris(client_id)
    if registered is None:
        return _oauth_error("invalid_client", "unknown client_id", status=401)
    if redirect_uri not in registered:
        return _oauth_error("invalid_request", "redirect_uri not registered for this client")
    try:
        granted = scopes.parse(scope)
    except ValueError as exc:
        return _oauth_error("invalid_scope", str(exc))
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "state": state,
        # Carried through the consent POST so the code, and the token minted from
        # it, grant exactly what the person was shown. Previously `scope` died
        # here and the token came back unrestricted whatever the client asked for.
        "scope": " ".join(granted),
        "resource": resource or "",
    }
    # Real browser sign-in when Clerk is configured; otherwise the credential-paste
    # fallback (self-hosted instances with no IdP).
    name = oauth.client_name(client_id) or ""
    issuer = clerk.issuer()
    if issuer and settings.clerk_publishable_key:
        host = issuer.split("://", 1)[-1]
        return HTMLResponse(
            _clerk_consent_html(
                params, settings.clerk_publishable_key, host, name or "an MCP client"
            ),
            headers={"Content-Security-Policy": _CLERK_CONSENT_CSP},
        )
    # The credential-paste page carries no script and only an inline <style>, which
    # the default policy already allows — it needs no override.
    return HTMLResponse(_consent_html(params, client_name=name))


@router.post("/oauth/authorize/complete", response_model=None)
async def authorize_complete(
    credential: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    code_challenge: str = Form(...),
    state: str = Form(""),
    scope: str = Form(""),
    resource: str = Form(""),
) -> Response:
    """The consent form posts the credential + params; resolve the caller and redirect the
    browser back to the client with a one-time code."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "state": state,
        "scope": scope,
        "resource": resource,
    }
    registered = oauth.client_redirect_uris(client_id)
    if registered is None or redirect_uri not in registered:
        return _oauth_error("invalid_request", "unknown client or redirect_uri")
    # Re-validate rather than trust the round-tripped form field: this endpoint is
    # POST-able directly, so the scope arriving here is caller-controlled input,
    # not something the authorize step can vouch for.
    try:
        granted = scopes.parse(scope)
    except ValueError as exc:
        return _oauth_error("invalid_scope", str(exc))

    ident = _resolve_grant_identity(credential.strip())
    if ident is None:
        return HTMLResponse(
            _consent_html(
                params,
                "That credential wasn't recognized — use a valid dst_ key or admin token.",
                client_name=oauth.client_name(client_id) or "",
            ),
            status_code=401,
        )
    org_id, caller_id = ident
    # THIS is what makes a registration a real client, and what exempts it from
    # retention. Deliberately not the `authorize` GET: that step is anonymous, so
    # marking there would let anyone immunise their own spam registrations against
    # the cap simply by visiting the consent page for each one. Reaching here means
    # a credential dst already trusts authorized this client.
    oauth.mark_client_used(client_id)
    code = oauth.sign_auth_code(
        caller_id=str(caller_id),
        org_id=str(org_id),
        code_challenge=code_challenge,
        redirect_uri=redirect_uri,
        client_id=client_id,
        scopes=granted,
        resource=resource or None,
    )
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        f"{redirect_uri}{sep}{urlencode({'code': code, 'state': state})}", status_code=302
    )


@router.post("/oauth/token")
async def token(
    grant_type: str = Form(...),
    code: str = Form(...),
    redirect_uri: str = Form(...),
    client_id: str = Form(...),
    code_verifier: str = Form(...),
) -> JSONResponse:
    """Authorization code + PKCE verifier → a `dsto_` access token. The code
    burns only after every other check passes, and a reused code revokes what
    its first redemption minted — a leaked code kills its own token."""
    if grant_type != "authorization_code":
        return _oauth_error("unsupported_grant_type", "only authorization_code is supported")
    claims = oauth.verify_auth_code(code)
    if claims is None:
        return _oauth_error("invalid_grant", "authorization code invalid or expired")
    if claims.get("ru") != redirect_uri or claims.get("client") != client_id:
        return _oauth_error("invalid_grant", "code does not match this client/redirect")
    if not oauth.verify_pkce_s256(code_verifier, str(claims.get("cc", ""))):
        return _oauth_error("invalid_grant", "PKCE verification failed")
    # Burn the code AFTER every other check: a request that fails PKCE has not
    # spent the code, and consuming it there would let anyone with a stolen code
    # deny the legitimate client its one redemption.
    if not oauth.claim_auth_code(claims):
        # Reuse means the code leaked. Kill whatever the first redemption minted.
        oauth.revoke_token_from_code(str(claims.get("jti", "")))
        return _oauth_error("invalid_grant", "authorization code already used")

    granted = [str(s) for s in (claims.get("scope") or [])]
    resource = claims.get("resource")
    raw = credentials.mint_oauth_token(
        uuid.UUID(str(claims["org"])),
        uuid.UUID(str(claims["cid"])),
        oauth.OAUTH_TOKEN_TTL,
        scopes=granted,
        resource=str(resource) if resource else None,
    )
    oauth.record_minted_key(str(claims.get("jti", "")), hash_token(raw))
    body: dict[str, Any] = {
        "access_token": raw,
        "token_type": "Bearer",
        "expires_in": int(oauth.OAUTH_TOKEN_TTL.total_seconds()),
    }
    # RFC 6749 §5.1: echo `scope` when it differs from the request. We always echo
    # what was actually granted — a client that assumes it got what it asked for
    # holds a grant it does not have.
    if granted:
        body["scope"] = " ".join(granted)
    return JSONResponse(body, headers={"Access-Control-Allow-Origin": "*"})
