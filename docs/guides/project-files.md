# Project files

The project directory is the source of truth. `dst init` scaffolds it, `dst apply`
deploys it, and the server's job is to serve it, measure it, and tell you when it has
drifted. The UI never authors: files do, versioned in your git repo like the rest of
your code.

```
dst.yaml                      providers + connection declarations
.env                              secrets, referenced by env-var NAME only (gitignored)
semantic/entities/<name>.yaml     shared entities (name is identity, unique project-wide)
semantic/definitions/<term>.md    shared governed terms (frontmatter + prose)
lenses/<name>/lens.yaml           selection + policy (+ `timezone:` — the lens's business clock)
lenses/<name>/queries.yaml        use_when (router anchors) + sample_queries
lenses/<name>/definitions/*.md    lens-LOCAL terms only
lenses/<name>/certified_answers.yaml
lenses/<name>/evals/cases.yaml    behavioral expectations
lenses/<name>/compiled.yaml       server-rendered artifact — read it, never edit it
lenses/<name>/README.md           runtime output, loader ignores it
profiles/<connection>.probe.json  committed warehouse profile (`dst probe`)
profiles/<connection>.json        drift baseline (`dst drift --accept`, also written by `dst probe`)
```

Subfolders are organization only — the asset *name* is identity — so anything
under `semantic/entities/**.yaml` loads. That is why `dst init --example`
parks its demo assets in `semantic/entities/examples/` and
`semantic/definitions/examples/`, clear of the layer you author. Without
`--example`, `init` creates those directories and writes no assets at all.

Only *managed* paths participate in plan/apply: `lens.yaml`, `queries.yaml`,
`certified_answers.yaml`, `evals/cases.yaml`, and `definitions/*.md`
(`services/project/loader.py:27`). `README.md`, `compiled.yaml`, `audit/*`, and
`certified/*` are runtime outputs the loader skips. Shared entities and definitions live
under `semantic/`, never under `lenses/`, and each [lens](../concepts/lens.md) selects
from them in its `lens.yaml`.

## Secrets discipline

Secrets never appear in files. A connection declares `secret_env: DST_API_KEY_BIGQUERY`
and the value lives in `.env`; an inline provider `api_key` is rejected at schema level:
`dst.yaml` is committed to a repo (`services/project/schema.py:53`). Env resolution
(`services/config.py:461`) reads the process environment, then does a *live* read of
the project dir's `.env`, and treats `@/path/to/file` as "load that file's contents":
the idiom for a BigQuery service-account JSON. An `@`-ref that cannot be read is an
error, never a silent empty value.

## plan / apply / export

- **`dst plan`** is a dry run: per-path diffs, plus `stale_lenses`, published lenses
  whose compiled provenance no longer matches the shared assets they selected. Edit a
  shared entity and every lens selecting it is named stale (`services/project/plan.py:309`).
  Rendering is deterministic (`compiled.yaml` carries no timestamp,
  `services/lenses/repo.py:110`), so `plan` stays quiet unless something real changed.
- **`dst apply`** is blue/green and atomic: one transaction under a per-org advisory
  lock, ordered connections → shared assets → lenses → recompile-stale pass. Any error
  aborts everything and prior versions keep serving; warnings never abort. Every *changed*
  warehouse declaration is probed (connect + read) before landing — unchanged ones are
  skipped, so a working connection is never re-tested for nothing: a dead credential
  never replaces a working one, and the error names the env ref to fix
  (`services/project/apply.py`).
- **`dst export`** writes server-side lenses into the project directory: the adoption
  path for lenses that predate the file model. It prints a `connections:` snippet to merge
  by hand, never auto-writes it.

## Every publish is a version

Each publish records a monotonically numbered `lens_version` with the full lens bundle.
The browsable file tree (`GET /mgmt/lenses/{name}/repo`) and version diffs are re-derived
from bundles by a pure materializer (`services/lenses/repo.py:65`): the bundle is
canonical, the tree is a render. The dashboard's Files tab is this tree with per-version
diffs.

