"""The public demo's front door: GET /demo (sign in, connect an AI) + POST /auth/demo-key,
and GET /SKILL.md, the file a visitor's AI reads to connect itself.

The first two exist only under DST_DEMO_ORG_ID (services/auth/demo.py) and answer
404 otherwise — an ordinary deployment has no self-serve credential. The page is
server-rendered like the MCP consent page: it boots Clerk's SDK, and on sign-in
trades the session token for a `dst_` caller key through the mint endpoint,
which also returns the copy-and-paste setup for each AI client. One live key
per visitor: a re-mint revokes the previous one, so a pasted-around key is one
click from dead. /SKILL.md is every deployment's: the same client setup, with
the key left as a placeholder, in a file an agent follows.

The page is for the people the demo serves, not for the engineer behind it:
its generic wording avoids the product's own vocabulary, and everything
specific to the deployment (the tagline, an example conversation, the privacy
link) comes from the project's dst.yaml ``demo:`` section
(services/project/demo_page.py). What people can ask comes from the published
topics themselves.
"""

from __future__ import annotations

import html
import json
import logging
import re
import shlex
import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from services.api.oauth import _CLERK_CONSENT_CSP, CLERK_MOUNT_SIGNIN
from services.auth import clerk, demo
from services.config import instance_name, settings
from services.db.session import org_session
from services.governance import credentials, ratelimit
from services.governance.policy import authorize
from services.lenses.store import LensBundle, list_published_for_org
from services.project import demo_page
from services.project.schema import DemoConfig, DemoTurn

log = logging.getLogger("dst.demo")
router = APIRouter(tags=["demo"])

# Minting is cheap for us and free for the visitor; the budget exists so a script
# cannot churn keys, and since the caller is the sign-in email the key count per
# person is one whatever the rate. Per source address, like /oauth/register.
_MINT_IP_RPM = 20

# The page's one sentence when the project declares no `demo.tagline`.
TAGLINE = "Ask your questions from Claude, ChatGPT or any AI you use."

# More topics than this fold under a "more" line: the list is a taste, not a menu.
_ASKS_SHOWN = 4


def _off() -> HTTPException:
    return HTTPException(status_code=404, detail="not a public demo deployment")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _base(request: Request) -> str:
    return (settings.public_base_url or str(request.base_url)).rstrip("/")


def _example(bundle: LensBundle) -> str | None:
    """The first common question the lens's semantic layer declares: a question it
    was authored to answer, never one from some other dataset."""
    return next((q for e in bundle.semantic_model.entities for q in e.common_questions), None)


def _lenses_for(visitor: credentials.CallerIdentity) -> dict[str, str | None]:
    """The lenses this visitor may ask, each with its example question."""
    return {
        name: _example(bundle)
        for name, _, _, bundle in list_published_for_org(visitor.org_id)
        if authorize(visitor, bundle.config)[0]
    }


def _asks() -> list[tuple[str, str | None]]:
    """``(display name, example question)`` for every published lens a demo
    visitor is authorized for. All of it comes from the lens files."""
    visitor = credentials.CallerIdentity(
        org_id=demo.org_id(), name="visitor", is_admin=False, groups=[demo.GROUP]
    )
    try:
        published = list_published_for_org(visitor.org_id)
    except Exception:  # noqa: BLE001 — the list informs; it must never take the page down
        log.exception("demo page: listing the demo's lenses failed")
        return []
    return [
        (bundle.config.display_name or bundle.config.name, _example(bundle))
        for _name, _, _, bundle in published
        if authorize(visitor, bundle.config)[0]
    ]


def _page_config() -> DemoConfig | None:
    """The project's `demo:` section, or None — the page renders without it."""
    try:
        with org_session(demo.org_id()) as session:
            return demo_page.load(session)
    except Exception:  # noqa: BLE001 — optional content must never take the page down
        log.exception("demo page: reading the `demo:` section failed")
        return None


# ── rendered fragments ────────────────────────────────────────────────────────


