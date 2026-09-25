# Changelog

All notable changes to dst are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file begins at the open-source launch. dst was built privately before that, and
none of the earlier history is reconstructed here.

## The upgrade contract

**Migrate before you serve.** The database schema and the code ship together:

```bash
pip install -U dst-core
dst migrate
# then restart the server
```

`dst dev` migrates automatically, and in containers the entrypoint does it for you
(`DST_MIGRATE_ON_START`, default `true`); orchestrated deploys set that to `false`
and run `dst migrate` once per release.

`dst serve` **refuses to start against a schema behind its build**, by design. A
server on an out-of-date schema answers questions correctly and loses every trace in
silence — the audit trail, the review queue, the drift audit and `dst test` are all
views over the request log. The refusal names the current revision, the one the build
needs, and the unapplied list, then tells you to run `dst migrate`.

Full upgrade, rollback and restore paths: **[docs/upgrading.md](docs/upgrading.md)**.

## [0.5.4] — 2026-09-25

No schema change.

### Changed

- **`DST_INSTANCE_NAME` names the deployment everywhere a person looks.** It already
  named the MCP server and the manual the driver AI reads; the demo sign-in page and
  both consent pages now carry it too, and a deployment with its own name credits
  dst (data serve tool) beneath it.

## [0.5.3] — 2026-09-25

No schema change.

### Fixed

- **A count of the primary key is a count of every row.** A ratio declared over
  `COUNT(<primary key>)` now matches SQL dividing by `COUNT(*)` or `COUNT(1)`, so an
  inline ratio reads back as the declared ratio whichever way its count is written.
  The slot lane follows the certified suite's rule for stored gold: a stored
  construction stands, a stored attribution is re-read.
- **Only an exact pass becomes gold.** A shape-lenient pass (the certified value
  found beside other columns) can carry an extra metric, so its typed reading is no
  longer stored as the certified answer's gold.

## [0.5.2] — 2026-09-25

No schema change.

### Fixed

- **A certified answer's stored reading no longer goes stale.** The suite graded
  typed answers against the resolution stored when the answer was certified. An
  attributed one is recomputed with the current attributor, so improvements (such
  as reading an inline ratio back as the ratio) reach answers certified earlier.
  A construction, the typed reading approved at certification, still stands.
- **Only served answers count against a daily quota.** Refusals, clarifications
  and dst's own errors are logged too, and they no longer use up a caller's day.
- **The demo page's example question comes from the lens.** The key mint returns
  one example per lens, the first common question its semantic layer declares.
- **`allow_untyped` help text:** an untyped answer carries `typed: false` and the
  `UNTYPED:` line; its tag is what its SQL reads back as.
- **Demo recipe:** the Cloud Run section links the public deployment guide, the
  unused Clerk secret is no longer required, and DeepSeek spend is bounded by a
  prepaid balance.

## [0.5.1] — 2026-09-25

No schema change.

### Fixed

- **A date attribute is no longer read as data freshness.** A lens's "data as of"
  took the newest date of every table, so a patches table's latest release date
  dated every answer over a freshly loaded warehouse and called it months stale.
  A table's newest date now counts as freshness only when its entity declares a
  time axis (`default_time_field`); other tables fall back to physical freshness
  or make no claim.
- **An inline ratio reads back as the declared ratio.** SQL that divides a ratio
  metric's numerator by its denominator (`SUM(CASE …) * 1.0 / COUNT(*)`, a cast,
  a `NULLIF`) is attributed to the ratio metric, not to its two parts. A typed
  answer that picked the ratio no longer grades as a mismatch against a
  certified answer written that way.

## [0.5.0] — 2026-09-25

No schema change. Re-run `dst probe` after upgrading: documented columns are now
sampled, and declared dimensions get whole value lists.

### Added: typed serving you can leave on

- **A ranking shape.** "Which team has the highest win rate?" resolves to the
  metric per team, ordered, instead of a list of names. "Top 5" carries its
  limit. A ranking with no stated direction asks which end is meant.
- **`better: higher | lower` on metrics.** The author declares which end of a
  metric is the better one; "best" and "worst" rank by it on the typed path, and
  raw-SQL generation sees it too. Unset, a best/worst question asks.
