# API

One FastAPI app (`services/app.py`), three surfaces on one port: the **data plane**
(`/v1`, caller keys: asking questions), the **control plane** (`/mgmt`, admin auth:
managing the install), and the governed **MCP** door (`/mcp`). A running server serves
its own interactive OpenAPI at `/docs`.

## Authentication

Bearer tokens in the `Authorization: Bearer <token>` header. The local dashboard rides
a `dst_session` cookie instead; a header always wins over the cookie
(`services/auth/deps.py`).

| Prefix | What | Where it works |
|---|---|---|
| `dstadm_` | admin token (`dst bootstrap`) | `/mgmt`; also `/v1` as a superuser identity. **Rejected at `/mcp`** with a 403 naming the fix |
| `dst_` | caller API key (`dst keys create`) | `/v1` and `/mcp`: the governed identity, one per person |
| `dsto_` | OAuth access token | same plane as caller keys, differs only by expiry |
| `dstsess_` | local dashboard session | cookie-carried |

Local sessions: `POST /auth/login` (email + password → `dstsess_`, in the body and as
an httpOnly cookie), `POST /auth/logout`, `GET /auth/me`; accounts are managed via
`POST`/`GET /mgmt/users`. Token sanity: `GET /mgmt/ping` (does this admin token
resolve?) and `GET /mgmt/whoami` (to which org?).

Prefixes: `services/auth/tokens.py`. MCP authentication happens at the ASGI edge,
before routing (`services/app.py`): no bearer → 401 with a `WWW-Authenticate` challenge
pointing at the OAuth protected-resource metadata (the hook a client's native OAuth
flow hangs on); a bad key fails at connect time rather than yielding a
working-looking session that fails on every tool call.

Access to lenses is deny-by-default per lens; every tenant table sits behind Postgres
row-level security whose failure mode is *no rows*, never cross-tenant
(`services/db/session.py`).

## Meta

| Route | Description |
|---|---|
| `GET /health` | Liveness: the process is up |
| `GET /ready` | Readiness. Four checks gate `status`: DB, MCP session manager, schema state, and whether trace writes are landing. Reported but never gating: `embeddings`, `certified_matching`, `plugins`, and `models`: what this install actually resolves (`fast=`/`smart=` provider/model + embedder). A lens that names no model runs on the `smart=` entry, so this is where "which model does my lens run on?" is answered |

## Data plane (`/v1`)

Caller keys (or an admin token). Routes in `services/api/query.py`,
`services/api/route.py`, `services/api/reviews.py`, `services/api/openai_compat.py`.

| Route | Description |
|---|---|
| `GET /v1/lenses` | The lenses this key may query |
| `GET /v1/lenses/{name}` | One lens's schema, definitions, and certified count |
| `POST /v1/lenses/{name}/query` | Ask: body `{"q": "..."}` → a governed answer |
| `POST /v1/lenses/{name}/metrics` | Ask with a structured intent (metrics/dimensions/filters) instead of prose; same governance and receipts |
| `GET /v1/definitions` | Look a governed term up across every lens this key may use: the approved meaning verbatim, no SQL, no warehouse (`dst define`) |
| `POST /v1/sql` | Guarded read-only SQL, against a connection (admin token) or within a lens's boundary (caller key); row-capped (`limit`, default 20, max 500) and logged |
| `GET /v1/lenses/{name}/certified` | The lens's certified library; with `?q=` results are similarity-ranked (top 5, scored) |
| `POST /v1/lenses/{name}/certified/{cert_id}/run` | Run an approved question→SQL pair exactly as certified; zero AI SQL generation |
| `POST /v1/query` | Routed ask: dst picks the lens, or declines with an uncovered envelope |
| `POST /v1/verify-receipt` | Check an answer's signed receipt: signature + field-by-field cross-check against the logged trace |
| `POST /v1/reviews` | Open a review ticket on a traced request (optionally with a correction delta); 201, and only against a request served to this caller (admins excepted) |
| `GET /v1/reviews` | The tickets raised on this caller's own requests: the reporter's half of the queue |
| `GET /v1/reviews/{ticket_id}` | Ticket status + tracking URL |
| `POST /v1/chat/completions` | OpenAI-compatible: `model: "dst/<lens>"` selects the lens; `stream: true` returns a valid SSE stream, though composition is synchronous so it is one content delta then `[DONE]`; unknown fields ignored |
| `GET /v1/models` | This key's lenses as OpenAI model objects |

