"""dst MCP server — exposes governed lenses to AI clients (Claude Desktop, Cursor).

A thin MCP server that proxies to the dst REST data plane with a caller's API key.
Mirrors Omni's pickModel -> pickTopic -> getData:

  list_lenses       ->  the governed lenses this caller may use
  describe_lens     ->  a lens's entities, fields, definitions + certified library size
  search_certified  ->  find human-approved question→SQL pairs (the deterministic path)
  run_certified     ->  run an approved answer with zero AI SQL generation
  query             ->  ask a governed question; returns a grounded, cited answer
  query_metrics     ->  name the metrics/dimensions directly; compiled, no generation

Every tool returns a structured envelope so agents can branch programmatically
success is ``{"ok": true, ...payload}``; failure is ``{"ok": false,
"code": "auth|forbidden|not_found|rate_limited|no_exact_match|upstream|unreachable",
"error": "<human-readable>"}`` — never an error disguised as a successful payload.

Two transports, same tools and same governance:

  * Remote (recommended): mounted on the dst API at ``/mcp`` over streamable-HTTP.
    Each request carries its own ``Authorization: Bearer dst_…`` header, so the caller
    key is per-connection and the deny-by-default policy is enforced exactly as on the
    REST plane. No repo, no ``uv``, no admin token required. See ``app.py``.
  * Local stdio (dev): ``uv run python -m services.mcp.server`` with the key in env.

Config via env (stdio only; remote reads the key from the request header):
  DST_API_KEY   a caller key (dst_…) or admin token (dstadm_…)   [stdio only]
  DST_URL       dst base URL (default http://localhost:8000)

Run (stdio):  uv run python -m services.mcp.server
"""

from __future__ import annotations

import os
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from services.config import resolve_env_ref, settings
from services.contracts.correction import CorrectionKind


def _resolve_base_url() -> str:
    """resolve_env_ref, not a bare os.environ read: the scaffolded project declares
    its URL in .env, which nothing exports — a bare read leaves every MCP tool on a
    non-:8000 install proxying to a dead port."""
    return (resolve_env_ref("DST_URL") or "http://localhost:8000").rstrip("/")


DST_URL = _resolve_base_url()
DST_API_KEY = os.environ.get("DST_API_KEY", "")


def _resolve_instance_name() -> str:
    """The name this deployment answers to in the driver AI's context ("ask watson
    what our ARR is"). Env-shaped like DST_URL — the deploy contract is image +
    env with no project files, and stdio picks the project's .env up the same way.
    The client-side half of the alias is the MCP registration name, which `dst
    init` scaffolds from the same value."""
    return (resolve_env_ref("DST_INSTANCE_NAME") or "").strip() or "dst"


INSTANCE_NAME = _resolve_instance_name()