def _text(value: str) -> str:
    return html.escape(value.strip()).replace("\n", "<br>")


def asks_html(asks: list[tuple[str, str | None]]) -> str:
    """One line per topic: its name and one question it answers."""

    def items(rows: list[tuple[str, str | None]]) -> str:
        return "".join(
            f'<li><span class="ln">{html.escape(name)}</span>'
            f'<span class="q">{html.escape(ask or "")}</span></li>'
            for name, ask in rows
        )

    if not asks:
        return ""
    shown, rest = asks[:_ASKS_SHOWN], asks[_ASKS_SHOWN:]
    more = (
        f'<details class="more"><summary>{len(rest)} more</summary>'
        f'<ul class="asks">{items(rest)}</ul></details>'
        if rest
        else ""
    )
    return (
        '<section class="asks-box"><h2 class="h2">What you can ask</h2>'
        f'<ul class="asks">{items(shown)}</ul>{more}</section>'
    )


def _turn_html(turn: DemoTurn) -> str:
    if turn.user is not None:
        return f'<div class="u">{_text(turn.user)}</div>'
    if turn.tool is not None:
        return f'<div class="tool">{_text(turn.tool)}</div>'
    if turn.assistant is not None:
        return f'<div class="a">{_text(turn.assistant)}</div>'
    lines = "".join(
        f"<span><b>{html.escape(k)}</b> · {html.escape(v)}</span>"
        for k, v in (turn.receipt or {}).items()
    )
    return f'<div class="rc">{lines}</div>'


def example_html(turns: list[DemoTurn]) -> str:
    """The project's example conversation as the visitor's own AI would show it,
    folded: most visitors came to connect, not to read."""
    if not turns:
        return ""
    return (
        '<details class="more example" id="example" hidden>'
        "<summary>See an example conversation</summary>"
        '<div class="chat" aria-label="Example conversation">'
        '<div class="t">In your AI · example</div>'
        + "".join(_turn_html(t) for t in turns)
        + '<div class="src">Your AI\'s wording will differ.</div></div></details>'
    )


def limits(cfg: DemoConfig | None, *, you: str = "you", your: str = "your") -> list[str]:
    """The demo's limits and what is logged, as sentences: the fine print on the
    page (addressing the visitor) and the file an agent reads (addressing the
    person the agent works for) state the same facts."""
    kept = f" for {cfg.log_days} days" if cfg and cfg.log_days else ""
    return [
        "Each account has a per-minute and a per-day allowance, and the whole demo a daily "
        "cap; a refusal says which one, and when it frees.",
        f"Questions are logged with {your} sign-in email{kept} and reviewed to improve the "
        f"product, so ask nothing {you} would not want on a log.",
    ]


def credit(name: str) -> str | None:
    """A deployment with its own name credits the tool underneath."""
    return "answers by dst (data serve tool)" if name != "dst" else None


def fine_html(cfg: DemoConfig | None, name: str) -> str:
    """Limits, what is logged, the operator's privacy notice, and the credit."""
    tail = []
    if cfg and cfg.privacy_url:
        tail.append(f'<a href="{html.escape(cfg.privacy_url, quote=True)}">Privacy</a>')
    if by := credit(name):
        tail.append(by)
    return (
        '<p class="fine">'
        + " ".join(limits(cfg))
        + (" " + " · ".join(tail) if tail else "")
        + "</p>"
    )


# ── what the mint hands back: one setup per AI client ─────────────────────────


def _server_name(name: str) -> str:
    """The instance name as a config key every client accepts."""
    return re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-") or "dst"


