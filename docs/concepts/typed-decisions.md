# Typed decisions

A question either types or it doesn't. With a **typed-decision provider**
configured, dst stops asking a language model to write SQL and instead asks it
to *decide*: which lens, which entity, which governed metric, which dimension,
which grain, which filter column, which stored value. Every decision is a
choice over a closed set dst already owns — the semantic model and the column
profiles — and comes back with a probability per option. The SQL is then
compiled from those decisions, deterministically. Nothing in the answer was
extracted from prose.

## What gets decided

| slot | the options | where they come from |
|---|---|---|
| lens | every published lens | routing, when the caller names none |
| shape | a figure, a listing, or neither | — |
| entity | the lens's entities | `semantic/entities/` |
| reading | the declared readings of an ambiguous term | `definitions` with `possible_mappings` |
| metric, dimension, grain | the entity's declared metrics, dimensions and time grains (an entity that declares no `dimensions:` groups, when the question asks a breakdown such as "per SDR", "by source" or a ranking, by its string, boolean and date fields and by an integer field whose profile holds a complete value dictionary: the dimensions its metric catalog lists); a metric the question names word for word — by its identifier read as words, its `label`, or one of its `aliases` (`otd_rate` with `label: on-time delivery rate`) — is read off the text, and a question naming exactly one and nothing else is compiled without any decision | the entity file |
| ranking direction | "highest", "top 5" and "fewest" in the question; "best" and "worst" from the metric's declared `better: higher \| lower` | the entity file |
| filter column and value | the entity's fields; a value from the column's complete dictionary, or a stored value the question names word for word (declared dimensions keep a whole dictionary for this; a value spelled like a common word, such as a team named `Most`, only where the question writes it as a name: capitalised mid-sentence, quoted, or possessive), or a word a definition maps to one (`value_aliases: {offlaner: offlane}` on a definition `about: entity.column`) | the column profile (`dst introspect --profile`); `definitions` |
| window | none — a stated period is parsed, never decided ("in August 2026", "at the end of August 2026", "as of 2026-08-31", "Q3 2026"), days included ("on 28 August 2026", "August 28, 2026", "between 5 and 12 October", "5–12 Oct", "the week of 5 October", "the first half of August 2026", "the first 7 days of October", "the last 3 days of August", "the start of August", "the end of last month"); a day written without a year is its most recent occurrence on or before the lens clock, and a day window on a month-form period column asks rather than serving the month. A vague part of a month ("early", "mid-", "late August") asks, offering the days each reading would use, and so does any other day-level qualifier beside a month the parser cannot turn into days ("the first few days of August"): neither is ever served as the whole month. A level metric reads "the end of August" as August's last snapshot; on a timestamp column its bounds are midnights in the lens's declared `timezone` (DuckDB, Postgres), whatever the server's zone; a metric declared `additivity: level` reads the window as its last snapshot | `timewindow`; the entity file |

A slot the question does not settle is asked back as a **clarification naming
the slot**: which metric, which value of `status`. The asking agent answers by
re-asking with `bindings` (an open value it supplies) — it never has to know
the SQL. Raw-SQL generation survives only as a disclosed escalation: the caller
opts into it with `allow_untyped: true`, or the lens owner makes it the lens's
default with `untyped_fallback: true` in `lens.yaml`, for callers that cannot be
expected to know the flag. A caller's `allow_untyped: false` still demands
typed-only. Such an answer carries `typed: false` on its ledger and an
`UNTYPED:` line, and the audit counts it apart.

When the typed-decision provider itself fails (an HTTP error, a timeout, the
network), the typed attempt ends and the request does not. A question that
accepts an untyped answer is served by raw-SQL generation, and its `UNTYPED:`
line names the failure: `typed decisions unavailable: typesafe HTTP 402`. A
typed-only question is refused with the same reason; asked again once the
provider recovers, it types. The server logs the failure once until a typed
decision succeeds again, and `/ready` reports it under `typed_decisions`.