- **Names in the question are matched, not decided.** A stored value the question
  names word for word ("Crystal Maiden", "Lion") binds as a filter with no model
  decision; the model only picks which column it restricts. Two named values on
  two columns give two filters. A named value no filter uses is asked about,
  never dropped.
- **`untyped_fallback` in `lens.yaml`.** The lens owner can make "typed first, raw
  SQL when the question does not type" the lens's default, for callers that
  cannot be expected to pass `allow_untyped`. Every raw answer still carries the
  `UNTYPED:` disclosure. `allow_untyped` on the API, the router and the MCP tools
  is now true, false or unset: unset takes the lens default, false still demands
  typed-only. A typed answer whose rows do not answer the question gets the same
  single raw-SQL run.
- **Whole value lists for declared dimensions.** `dst probe` collects the full
  list of a text dimension up to 500 values (hero, team and item names), used
  only to match names in questions. Prompts never carry more than 25 values, and
  columns that are not declared dimensions (an email, a note) stay at the
  25-value cap.

### Fixed

- **A result that does not answer the question is refused, not served.** Before,
  a ranking read as a listing compiled to a bare list of names and served `ok`.
- **A ranking word is never dropped silently.** "Highest" with no measure to rank
  on asks (or falls to raw SQL) instead of serving an unordered list.
- **A definition whose SQL is a column or a formula never lands in WHERE.** It
  failed on type or filtered nothing; only true/false conditions apply as filters.
- **Numbers are stated, never guessed.** A numeric filter value (an id, a year)
  comes only from a number in the question, never from a list a model picks from.
- **A filter outside the table's declared population is refused before the
  query runs**, with the population named, instead of returning zero rows.
- **`dst probe` sampled nothing on a documented warehouse.** A column with a
  description was skipped, so describing every column removed every value list,
  while the output still said every table was sampled. Described columns are
  sampled, and the count is the real one.
- **A long value list never refuses a query.** A name newer than the last probe
  is not proof of absence, so the pre-query value check reads only lists of 25
  or fewer.
- **One dropped model-provider call no longer ends a `dst test` run.** It fails
  that attempt, which is retried like any failure and never counts as a pass.
- **`dst test` accepts a certified value returned beside label columns** (the
  patch name next to its win rate), marked shape-lenient. Exactly one matching
  cell passes.

## [0.4.2] — 2026-09-25

No schema change.

### Fixed

- **A result that does not answer the question is refused, not served.** The answer
  composer is the one stage that sees the rows beside the question. When they do not
  carry what was asked (a list of hero names for "what should I build on Juggernaut"),
  it now declines in a fixed form and the response is `refused` with the gap named,
  the same shape as the generator's own decline. Before, the prose said the data could
  not answer while the status said `ok`. An empty result or a zero is still an answer.
- **MotherDuck read-only, corrected.** The 0.4.1 notes said read-only needs a
  read-scaling token. It does not: under a regular token the read-only open refuses
  CREATE, INSERT, UPDATE and DELETE on the attached database, so the default holds
  either way. A read-scaling token narrows the credential itself. DuckDB keeps one
  configuration per `md:` database per process, so `probe_write` works on MotherDuck
  only when nothing opened that database read-only first.

### Changed

- The release workflow publishes only from `get-dst/dst`. A fork or mirror that pushes
  a `v*` tag builds nothing and pushes no image or package under its own name.
- The dashboard's design spec lives beside the dashboard, in `apps/web/DESIGN.md`.

### Removed

- `scripts/ai_tells_lint.py`, a writing helper unrelated to the product.

## [0.4.1] — 2026-09-24

One schema change ships with this release: run `dst migrate` before serving.
Migration 0067 adds an index the daily quota reads.

### Added — a deployment other people can reach

- **MotherDuck on the DuckDB connector.** A path of the form `md:<database>` is
  MotherDuck. The token rides the connection's `secret_env`, never the path,
  since a path is copied into snapshots and logs. Read-only stays the default
  and expects a read-scaling token; `read_only: false` opens read-write and
  leaves the SQL guard as the only line. Every catalog read is pinned to the
  connection's own database, because a MotherDuck session attaches every
  database in the account.
