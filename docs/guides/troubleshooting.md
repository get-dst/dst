# Troubleshooting

The failure modes people hit when they run dst against a real warehouse, with what
each one looks like, where the evidence is, and who owns the fix. Three owners come
up, and the first thing to do with any symptom is to say which:

- **dst**: the machinery misbehaved. A number served wrong, a fact that reaches one
  surface and not another, a guarantee that passed without checking anything.
- **your files**: the lens, semantic layer or eval cases are incomplete or wrong. dst has
  the mechanism; the project does not use it yet.
- **configuration**: a provider, a database URL or a budget is unset or points
  somewhere else.

A refusal or a clarification is an outcome, not an error. The defects worth chasing are
the silent ones: a wrong number served as right, a run that exists and cannot be found,
a pass that checked nothing. The scaffolded `dst-debug` skill walks a coding agent
through the same table; `dst init --skills-only` adds it to an older project.

## `dst test` ran, but `dst runs` and the dashboard show no runs

`dst test` runs in-process and records the run in the database named by `DATABASE_URL`
for that shell or `--dir` project. `dst runs` and the dashboard read the history through
the server at `DST_URL`. When those two name different databases, the run exists where
`dst test` said and nowhere the server looks. The last lines of `dst test` say where:

```
recorded: run 3f9a12c0 (acme/finance) in postgresql+psycopg://dst_app:***@db.internal:5432/dst
```

Compare that with the server's `DATABASE_URL`. A CI job whose database is a service
container records every run and discards it with the runner. Owner: configuration. See
[Environments and CI](environments-and-ci.md#which-database-did-dst-test-write-to).

## Certified answers never match, and `N certified answers have no embedding`

Certified matching is a cosine match over embeddings. Without an embeddings provider
nothing is embedded, no question ever matches a certified answer, every request logs
`certification: none`, and routing falls back to the decider alone. `GET /ready` says so:

```json
{"embeddings": "unconfigured", "certified_matching": "unavailable"}
```

Configure an embeddings provider ([Configuration](../reference/configuration.md)), then
`dst reindex`. Owner: configuration. The certified suite still runs without embeddings,
since it executes the stored SQL directly, so a green `dst test` does not mean matching
works.

## Slot-lane failures where the gold names every column the SQL touches

A slot-lane row like `expected bop_arr, arr_change, nrr; got nrr` with `"gold":
"attributed"` means the lane had no stored gold for that answer and attributed one from
the certified SQL. Attribution reads columns, and a ratio's SQL touches more columns
than the question means. The decider picked the one metric the question asks for, and
the data lane passed.

Stored gold comes from an exact, typed, green rows run: `dst test --rows` executes
every case, and a case whose rows match the certified result exactly, with a typed
resolution, has that resolution saved as the answer's gold. From then on the row reads
`"gold": "stored"` and the lane grades the question, not the SQL. Two things keep gold
unstored on purpose: a lenient pass (the reason says `shape-lenient` or
`month-lenient`), and a case that answered untyped. If a case passed exactly and typed
and the gold is still attributed on the next run, that is a dst defect; report it with
the JSON row. Owner: dst, with one `--rows` run first.

## `did not type` on a question the untyped path answers

The slot row says `did not type: <why>` and the data lane passed, or the same question
through `/v1/query` returns the right figure with an `UNTYPED` note. The decider
declined or asked where generation answered. Read the `decisions` list on the row: a
refusal reads `not a question this lens's data answers`, a clarification names the
slot it could not fill.

When a sibling question types and this one does not, the difference is usually one
word the decider does not map to a declared metric. When the question is a label
turned into a sentence ("What was the <column label> in August 2026?"), the fix is a
real question in the lens's words. Owner: your files first. When wording is not the
cause, the untyped fallback serves the question correctly meanwhile, and the
`decisions` on the row are what to send. Owner: dst.

## A level metric asks which field to restrict by

After `additivity: level` on a metric, "customers at the end of August" clarifies with a
list of columns while the sibling "ARR at the end of August" types. A level declaration
must not open a filter slot. Send the slot row with its `decisions` and the metric's
entity file. Owner: dst. Until it is fixed, the certified answer serves the question
verbatim.

## A pass with a `month-lenient` or `shape-lenient` reason

Not a failure. The rows matched under a stated rewrite: month-end labels on one side and
month-start labels on the other for the same months and values, or a certified scalar
found beside label columns. The reason names the rewrite, and such a pass never stores
typed gold. Rows whose labels differ in any other way, two arbitrary days in one month
for instance, stay a failure. Owner: nobody; the reason is the receipt.

## Apply fails with a 504 naming `DST_APPLY_STEP_TIMEOUT_S`

Every warehouse-touching step of an apply has a deadline (60 seconds by default). A
connection that spans many datasets probes longer than one that spans one, and the probe
is the step that exceeds it first. The message names the step and the setting. Raise
`DST_APPLY_STEP_TIMEOUT_S` on the server; the first open of a warehouse has its own
budget (`warehouse_first_open_timeout_s`, 300 seconds) and is not what this bound
measures. Owner: configuration. A budget that scales with the connection's table count
instead of one constant is an open dst item.

## The router sends questions to the wrong lens once there are many

With one lens per mart and sibling marts on the same subject, routing picks between
anchors that overlap. `use_when` is the anchor, and anchors copied from one source ("use
for daily trends") read the same across siblings. Without embeddings the router is the
decider alone. Write anchors that say what this lens answers that its sibling does not,
and pin the boundary with `expect: refuse` behavioral cases on each sibling's questions.
Owner: your files, and configuration for embeddings. A lens nothing ever routes to, with
distinct anchors and embeddings on, is worth reporting.

## A lens answers a sibling's question when asked directly

`use_when` steers routing. On the direct door (`/v1/lenses/<lens>/query`) a lens answers
whatever its columns can compute, so "show me membership fee levels" asked of a
movements lens produces a number. Scope on the direct door is not enforced from prose,
by design: a heuristic that guesses intent from wording would refuse real questions. The
pin is an `expect: refuse` behavioral case per sibling question; the business suite that
found the gap is that suite. Owner: your files.

## Serving KPIs count your own test traffic

`dst test` asks the lens its own questions, so every run lands in `request_log` as
served traffic until real callers exist. The suite sets the `agent` column; measurement
traffic you send yourself should carry an `X-dst-Agent` header. Filter on it. Owner:
nobody; by design.

## Business-suite questions "fail" with a clarification

An author-phrased question that names no single metric ("how is occupancy tracking
against the weekly member goal?") gets a clarification. That is the decider refusing to
guess, which is what it is for. The gap is the alias or definition the lens does not
declare. Owner: your files.

## Result rows are not kept

`request_log` keeps the question, the SQL and the outcome. Row samples are kept only when
the lens sets `logging.log_samples: true`; the default is off. Owner: configuration.

## Probe refuses a table over the sampling budget

The refusal names `sample_bytes_budget`. Set it under the connection's `config` in
`dst.yaml`. Owner: configuration. Never patch the installed package to move a constant:
the next upgrade reinstalls the original, and nothing says so.

## What to send when reporting a dst defect

- `dst --version`, and `GET /ready` from the server.
- The `recorded:` line from `dst test`, and the run id.
- The failing row from `dst test --json`: its `answer_id` or `case_id`, `decisions`,
  `gold`, `reason`, `certified` and `generated`.
- For a served answer, the request id and the lens.
- The triage line: which owner you think it is, and why.
