# Certified answers & certified definitions

Two things with similar names, easily confused. They close two different gaps.

A **certified definition** is a page about one metric. The pages are loaded
from the directory a lens names in `model.certified_dir`, and rendered into the
lens's file tree at `lenses/<name>/certified/*.md` — that copy is an export the
loader skips, not an authoring path. A page carries the definition, the grain (what
one row means), the source tables, the canonical SQL, and a verified value
that was actually run and checked. It closes the **decision gap**: which
meaning, which table, which filter counts as this metric here.

A **certified answer** is a question-and-SQL pair a named human vouched for.
Each entry (`lenses/<name>/certified_answers.yaml`) records where it came from
(`source`), who checked it (`verified_by`), a `verified_value` that was
actually run, and a `status: active | retired`. It closes the **execution
gap**: the exact query a human verified, served as-is instead of being
generated again.

Incoming questions are matched to certified answers by meaning, not exact
wording. dst compares embeddings — numeric fingerprints of the text — so every
`active` answer needs one. If no embedding provider is configured, the answer
lands in a third, derived status: `pending_embedding`. Both `dst apply` and
`dst test` name it until `dst reindex` embeds it and promotes it back. It is
never silently unmatchable.

## Knowing isn't doing

A model with the right definition in context can still compute it wrong.
Choosing the right meaning and computing it correctly fail independently. That
is why both artifacts exist. A well-written definition alone is not a
correctness guarantee; the certified answer — a worked, verified example — is
what closes the second gap.

## How certified answers serve

When a question arrives, dst scores how similar it is to each active certified
answer. Three bands:

| Band | What happens |
|---|---|
| ≥ 0.95 | the approved SQL is served word for word, `certification="certified"`, no generation at all |
| 0.90–0.95 | a cheap check asks the model "would the SAME SQL answer both questions?"; yes promotes to certified; any provider error fails closed — the match is not treated as certified |
| ≥ 0.83 | matching pairs go into the prompt as worked examples, `certification="assisted"` |

The cutoffs are embedder-relative and dst-owned, never a knob: small local
models spread cosines where hosted ones compress them, so an embedder dst pins
gets a calibrated preset instead (`BAAI/bge-small-en-v1.5`: 0.93 / 0.80 / 0.78).
The numbers above are the defaults every other embedder gets.

One veto sits above the bands: when the question and the approved question
disagree on a temporal qualifier — "this quarter" against "last quarter",
which cosine cannot see — the pair never serves. It folds in as an exemplar
instead and generation handles the asked window.

The caller always sees which band applied. A certified response also names who
certified the pair, and when.

## Templates: one entry for a question family

A certified answer can carry `{slot}` placeholders in both the question and
the SQL. A family of questions that differ only in one value — "revenue in
Q1", "revenue in Q2" — becomes one entry instead of many frozen pairs:

```yaml
- question: revenue in {period}
  sql: >
    SELECT sum(amount) FROM orders
    WHERE order_date >= {period.start} AND order_date < {period.end}
  slots:
    period:
      type: date_range          # date_range | date | enum | number
      # enum slots list their values inline: `values: [EUR, USD]`
  sample_bindings:
    - period: 2026-Q2           # non-empty = testable
```

