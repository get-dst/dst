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

Eighteen tools (`services/mcp/server.py`). Every MCP request runs in
[driver mode](#driver-mode): on a lens served through Cube, dst calls no model for it.
Lenses on the typed core answer exactly as without it.

| Tool | What it does |
|---|---|
| `route_query` | **the default door**: dst picks the lens or declines; on a decline you get `covered: false` with a reason and nearest miss, never a wrong-lens guess, or, when a lens you may use declares the named thing not computable, that lens's refusal (`decided_by: refusal`, `answer.status: refused`). With lenses served through Cube in the catalogue, see [driver mode](#driver-mode) |
| `query_cube` | run your own Cube query and word ledger against a lens served through Cube: every check before and after execution, rows, mode and a signed receipt; a stopped query returns `retry` with what to change |
| `values` | the observed values of a member (`view.member`) or an exposed column (`table.column`), filtered by prefix |
| `data_end` | where a member's, a cube's or an exposed table's data ends, the lens clock, and a partly loaded period |
| `grain` | an exposed table's blueprint (grain, key, versions, clocks, caveats), check verdicts, probe facts, and rows against distinct key |
| `query` | ask a named lens |
| `query_metrics` | ask with a structured intent (metrics, dimensions, filters) instead of prose; `compare` adds the same figures over a comparison window, with deltas |
| `query_metrics_batch` | up to 50 structured intents in one call, read at one `data_as_of`, results keyed by each intent's `id`; one failing intent fails alone |
| `sql` | run your own read-only SQL inside a named lens's scope; guarded, row-capped, logged; honours the lens's `hide` and `not_computable` refusals (code `refused`, the same sentence `query` answers with); answers carry `grain`, `data_end` and `checks` lines and a signed receipt; takes `purpose` like the other asking tools |
| `list_lenses` / `describe_lens` | what this caller may see: schema, definitions, certified count, and the metric catalog; for a lens served through Cube, its views (one in full), members with their `meta.dst` facts and values, refusals, ambiguous terms, freshness and certified pairs |
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
| `query_cube` | `lens`, `query`, `ledger`; `question` |
| `values` | `lens`, `member`; `prefix` |
| `data_end` | `lens`, `member` |
| `grain` | `table`; `lens` |
| `query` | `name`, `question`; `format`, `bindings`, `allow_untyped`, `purpose` |
| `query_metrics` | `lens`; `metrics`, `dimensions`, `filters`, `order_by`, `limit`, `entity`, `grain`, `compare`, `format` |
| `query_metrics_batch` | `lens`, `intents`; `format` |
| `sql` | `lens`, `sql`; `limit`, `purpose` |
| `list_lenses` | none |
| `describe_lens` | `name`; `table`, `view` |
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
`describe_lens` lists them under `exposed_tables`, apart from `entities`, one
summary line each (grain, key, rows, flags) and a note that answers from them are
ungoverned. Call it again with `table` for one table's columns, blueprint, check
verdicts and probe facts. The output is bounded, and `elided` says what it left
out; the `sql` tool
reads them inside the lens's scope, and an answer that touches one carries an
`EXPOSED` line in `degraded` the way an untyped answer carries `UNTYPED`.

## Driver mode

dst spends model tokens only where no agent is driving: on a lens served through Cube
its frontier lane reads a question into a Cube query, and its router asks a model to
break ties, for callers that send plain questions over HTTP. An agent reads the
question better than a prompt dst writes for it, so when an agent drives a Cube-backed
lens, dst calls no model. The agent writes the Cube query and the word ledger; dst
routes on coverage, runs every check on what the agent submits, compiles through Cube,
executes through its own connection and signs the receipt.

Driver mode applies to lenses served through Cube only: a Cube-backed lens with no
`serving:` key, or one set to `serving: cube`. A lens on the typed core (every lens of
an entity project, or one set to `serving: typed`) answers every tool exactly as it
does without driver mode: `query`, `run_certified`, `search_certified` and `route_query`, decider included.

A request is in driver mode when:

- it comes through the MCP server, which sends `X-DST-Driver: 1` on every call;
- any HTTP caller sends `X-DST-Driver: 1` itself; or
- an admin marked the caller a driver (`dst keys create --caller <name> --driver`, or
  `PUT /mgmt/callers/{name}/driver` with `{"driver": true}`).

The flow for a lens served through Cube:

1. `route_query(question)` returns `candidates`: the Cube-backed lenses holding every
   word of the question that some lens declares, each with `matched` (the members,
   values, segments and definitions each word hit), `serving` and `score`: how many of
   the question's words the lens names as a member, title, alias or value rather than
   in prose. Candidates come highest score first, at most three unless more tie at the third
   score (a tie is never cut by name); a lens that names as
   many words as the best covering one is listed too, with `uncovered` saying what it
   lacks, and a lens that holds the words only in prose drops beside one that names
   them. Typed lenses that hold every word are listed beside them, to ask with `query`. `no_model` says per
   lens whether dst calls a model there. `choose: true` means several cover it and the
   agent picks, or asks the user. A Cube-backed lens that declares the term not
   computable, or says the ask belongs to another lens, returns its sentence as
   `refusal` with the mode. When no Cube-backed lens covers the question, the typed
   lenses route it as they always do and their answer is under `routed`. With no
   typed lens either, `nearest` lists what each close lens lacks and `explore_kit` the
   exposed tables the caller may read. When lenses do cover it, `explore_kit` still
   lists (at most five) exposed tables carrying at least as many of the question's words
   as the best candidate names, so a covered ask still sees a table that holds it. A
   catalogue with no Cube-backed lens gets
   today's response unchanged.
2. `describe_lens(lens)` summarises each view (its member names; a lens serving one
   view lists it in full when it fits), the lens's `not_computable` entries and
   ambiguous terms with their readings, `freshness` on the lens's own cubes (the lens
   clock, each time dimension's data end, partly loaded periods), and certified
   question → Cube query pairs, under 40 KB with `elided` naming what did not fit.
   `describe_lens(lens, view=…)` lists that view in full: each member's title,
   description, aliases, additivity, clock and `clock_kind`, observed values (bounded,
   with `values_total`) and data end.
3. `query_cube(lens, query, ledger, question)` runs the agent's query. The ledger is
   the same one dst's own lane writes: one entry per content word of the question,
   `{"word", "to": member | filter | segment | period | none, "ref"}`.

The checks `query_cube` runs, in order, each reported on the receipt as `name:outcome`:

| Check | What stops the query |
|---|---|
| `not_computable` | the question names a term the lens declares it cannot compute; nothing to retry |
| `not_for` | the question is an ask the lens says is another lens's; the detail names it |
| `scope` | a member outside the lens's views |
| `hidden` | a member withheld from this caller's groups |
| `declared` | a member the Cube model does not hold |
| `ambiguous` | an ambiguous term asked without naming a reading; when the readings agree within 1 %, the default is served and the others disclosed |
| `words` | a content word of the question carried by nothing in the ledger or the query |
| `unasked_scope` | a filter on a company or scope the question did not ask for |
| `values` (dictionary) | a filter value the dimension's observed values do not hold |
| `window` | a window that disagrees with the question's period ("this month" is bound to its dates on the lens clock) |
| `label_key` | a label grouped without the key it belongs to |
| `freshness` / `partial_period` | the data ends before the window, or the period is only partly loaded: served with the fact (which period, how complete, judged by which measure) in the answer, the checks and the receipt, and graded `partial`; refused only when no window of the ask has data loaded |
| `empty_result`, `null_bucket`, `tie_at_limit`, `snapshot_pin`, `empty_window` | after execution: no rows, an empty bucket, rows tied past the limit, a level metric read at one snapshot, an empty window |

An answer that read a cube or member the overlay gives caveats
(`cubes.<cube>.caveats`, `cubes.<cube>.member_caveats`: an allocated measure, a known
duplicate) carries them as `caveats`, leads its `answer` text with a `CAVEAT` line per
caveat, and lists them on the receipt with a `caveats:disclose` check. dst cannot see
whether the agent's final reply repeats them, so relay every one beside the figure.

A query a check stops comes back with `status` `clarification` or `refused`, `mode`
from the [failure modes](../reference/failure-modes.md), and `retry`: `{check, mode,
detail, change}`, where `change` says what to alter before sending it again. Without
`question`, the `words` and `unasked_scope` checks report `skip` with an `UNTESTED`
reason instead of passing.

When no lens covers the question, `explore_kit` lists the exposed tables with their
grain, key and caveats. Before stating a number from one, call `grain(table)` for rows
against distinct key, `data_end(lens, table)` for where its dated columns end, and
`values(lens, table.column)` for a filter value. The `sql` tool then adds the same
facts to its answer: a `grain` line and a `data_end` line per exposed table read, an
`empty_result` check that says no rows is not a zero, and a signed receipt whose
`checks` say `exposed, could not verify …` wherever a check could not run (no
blueprint, no fresh profile).

In driver mode, `query` on a lens served through Cube answers 400 pointing at
`query_cube`, and `run_certified` there serves the approved rows with no written
answer and no model call, whatever `format` asks. On a lens served through Cube a
certified pair whose `certified_answers.yaml` entry carries a Cube `query` beside its
SQL serves through that query: a template's slots are validated and written into it,
every `query_cube` check runs, Cube compiles it and dst executes it, and the receipt is
certified. A pair with no `query` serves its SQL as before. HTTP callers that are not in driver mode, and every lens on the typed
core, keep the question doors as they were.

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

