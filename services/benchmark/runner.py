"""Run lanes × questions, grade against the oracle, emit the report."""

from __future__ import annotations

import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from math import comb
from pathlib import Path
from typing import Protocol

from .grading import (
    STAGE_OWNERS,
    STAGES,
    Grade,
    answer_signature,
    first_failed_stage,
    grade,
    grade_absent,
    prose_signature,
    stage_statuses,
)
from .lanes import LaneAnswer
from .questions import Question, resolve_oracle


class Lane(Protocol):
    name: str

    def answer(self, question: Question) -> LaneAnswer: ...


@dataclass(frozen=True)
class QuestionResult:
    question_id: str
    category: str
    lane: str
    correct: bool
    reason: str
    sql: str | None
    error: str | None
    rows_sample: list[list[str]] | None = None  # first rows, stringified — for diagnosis
    tier: str = "calibration"
    # correct | wrong | declined. A decline (no answer delivered) is not a
    # win, but it is categorically better than a confident wrong number —
    # wrong-rate is the co-headline metric, to be minimized.
    outcome: str = "wrong"
    # The HAL four-tuple (arXiv 2407.01502): tokens, dollars, wall-clock, trace.
    ai_cost_usd: float = 0.0
    latency_ms: float = 0.0
    tokens: int = 0
    trace_id: str | None = None
    calls: int = 1
    # Agentic lanes: the loop transcript (model turns + tool observations) —
    # a declined agent run must be explainable from results.json alone.
    transcript: str | None = None
    confidence: str | None = None  # the pipeline's trust label, for gate pricing
    certification: str | None = None  # certified | none — was approved SQL served
    # --stale-days runs only: did the answer disclose the staleness (a failed
    # `freshness` check)? None = freshness was not under test.
    stale_disclosed: bool | None = None
    # Agentic lanes: replies the harness could not parse as a protocol step.
    # A first-class metric, because a run that folds this noise into the score
    # reads a formatting failure as task difficulty.
    protocol_failures: int = 0
    free_calls: int = 0  # zero-cost tool calls (describe) — free of the call budget
    # Normalized fingerprint of the delivered answer — the unit of the
    # per-question agreement metric under --repeat.
    answer_signature: str | None = None
    # Stage attribution. `stages` is each stage's verdict
    # (passed | failed | skipped); `wrong_at` is the FIRST failed stage when
    # the outcome is wrong (or "unattributed", counted, never dropped);
    # `stage_evidence` is the one-liner of what that stage's grader saw.
    stages: dict[str, str] = field(default_factory=dict)
    wrong_at: str | None = None
    stage_evidence: str = ""
    # The delivered prose and the fingerprint of its numeric claims — the unit
    # of composer determinism ("same SQL, different responses").
    answer: str | None = None
    prose_signature: str | None = None


@dataclass(frozen=True)
class LaneSummary:
    lane: str
    total: int
    correct: int

    @property
    def accuracy(self) -> float:
        return round(self.correct / self.total, 3) if self.total else 0.0

    @property
    def ci95(self) -> tuple[float, float]:
        return wilson_ci(self.correct, self.total)