Every ask route takes `"format"`: `both`, `structured` (`data` is an accepted alias),
or `prose`. The default is `both`, except `POST /v1/lenses/{name}/metrics`, whose door
defaults to `structured`.
`structured` returns the rows without the written answer, and skips the LLM call that
writes it, which is most of the wait: on a certified serve the difference is roughly
two orders of magnitude. Everything else is unchanged (same SQL, citations, confidence,
trace, receipt), so it is the right shape whenever the caller phrases the reply itself.
A `prose` caller still receives the envelope-level `truncated` block: dropping the
data payload cannot drop the fact that it was incomplete.

The query response (`services/contracts/response.py`) carries the answer plus its
[receipts](../concepts/receipts.md): the `sql`, `citations`, a graded `confidence`
(`verified` / `partial` / `unverified`; derived from named checks, never asserted),
`certification` (`certified` / `assisted` / `none`, plus `certified_failed` on an
error status: the approved answer's SQL no longer runs, so the badge never stands
beside the fault as plain `certified`), `data_as_of`, a `request_id`
that ties it to the audit trail, and a signed `receipt`, a portable block anyone in
the org can POST back to `/v1/verify-receipt` later to confirm these exact claims were
really served. A `clarification` field set means it is *not* a data
answer; its `kind` says why. `ambiguous_term`: a governed term needs the caller to pick
a meaning first. `unknown_value`: a filter value the question used was **proven absent**
from the column it filters (checked against the committed value dictionary, or by a
governed probe of the warehouse): `term` names the column, `options` are the values it
actually holds; re-ask with the stored value you mean, or read the absence as the
answer ([Clarify & refusal](../concepts/clarify-and-refusal.md)).

### Row caps and truncation

Three caps, deliberately separate:

| Cap | Default | What it bounds |
|---|---|---|
| `max_rows_to_compose` (per lens) | 200 | rows the composer's prompt sees (a prompt budget) |
| `max_rows_to_return` (per lens) | 1000 | the data payload the caller gets back |
| fetch cap (fixed) | 5000 | rows dst fetches from the warehouse at all; past it `row_count` is a floor |

Either cap biting is stated, never inferred: `truncated` is a block on the
response **envelope**, not only inside `data`, so a `format: "prose"` caller
(whose data block is dropped) still holds the fact deterministically. It carries
`returned` and `total`, and `total` is `null` when the engine-side fetch cap
bit: the true count is then unknown and must never be stated as a number
(`services/contracts/response.py`, `services/runtime/pipeline.py`).

### Verifying a receipt

`POST /v1/verify-receipt` runs two independent checks, both deterministic, no
LLM (`services/api/receipts.py`, `services/runtime/receipt.py`):

1. **Signature**: recompute the HMAC-SHA256 over the receipt's canonical JSON,
   keyed by `DST_SECRET_KEY`, the same key list and rotation contract as
   stored-secret encryption: the first key signs, every key verifies.
2. **Trace cross-check**: read the `request_log` row serving already wrote and
   compare the receipt's claims (`lens`, `confidence`, `certification`,
   `sql_sha256`) field by field; every disagreement is listed.

Verification is stateless: nothing new is persisted. `ok` means both held:
valid signature, trace found, zero mismatches.

The request body is the receipt object itself, **not** wrapped in a
`{"receipt": …}` envelope. A wrapped body returns 422 naming `request_id`,
`lens` and `served_at` as missing, which is the first thing everyone gets wrong.

```
$ curl -sX POST $DST_URL/v1/verify-receipt -H "Authorization: Bearer $TOKEN" \
    -H 'content-type: application/json' -d "$RECEIPT_JSON"

{"ok": true, "signature": "valid", "trace_found": true, "mismatches": [],
 "question": "How many customers are repeat customers?",
 "lens": "customer_value", "caller": "admin"}
```

Edit any field and resubmit, and the failure is two-part: the signature and the
specific field that no longer matches the logged trace:

```
{"ok": false, "signature": "invalid", "trace_found": true,
 "mismatches": ["confidence"], "question": "…", "caller": "admin"}
```

It returns the original `question` and `caller` too, which is what makes it
useful to a skeptic holding only a pasted number: they learn what was actually
asked, by whom, and whether anything was tampered with. `signature` is one of
`valid` / `invalid` / `unsigned` (the receipt carries no digest: the serving
server had no signing key, and says so rather than faking one) / `unkeyed`
(the receipt is signed but *this* server holds no key to check with: a config
gap named as itself, never reported as forgery). The receipt carries the SQL's
hash, not the SQL: receipts travel further than SQL should, and the hash still
pins the receipt to the exact query. Refusals and clarifications carry no
receipt: they make no data claim to attest.

