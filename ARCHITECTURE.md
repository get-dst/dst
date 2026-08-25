# Architecture

dst is the data serve tool: it serves governed, verifiable answers from a data
warehouse to AI callers. Analysts declare meaning in files (semantic models,
lenses, certified answers); dst compiles them, generates SQL under deterministic
guards, executes read-only, and returns answers with receipts.

This is the contributor orientation document. For running the project, see
[CONTRIBUTING.md](CONTRIBUTING.md) and the docs site at
<https://www.dataservetool.com> (quickstart, concepts, deployment).

## The answer path

A caller (an agent or AI assistant) reaches dst over MCP
(`services/mcp/server.py`) or REST (`services/api/query.py`, plus an
OpenAI-compatible surface in `services/api/openai_compat.py`). Both converge on
the runtime pipeline, `services/runtime/pipeline.py`:

1. **Route.** If the caller did not name a lens, `services/router/decider.py`
   shortlists lenses by embedding similarity and an LLM decider picks one.
2. **Ground.** `services/runtime/assembly.py` assembles the lens's semantic
   model, definitions, and any matching certified answer into generator inputs.
   A certified match skips generation entirely and serves the pinned SQL.
3. **Generate.** `services/runtime/generator.py` (and `intent_generator.py`)
   produce SQL via a model from `services/llm/registry.py`, with
   execution-guided self-repair on guard or warehouse errors.
4. **Guard.** `services/runtime/sql_guard.py` and its siblings (`shape_guard`,
   `time_guard`, `value_guard`, `filter_guard`, `cte_guard`)
   statically validate the SQL. Guards are deterministic and they refuse rather
   than repair meaning; the one thing `sql_guard` does rewrite is name
   canonicalisation (a bare entity or table name resolved to its declared
   source), and the rewritten SQL is what executes and what the receipt carries.
5. **Execute.** The lens's warehouse connection (`services/connectors/`) runs
   the query on the credential the operator configured — the setup guide grants
   read-only, and the guard above holds SELECT-only either way.
6. **Compose and verify.** `services/runtime/answer.py` composes prose from the
   returned rows; `services/runtime/faithfulness.py` and
   `services/runtime/verification.py` check the prose against the rows
   (numeric grounding) and attribute which stage failed when something did.
7. **Receipt.** `services/runtime/receipt.py` signs the answer trace;
   `services/api/receipts.py` verifies it later.

A refusal (out-of-scope table, ungoverned metric, ambiguity) is a governed
outcome with a named reason, not an error.

## Subsystem map

Backend (`services/`):

- `app.py` — FastAPI application factory; mounts REST, MCP, and the dashboard.
- `api/` — HTTP routes: query/route/sql serving, `mgmt_*` dashboard endpoints,
  receipts, reviews, auth surfaces.
- `contracts/` — shared dataclasses and protocols; the coordination seam
  between subsystems.
- `connectors/` — warehouse drivers: Postgres, BigQuery, Snowflake, DuckDB,
  MySQL; plus the guarded sampling pass.
- `runtime/` — the answer path above: pipeline, generators, guards, composer,
  verification, receipts.
- `project/` — the file-first lifecycle: `loader`/`compile`/`plan`/`apply`,
  probes, warehouse drift detection.
- `semantic/` — shared semantic assets: files, store, warehouse introspection,
  name resolution.
- `lenses/` — lens store, warehouse profiling and enrichment, connection and
  credential management.
- `router/` — lens selection: anchor embeddings, decider, routing eval.
- `certify/` — certified answers: binding questions to pinned SQL and prose.
- `evals/` — eval cases, the runner, and the certified suite that gates apply.
- `governance/` — audit log, policy, caller directory, rate limits, drift watch.
- `observability/` — serving KPIs, cost tracking, structured logs.
- `auth/` — API keys, scopes, OAuth/OIDC, local sessions.
- `cli/` — the `dst` command; `style.py` is the single ANSI/formatting seam.
- `mcp/` — the MCP server. Tools must stay `async def`: under the remote
  transport they share the API's event loop, and a blocking tool deadlocks it
  (a regression test pins this).
- `llm/` — provider registry (`registry.py`), provider adapters, retries.
- `plugins.py` — entry-point discovery for third-party connectors/providers.
- `context/` — document ingestion and embedding for grounding context.
- `db/` — SQLAlchemy models, sessions, and the RLS org scoping.
- `security/` — secret crypto and the credential sentinel.
- `reviews/` — the reviewer queue: proposals, judging, patches.
- `probe/`, `benchmark/` — accuracy/consistency probes and the benchmark
  harness (dev tooling, not the serving path).
- Smaller pieces: `dbt/` (one-shot artifact import), `osi/` (Open Semantic
  Interchange emit/load), `definitions/`, `certdefs/`, `validate/`;
  `web_dist/` holds the built dashboard the API serves.

Around the backend: `apps/web/` (React dashboard), `migrations/` (Alembic,
sequentially numbered `00NN_*`), `deploy/` (docker-compose and Helm),
`fixtures/` (the jaffle DuckDB demo warehouse), `tests/`.

## Seams

- `services/contracts/` is the boundary between subsystems: everyone imports
  it, changes to it are coordinated, nothing else is a shared surface.
- `services/plugins.py` is the extension seam — connectors and LLM providers
  register via Python entry points, no core edits required.
- `services/llm/registry.py` is the one place model tiers and providers are
  resolved; nothing else constructs a provider.
- `services/cli/style.py` is the only module that emits ANSI.
- Files are truth. Lenses, semantic models, definitions, certified answers,
  and eval cases live in the customer's project directory; `dst plan` and
  `dst apply` compile them into the database. The dashboard governs (review,
  audit, observe) but never authors — edits flow through files.

## Guarantees and their tripwires

Every guarantee has a test or gate that fails loud; an unchecked claim is
reported as untested, never as clean.

- **Read-only SQL.** `sql_guard` admits a single SELECT statement whose tables
  and columns are on the lens allow-list, failing closed on anything it cannot
  classify; the read-only warehouse credential is the backstop.
- **Multi-tenancy.** Postgres RLS on org-scoped tables; every query runs in
  `org_session` (`services/db/session.py`), which sets the `app.current_org`
  GUC. On managed Postgres the admin role has no BYPASSRLS, so admin reads of
  force-RLS tables must set the GUC too — `tests/test_rls_managed_pg.py` pins
  this.
- **Certified answers are regression tests.** `services/evals/certified_suite.py`
  replays the certified corpus against live generation; `dst apply` runs it as
  a publish gate.
- **Atomic apply.** `services/project/apply.py` is blue/green: all writes stage
  on one session, and any gate failure aborts the whole apply — the prior
  bundle keeps serving.
- **Receipts.** `services/runtime/receipt.py` signs each answer trace with
  HMAC-SHA256; tampering fails verification at `services/api/receipts.py`.
