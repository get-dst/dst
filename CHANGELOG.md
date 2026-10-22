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

## [Unreleased]

No schema change.

### Added

- **Troubleshooting guide and a `dst-debug` skill.** `docs/guides/troubleshooting.md`
  names the failure modes seen against real warehouses (runs that cannot be found,
  certified answers that never match, slot-lane gold, `did not type`, apply timeouts,
  routing between sibling lenses, test traffic in the KPIs) with the evidence to read and
  the owner of each: dst, the project's files, or configuration. The scaffold ships the
  same table as a seventh skill, `.claude/skills/dst-debug/`; `dst init --skills-only`
  adds it to an existing project.
- **`dst runs --all [--last N]`.** Every lens on the server with its last N recorded runs
  (default 5), as a table or `--json` (`{"runs": [{"lens", "runs": [...]}]}`); a lens with
  no history is listed as such. `--diff` still takes one lens.
- **Stable ids on `dst test --json` rows.** Data-lane and slot-lane rows carry `answer_id`
  (the certified answer's id, what `eval_result.case_id` holds as `certified:<id>`), and
  behavioral rows carry `case_id`, so a report joins history on the id instead of the
  question text, which a rewording breaks.

### Fixed

- **`eval_result.actual_value` was never written.** A certified case now stores the
  generated value there, and `checks` carries both `certified` and `generated` as the
  JSON output renders them, so a copy of the eval tables holds the numbers, not only
  verdicts and SQL.
- **Month-grain rows compared on their day label.** A trend certified on month-end dates
  against a generation that labels each bucket by its first day carried identical values
  and graded as every row missing and extra. When every date column on both sides names
  the same months, the rows match within the usual tolerance, and no row folds into
  another, the case passes with a `month-lenient:` reason; such a pass is never stored as
  typed gold.

### Changed

- **`dst test` says where the run went.** The sweep ends with a `recorded:` line per lens
  naming the run id and the database it was written to (`DATABASE_URL`, password hidden),
  and `--json` carries `recorded` and `database`. `dst test` writes in-process while
  `dst runs` and the dashboard read through the server at `DST_URL`; when those name
  different databases the run existed and nothing in the output said where. The empty
  `dst runs` message now says the same, and the docs name the failure mode (FAQ,
  Evaluation, Environments and CI).

## [0.14.0] — 2026-10-22

Three migrations: 0070 (stored serve bands per embedding model), 0071 (`ai_call`, one row per
provider call; token and cost columns on eval runs and results), 0072
(`ai_call.cache_write_long_tokens`). Run `dst migrate` before serving.

New keys: lens `sources` and `scope`; metric `window_reading`; dimension `aliases`; entity
`internal`; project `lineage`. New selectors `table/NAME`, `*` and `--tables-from`. New
provider type `openai-decisions`. New command `dst calibrate-bands`.

Behaviour changes to read before upgrading:

- A ratio or derived metric asked with no period reads the last complete month and names it.
  0.13.0 asked which period.
- Metric aliases no longer route questions between lenses. A metric routes by its name or
  label; aliases still work inside the lens.
- A word qualifying what the answer counts must map to something the lens declares (a metric,
  field, dimension, stored value, definition, period, or the entity's description). A scope
  word nothing declares now asks instead of being ignored.
- A certified answer is not served for a question that names a different stored value or
  true/false field than its certified question.
- `dst drift` lists a comment-only change as comment only: no warning on readers, no effect on
  the exit code.
- A typed-decision provider that refuses a question declines it and names the refusal.

### Added

- **Table selectors.** `--select table/NAME` picks the entities reading a warehouse table;
  `table/NAME+` adds their definitions and every lens reading them. Names match
  case-insensitively on the qualified name or its trailing parts.
- **Lineage from dbt or SQLMesh.** With `lineage:` in `dst.yaml` (a dbt target directory or a
  SQLMesh project) or `--lineage PATH`, `table/NAME+` starts from a model, seed or source and
  walks the tool's DAG down to the tables entities read. dst runs neither tool.
- **`--tables-from FILE|-`** selects `table/NAME+` for each name read, one per line, so
  `dbt -q ls -s state:modified+ --resource-type model --output name | dst apply --tables-from -`
  re-gates exactly the lenses a change touches. A selection that reaches no lens names the
  tables no entity reads, pushes nothing and exits 0.
- **`--select '*'`** selects the whole project on `dst plan`, `dst apply` and `dst test`, the
  same as leaving `--select` out.
- **`dst test`** takes `--select`, `--tables-from` and `--lineage`, and runs only the lenses
  the selection reaches. The plan and apply selection row carries `paths` (table → entity →
  lens).
- **OpenAI Decisions API** as a typed-decision provider, `type: openai-decisions`
  (`gpt-6-luna`): key from `OPENAI_API_KEY` by default, independent decisions batched into one
  call, probabilities recorded as returned. With several typed-decision providers declared, the
  first declared one with a key decides. `DST_TYPED_SERVING=auto` serves typed on any of them.
- **Declared source systems.** A lens can declare `sources` (the systems its data comes from)
  and `scope` (`own` or `market`). A question naming a declared system routes only to lenses
  declaring it; one naming a system no lens the caller may use declares is uncovered, and the
  reason names the system.
- **`window_reading: snapshot | any_in_window`** on level metrics. `any_in_window` counts each
  thing flagged at any snapshot of the period once. Typed answers and the certified gate read
  the same declaration; a certified answer whose SQL reads the other way fails the apply gate.
- **"At the start of" a period** reads a level's first snapshot in it. Structured intents accept
  `snapshot: first | last`.
- **Period comparison from the question's words.** Growth, change and "vs" wording compares
  periods on typed serving; "which X grew most" ranks by the change; with no period stated it
  asks, offering the last complete one.
- **Dimension `aliases:`** for breakdowns, rankings and binding keys. Plan fails when one word
  names two dimensions.
- **Entity `internal:`** keeps columns, a surrogate primary key included, out of every filter,
  breakdown and ranking offer and out of the metric catalog, while they stay readable.
- **`dst calibrate-bands --pairs FILE`** fits the certified serve bands for the configured
  embedding model from labelled pairs and stores them for that model. With fewer than 20 pairs
  of each kind it stores nothing and says how many more are needed.
- **One usage record per provider call** in `ai_call`, with model, tokens, cost and purpose
  (route, decide, judge, equivalence, bind, generate, compose). Eval runs and results store
  their tokens and cost. `dst observe` shows AI and warehouse cost apart and AI spend by
  purpose.
- **`dst plan`** hints SUM metrics with no `additivity:` on tables keyed by a date and another
  column.

### Changed

- **Routing.** A lens is dropped when the question names one of its `not_for` asks or reads
  closer to one than to anything the lens covers, on every routing path; the routing decider is
  shown each lens's asks, and `Avoid for:` lines no longer count as coverage. A question naming
  a metric's name or label that only one lens declares routes to that lens before similarity. A
  metric several lenses declare goes to the one that alone carries a table or entity the
  question names, or alone can split it over time; otherwise the question asks which lens,
  naming them. A lens whose anchors are all shared with a sibling still reaches the decider.
- **Two clocks.** On an entity with two time fields, a period goes on the date the asked
  metric declares, else the one the question names, else the question asks which date.
- **Probes.** `dst apply` re-gates only lenses whose scope reads a table a pushed probe
  re-profiled. `dst probe --tables a` keeps the previous probe's sampled facts for every table it
  did not sample. A refresh reads a table whole when the stored probe never did, and names any
  value dictionary it leaves partial that was complete. `dst probe` counts the billed metadata
  read and states BigQuery's 10 MB per-statement minimum. A table over `sample_bytes_budget` is
  recorded as catalog-only with the reason, which describe and value clarifications repeat.
- **Embeddings.** Plan and apply name a missing embedder on any lens with certified answers;
  answers on such a lens say how many certified answers cannot serve and what to install.
  `dst init` declares the local embedder when its extra is installed. Plan, apply and `/ready`
  say when the serve bands were not calibrated for the configured model.
- **Prices.** Built-in prices cover current Claude and OpenAI models, cited from the providers'
  pages; Claude Sonnet 5 is $2/$10. Prompt-cache reads and writes are priced at each model's
  published rates. A call reporting tokens its model has no rate for is unpriced and named on
  `/ready` and by `dst doctor`, never counted as free. `gpt-6-luna` is priced by endpoint. Rates
  in `ai_pricing` accept cache read, write and long-lived write, and a `"<endpoint>:<model>"`
  key.
- **Clarifications** ask in words and never offer internal labels; "which kind" lists the
  stored values on every door.

### Fixed

- **Booleans.** A boolean or numeric column compared with a quoted value is bound by the
  profiled type in every clause and dialect; BigQuery rejected `flag = 'true'`. A true/false
  field the question names in words, negatives included, is applied or asked about, never
  dropped. A pinned boolean held by a binding or a `NOT col` / `col IS TRUE` condition is not
  asked back.
- **Drift blame.** A warehouse error on compiled SQL is blamed on drift only when a column or
  table the statement reads was dropped or retyped; otherwise it is dst's, and no drift ticket
  opens.
- **Values and bindings.** Stored values the question names resolve from the dictionary again,
  whatever the column is named. A binding matches the stored value whatever its case, at any
  dictionary size, whether or not its field was picked; an unknown value is asked about with the
  values the column holds. A caller's binding, a value the population filter pins, and a value
  the question settles are never asked back.
- **Words inside a metric's name.** A number, a grain word ("daily target"), a bracketed code
  ("errors (5xx)") or a "per X" inside the chosen metric's name or label belongs to the metric,
  never a filter, a series or a breakdown. A dimension said only inside the metric's name is not
  a breakdown. An exact label or alias outranks a voted metric whose name it only contains.
- **Measured words.** The "figure asked must be one the metric measures" check reads names as
  stemmed whole words, ignores undeclared short tokens, and asks only about words the lens
  declares nowhere. A qualifier that maps to nothing ("deleted members" with no deleted flag)
  asks instead of serving the unqualified figure; one only another metric carries asks which
  metric.
- **Shapes.** A listing leads with the entity's key and label, and reads the table and date the
  question names. A superlative no metric carries ranks over the noun it names, or asks what to
  rank. A ranking groups by the member the question names, asks instead of ranking by a field
  only another table holds, and asks for another figure instead of ranking a count of its own
  key. A share or rate asked of a non-ratio metric asks which ratio; "net new" of a one-sided
  metric asks which net metric. An acronym alone or before a period word must be measured by
  the chosen metric.
- **Periods.** "The end of Q3 2026", "the end of 2026" and other named ends read as the period's
  last day (a level: its last snapshot). "The first 2 quarters of 2026" and the first or last N
  months, weeks or days of a period read as that part; the number is never a limit. A day bound
  on a column declared `timestamp` closes at the next midnight on every door. A level whose
  snapshot field is declared `date` over stored timestamps reads the whole latest day. A ratio or
  derived metric whose operands are all levels reads one snapshot, never an average of every
  snapshot. A period binding on the time field or as `window` is the question's window.
- **Empty and impossible answers.** An empty result for a period after the newest row says where
  the data ends. A figure asked by month or as a trend on an entity with no time field is
  refused with "this lens cannot split <metric> by <unit>" instead of served as one total.
- **Request log.** A row is written whatever values the answer carried; a Decimal or date in the
  logged sample raised out of the logger, and NaN lost the row.
- **Spend.** Every token a provider billed reaches a row exactly once: routing decider spend on
  served answers and on questions no lens served; a batched decision call's whole usage even
  when the resolution ended early, with shares that add up; spend billed before a provider
  failed; the typed judge, readings, the certified-equivalence gate, the template bind gate, the
  rubric judge fallback and the adversary. A model id containing a colon is priced correctly.
- **Anthropic models** that reject `temperature` are not sent it; an unknown alias that refuses
  it is retried once without.
- **Comments and examples** use invented wording throughout.

## [0.13.0] — 2026-10-22

No schema change. New `lens.yaml` key `not_for`; new metric `agg: sum_distinct_on` with
`distinct_on`; new endpoints for the metric catalog, batch metrics and apply jobs; new
`compare` field on structured intents. Behaviour changes: `dst apply` runs as a server-side job
the CLI polls; the publish gate and `dst test` grade behavioural cases through the query door's
own serving path; an aggregate over an entity with `pinned_dimensions` that neither pins nor
groups by them now asks which kind instead of summing them; sqlglot, duckdb and fastapi are
bounded to tested ranges.

### Added

- **Metric catalog.** `GET /v1/lenses/{name}` lists `metrics` (label, aliases, unit,
  additivity, grains, callable dimensions, `depends_on`, linked certified answers) and
  `dimensions` (with complete value lists from the profile). `GET /v1/lenses/{name}/catalog`
  returns them with a `tree` view built from `depends_on`. New MCP tool `metric_catalog`.
- **Batch metrics.** `POST /v1/lenses/{name}/metrics/batch` runs up to 50 structured intents in
  one request at one `data_as_of`, on one shared snapshot on Postgres. Results are keyed by id,
  each with its own `request_id` and receipt; one failing intent does not fail the rest, and an
  intent whose answer errored reports `ok: false`. New MCP tool `query_metrics_batch`.
- **Period comparison.** `compare: {to: previous_period | previous_year | <range>}` on a
  structured intent adds `<metric>_compare`, `_delta` and `_delta_pct`; level metrics compare
  snapshot to snapshot and flows sum to sum. An intent without a bounded date window is refused.
  Runs on DuckDB, Postgres, Snowflake and BigQuery, including month, quarter and year grains on
  BigQuery TIMESTAMP columns.
- **Guide: Building data apps.** The pattern for apps on dst: a node is a typed intent, the tree
  lives in dst as derived metrics, certified answers for signed nodes by `cert_id`.
- **Asks a lens does not answer.** `not_for: [{ask, sibling}]` in `lens.yaml` refuses a question
  that names every word of the ask, on the direct and the routed door, before typing or
  generation, naming the lens that answers it. `Avoid for:` lines in a single entity's
  `use_cases` are read the same way.
- **One value per key.** `agg: sum_distinct_on` with `distinct_on: [...]` sums one value per
  distinct key, so a monthly target joined onto daily rows counts once per month.
- **`dst plan` and `dst apply --select`** take `lens/`, `entity/` and `definition/` selectors,
  globs, `+X`, `X+` and `@changed`; `--scope {all,semantic,certified,cases,config}` limits what
  is pushed, and `--scope cases` lands eval cases without a gate. `--lens` is an alias for
  `lens/NAME`.
- **Apply runs as a server-side job.** `dst apply` polls it; `--no-wait` and
  `dst apply status <id>` let you leave and come back; a concurrent apply's 409 names the job.
- **Request-ledger export:** `GET /mgmt/requests` and `dst observe export`. **`dst runs --all`**
  in one call. **`dst test --json`** gains `calibration_by_slot`, `case_id` and
  `certified_answer_id` on every row. **Every answer carries `source`** (certified, compiled or
  generated) and `certified_answer_id`.
- **`make ci-newest`** runs the suite against the newest sqlglot and duckdb the bounds allow.

### Changed

- **Routing separates sibling lenses deterministically.** A question equal to one lens's
  certified question routes to that lens; text two or more lenses carry word for word no
  longer counts toward routing; a declared table, entity or metric name breaks a two-way tie.
- **The publish gate and `dst test` grade what the door serves.** Behavioural cases run through
  the query door's own path: typed serving and its decider, the lens's `untyped_fallback`,
  certified matching, caps and answer mode. A case pinned to a clarification the door gives
  passes.
- **`dst plan` right after `dst apply` reports zero changes.** Server-stamped values no longer
  appear as plan changes, an eval case removed from `evals/cases.yaml` is retired, and an
  unchanged probe artifact is not re-landed, so an apply after a clean plan runs no gate.
- **Test and benchmark traffic** (agent `dst-test` or `benchmark`) is left out of observe KPIs,
  the basis line and the audit statement by default; `--include-test` shows it. `dst query`
  and `dst sql` send `X-Dst-Agent: dst-cli`.
- **Decider calls count in AI tokens and cost**, per request and in `dst observe`.
- **The apply-time connection probe reads each declared table once** instead of scanning every
  dataset; step timeouts name the connection's table and dataset counts.
- **`dst probe --tables`** accepts a table as the entity names it (`dataset.table` on BigQuery)
  and warns about a name that matches nothing.
- **A SQL error dst compiled is reported as dst's.** When the warehouse rejects compiled SQL and
  the schema has not changed since apply, no data-team ticket is opened.

### Fixed

- **A value the question names is never overridden by a metric's own filter.** A metric whose
  filter pins a column to another value is not chosen when the question names a stored value
  of that column; the value binds as a filter, or the question is clarified.
- **Days are read as days.** "On 28 August 2026", "between 5 and 12 October", "5–12 Oct", "the
  week of 5 October", "the first 7 days of October", "the last 3 days of August" and "the start
  or end of August" are those days, not the whole month; "early/mid/late August" and any other
  day qualifier the parser cannot place ask for the days. `window_applied` fails an answer whose
  window is wider than the days asked.
- **The population filter applies on every compiled query**, including a level metric's
  latest-snapshot pin.
- **Pinned dimensions are enforced when a query is compiled**; an aggregate that neither pins
  nor groups by one asks which kind, naming the stored values.
- **Window bounds carry the column's type.** Date bounds are cast to the profiled physical type
  on every door, including the metrics and batch doors; a day bound on a timestamp column
  closes at the next midnight; on BigQuery a `timestamp` declaration over a `DATETIME` column
  no longer fails.
- **Clarifications ask the right thing.** Key columns and unasked timestamps are never offered
  as candidates; a question declared in `common_questions` or `use_cases` clarifies instead of
  declining; sibling metrics that differ only by a parameter (a window size, a unit) ask which; "this month" resolves
  on the lens clock; a level metric over a window asks no restricting field; a value no column can
  take asks instead of vanishing; a measure the entity declares no metric for asks
  instead of answering in another unit; a bare row count answers only questions counting the
  entity's own rows; a question naming a measure gets a clarification, not a raw listing.
- **Breakdowns answer.** A text column inside a composite primary key can be a dimension, and
  an entity with no declared `dimensions:` groups "by X" by its label fields; a breakdown
  clarification always names the fields.
- **A certified answer is not served exact for a breakdown its SQL does not group by.**
- **Eval and test surface:** gold attributed from certified SQL is the answer's projected
  metric; rows on a declared grain match by bucket in both lanes; the slot lane proves a scalar
  latest-date subquery equivalent under both supported sqlglot versions; `eval_result.actual_value`
  is written on every row; `sql_data_equal` is only claimed beside the right metric.
- **Quarters and halves are read as their months.** "The third quarter of 2026", "the 3rd
  quarter", "the second half of 2026", "the last quarter of 2026" and H1/H2 with a year resolve
  to their months instead of the whole year; without a year, the most recent one on or before
  the lens clock. `window_applied` fails an answer covering a whole year when the question asked
  for a quarter or a half.
- **A number inside a window phrase is not a limit.** "The first 2 days of October" is a window,
  never a top-2; a breakdown over a window of days sums the days per group instead of adding an
  unasked grouping by day.
- **"The end of the month"** (also "month end", "end of month", "quarter-end", "the end of the
  quarter/year") with no period named means the last completed period's last day.
- **The figure asked for must be one the chosen metric measures.** When the question asks for a
  measure (an acronym, a measure column's word, a measure definition) the chosen metric does not
  measure, or a count the chosen count metric does not count, typed serving asks instead of
  answering; an acronym spelled out in a metric's description, its columns or a definition maps
  to that metric.
- **A rejected lens says why in the default output.** `dst apply` lists every rejected lens with
  its reasons above the counts line, `rejected-recompile` rows included; `dst plan` prints the
  compile error of an invalid lens even when it is also stale, and counts any lens row carrying
  errors as invalid in the summary and the exit code.
- **Examples, fixtures and comments use invented data throughout.** Repro values that had been
  copied into tests, docs, code comments and the changelog are replaced with invented examples.
- **Package installs match CI.** sqlglot (`>=30.8,<30.25`), duckdb (`>=1.5.3,<2.1`) and fastapi
  (`>=0.115,<0.159`) are bounded to the versions the suite was run on.

## [0.12.0] — 2026-10-18

No schema change. New fields: definition `period:`, metric `period_weight:`; new checks
`definition_period`, `grain_applied`, `row_conservation`; `dst init` compose project names are
now unique per directory.

### Added

- **Definitions ride the certified rail.** A definition with `sql:` and `period:` about an
  entity fails `definition_period` on certified SQL that reads past it: the grade is at most
  partial, the answer carries `DEFINITION NOT APPLIED: <term>` on every surface, and
  `dst plan` and `dst apply` warn `certified_ignores_definition` naming both.
- **A question that says monthly, weekly or by month pins that grain** on DATE columns too,
  and `grain_applied` fails a result not bucketed at the asked unit.
- **`row_conservation`**: a UNION ALL over one keyed table that yields more rows than
  distinct keys grades partial naming both counts.
- **A ratio across additivities** (a level over a flow) compiles when the metric declares
  `period_weight: weeks|days`.
- **The dbt import warns `join_cardinality`** when the probe shows a 2:1 key mapping on a
  relationship, and writes `schema.alias` on every path.

### Changed

- **Certify-time prose is composed over the whole result** and stored with its row count;
  prose composed over a sample never serves verbatim (`SAMPLED`), and a changed row count
  counts as restated.
- **A certified basis names only the definitions whose member the SQL reads** or whose
  metric it computes.
- **`dst init` derives a unique compose project name**; `dst dev` refuses a database whose
  encryption key does not match, naming the project; `--daemon` exits non-zero with the
  log tail when the child dies.

### Fixed

- **A `NO_ANSWER` anywhere in the composer's prose is a decline**, never a verified answer.
- **A generated literal on a column a selected metric's own filter governs is dropped** and
  recorded on the ledger as `literal_dropped`.
- `sample_queries` shape and template slot casts are documented.

## [0.11.2] — 2026-10-17

No schema change.

### Fixed

- **A certified serve no longer reports a timestamp cell as restated on its text form.** The
  serve-time check reads values through the same canonical normalisation as the eval gate.
- **"At the end of each month", "per month-end" and "MRR monthly for 2026" are read as a
  month bucket**; a level metric asked that way returns one row per month even when
  generation grouped by the entity's day column.
- **`dst apply --refresh-samples` prints an old → new line only for templates whose value
  moved**, and says how many moved.
- **The apply-time router lint names the anchors two lenses carry verbatim** instead of
  reporting a 1.000 cosine as an embedding failure, and skips with a reason when no
  embedding provider resolves.
- **A certified template frame or rows-only serve names every governing definition in the
  answer text**, not only the table it dates from.

## [0.11.1] — 2026-10-16

No schema change. Upgrade fix: 0.11.0's restated check compared values an earlier release
stored as text against live numbers and blocked the first apply; upgrade straight to this
release.

### Fixed

- **The eval gate compares certified values by value, not by type or text form.** Values an
  earlier release stored as text are re-recorded on first comparison, so the first apply
  after upgrading never blocks on them. `dst apply --refresh-samples` re-records every
  template and prints old → new.
- **A certified answer discloses definitions written about a metric or dimension**, not only
  about a column.
- **A level metric asked per month, week, quarter or year is one snapshot per bucket on
  every rail**; `level_grain` fails a series collapsed to one global latest day.
- **`aggregation_scope` no longer fires on counts built over dates, timestamps or flags.**
- **`dst apply` prints the breaking drift findings it left in the baseline** at the end of its
  report, with the `--accept-drift` hint.
- **`dst import dbt` imports a measure and metric twin once** and writes no empty
  `primary_key`.
- **The gate's block message names a status apply accepts.**
- **`run_certified` resolves the stored question asked verbatim** whatever its similarity
  score.

## [0.11.0] — 2026-10-16

No schema change. Behaviour changes: the publish gate grades certified answers by their
stored value, not by whether generation reproduces them (`eval_gate: block_generation`
restores the old blocking); `dst apply` no longer advances the drift baseline over a
breaking finding (`--accept-drift`); a definition the question never named is not applied.
New files and keys: `evals/known_divergences.yaml`, entity `hide:`, `not_computable`
`row_level`/`keys`, metric `label`/`aliases` in the veto.

### Added

- **A certified answer that moved says so.** Every certified serve compares the live result
  with the value stored at certify time (`restated` check); a move caps the grade at
  partial, adds a `RESTATED: … moved from X to Y since it was verified on <date>` line, and
  composes over today's rows instead of serving the certify-time sentence.
- **`hide:` on an entity** withholds columns from every door, generated SQL included; plan
  fails on a hide that matches nothing or that an expression still reads. A plan warning
  `sensitive_columns` names in-scope columns that look like personal data with no hide and
  no definition about them.
- **Declared refusals hold on every door.** A lens's `not_computable` applies to
  caller-written SQL with the same wording; `row_level: true` and `keys:` refuse listings
  per entity key. `route_query` returns a lens's declared refusal instead of "not covered"
  when a lens the caller may use declares the named term not computable.
- **`evals/known_divergences.yaml`** (`id:` or `question:` plus `reason:`) covers judged
  generation divergences file-side; the apply report lists each one it covered.
- **A unit veto on certified matches.** A question that names a currency, a duration unit or
  a percent against a hit whose question, SQL or declared `currency`/`format` reports a
  different unit drops to assisted. Labels and aliases are governed names in the veto.
- **A level metric by month is read, not summed, on the generated rail too**: the prompt
  states the per-bucket rule and a new `level_grain` check fails a SUM over a level metric
  bucketed wider than its snapshot grain.
- **Shares, deltas and ratios the question asked for are graded as derived**, one division
  or subtraction of result values, named in the grounding reason.
- **`dst apply --accept-drift`**; `dst drift --accept` and `--refresh-samples` print old → new.

### Changed

- **The publish gate grades certified answers by their stored value.** A restated value
  blocks under `block`, naming both figures; generation divergence is reported, never
  blocking unless `eval_gate: block_generation`. The previous score is the bar only when a
  previous run scored the same case set; the denominator is the active corpus.
- **`dst apply` advances the drift baseline only over a clean artifact.** With a breaking
  finding it lands the layer, leaves the baseline alone and names the findings.
- **Certified answers carry their definitions** on the basis line, `definition_used` and
  the ledger, the same as generated answers.
- **"Data as of" never lies ahead of the clock**: a future newest time value dates the
  answer to today and says how far the rows run.

### Fixed

- **A definition the question never named is no longer applied**; an unnamed model pick is
  reported as `definition_suggested` and not compiled.
- **Metrics on different time fields each take the window on their own field**; the
  compiler feeds its refusal back for one repair and clarifies when the readings cannot
  share a query.
- **Honest partial reasons on approved and counted SQL**: `window_applied`,
  `aggregation_scope` and `metric_filter_waived` no longer fire on certified snapshot SQL,
  on COUNTs, or on columns whose metric declares its currency.
- **A `schema.table` literal no lens holds clarifies** instead of being answered from
  another table under that name.
- **A definition `sql:` the parser cannot read is an error**; `FILTER (WHERE …)` and window
  functions are aggregates, not predicates.
- **`describe_lens.certified_count` is counted in SQL** by the library's own predicate and
  can no longer read 0 while the library lists answers.
- **`dst import dbt` MetricFlow output applies**: qualified expressions, no expression
  primary keys, time dimensions resolved to columns, definitions bound by `about` without
  aggregate `sql:`, duplicate measures deduped with a note, `non_additive_dimension` →
  `additivity: level`.
- **`dst test` passes a certified result of the same width as the generated one** when the
  measures pair by value, and **a serve compiled from a governed metric grades on that
  metric** instead of `partial` for "no semantic check confirmed".

## [0.10.3] — 2026-10-16

No schema change.

### Fixed

- **A question naming one governed metric and a period compiles on every door.** "On-time
  delivery rate in September" or "revenue for Q3 2026" is served by the compiler with the
  window; a bare month is the most recent one on or before the lens clock.
- **Certified lookups read a hit's stored terms and resolution ledger.** They were not loaded
  at serve, so the lexical veto had nothing to compare. Attribution now names the metric a raw
  aggregate is, including filtered siblings over one column, and a measure veto keeps an asked
  metric off a template attributed to another one.
- **`dst test` passes a certified result that carries helper columns** beside the value
  generation returned, projecting onto the generated columns and reporting the dropped
  columns and the value equivalence in prose and `--json`.
- **`dst drift --accept` re-records the baseline on a quiet warehouse** whenever the recorded
  facts differ; `dst apply`'s advance uses the same merge, and a catalog-only artifact never
  nulls a measured fact.
- **`dst apply --probe-certified` prints `probed N of N`** on the lens row.

### Added

- **`dst apply --refresh-samples`** re-records the sample results of templates whose explicit
  ranges read inclusive since 0.10.0; the certify self-test names that cause when a sample
  changed.

## [0.10.2] — 2026-10-16

Schema change: migration 0069 adds `certified_answer.terms`, backfilled on the next publish.
Run `dst migrate`.

### Added

- **`label` and `aliases` on a metric.** A question names a metric word for word by its
  identifier, its label or an alias ("on-time delivery rate" for `otd_rate`); the sole-metric
  door and the typed metric rail read them, so the bare question compiles on every door.
  `dst plan` hints at multi-word metrics with neither.
- **Certified answers store what their SQL reads.** Apply attributes every answer's SQL and
  stores the metric and definition names and aggregated columns; the serve-time veto reads a
  candidate that carries no governed name word by word, so a raw collections template never
  serves an invoicing question.
- **Plan warns on explicit ranges that read exclusive.** A `date_range` sample binding ending
  on the first of a month, or spanning a month or year plus one day, gets "explicit ranges
  are inclusive; did you mean 2026-09-30?".

### Fixed

- **Cross-match errors only on indistinguishable pairs.** Two certified answers sharing a
  metric name are rejected only when, with stopwords and the shared governed names removed,
  neither question says a word the other does not; "MRR level" vs "MRR growth" and "OTD by
  month" vs "OTD by service level" are warnings that name the differing words.
- **`dst drift --accept` keeps the measured facts.** The baseline carries `mean` and
  `time_offset_seconds` and the dictionaries from the probe artifact, and keeps the previous
  baseline's facts for columns the artifact did not measure, so `column_shifted` can fire
  after a re-record.
- **`dst test` names a value-equivalent pass in the row-set path.** The PASS line and the
  `--json` row carry the `value-equivalent:` reason; a `Decimal` certified result beside an
  extra generated column is projected and compared as numbers.
- **`--probe-certified` reports what it probed.** The lens row says
  `probed N of N new certified answer(s)`.

## [0.10.1] — 2026-10-16

No schema change. Upgrade fix: 0.10.0's certified plan check rejected valid corpora;
upgrade to this release before applying a 0.9.x project.

### Fixed

- **Upgrade fix: the certified plan check no longer rejects legitimate near-paraphrases.** Two
  certified answers above the paraphrase band with different SQL fail plan and apply only
  when both name the same governed names including a metric or definition, or share no
  distinguishing name inside the exact band; every other pair is a warning. A stored row the
  file no longer carries is not compared, so a rename lands in one apply. One-line fix for a
  corpus that still fails: reword one question so it names its metric.
- **Template hits respect the lexical veto.** A certified template never binds for a question
  naming a different governed term; the veto also reads each answer's own resolution ledger,
  folds regular English inflections (invoice, invoiced, invoicing), and lets a distinctive
  word of a multi-word name stand for it. Entity names join the governed set.
- **`status_done_ambiguous` and `definition_double_truth` can no longer fire for the same
  column**: a definition about a status column whose `sql:` reads it is exempt from double
  truth.
- **A `hide:` pattern that matches no column of the exposed tables fails plan and apply**,
  naming the pattern.
- **Correct counts are never withheld.** A count claim grounds on the noun's column first, then
  the row count, the distinct values of any column, the sum of any integer column, or any
  cell; `numeric_grounding` names which one matched on a pass.
- **dbt manifest-only import writes `table: schema.alias`** and keeps every catalog column even
  when `schema.yml` documents fewer.
- **`dst test` prints the lenient reason on the PASS line**, as `--json` already did.
- **A certified serve demotes to `partial` on any failed check** except the two the certified
  contract waives (`definition_applied`, which abstains on approved SQL, and the advisory
  `judge`).
- **Semantic drift fires on Postgres.** Numeric and time columns the catalog already measured
  are still sampled for their mean and median clock offset, without overwriting catalog
  facts; a column comment that appears or disappears is `column_recommented`.
- **A dimension table (no `default_time_field`) never fails `date_coverage`**; it skips with
  the reason.
- **Month spans bind the last month whole under every phrasing**: "to", "until", "up to",
  "between … and", "inclusive", "from … through".
- **A single-row ratio of exactly 1 whose numerator and denominator share a filter predicate
  fails `all_ones_ratio`** naming the predicate; other single rows stay a skip.
- **`dst test` treats `Decimal` cells as numbers**, so a percentage beside a label column
  passes value-equivalent against its fraction.
- **Regular-expression matches are predicates** in the definition filter check (`~`, `~*`,
  `REGEXP`, `RLIKE`, `SIMILAR TO`, `REGEXP_LIKE`, `REGEXP_CONTAINS`, `starts_with`).

## [0.10.0] — 2026-10-16

No schema change. Behaviour changes: explicit date ranges are inclusive; an exposed read
grades at most partial; the inline judge is advisory; `dst probe` no longer overwrites the drift
baseline; `dst test` exits non-zero on any failing case. New `dst.yaml` key: `probe.drift_tolerance`.

### Added

- **A ratio pinned to 1 on every row fails verification.** The `all_ones_ratio` check fails a
  rate or ratio result that is exactly 1 (or 100) across more than one row; a single-row
  result skips and says so.
- **`partial` always names its cause.** `verification.partial_reason` on every response and
  trace carries the check that capped the grade, or the abstention that kept it short of
  verified.
- **Plan rejects a definition whose `sql:` cannot filter.** A `sql:` that is not a boolean
  predicate (`definition_not_predicate`) or that sits on a ratio metric
  (`definition_filters_ratio`) fails plan and apply with the fix named.
- **Plan warns on a status column with several done states.** When a status-like column's
  complete value dictionary holds more than one done-family value and no definition is about
  the column, plan and apply emit `status_done_ambiguous` naming the values.
- **Plan fails when two certified answers would answer each other's question.** Every
  certified question is embedded and compared with the corpus; a pair inside the paraphrase
  band with different SQL is a plan/apply error naming both.
- **A lexical veto on certified matches.** An exact or equivalent hit whose approved question
  names a different governed metric or definition than the asked one drops to assisted.
- **`dst doctor` fails when a certified corpus exists without an embedder**, and `dst init`
  says whether certified matching is on.
- **`probe.drift_tolerance`** in `dst.yaml` (`time_shift`, default `1h`; `numeric_shift`,
  default `0.2`).
- **Warehouse bytes in `dst observe`**: the KPI block carries `wh_bytes` in total and per
  lens, and the headline shows bytes scanned.
- **`dst serve --daemon` and `dst serve --stop`.** The server can run detached, with its pid
  in `.dst/serve.pid` and its output in `.dst/serve.log`; `--stop` ends it. `dst dev --daemon`
  does the same. On any signal the server logs which one arrived and that it is shutting
  down.
- **A decline from `route_query` carries a `request_id`.** The recorded routing decision's
  id, so a caller can quote a decline the way it quotes an answer.
- **A missing column no longer refuses the whole question.** When generated SQL is rejected
  for one column outside the lens and the question asked for other things too, one repair
  leaves that column out and the rest is served. The response carries a `declined` list
  (`"<column>: not in this lens"`), a `degraded` line reading
  `answered the rest: <column> is not in this lens`, the same note in the answer prose, and
  the trace records it. A repair that still fails refuses as before, with no further
  generation. Typed listings do the same for a further field the resolver cannot place.

### Changed

- **An exposed read grades at most partial.** Any answer whose SQL reads an exposed table
  carries the `governed_read` check ("ungoverned read") and never grades verified, on
  certified serves too.
- **The inline judge is advisory.** It reads the governed context the composer had
  (definitions, data notes, population scope), runs at temperature 0, and its verdict lands as
  the `judge` check without changing the grade.
- **A count in prose is graded against the column it counts.** "7 contracts" is checked
  against the `contracts` column's cells and sum (or its distinct values and the row count),
  not against any cell holding a 7; distinct counts per column are now groundable.
- **Explicit date ranges are inclusive.** A behaviour change: a template slot bound as
  `YYYY-MM-DD/YYYY-MM-DD` now covers both dates; the second date was previously an exclusive
  end, so `2026-01-01/2026-09-30` dropped 30 September. Rendered `{slot.end}` is the day
  after the last day, as for months and quarters. Stored bindings written under the old
  reading cannot be told apart and now cover one more day; re-check any explicit-range sample
  binding.
- **The certified suite compares on the certified projection.** Columns match by name and
  extra generated columns are ignored; numbers compare as values (a percentage against its
  fraction, rounding at the certified value's printed precision), each lenient pass saying
  why. `dst test` exits non-zero on any failing case, `--json` included.
- **`--allow-failing-cases` takes case ids** and covers exactly what it names, a certified
  divergence (`certified:<answer id>`) included; bare it stays blanket. A covered divergence
  is recorded on the apply row and in the version summary.
- **Drift sees meaning, not only shape.** The baseline records column comments, each numeric
  column's mean, each time column's median offset from the entity's `default_time_field`,
  and complete enum dictionaries; `dst drift` compares it against the live catalog and the
  newest probe artifact, and a changed comment, a shift past `probe.drift_tolerance`, or a
  dictionary member gone is a breaking finding that names the entities, definitions and
  certified answers reading the column.
- **A probe never erases the drift baseline.** The first `dst probe` arms drift; later probes
  write beside it. The baseline advances by `dst drift --accept` or by `dst apply`, and only
  when the recorded facts differ.
- **Answers are dated from the tables they read.** `data_as_of` is the oldest of the tables
  the served SQL resolves to, not the slowest table in the lens; the stamp names the table
  (`data as of 2026-10-14, orders`); a dimension without a `default_time_field` dates an
  answer only when it is all that was read. The freshness check uses the same date.
- **Probe cost.** The sampling pass issues at most three statements per table, and
  `dst probe` ends with statements, bytes billed and the estimated cost per connection.
- **`dst import dbt` no longer needs MetricFlow.** A project with no semantic manifest imports
  its models as entities: table, columns with descriptions, types from `catalog.json`, and a
  `unique` + `not_null` tested column as the key. The summary says metrics and dimensions
  still need authoring.
- **A question naming one governed metric and nothing else is served by the compiler.** On
  the direct and routed query doors the compiled intent serves before any generated SQL, the
  way the structured door does; typed serving is unchanged.

### Fixed

- **A certified answer whose stored prose is a decline now refuses** instead of grading
  verified.
- **An adversary reply that cannot be parsed folds as a skip**, no longer as a note on a
  passing check.
- **A prose-only correction never auto-approves.** A review carrying correction text lands
  `needs_human` unless the correction includes SQL the judge verified.
- **Apply no longer 500s on a bare YAML date in `sample_bindings`.** Dates load as ISO text;
  a list or mapping value is a plan error.
- **A filter value was refused against another table's dictionary.** Value dictionaries are
  now kept per table and a literal is checked against the table the SQL reads; a column name
  shared by two entities no longer borrows one table's values for the other.
- **Filter literals are checked against the dictionary of the table the SQL reads.** A column
  name shared by two entities with different value dictionaries was skipped by the pre-query
  check; a value valid on the table the query reads serves, and one absent from it clarifies
  with that table's values.
- **The freshness check names the table it measured.** The fail reason reads
  `data in <table> is as of …`, the same table the answer's as-of stamp names.

## [0.9.0] — 2026-10-15

No schema change. New `dst.yaml` keys: `probe.max_age`, `probe.recent_partitions`.

### Added

- **Profile age on every rail.** `dst.yaml` gains `probe.max_age` (default `7d`, `0` disables).
  Past it an answer carries `PROFILE: last probed N days ago (max M) — run dst probe` in
  `degraded`, the freshness and date-coverage checks skip instead of failing on a stamp that
  only dates the last probe, the value guard consults no dictionary, `dst observe` and
  `/ready` report the oldest profile age and the stale connections, `dst plan` and
  `dst apply` warn when the pushed artifact is older than the limit, and `dst test` skips
  the lens and exits 4. The section is stored per org on apply.
- **Recent-window sampling.** `probe.recent_partitions` (default 7, `0` reads whole tables)
  narrows the sampling pass to the latest N partitions of a time-partitioned table or the
  last N days of the entity's `default_time_field`, anchored on the data's newest value; the
  catalog pass and the whole-table freshness and date-span probes are unchanged. The
  artifact records the slice per table (`sample_window`), windowed facts are labelled
  partial, the first probe of a connection reads whole tables, and a dictionary an earlier
  probe proved complete is kept through a windowed refresh as the dated union
  (`values_complete_at`).
- **A question that names a table the answer does not read now clarifies.** On the
  generation path, a named entity, source table or exposed table, in the lens or in another
  the caller may use, that the served SQL does not read returns a `clarification` of kind
  `named_table` naming both tables, and the lens that holds the named one, instead of a
  figure read from a different table.

### Changed

- **Warehouse error suggestions name only tables and columns in the lens's scope.** On
  `query` and the `sql` door's lens scope, an engine's "Did you mean …" is rewritten to the
  closest in-scope name or removed; the rest of the error and the admin connection scope are
  unchanged.

## [0.8.1] — 2026-10-15

No schema change. A security release: upgrade if any lens exposes tables or serves
the caller `sql` door.

### Security

- **Unqualified columns are scoped.** A hidden or unmodeled column named without a table
  prefix (`SELECT ssn FROM customers`) is refused as out of lens scope, the same as the
  qualified form; a column from a CTE, a projection alias, or a row generator still passes.
- **A table reference may omit qualifiers, never add them.** `staging.orders` no longer
  passes a lens whose source is the unqualified `orders`; omitting a schema or catalog the
  source has still names the table.
- **`SELECT *` over exposed tables is rewritten, not trusted.** When every table in a
  statement is an exposed table, a bare or qualified star in any scope is expanded into the
  table's allowed columns, so the returned SQL and the rows never carry a hidden column; a
  star the guard cannot make explicit, or a statement that also reads a declared entity, is
  still refused.

### Fixed

- **`SELECT *` on a whole exposed table was refused** whenever the lens hid a column on some
  other table, or a declared entity shared its bare table name; it now passes and lists the
  columns.
- **A dropped table no longer keeps its grant alive.** A stored profile the push's probe
  artifact no longer lists counts as gone at plan and apply: the pattern that matched only it
  fails, naming the table as dropped upstream. The store is not pruned by a push, and an
  artifact older than the stored profile cannot declare a table gone.
- **`dst plan` counts hidden columns:** `exposes N tables (M columns, H hidden)` when `hide`
  withheld any.
- **An apply row says `unchanged`** for a lens whose managed files match the server when a
  touched input republishes it, as the plan said; a lens the same push made stale still
  reads `updated`.
- **The `untyped_fallback` warning** on an `expose:` grant says what an answer then carries:
  an EXPOSED line, and an UNTYPED line only when a typed reading was bypassed.
- **`dst observe show` prints the purpose and a probe's SQL once.** The KPI basis block gains
  `raw_refused`, the refused-probe count the governed outcomes never held.
- **A lens without `queries.yaml` no longer plans as `update` forever.** The plan compares a
  missing file as the empty one apply reads it as.

### Added

- **A miss names the lens that exposes the table.** When a question names an exposed table
  word for word, the routed decline carries `exposed_in` and both the uncovered reason and a
  lens refusal end with "lens '<name>' exposes <table> (no governed definitions): ask it by
  name".
- **`dst mcp`.** Runs the stdio MCP server from a wheel install; `DST_API_KEY` and `DST_URL`
  as before.

### Changed

- **MCP failure codes name the owner.** A data-plane 400 returns `code: "refused"` and a 422
  `code: "invalid"`; `upstream` is reserved for server faults.
- **`dst drift` runs every declared connection.** `--connection` is optional; without it each
  warehouse connection runs in turn, findings under a per-connection heading, exit code the
  worst, `--json` one list.
- **`describe_lens` note on exposed tables** says answers carry an EXPOSED line and never a
  declared or certified tag.

## [0.8.0] — 2026-10-15

No schema change.

### Added

- **Expose tables that have no definitions.** A lens may declare `expose:` grants in
  `lens.yaml` (connection, table patterns, `hide` column patterns); apply resolves them
  against the connection's profiled tables, and each match becomes an entity with the
  profile's columns and nothing else. A pattern that matches nothing, or a bare name that
  collides with a selected entity, fails apply by name. `dst plan` prints
  `exposes N tables (M columns)` per lens and warns when `untyped_fallback` is off.
- **EXPOSED disclosure.** An answer that reads an exposed table carries
  `EXPOSED: reads <table>, a table with no governed definitions` in `degraded`, the way an
  untyped answer carries `UNTYPED`; such columns never count as declared in the resolution
  ledger.
- **`describe_lens` lists exposed tables apart** from entities, under `exposed_tables`,
  with a note that answers from them are ungoverned.
- **Basis over every read.** The observe KPIs and the windowed audit statement carry a
  `basis` block counting every served caller read by what its figure rested on: `certified`,
  `declared`, `mixed`, `inferred`, `raw` for the caller's own SQL, `unknown` for rows written
  before the ledger, with `n` and a `governed_share` that is null when nothing was read. The
  raw probe sits inside this denominator, where it was always excluded from the governed
  counters, so the share no longer flatters itself; the admin passthrough stays out.
  `dst observe` prints the split as one `basis:` line with the governed share.
- **`purpose` on the SQL probe.** `POST /v1/sql` and the `sql` MCP tool take an optional
  `purpose` sentence like the other asking doors, stored on the lens-scoped log row only
  when the lens sets `logging.log_purpose`; the connection scope never stores it.

### Changed

- **SQL guard star rule.** `SELECT *` is admitted when every table in the statement is an
  exposed table with no hidden column; everything else is refused as before.
- **Typed serving and routing ignore exposed tables.** The typed resolver never offers them
  (a question about one does not type and follows the lens's untyped rule), and the router
  takes no anchors from them.
- **Drift audit names lenses.** `dst drift` cross-references `expose:` grants, so a dropped
  exposed table is reported as a breaking reference to the lens that exposes it.

## [0.7.0] — 2026-10-15

Schema change: migration 0068 adds `request_log.purpose`. Run `dst migrate`.

### Added

- **A question can say why.** `query`, `route_query` and `run_certified` take an optional
  `purpose`: one sentence on what the asker is trying to accomplish ("building a
  forecasting app"), capped at 300 characters, sent only when the agent has one. It is
  stored on the request's log row only when the lens sets `logging.log_purpose: true`
  (default off: it is a fragment of someone's conversation with their AI), and it changes
  nothing about how the question is answered yet. The column exists so the next step can
  be measured rather than assumed: how often agents send one, and whether it should pick
  the grain and window of an answer.
- **A metric can be a level, not a flow.** `additivity: level` on a metric declares its rows as
  snapshots (ARR, headcount, a balance): a period's figure is the last snapshot inside the
  window, and a monthly trend is each month's last snapshot, instead of a sum over every
  snapshot row. The answer discloses the reading and the ledger carries it as a declared
  window slot; a level metric without a time field is rejected at plan. The window parser now
  reads a full ISO date ("as of 2026-08-31") as that day rather than its year.
- **`dst apply --lens <name>` publishes a selection of the push.** Repeatable; the other lenses
  in the tree are reported `unchanged` and left alone, so a sibling's red gate no longer holds
  yours back. A push that changes shared inputs a non-selected published lens is compiled
  against (a semantic asset it reads, a connection it declares) is refused before anything
  lands, naming the lens and the two ways out: add it to `--lens`, or drop `--lens`.

### Changed

- **`dst test` proves certified SQL equivalent, not only identical.** The slot lane compares the
  typed SQL to the certified text through sqlglot's optimizer, so a hand-written query with
  its own aliases, another predicate order or a wrapping subquery is proven without the
  warehouse and skips the rows lane; a different literal still differs. `sql_match` in the
  JSON output is now `"identical" | "equivalent" | "differs" | null` in place of the boolean,
  and the summary counts `sql_equivalent`.
- **`dst test --compare-last` reports verdict flips.** Slot-lane cases are recorded on the run
  as `slots:<answer id>`, every run carries the published lens's fingerprint, and when two
  consecutive runs share it the compare lists each case that flipped (`compare_last[].flips`);
  when the lens changed, it says so instead.

### Fixed

- **`2026-08` and `2026-Q2` in a question are the month and the quarter.** A period written
  the way a template binding renders it parsed as the bare year, so a monthly figure asked as
  "in 2026-08" was served over all of 2026. The ISO month and quarter forms now read like
  "August 2026" and "Q2 2026".
- **The probe sampling budget is a connection setting.**
  `connections.<name>.config.sample_bytes_budget` sets the BigQuery sampling budget per
  connection, beside `max_bytes_billed`; it was a fixed 2 GB constant no configuration could
  reach, and the refusal that left a wide table unsampled named no way to raise it. The
  refusal now names the key and the `--tables` alternative.
- **`dst bootstrap` reruns keep the admin token and existing users.** A rerun reuses the
  `DST_ADMIN_TOKEN` already in `.env` when it is live for that org, and refuses when it belongs
  to a different org than `--org`, naming both; `--rotate-admin-token` mints and replaces. An
  existing dashboard user's password is only reset with `--reset-password`. Previously every
  run minted a new token, overwrote the `.env` line without revoking the old one, and `--email`
  silently reset an existing user's password.
- **Two metrics named in the question are the answer's metrics, not a clarification.** "The
  split between member revenue and guest revenue" used to divide the metric vote and fall to
  untyped serving; governed names said word for word are now read off the text when the first
  pick cannot settle between them. Names on different entities still clarify, and the
  clarification says why.
- **"by <dimension>" is read, not decided, and a further metric must be named.** "Revenue by
  member plan" typed as three metrics and no dimension: the word "member" pulled the
  `members` metric through the "another metric?" round. The breakdown phrase now sets the
  first dimension deterministically, and further metrics are offered only when the question
  names them outside that phrase.
- **A routed refusal keeps its reason.** Pinned by test: on `/v1/query` a lens's refusal
  arrives as `answer.status: refused` with the reason in `answer.answer`, for both audiences.
- **Slot-lane gold no longer lists a metric twice.** A definition that shares its name with a
  metric read as a second governed slot ("expected revenue, revenue"), failing a typed answer
  that resolved to the metric once. It is now one slot; a genuine duplicate prints with its kind.
- **Templated and no-gold certified answers are no longer skipped silently by the slot lane.**
  Templates are graded on their first sample binding; every answer not graded is listed with
  its reason in the output and in the JSON `slot_lane.skipped`, so graded plus skipped is
  always the corpus.
- **A unanimous vote no longer reads as certainty.** Decisions carry `votes`, `k` and
  `p_lower` ((votes+1)/(k+2), 0.857 for 5 of 5); p stays the vote share and the verdict is
  unchanged. The calibration report counts n as distinct questions, drops `--repeat` re-runs,
  and shows the unanimous bin's accuracy.

## [0.6.2] — 2026-09-28

No schema change.

### Fixed

- **The demo page leads with the steps, not the one line.** The page opened with "Connect
  me to <name>: …/SKILL.md" for every visitor, but a chat app such as Claude or ChatGPT
  cannot add a connector from a pasted line: a person adds it in the app's settings, and
  Claude rightly treats an unknown file that asks to be followed as untrusted. The first
  thing most visitors tried was refused. "Connect your AI" now opens on each client's
  steps, before any sign-in, Claude and ChatGPT first since they sign in when connecting
  and need no key; the key fills in after sign-in. The one line folds under them for
  Claude Code, Codex and Cursor, which can run the setup themselves. `/SKILL.md` now tells
  a chat assistant that cannot add a connector to show the person the steps instead.

## [0.6.1] — 2026-09-27

No schema change.

### Fixed

- **A value the metric is named for is no longer read as a missing filter.** Since 0.6.0
  a typed question naming a stored value that the chosen entity holds no column for moved
  the answer to an entity that does. That also fired when the value was the metric's own
  subject. "How often does the Radiant side win" left `radiant_win_rate` for a per-side
  table and then asked which field 'radiant' should restrict. "How many times was Shadow
  Fiend the first pick" left the hero's first-pick count for a per-match count, because
  'pick' is a stored draft action. "What share of lanes are drawn" moved to a per-laner
  draw rate and filtered it to `lane_result = 'drawn'`, which served 1.0 instead of the
  share of all lanes. A stored value that the chosen metric's name or the columns and
  literals of its SQL already carry is now part of the metric, not a restriction: the
  answer stays on the metric the question named and no filter is asked for. The same
  check closes an older hole: an equality filter to a value the metric's own SQL selects
  its rows by (a draw rate counting `lane_result = 'drawn'`) is no longer applied, so the
  rate can't collapse to 1.0.
- **The demo page says who needs a key.** The note under the connect line read as if
  signing in came first. It now says Claude and ChatGPT sign in on their own and need no
  key, and that Claude Code, Codex and Cursor get the key added to the line after signing
  in.

## [0.6.0] — 2026-09-27

No schema change.

### Added

- **One line connects an AI.** Every deployment serves `GET /SKILL.md`, a public markdown
  file generated from the instance: its name, its MCP URL, the setup for Claude, Claude
  Code, Codex, ChatGPT and Cursor with the key as a placeholder, which of those sign in
  through OAuth and need no key, and in demo mode the topics with an example question each
  and the limits the page states. A person pastes
  `Connect me to <name>: <base>/SKILL.md — my key: <key>` into the AI they use, and it
  connects itself. The demo page now opens its "Connect your AI" block with that line and
  one Copy button, without the key before sign-in and with it after; the per-client setup
  folds under "Or connect by hand". Outside demo mode the file needs `DST_PUBLIC_BASE_URL`
  and answers 404 with the reason without it.

### Changed

- **A consumer demo's sentence is written for the person asking.** Under
  `demo.audience: consumer` the envelope had lost its trust fields, but the sentence
  inside still spoke the system's language: the metric and its definition by name, the
  declared population quoted whole, "stale after 2 days per this lens", "NULL rows", the
  date as `(data as of 2026-09-26)`. The composer now writes in a register chosen by the
  same audience the envelope is cut to: the figures as figures, no metric, column,
  definition, table, lens or threshold named, the scope said once in one plain clause
  derived from the declared population and from nothing else, and a decline in plain
  words. The date reads as a person writes it ("as of 26 September"), and a truncation,
  a cut-off answer or stale data are said in the same words. Everything checked stays
  checked: the same reconcile and numeric gate grade both registers, the scope is still
  disclosed, and the engineer's answer is unchanged. The declared population now also
  grounds the numeric gate on every serve, so a scope that names a year ("since September
  2025") is no longer withheld as an invented figure.

### Fixed

- **A filter the question states is never dropped for the table's sake.** A typed
  question naming a stored value — "which carry heroes have the highest win rate on the
  current patch" — could land on an entity with no column for that value (a per-patch
  table with no position), and was answered over every hero with a note that no
  position filter applied, while a sibling entity held the column. When the entity a
  question types onto has no column for a value the question names, the resolver now
  decides the metric (or, for a listing, the entity) again among the entities of the
  lens that hold it, and the value binds the filter there; when none of them carries a
  figure for the question, or no pick is confident, it asks which entity should answer,
  naming both, instead of serving with the filter gone. Two entities declaring a metric
  of the same name no longer see each other's figure offered as their own.

## [0.5.13] — 2026-09-27

No schema change.

### Fixed

- **A public demo's front door is the demo page.** On a deployment in demo mode, the
  root URL served the operator dashboard's login screen, so a visitor typing the
  domain met an admin sign-in instead of the demo. The root now redirects to `/demo`,
  query string preserved, on GET and HEAD alike; the dashboard's own routes are
  unchanged, and so is every deployment without demo mode.

## [0.5.12] — 2026-09-27

No schema change.

### Fixed

- **A consumer demo's answers carry no trust fields.** Under `demo.audience: consumer`,
  a demo caller's answer still carried the receipt, the trust summary, the grade, the
  certification and its provenance, the degraded lines, `definition_used` and
  `composition`: the operator's apparatus, which the person asking has no use for and
  can read as a warning. The answer is now a response type of its own, with the answer
  text, `status`, `clarification`, `data_as_of`, `truncated` and `request_id` and
  nothing else, on REST, the OpenAI-compatible endpoint and MCP alike, with no null
  where a trust field would be. The request log keeps the whole answer as before.

## [0.5.11] — 2026-09-27

No schema change.

### Fixed

- **Sign-in returns to the page it started on.** After signing in through Google, GitHub
  or Discord, a visitor to a public demo's `/demo` page landed on the site root, which is
  the dashboard login, instead of back on `/demo` with their key. The MCP consent page
  had the same fault and lost the pending OAuth request with it. Both pages now pass
  Clerk's `forceRedirectUrl` and `signUpForceRedirectUrl` set to the page's own URL,
  query string included.

### Added

- **A `demo:` section in `dst.yaml`** for what a public demo's page shows besides what dst
  derives itself: `tagline` (the sentence under the instance name), `example` (an example
  conversation as a list of `user`, `tool`, `assistant` and `receipt` turns), `privacy_url`
  and `log_days`. `dst plan` and `dst apply` validate it with the rest of the file, so an
  unknown key or an empty turn is an error naming the key. Apply stores it per org, and a
  pushed `dst.yaml` without the section clears it. Apply warns when the org it lands in
  is not the deployment's demo org, since nothing there would show it.
- **`demo.audience: consumer`** cuts a demo caller's answers to what a consumer reads:
  the prose with its scope and freshness lines, a clarification or a refusal's reason,
  and the receipt. `sql`, `data`, `citations`, `verification` and `resolution` are
  omitted on every door, REST, the OpenAI-compatible endpoint and MCP alike. The request
  log keeps the whole answer, so `dst observe` and the dashboard show it as before.
  The default, `engineer`, is the answer as it always was, and so is every caller
  outside the demo group.

### Changed

- **The public demo page is shorter and leads with connecting an AI.** Before sign-in it
  shows the instance name, one sentence, the sign-in and one line per lens with an
  example question. After sign-in it shows setup for Claude, Claude Code, Codex, ChatGPT
  and Cursor first, each with a copy button and a link to that client's docs, then the
  key, then curl and OpenAI-compatible snippets under "Other ways in", then the example
  conversation, folded. The page no longer scrolls sideways at phone width. `POST
  /auth/demo-key` returns the setup snippets as `connect` and `other`; its existing
  fields are unchanged.

## [0.5.10] — 2026-09-27

No schema change.

### Fixed

- **A failing typed-decision provider no longer errors every answer.** When the provider
  behind typed serving answered every call with an HTTP error (a 402 billing error, a
  520) or timed out, every question failed with a 502, even on a lens that declares
  `untyped_fallback: true`. The provider's failure now ends the typed attempt, not the
  request. Where the lens or the caller accepts an untyped answer, raw-SQL generation
  serves the question and its `UNTYPED:` line names the failure
  (`typed decisions unavailable: <provider> HTTP 402`). Where it does not, the question
  is refused with the same reason instead of erroring. An ambiguous term the provider
  could not decide is asked back, as on an install with no typed decider. The server
  logs the failure once at WARNING until a typed decision succeeds again, and `/ready`
  reports it in `typed_decisions` and turns `degraded` meanwhile.
- **A common word no longer binds a stored value.** "Which country has the most pro
  players?" compiled `WHERE team_name = 'Most'`, because a team is named Most and a
  question's words were matched to stored values without regard to case. A one-word
  value spelled like a function or quantifier word (most, top, all, first, any and the
  like) now binds only where the question writes it as a name: capitalised as stored in
  the middle of a sentence ("How did Most do this patch?"), quoted, or possessive
  ("Most's record"). A capital at the start of a sentence is not enough, and neither is
  a type noun in front of the word ("which team most often wins"). Multi-word values
  and words with no ordinary reading ("team spirit", "liquid") match in any case, as
  before.
- **A time window on a timestamp column follows the lens's clock, not the server's.**
  "How many matches were played last month?" bounded a timestamp-with-time-zone column
  with plain dates (`>= '2026-08-01'`), which the warehouse reads in its session's zone:
  the same query counted one match more on a server in UTC than on one in Europe/Paris,
  though the lens declares `timezone: UTC`. On DuckDB and Postgres the bounds now carry
  the lens zone's offset (`'2026-08-01 00:00:00+00:00'`), so a zone-aware column is cut
  at the lens's midnight under any session zone, and a zone-less column still compares
  wall clock to wall clock, as before. A lens with no declared timezone keeps plain
  dates. BigQuery, Snowflake and MySQL keep plain dates for now: how each reads an
  offset against its zone-less timestamp types is not yet verified.

## [0.5.9] — 2026-09-26

No schema change.

### Fixed

- **A count over a join is the count of the table the query reads from.** A certified
  answer counting the rows of a fact table joined to a dimension (`COUNT(*)`, or a count
  of the fact's key) was read as the fact's declared count, the dimension's declared
  count and an inferred count at once, so `dst test --slots` failed it against the
  typed reading of the same question. A count of every row now belongs to the entity
  its SELECT reads FROM, or to the entity whose declared key it counts, never also to a
  joined entity; a projection a declared metric accounts for is no longer also reported
  as inferred; and a column of one entity no longer matches a same-named column of
  another entity the query also reads (the fact's join key is not the dimension's key).
- **A warehouse's first open in a process is not held to the apply's step deadline.**
  A fresh machine's first apply aborted at the probe of a MotherDuck connection, past
  `DST_APPLY_STEP_TIMEOUT_S`, while a warm machine opens the same database in well under
  a second. The first open of a MotherDuck database in a process also loads the
  MotherDuck extension, downloading it on a machine that never loaded it, and attaches.
  `dst apply` now pays that one-time cost before the step deadline starts, under its
  own bound, `DST_WAREHOUSE_FIRST_OPEN_TIMEOUT_S` (default 300 s), and serving holds a
  first open to the same bound instead of the 60 s one on a routine open. A first open
  that never returns still aborts the apply with a 504 naming the open. Every warehouse
  step of an apply or a served request now logs its name, its connection and its seconds
  at INFO, so a slow step shows in the server log.

## [0.5.8] — 2026-09-26

No schema change.

### Fixed

- **Numbers reach the reader as figures, not as the machine holds them.** A win rate
  composed as 0.47085201793721976 and a median minute as 13.666441441441439 came from
  two places: the rows the composer reads carried the raw values, and the pass that
  snaps a rounded figure back to the data snapped it to the raw value too. Both now use
  one presentation: a declared rate in [0, 1] reads 47.1%, other fractions at four
  significant figures (13.67), integers with thousands separators; identifiers, years
  and text stay as stored. The rows in the response are unchanged.
- **A stated value filters; it never groups.** "Which hard supports place the most
  observer wards" grouped by position instead of ranking heroes among the hard supports,
  and a plural ("carries", "mids") did not match the stored word at all, so the filter
  was left to a decision that could drop it. A lowercase stored value now matches its
  regular plural, and a column whose value the question names is never offered as a
  grouping: the filter binds off the text, on every run.
- **An apply whose warehouse step never answers fails within a deadline and frees the
  lock.** A connection probe that the warehouse accepted and never answered held the
  org's apply lock and its transaction open for as long as the process lived, and every
  later apply got a 409. Every warehouse-touching step of an apply (opening a connection,
  probing it, running a certified answer's or an eval case's SQL) now runs under
  `DST_APPLY_STEP_TIMEOUT_S` (default 60 s); past it the apply returns a 504 naming the
  step, rolls back, and the next apply proceeds.
- **A definition that moved to a longer column name no longer aborts the apply.**
  The check for sample queries still carrying a definition's retired SQL matched by
  substring, so `position` flagged every sample reading `position_name`. It now
  matches whole tokens; a sample that really embeds the old expression still aborts.
- **A warehouse connection that never answers fails one request, not the server.** A
  MotherDuck open that never completed left every later open of the same database
  waiting on it inside DuckDB's process-wide instance cache for as long as the process
  lived; the connection's `statement_timeout_ms` never fired, because it starts once a
  connection exists, and serving had no deadline of its own on the warehouse. Each
  warehouse step of a served request (the dry run, the query, a value probe) now runs
  under `DST_SERVING_TIMEOUT_S`; past it the request answers 504 naming the step and
  logs an error, and the repair loop does not ask again. MotherDuck queries run on
  cursors of one connection the process holds per database instead of opening one per
  query; an open that does not return within 60 s fails its request, and the next
  request opens a fresh instance rather than waiting on the stuck one.
- **A name the question states binds in a dimension of thousands of values.** A
  question naming one team out of 1,662 could not type: the typed resolver matches a
  stated name against the dimension's whole value list, and `dst probe` kept that list
  only up to 500 values. Declared text dimensions now keep it up to 5,000, and
  matching stays near a millisecond. `dst query` now prints what an answer discloses
  about itself, including the `UNTYPED:` line of a raw-SQL escalation, which it used
  to drop, so such an answer no longer reads like a typed one. Run `dst probe` to
  collect the longer lists.
- **A column the question fixes to one value is never a grouping.** "The most common
  first item" of one hero ranks items among that hero's first items; the typed reading
  sometimes grouped by the item order as well, a column its own filter fixed to
  `'first'`, so one question came back in two shapes. A column an equality filter pins
  is now dropped from the grouping however its value was reached (named in the
  question, decided, bound, or fixed by a one-month window), and a ranking left with
  nothing to rank asks what to rank across.
- **A definition applied in another form is recognised as applied.** Whether an answer
  used the lens's definition was decided by comparing SQL as text: the compiled metric
  that implements a definition wraps each part in parentheses, a definition written
  over `COUNT(*)` never matched a count of the key, and correct answers were graded
  partial as computed without their own definition. Text also erred the other way: a
  condition on `name` was found inside one on `team_name`. Definitions and SQL are now
  compared as parsed trees, where qualifiers, quoting, case, parentheses and the order
  of ANDed conditions do not count, and `COUNT(*)` equals a count of a declared
  primary-key column and of nothing else. The same comparison decides which
  definitions a certified answer implements, whether a sample query still carries a
  retired definition (in `dst plan` and `dst apply` alike), and that a generated
  `COUNT(*)` computes a declared count of the key.
- **A month on a timestamp column includes its last day.** A window the question
  states ("this month", "last month") compiled on a timestamp time field to
  `started_at <= '2026-09-30'`, which is midnight, so every row after 00:00 on the
  window's last day fell out. On a timestamp the upper bound is now the first day after
  the window (`< '2026-10-01'`); a date column keeps its inclusive last day.
- **A word people use for a stored value can name it.** "Offlaners" and "midlaners"
  are not plurals of the stored `offlane` and `mid`, so a question using them left the
  filter to a decision that could drop it. A definition `about` a column can now map
  such words to stored values (`value_aliases: {offlaner: offlane, midlaner: mid}`);
  a question naming one, or its plural, filters the column to the value, read off the
  text, wherever the column's value list holds it. A map on a definition whose `about`
  names no column is a warning at plan and apply.
- **An answer that mentions a measure the lens cannot compute is not refused for
  saying so.** The backstop behind `not_computable` refused any answer whose prose named
  such a measure or one of its aliases, so an answer whose population sentence said what
  is not counted was refused, though neither the question nor its SQL touched the
  measure. The backstop now reads the result's columns, never the prose: a column named
  after a declared measure (`amount AS lifetime_value`) refuses before any prose is
  composed, and a rows-only answer, which had no prose to read, is now held to it too.
  A question naming the measure or an alias refuses before generation, as before.
- **A MotherDuck query that never returns no longer holds up the queries after it.** A
  query past `DST_SERVING_TIMEOUT_S` failed its request but kept running on the one
  connection the process holds per database, so later queries could queue behind it
  and fail at the deadline in turn. A DuckDB or MotherDuck statement the deadline gives
  up on is now interrupted; on MotherDuck, one still running 5 s after the interrupt
  retires the held connection, and the next request opens a fresh one. An apply step
  past `DST_APPLY_STEP_TIMEOUT_S` is interrupted the same way.
- **A raw-SQL answer says so on every door.** The `UNTYPED:` line could go missing two
  ways. When certified matching could not run, the query door replaced the answer's
  `degraded` lines with its own, dropping the `UNTYPED:` line and any
  graded-on-partial-evidence line; it now adds its line in front of them, and the
  request log keeps the same list. A typed reading that failed a check before serving
  was repaired by raw-SQL generation with no `UNTYPED:` line at all, so the response
  said `typed: false` while `dst query` printed nothing about it; that repair is now
  disclosed like any other. The OpenAI-compatible endpoint's `dst` block now carries
  `degraded` and `resolution` as well.

## [0.5.7] — 2026-09-25

No schema change.

### Fixed

- **A filter that decides a ratio is refused.** A typed reading that filtered on a
  column only the ratio's numerator reads (`win_rate` where `is_win`) made every row
  1.0 and served that as a ranking. It now asks, and with `untyped_fallback` raw
  generation answers. A simple metric filtered on its own column still types.
- **A number stated once restricts one column.** "The winner's gold lead at 15
  minutes" bound 15 to both the minute and the gold lead. Once a stated number is
  bound, a second column reading it asks which field it restricts.

## [0.5.6] — 2026-09-25

No schema change.

### Changed

- **The demo page says what it is about.** Its heading is the instance name, and
  before sign-in it lists what a visitor will be able to ask: each lens a demo
  caller may use, by its display name and description, with an example question its
  semantic layer declares. The MCP step says to name the connector after the
  instance.

## [0.5.5] — 2026-09-25

No schema change.

### Changed

- **In demo mode an OAuth token lives as long as a demo key** (`DST_DEMO_KEY_DAYS`,
  7 days by default). There is no refresh token, so the token is the whole grant:
  a stranger's client holds access for the week the demo promises, then reconnects.
  Outside demo mode the long default stands.

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