## MCP (`/mcp`)

A remote, stateless streamable-HTTP MCP server (`services/mcp/server.py`); connect with
a URL and a `dst_` key, or via the built-in OAuth flow. Twelve tools:

| Tool | Description |
|---|---|
| `list_lenses` | What this key can query |
| `describe_lens` | Fields, definitions, certified count for one lens |
| `lookup_definition` | A governed term's approved meaning verbatim; no SQL, no warehouse |
| `search_certified` | Similarity-search the certified library |
| `run_certified` | Deterministic run; a bare question auto-resolves only at or above the configured embedder's exact band (`exact_band` on the certified-library response — 0.95 by default, and embedder-relative), otherwise returns `no_exact_match` + near misses |
| `query` | Ask a named lens |
| `query_metrics` | Ask with a structured intent (metrics/dimensions/filters) instead of prose |
| `sql` | Run your own read-only SQL inside a named lens's scope; guarded, row-capped, logged |
| `route_query` | **The default door**: dst picks the lens or declines |
| `send_for_review` | Flag an answer into the human review queue |
| `review_status` | Poll a ticket |
| `verify_receipt` | Check a receipted number before repeating it: signature + trace cross-check |

plus a `getting_started` prompt. Every tool returns a uniform envelope, `{"ok": true,
…}` or `{"ok": false, "code": auth | forbidden | not_found | rate_limited |
no_exact_match | upstream | unreachable, "error": …}`, so an agent can branch on
`code` instead of parsing prose. See [Agents over MCP](../guides/agents-mcp.md).

## OAuth

A self-contained authorization-server facade for MCP clients (`services/api/oauth.py`):
PKCE S256 only, dynamic client registration, a server-rendered consent page.

| Route | Description |
|---|---|
| `GET /.well-known/oauth-protected-resource/mcp` | Protected-resource metadata (RFC 9728); also served bare at `/.well-known/oauth-protected-resource` |
| `GET /.well-known/oauth-authorization-server` | AS metadata |
| `POST /oauth/register` | Dynamic client registration |
| `GET /oauth/authorize` → `POST /oauth/authorize/complete` | Consent flow |
| `POST /oauth/token` | Code + PKCE → `dsto_` access token |

## Control plane (`/mgmt`)

Admin auth. One router per concern under `services/api/`; the surface is large
(~100 endpoints under `/mgmt`), so this lists the groups and the endpoints worth knowing by name;
the rest is on `/docs`, and the [CLI](cli.md) wraps the ones you'd call by hand.

| Prefix | Concern |
|---|---|
| `/mgmt/project` | export / plan / apply: the file-first deployment door |
| `/mgmt/lenses` | drafts, publish, versions, repo + diffs, context, drift, sample queries |
| `/mgmt/lenses/{lens}/certified` · `/evals` · `/distill` · `/patches` | the certified corpus, eval cases, and drafted fixes per lens |
| `/mgmt/lenses/{lens}/join-candidates` · `/profile` · `/profile-drift` | profiling |
| `/mgmt/semantic` | shared entities/definitions + `GET /mgmt/semantic/introspect` |
| `/mgmt/connections` | warehouse connections, catalog, per-connection profile, and the [drift-audit](../guides/drift-audit.md) endpoints |
| `/mgmt/reviews` | queue, rulings, patch approve/reject |
| `/mgmt/callers` · `/mgmt/users` · `/mgmt/directory` | identities and keys |
| `/mgmt/observe` | KPIs, requests, callers, evals |
| `/mgmt/standards` · `/mgmt/activation` · `/mgmt/surface` · `/mgmt/gap-map` | the rest of the cockpit |

Notable:

- `POST /mgmt/project/apply`: the single deployment door (`dst apply`):
  blue/green, one transaction under a per-org advisory lock; a concurrent apply gets a
  409 (`services/project/apply.py`).
- `POST /mgmt/project/plan`: the dry run behind `dst plan`.
- `GET /mgmt/observe/requests/{request_id}`: the full trace drill-down for one
  request: question, SQL, cost, outcome.
- `GET /mgmt/lenses/{name}/repo` and `GET /mgmt/lenses/{name}/diff?from=&to=`: the
  lens-as-repo file tree and version diffs (`services/api/mgmt_lenses.py`).

*(Routes verified against `services/app.py` and the routers under `services/api/`.)*