# The operating manual that ships WITH the tools. MCP returns this `instructions` string
# in its initialize response, so any client (Claude Code/Desktop, Cursor) injects it as
# system context the moment dst connects — zero install, every transport. The tool
# docstrings teach each tool; this teaches the cross-tool discipline no single tool owns:
# when to reach for dst at all, what a lens is, deterministic-first, route-vs-query,
# and the review queue. Keep the *protocol* here; never bake the live lens catalog in
# (that stays behind list_lenses, so a new lens can't make this manual wrong).
# {name} is the instance's chosen name (DST_INSTANCE_NAME): the manual speaks AS the
# name users address, so "check in watson" and the self-description agree.
_GUIDE_TEMPLATE = """\
{name} answers data questions against GOVERNED LENSES — curated, access-controlled views
of this org's warehouse with business definitions built in. Reach for {name} whenever a
question needs real figures from the org's data. Do NOT answer such questions from memory
or hand-written SQL — an ungoverned guess is exactly the failure {name} exists to prevent.

A LENS is the unit of governance: a named scope of tables + typed fields + business
definitions + a library of certified answers. You may use only the lenses your caller key
grants (deny-by-default) — what list_lenses returns is your entire world.

MEANING vs DATA — classify the ask before the loop. A DEFINITIONAL question ("what do we
mean by traded volume?", "how is average purchase price calculated?") wants the governed
MEANING, not a data answer: call lookup_definition(term). It returns the approved
definition verbatim, across every lens you may use, with `cites` — the governed terms the
body depends on; look those up too before answering. No SQL is generated and no warehouse
is touched. Sending a definitional question through query/route_query runs governed SQL
against the warehouse to answer it — the wrong machine for a meaning. If lookup_definition
returns nothing, the term is ungoverned: say so; never invent a definition.

Operating loop:
1. DEFAULT TO route_query(question) — let {name} choose the lens. Lens selection is the
   router's job, not yours: it scores the question against governed coverage you cannot
   see. Never pre-pick a lens because one "seems more suitable" — that swaps your guess
   for {name}'s governed routing, the exact failure to avoid. Target a specific lens only
   when the user named it, or to follow up on the lens route_query already chose.
2. ON A ROUTE — present answer.answer and attribute it: name the lens that answered and
   say {name} routed it (routed_to.lens, routed_to.score). If the score is not high or
   the answer's confidence is low, flag that the match was uncertain and offer
   send_for_review — don't present an uncertain route as settled fact.
3. ON A DECLINE (covered:false) — no governed lens covers this. Say so plainly; do not
   fabricate an answer and do not fall back to hand-picking a lens.
4. PREFER DETERMINISTIC — once the lens is known, describe_lens; if certified_count>0 try
   search_certified / run_certified first (a human-approved question->SQL pair, zero AI
   SQL generation — lead with its trust_summary). Otherwise query(name, question) for
   grounded, cited, scope-enforced SQL with a confidence score. On a certified result,
   `certified_match` says how it matched: 'exact' = the approved question itself — quote
   the number as approved; 'equivalent' = a confirmed paraphrase — mention the approved
   question's wording alongside the number; 'parameterized' = an approved TEMPLATE bound
   with this question's values (certified_provenance.bound_values) — name the values.
   A non-empty `degraded` list means a governance capability did NOT run for that
   answer (e.g. certified matching, with no usable embedder): certification='none'
   then means "nothing was checked", not "no approved answer covers this". Relay the
   line and treat the answer as ungoverned generation.
5. TRUST GATE — when an answer must be relied on, when the route was uncertain, or when you
   believe it is wrong, put it in the REVIEW QUEUE with send_for_review(request_id, note,
   corrected_sql); an AI judge audits it and may escalate to the data team. Filing a
   correction? Name `target` (the definition term it is about) and `kind` — target routes
   the drafted patch and outranks kind; omitting it mistargets. Poll review_status(ticket_id):
   open -> needs_human -> approved | changes_requested | rejected.
6. CLARIFICATIONS — a result carrying `clarification` is NOT a data answer: the question
   hit an ambiguous governed term. Relay clarification.question (options listed) to the
   user, then re-ask with the chosen meaning spelled out in the question.
7. SHAPE THE ANSWER — every ask tool takes `format`. {name} writes the English answer
   with a model, and on a certified answer — where the SQL is approved and no generation
   runs — that write is nearly the whole wait. When you are going to read the numbers and
   phrase the reply yourself — the
   usual case — pass format='structured': same governed SQL, same rows, same citations,
   confidence and trust_summary, no prose and no model call. Keep the 'both' default when
   you want dst's own wording to quote.

Every tool returns an envelope: {ok:true,...payload} or {ok:false,code,error}. Branch on
`ok`; never treat a failure as data.
"""


def build_guide(name: str = "dst") -> str:
    """The operating manual, speaking as *name*. `.replace`, not `.format` — the
    template's envelope examples ({ok:true,...}) are literal braces. A custom name
    gets one identity line so the driver can still connect the alias to dst."""
    guide = _GUIDE_TEMPLATE.replace("{name}", name)
    if name != "dst":
        guide = f'"{name}" is this org\'s dst (data serve tool) instance.\n' + guide
    return guide