def connect_snippets(
    base: str,
    name: str,
    key: str,
    ask: str | None,
    *,
    account: str = "the account you used here",
) -> list[dict[str, Any]]:
    """Copy-and-paste setup for each AI client, as each vendor documents adding a
    remote MCP server today (the ``doc`` link on every entry). ``auth`` says what
    the client signs in with: clients whose connectors take no static key (Claude
    on the web, ChatGPT) go through the MCP OAuth flow with ``account`` — "here"
    on the page that shows them, spelled out in the file an agent reads."""
    url = f"{base}/mcp"
    server = _server_name(name)
    env = f"{server.upper().replace('-', '_')}_API_KEY"
    then = f"Then ask, for example: `ask {name}: {ask}`" if ask else f"Then ask `{name}` anything."
    return [
        {
            "id": "claude",
            "label": "Claude",
            "auth": "oauth",
            "code": url,
            "steps": [
                "In Claude or Claude Desktop, open Customize → Connectors, click + and "
                "choose Add custom connector.",
                f"Name it `{server}`, paste this URL, click Add, then Connect and sign in "
                f"with {account}.",
                then,
            ],
            "note": "Free plans allow one custom connector. On Team and Enterprise an owner "
            "adds it first, under Organization settings → Connectors.",
            "doc": "https://support.claude.com/en/articles/"
            "11175166-get-started-with-custom-connectors-using-remote-mcp",
        },
        {
            "id": "claude-code",
            "label": "Claude Code",
            "auth": "either",
            "code": f"claude mcp add --transport http {server} {url} \\\n"
            f'  --header "Authorization: Bearer {key}"',
            "steps": ["Run this in a terminal.", f"Start `claude`. {then}"],
            "note": "Or leave out the --header line and run /mcp inside Claude Code to "
            f"sign in with {account} instead.",
            "doc": "https://code.claude.com/docs/en/mcp",
        },
        {
            "id": "codex",
            "label": "Codex",
            "auth": "key",
            "code": f"export {env}={key}\n"
            f"codex mcp add {server} --url {url} --bearer-token-env-var {env}",
            "steps": [
                "Run this in a terminal.",
                "Keep the export line in your shell profile: Codex reads the key from it "
                "each time it starts.",
                f"Start `codex`. {then}",
            ],
            "doc": "https://developers.openai.com/codex/mcp",
        },
        {
            "id": "chatgpt",
            "label": "ChatGPT",
            "auth": "oauth",
            "code": url,
            "steps": [
                "On chatgpt.com, open Settings → Security and login and turn on Developer mode.",
                f"Go to chatgpt.com/plugins, click +, name it `{server}`, paste this URL "
                f"under Connection with OAuth, and sign in with {account}.",
                f"In a chat, choose Developer mode from the + menu. {then}",
            ],
            "note": "Developer mode is on the web, for Plus, Pro, Business, Enterprise and "
            "Education accounts.",
            "doc": "https://developers.openai.com/api/docs/guides/developer-mode",
        },
        {
            "id": "cursor",
            "label": "Cursor",
            "auth": "key",
            "code": json.dumps(
                {
                    "mcpServers": {
                        server: {"url": url, "headers": {"Authorization": f"Bearer {key}"}}
                    }
                },
                indent=2,
            ),
            "steps": [
                "Put this in ~/.cursor/mcp.json, or add the entry under mcpServers if the "
                "file already exists.",
                f"Open Cursor's chat. {then}",
            ],
            "doc": "https://cursor.com/docs/mcp",
        },
    ]


def other_snippets(base: str, key: str, lens: str, ask: str | None) -> list[dict[str, Any]]:
    """The same key without an AI client: plain HTTP, and any OpenAI SDK."""
    question = ask or "What can I ask about?"
    return [
        {
            "id": "curl",
            "label": "curl",
            "code": f"curl -s {base}/v1/query \\\n"
            f"  -H 'Authorization: Bearer {key}' -H 'Content-Type: application/json' \\\n"
            f"  -d {shlex.quote(json.dumps({'q': question}, ensure_ascii=False))}",
        },
        {
            "id": "openai",
            "label": "OpenAI-compatible client (Python)",
            "code": "from openai import OpenAI\n"
            f'client = OpenAI(base_url="{base}/v1", api_key="{key}")\n'
            f'client.chat.completions.create(model="dst/{lens}",\n'
            f'    messages=[{{"role": "user", "content": {json.dumps(question)}}}])',
        },
    ]


