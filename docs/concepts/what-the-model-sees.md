# What the model sees

Authoring stops being guesswork when you know what reaches the prompt, in what
order, and what never reaches it at all. This page is the map: what each model
call sees, what is deliberately withheld, and the one command that renders all
of it for a real question — no generation call, no warehouse call.

Every serving call is plain text in, plain text out: one cacheable system block
plus one user message. No tool use, no structured-output API — the generators
ask for JSON in the prompt and parse it themselves (the raw-SQL tier re-asks up
to twice on a malformed reply); the composer writes prose. The surface is the
lowest common denominator any OpenAI-compatible endpoint can serve, which is
what keeps bring-your-own-model honest.

## The grounded prompt, piece by piece

The system block carries the rules and the **whole serialized semantic model**
— every entity the lens selected, written out in full. dst does not gamble on
a search step for the facts the SQL must bind to. Tables render as
`<physical> AS <entity>` (the alias is dropped when the two names are already
identical), so the model never resolves physical table names itself. Metric
lines are produced by the same deterministic compiler that produces real SQL,
so the prompt teaches the exact expression the guards will later demand. Joins
carry their type and cardinality — how the row counts multiply. Definitions
render in full: a settled term with its `about:` binding and any enforceable
`[sql: …]`; a contested one instead with the
`[AMBIGUOUS — MUST clarify, never guess]` marker and its options, and nothing
else. The only cap in the serializer is `common_questions[:5]`: the way to
keep the prompt small is scoping (a [lens](lens.md) selects few entities), not
truncation.

The user turn carries the per-question context. The certified-definition page
comes **first**, ahead of every retrieved chunk. Then up to 6 retrieved
context chunks. Then the answer contract, **last**, right next to the
question: project exactly the quantities the question names plus grouping
keys, return results at the asked grain, full precision unless rounding is
asked. Certified exemplars enter anonymously: approved pairs render exactly
like lens-authored sample queries — no score, no provenance, no "prefer these"
instruction. The caller is told an answer was certified-assisted; the model is
not.

## The intent path is the blind path

When a lens has metrics, the cheap first pass is the intent generator: the
model fills in a structured `QueryIntent` — a small form naming the metric,
grouping, and filters — and code compiles it to SQL. Its prompt is
deliberately leaner, and knowing what it lacks is the single most important
thing about the governance model:

| Rendered | Grounded prompt | Intent prompt |
|---|---|---|
| Physical table names | yes (`AS entity`) | never; SQL is compiled by code |
| Metrics | compiled SQL + `REQUIRES filters` | measure expression + `[only where …]`, never compiled SQL |
| Joins | with type and cardinality | a bare "entities you may combine" list |
| Definitions | full body + `about` + `[sql: …]` + `AMBIGUOUS` marker | full body + `[filter: …]`, no `AMBIGUOUS` marker |
| Certified exemplars | yes, as sample queries | never |
| Common questions | first 5 | never |
| `ai_instructions` | yes | never; by design, below |

The cheap tier cannot see the ambiguity marker, the join keys, the exemplars,
or the instructions. So ask-don't-guess is a guarantee carried by **code**,
not a model behavior. Deterministic clarification and exclusion run before
either generator, and the filter, time, value and shape guards run on whatever
SQL comes back, whichever tier produced it. Any promise that lived only in the
prompt would silently not apply on this path. See
[Clarification & refusal](clarify-and-refusal.md).

`ai_instructions` is left out of the lean pass because the gap is grammar, not
knowledge. The structured `QueryIntent` form simply cannot express rulings
like "list DISTINCT" or "order by a count you do not project". The lean pass
could read such a ruling and still be unable to obey it; its only lever is
escalating to the raw-SQL tier. `dst apply` warns
`intent_tier_escalation_only`, naming what a metric lens loses. A ruling that
must hold on every answer belongs on the dimension, metric, or definition it
is about — those the lean pass does render.

## The composer, and the `about:` rule

The second model call — the composer — writes the English. It sees the
question, the SQL, up to `max_rows_to_compose` rows (default 200), a flag
saying whether the rows were cut short, and two curated extras:

- **Governing definitions**: the full bodies of the definitions bound to the
  columns the answer projects — they decide what the values mean — plus
  whatever definition the generator reports it applied. Never capped, never
  truncated: a silently dropped definition is the exact bug this scoping
  exists to prevent.
- **Data notes**: declared facts about the projected columns, taken from the
  table profile and handed over explicitly, with a rule to cite them. A useful
  caveat becomes a cited one instead of an invention.

It does **not** see the semantic model or the retrieved chunks, and no
definition reaches it except through one of two doors — a binding to a
projected column, or the generator's own report of the one it applied. (A
handful of declared facts do ride along: the
lens's currency, timezone and freshness contract, and the `population` of the
entities the SQL touches.) Which produces the one authoring trap worth
memorizing:

!!! warning "A caveat in a definition body does not reach the prose on its own"
    A definition reaches the composer through `about: entity.column` (or,
    failing that, a result column literally named after the term). A
    definition without that binding still steers SQL generation — that is a
    different prompt — but the model writing the sentence will not have it. A
    caveat you wrote into the body ("returned orders still count toward this")
    silently fails to appear in the answer. If you care that a caveat reaches
    the reader, give the definition an `about:` pointing at a column answers
    actually project.

## What the model never sees

Some facts are withheld by construction, and where they stop is the point:

| Withheld | Why, and where it stops |
|---|---|
| Caller identity and groups | used for authorization and the trace only; never in any serving prompt |
| `excluded_metrics` | the model is never told what it must not compute: the refusal runs in code before the LLM, so the boundary cannot be argued with |
| `data_as_of`, certified provenance | response-only trust signals; the model neither knows nor can fake them; see [Receipts](receipts.md) |
| Rejected SQL | never returned to the caller; kept in the trace for review |

## `dst lens prompt`: stop guessing, just look

This verb renders everything above for a real question, fully assembled — no
generation call, no composition call, no warehouse call. (It does embed the
question, exactly as serving would, so retrieval and certified matching are
the real ones.) It is the only way to answer "did the thing I authored
actually reach the model?". Abridged output, annotated:

```
$ dst lens prompt customer_value "How many repeat customers are there?"
lens customer_value · tier intent · prompt-set cb6c43241f0a · assembled, no LLM call

=== first pass: metric-layer prompt ===     # the lean tier, verbatim
=== escalation: raw-SQL prompt ===          # what only an escalation would see
=== context (user turn) === context_chunks: 0 · certified_exemplars: 0 · ...

=== reaching the model ===                  # per authored asset kind: n/n counts
entity 2/2 · field 11/11 · metric 5/5 · join 1/1 · definition 3/3

=== NOT reaching the model ===              # named, with the reason
=== reaching it only if generation escalates to the raw-SQL tier ===
join          1    orders -> customers      # the per-lens version of the
sample_query  1    ...                      # intent_tier_escalation_only warning
instructions  1    ai_instructions

=== second call: compose prompt (writes the English) ===
=== definitions that can reach the composer ===
lifetime_value    sent when the answer projects `customer_lifetime_value`

=== NOT reaching the composer ===           # named, with the reason
repeat_customer   no `about: entity.column` — reaches the composer only if a
                  result column is literally named after the term
```

The counts panel is reassurance. The two "NOT reaching" panels are the
debugging surface: an asset listed there, by name, with its reason, is an
authored fact that will not influence the next answer. Fix the binding, or
accept that it only steers the tier that renders it.