DST_GUIDE = build_guide(INSTANCE_NAME)

# stateless_http: each HTTP request is self-contained (it carries its own bearer key),
# so no session id needs to be threaded across calls. streamable_http_path="/" so the
# endpoint is exactly the mount point (we mount the app at /mcp in app.py).
#
# The transport's DNS-rebinding protection allowlists Host headers; its defaults
# only pass localhost forms, so every deployed hostname 421'd (measured).
# The deployment's own origin comes from DST_PUBLIC_BASE_URL — allow its
# host on any port alongside the local dev forms.


def _allowed_hosts() -> list[str]:
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    if settings.public_base_url:
        public_host = urlsplit(settings.public_base_url).hostname
        if public_host:
            hosts += [public_host, f"{public_host}:*"]
    return hosts


mcp = FastMCP(
    INSTANCE_NAME,
    instructions=DST_GUIDE,
    stateless_http=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(allowed_hosts=_allowed_hosts()),
)


class _AuthError(Exception):
    """No caller key available for this request."""


def _resolve_key(ctx: Context | None) -> str:  # type: ignore[type-arg]
    """The caller key for this invocation: the request's bearer header (remote HTTP)
    or, failing that, the DST_API_KEY env var (local stdio)."""
    if ctx is not None:
        request = getattr(ctx.request_context, "request", None)
        header = request.headers.get("authorization") if request is not None else None
        if header and header.lower().startswith("bearer "):
            return str(header[len("bearer ") :]).strip()
    if DST_API_KEY:
        return DST_API_KEY
    raise _AuthError(
        "No dst caller key. Remote clients must send 'Authorization: Bearer dst_…'; "
        "for local stdio set DST_API_KEY in the MCP client config."
    )


def _agent_name(ctx: Context | None) -> str:  # type: ignore[type-arg]
    """The acting client's name for attribution — clientInfo.name when the client
    gave one, else the honest constant "mcp".

    A LABEL, never a security input (the MCP spec: clientInfo is self-reported and
    MUST NOT drive security decisions). `client_params` is not always populated under
    stateless HTTP, so the fallback keeps the agent rail from ever being empty on the
    MCP path — "mcp" says truthfully "an MCP client we couldn't get a name from"."""
    if ctx is not None:
        try:
            info = ctx.request_context.session.client_params.clientInfo
            if info and info.name:
                return str(info.name)[:120]
        except Exception:
            pass
    return "mcp"


def _client(key: str, agent: str = "mcp") -> httpx.AsyncClient:
    # Async is load-bearing, not stylistic: under the remote transport these tools run
    # on the API server's own event loop and call back into the same server. A blocking
    # client would wedge the loop (and with it every request, /health included) until
    # the timeout — a self-deadlock on any single-worker deployment.
    return httpx.AsyncClient(
        base_url=DST_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
            # The acting client, carried to the REST data plane so the trace and audit
            # rows record "person X, through agent Y". get_caller reads it.
            "X-dst-Agent": agent,
        },
        timeout=180.0,
    )


def _explain(exc: httpx.HTTPStatusError) -> str:
    code = exc.response.status_code
    try:
        detail = exc.response.json().get("detail", exc.response.text)
    except Exception:
        detail = exc.response.text
    if code in (401, 403):
        return f"Not authorized ({code}): {detail}. Check your caller key and lens access."
    if code == 404:
        return f"Not found (404): {detail}."
    return f"dst error {code}: {detail}"


# Mirrors CERTIFIED_EXACT in services/api/query.py — the similarity at which a stored
# certified question is treated as the same question. Kept local so the stdio server
# stays a standalone thin client.
CERTIFIED_EXACT = 0.95

