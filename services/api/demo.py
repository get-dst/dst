"""The public demo's front door: GET /demo (sign in, get a key) + POST /auth/demo-key.

Both exist only under DST_DEMO_ORG_ID (services/auth/demo.py) and answer 404
otherwise — an ordinary deployment has no self-serve credential. The page is
server-rendered like the MCP consent page: it boots Clerk's SDK, and on sign-in
trades the session token for a `dst_` caller key through the mint endpoint,
then shows the three ways in (curl, an OpenAI-compatible client, MCP). One live
key per visitor: a re-mint revokes the previous one, so a pasted-around key is
one click from dead.
"""

from __future__ import annotations

import html
import uuid

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from services.api.oauth import _CLERK_CONSENT_CSP
from services.auth import clerk, demo
from services.config import settings
from services.db.session import org_session
from services.governance import credentials, ratelimit
from services.governance.policy import authorize
from services.lenses.store import list_published_for_org

router = APIRouter(tags=["demo"])

# Minting is cheap for us and free for the visitor; the budget exists so a script
# cannot churn keys, and since the caller is the sign-in email the key count per
# person is one whatever the rate. Per source address, like /oauth/register.
_MINT_IP_RPM = 20


def _off() -> HTTPException:
    return HTTPException(status_code=404, detail="not a public demo deployment")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _base(request: Request) -> str:
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


def _lenses_for(visitor: credentials.CallerIdentity) -> dict[str, str | None]:
    """The lenses this visitor may ask, each with an example question: the first
    common question its semantic layer declares, so the page shows a question the
    lens was authored to answer rather than one from some other dataset."""
    out: dict[str, str | None] = {}
    for name, _, _, bundle in list_published_for_org(visitor.org_id):
        if not authorize(visitor, bundle.config)[0]:
            continue
        out[name] = next(
            (q for e in bundle.semantic_model.entities for q in e.common_questions), None
        )
    return out


@router.post("/auth/demo-key", status_code=201)
def demo_key(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, object]:
    """A signed-in visitor's own `dst_` key — the one credential the demo hands
    out. Bearer = the Clerk session token. Revokes the visitor's previous key."""
    if not demo.enabled():
        raise _off()
    key = f"demo-mint-ip:{_client_ip(request)}"
    if not ratelimit.check(key, _MINT_IP_RPM):
        raise HTTPException(
            status_code=429,
            detail="too many key requests from this address — wait a moment",
            headers={"Retry-After": str(ratelimit.retry_after(key))},
        )
    raw = (authorization or "").removeprefix("Bearer ").strip()
    visitor = demo.resolve(raw) if raw else None
    if visitor is None or visitor.caller_id is None:
        raise HTTPException(status_code=401, detail="sign in first — the bearer is your session")
    with org_session(visitor.org_id) as session:
        for existing in credentials.list_keys(session, visitor.name):
            if not existing["revoked"]:
                credentials.revoke_key(session, uuid.UUID(str(existing["id"])))
        minted = credentials.issue_key(
            session, visitor.caller_id, expires_in_days=settings.demo_key_days
        )
    lenses = _lenses_for(visitor)
    return {
        "caller": visitor.name,
        "key": minted,
        "expires_in_days": settings.demo_key_days,
        "lenses": list(lenses),
        "examples": lenses,
        "base_url": _base(request),
    }