## The deterministic rails on an entity

Three entity keys do more work than everything else in the file, because the
machinery *enforces* them instead of hoping the model reads them. Prose in
`description` steers generation non-deterministically (obeyed for one
phrasing, violated for the next), and apply warns (`constraint_in_prose`) when a
rule is written there. These are the structural homes for those rules:

```yaml
# semantic/entities/account_feature_usage.yaml
name: account_feature_usage
source: { connection: bq, table: marts.account_feature_usage }
population: "Active paying accounts only — trials and internal test accounts are excluded upstream."
population_filter: "account_feature_usage.is_active_paying = TRUE"
pinned_dimensions: ["currency"]
```

- **`source.table`**: the physical table, and the allow-list a lens over this
  entity may read. Qualify it only as far as the connection does not: a BigQuery
  connection pinning `project:` (or a Snowflake one pinning `database:`) gets
  that catalog stamped in at compile time, so `marts.orders` compiles to
  `acme-prod.marts.orders` and the same file serves a second environment
  unedited ([Environments and CI](environments-and-ci.md)). A table that names
  its own catalog is left exactly as written.
- **`population`**: one sentence declaring who or what the rows cover. It rides
  the generation prompt, and the serve-time `population_declared` check requires
  answers over this entity to carry the scope, so a partial population can never
  read as the whole business.
- **`population_filter`**: a SQL predicate the **compiler ANDs into every query**
  against this entity, regardless of what the model generates. This is the one
  scope mechanism that generalises across phrasings and holds under adversarial
  prompting ("just the number, no caveats"), because no model decides whether it
  applies. It is not a substitute for lens scope: it bounds rows *within* a
  table the lens already exposes.
- **`pinned_dimensions`**: dimensions that must be pinned to one value or
  GROUPed before any aggregate. The structural form of "never sum across
  currencies": the `aggregation_scope` serve check enforces it deterministically.

### Which mechanism for which problem

| The wrong answer looks like… | Reach for | Deterministic? |
|---|---|---|
| rows outside the intended scope counted in | `population_filter` | yes; compiler ANDs it in |
| a partial population read as the whole | `population` | yes; serve check requires the caveat |
| summed across currencies / entities that must not mix | `pinned_dimensions` | yes; serve check |
| a metric served without its defining constraint | `filters:` on the metric | yes; filter guard |
| a term's meaning never checkable in answers | `sql:` on the definition (alias `sql_expr`) | yes; definition_applied check + generation grounding |
| a term with two meanings silently guessed | `status: ambiguous` + `aliases` | yes; clarifies instead |
| a metric that must never serve from this lens | leave it out of `select:` | yes; refusal |
| known-good numbers that must not drift | certified answer | yes; served verbatim, re-verified |
| tone/derivation guidance for the prose | `description` / definition prose | no; advisory |

The deterministic rows are guarantees; the advisory row steers. When a wrong
answer costs money, encode it in a deterministic rail and keep the prose as
explanation.

## grain and primary_key: who reads which

Both describe what one row is, and they have completely separate consumers:

- **`grain`** is free prose and goes only to the model: rendered verbatim into
  both generation prompts (`services/runtime/generator.py`,
  `services/runtime/intent_generator.py`). Nothing validates it.
- **`primary_key`** is a column list and is never shown to the model. It is read
  by machinery: the time guard treats a GROUP BY on a key column as deliberate
  row-grain grouping and skips its repair (`services/runtime/time_guard.py`),
  the reference resolver checks the named columns exist
  (`services/semantic/resolve.py`), and the OSI export carries it.

`grain` is where the sentence that prevents a double-count lives: `description`
says what the table holds, `grain` says what counting the rows would actually
count: "one row per order line, so summing `amount` double-counts the order".

