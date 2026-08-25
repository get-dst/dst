"""The certified test suite: generation vs certification.

For each ACTIVE certified answer of a lens, its stored SQL is the oracle:
execute it read-only + row-capped, run the QUESTION through the real generation
pipeline, execute what generation produced, and compare the two EXECUTED
results. Generation inputs come from the SAME assembly seam serving uses
(`services/runtime/assembly.py`): per-question retrieval,
profile enrichment, exemplar folding, generator tiering — so the
suite grades the pipeline production runs, not a leaner one. The serve bypass
stays off by construction (testing it would trivially pass), and the answer
under test is excluded from its own exemplars (it would match itself at ~1.0
and the suite would grade copying).

The comparison semantics on fetched rows (inherited from the removed value-case
regression scorer, which this suite superseded):
EXCEPT-style set diff, column-order-insensitive when both sides name the same
columns, and the single-scalar float tolerance (``runner._SCALAR_RTOL``). A
diverging case re-runs generation once before counting (nondeterminism
damping); flapping that survives the re-run is signal.
``_ROW_CAP`` bounds both executions — the suite compares within that window,
which certified answers (small, verified aggregates) fit by construction.

Pure orchestration over injected connector/harness/composer, testable offline
with fake harnesses. Production callers — the publish gate,
``dst test``, and the standing scheduler cycle — build the real harness
with ``assembly.eval_harness(bundle, org_id, llm)``.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from services.certify import binding as certify_binding
from services.certify.generate import _value_summary
from services.certify.store import CertifiedAnswer, is_active
from services.contracts.protocols import Connector, QueryGenerator
from services.evals.runner import _SCALAR_RTOL
from services.runtime.answer import AnswerComposer
from services.runtime.assembly import AssembledInputs
from services.runtime.pipeline import _jsonable, run_query

# The comparison window: certified answers are small verified results; anything
# larger diverges honestly at the cap instead of silently sampling.
_ROW_CAP = 1000


def _render_template_case(a: CertifiedAnswer, dialect: str) -> tuple[str, str] | None:
    """(oracle_sql, question) from the first sample binding, or None
    when the stored template no longer validates (fails its case, loudly)."""
    specs, spec_errors = certify_binding.parse_slots(a.slots)
    if spec_errors or not a.sample_bindings:
        return None
    canonical, errors = certify_binding.validate_binding(specs, a.sample_bindings[0])
    if errors:
        return None
    return (
        certify_binding.render_sql(a.sql, specs, canonical, dialect),
        certify_binding.render_question(a.question, dict(canonical)),
    )


@dataclass
class CertifiedCaseResult:
    answer_id: str
    question: str
    oracle_result: dict[str, Any]  # the certified SQL's executed value (summary shape)
    generated_result: dict[str, Any]  # generation's executed value, or {"error": …}
    passed: bool
    reason: str | None = None
    generated_sql: str | None = None
    # What the seam actually assembled (chunk/exemplar/profile counts) — a
    # leaner-than-production run (e.g. no embedder) is visible, never silent.
    assembly: dict[str, int] = field(default_factory=dict)
    # Wall-clock for this case (generation + both executions), for the ledger.
    elapsed_s: float | None = None


@dataclass
class CertifiedSuiteOutcome:
    lens: str
    results: list[CertifiedCaseResult]

    @property
    def score(self) -> float | None:
        if not self.results:
            return None
        return sum(1 for r in self.results if r.passed) / len(self.results)


def format_result(summary: dict[str, Any]) -> str:
    """One-line rendering for divergence messages and CLI output: a scalar shows
    bare (``certified SQL → 19``), an error names itself, a table dumps compact
    JSON — both sides of a divergence must be visible, never elided."""
    if "error" in summary:
        return f"error: {summary['error']}"
    if set(summary) == {"value"}:
        return str(summary["value"])
    return json.dumps(summary, separators=(",", ":"), default=str)


def _key(row: list[Any]) -> tuple[Any, ...]:
    """A hashable row key; exotic cells (structs/arrays) compare by repr."""
    return tuple(v if isinstance(v, bool | int | float | str | type(None)) else str(v) for v in row)


def _shape_story(
    oracle_cols: list[str],
    oracle_rows: list[list[Any]],
    gen_cols: list[str],
    gen_rows: list[list[Any]],
) -> str | None:
    """Plain words when exactly one side is a single value: "certified SQL
    returns a count (19); generation returned 99 row(s)" — the probe read
    "0 missing / 99 extra row(s)" as cryptic when the real story was a scalar
    oracle against a row-shaped generation. None when both sides share a shape
    (the set-diff/scalar messages already say it plainly)."""

    def _single(rows: list[list[Any]]) -> str:
        v = rows[0][0]
        kind = "a count" if isinstance(v, int) and not isinstance(v, bool) else "a single value"
        return f"{kind} ({v})"

    o_single = len(oracle_rows) == 1 and len(oracle_cols) == 1
    g_single = len(gen_rows) == 1 and len(gen_cols) == 1
    if o_single == g_single:
        return None
    if o_single:
        return f"certified SQL returns {_single(oracle_rows)}; generation returned " + (
            f"{len(gen_rows)} row(s)"
        )
    return f"certified SQL returns {len(oracle_rows)} row(s); generation returned " + (
        _single(gen_rows)
    )


def _compare(
    oracle_cols: list[str],
    oracle_rows: list[list[Any]],
    gen_cols: list[str],
    gen_rows: list[list[Any]],
) -> tuple[bool, str | None]:
    """Executed-result equality: set diff, column-order-insensitive, scalar-tolerant."""
    if len(oracle_cols) != len(gen_cols):
        story = _shape_story(oracle_cols, oracle_rows, gen_cols, gen_rows)
        return False, story or (
            f"result shape differs: generated columns {[c.lower() for c in gen_cols]} "
            f"vs certified {[c.lower() for c in oracle_cols]}"
        )
    # Single-scalar float tolerance — aggregation order alone can wiggle a
    # DOUBLE's last bits; the tolerance constant is the runner's, not a copy.
    if len(oracle_cols) == 1 and len(oracle_rows) == 1 and len(gen_rows) == 1:
        want, got = oracle_rows[0][0], gen_rows[0][0]
        numeric = (
            isinstance(want, int | float)
            and isinstance(got, int | float)
            and not isinstance(want, bool)
            and not isinstance(got, bool)
        )
        if numeric:
            if abs(got - want) <= _SCALAR_RTOL * max(abs(want), 1):
                return True, None
            return False, f"generated {got!r} vs certified {want!r}"
    # Column-order-insensitive when both sides name the same columns.
    oc, gc = [c.lower() for c in oracle_cols], [c.lower() for c in gen_cols]
    if oc != gc and sorted(oc) == sorted(gc) and len(set(gc)) == len(gc):
        order = [gc.index(c) for c in oc]
        gen_rows = [[r[i] for i in order] for r in gen_rows]
    want_set, got_set = {_key(r) for r in oracle_rows}, {_key(r) for r in gen_rows}
    missing, extra = len(want_set - got_set), len(got_set - want_set)
    if missing or extra:
        # Count-vs-cardinality bridge: a "how many"
        # oracle is a 1×1 integer K, and generation often row-shapes the same
        # question — the served answer then grounds on row count (a documented
        # groundable class in faithfulness), so cardinality == K passes,
        # VISIBLY (the reason records it). K >= 2 only: K==1 would bless any
        # single-row result and K==0 any empty one.
        scalar = oracle_rows[0][0] if len(oracle_rows) == 1 and len(oracle_cols) == 1 else None
        if (
            isinstance(scalar, int)
            and not isinstance(scalar, bool)
            and scalar >= 2
            and len(gen_rows) == scalar
        ):
            return True, (
                f"shape-lenient: certified count {scalar} matched by generated "
                "row cardinality (row-shaped result)"
            )
        story = _shape_story(oracle_cols, oracle_rows, gen_cols, gen_rows)
        return False, story or f"{missing} missing / {extra} extra row(s) vs certified"
    return True, None


def run_certified_suite(
    *,
    connector: Connector,
    lens: str,
    answers: list[CertifiedAnswer],
    assemble_for: Callable[[CertifiedAnswer], AssembledInputs],
    generators_for: Callable[[AssembledInputs], tuple[QueryGenerator, QueryGenerator | None]],
    composer: AnswerComposer,
    model_name: str = "claude-sonnet-4-6",
    deadline: float | None = None,
) -> CertifiedSuiteOutcome:
    """Oracle-vs-generation over *answers* (retired ones are skipped here too —
    never tested, whatever a caller passes). Both sides execute back-to-back
    against the live warehouse, so there is no snapshot to drift from: the
    oracle IS the certified SQL run now.

    *deadline* (a ``time.monotonic()`` stamp) stops the sweep BETWEEN cases:
    every case is a full generation, so a caller running inside a request that
    holds a lock — apply's certify self-test — must be able to bound the whole
    sweep, and report the answers it never reached. Cases already started always
    finish. None (the default) runs every answer: `dst test` and the standing
    cadence own their own wall clock and must sweep the whole corpus."""
    results: list[CertifiedCaseResult] = []
    # Per-case wall clock, stamped at iteration boundaries so every exit path
    # (including the early-continue failure results) gets one — the ledger
    # `dst test` prints carries it. Exactly ONE clock read per iteration,
    # shared with the deadline check: tests drive the budget through
    # a scripted clock, and an extra read per case would consume its script.
    case_started: float | None = None
    stamped = 0

    def _stamp(now: float) -> None:
        nonlocal stamped
        if case_started is not None and len(results) > stamped:
            delta = now - case_started
            if math.isfinite(delta):  # scripted clocks jump ±inf — never store that
                results[-1].elapsed_s = round(delta, 3)
        stamped = len(results)

    for a in answers:
        now = time.monotonic()
        _stamp(now)
        case_started = now
        if deadline is not None and now >= deadline:
            break
        if not is_active(a.status):
            continue
        assembled = assemble_for(a)
        oracle_sql, case_question = a.sql, a.question
        if not a.slots and certify_binding.placeholders_in(a.sql):
            # Belt-and-suspenders: template-shaped SQL with no
            # slots on the row must NEVER execute raw against the warehouse —
            # fail the case with the state named instead of a parser error.
            results.append(
                CertifiedCaseResult(
                    answer_id=a.id,
                    question=a.question,
                    oracle_result={"error": "template SQL but no slots stored"},
                    generated_result={},
                    passed=False,
                    reason=(
                        "the stored SQL carries {placeholders} but the row has no "
                        "slots — re-apply the template entry (or re-certify); raw "
                        f"template SQL is never executed (stored slots={a.slots!r}, "
                        f"sample_bindings={a.sample_bindings!r})"
                    ),
                )
            )
            continue
        if a.slots:
            # A template's executable witness is its first sample
            # binding — the oracle is the sample-bound SQL, the question under
            # test is the sample-bound question (concrete, like real traffic).
            rendered = _render_template_case(a, assembled.model.dialect)
            if rendered is None:
                results.append(
                    CertifiedCaseResult(
                        answer_id=a.id,
                        question=a.question,
                        oracle_result={"error": "template invalid"},
                        generated_result={},
                        passed=False,
                        reason="template slots/sample_bindings failed validation — re-certify",
                    )
                )
                continue
            oracle_sql, case_question = rendered
        try:
            oracle = connector.execute(oracle_sql, read_only=True, row_limit=_ROW_CAP)
        except Exception as exc:  # noqa: BLE001 — a broken oracle fails its case, not the run
            results.append(
                CertifiedCaseResult(
                    answer_id=a.id,
                    question=a.question,
                    oracle_result={"error": str(exc)},
                    generated_result={},
                    passed=False,
                    reason=f"certified SQL failed to execute: {exc}",
                )
            )
            continue
        oracle_rows = [[_jsonable(v) for v in row] for row in oracle.rows]
        oracle_summary = _value_summary(oracle.columns, oracle_rows)
        generator, escalate_generator = generators_for(assembled)
        passed, reason = False, None
        gen_summary: dict[str, Any] = {}
        gen_sql: str | None = None
        for _attempt in range(2):  # a diverging case re-runs once before counting
            pr = run_query(
                question=case_question,
                lens_name=lens,
                org_id="certified-suite",
                caller="certified-suite",
                semantic_model=assembled.model,
                value_domains=assembled.value_domains,
                connector=connector,
                generator=generator,
                composer=composer,
                escalate_generator=escalate_generator,
                prose_context=assembled.prose,
                max_rows=_ROW_CAP,
                model_name=model_name,
                data_as_of=assembled.data_as_of,
            )
            t = pr.trace
            gen_sql = t.sql
            if t.status != "ok" or pr.response.data is None:
                reason = t.error or f"generation ended in status '{t.status}'"
                gen_summary, passed = {"error": reason}, False
            else:
                data = pr.response.data
                passed, reason = _compare(oracle.columns, oracle_rows, data.columns, data.rows)
                gen_summary = _value_summary(data.columns, data.rows)
            if passed:
                break
        results.append(
            CertifiedCaseResult(
                answer_id=a.id,
                question=case_question,
                oracle_result=oracle_summary,
                generated_result=gen_summary,
                passed=passed,
                reason=reason,
                generated_sql=gen_sql,
                assembly=dict(assembled.counts),
            )
        )
    if len(results) > stamped:  # guarded: no clock read unless something needs it
        _stamp(time.monotonic())
    return CertifiedSuiteOutcome(lens=lens, results=results)