- **`statement_timeout_ms` on DuckDB connections**, the cap the other
  connectors already had: a runaway query is interrupted and the failure names
  the cap. Unset stays unbounded for a developer's own file.
- **Daily quotas.** `rate_limit.per_caller_rpd` on a lens bounds one caller's
  day; `DST_DAILY_REQUEST_CAP` bounds the whole org when many callers each
  stay under theirs. Both count the answers actually served over a rolling
  24 hours, so a refusal never eats budget, and both refuse the way the
  per-minute limit does: 429, a `Retry-After` measured from the oldest counted
  answer, a deny row in the audit log. 0 means no quota.
- **Public demo mode.** With `DST_DEMO_ORG_ID` set, every verified Clerk
  sign-in is a non-admin caller in that one org, in group `demo`, named by the
  sign-in email, on the data plane and the MCP grant alike, and sign-in never
  reaches the control plane. `GET /demo` signs a visitor in and
  `POST /auth/demo-key` trades the session for a `dst_` key, one live key per
  person, expiring after `DST_DEMO_KEY_DAYS`. Neither route exists outside
  demo mode.
- **`dst demo`** takes `--path` (a file or `md:<database>`), `--secret-env`,
  `--statement-timeout-ms`, `--allow-group` and `--per-caller-rpd`, probes the
  warehouse before anything lands, and updates in place on a re-run.
  **`dst prune-log --keep-days N`** deletes old request-log rows per org.
- **`deploy/demo/`**: a compose file with TLS for a single VM, the env
  contract, a script that loads a DuckDB file into MotherDuck, and the runbook,
  including the Cloud Run variant.

## [0.4.0] — 2026-09-17

Schema changes ship with this release: run `dst migrate` before serving (the
upgrade contract above). Migrations 0063–0066 add the resolution ledger to the
request log, the measured routing decision, typed gold on certified answers,
and the typed-serving audit columns.

### Added — typed serving

A question either types or it doesn't. With a **typed-decision provider**
configured (`type: typesafe`, one more entry under `providers`), no model writes
SQL: every slot of an answer — lens, entity, the reading of an ambiguous term,
metric, dimension, grain, filter column, stored value — is a decision over a
closed set the layer already owns, with a probability per option, and the SQL
is compiled from the decisions. Windows are parsed, never decided.

- **The resolution ledger** on every answer, trace and receipt: which slots
  the answer rests on and where each came from (`certified`, `declared`,
  `inferred`, `supplied`), the headline tag from the measure slots, and every
  decision with its probability. The audit statement gains **governed basis**
  (the share of answers resting on declared meaning) and **typed share**.
- **`bindings`** on the query doors (REST and MCP): an open value the asking
  agent supplies when a clarification names the slot. **`allow_untyped`**: the
  raw-SQL escalation is opt-in, tagged by what it earns, disclosed with an
  `UNTYPED:` line, and counted apart. Certifying an untyped serve is a
  different act: `dst reviews rule … --certify --allow-untyped`.
- **The policy:** a typed-decision provider acts on its top choice unless
  `none` wins. A measured bar is disclosure, not a gate: an answer that acted
  below it fails a `decision_confidence` check, grades partial, and names the
  slot and probability in the answer. The voting chat decider (five samples
  per decision, `DST_TYPED_SERVING=on`) keeps its measured act bars.
- **Routing on a typed decider** decides over every published lens; the
  cosine shortlist is gone from that path. Embeddings remain for certified
  matching and as the fallback router when no decider is configured.
- **Rails that keep a typed answer honest:** a month period stored as a string
  is read off the column profile and the window compiles to it; month spans
  and abbreviations are one window; a stated month no term carries clarifies
  instead of serving the months it could place; a filter value's two nones
  ("no value stated" drops the column, "not one of these" clarifies); each
  entity's own value dictionaries; the grain is asked only when the wording
  asks for a breakdown over time; the metric is decided first over every
  metric of the lens and names the entity.
- **Typed judge:** deterministic numeric grounding plus one typed scope
  decision replaces the free-text rubric under typed serving; the corrected
  SQL is what gets graded when a ticket carries one; a structured serve is
  never "no data".

### Changed — `dst test` is three lanes

