"""The slot lane: grade a certified question's typed resolution before any SQL runs.

A certified answer that carries a stored resolution (its typed gold, copied
from the trace at certify time) can be graded in milliseconds: run the typed
resolver on the question, compare the governed names it resolved to against
the stored slots (the same ``resolution.grade`` the rows lane uses), and grade
every slot decision the resolver made against the gold the stored slots imply.
No warehouse, no composer — the cost is the decisions. An answer certified
before the ledger carries no stored gold; its human-approved SQL is attributed
into slots instead (``method: attributed``) and graded the same way, provided
the attribution found a governed measure — an empty gold would pass anything,
so it is skipped WITH ITS REASON (``SlotLaneOutcome.skipped``), never dropped
on the floor: graded plus skipped is every active answer. A templated answer
is graded on its first sample binding, the same rendering the rows lane
executes; one with no valid sample binding is skipped and says so.

``repeat`` resolves every question N times: the verdict holds only when every
run passes AND every run produced the same typed intent — determinism is the
claim typed serving makes, and here it is measured, not assumed.

The compiled SQL is a deterministic function of the intent, so the lane also
compares it to the certified SQL text: identical (the same query after
sqlglot canonicalisation) or equivalent (the same query once both are run
through sqlglot's optimizer — aliases, predicate order and a subquery wrapper
are spelling, a different literal is not) means the query is proven without a
warehouse; differs means only that the certified text was written another way
(a raw-SQL CTE, a CASE) — the rows lane on apply is the oracle for those until
they earn typed gold.

Pure over an injected resolver; `dst test --slots` builds it from the install's
decider and the assembly seam.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import sqlglot
from sqlglot import exp
from sqlglot.optimizer import optimize

from services.certify.store import CertifiedAnswer, is_active
from services.contracts.eval import EvalResult
from services.contracts.protocols import spent_by
from services.contracts.resolution import MEASURE_KINDS, DecisionRecord, Resolution
from services.contracts.semantic_model import SemanticModel
from services.evals.calibration import GradedDecision
from services.runtime import resolution as ledger
from services.runtime.compiler import CompileError, compile_intent
from services.runtime.typed_resolver import TypedResolution

GOVERNED = {"declared", "certified"}

# identical: the same query text once canonicalised; equivalent: the same
# query once both sides are optimised (aliases, predicate order, a wrapping
# subquery are spelling); differs: written another way, the rows lane decides.
SqlMatch = Literal["identical", "equivalent", "differs"]


@dataclass
class SlotCase:
    answer_id: str
    question: str
    verdict: str  # passed | failed | skipped
    evidence: str
    typed: bool
    decisions: list[DecisionRecord] = field(default_factory=list)
    graded: list[GradedDecision] = field(default_factory=list)
    gold: str = "stored"  # stored | attributed
    runs: int = 1
    agreement: int = 1  # runs whose typed intent equals the first run's
    # Compiled SQL vs the certified SQL text: identical or equivalent = proven
    # (no warehouse needed), differs = written another way, None = no compiled
    # SQL (the question did not type).
    sql_match: SqlMatch | None = None
    # Set by the caller once the rows lane executed a ``differs`` case: True
    # when its rows matched the certified oracle — the data lane is the oracle,
    # so a compiled SQL written another way that returns the certified rows is
    # "differs (data equal)", never a lane failure. None when not executed.
    data_equal: bool | None = None
    # What the first run generated: the typed intent (JSON) and the SQL it
    # compiled to — the slot lane's actual value, recorded on eval_result.
    intent: str | None = None
    compiled_sql: str | None = None
    # Per resolution (mean over runs): wall seconds, provider calls, tokens,
    # and USD from the provider's price (None when unpriced).
    elapsed_s: float = 0.0
    calls: float = 0.0
    input_tokens: float = 0.0
    output_tokens: float = 0.0
    cost_usd: float | None = None


@dataclass(frozen=True)
class SkippedAnswer:
    """An active certified answer the lane did not grade, and why."""

    answer_id: str
    question: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"answer_id": self.answer_id, "question": self.question, "reason": self.reason}


@dataclass
class SlotLaneOutcome:
    cases: list[SlotCase]
    # Every active answer not in ``cases``, each with the reason — the count a
    # reader adds to the graded cases to get back to the corpus.
    skipped: list[SkippedAnswer] = field(default_factory=list)
    attributed_gold: int = 0
    elapsed_s: float = 0.0
    workers: int = 1
    repeat: int = 1
    # Over every resolution the lane ran: USD (None when any was unpriced)
    # and the per-resolution latencies, for a median and a p90 per arm.
    cost_usd: float | None = None
    latencies_s: list[float] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.verdict == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for c in self.cases if c.verdict == "failed")

    @property
    def skipped_no_gold(self) -> int:
        return sum(1 for s in self.skipped if s.reason.startswith(NO_GOLD))

    @property
    def skipped_templates(self) -> int:
        return sum(1 for s in self.skipped if s.reason.startswith(TEMPLATED))


NO_GOLD = "no gold"
TEMPLATED = "templated"


def _gold_names(expected: Resolution, kind: str) -> set[str]:
    return {s.name for s in expected.slots if s.kind == kind and s.source in GOVERNED}


def grade_decisions(
    records: list[DecisionRecord], expected: Resolution, question: str | None = None
) -> list[GradedDecision]:
    """Each slot decision against the gold the stored slots imply.

    ``shape``: a figure (aggregate or ranking) iff the gold carries a measure slot. ``metric`` /
    ``dimension`` / ``grain``: a pick is right when the gold carries that name;
    a ``none`` is right when every gold name of that kind was already picked
    (tracked in order). Filters and definitions are not graded here — their
    gold is a value, which the stored slots carry only as text.
    """
    out: list[GradedDecision] = []
    has_measure = any(s.kind in MEASURE_KINDS and s.source in GOVERNED for s in expected.slots)
    remaining = {
        "metric": _gold_names(expected, "metric"),
        "dimension": _gold_names(expected, "dimension"),
    }
    grain_gold = next(
        (s.value for s in expected.slots if s.kind == "grain" and s.source in GOVERNED), None
    )
    for r in records:
        gold: str | None
        if r.slot == "shape":
            # The stored slots carry no order, so a ranking and a plain aggregate
            # grade as one family: a figure was asked for, or a listing was.
            figure = "ranking" if r.chosen == "ranking" else "aggregate"
            gold = figure if has_measure else "listing"
        elif r.slot in ("metric", "dimension"):
            pool = remaining[r.slot]
            if r.chosen is None:
                gold = None if not pool else next(iter(sorted(pool)))
            else:
                first = next(iter(sorted(pool))) if pool else None
                gold = r.chosen if r.chosen in pool else first
                pool.discard(r.chosen)
        elif r.slot == "grain":
            gold = grain_gold
        else:
            continue
        out.append(
            GradedDecision(
                decider=r.provider,
                slot=r.slot,
                chosen=r.chosen,
                gold=gold,
                p=r.p,
                margin=r.margin,
                question=question,
            )
        )
    return out


def canonical_sql(sql: str, dialect: str) -> str:
    try:
        return sqlglot.parse_one(sql, read=dialect).sql(dialect=dialect).lower()
    except Exception:  # noqa: BLE001 — unparseable SQL canonicalises to itself
        return " ".join(sql.split()).lower()


def _distinct_aliases(tree: exp.Expr) -> exp.Expr:
    """Every table read without an alias gets one of its own, scope by scope,
    with the columns that name it moved along: ``FROM t`` in a query and
    ``FROM t`` in its filter subquery are two readings of ``t``, and the
    optimizer would otherwise qualify both as ``t`` and fold them into one
    name where a compiled ``t`` / ``t_latest`` pair keeps two."""
    from sqlglot.optimizer.scope import traverse_scope

    n = 0
    for scope in traverse_scope(tree):
        for name, source in list(scope.sources.items()):
            if isinstance(source, exp.Table) and not source.alias:
                fresh = f"_s{n}"
                n += 1
                source.set("alias", exp.TableAlias(this=exp.to_identifier(fresh)))
                for col in scope.columns:
                    if col.table == name:
                        col.set("table", exp.to_identifier(fresh))
        # A column read in a scope with ONE source is that source's. Spelled
        # out here, not left to the optimizer: sqlglot 30.8 inferred it with
        # no schema, 30.24 refuses an unqualified column without one, and every
        # hand-written certified SQL was then unprovable.
        if len(scope.sources) == 1:
            (only,) = scope.sources.values()
            if isinstance(only, exp.Table) and only.alias:
                selected = set(getattr(scope.expression, "named_selects", ()))
                for col in scope.columns:
                    if not col.table and not (
                        col.name in selected and col.find_ancestor(exp.Order, exp.Having)
                    ):
                        col.set("table", exp.to_identifier(only.alias))
    return tree


def _equivalence_form(sql: str, dialect: str) -> str | None:
    """The query as sqlglot's optimizer writes it (columns qualified, predicates
    normalised, mergeable subqueries merged, a scalar subquery decorrelated
    into a join), with table aliases renamed in order of appearance so
    ``orders o`` and ``orders AS orders`` read the same, and the output
    columns renamed by position: an alias labels a value and never changes it,
    so ``SUM(arr) AS arr`` and ``SUM(arr) AS total_arr`` are one query. Literals
    survive untouched: a different filter value stays a different query. None
    when the text does not parse or optimise (no schema is given, so an
    unqualified column two tables could own is one such case)."""
    try:
        parsed = _distinct_aliases(_unwrapped(sqlglot.parse_one(sql, read=dialect)))
        tree = optimize(parsed, dialect=dialect)
    except Exception:  # noqa: BLE001 — not provable this way; the plain compare decides
        return None
    aliases: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        if table.alias and table.alias not in aliases:
            aliases[table.alias] = f"_t{len(aliases)}"

    def rename(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Table) and node.alias in aliases:
            node.set("alias", exp.TableAlias(this=exp.to_identifier(aliases[node.alias])))
        elif isinstance(node, exp.Column) and node.table in aliases:
            node.set("table", exp.to_identifier(aliases[node.table]))
        return node

    def ordered(node: exp.Expression) -> exp.Expression:
        # ``a = b`` is ``b = a``: the decorrelated join writes the operands in
        # whichever order the subquery stood.
        if isinstance(node, exp.EQ) and node.left.sql() > node.right.sql():
            return exp.EQ(this=node.right.copy(), expression=node.left.copy())
        return node

    tree = tree.transform(rename).transform(ordered)
    if isinstance(tree, exp.Select):
        for i, proj in enumerate(tree.selects):
            if isinstance(proj, exp.Alias):
                proj.set("alias", exp.to_identifier(f"_c{i}"))
    return tree.sql(dialect=dialect).lower()


def _unwrapped(tree: exp.Expr) -> exp.Expr:
    """``SELECT * FROM (q) AS x`` (or the same columns q projects, in q's order,
    unrenamed) with no other clause IS q. The optimizer keeps a derived table
    that groups, so a hand-written wrapper is peeled here, by shape alone."""
    while isinstance(tree, exp.Select):
        source = tree.args.get("from_")
        inner = source.this if source is not None else None
        if not isinstance(inner, exp.Subquery) or not isinstance(inner.this, exp.Select):
            break
        if any(
            tree.args.get(k)
            for k in ("where", "group", "having", "order", "limit", "joins", "distinct", "with")
        ):
            break
        star = len(tree.selects) == 1 and isinstance(tree.selects[0], exp.Star)
        plain = all(isinstance(c, exp.Column) and not c.table for c in tree.selects)
        if not star and not (plain and [c.name for c in tree.selects] == inner.this.named_selects):
            break
        tree = inner.this
    return tree


def sql_match(compiled: str, certified: str, dialect: str) -> SqlMatch:
    """How the compiled SQL relates to the certified text — see ``SqlMatch``.
    Both proofs are syntactic: nothing here runs, and a text the optimizer
    cannot settle falls back to the canonical compare, never to a guess."""
    if canonical_sql(compiled, dialect) == canonical_sql(certified, dialect):
        return "identical"
    a, b = _equivalence_form(compiled, dialect), _equivalence_form(certified, dialect)
    if a is not None and a == b:
        return "equivalent"
    return "differs"


class EmissionResolver:
    """The lane's view of the one-shot intent emission (typed serving off):
    the generator's QueryIntent graded like a typed one, one ``intent`` record
    carrying the call's tokens and latency. Not a decider — there is nothing
    per slot to grade — but the same accuracy, latency and cost columns, so
    the emission is an arm beside the typed ones."""

    def __init__(
        self,
        generator: Any,
        prose: list[Any],
        *,
        domains: dict[str, list[str]] | None = None,
    ) -> None:
        self._generator = generator
        self._prose = prose
        self._domains = domains or {}
        self.provider = f"json:{getattr(generator, 'model', 'unknown')}"

    def resolve(self, question: str, model: SemanticModel) -> TypedResolution:
        started = time.perf_counter()
        out = self._generator.generate(
            question=question,
            semantic_model=model,
            prose_context=self._prose,
            dialect=model.dialect,
        )
        usage = {
            "calls": 1.0,
            "input_tokens": float(out.input_tokens or 0),
            "output_tokens": float(out.output_tokens or 0),
            "latency_s": round(time.perf_counter() - started, 4),
        }
        record = DecisionRecord(
            slot="intent",
            chosen="emitted" if out.intent is not None else None,
            verdict="act" if out.intent is not None else "clarify",
            provider=self.provider,
            usage=usage,
        )
        if out.intent is None:
            return TypedResolution(
                intent=None,
                decisions=[record],
                clarification=out.clarification,
                decline=None if out.clarification else (out.no_answer_reason or "no intent"),
            )
        return TypedResolution(intent=out.intent, decisions=[record])


def gold_for(a: CertifiedAnswer, model: SemanticModel, resolver: Any) -> Resolution | None:
    """The gold a certified answer grades against: its stored typed resolution,
    else its approved SQL attributed into slots — None when neither carries a
    governed measure (nothing to grade; never a vacuous pass)."""
    if a.resolution:
        stored = Resolution.model_validate(a.resolution)
        # A stored CONSTRUCTION is the approved typed reading; a stored
        # attribution is a cache of the attributor, re-read with today's.
        if stored.method == "construction":
            return stored
    # The answer's metric, not every column the SQL reads (see answer_measures).
    attributed = ledger.answer_measures(
        ledger.attribute(a.sql, model, resolver._domains), a.sql, model
    )
    if any(s.kind in MEASURE_KINDS and s.source in GOVERNED for s in attributed.slots):
        return attributed
    return None


def render_sample(a: CertifiedAnswer, dialect: str) -> CertifiedAnswer | None:
    """A templated answer as the concrete case the suite asks: question and
    SQL rendered from the first sample binding (the rows lane's rendering),
    the template keys cleared so the resolver sees a plain question. None when
    no sample binding validates."""
    from services.evals.certified_suite import _render_template_case

    rendered = _render_template_case(a, dialect)
    if rendered is None:
        return None
    sql, question = rendered
    return replace(a, sql=sql, question=question, slots=None, sample_bindings=None)


@dataclass
class _Run:
    verdict: str
    evidence: str
    typed: bool
    decisions: list[DecisionRecord]
    graded: list[GradedDecision]
    intent_key: str | None
    sql_match: SqlMatch | None = None
    elapsed_s: float = 0.0
    compiled_sql: str | None = None
    usage: dict[str, tuple[float, float, float]] = field(
        default_factory=dict
    )  # provider -> (calls, in, out)


def _usage(spent: list[tuple[str, dict[str, float]]]) -> dict[str, tuple[float, float, float]]:
    """``provider -> (calls, input tokens, output tokens)`` summed."""
    usage: dict[str, tuple[float, float, float]] = {}
    for provider, u in spent:
        c, i, o = usage.get(provider, (0.0, 0.0, 0.0))
        usage[provider] = (
            c + u.get("calls", 0.0),
            i + u.get("input_tokens", 0.0),
            o + u.get("output_tokens", 0.0),
        )
    return usage


def _run_once(
    a: CertifiedAnswer,
    model: SemanticModel,
    resolver_for: Callable[[CertifiedAnswer], Any],
    expected: Resolution,
) -> _Run:
    resolver = resolver_for(a)
    started = time.perf_counter()
    try:
        res = resolver.resolve(a.question, model)
    except Exception as exc:  # noqa: BLE001 — a starved decider fails the case, never the lane
        return _Run(
            "failed",
            f"decider unavailable: {type(exc).__name__}: {exc}",
            False,
            [],
            [],
            None,
            elapsed_s=time.perf_counter() - started,
            # What the resolution was billed before the decider failed.
            usage=_usage([(p, u.as_dict()) for p, u in spent_by(exc)]),
        )
    elapsed = time.perf_counter() - started
    # Billed whether or not the resolution used it: a batched call's
    # unused answers cost the same.
    usage = _usage([(r.provider, r.usage) for r in [*res.decisions, *res.unused] if r.usage])
    graded = grade_decisions(res.decisions, expected, a.question)
    if res.intent is None:
        why = res.clarification.question if res.clarification else (res.decline or "did not type")
        return _Run(
            "failed",
            f"did not type: {why}",
            False,
            res.decisions,
            graded,
            None,
            elapsed_s=elapsed,
            usage=usage,
        )
    got = ledger.from_intent(res.intent, model, resolver._domains, supplied=res.supplied)
    verdict, evidence = ledger.grade(expected, got)
    match: SqlMatch | None = None
    try:
        compiled = compile_intent(res.intent, model)
    except CompileError:
        compiled = None
    if compiled is not None and a.sql.strip():
        match = sql_match(compiled, a.sql, model.dialect)
    return _Run(
        verdict,
        evidence,
        True,
        res.decisions,
        graded,
        res.intent.model_dump_json(),
        match,
        elapsed_s=elapsed,
        usage=usage,
        compiled_sql=compiled,
    )


def _fold(a: CertifiedAnswer, runs: list[_Run], gold: str) -> SlotCase:
    first = runs[0]
    agreement = sum(1 for r in runs if r.intent_key == first.intent_key)
    failed = [r for r in runs if r.verdict != "passed"]
    if failed:
        verdict, evidence = "failed", failed[0].evidence
        if len(runs) > 1:
            evidence = f"{len(failed)}/{len(runs)} runs failed: {evidence}"
    elif agreement < len(runs):
        verdict = "failed"
        evidence = (
            f"not deterministic: {agreement}/{len(runs)} runs produced the first run's intent"
        )
    else:
        verdict, evidence = "passed", first.evidence
    return SlotCase(
        answer_id=a.id,
        question=a.question,
        verdict=verdict,
        evidence=evidence,
        typed=all(r.typed for r in runs),
        decisions=first.decisions,
        # Every run's decisions ride the case, each stamped with its repeat:
        # the calibration keeps the first and drops the rest (one measurement
        # per question, however many times it was re-asked).
        graded=[replace(g, repeat=i) for i, r in enumerate(runs) for g in r.graded],
        gold=gold,
        runs=len(runs),
        agreement=agreement,
        # Runs that disagree about the SQL proved nothing: differs, so the rows
        # lane executes the case.
        sql_match=(
            first.sql_match if all(r.sql_match == first.sql_match for r in runs) else "differs"
        ),
        intent=first.intent_key,
        compiled_sql=first.compiled_sql,
        elapsed_s=round(sum(r.elapsed_s for r in runs) / len(runs), 3),
        calls=round(sum(c for r in runs for c, _i, _o in r.usage.values()) / len(runs), 3),
        input_tokens=round(sum(i for r in runs for _c, i, _o in r.usage.values()) / len(runs), 1),
        output_tokens=round(sum(o for r in runs for _c, _i, o in r.usage.values()) / len(runs), 1),
        cost_usd=_mean_cost(runs),
    )


def _mean_cost(runs: list[_Run]) -> float | None:
    """Mean USD per resolution over the runs; None when any provider is unpriced."""
    from services.observability import cost as pricing

    total: float | None = 0.0
    for r in runs:
        by_provider = {p: (i, o) for p, (_c, i, o) in r.usage.items()}
        total = pricing.add_cost(total, pricing.decisions_cost_usd(by_provider))
    return None if total is None else round(total / len(runs), 6)


def run_slot_lane(
    answers: list[CertifiedAnswer],
    model: SemanticModel,
    resolver_for: Callable[[CertifiedAnswer], Any],
    *,
    concurrency: int = 1,
    repeat: int = 1,
) -> SlotLaneOutcome:
    """Every eligible answer graded ``repeat`` times; ``concurrency`` > 1 runs
    the resolutions on a thread pool (each run gets its own resolver from
    ``resolver_for``, so no per-resolution state is shared). Cases land in
    corpus order either way — the report must not depend on which decision
    came back first."""
    started = time.perf_counter()
    eligible: list[tuple[CertifiedAnswer, Resolution, str]] = []
    skipped: list[SkippedAnswer] = []
    attributed = 0
    for a in answers:
        if not is_active(a.status):
            continue
        if a.slots:
            rendered = render_sample(a, model.dialect)
            if rendered is None:
                skipped.append(
                    SkippedAnswer(
                        a.id,
                        a.question,
                        f"{TEMPLATED}: no valid sample binding renders the question and SQL",
                    )
                )
                continue
            a = rendered
        expected = gold_for(a, model, resolver_for(a))
        if expected is None:
            skipped.append(
                SkippedAnswer(
                    a.id,
                    a.question,
                    f"{NO_GOLD}: no stored typed resolution, and the certified SQL "
                    "attributes to no declared metric or definition",
                )
            )
            continue
        if not a.resolution:
            attributed += 1
        eligible.append((a, expected, "stored" if a.resolution else "attributed"))
    repeat = max(1, repeat)
    jobs = [(i, a, expected) for i, (a, expected, _g) in enumerate(eligible) for _ in range(repeat)]
    workers = max(1, min(concurrency, len(jobs)))

    def run(job: tuple[int, CertifiedAnswer, Resolution]) -> _Run:
        _i, a, expected = job
        return _run_once(a, model, resolver_for, expected)

    if workers == 1:
        results = [run(j) for j in jobs]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(run, jobs))
    by_case: list[list[_Run]] = [[] for _ in eligible]
    for (i, _a, _e), r in zip(jobs, results, strict=True):
        by_case[i].append(r)
    cases = [_fold(a, by_case[i], g) for i, (a, _e, g) in enumerate(eligible)]
    from services.observability import cost as pricing

    lane_cost: float | None = 0.0
    for r in results:
        by_provider = {p: (i, o) for p, (_c, i, o) in r.usage.items()}
        lane_cost = pricing.add_cost(lane_cost, pricing.decisions_cost_usd(by_provider))
    return SlotLaneOutcome(
        cases=cases,
        skipped=skipped,
        attributed_gold=attributed,
        elapsed_s=round(time.perf_counter() - started, 2),
        workers=workers,
        repeat=repeat,
        cost_usd=None if lane_cost is None else round(lane_cost, 6),
        latencies_s=[round(r.elapsed_s, 3) for r in results],
    )


def metric_slot_passed(case: SlotCase) -> bool:
    """Did every metric decision of the case pick its gold? The rows lane runs
    its own generation, so its rows matching the certified oracle say nothing
    about a compiled SQL that read another metric: "differs (data equal)" is
    claimed only for a case whose metric slot is right."""
    metric = [g for g in case.graded if g.slot == "metric"]
    return bool(metric) and all(g.chosen == g.gold for g in metric)


def eval_results(cases: list[SlotCase]) -> list[EvalResult]:
    """The lane's cases as eval results, so a run records them beside the rows
    lane's (``slots:<answer id>``) and a run-over-run compare can see a slot
    verdict flip. ``run_id`` is stamped by the caller that creates the run."""
    return [
        EvalResult(
            run_id="",
            case_id=f"slots:{c.answer_id}",
            question=c.question,
            passed=c.verdict == "passed",
            grade="pass" if c.verdict == "passed" else "fail",
            checks={
                "lane": "slots",
                "typed": c.typed,
                "gold": c.gold,
                "sql_match": c.sql_match,
                **({"sql_data_equal": c.data_equal} if c.data_equal is not None else {}),
                "runs": c.runs,
                "agreement": c.agreement,
            },
            reason=c.evidence or None,
            actual_sql=c.compiled_sql,
            # The lane keeps per-resolution means; the row holds what all
            # of the case's runs cost.
            ai_input_tokens=round(c.input_tokens * c.runs),
            ai_output_tokens=round(c.output_tokens * c.runs),
            ai_cost_usd=None if c.cost_usd is None else round(c.cost_usd * c.runs, 6),
            # The lane's generated value: the typed intent the question resolved
            # to (JSON), or the case's verdict when nothing typed.
            actual_value=c.intent
            if c.intent is not None
            else json.dumps({"typed": False, "evidence": c.evidence}),
        )
        for c in cases
    ]


def summary(lanes: list[tuple[str, SlotLaneOutcome]], *, repeat: int) -> dict[str, Any] | None:
    """The ``slot_lane`` block of ``dst test --json`` over every lens's lane:
    what was resolved, proven, skipped (each with its reason) and what it
    cost. None when no lane ran."""
    if not lanes:
        return None
    latencies = sorted(t for _lens, lane in lanes for t in lane.latencies_s)
    cost: float | None = 0.0
    for _lens, lane in lanes:
        cost = None if cost is None or lane.cost_usd is None else cost + lane.cost_usd
    return {
        "graded": sum(len(lane.cases) for _lens, lane in lanes),
        "resolutions": sum(len(lane.cases) * lane.repeat for _lens, lane in lanes),
        "sql_identical": sum(
            1 for _lens, lane in lanes for c in lane.cases if c.sql_match == "identical"
        ),
        "sql_equivalent": sum(
            1 for _lens, lane in lanes for c in lane.cases if c.sql_match == "equivalent"
        ),
        # Written another way, but the rows lane returned the certified rows:
        # the data lane is the oracle, so these are not failures of anything.
        "sql_differs_data_equal": sum(
            1
            for _lens, lane in lanes
            for c in lane.cases
            if c.sql_match == "differs" and c.data_equal is True
        ),
        # Active answers the lane did not grade — graded + skipped is the corpus.
        "skipped": [{"lens": lens, **sk.as_dict()} for lens, lane in lanes for sk in lane.skipped],
        "elapsed_s": round(sum(lane.elapsed_s for _lens, lane in lanes), 1),
        "cost_usd": None if cost is None else round(cost, 6),
        "latency_s": (
            {
                "median": latencies[len(latencies) // 2],
                "p90": latencies[min(len(latencies) - 1, int(len(latencies) * 0.9))],
            }
            if latencies
            else None
        ),
        "workers": max(lane.workers for _lens, lane in lanes),
        "repeat": repeat,
    }