def wilson_ci(correct: int, total: int, *, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval — honest uncertainty for small n."""
    if total == 0:
        return (0.0, 1.0)
    p = correct / total
    denom = 1 + z**2 / total
    center = (p + z**2 / (2 * total)) / denom
    half = (z / denom) * ((p * (1 - p) / total + z**2 / (4 * total**2)) ** 0.5)
    return (round(max(0.0, center - half), 3), round(min(1.0, center + half), 3))


def paired_vs_first(results: list[QuestionResult]) -> dict[str, tuple[int, int]]:
    """Per lane: (wins, losses) on the same question runs vs the first lane —
    discordant pairs, the honest small-n comparison (McNemar's table). Results
    arrive lane-by-lane in identical question order, so the i-th run of a lane
    pairs with the i-th run of the base lane."""
    by_lane: dict[str, list[bool]] = {}
    for r in results:
        by_lane.setdefault(r.lane, []).append(r.correct)
    lanes = list(by_lane)  # insertion order: the first lane run is the base
    if len(lanes) < 2 or len({len(v) for v in by_lane.values()}) != 1:
        return {}
    base = by_lane[lanes[0]]
    return {
        lane: (
            sum(1 for got, b in zip(by_lane[lane], base, strict=True) if got and not b),
            sum(1 for got, b in zip(by_lane[lane], base, strict=True) if not got and b),
        )
        for lane in lanes[1:]
    }


def is_silent_wrong(r: QuestionResult) -> bool:
    """Wrong, and carrying NO signal that anything was wrong.

    The headline metric — the only one that separates a governed layer from a
    merely good agent. A raw agent re-probes its way to a correct answer and
    scores fine on accuracy while scoring structurally zero on *detection*: when
    it is wrong, nothing anywhere says so. Signals that make a wrong answer
    non-silent, in the harness's reach: a refusal or decline (the answer was
    never delivered), a clarification, and the pipeline's own confidence label
    when it is below `verified`. A lane that emits no confidence at all — every
    raw-SQL arm — is silent by construction whenever it is wrong.
    """
    if r.outcome != "wrong":
        return False
    return r.confidence not in ("unverified", "partial")


def _runs_by_question(results: list[QuestionResult], lane: str) -> dict[str, list[QuestionResult]]:
    out: dict[str, list[QuestionResult]] = {}
    for r in results:
        if r.lane == lane:
            out.setdefault(r.question_id, []).append(r)
    return out


def pass_hat_k(results: list[QuestionResult], k: int) -> dict[str, float | None]:
    """tau-bench's pass^k: the chance that ALL k of k independent trials of the
    same question succeed. Per question with n trials and c successes the
    unbiased estimate is C(c,k)/C(n,k); averaged over questions.

    k=1 is plain accuracy. k>1 is the metric that punishes an arm which is right
    on average but not the same way twice — reliability is one of the product's
    real claims, and nothing measured it before. ``None`` when no question has
    n >= k runs (i.e. --repeat was too small to say anything).
    """
    out: dict[str, float | None] = {}
    for lane in sorted({r.lane for r in results}):
        scores = []
        for runs in _runs_by_question(results, lane).values():
            n, c = len(runs), sum(1 for r in runs if r.correct)
            if n >= k:
                scores.append(comb(c, k) / comb(n, k))
        out[lane] = round(sum(scores) / len(scores), 3) if scores else None
    return out


def agreement(results: list[QuestionResult]) -> dict[str, float | None]:
    """Per lane: how often the arm gives the SAME answer to the same question.

    Per question, the share of runs landing on the modal answer signature,
    averaged over questions with more than one run. 1.0 = perfectly repeatable
    (right or wrong); 0.5 with 2 runs = it never agreed with itself. Scored on
    the answer, not on correctness, because a caller who reruns a report cares
    that the number does not move.
    """
    out: dict[str, float | None] = {}
    for lane in sorted({r.lane for r in results}):
        shares = []
        for runs in _runs_by_question(results, lane).values():
            if len(runs) < 2:
                continue
            counts = Counter(r.answer_signature or "∅" for r in runs)
            shares.append(counts.most_common(1)[0][1] / len(runs))
        out[lane] = round(sum(shares) / len(shares), 3) if shares else None
    return out


def prose_agreement(results: list[QuestionResult]) -> dict[str, float | None]:
    """Composer determinism: among same-question runs that landed on
    the SAME modal rows signature, the share agreeing on the prose claims.

    ``agreement`` scores the rows; this scores the narration OVER identical
    rows, so a drop here is "same SQL, different responses" — a pure composer
    defect, separated from generation nondeterminism by construction. ``None``
    when no question has two same-rows runs with prose (a run without --repeat,
    or a lane that delivers no prose)."""
    out: dict[str, float | None] = {}
    for lane in sorted({r.lane for r in results}):
        shares = []
        for runs in _runs_by_question(results, lane).values():
            counts = Counter(r.answer_signature or "∅" for r in runs)
            modal = counts.most_common(1)[0][0] if counts else "∅"
            same_rows = [
                r
                for r in runs
                if (r.answer_signature or "∅") == modal and r.prose_signature is not None
            ]
            if len(same_rows) < 2:
                continue
            prose_counts = Counter(r.prose_signature for r in same_rows)
            shares.append(prose_counts.most_common(1)[0][1] / len(same_rows))
        out[lane] = round(sum(shares) / len(shares), 3) if shares else None
    return out


def run_benchmark(
    lanes: list[Lane],
    questions: list[Question],
    oracle: dict[str, object],
    *,
    repeat: int = 1,
    workers: int = 1,
    callers: list[str] | None = None,
) -> list[QuestionResult]:
    """Lanes run in order (paired stats need identical question order per lane);
    within a lane, question runs fan out over ``workers`` threads — LLM calls
    are I/O-bound and every lane opens its own read-only DuckDB connection per
    call, so the parallelism is safe. ``executor.map`` preserves order.

    ``callers`` rotates the asking identity across a question's repeats — pass^k
    is only honest when the caller varies as well as the seed (tau-bench's
    ``agent_metrics.py``); a same-caller reseed measures sampling noise.
    """
    runs = [
        replace(q, caller=callers[i % len(callers)]) if callers else q
        for q in questions
        for i in range(repeat)
    ]
    results: list[QuestionResult] = []
    for lane in lanes:
        print(f"[{lane.name}] {len(runs)} question runs…", file=sys.stderr, flush=True)

        def _one(numbered: tuple[int, Question], lane: Lane = lane) -> QuestionResult:
            i, q = numbered
            expected = resolve_oracle(oracle, q.oracle_path)
            try:
                answer = lane.answer(q)
            except Exception as exc:  # noqa: BLE001 — one dead call must not kill the run
                answer = LaneAnswer(columns=[], rows=[], sql=None, error=f"lane crashed: {exc}")
            if answer.protocol_failures and not answer.calls and not answer.rows:
                # A run that never made a tool call did not *decline* — it never
                # engaged. Under grade_absent's documented leniency it would
                # score a point on every `absent` question, paying an arm for
                # being unable to format JSON. Protocol noise scores nothing.
                g = Grade(False, f"protocol failure: {answer.error}")
                outcome = "declined"
            elif q.kind == "absent":
                g = grade_absent(answer.rows, answer.error)
                outcome = "correct" if g.correct else "wrong"
            elif answer.error and not answer.rows:
                g = Grade(False, f"lane error: {answer.error}")
                outcome = "declined"
            else:
                g = grade(q.kind, expected, answer.columns, answer.rows)
                outcome = "correct" if g.correct else ("declined" if not answer.rows else "wrong")
            stages = stage_statuses(
                rows_correct=g.correct,
                delivered=bool(answer.rows),
                grounding=answer.grounding,
                expected_lens=q.expected_lens,
                served_lens=answer.lens,
            )
            if outcome == "correct" and stages["grounding"] == "failed":
                # Rows matched the oracle but the DELIVERED prose does not state
                # them — production serves the sentence, so this is a wrong
                # answer that graded correct until the harness read the prose.
                outcome = "wrong"
                g = Grade(False, "rows matched oracle; delivered prose failed grounding")
            wrong_at = first_failed_stage(stages, outcome)
            evidence = {
                "routing": f"routed to {answer.lens}, expected {q.expected_lens}",
                "rows": g.reason,
                "grounding": answer.grounding_reason or "prose failed numeric_grounding",
                "unattributed": f"wrong, but no graded stage failed: {g.reason}",
            }
            result = QuestionResult(
                question_id=q.id,
                category=q.category,
                lane=lane.name,
                correct=g.correct,
                reason=g.reason,
                sql=answer.sql,
                error=answer.error,
                rows_sample=[[str(c)[:80] for c in row] for row in answer.rows[:3]] or None,
                tier=q.tier,
                outcome=outcome,
                ai_cost_usd=answer.ai_cost_usd,
                latency_ms=answer.latency_ms,
                tokens=answer.tokens,
                trace_id=answer.trace_id,
                calls=answer.calls,
                transcript=answer.transcript,
                confidence=answer.confidence,
                certification=answer.certification,
                stale_disclosed=answer.stale_disclosed,
                protocol_failures=answer.protocol_failures,
                free_calls=answer.free_calls,
                answer_signature=answer_signature(answer.rows),
                stages=stages,
                wrong_at=wrong_at,
                stage_evidence=evidence.get(wrong_at or "", ""),
                answer=answer.answer,
                prose_signature=prose_signature(answer.answer),
            )
            mark = "✓" if result.correct else f"✗ {result.reason[:60]}"
            print(f"[{lane.name}] {i}/{len(runs)} {q.id}: {mark}", file=sys.stderr, flush=True)
            return result

        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results.extend(pool.map(_one, enumerate(runs, 1)))
        else:
            results.extend(_one(item) for item in enumerate(runs, 1))
    return results


def summarize(results: list[QuestionResult]) -> list[LaneSummary]:
    lanes = sorted({r.lane for r in results})
    return [
        LaneSummary(
            lane=lane,
            total=sum(1 for r in results if r.lane == lane),
            correct=sum(1 for r in results if r.lane == lane and r.correct),
        )
        for lane in lanes
    ]


def by_category(results: list[QuestionResult]) -> dict[str, dict[str, str]]:
    """{category: {lane: 'correct/total'}} for the per-category table."""
    out: dict[str, dict[str, str]] = {}
    for r in results:
        cell = out.setdefault(r.category, {})
        got, total = (cell.get(r.lane) or "0/0").split("/")
        cell[r.lane] = f"{int(got) + int(r.correct)}/{int(total) + 1}"
    return dict(sorted(out.items()))


def _pareto_section(results: list[QuestionResult], lanes: list[str]) -> list[str]:
    """Accuracy against cost, with domination marked — never a bare bar chart.

    "AI Agents That Matter" (arXiv 2407.01502) showed trivial retry baselines
    Pareto-dominating SOTA agents at 50x lower cost, which demolishes any
    accuracy claim reported without its price. A lane is dominated when another
    lane is at least as accurate AND at least as cheap.
    """
    if len(lanes) < 2:
        return []
    stats: dict[str, tuple[float, float]] = {}
    for lane in lanes:
        rs = [r for r in results if r.lane == lane]
        stats[lane] = (
            sum(1 for r in rs if r.correct) / len(rs),
            100 * sum(r.ai_cost_usd for r in rs) / len(rs),
        )
    lines = [
        "",
        "## Accuracy–cost Pareto (HAL four-tuple: tokens, $, wall-clock, trace)",
        "",
        "| Lane | Accuracy | ¢/question | Frontier |",
        "|---|---|---|---|",
    ]
    for lane in lanes:
        acc, cents = stats[lane]
        dominated = any(
            other != lane
            and stats[other][0] >= acc
            and stats[other][1] <= cents
            and (stats[other][0] > acc or stats[other][1] < cents)
            for other in lanes
        )
        lines.append(
            f"| {lane} | {acc:.1%} | {cents:.2f}¢ | "
            + ("dominated" if dominated else "**on the frontier**")
            + " |"
        )
    return lines


def _enforcement_section(results: list[QuestionResult], lanes: list[str]) -> list[str]:
    """Experiment A: compliance with the convention when the caller argues.

    Reported next to the uncontested tier, because the number that matters is
    the DROP: an arm whose conventions live in the same context window the
    caller writes into loses to later text; a governed definition is out-of-band
    from the caller's message. Same conventions on both sides, by construction.
    """
    pressured = [r for r in results if r.tier == "pressure"]
    if not pressured:
        return []
    lines = [
        "",
        "## Enforcement — compliance under caller pressure (experiment A)",
        "",
        "Identical conventions on every arm; the caller then countermands them "
        "in-band. Held = the governed answer survived the argument.",
        "",
        "| Lane | Uncontested | Under pressure | Drop |",
        "|---|---|---|---|",
    ]
    for lane in lanes:
        calm = [r for r in results if r.lane == lane and r.tier != "pressure"]
        hot = [r for r in pressured if r.lane == lane]
        if not hot:
            continue
        hot_rate = sum(1 for r in hot if r.correct) / len(hot)
        calm_rate = sum(1 for r in calm if r.correct) / len(calm) if calm else None
        drop = f"{(hot_rate - calm_rate) * 100:+.0f}pp" if calm_rate is not None else "—"
        calm_cell = f"{calm_rate:.0%}" if calm_rate is not None else "—"
        lines.append(f"| {lane} | {calm_cell} | {hot_rate:.0%} ({len(hot)} runs) | {drop} |")
    return lines


def _consistency_section(results: list[QuestionResult], lanes: list[str]) -> list[str]:
    """pass^k + agreement — only meaningful once a question ran more than once."""
    reps = max(
        (len(v) for lane in lanes for v in _runs_by_question(results, lane).values()), default=1
    )
    if reps < 2:
        return []
    ks = [k for k in (1, 2, 3, 5, 8) if k <= reps]
    agree = agreement(results)
    prose_agree = prose_agreement(results)
    lines = [
        "",
        f"## Consistency ({reps} runs/question)",
        "",
        "pass^k = all k of k trials correct (tau-bench). agreement = share of runs "
        "landing on the modal answer, right or wrong. prose = share of SAME-rows "
        'runs narrated with the same numbers — below 100% is "same SQL, '
        'different responses", a composer-determinism defect.',
        "",
        "| Lane | " + " | ".join(f"pass^{k}" for k in ks) + " | agreement ↑ | prose ↑ |",
        "|---|" + "---|" * (len(ks) + 2),
    ]
    scores = {k: pass_hat_k(results, k) for k in ks}
    for lane in lanes:
        cells = []
        for k in ks:
            score = scores[k].get(lane)
            cells.append(f"{score:.0%}" if score is not None else "—")
        for metric in (agree, prose_agree):
            a = metric.get(lane)
            cells.append(f"{a:.0%}" if a is not None else "—")
        lines.append(f"| {lane} | " + " | ".join(cells) + " |")
    return lines


def _stage_section(results: list[QuestionResult], lanes: list[str]) -> list[str]:
    """Wrong-rate decomposed by the first stage that broke — the
    table that answers "router problem, layer problem, or composer problem"
    from the report alone. `unattributed` always renders: a wrong case the
    classifier could not place is counted loudly, never dropped."""
    wrongs = [r for r in results if r.outcome == "wrong"]
    if not wrongs:
        return []
    columns = [*STAGES, "unattributed"]
    lines = [
        "",
        "## Wrong, by stage (first stage that broke)",
        "",
        *[f"- `{s}` → {STAGE_OWNERS[s]}" for s in columns],
        "",
        "| Lane | wrong | " + " | ".join(columns) + " | routing tested |",
        "|---|" + "---|" * (len(columns) + 2),
    ]
    for lane in lanes:
        lane_rs = [r for r in results if r.lane == lane]
        lane_wrong = [r for r in lane_rs if r.outcome == "wrong"]
        cells = [str(sum(1 for r in lane_wrong if r.wrong_at == s)) for s in columns]
        # `skipped` made visible: a lens-pinned lane never tested routing, and
        # an untested stage must read as untested, not clean.
        tested = sum(1 for r in lane_rs if r.stages.get("routing") != "skipped")
        lines.append(
            f"| {lane} | {len(lane_wrong)} | " + " | ".join(cells) + f" | {tested}/{len(lane_rs)} |"
        )
    return lines


def _freshness_section(results: list[QuestionResult], lanes: list[str]) -> list[str]:
    """Silent-stale accounting for --stale-days runs.

    A DELIVERED answer over a world older than the contract either disclosed
    the staleness (failed `freshness` check → partial + in-band note) or served
    it silently fresh. Non-deliveries are excluded: an answer that never
    delivered has nothing to disclose — and outcome alone cannot say which
    those are, because ``grade_absent`` grades a correct REFUSAL on an `absent`
    question as outcome "correct", so refusals would count as undisclosed
    deliveries; rows are the delivery test.
    Lanes where freshness was not under test (stale_disclosed None throughout)
    are omitted; the section disappears on runs without --stale-days."""
    tested = [r for r in results if r.stale_disclosed is not None]
    if not tested:
        return []
    lines = [
        "",
        "## Freshness disclosure (--stale-days)",
        "",
        "| Lane | Delivered | Disclosed stale | **Silent-stale ↓** |",
        "|---|---|---|---|",
    ]
    for lane in lanes:
        delivered = [
            r
            for r in tested
            if r.lane == lane and r.outcome in ("correct", "wrong") and r.rows_sample is not None
        ]
        if not delivered:
            continue
        disclosed = sum(1 for r in delivered if r.stale_disclosed)
        silent = len(delivered) - disclosed
        lines.append(f"| {lane} | {len(delivered)} | {disclosed} | **{silent}** |")
    return lines


def render_markdown(results: list[QuestionResult]) -> str:
    summaries = summarize(results)
    lanes = [s.lane for s in summaries]
    paired = paired_vs_first(results)
    lines = ["# Proving-ground run", "", "## Accuracy by lane", ""]
    lines += [
        "Accuracy against an uninformed control is NOT the result: every fact a "
        "question needs is either derivable from the warehouse or must be "
        "transferred as text, and every text channel transfers it equally, so "
        "such a headline measures the presence of a file. Read **SWR** "
        "(silent-wrong rate), the enforcement tier, and drift.",
        "",
        "| Lane | Correct | Wrong | **SWR ↓** | Declined | Accuracy | 95% CI "
        "| ¢/question ↓ | s/question ↓ | ktok/q ↓ | calls/q ↓ | protocol-fail ↓ | vs first lane |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        lo, hi = s.ci95
        lane_rs = [r for r in results if r.lane == s.lane]
        wrong = sum(1 for r in lane_rs if r.outcome == "wrong")
        declined = sum(1 for r in lane_rs if r.outcome == "declined")
        cents = 100 * sum(r.ai_cost_usd for r in lane_rs) / len(lane_rs)
        secs = sum(r.latency_ms for r in lane_rs) / len(lane_rs) / 1000
        calls = sum(r.calls for r in lane_rs) / len(lane_rs)
        # First-class, not a footnote: the share of runs where the harness could
        # not parse the agent's reply. Unparsed formatting is harness noise; read
        # as "declined" it becomes a false claim about task difficulty.
        broke = sum(1 for r in lane_rs if r.protocol_failures) / len(lane_rs)
        swr = sum(1 for r in lane_rs if is_silent_wrong(r)) / len(lane_rs)
        ktok = sum(r.tokens for r in lane_rs) / len(lane_rs) / 1000
        wins_losses = paired.get(s.lane)
        delta = f"+{wins_losses[0]} / −{wins_losses[1]}" if wins_losses else "—"
        lines.append(
            f"| {s.lane} | {s.correct}/{s.total} | {wrong} | **{swr:.1%}** | {declined} "
            f"| {s.accuracy:.1%} | {lo:.1%}–{hi:.1%} | {cents:.2f}¢ | {secs:.1f}s "
            f"| {ktok:.1f} | {calls:.1f} | {broke:.1%} | {delta} |"
        )
    lines += _stage_section(results, lanes)
    lines += _pareto_section(results, lanes)
    lines += _enforcement_section(results, lanes)
    lines += _consistency_section(results, lanes)
    lines += _freshness_section(results, lanes)
    tiers = sorted({r.tier for r in results}, reverse=True)  # discriminating first
    if len(tiers) > 1:
        lines += ["", "## Accuracy by tier (discriminating = the headline)", ""]
        lines += ["| Tier | " + " | ".join(lanes) + " |", "|---|" + "---|" * len(lanes)]
        for tier in tiers:
            tier_cells = []
            for lane in lanes:
                sub = [r for r in results if r.lane == lane and r.tier == tier]
                ok = sum(1 for r in sub if r.correct)
                lo, hi = wilson_ci(ok, len(sub))
                tier_cells.append(f"{ok}/{len(sub)} ({ok / len(sub):.0%}, {lo:.0%}–{hi:.0%})")
            lines.append(f"| {tier} | " + " | ".join(tier_cells) + " |")
    lines += ["", "## By category", "", "| Category | " + " | ".join(lanes) + " |"]
    lines += ["|---|" + "---|" * len(lanes)]
    for category, cells in by_category(results).items():
        lines.append(f"| {category} | " + " | ".join(cells.get(lane, "—") for lane in lanes) + " |")
    misses = [r for r in results if not r.correct]
    if misses:
        lines += ["", "## Misses", ""]
        lines += [
            f"- `{r.lane}` × `{r.question_id}`"
            + (f" [{r.wrong_at}]" if r.wrong_at else "")
            + f": {r.reason}"
            for r in misses
        ]
    return "\n".join(lines) + "\n"


def write_report(results: list[QuestionResult], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "results.json"
    md_path = out_dir / "report.md"
    json_path.write_text(
        json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(results), encoding="utf-8")
    return json_path, md_path