- **Resolution lane** (`--slots`): every certified question resolved by the
  provider and graded per slot against its typed gold — stored from a green
  run, or attributed from the certified SQL for answers certified earlier.
  No warehouse; `--repeat N` fails any case that does not produce the same
  intent every time; per case: elapsed seconds, provider calls, tokens, USD.
- **Compile lane:** the compiled SQL against the certified text, canonicalised.
  Identical is proven without a query.
- **Data lane:** the certified SQL executed against the warehouse — by default
  only for the cases the first two could not prove; `--rows` runs it for all.
- The lanes run on the gate's worker pool (`DST_GATE_CONCURRENCY`), the
  calibration lane too; `dst test --json` carries the lane's cost and a
  median / p90 latency; `eval_run` records the calibration report.
- **Plan lints:** a `population_filter` that references the physical table
  where compiled SQL aliases the entity is an error (`population_filter_unbound`);
  every entity with a metric must compile at plan (`entity_does_not_compile`);
  an ambiguous term's reading that names a metric warns (`reading_names_a_metric`).
- `dst apply --allow-case <id> --reason …` replaces the blanket
  `--allow-failing-cases` for one apply.

### Docs

Typed decisions (concepts), the configuration reference (`type: typesafe`,
`DST_TYPED_SERVING`, `DST_GATE_CONCURRENCY`), the quickstart's optional step,
the evaluation guide's three lanes, the CLI reference, the FAQ, the answer
path, and the `dst init` scaffold's commented typed-provider entry.

## [0.3.0] — 2026-09-08

No schema changes: upgrading from 0.2.0 is `pip install --upgrade dst-core`
with nothing to migrate.

### Added — disposable environments and measured change

An environment is an org on the server — nothing new to operate, just verbs
that make one cheap to mint, select, and throw away:

- **`dst env new|ls|rm`** — `new` mints an org and its admin token without
  touching your project's `.env` (a sandbox must not steal the project's
  identity; the token is recorded in the gitignored `.dst/` map instead);
  `rm` deletes the org and everything in it, one confirmation, one cascade.
  Env-aware verbs take `--env <name>` to run against it by name.
- **`dst runs <lens>`** — the run history `dst test` has always recorded,
  finally visible. `--diff A B` compares two runs: score delta plus the
  per-case flips, exit `1` on a score regression *or any newly failing
  case* — a flip masked by an unchanged score still fails CI. Works
  cross-environment and cross-server (`--other-url`, `--other-token`).
- **`dst experiment <lens> --vary key=a,b`** — N lens-config variants,
  each applied to a disposable env, measured against the same suite, and
  reported side by side. An orchestrator over the verbs above, nothing more.
- **`dst evals from-traffic <lens>`** — production questions become the
  suite: recent request-log rows are drafted into `evals/cases.yaml` with
  the observed outcome shape as the expectation, ready for review.
- A new **[Environments and CI](docs/guides/environments-and-ci.md)** guide,
  including the per-PR ephemeral-environment recipe, and CLI reference
  sections for every verb above. The scaffolded agent skills teach the same
  loop (`dst runs --diff` as the re-measure verdict, `dst experiment` for
  config forks, disposable envs in the agent workflow).

## [0.2.0] — 2026-09-07

Schema changes ship with this release: run `dst migrate` before serving (the
upgrade contract above).

### Changed — relationships are their own files

A join is a fact about a *pair* of entities, so it no longer lives inside one
of them. Each pair gets one file:

```yaml
# semantic/relationships/orders__customers.yaml
left: orders          # the FK side (the many side of many_to_one)
right: customers
"on": orders.customer_id = customers.customer_id
type: left
relationship: many_to_one
```

**To migrate an existing project:** move each entity's `joins:` list into
per-pair files — `left:` is the entity that carried the block, the other
fields are unchanged. An entity file still carrying `joins:` fails at plan
and apply with a pointer to the new home; nothing is silently ignored.

What one home per pair buys:

- Declaring the same two entities twice — reversed direction included — is
  refused at plan and apply as a competing claim, instead of both declarations
  silently compiling.
- A lens gets the join exactly when it selects both endpoints; lens files are
  unchanged.
