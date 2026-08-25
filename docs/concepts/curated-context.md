# Curated context

Before the model writes any SQL, it reads background material — context. dst's
context is **curated**: hand-picked and deliberately small. It is the semantic
model (the files that describe your tables and metrics), the governed
definitions (what your business terms mean), and the certified-definition
pages a lens selects. A lens is dst's unit of scope; it picks which of those
files apply.

The opposite is **dumping**: pointing the model at the whole catalog, the
auto-generated data dictionary, everything you have. A data dictionary teaches
the model enough to compute a *plausible* answer. It says nothing about which
computation is the approved one, so the model decides that itself. Select the
context instead, and the approved computation is the one it has.

## How the machinery enforces it

- **The semantic model goes into the prompt whole.** dst never gambles on a
  search step that might miss the facts the SQL must bind to. The way to keep
  the prompt small is scoping: the lens selects few entities. dst does not
  truncate — the one cap in the serializer is the first five
  `common_questions` per entity. (A lens with metrics generates in two tiers,
  and the cheap first tier renders a reduced form of the model; see
  [What the model sees](what-the-model-sees.md).)
- **Certified-definition pages are selected, not dumped.** Pages marked
  `usage_mode: auto` are always in context. Pages marked `search` are included
  only when a simple word-overlap check says they match the question, and at
  most six of them.
- **Prose context** — free-form documents pushed into a lens — **is retrieved
  top-6**: the six most relevant chunks. The certified page always comes
  *first*, ahead of every retrieved chunk.

## How curated context is authored

An entity file describes one table. Its fields teach judgment, not just
structure: `grain` ("one row per closed deal"), `use_cases` (when to use the
table *and when to avoid it*), `common_questions`, how joins multiply rows,
and metric `filters` — conditions added to the WHERE clause automatically
whenever that metric is computed.

```yaml
# semantic/entities/deals.yaml (excerpt)
grain: one row per closed deal
use_cases:
  - Use for bookings, deal counts, deal sizes, and new-vs-existing business questions.
  - Avoid for commission or payout questions - payouts carries what reps earned.
```

Definitions (`semantic/definitions/<term>.md`) are small markdown files: a
header plus prose. A definition can carry an enforceable `sql_expr` — the
exact SQL expression for the term. A contested term can be marked
`status: ambiguous`, with its possible meanings listed. That turns guessing
into asking (see [Clarification & refusal](clarify-and-refusal.md)).
Per-metric [certified-definition pages](certified.md) carry the verified end
of the curated layer: a definition plus a value a human actually ran and
checked.

## How curated context is selected

The [lens](lens.md) selects; nothing flows in by default. `select.definitions`
starts empty on purpose — you name the terms you want. Every selected term
also helps route questions to the lens, so shared vocabulary must not spill
into every lens automatically.

The wrong answers this closes are rarely obvious errors. They are plausible:
test accounts counted as customers, two eras of invoices never combined, the
same row counted twice because old versions were kept. Curated context and
certified answers carry the facts that decide those cases.
