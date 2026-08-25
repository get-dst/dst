# Clarification & refusal

dst asks rather than guesses. That is a **guarantee, not a model behavior**:
every promise on this page is enforced in code that runs before or after the
model, never by a prompt rule alone — prompt rules alone do not hold. In
particular, the cheap first-tier generation path uses a leaner prompt that
renders no ambiguity marker and no list of a term's possible meanings, so any
promise that lived only in the prompt would silently not apply there.

## Contested terms: clarify, deterministically

A governed term can be *ambiguous*: instead of one meaning, it carries
`possible_mappings` — a list of the meanings it might have. A question that
uses such a term without picking a meaning gets a `ClarificationRequest` back,
before any SQL is generated and before any warehouse touch. The [lens](lens.md)
presents the readings it can actually serve, in the order they were authored —
it does not rank them, and it never silently decides.

!!! clarify "Clarify"
    "Average value per customer?" `value` maps to lifetime value
    (`customers.customer_lifetime_value`) or order amount (`orders.amount`).
    dst returns the options and waits; naming a meaning in the question
    answers normally.

Certified answers are exempt by design: a human approved that exact
question→SQL pair, so there is nothing left to clarify.

## Excluded scope: refuse, before anything runs

A lens can deliberately leave a metric out — by not selecting it in
`lens.yaml`. A question naming that metric is rejected before any SQL is
generated and before any warehouse touch. A curator who left
`conversion_rate` out drew a boundary, and the refusal must not depend on a
model's mood. The boundary covers shape, not just name: generated SQL that
would rebuild a dropped metric from raw columns — a conversion rate reassembled
as a flag-grouped count and a division — is refused the same way.

Both doors speak one refusal, from one function, differing only in the opening
clause (asked by name / would be composed from raw columns). It names the
metric, the
path that would make it governed (select it in `lens.yaml`, or certify the
answer), and — when exactly one other lens in the org carries the metric —
that lens by name. One knob covers both doors too:
`serve_ungoverned_shapes: true` lets the question run and serve at
`confidence: unverified`, whether it asked by name or rebuilt the shape. A
rejected response never includes the SQL it refused to run; the trace keeps it
for review.

## Absent data: decline, never a confident zero

When the data to answer a question doesn't exist, the correct output is a
decline — not an empty result dressed up as `0`. A result row where every
aggregate is NULL counts as *no evidence* in verification: for a question whose
data does not exist, declining is the correct outcome.

Where dst measures answer quality — the benchmark harness, and the declined
column in Observe — it scores three ways (correct / wrong / declined), with
wrong-rate as a co-headline, because a wrong answer is worse than no answer.

A related failure hides one layer down: the data EXISTS, but the filter is
written in the question's vocabulary instead of the column's.
`WHERE country = 'Finland'` against a column that holds `'FI'` returns zero
rows, and there is nothing to decline, because the query ran fine. The zero
even has a disguise: `COUNT(*)` over an empty match returns one row holding
`0`, and a row being present looks like evidence. So before either kind of
zero leaves the building, serving investigates — deterministically, with no
extra model call:

- **Known value dictionary.** If a column has a committed `dst probe` artifact
  — a saved list of the values it actually holds — then a `=`/`IN` string
  literal outside that *complete* dictionary is caught **before execution**,
  and the query is repaired with the real values.
- **No dictionary.** A zero-evidence result (no rows, or the all-aggregate
  `0`/NULL row) with string-literal filters buys one governed probe: at most 2
  `SELECT DISTINCT` reads, read-only, row-capped. If the probe proves a
  literal absent from a column with a small
  fixed set of values, the query is repaired. If the column really holds the
  literal, the zero is honest and serves unchanged.

When repair can't use what the investigation learned, there is a floor:

- **Proven absent, repairs exhausted → ask.** The response is a
  `clarification` with `kind: unknown_value`: `term` names the column,
  `options` lists the values it actually holds, and the question notes that
  the absence may itself be the answer. Ask-don't-guess, extended from
  governed terms to warehouse values.
- **Nothing learned → serve, graded honestly.** A zero the probes could not
  check serves with the `empty_result_investigation` check **failed** and
  confidence capped at `partial`, never `verified` — which is exactly what
  `auto_review: "partial"` routes into the review queue. An
  investigated-and-confirmed absence keeps its badge: that zero is the data's
  answer, and now there is a receipt saying so.

## An invented figure: withheld, never shipped

The same doctrine covers the written answer itself. On a generated serve,
every figure in the sentence must be traceable to the rows that came back —
the numeric-grounding check. If the composed answer fails it, composition
retries **once**, with the failure named in the prompt. If it fails again, the
prose is withheld entirely. The response sets `composition: "fallback"` and
the answer becomes a frame written by code: the question restated, the row and
column count, and a preview of the rows themselves. The rows ride along
because they were never the problem. Withholding is about the prose, and the
table is a plain rendering of the data the prose failed to describe.

The failing check stays on the report on purpose. It is the evidence for the
fallback, it grades the serve `unverified`, and that grade is what routes it
into the review queue. Re-grading the code-written frame as clean would bury
the incident. A degraded-but-true answer is an outcome; an invented figure is
not.

## The lens-less door declines too

A caller can also just ask, without naming a lens. The router either routes
the question to a lens the caller may use, or declines — with the reason and
the nearest miss. A cosine score at or above 0.95 routes outright. Below it,
an LLM decider rules when one is configured; with no model, the pure-cosine
arm decides on a floor of 0.78 plus a 0.07 margin over the runner-up. All
three numbers are dst-owned, never a per-org knob, and there is deliberately
no ungoverned full-warehouse catch-all behind a decline. With no embedding
provider configured at all, a built-in fallback matches roughly on word
overlap, so the cold-start path *declines* rather than guesses.

Declines are not dead ends: they persist, cluster into named coverage gaps,
and become the to-do list for the next lens (`GET /mgmt/surface`).