# ── the file an agent reads: GET /SKILL.md ────────────────────────────────────
#
# A visitor hands their AI one line ("Connect me to roshan: https://…/SKILL.md —
# my key: dst_…"); the agent fetches the file and does the setup a person would
# otherwise copy from the page, client by client, from the same snippets. Every
# deployment serves it (the demo adds its topics and limits), and it is public: it
# holds nothing a stranger may not read, and the key rides in the line, never in
# the file.

skill_router = APIRouter(tags=["meta"])

SKILL_PATH = "/SKILL.md"
KEY_PLACEHOLDER = "<KEY>"


def agent_line(base: str, name: str, key: str | None = None) -> str:
    """The one line a person pastes into their AI."""
    line = f"Connect me to {name}: {base}{SKILL_PATH}"
    return f"{line} — my key: {key}" if key else line


def render_skill(
    base: str,
    name: str,
    cfg: DemoConfig | None,
    asks: list[tuple[str, str | None]],
    *,
    demo: bool,
) -> str:
    """The whole file from its inputs — pure, like render_page. Written for the
    agent reading it: what to run for its client, then what it may ask."""
    ask = next((q for _, q in asks if q), None)
    account = (
        f"the account the person signed in with at {base}/demo"
        if demo
        else f"the person's account on {name}"
    )
    items = connect_snippets(base, name, KEY_PLACEHOLDER, ask, account=account)

    def labels(auth: str) -> str:
        return " and ".join(i["label"] for i in items if i["auth"] == auth)

    key_from = (
        f"The key is the `my key:` part of the line the person gave you. They got it by "
        f"signing in at {base}/demo, and can get a new one there."
        if demo
        else "The key is the `my key:` part of the line the person gave you. Without one, "
        f"ask the operator of {name} for a key."
    )
    out = [f"# {name}", ""]
    if demo:
        out += [cfg.tagline if cfg and cfg.tagline else TAGLINE, ""]
    out += [
        f"This file is for the AI reading it. Connect yourself to {name} with the steps for "
        "your client below, then ask it the person's questions.",
        "",
        f"MCP URL: {base}/mcp",
        "",
        "## The key",
        "",
        f"- {labels('oauth')} sign in through OAuth and need no key.",
        f"- {labels('either')} takes the key, or signs in through OAuth without it.",
        f"- {labels('key')} take the key.",
        f"- {key_from} Put it where a step says `{KEY_PLACEHOLDER}`.",
        "- Never paste the key anywhere public: not in a repository, an issue, a shared chat, "
        "or a message to anyone but the person it belongs to.",
        "",
    ]
    for item in items:
        out += [f"## {item['label']}", "", "```", item["code"], "```", ""]
        out += [f"{n}. {step}" for n, step in enumerate(item["steps"], 1)]
        note = f"{item['note']} " if item.get("note") else ""
        out += ["", f"{note}Docs: {item['doc']}", ""]
    out += [
        "## How to ask",
        "",
        f"Ask {name} by name, in plain words: `ask {name}: {ask or '<the question>'}`.",
    ]
    if asks:
        out += ["", "The topics, each with a question it answers:", ""]
        out += [f"- {topic}" + (f" — {q}" if q else "") for topic, q in asks]
    if demo:
        privacy = f" Privacy notice: {cfg.privacy_url}" if cfg and cfg.privacy_url else ""
        facts = " ".join(limits(cfg, you="they", your="the person's"))
        out += ["", "## Limits", "", facts + privacy]
    if by := credit(name):
        out += ["", f"{by[0].upper()}{by[1:]}."]
    return "\n".join(out) + "\n"