## Rails that ask instead of acting

A few facts are read off the text and the lens, never decided, and each one
can only move an answer toward a clarification:

- **Candidates are answerable.** A field, dimension or filter is never offered
  when it is a key (the declared `primary_key`, a column named `*_id`,
  `*_key` or `unique_key`, a hidden column), nor a date or timestamp when the
  question is not about time, unless the question names the column. A text
  column that is one part of a composite key (`host_name` in `[day,
  host_name]`) is not a key on its own: it is what a per-host question groups
  by. A listing with nothing left asks which metric is meant, and a
  breakdown nothing on offer groups asks with the entity's fields as the
  options.
- **One measure, several settings.** When the chosen metric's siblings differ
  from it by one parameter token (`rebooked_within_7d`, `_30d`, `_90d`) and the
  question states none, the answer asks which one, whatever the probability.
  The ledger records it as a `parameter` clarification.
- **A measure the lens does not declare.** A question naming a measure that
  the entity holds only as a numeric column or a measure definition ("desk
  hours" where every metric counts bookings) asks, rather than answering in
  the nearest metric's unit. A bare `COUNT(*)` metric answers only a question
  counting the entity's own rows ("how many records", "number of movements").
- **A value no column can take.** A code-form value after a preposition ("for
  APAC") that is no declared name, no period and no stored value, on an
  entity whose text columns all have complete dictionaries, asks: "this lens
  has no field for APAC".
- **A shape `none` is not always a decline.** A question naming a governed
  metric word for word is a figure; a question matching an entity's
  `common_questions` or a "Use when" `use_cases` line asks for the shape
  instead of refusing.
- **The current period is to date.** "This month", "this week", "today"
  resolve on the lens clock from the period's first day through today, and
  the ledger carries the window it used. A `level` metric over a window asks
  no further restriction: the window and the snapshot rule settle it.

## The policy: act unless none

A typed-decision provider acts on its top choice. The one thing that stops an
answer is the provider choosing **none** — no metric fits, no entity holds
this, the term's readings do not cover the wording. There is no per-slot
confidence threshold to tune. Where a slot's decisions have been measured
against gold, the measured bar is **disclosure**, not a gate: an answer that
acted below it is served with a `decision_confidence` check on its
verification, its grade capped at partial, and the slot and probability named
in the answer. Nothing is refused for being unsure; nothing unsure is silent.

Every decision — slot, choice, probability, runner-up, verdict — rides the
answer's resolution ledger and its [receipt](receipts.md), so a wrong answer
is a named slot with a number, not a paragraph of SQL to read.

## Configuring one

```yaml
providers:
  jev:
    type: typesafe
    api_key_env: DST_API_KEY_JEV
```

Add the key to `.env` (or the deployment's secret store) under the name you
chose. That is all: with `DST_TYPED_SERVING=auto` (the default) every lens
switches to typed serving the moment a `typesafe` provider resolves. `on`
runs the same machinery on a voting chat model (five samples per decision — a
coarser probability, and act bars apply); `off` keeps the one-shot intent
emission.

A typed-decision provider also routes: it decides over **every** published
lens, with no similarity shortlist in front of it. Embeddings remain for one
job — shortlisting certified answers a new question might repeat — and as the
fallback router when no decider is configured.

## Testing what types

Typed serving makes the test suite three lanes, each with one owner for its
failures ([Evaluation](../guides/evaluation.md)):

1. **Resolution.** Every certified question resolved by the provider and
   graded per slot against the answer's typed gold. No warehouse, seconds per
   corpus; `--repeat N` resolves each question N times and fails any that does
   not produce the same intent every time.
2. **Compile.** The compiled SQL compared to the certified SQL, canonicalised.
   Identical is proven without a query. A layer that does not compile is a
   dst defect, caught at `dst plan`.
3. **Data.** The certified SQL executed and compared to its stored value — the
   only lane that touches the warehouse, and it runs only for what the first
   two could not prove (`--rows` runs it for everything).