# Plain template (not an f-string): the JS/CSS braces stay literal, only __TOKENS__
# are substituted. Same shape and policy as the MCP consent page in oauth.py.
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>dst demo</title>
<style>
  :root { color-scheme: light; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    background:#f5f1e8; color:#2b2824;
    font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; }
  .card { width:100%; max-width:40rem; margin:1.5rem; background:#fffdf8;
    border:1px solid #e4ddcf; border-radius:8px; padding:1.6rem 1.8rem; }
  .tag { font-size:11px; font-weight:600; letter-spacing:.14em;
    text-transform:uppercase; color:#206b4e; }
  h1 { font-size:17px; margin:.5rem 0 .4rem; }
  h2 { font-size:13px; margin:1.2rem 0 .3rem; letter-spacing:.06em; text-transform:uppercase; }
  p { color:#6b6459; margin:.3rem 0 .8rem; }
  pre { background:#f5f1e8; border-left:2px solid #206b4e; padding:.6rem .8rem;
    overflow-x:auto; white-space:pre-wrap; word-break:break-all; margin:.3rem 0 .8rem; }
  code { color:#2b2824; }
  .key { font-weight:600; }
  .fine { font-size:12px; }
  #signin { display:flex; justify-content:center; }
  [hidden] { display:none !important; }
</style></head><body><div class="card">
  <div class="tag">data serve tool · public demo</div>
  <h1>Ask a governed warehouse, from your own AI</h1>
  <p>Sign in to get a key. The key works with curl, any OpenAI-compatible client, and any
  MCP client. Answers come from a lens over a sample warehouse; every answer carries its SQL
  and its verification.</p>
  <div id="signin"><p id="status">Loading sign-in…</p></div>
  <div id="issued" hidden>
    <h2>Your key</h2>
    <pre class="key" id="key"></pre>
    <p class="fine">Shown once. Valid for <span id="days"></span> days, one live key per
    account — coming back here mints a new one and retires this one.</p>
    <h2>curl</h2>
    <pre id="curl"></pre>
    <h2>OpenAI-compatible client</h2>
    <pre id="openai"></pre>
    <h2>MCP</h2>
    <pre id="mcp"></pre>
    <p class="fine">Limits: a per-minute and a per-day budget per account, and a daily cap
    for the whole demo. A refusal says which one, and when it frees. Questions you ask are
    logged with your sign-in email and reviewed to improve the product; ask nothing you
    would not want on a log.</p>
  </div>
  <p id="err" class="fine" hidden></p>
  <noscript>JavaScript is required to sign in.</noscript>
</div>
<script async crossorigin="anonymous" data-clerk-publishable-key="__PK__"
  src="https://__HOST__/npm/@clerk/clerk-js@5/dist/clerk.browser.js"
  onload="boot()"></script>
<script>
const $ = (id) => document.getElementById(id);
async function mint() {
  $('status').textContent = 'Minting your key…';
  const token = await window.Clerk.session.getToken();
  const r = await fetch('/auth/demo-key', {method: 'POST',
    headers: {'Authorization': 'Bearer ' + token}});
  if (!r.ok) {
    $('status').hidden = true;
    $('err').hidden = false;
    $('err').textContent = 'Could not mint a key: ' + r.status + ' ' + (await r.text());
    return;
  }
  const b = await r.json();
  const lens = b.lenses[0] || '<lens>';
  const ask = ((b.examples || {})[lens] || 'What can this lens answer?').replace(/["\\\\]/g, '');
  $('key').textContent = b.key;
  $('days').textContent = b.expires_in_days;
  $('curl').textContent =
    `curl -s ${b.base_url}/v1/lenses/${lens}/query \\\\\n` +
    `  -H 'Authorization: Bearer ${b.key}' -H 'Content-Type: application/json' \\\\\n` +
    `  -d '{"q": "${ask.replace(/'/g, '')}"}'`;
  $('openai').textContent =
    `from openai import OpenAI\n` +
    `client = OpenAI(base_url="${b.base_url}/v1", api_key="${b.key}")\n` +
    `client.chat.completions.create(model="${lens}",\n` +
    `    messages=[{"role": "user", "content": "${ask}"}])`;
  $('mcp').textContent = `${b.base_url}/mcp   (sign in with the same account when the client asks)`;
  $('signin').hidden = true;
  $('issued').hidden = false;
}
async function boot() {
  await window.Clerk.load();
  if (window.Clerk.user) { mint(); return; }
  window.Clerk.addListener((res) => { if (res.user && $('issued').hidden) mint(); });
  window.Clerk.mountSignIn($('signin'));
}
</script></body></html>"""


@router.get("/demo", response_model=None)
def demo_page() -> HTMLResponse:
    """The demo's sign-in page: boots Clerk, mints the visitor's key through
    /auth/demo-key, and shows the three ways in. 404 outside demo mode, 503
    when demo mode is on without Clerk."""
    if not demo.enabled():
        raise _off()
    issuer = clerk.issuer()
    if not issuer or not settings.clerk_publishable_key:
        raise HTTPException(
            status_code=503, detail="demo mode needs Clerk sign-in (DST_CLERK_PUBLISHABLE_KEY)"
        )
    host = issuer.split("://", 1)[-1]
    page = _PAGE.replace("__PK__", html.escape(settings.clerk_publishable_key, quote=True)).replace(
        "__HOST__", html.escape(host, quote=True)
    )
    return HTMLResponse(page, headers={"Content-Security-Policy": _CLERK_CONSENT_CSP})
