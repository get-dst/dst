# Agents over MCP

Point your agent at dst's MCP server instead of the warehouse's. An agent holding
warehouse credentials authors and runs its own SQL, against whatever that credential
reaches. An agent holding a dst caller key gets
[the answer path](../concepts/answer-path.md): governed vocabulary, guarded read-only
SQL, receipts, and a scope you set per agent.

## Setup

Mint one key per caller (a person or an agent, never a shared team key), then register
the server:

```bash
dst keys create --caller support-bot
claude mcp add dst http://localhost:8000/mcp --transport http \
  --header "Authorization: Bearer dst_..."
```

The registration name is the alias your org's AI answers to — register it as `watson`
and "check in watson what our ARR is" just works. Set `DST_INSTANCE_NAME=watson` on the
deployment (or in `.env`) to match: the server then presents itself by that name too —
MCP server name, operating manual, and the sign-in and consent pages a person sees
when connecting — so the alias and the self-description agree (`dst init` asks for
this name and wires both ends).

The server is mounted at `/mcp` on the API itself (`services/mcp/server.py` builds it,
`services/app.py` gates and serves it at that path); each request carries its own bearer
key, so any MCP client that speaks streamable HTTP works.
For a local stdio client, `dst mcp` runs the same server over stdio with the key in a
`DST_API_KEY` env var and the base URL in `DST_URL` (env or the project's `.env`):

```bash
claude mcp add dst --transport stdio --env DST_API_KEY=dst_... -- dst mcp
```

Revoke a key with `dst revoke-key` or from the dashboard.

## The tools

Fourteen tools (`services/mcp/server.py`):

| Tool | What it does |
|---|---|
| `route_query` | **the default door**: dst picks the lens or declines; on a decline you get `covered: false` with a reason and nearest miss, never a wrong-lens guess — or, when a lens you may use declares the named thing not computable, that lens's refusal (`decided_by: refusal`, `answer.status: refused`) |
| `query` | ask a named lens |
| `query_metrics` | ask with a structured intent (metrics, dimensions, filters) instead of prose; `compare` adds the same figures over a comparison window, with deltas |
| `query_metrics_batch` | up to 50 structured intents in one call, read at one `data_as_of`, results keyed by each intent's `id`; one failing intent fails alone |
| `sql` | run your own read-only SQL inside a named lens's scope; guarded, row-capped, logged; honours the lens's `hide` and `not_computable` refusals (code `refused`, the same sentence `query` answers with); takes `purpose` like the other asking tools |
| `list_lenses` / `describe_lens` | what this caller may see: schema, definitions, certified count, and the metric catalog |
| `metric_catalog` | the catalog alone: metrics with label, unit, grains, callable dimensions and `depends_on`; dimensions with their value lists; and the metric `tree` |
| `lookup_definition` | a governed term's approved meaning verbatim; no SQL, no warehouse call |
| `search_certified` | similarity-ranked [certified answers](../concepts/certified.md) |
| `run_certified` | deterministic run of approved SQL; auto-resolves only at or above the configured embedder's exact band (0.95 by default), otherwise returns `no_exact_match` plus near-misses |
| `send_for_review` | flag an answer; a note and corrected SQL become a correction the [review loop](correction-loop.md) turns into a fix |
| `review_status` | poll a ticket |
| `verify_receipt` | check a [receipted](../concepts/receipts.md) number before repeating it: signature plus trace cross-check |

The argument names, as the tools declare them (required first; `format` is
`both` | `structured` | `prose` everywhere it appears):

| Tool | Arguments |
|---|---|
| `route_query` | `question`; `format`, `bindings`, `allow_untyped`, `purpose` |
| `query` | `name`, `question`; `format`, `bindings`, `allow_untyped`, `purpose` |
| `query_metrics` | `lens`; `metrics`, `dimensions`, `filters`, `order_by`, `limit`, `entity`, `grain`, `compare`, `format` |
| `query_metrics_batch` | `lens`, `intents`; `format` |
| `sql` | `lens`, `sql`; `limit`, `purpose` |
| `list_lenses` | none |
| `describe_lens` | `name` |
| `metric_catalog` | `lens` |
| `lookup_definition` | `term` |
| `search_certified` | `lens`, `question` |
| `run_certified` | `lens`; `cert_id` or `question`, `bindings`, `format`, `purpose` |
| `send_for_review` | `request_id`; `note`, `kind`, `target`, `corrected_sql` |
| `review_status` | `ticket_id` |
| `verify_receipt` | `receipt` |

A decline from `route_query` (`covered: false`) carries a `request_id` too: the
recorded routing decision's id, the handle to quote about that decline the way an
answer's `request_id` is quoted to `send_for_review`.

Plus a `getting_started` prompt. Every tool returns a uniform envelope, `{"ok": true, …}`
or `{"ok": false, "code": auth|forbidden|not_found|rate_limited|refused|invalid|
no_exact_match|upstream|unreachable, "error": …}`, so agents branch on `ok` and `code`,
never parse prose. `refused` is dst declining the request as asked (an unknown metric, a
fan-out over the cap, a lens that does not cover the question) and `invalid` a body that
failed validation: both are the caller's to fix, and `error` says what. `upstream` is a
genuine server fault.

A lens can also [expose tables that have no
definitions](project-files.md#exposing-tables-that-have-no-definitions).
`describe_lens` lists them under `exposed_tables`, apart from `entities`, with
their columns and a note that answers from them are ungoverned; the `sql` tool
reads them inside the lens's scope, and an answer that touches one carries an
`EXPOSED` line in `degraded` the way an untyped answer carries `UNTYPED`.

## Scoping a lens per agent

Access is deny-by-default: a caller can query a lens only if the lens's `lens.yaml`
grants it (`services/contracts/lens_config.py`):

```yaml
access:
  allow:
    - caller: support-bot
```

`list_lenses` and `route_query` operate over the caller's *accessible* lenses only
(`services/api/query.py`, `services/api/route.py`): an agent cannot route into, or
even see, a lens it was not granted. Rules match a caller by name or by group; the
lens's own `rate_limit.per_caller_rpm`
(60 by default, admin tokens exempt) then caps each caller separately, and every call is
attributed to the caller in the audit log. One agent, one key, one scope: the agent for
support sees the support lens and nothing else.

Grants are edits to `lens.yaml`: change the file, `dst apply`, done. The same
deny-by-default rule governs humans and applications; agents are just callers.

