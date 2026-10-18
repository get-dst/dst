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

Two vetoes sit above the bands, both deterministic. When the question and the
approved question disagree on a temporal qualifier — "this quarter" against
"last quarter", which cosine cannot see — the pair never serves. And when each
of them names a governed metric or definition (whole words; `invoiced_revenue`
matches "invoiced revenue") and the names differ, the pair never serves either:
an invoicing question does not take a collections answer because the sentences
around the name agree. A vetoed pair folds in as an exemplar instead and
generation handles the asked question.

The same inconsistency is caught before it can serve: `dst plan` and `dst apply`
embed every certified question and compare it with the rest of the corpus. Two
answers inside the paraphrase band with different SQL fail the plan, naming the
pair — one would serve the other's question as certified. Reword one so the
questions differ, or merge them. Two phrasings over the same SQL are one answer
and pass.

The caller always sees which band applied. A certified response also names who
certified the pair, and when. Never improve a verified query unless it is wrong,
and then certify the correction and say why: an approved pair is a promise about a
number, and a silent rewrite breaks the promise whether or not it changes the number.

## What a certified serve still checks

Approval vouches for the SQL, not for the warehouse standing still. Every
certified answer stores the value its SQL produced when it was verified, and
every serve runs a `restated` check against it: the live result is compared
with the stored one cell by cell, numbers at the precision the stored value
prints with. When the result has moved, the check fails, the grade caps at
`partial`, a `RESTATED` line on `degraded` names the old value, the new one and
the date it was verified, and the prose composed at certify time is not
served — the answer is written over today's rows instead (or served as rows
only, when no prose was asked for). A template compares only when it serves
the sample binding its value was recorded under; with other bound values, or
with no stored value at all, the check abstains and says why. It never passes
on nothing.

The prose a certified answer serves verbatim is composed, at certify time, over
the whole result (up to the composer's 200-row budget) and stored with the
result's row count. A result past that budget stores no prose and composes per
request. On a serve, a row count that differs from the stored one is a moved
result (`restated` fails, as above); prose from an answer certified before the
row count was recorded, over a result longer than its stored sample, is not
served either — the answer composes afresh or serves rows only, with a
`SAMPLED` line on `degraded` saying so. A sentence written over five rows never
describes fourteen.

A certified answer also discloses what governs it. The definitions written
`about:` a column the approved SQL reads, a dimension whose columns it reads,
or a metric it computes (or whose every column it reads — the de-duplication
shape), ride the certified answer the way they ride a generated one: on the
basis line after the provenance ("Computed as `net_revenue` — …"), in
`definition_used`, and on the resolution ledger, one line per term. Reading a
table does not engage every metric declared on it: a median over appointments
does not cite the no-show-rate page. Two people holding different certified
numbers for "revenue" each hold a line saying which meaning theirs is.

Definitions ride the certified rail as rules, too. A definition `about:` an
entity that carries a `sql:` predicate and a `period:` (`2026-05`, `2026`, or
`2026-05-01/2026-05-31`) is a rule on those dates — a billing export loaded
twice in one month, a migration week. Certified SQL that reads the entity over
a window overlapping the period without applying the predicate (compared as
parsed expressions, not text) fails `definition_period`: the grade caps at
`partial`, a `DEFINITION NOT APPLIED: <term>` line rides `degraded` and the
answer text, and the basis names the definition. Outside the period the rule
is silent; with no `period:` it governs the certified answers whose question
names the term, as a term's predicate does on the generated rail. `dst plan`
and `dst apply`
run the same check over every active certified answer and warn
`certified_ignores_definition`, naming the answer and the definition, so the
rule is known before the answer serves below `verified`. Approval vouches for
the SQL's meaning on the day it was written, not for a rule declared on a
period it spans.

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
`YYYY-Qn`, `YYYY-MM`, or `YYYY-MM-DD/YYYY-MM-DD`. Every form names the days it
covers inclusively: `2026-01-01/2026-09-30` covers 30 September, like `2026-09`
does. The rendered range is half-open — `{slot.start}` is the first day,
`{slot.end}` the day after the last — so `>= {slot.start} AND < {slot.end}` is
the shape to write, whichever form bound the value. An LLM
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
copying), runs the generated SQL, and compares the two **executed results** on the
certified projection: columns matched by name (a label or helper column
generation adds beside the certified ones is ignored, and the pass says so),
as sets, ignoring column order, with numbers compared as values — a percentage
against its fraction, and a value rounded to the precision the certified value
prints with, are the same number. A divergence names both sides and the choice
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
enforceable SQL belongs on the entity. Two shapes are errors, because the
`sql:` is ANDed into WHERE whenever a question names the term: a `sql:` that
is not a boolean predicate (a literal, arithmetic, an aggregate, a CASE that
yields numbers) fails plan as `definition_not_predicate`, and a `sql:` on a
page `about:` a ratio metric fails as `definition_filters_ratio` — the
predicate would pin both sides of the division to the same rows and serve
100% everywhere. Plan also warns when a status-like column's complete value
dictionary holds more than one value that reads as done (`Completed` beside
`Invoiced`) and no definition is `about:` that column
(`status_done_ambiguous`): the model will filter on whichever it picks, so a
definition has to say which values count.