@skill_router.get(SKILL_PATH, response_model=None)
def skill_md(request: Request) -> Response:
    """The file an agent reads to connect itself: the deployment's name, its MCP
    URL, the setup for each AI client, and in demo mode what may be asked and
    under which limits. Public, unauthenticated, cached for five minutes; no key
    is in it. Outside demo mode it needs DST_PUBLIC_BASE_URL, and answers 404
    with the reason without it."""
    if demo.enabled():
        body = render_skill(_base(request), instance_name(), _page_config(), _asks(), demo=True)
    elif settings.public_base_url:
        body = render_skill(
            settings.public_base_url.rstrip("/"), instance_name(), None, [], demo=False
        )
    else:
        raise HTTPException(
            status_code=404,
            detail="no public address to point clients at — set DST_PUBLIC_BASE_URL "
            f"to serve {SKILL_PATH}",
        )
    return Response(
        body, media_type="text/markdown", headers={"Cache-Control": "public, max-age=300"}
    )


@router.post("/auth/demo-key", status_code=201)
def demo_key(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, object]:
    """A signed-in visitor's own `dst_` key — the one credential the demo hands
    out — with each AI client's setup around it. Bearer = the Clerk session
    token. Revokes the visitor's previous key."""
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
    base = _base(request)
    first = next(iter(lenses), "")
    ask = lenses.get(first) if first else None
    return {
        "caller": visitor.name,
        "key": minted,
        "expires_in_days": settings.demo_key_days,
        "lenses": list(lenses),
        "examples": lenses,
        "base_url": base,
        "agent_line": agent_line(base, instance_name(), minted),
        "connect": connect_snippets(base, instance_name(), minted, ask),
        "other": other_snippets(base, minted, first or "<topic>", ask),
    }


