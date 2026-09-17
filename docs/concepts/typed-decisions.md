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
| metric, dimension, grain | the entity's declared metrics, dimensions and time grains | the entity file |
| filter column and value | the entity's fields; a value from the column's complete dictionary | the column profile (`dst introspect --profile`) |
| window | none — a stated period is parsed, never decided | `timewindow` |

A slot the question does not settle is asked back as a **clarification naming
the slot**: which metric, which value of `status`. The asking agent answers by
re-asking with `bindings` (an open value it supplies) — it never has to know
the SQL. Raw-SQL generation survives only as a disclosed escalation the caller
opts into with `allow_untyped: true`; such an answer carries `typed: false` on
its ledger and an `UNTYPED:` line, and the audit counts it apart.

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
