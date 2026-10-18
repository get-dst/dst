# Project files

The project directory is the source of truth. `dst init` scaffolds it, `dst apply`
deploys it, and the server's job is to serve it, measure it, and tell you when it has
drifted. The UI never authors: files do, versioned in your git repo like the rest of
your code.

```
dst.yaml                      providers + connection declarations
.env                              secrets, referenced by env-var NAME only (gitignored)
semantic/entities/<name>.yaml     shared entities (name is identity, unique project-wide)
semantic/relationships/<left>__<right>.yaml  one join pair per file (left = the FK side)
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
parks its demo assets in `semantic/entities/examples/`,
`semantic/relationships/examples/` and
`semantic/definitions/examples/`, clear of the layer you author. Without
`--example`, `init` creates those directories and writes no assets at all.

Only *managed* paths participate in plan/apply: `lens.yaml`, `queries.yaml`,
`certified_answers.yaml`, `evals/cases.yaml`, and `definitions/*.md`
(`services/project/loader.py:27`). `README.md`, `compiled.yaml`, `audit/*`, and
`certified/*` are runtime outputs the loader skips. Shared entities and definitions live
under `semantic/`, never under `lenses/`, and each [lens](../concepts/lens.md) selects
from them in its `lens.yaml`.

## `queries.yaml`: use_when and sample_queries

```yaml
use_when:
  - revenue, VAT, medical vs retail split, payers, revenue per clinic
sample_queries:
  - question: net revenue per clinic in September 2026
    sql: >
      SELECT clinic_code, SUM(revenue_eur) FROM marts.fct_revenue_daily
      WHERE day >= '2026-09-01' AND day < '2026-10-01' GROUP BY 1
```

`use_when` is a list of strings the router anchors on. `sample_queries` is a
list of `{question, sql}` pairs — both keys required, the SQL in the lens's
dialect (`services/contracts/semantic_model.py`, `SampleQuery`); a bare string
is rejected at plan with "valid dictionary", a pair without `sql` with "sql:
required". They reach the raw-SQL prompt as worked examples and `dst test` can
run them.

### Template slots render as typed literals

A certified template's `{slot}` renders as a proper literal for the dialect —
`'2026-09-01'` for a date or a date_range bound, `42` for a number, `'EUR'` for
an enum. A function that needs a typed argument still needs the cast the
warehouse would need for any string literal: DuckDB's `date_diff('day',
{period.start}, day)` is a binder error, `date_diff('day', CAST({period.start}
AS DATE), day)` runs. Comparisons (`day >= {period.start}`) need none. The eval
gate at apply catches the uncast form; the cast is yours to write.

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
- **`hide`**: column patterns the entity withholds from every door (fnmatch on
  the column name, bare or as `entity.column`). A hidden column is absent from
  the lens: generated SQL, the `sql` tool and `describe_lens` never see it, and
  naming it is out of scope on every one of them. Plan fails on a pattern that
  matches no field, and on a hidden column a dimension, metric, key or filter
  still reads: hiding less than you believe is the silent miss this exists to
  stop. Plan also warns `sensitive_columns` when an entity in scope carries a
  column named like personal data (`personal_id`, `ssn`, `email`, `phone`,
  `birth_date`, `date_of_birth`, `address`) with no `hide` on it and no
  definition `about` it; the column reaching a door is a declared schema fact,
  and the author decides whether the hide or the definition is the fix.

## A declared refusal holds on every door

`not_computable` in `lens.yaml` names a measure the lens must refuse rather than
approximate: `measure`, `aliases`, an optional `route_to` and `reason`. The
`query` door refuses a question naming any of them, deterministically, and so
does the `sql` door: a caller-written statement that names a column the entry
names (`birth_date` for an alias `birth date`, projected or filtered) is refused
with the same sentence. With `row_level: true`, the listing itself is the
measure: a statement that returns one row per entity key (a key column in its
projection outside any aggregate; `keys:` names them, defaulting to the lens's
entity primary keys) is refused too, whatever the columns beside it are called,
while `COUNT(DISTINCT key)` stays a count.

```yaml
not_computable:
  - measure: patient identifiers
    aliases: [email, personal id, birth date, date of birth, which patients]
    reason: "a list of identifiable people is not analytics output — ask the data owner"
    row_level: true
    keys: [patient_id]
```

`route_query` carries the same refusal: when it would decline and a lens the
caller may use declares a term the question names not computable, the response
is that lens's refusal (`routed_to.decided_by: refusal`, `answer.status:
refused`) rather than "no lens covers this".

### Which mechanism for which problem

| The wrong answer looks like… | Reach for | Deterministic? |
|---|---|---|
| rows outside the intended scope counted in | `population_filter` | yes; compiler ANDs it in |
| a partial population read as the whole | `population` | yes; serve check requires the caveat |
| summed across currencies / entities that must not mix | `pinned_dimensions` | yes; serve check |
| a metric served without its defining constraint | `filters:` on the metric | yes; filter guard |
| a term's meaning never checkable in answers | `sql:` on the definition (alias `sql_expr`) | yes; definition_applied check + generation grounding |
| a rule on one period (a duplicated export month) that certified SQL reads past | `sql:` + `period:` on a definition `about:` the entity | yes; definition_period check on every certified serve, `certified_ignores_definition` at plan/apply |
| a term with two meanings silently guessed | `status: ambiguous` + `aliases` | yes; clarifies instead |
| a metric nobody says by its identifier (`otd_rate`) resolved to a sibling | `label:` / `aliases:` on the metric | yes; the question names it word for word, and a bare metric question compiles without a decision |
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

### A level metric is a snapshot, not a sum

`additivity: level` on a metric says its rows are snapshots of a stock (ARR,
headcount, a balance) rather than events. The compiler then reads a period's
figure as the **last snapshot inside the window** on the metric's time field
(`agg_time_field`, else the entity's `default_time_field`), and a trend by
month as each month's last snapshot. The default, `flow`, aggregates every row
in the window, which is right for revenue and wrong for ARR: "total ARR for Q3"
over month-end snapshots would otherwise sum three balances into one number.

```yaml
metrics:
  - name: arr
    agg: sum
    expr: arr_snapshots.arr
    additivity: level
```

A level metric needs a time field, and apply says so when there is none. A
level and a flow cannot share one query (the window cannot both keep every row
and keep only the last snapshot), so a question naming both is asked to split.
The answer discloses the reading (a `Reading applied:` sentence naming the metric) and
the ledger carries it as a declared window slot, so a snapshot read as a total
is never silent. "At the end of August 2026" and "as of 2026-08-31" are windows
the parser already understands: the month, and the day.

### A ratio across additivities: `period_weight`

Utilisation is scheduled appointments (a flow, summed over the month) over
slot capacity per week (a level: the month's plan, repeated on every day)
times the weeks in the month. A level beside a flow is refused in one intent,
but a **ratio metric** can declare how the level is weighted:

```yaml
metrics:
  - name: planned_capacity_slots
    agg: max
    expr: clinic_capacity_daily.slot_capacity_per_week
    additivity: level
  - name: scheduled_appointments
    agg: sum
    expr: clinic_capacity_daily.scheduled_appointments
  - name: utilisation
    type: ratio
    numerator: scheduled_appointments
    denominator: planned_capacity_slots
    period_weight: weeks        # weeks | days
```

The compiler sums the flow over the window, reads the level at the window's
latest snapshot (the same correlated pin a level metric uses on its own) and
multiplies it by the window's length — `days / 7` for `weeks`, `days` for
`days` — as a number in the served SQL, so the weight is visible on every
answer. Exactly one operand must be a level. The window must be bounded on
both ends ("in September 2026"); an open window has no length, and a trend
(`grain`) is one window at a time — both are refused with the reason, never
guessed.

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
last-update among the tables the answer's SQL actually read, resolved the way
the SQL guard resolves them, and the stamp names the table (`data as of
2026-10-14, orders`; several tables, the oldest of them). Only an entity with
a `default_time_field` dates an answer; a dimension (a country lookup that
legitimately hasn't changed in months) dates one only when it is the only
table read, so a join to it never marks an orders question stale. Profiles
are read from the lens's **first declared connection only**
(`services/runtime/assembly.py`), so on a multi-connection lens freshness on
the second connection is not measured at all. Set it against the tables your
questions read, and leave it unset until you have checked what `data_as_of`
actually reports for that lens.

There are two ages here, and they are not the same thing. Data freshness is
how old the newest row is: `data_as_of`, measured by the probe. Profile age is
how old the probe itself is: `profiled_at` in `profiles/<connection>.probe.json`,
which `dst apply` carries into the store. A warehouse loaded this morning
behind a profile taken three weeks ago still reports `data_as_of` as three
weeks ago, because nothing has measured it since; and a value dictionary from
that probe cannot prove a value loaded since then absent. So the profile has
its own limit, `probe.max_age` in `dst.yaml` (default seven days). Past it the
freshness and date-coverage checks skip rather than fail on a stamp that only
dates the last probe, the value guard stops consulting dictionaries, and the
answer carries `PROFILE: last probed N days ago (max M) — run dst probe` in
`degraded`. The fix is the probe job, not the lens: run `dst probe`, commit
`profiles/`, apply.

## Not everything you author reaches every prompt

A lens with metrics generates in two tiers: the first pass is the compiled
metric-layer prompt, and some authored assets (joins, sample queries,
`instructions`) ride only the escalation prompt, reached when that pass fails.
`dst apply` says so per lens with the `intent_tier_escalation_only` warning,
naming exactly what this lens loses on the first pass;
`dst lens prompt <lens> "<question>"` renders both prompts so you can check
what actually reached the model.

## Exposing tables that have no definitions

Not every read is a governed one. Investigative work on staging tables or a stray
dimension needs the columns, not a metric, and an entity file per such table is a
meaning-free file that rots with every upstream column. A lens can instead declare a
grant in `lens.yaml`, beside `select:`:

```yaml
expose:
  - connection: warehouse
    tables: ["staging.*", "analytics.dim_*"]
    hide: ["*.ssn", "staging.customers.email"]
```

Patterns are fnmatch globs (`*`, `?`, `[abc]`), case-insensitive, on the table name
exactly as `dst introspect` names it: `schema.table` on most warehouses, and on
BigQuery `project.dataset.table`, so a BigQuery grant reads `*.analytics.dim_*` (or
spells the project); a bare `analytics.dim_*` matches nothing there and apply says so.
Regular expressions are not supported. `hide` patterns match `table.column`, with the table bare or qualified. The
grant is resolved at **apply**, against the connection's stored profiles (`dst probe` /
`dst introspect` push them; a probe artifact in the same push counts). Each matched
table becomes an entity in the compiled model: name = the bare table name, fields = the
profile's columns minus `hide`, description = the profile's, and no metrics, dimensions
or time field. Because it is resolved from the profile, the columns are the warehouse's
as of the last probe; re-apply after a probe and the entity follows the table.

Apply fails loud when a pattern matches no profiled table (the error names the
pattern: run `dst probe` or fix it). It also fails on a name collision, because an
exposed table is addressed by its bare name and two things cannot answer to one:
a matched table whose bare name is an entity the same lens selects
(`staging.orders` beside a selected `orders`) fails, naming both and asking you to
exclude it from the pattern or drop the entity from `select`; and two exposed
tables that share a bare name (`staging.customers` and `analytics.customers` under
one lens) fail, asking you to narrow the patterns so one lens exposes one table
under that name. Two patterns matching the same table is not a collision: it
becomes one entity. `dst plan` prints `exposes N tables (M columns)` per
lens, `(M columns, H hidden)` when `hide` withheld any, and warns when `expose` is set
with `untyped_fallback: false`: a question about an exposed table never types, so
`query` cannot answer from it until the lens accepts untyped answers (the `sql` tool
reads it either way).

When a table disappears upstream, the next `dst probe` no longer lists it, and the
push that carries that artifact fails the grant: a stored profile the artifact does not
list counts as gone, so a pattern that matched only that table fails apply with the
same "no profiled table matches" error, naming the table as dropped upstream. The
stored profile itself is not deleted by the push (a file's absence never deletes server
state, so an old commit cannot downgrade a server), and for the same reason an artifact
older than the stored profile cannot declare the table gone. A push with no artifact
resolves from the store as before. `dst probe --tables a,b` narrows only the sampling
pass; the artifact still lists every table the connection has.

What an exposed table is, on every rail:

- the SQL guard admits its tables and columns like any entity's. A hidden column is
  simply absent from the lens, and the guard enforces that on every door a lens
  serves, generated SQL and caller-written SQL alike (`query`, the `sql` tool, the
  REST endpoints behind them), in all three ways the column could be named: qualified
  (`c.ssn`) is out of scope, bare (`ssn`) is out of scope, and `SELECT *` (or `t.*`)
  never carries it. A star is allowed only when every table in the statement is an
  exposed table, and is then rewritten into the explicit columns the lens lists for
  that table, so neither the returned SQL nor the rows include a hidden column; a
  statement that also reads a declared entity keeps the ban. A table reference may
  omit leading qualifiers the source has, never add one: `staging.orders` is not the
  lens's unqualified `orders`.
- the typed resolver never offers it; the raw-SQL prompt lists it with its columns
  and the line `no governed definitions`; the router takes no anchors from it.
- an answer that reads one carries a line in `degraded`, in the same idiom as
  `UNTYPED`: `EXPOSED: reads staging.orders, a table with no governed definitions`,
  and grades at most `partial` — the `governed_read` check names the ungoverned
  read. Its columns never count as declared sources in the resolution ledger, so a
  measure read from it is `inferred`, never `declared` or `certified`.
- `describe_lens` lists it under `exposed_tables`, apart from `entities`, with a note
  that answers from it are ungoverned.

Promote a table into a real entity the moment a number from it is going to be
repeated: the first metric, the first definition someone asks about, the first
certified answer. An exposed table is for looking; an entity is for serving.

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
credential prompts, `git init`, an `AGENTS.md` guide for coding agents, and seven Claude
Code skills: [reviewing the warehouse for answerability](answerable-tables.md) before
building on it, authoring the semantic layer, writing curated context,
importing verified BI queries as certified answers,
[bootstrapping from query history](drift-audit.md), and running the
[correction loop](correction-loop.md) from a wrong answer back to a certified one
(`services/cli/init.py`). `dst.yaml` and `lens.yaml` end with a commented reference
block rendered from the actual config schema (`services/project/template.py`), and
`semantic/README.md` and `lenses/REFERENCE.md` carry the rest: uncomment fields from
the reference instead of guessing names.