# ── the page ──────────────────────────────────────────────────────────────────
#
# Plain templates (not f-strings): the JS/CSS braces stay literal, only __TOKENS__
# are substituted. _PAGE is the layout and its CSS; _SCRIPT is the behaviour, and
# reaches the layout only through the element ids below. Same policy as the MCP
# consent page in oauth.py.
#
#   #connect     always: the one line for an agent (#line, #copyline, #linenote), and
#                #byhand, the folded per-client setup (#tabs + #panels), shown after sign-in
#   #signin-box  shown before sign-in: the heading, #status and the #signin mount
#   #issued      shown after: #key #copykey #days, #other
#   #example     the folded example conversation (absent without one), shown after
#   #err         a failed mint

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__NAME__ · demo</title>
<style>
  :root { color-scheme: light;
    --paper:#faf9f6; --paper-2:#f3f1ec; --card:#ffffff; --ink:#1a1917; --muted:#6f6a64;
    --rule:#dddbd6; --green:#206b4e; --chat:#1b1a17; --chat-ink:#e9e4da;
    --chat-muted:#9b9387; --chat-rule:#34302b;
    --sans:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
    --mono:"IBM Plex Mono",ui-monospace,"SF Mono",Menlo,monospace; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--paper); color:var(--ink); font:14px/1.55 var(--sans); }
  main { max-width:640px; margin:0 auto; padding:clamp(24px,5vw,48px) 16px 48px;
    display:flex; flex-direction:column; gap:22px; }
  h1, h2, p { margin:0; }
  .eyebrow { font:500 11px var(--mono); letter-spacing:.14em; text-transform:uppercase;
    color:var(--green); }
  .name { font:600 30px/1.1 var(--mono); letter-spacing:-.02em; margin:.3rem 0 .45rem;
    overflow-wrap:anywhere; }
  .lede { font-size:16px; line-height:1.5; max-width:46ch; }
  .muted { color:var(--muted); }
  .h2 { font:600 12px var(--mono); letter-spacing:.1em; text-transform:uppercase;
    margin:0 0 .6rem; }
  #signin { max-width:420px; }
  #status { color:var(--muted); font-size:13px; }
  .tabs { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; }
  .tab { font:500 12.5px var(--mono); padding:5px 10px; border:1px solid var(--rule);
    border-radius:999px; background:var(--card); color:var(--muted); cursor:pointer; }
  .tab[aria-selected="true"] { background:var(--ink); color:var(--paper); border-color:var(--ink); }
  .tab:focus-visible, .copy:focus-visible, summary:focus-visible {
    outline:2px solid var(--green); outline-offset:2px; }
  .snip { position:relative; background:var(--card); border:1px solid var(--rule);
    border-radius:8px; }
  .snip pre { margin:0; padding:12px 64px 12px 14px; font:13px/1.55 var(--mono);
    white-space:pre-wrap; overflow-wrap:anywhere; }
  .copy { font:500 11.5px var(--mono); padding:4px 9px; border-radius:6px;
    background:var(--green); color:#fff; border:0; cursor:pointer; }
  .snip .copy { position:absolute; top:9px; right:9px; }
  .steps { margin:.5rem 0 0; padding-left:1.1rem; color:var(--muted); font-size:13.5px; }
  .steps li { margin:.15rem 0; }
  .steps code { font:12.5px var(--mono); color:var(--ink); background:var(--paper-2);
    padding:1px 5px; border-radius:4px; overflow-wrap:anywhere; }
  .note { font-size:12px; color:var(--muted); margin-top:.4rem; }
  .note a, .fine a { color:var(--green); }
  .keyrow { display:flex; flex-wrap:wrap; align-items:center; gap:8px 12px; font:13px var(--mono); }
  .keyrow .k { background:var(--paper-2); border:1px solid var(--rule); border-radius:6px;
    padding:4px 9px; }
  #other h3 { font:500 12px var(--mono); margin:12px 0 6px; color:var(--muted); }
  .asks { list-style:none; margin:0; padding:0; }
  .asks li { display:grid; grid-template-columns:8.5rem 1fr; gap:10px; padding:7px 0;
    border-top:1px solid var(--rule); font-size:13.5px; }
  .asks li:last-child { border-bottom:1px solid var(--rule); }
  .asks .ln { font:500 12px var(--mono); padding-top:1px; overflow-wrap:anywhere; }
  .asks .q { color:var(--muted); }
  details.more { padding-top:10px; }
  details.more summary { cursor:pointer; font:500 12.5px var(--mono); color:var(--green);
    list-style:none; }
  details.more summary::-webkit-details-marker { display:none; }
  details.more > .asks, details.more > .chat, details.more > .tabs, #other { margin-top:10px; }
  .chat { background:var(--chat); color:var(--chat-ink); border-radius:10px;
    padding:16px 16px 14px; font:13px/1.55 var(--mono); display:flex; flex-direction:column;
    gap:9px; }
  .chat .t { font:500 10.5px var(--mono); letter-spacing:.12em; text-transform:uppercase;
    color:var(--chat-muted); border-bottom:1px solid var(--chat-rule); padding-bottom:8px;
    margin-bottom:2px; }
  .chat .u { align-self:flex-end; max-width:86%; background:#2b2823; padding:8px 11px;
    border-radius:10px 10px 2px 10px; }
  .chat .tool { font-size:11.5px; color:var(--chat-muted); border-left:2px solid #4f9a7a;
    padding-left:9px; }
  .chat .a { max-width:94%; }
  .chat .rc { border:1px solid var(--chat-rule); border-radius:6px; padding:7px 10px;
    font-size:11px; color:var(--chat-muted); display:grid; gap:2px; }
  .chat .rc b { color:#8fc7ae; font-weight:500; }
  .chat .src { font-size:10.5px; color:var(--chat-muted); }
  .fine { font-size:12px; color:var(--muted); max-width:70ch; }
  [hidden] { display:none !important; }
  @media (max-width:560px) { .asks li { grid-template-columns:1fr; gap:2px; } }
</style></head><body><main>
  <header>
    <div class="eyebrow">Public demo</div>
    <h1 class="name">__NAME__</h1>
    <p class="lede">__TAGLINE__</p>
  </header>
  <section id="connect">
    <h2 class="h2">Connect your AI</h2>
    <div class="snip"><pre id="line">__LINE__</pre>
      <button class="copy" id="copyline" type="button">Copy</button></div>
    <p class="note" id="linenote">Paste this into the AI you use: it reads the file and connects
    itself. Sign in below and your key is added to the line.</p>
    <details class="more" id="byhand" hidden><summary>Or connect by hand</summary>
      <div class="tabs" id="tabs" role="tablist" aria-label="Your AI"></div>
      <div id="panels"></div></details>
  </section>
  <section id="signin-box">
    <h2 class="h2">Sign in for your key</h2>
    <p id="status">Loading sign-in…</p>
    <div id="signin"></div>
  </section>
  <section id="issued" hidden>
    <div class="keyrow"><span class="muted">Your key</span><span class="k" id="key"></span>
      <button class="copy" id="copykey" type="button">Copy</button>
      <span class="muted">valid <span id="days"></span> days · for curl, scripts and the
      OpenAI-compatible API</span></div>
    <p class="note">One live key per account: signing in here again makes a new one and
    retires this one.</p>
    <details class="more"><summary>Other ways in</summary><div id="other"></div></details>
  </section>
  <p id="err" class="fine" hidden></p>
  __ASKS__
  __EXAMPLE__
  __FINE__
  <noscript>JavaScript is required to sign in.</noscript>
</main>
__SCRIPT__
</body></html>"""

_SCRIPT = """<script async crossorigin="anonymous" data-clerk-publishable-key="__PK__"
  src="https://__HOST__/npm/@clerk/clerk-js@5/dist/clerk.browser.js"
  onload="boot()"></script>
<script>
const $ = (id) => document.getElementById(id);
const make = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};
function copyButton(button, text) {  // text: a string, or a function read at click time
  button.addEventListener('click', async () => {
    const value = typeof text === 'function' ? text() : text;
    try { await navigator.clipboard.writeText(value); button.textContent = 'Copied'; }
    catch (e) { button.textContent = 'Select to copy'; }
    setTimeout(() => { button.textContent = 'Copy'; }, 1600);
  });
  return button;
}
function snippet(code) {
  const box = make('div', 'snip');
  box.append(make('pre', '', code), copyButton(make('button', 'copy', 'Copy'), code));
  return box;
}
function withCode(text) {           // `x` in a step renders as code
  const li = make('li');
  text.split('`').forEach((part, i) => li.append(i % 2 ? make('code', '', part) : part));
  return li;
}
function renderConnect(items) {
  const tabs = [], panels = [];
  const select = (i) => items.forEach((_, j) => {
    tabs[j].setAttribute('aria-selected', String(i === j));
    tabs[j].tabIndex = i === j ? 0 : -1;
    panels[j].hidden = i !== j;
  });
  items.forEach((item, i) => {
    const tab = make('button', 'tab', item.label);
    tab.type = 'button'; tab.id = 'tab-' + item.id;
    tab.setAttribute('role', 'tab'); tab.setAttribute('aria-controls', 'panel-' + item.id);
    tab.addEventListener('click', () => select(i));
    tab.addEventListener('keydown', (ev) => {
      const step = {ArrowRight: 1, ArrowLeft: -1}[ev.key];
      if (step) { const k = (i + step + items.length) % items.length; select(k); tabs[k].focus(); }
    });
    const panel = make('div', 'panel');
    panel.id = 'panel-' + item.id;
    panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', tab.id);
    const steps = make('ol', 'steps');
    item.steps.forEach((s) => steps.append(withCode(s)));
    const note = make('p', 'note', item.note ? item.note + ' ' : '');
    const doc = make('a', '', item.label + ' docs');
    doc.href = item.doc; doc.rel = 'noopener';
    note.append(doc);
    panel.append(snippet(item.code), steps, note);
    tabs.push(tab); panels.push(panel);
    $('tabs').append(tab); $('panels').append(panel);
  });
  select(0);
}
copyButton($('copyline'), () => $('line').textContent);
let minting = false;
async function mint() {
  if (minting) return;       // Clerk's listener fires again while the first mint runs
  minting = true;
  $('status').hidden = false;
  $('status').textContent = 'Getting your key…';
  const token = await window.Clerk.session.getToken();
  const r = await fetch('/auth/demo-key', {method: 'POST',
    headers: {'Authorization': 'Bearer ' + token}});
  if (!r.ok) {
    $('status').hidden = true;
    $('err').hidden = false;
    $('err').textContent = 'Could not get a key: ' + r.status + ' ' + (await r.text());
    minting = false;
    return;
  }
  const b = await r.json();
  $('line').textContent = b.agent_line;
  $('linenote').textContent = 'Paste this into the AI you use: it reads the file and ' +
    'connects itself with your key.';
  renderConnect(b.connect);
  $('byhand').hidden = false;
  $('key').textContent = b.key.slice(0, 8) + '…' + b.key.slice(-4);
  copyButton($('copykey'), b.key);
  $('days').textContent = b.expires_in_days;
  b.other.forEach((o) => $('other').append(make('h3', '', o.label), snippet(o.code)));
  $('signin-box').hidden = true;
  $('issued').hidden = false;
  if ($('example')) $('example').hidden = false;
}
async function boot() {
  await window.Clerk.load({appearance: {
    variables: {colorPrimary: '#206b4e', colorText: '#1a1917', borderRadius: '8px',
      fontFamily: '"IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif'},
    layout: {socialButtonsVariant: 'blockButton', socialButtonsPlacement: 'top'},
    elements: {cardBox: {boxShadow: 'none', border: '1px solid #dddbd6'},
      header: {display: 'none'}},
  }});
  if (window.Clerk.user) { mint(); return; }
  window.Clerk.addListener((res) => { if (res.user && $('issued').hidden) mint(); });
  $('status').hidden = true;
  mountSignIn($('signin'));
}
__MOUNT_SIGNIN_FN__
</script>"""


@router.get("/demo", response_model=None)
def demo_page_view(request: Request) -> HTMLResponse:
    """The demo's sign-in page: the one line for the visitor's AI, then Clerk
    sign-in, which mints the visitor's key through /auth/demo-key and shows how
    to connect each AI client by hand. 404 outside demo mode, 503 when demo mode
    is on without Clerk."""
    if not demo.enabled():
        raise _off()
    issuer = clerk.issuer()
    if not issuer or not settings.clerk_publishable_key:
        raise HTTPException(
            status_code=503, detail="demo mode needs Clerk sign-in (DST_CLERK_PUBLISHABLE_KEY)"
        )
    return HTMLResponse(
        render_page(
            _page_config(),
            _asks(),
            base=_base(request),
            publishable_key=settings.clerk_publishable_key,
            frontend_host=issuer.split("://", 1)[-1],
        ),
        headers={"Content-Security-Policy": _CLERK_CONSENT_CSP},
    )


def render_page(
    cfg: DemoConfig | None,
    asks: list[tuple[str, str | None]],
    *,
    base: str,
    publishable_key: str,
    frontend_host: str,
) -> str:
    """The whole page from its inputs — pure, so a template test needs no server."""
    name = instance_name()
    script = (
        _SCRIPT.replace("__PK__", html.escape(publishable_key, quote=True))
        .replace("__HOST__", html.escape(frontend_host, quote=True))
        .replace("__MOUNT_SIGNIN_FN__", CLERK_MOUNT_SIGNIN)
    )
    slots = {
        "NAME": html.escape(name),
        "TAGLINE": html.escape(cfg.tagline if cfg and cfg.tagline else TAGLINE),
        "LINE": html.escape(agent_line(base, name)),
        "ASKS": asks_html(asks),
        "EXAMPLE": example_html(cfg.example if cfg else []),
        "FINE": fine_html(cfg, name),
        "SCRIPT": script,
    }
    # One pass, so text the project supplied is never itself scanned for a slot.
    return re.sub(r"__(NAME|TAGLINE|LINE|ASKS|EXAMPLE|FINE|SCRIPT)__", lambda m: slots[m[1]], _PAGE)