The honest caveat: because `grain` is prose the model trusts and nothing checks,
a wrong declared grain ("one row per order" written over an order-lines table)
makes the model confidently double-count with that sentence in its prompt. That
is precisely the failure `grain` exists to prevent, and today it is the one
claim on an entity no gate can catch. `primary_key` is the structural version of
the same fact and is resolvable, but it is not cross-checked against `grain` and
not probed against the warehouse for uniqueness. (The dbt importer's coverage
report counts "entities with grain" from `primary_key`, not from `grain`.)

Three different things are called "grain": on an entity, what one row of the
table is; on a definition, what one row of that term's result is, rendered with
an aggregate-at-this-grain, dedupe-before-summing instruction; and inside the
query the model plans, a time bucket (month, week, day) it sets for an over-time
question, unrelated to either of the above.

### Currency is never guessed

`currency:` on a metric is the author's judgment about their own data. Set it
and the composer states the amount in that currency; leave it unset and the
answer carries a bare number and asserts no currency at all
(`services/contracts/semantic_model.py`). The product never infers one: a bare
number beats a wrong symbol.

## Freshness is a declared contract

Two facts ride every answer, one measured and one declared. `data_as_of` is
measured: read from the stored table profiles, never asserted.
`stale_after_days` on `lens.yaml` is what you declare: how old is too old for
this use case, which nothing downstream can infer. Past it the freshness check
fails, the confidence grade caps at `partial`, and the answer says so,
certified serves included. Undeclared, the check reports skip, never a vacuous
pass.

It sits on the lens rather than on a table because tolerance is a property of
the question, not the data: the same `orders` table is fresh enough for a
finance close and too stale for an ops board, and only the use case knows which.

Know how `data_as_of` is measured before setting it: it is the **oldest**
last-update across every entity in the lens's scope, not the tables an answer
actually touched, and profiles are read from the lens's **first declared
connection only** (`services/runtime/assembly.py`). Two consequences. A single
slow-moving table in scope (a country lookup that legitimately hasn't changed
in months) drags `data_as_of` down and can mark every answer from the lens
stale, including questions that never read it. And on a multi-connection lens,
freshness on the second connection is not measured at all. So set it against
the slowest thing in scope, or split the slow-moving dimension into its own
lens, and leave it unset until you have checked what `data_as_of` actually
reports for that lens.

## Not everything you author reaches every prompt

A lens with metrics generates in two tiers: the first pass is the compiled
metric-layer prompt, and some authored assets (joins, sample queries,
`instructions`) ride only the escalation prompt, reached when that pass fails.
`dst apply` says so per lens with the `intent_tier_escalation_only` warning,
naming exactly what this lens loses on the first pass;
`dst lens prompt <lens> "<question>"` renders both prompts so you can check
what actually reached the model.

## Deletion is explicit: certified answers follow their file

File absence never deletes *objects*: `dst lens rm` prints the cascade first and
asks; `dst semantic rm` is the only way to remove a shared asset, and the server
refuses while any published lens still selects it (`services/semantic/store.py:103`):
deselect it in `lens.yaml` and apply first. Certified answers are the one file-owned
exception: a pushed `certified_answers.yaml` owns its file-originated entries, so
removing an entry deletes it on the next apply (the apply row counts `deleted N`).
Review-approved answers (`source: review:*`) are server-origin and survive file
absence, and a tree that carries no `certified_answers.yaml` leaves the surface
untouched; `plan` diffs exactly what `apply` will do.

## The scaffold teaches

`dst init` writes a dbt-style project: a demo DuckDB warehouse option, per-warehouse
credential prompts, `git init`, an `AGENTS.md` guide for coding agents, and six Claude
Code skills: [reviewing the warehouse for answerability](answerable-tables.md) before
building on it, authoring the semantic layer, writing curated context,
importing verified BI queries as certified answers,
[bootstrapping from query history](drift-audit.md), and running the
[correction loop](correction-loop.md) from a wrong answer back to a certified one
(`services/cli/init.py`). `dst.yaml` and `lens.yaml` end with a commented reference
block rendered from the actual config schema (`services/project/template.py`), and
`semantic/README.md` and `lenses/REFERENCE.md` carry the rest: uncomment fields from
the reference instead of guessing names.