# Mirrors the data plane's `format` (services/api/query.py), likewise kept local. It is
# the biggest latency lever an agent has: on the certified door the only model call left
# is the one that writes the English answer, so skipping it removes nearly all of the
# wait — and an agent that is going to read the rows and phrase the reply itself never
# uses that prose anyway.
AnswerFormat = Literal["both", "structured", "prose"]

_CODE_BY_STATUS = {401: "auth", 403: "forbidden", 404: "not_found", 429: "rate_limited"}


def _fail(code: str, error: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": error, **extra}


async def _request(
    ctx: Context | None,  # type: ignore[type-arg]
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> tuple[Any, dict[str, Any] | None]:
    """Call the dst data plane. Returns (payload, None) or (None, failure-envelope)."""
    try:
        key = _resolve_key(ctx)
    except _AuthError as exc:
        return None, _fail("auth", str(exc))
    try:
        async with _client(key, _agent_name(ctx)) as c:
            r = await c.request(method, path, json=json_body, params=params)
            r.raise_for_status()
            return r.json(), None
    except httpx.HTTPStatusError as exc:
        code = _CODE_BY_STATUS.get(exc.response.status_code, "upstream")
        return None, _fail(code, _explain(exc))
    except httpx.HTTPError as exc:
        return None, _fail("unreachable", f"Could not reach dst at {DST_URL}: {exc}")


@mcp.tool()
async def list_lenses(ctx: Context) -> dict[str, Any]:  # type: ignore[type-arg]
    """List the governed dst lenses this caller is permitted to use.

    Each lens is a curated, access-controlled view of data with business context.
    Start here, then call describe_lens to learn a lens's fields before querying.

    Returns {ok, lenses: [{name, display_name, description}]}; on failure
    {ok: false, code, error}.
    """
    payload, failure = await _request(ctx, "GET", "/v1/lenses")
    if failure is not None:
        return failure
    lenses = list(payload)
    if not lenses:
        return {
            "ok": True,
            "lenses": [],
            "note": "your key is valid but has no lens grants — an admin must add "
            "your caller to a lens's allow-list (or set its access to Everyone)",
        }
    return {"ok": True, "lenses": lenses}


@mcp.tool()
async def describe_lens(name: str, ctx: Context) -> dict[str, Any]:  # type: ignore[type-arg]
    """Describe a lens: its entities, typed fields, business definitions, sample
    questions, and the size of its certified library. Use this to learn what a lens
    can answer before calling query.

    If certified_count > 0, prefer search_certified before query — a certified answer
    is human-approved and served deterministically (no AI generation).

    Args:
        name: the lens name from list_lenses.
    """
    payload, failure = await _request(ctx, "GET", f"/v1/lenses/{name}")
    if failure is not None:
        return failure
    return {"ok": True, **payload}


@mcp.tool()
async def lookup_definition(term: str, ctx: Context) -> dict[str, Any]:  # type: ignore[type-arg]
    """Look up what a business term MEANS, across every lens you may use.

    Use this for definitional questions — "what do we count as an active customer",
    "how is traded volume calculated" — and whenever you are about to write SQL,
    a dbt model, or a metric by hand and need the governed meaning. It reads the
    approved definition and returns it verbatim: no SQL is generated, no warehouse
    is touched, nothing is billed. `query`/`route_query` answer questions ABOUT DATA
    and will happily run a 3,000-row scan to answer "what does this term mean"; this
    tool is the cheap door for the meaning itself.

    Each hit carries the governing lens, the definition body, and `cites` — the other
    governed terms that body depends on. Follow those with another lookup: a
    definition is only complete once its citations are read.

    A hit with status "ambiguous" means the term has more than one governed meaning —
    ask the user which one is intended, never pick.

    Args:
        term: the business term or phrase, as the user said it.
    """
    payload, failure = await _request(ctx, "GET", "/v1/definitions", params={"q": term})
    if failure is not None:
        return failure
    hits = (payload or {}).get("definitions") or []
    if not hits:
        return {
            "ok": True,
            "definitions": [],
            "note": f"no governed definition mentions '{term}' in the lenses you may use — "
            "call list_lenses/describe_lens to see what IS governed, and do not "
            "invent a definition",
        }
    return {"ok": True, **payload}


@mcp.tool()
async def search_certified(lens: str, question: str, ctx: Context) -> dict[str, Any]:  # type: ignore[type-arg]
    """Search a lens's certified answers — human-approved question→SQL pairs.

    Call this before query when you need a deterministic, zero-hallucination result:
    a certified answer is served from approved SQL with no AI generation. Returns
    matches with similarity scores and provenance (who approved it, when). A match
    with score >= 0.95 can be run as-is via run_certified; lower scores are related
    questions you may still pin by cert_id if the user confirms. An entry carrying
    `slots` is a TEMPLATE — one approved SQL shape covering a question family;
    run it via run_certified with `bindings` for those slots (sample_bindings
    show the shape and the canonical value grammar).

    Args:
        lens: the lens name from list_lenses.
        question: the user's question, as asked.
    """
    payload, failure = await _request(
        ctx, "GET", f"/v1/lenses/{lens}/certified", params={"q": question}
    )
    if failure is not None:
        return failure
    return {"ok": True, **payload}


@mcp.tool()
async def run_certified(
    lens: str,
    ctx: Context,  # type: ignore[type-arg]
    cert_id: str = "",
    question: str = "",
    bindings: dict[str, str] | None = None,
    format: AnswerFormat = "both",
) -> dict[str, Any]:
    """Run a certified answer exactly as approved — zero AI SQL generation.

    Pass cert_id (from search_certified) to pin an exact answer, or question to
    auto-resolve: it runs only on a >= 0.95 similarity match, otherwise you get the
    near matches back to choose from. The result carries certification='certified',
    certified_match='exact', certified_provenance {cert_id, certified_by,
    certified_at}, and a trust_summary.
    A TEMPLATE answer (its search entry carries `slots`) additionally needs
    `bindings` — slot values in the canonical grammar (date_range: YYYY |
    YYYY-Qn | YYYY-MM | YYYY-MM-DD/YYYY-MM-DD; enum: a listed value). dst
    validates every value and renders typed literals into the approved SQL
    shape; the result carries certified_match='parameterized' with the bound
    values in certified_provenance — name them when presenting the answer.
    When presenting the answer, lead with trust_summary so the user sees it is a
    human-approved, deterministic result.

    Args:
        lens: the lens name from list_lenses.
        cert_id: an exact certified-answer id to run (preferred).
        question: a question to resolve to an exact certified match.
        bindings: slot values for a template answer (from its `slots` spec).
        format: 'structured' returns the approved rows without a written answer and
            skips the model call that writes one — on this door that IS the wait
            (everything else is guard + warehouse). Prefer it unless you want
            dst's wording; trust_summary and provenance come back either way.
    """
    if not cert_id and not question:
        return _fail("not_found", "Pass cert_id or question to run_certified.")
    if not cert_id:
        payload, failure = await _request(
            ctx, "GET", f"/v1/lenses/{lens}/certified", params={"q": question}
        )
        if failure is not None:
            return failure
        matches = list(payload.get("certified", []))
        best = matches[0] if matches else None
        # The server advertises the configured embedder's exact band (bands are
        # embedder-relative); the constant is only the legacy fallback.
        exact_band = float(payload.get("exact_band") or CERTIFIED_EXACT)
        if best is None or (best.get("score") or 0.0) < exact_band:
            return _fail(
                "no_exact_match",
                f"No certified answer matches '{question}' at >= {exact_band} similarity. "
                "Pick one explicitly by cert_id, or fall back to query.",
                near_matches=[
                    {k: m.get(k) for k in ("cert_id", "question", "score")} for m in matches
                ],
            )
        cert_id = str(best["cert_id"])
    payload, failure = await _request(
        ctx,
        "POST",
        f"/v1/lenses/{lens}/certified/{cert_id}/run",
        json_body={"bindings": bindings or {}, "format": format},
    )
    if failure is not None:
        return failure
    return {"ok": True, **payload}


@mcp.tool()
async def query(
    name: str,
    question: str,
    ctx: Context,  # type: ignore[type-arg]
    format: AnswerFormat = "both",
) -> dict[str, Any]:
    """Ask a governed, natural-language question against a NAMED lens.

    Prefer route_query unless the user named the lens (or you are following up on the lens
    route_query already chose) — let dst route rather than choosing a lens yourself.

    dst grounds the question in the lens's semantic model, generates and
    validates SQL (read-only, scope-enforced), runs it, and returns a cited answer
    plus structured fields (sql, rows, definition_used, citations, confidence).
    If the question matches a certified answer, the approved SQL is served instead
    (certification='certified' with provenance + trust_summary — lead with
    trust_summary when presenting such an answer). certified_match says how it
    matched: 'exact' = the approved question itself; 'equivalent' = a confirmed
    paraphrase — mention the approved question's wording alongside the number.
    For an explicitly deterministic path, use search_certified / run_certified first.

    Args:
        name: the lens name from list_lenses.
        question: a natural-language question answerable within the lens's scope.
        format: 'both' (default) returns the written answer AND the rows. 'structured'
            returns the rows, SQL, citations and confidence with no written answer —
            and skips the model call that writes it, which is nearly all of the wait.
            Ask for 'structured' whenever you will read the numbers and phrase the
            reply yourself (the usual case); ask for 'both' when you want dst's
            own wording. 'prose' is the answer without the row payload.
    """
    payload, failure = await _request(
        ctx, "POST", f"/v1/lenses/{name}/query", json_body={"q": question, "format": format}
    )
    if failure is not None:
        return failure
    return {"ok": True, **payload}


@mcp.tool()
async def query_metrics(
    lens: str,
    ctx: Context,  # type: ignore[type-arg]
    metrics: list[str] | None = None,
    dimensions: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    order_by: list[dict[str, Any]] | None = None,
    limit: int | None = None,
    entity: str | None = None,
    grain: str | None = None,
    format: AnswerFormat = "structured",
) -> dict[str, Any]:
    """Ask for named metrics and dimensions directly — no SQL, no generation, no guessing.

    Use this instead of `query` whenever you already know WHICH metric you want, which
    is the usual case for a follow-up: you asked a question, got an answer, and now want
    the same number broken down differently. Naming the metric skips the model call that
    would translate your question back into these same names — it is faster, cheaper, and
    cannot pick the wrong column, because every name is resolved against the lens's
    semantic model and the SQL is built by code.

    Call describe_lens first to see the metric, dimension and field names this lens
    carries. Anything not in that list is an error, not an improvisation.

    Joins are resolved from the lens's declared relationships, so a dimension living on
    another entity just works. A join that would duplicate the metric's rows is REFUSED
    rather than silently inflating the number — if that happens, ask `query` instead and
    let the grounded path handle it.

    Args:
        lens: the lens name from list_lenses.
        metrics: metric names to compute, e.g. ["revenue"]. Omit for a plain listing.
        dimensions: names to group by, e.g. ["region_name"]. Qualify as
            "entity.member" when two entities carry the same name.
        filters: [{"field": "status", "op": "=", "value": "won"}] — op is one of
            =, !=, >, <, >=, <=, in, like.
        order_by: [{"field": "revenue", "dir": "desc"}].
        limit: maximum rows to return.
        grain: for an over-time question, one of hour|day|week|month|quarter|year. The
            entity's time field is bucketed to that period and grouped by it — do not
            also pass the raw time field as a dimension.
        entity: the entity owning the metrics; usually inferred, pass it to disambiguate.
        format: 'structured' (default) returns rows with no written answer. 'both' adds
            dst's own prose, at the cost of a model call.
    """
    body: dict[str, Any] = {"format": format}
    for key, value in (
        ("metrics", metrics),
        ("dimensions", dimensions),
        ("filters", filters),
        ("order_by", order_by),
        ("limit", limit),
        ("entity", entity),
        ("grain", grain),
    ):
        if value is not None:
            body[key] = value
    payload, failure = await _request(ctx, "POST", f"/v1/lenses/{lens}/metrics", json_body=body)
    if failure is not None:
        return failure
    return {"ok": True, **payload}


@mcp.tool()
async def sql(
    lens: str,
    sql: str,
    ctx: Context,  # type: ignore[type-arg]
    limit: int = 20,
) -> dict[str, Any]:
    """Run your OWN read-only SQL inside a lens's scope, and get the rows back.

    For LOOKING at data — five rows to see what a column actually contains, a
    count to check whether a filter is right — when describe_lens told you the
    shape but not the content. For answering a user's question, use query or
    run_certified: those give a grounded, cited, verified answer; this gives you
    rows and nothing else, and you own whatever you conclude from them.

    Governed exactly like generated SQL: SELECT-only, single statement, every
    table and column checked against the lens's allow-list, row-capped, and
    logged to the audit trail with your caller name on it. `SELECT *` is refused
    — name the columns (describe_lens lists them). `truncated: true` means the
    cap cut the result off, so do not read it as the whole story.

    Args:
        lens: the lens name from list_lenses.
        sql: one SELECT statement over that lens's tables.
        limit: rows to return (default 20, max 500).
    """
    payload, failure = await _request(
        ctx, "POST", "/v1/sql", json_body={"lens": lens, "sql": sql, "limit": limit}
    )
    if failure is not None:
        return failure
    return {"ok": True, **payload}


@mcp.tool()
async def route_query(
    question: str,
    ctx: Context,  # type: ignore[type-arg]
    format: AnswerFormat = "both",
) -> dict[str, Any]:
    """Ask a governed question WITHOUT naming a lens — the DEFAULT way to ask.

    Let dst's managed router choose the lens; do NOT pre-select one because it
    "seems more suitable" — lens selection is governed and the router sees coverage you
    don't, so hand-picking substitutes an ungoverned guess for dst's routing. The
    router matches the question against every lens you may use and either routes it to the
    covering lens (running the same governed pipeline as query, stamping the answer with
    which lens answered + a routing score) or DECLINES — an honest "no governed lens
    covers this" rather than an ungoverned guess.

    On a route: {ok: true, covered: true, routed_to: {lens, score}, answer: {...}} —
    present answer.answer to the user and note it came from routed_to.lens.
    On a decline: {ok: true, covered: false, reason, nearest_miss} — there is NO
    governed answer; tell the user the question is uncovered (do not fabricate one).

    Args:
        question: the user's natural-language question, as asked.
        format: as in `query` — 'structured' returns the routed lens's rows with no
            written answer and skips the model call that writes it; the routing
            provenance (routed_to) is unaffected.
    """
    payload, failure = await _request(
        ctx, "POST", "/v1/query", json_body={"q": question, "format": format}
    )
    if failure is not None:
        return failure
    return {"ok": True, **payload}


@mcp.tool()
async def send_for_review(
    request_id: str,
    ctx: Context,  # type: ignore[type-arg]
    note: str | None = None,
    kind: CorrectionKind = "other",
    target: str | None = None,
    corrected_sql: str | None = None,
) -> dict[str, Any]:
    """Send a previous answer for verification review.

    Pass the `request_id` from a `query` result. Returns a review ticket and a
    tracking URL; an AI judge audits the reasoning trace and may escalate to the
    data team. Use this when an answer needs a trust sign-off before you rely on it.
    When you believe the answer is *wrong*, say so: a `note` (and the right SQL if
    you know it) records a correction the data team can turn into a concrete fix.

    Filing a correction routes to a drafted patch, and placement follows `target`
    first, `kind` second. NAME THE TARGET whenever you know it — the definition
    term (or artifact) the correction is about, used verbatim: without it the
    patch drafter falls back to matching your note's vocabulary against every
    definition, which mistargets. This is the same routing the `dst correct`
    CLI requires `--target`/`--kind` for, so an agent files as precisely as a human.

    Args:
        request_id: the request_id returned by a prior query.
        note: what is wrong with the answer (records a correction, not just a flag).
        kind: which kind of wrong — definition | scope | number | freshness | other;
            the drafter routes on it (after `target`).
        target: the definition term (or artifact) this correction is about, used
            verbatim by the drafter (an unknown term drafts a new definition). Set it
            when filing a correction — it outranks `kind` and is what makes the patch
            land in the right place.
        corrected_sql: the SQL you believe is correct, when you have it.
    """
    json_body: dict[str, Any] = {"request_id": request_id}
    if note or corrected_sql:
        correction: dict[str, Any] = {"kind": kind, "note": note or ""}
        if target:
            correction["target"] = target
        if corrected_sql:
            correction["corrected_sql"] = corrected_sql
        json_body["correction"] = correction
    payload, failure = await _request(ctx, "POST", "/v1/reviews", json_body=json_body)
    if failure is not None:
        return failure
    return {"ok": True, **payload}


@mcp.tool()
async def review_status(
    ticket_id: str,
    ctx: Context,  # type: ignore[type-arg]
) -> dict[str, Any]:
    """Check the verdict on a review ticket you submitted with send_for_review.

    Poll this with the `ticket_id` from a send_for_review result to see whether the
    answer has been signed off. `state` is the lifecycle:
      open → needs_human → approved | changes_requested | rejected
    (the AI judge may resolve straight to `approved`). `human_verdict`/`human_reasoning`
    are filled once the data team rules; until then `state` tells you it's still pending.

    Args:
        ticket_id: the rev_… id returned by send_for_review.
    """
    payload, failure = await _request(ctx, "GET", f"/v1/reviews/{ticket_id}")
    if failure is not None:
        return failure
    return {"ok": True, **payload}


@mcp.tool()
async def verify_receipt(
    receipt: dict[str, Any],
    ctx: Context,  # type: ignore[type-arg]
) -> dict[str, Any]:
    """Check an answer receipt: was this really served, by this server, as claimed?

    Every data answer carries a `receipt` block (signed with the deployment's key).
    When you are handed a number with a receipt — from an earlier session, another
    agent, a pasted message — verify it before relying on it. Two deterministic
    checks run server-side: the signature (`valid` / `invalid` / `unsigned` /
    `unkeyed`) and a field-by-field cross-check against the logged trace
    (`mismatches` lists any disagreement). Trust the number only on
    `ok: true`; on `invalid` or mismatches, say so rather than repeating the claim.

    Args:
        receipt: the receipt object exactly as it appeared on the answer.
    """
    payload, failure = await _request(ctx, "POST", "/v1/verify-receipt", json_body=receipt)
    if failure is not None:
        return failure
    return {"ok": True, **payload}


@mcp.prompt()
def getting_started() -> str:
    """A first-session walkthrough for using dst's governed lenses.

    On-demand companion to the always-on server instructions: surfaces in clients as a
    user-invokable prompt (e.g. /mcp__dst__getting_started in Claude Code) that kicks
    off a concrete governed-querying session rather than restating the manual.
    """
    return (
        "Walk me through using dst on a real question. Call list_lenses to see what I "
        "can access, then describe_lens on the most relevant one to learn its fields and "
        "whether it has certified answers. Ask me what I want to know, then take the "
        "deterministic path (search_certified / run_certified) if a certified answer fits, "
        "otherwise query the lens. Present the answer with its citations and confidence, and "
        "offer to send_for_review if I need a trust sign-off."
    )


def main() -> None:
    """Local stdio entry point (dev). Remote transport is mounted in app.py."""
    mcp.run()


if __name__ == "__main__":
    main()
