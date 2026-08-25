# Evaluation

There is no separate test suite to write. Every active
[certified answer](../concepts/certified.md) **is** a regression test: it pins a
correct value, so certifying an answer and writing its test are the same act. The
suite runs on demand (`dst test`), as a gate inside `dst apply`, and on
whatever schedule your CI or cron gives it.

## The corpus is the suite

For each active certified answer, the stored SQL is the oracle — the
known-good reference the test compares against:

1. Execute the certified SQL, read-only, capped at 1000 rows — and compare
   over at most 1000 rows from either side. Certified answers are small
   verified aggregates, so they fit by construction.
2. Ask the question again through the **real generation path**. Certified
   matching is switched off by construction (testing the serve shortcut would
   trivially pass), and the answer under test is excluded from its own
   examples — with them, it would match itself almost perfectly and the suite
   would be grading copying.
3. Execute what generation produced and compare the two **executed results**:
   a set difference, ignoring column order when both sides name the same
   columns, with a 1e-9 relative tolerance on single scalars (the order of an
   aggregation alone can wiggle a floating-point number's last bits). One
   deliberate leniency: when the oracle is a single count of 2 or more, a
   generated result carrying that many rows passes — "how many X" and "which
   X" are the same answer at two grains.

Both sides run back-to-back against the live warehouse, so there is no
snapshot to drift from: the oracle is the certified SQL run *now*. A diverging
case re-runs generation once before it counts, to absorb model randomness;
flapping that survives the re-run is signal. A divergence prints both executed
values and the choice it forces: *fix the definition, or re-certify/retire the
answer*. A green answer in `dst test` or the apply gate is a
**re-verification**: its bindings — the recorded hashes of the shared assets it
depends on — re-stamp to the current versions.

Generation inputs come from the same assembly seam serving uses (retrieval,
profile enrichment, exemplar folding, generator tiering), so the suite grades
the pipeline production actually runs. Each result records what
was assembled, so a leaner-than-production run (say, no embedding provider
configured) is visible, never silent.

## Running it

```bash
dst test            # every published lens
dst test finance    # one lens
dst test --all      # explicit form of the default
```

Runs in-process against the configured database, like `migrate`: no server,
token, or URL needed. Always the **full** active corpus (the apply gate scopes
to what a push touches; this command is the cron/CI sweep). Exit `1` on any
divergence or failed expectation.

[![dst test before anything is certified: 0/0 passed, exit 4 — nothing was verified, so the run could not have failed](../assets/term/test.svg)](../assets/term/test.svg)

![dst test with a certified corpus: three green PASS rows, 3/3 passed](../assets/term/test2.svg)

Every run is recorded: `dst test` persists the run and its per-case results in
the same tables the publish gate writes, so accuracy is a queryable trend.
The cadence is yours — run the sweep from CI, cron, or a deploy pipeline
(see [Environments and CI](environments-and-ci.md)); there is no in-process
scheduler to configure.

## Behavioral cases: pinning shape

`lenses/<name>/evals/cases.yaml` holds the behavioral cases: cases that pin the
*shape* of a response, not its value. A case declares
`expect: clarify | refuse | answer`, plus an optional `term` — the term a
clarify must name, ignored on the other two shapes.
Each case runs through the real pipeline — including the deterministic clarify
and exclusion pre-checks, since that is the behavior being pinned — and passes
only if the response comes back in the expected shape: a clarification, a
refusal with no data served, or a data answer. Shape is the one thing a value
test can't express, and `expect: answer` measures refusal in both directions:
a lens that drifts into refusing an answerable question was previously
invisible unless that question was certified. Approved behavioral cases run
alongside the certified suite in `dst test` and in the gate. (Legacy value
cases with expected SQL are **not scored anywhere**: certified answers are the
regression suite; `dst evals migrate` converts them into certified answers.)

## The gate on publish and apply

`eval_gate: off | warn | block` per lens, default `block`: a certified answer
gates by default, the way a dbt test blocks by default. A fresh lens with
nothing certified publishes with a loud "gate SKIPPED" line, never a refusal.
One shared check runs inside interactive publish *and* inside `dst apply`;
apply cannot bypass it. A blocked publish returns `409` with the score, the
previous score, and the failing cases. Under `warn`, the same findings surface
loudly and publish proceeds.

[![dst apply blocked by the eval gate: the certified answer diverged under a changed definition, accuracy regressed, nothing deployed](../assets/term/apply_blocked.svg)](../assets/term/apply_blocked.svg)

On apply, staleness picks what to test: only active answers whose stored
bindings disagree with the push's asset hashes run, which keeps it cheap by
construction. A certified divergence under `block` is an error in its own
right, not merely a score change. And the abort is blue/green and atomic: the
whole apply rolls back, the prior versions keep serving, and the staged eval
run and binding re-stamps roll back with it. A rejected apply can never lower
the gate's baseline for the next one.

The answers a push itself lands get fresh bindings, so the staleness selection
never picks them. Instead, certifying tests itself: an answer the apply
**creates or SQL-re-authors** runs through the same suite in the same apply,
generation against the just-stored oracle. A divergence here is an *alert,
never a block*: divergence at certification time can be the point (the
certified answer overrides generation), so the warning names both executed
results and asks you to re-check the oracle if override wasn't the intent.
The self-test is **unconditional**: `eval_gate` governs the publish gate,
never this; applying a certification tests it and alerts, period. Edits that
touch only provenance or status verify nothing new and are not re-tested.

Two things stop it reaching an answer, and both say so out loud with the
*landed untested* nudge toward `dst test`: generation cannot serve the lens at
all (the lens's model does not resolve, no connector), or the apply runs out of
its self-test budget. That budget is wall-clock — `DST_CERTIFY_SELFTEST_BUDGET_S`,
default 120 seconds — because apply holds the org's lock and its transaction open
for the whole run. Read it in answers, not seconds: at roughly 30 seconds per
generation it covers about four, so a larger push lands the remainder untested by
design and `dst test` sweeps them unbounded.

A configured gate that cannot score (no smart-tier model resolves, nothing to
run) stands down **loudly**, with the reason in the publish/apply output, so a
gate that did not run is never mistaken for a gate that passed. One starvation refuses
to stand down at all: under `eval_gate: block`, an apply that empties the
active certified corpus — retiring the **last** active answer, or deleting its
file entry — aborts instead of publishing over an emptied corpus. Keep at
least one active answer, or set `eval_gate: warn`/`off` in lens.yaml and
re-apply. Under `warn`, the same ≥1→0 transition publishes but **warns**, in
the apply row itself: even when approved behavioral cases keep the gate
scoring, the certified gate now has nothing left to test. The scope is exactly
the ≥1→0 transition: a lens that never had active answers (a fresh lens doing
its first applies under `block`) is untouched.

## Scored three ways

The suite on this page is pass/fail per case. Where dst measures answer
quality more broadly — the benchmark harness, and Observe's per-lens
columns — it scores three ways (**correct / wrong / declined**), with
wrong-rate as a co-headline, because a wrong answer is worse than no
answer. Declining is the *correct* response for a question whose data does not
exist; a confident number there is a hallucination by construction.

Definitions and exemplars generalize beyond the questions they were written
for: they ground questions whose SQL is *not* in the certified library.