Everything about a template is deterministic: same input, same output, no
model judgment on the serving path. Slots are typed (`date_range`, `date`,
`enum`, `number`; an enum's values go inline under `values:`). The `date_range`
grammar is tiny and fixed — one canonical string, never a mapping: `YYYY`,
`YYYY-Qn`, `YYYY-MM`, or `YYYY-MM-DD/YYYY-MM-DD`.
Ranges are half-open: `{slot.start}` is included, `{slot.end}` is not. An LLM
does *propose* slot values for a matched question, but nothing it says reaches
the SQL except through a validator, and the value is rendered as a proper SQL
literal for your warehouse's dialect — never pasted in as a string. A template
serve writes no prose either: the answer text is a fixed frame filled in by
code.

`sample_bindings` must be non-empty for a template to be testable: the first
binding anchors matching and serves as the test case. A template match reports
`certified_match: "parameterized"` on the response, alongside `exact` and
`equivalent`.

## Where certified answers come from

Four paths. A review ruling can promote a correction. You can author entries
in `certified_answers.yaml` — or have your agent import them from a BI tool's
exports — and land them with `dst apply` (dst owns the slots and gates, not a
pile of brittle per-BI importers). dst can generate one per governed
definition. Or you can POST one directly to the API.

The two authoring paths — `dst apply` and the direct POST — are gated the same
way: the SQL must parse, pass shape checks, and touch no tables outside the
lens boundary. The doctrine underneath them is **never auto-certify**: the
human approval *is* the trust, and `verified_by` is a human's name or nothing.
The generate-per-definition endpoint is the exception to both, and worth
knowing before you call it — it guards SQL shape but not the lens boundary,
and it stores what it drafts as `active` without asking anyone. Treat its
output as a draft to review and retire, not as certification.

## The corpus is the regression suite

`dst test` treats every active certified answer as a test with a known-good
result. For each one it runs the stored SQL, asks the question again through
generation (with certified matching switched off, so the test cannot pass by
copying), runs the generated SQL, and compares the two **executed results** —
as sets, ignoring column order. A divergence names both sides and the choice
it forces: fix the definition, or re-certify/retire the answer.

Each pair also records `bindings`: content hashes of the shared assets its SQL
depends on. When a shared asset changes, the answers that depend on it are
flagged for re-test, and a green `dst test` or apply gate re-stamps them as
current. Bindings are derived server-side — never a key you write into
`certified_answers.yaml`, which rejects unknown keys outright.

The apply that *lands* an answer (newly created, or with re-authored SQL) also
tests it on the spot, **unconditionally** — `eval_gate` governs the publish
gate, never this self-test. A divergence there is an alert, never a block.
That is deliberate: divergence at certification time can be the point, because
the certified answer overrides generation. The alert asks you to re-check the
stored SQL if override wasn't what you meant.

Retirement keeps history: a `retired` answer stays listed and exported but is
never served, never matched, never tested. Retire rather than delete, for two
reasons. First, provenance: a certified answer records that a named human
vouched for a number the org then served. Deleting it erases the fact you ever
served that number; retiring keeps the trail and stops the serving. Second,
answers approved through the review queue live on the server, not in your
files (see below), so `status: retired` is the only way to stop them from the
files. Delete only when the pair should never have existed.

Deletion follows the files. A pushed `certified_answers.yaml` owns the entries
that came from files, so removing an entry deletes it on the next apply, and
the apply output counts the deletion loudly. Review-approved answers
(`source: review:*`) originate on the server and survive even when the file
doesn't mention them. A tree with no `certified_answers.yaml` at all leaves
the whole surface untouched. One guard applies either way the library empties
— the last active answer retired or deleted. Under `eval_gate: block` the
apply aborts: an empty library would leave the gate with nothing to check,
waving every later apply through. Keep at least one active answer, or set
`eval_gate: warn`/`off` and re-apply. Under `warn` it publishes, with a loud
warning that the library is now empty. A lens that never had active answers is
unaffected and keeps doing its first applies.

## The pages, precisely

A certified-definition page's frontmatter carries `metric`, `summary`,
`question`, `grain`, `sources`, `usage_mode` (`auto` = always in context,
`search` = included when relevant), `verified_value` (a `{value, as_of}`
mapping, not a bare number), the canonical `sql`, and optionally the ambiguous
form — `status: ambiguous` plus `possible_mappings`. One page is at once
context for the model, ground truth, a certified answer, and governance
metadata. Three of those keys — `question`, `usage_mode`, `verified_value` —
only do anything on a certified-definitions page; on a `semantic/definitions/`
or lens-local page they parse and are ignored, and `dst apply` says so. One
rule: a page carrying both a canonical `sql:` and an `about:` pointing at
something that is not a metric warns in lint (`definition_double_truth`) —
enforceable SQL belongs on the entity.