- Staleness and re-verification get sharper: adding, editing or deleting a
  relationship recompiles exactly the published lenses that select both
  endpoints, and flags for re-verification exactly the certified answers
  whose SQL joins over it.
- `dst drift` names the relationships a schema change breaks (a dropped
  column referenced in an ON clause, a dropped table a pair joins through).
- `dst import dbt` and OSI import/export read and write the new shape;
  `dst introspect --check-joins` measures the declared cardinalities as
  before.

### Added

- **Ambiguity that resolves itself.** An ambiguous definition can declare
  `audiences:` — which meaning each audience of a lens gets — and a question
  that already settles the ambiguity is answered deterministically instead of
  triggering a clarify round. Genuinely open ambiguity still asks.
- **Freshness measures coverage, not just recency.** Profiling records the
  date coverage of a table, and a period the warehouse never loaded is
  disclosed in the answer rather than served as a silent zero.
- **Certified corrections converge.** A reviewed correction wins over the
  answer it corrects, each question holds one certified pair, and
  un-certifying is a first-class path back out.
- A composed answer whose prose was cut off by the token cap now says so
  instead of ending mid-sentence as if complete.

### Fixed

- Numbers formatted in locales that use space grouping and decimal commas
  (e.g. `1 234,56`) ground correctly during answer verification.
- A filter the lens demands but the question never bound is waived with a
  disclosure instead of dead-ending the request.
- A cost total that could not be fully counted is labeled as such instead of
  reading as a counted total.
- A lens with an empty access allow-list reports that it refuses every
  caller, instead of implying admin-only access.
- Columns the model references in expressions count as modelled for the
  read allow-list.
- Authoring errors got sharper: a required key names what it is for, and the
  YAML flow-map comma trap (`{a: one, two}` inventing a phantom key) is
  named as such with the fix.
- The eval gate runs cases at concurrency 16 by default (was 4), so a
  full-corpus gate finishes in minutes.

## [0.1.1] — 2026-08-29

### Security

Dependency refresh clearing every open Dependabot advisory against the locked
development environment. User installs resolve dependencies freshly and were
already receiving the patched versions; this pins the repository's own
toolchain and CI to the same ground.

- `cryptography` 48.0.0 → 50.0.1 (PKCS#7 Bleichenbacher oracle, certificate
  path-building exhaustion, name-constraint wildcard escape)
- `mcp` 1.27.2 → 1.29.1 (WebSocket transport Host/Origin validation — dst
  serves MCP over streamable HTTP, so the vulnerable transport was never
  exposed, but the SDK moves past it regardless)
- `pyasn1` 0.6.3 → 0.6.4 (three decoder denial-of-service advisories)
- `pillow` 12.2.0 → 12.3.0 (development lockfile only — Pillow is not a
  dst-core dependency and never reaches an installed copy)

No schema changes: upgrading from 0.1.0 is `pip install --upgrade dst-core`
with nothing to migrate.

## [0.1.0] — 2026-08-25

The first public release. Nothing public preceded it, so this entry describes
what dst is, not what changed.

dst is the governed layer an AI calls instead of opening a raw connection to
the warehouse. A caller — the AI your team uses over MCP, or the agent inside
your product over MCP or REST — asks a natural-language question; dst grounds
it in your semantic files, generates SQL inside a lens's allow-list, executes
it read-only, and returns a cited answer with the SQL, a verification grade,
and the cost attached. What the lens cannot answer is declined, never guessed.

- **Lenses and semantic files are the truth.** Definitions, entities and
  access live in versioned files; `dst plan` shows what a change does and
  `dst apply` publishes it. Static inconsistency dies at apply, not at 2am.
- **Certified answers are regression tests.** A reviewed correction is served
  verbatim from then on and pinned by `dst test`, so a mistake fixed once
  stays fixed.
- **Every answer carries its receipt.** Provenance for the caller; a native
  audit ledger, review queue and drift audit for the platform engineer — all
  views over the same request log.
- **One pipeline for every caller.** MCP and REST enter the same answer path,
  so who asks changes access, never the answer.

Install `dst-core` from PyPI, or run the container image. `dst init` scaffolds
a project with a demo warehouse; the [quickstart](docs/quickstart.md) goes
from empty directory to a governed answer.
