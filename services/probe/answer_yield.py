"""Answer yield — the stratified, cold-start de-risk metric.

Answer yield = correct / asked. The honest version a data leader can stake their
name on classifies every answer by EVIDENCE STRENGTH instead of blending one soft
number:

  VERIFIED    — the org's current (ungoverned) AI matches the org's OWN ground
                truth (a certified/certified query executed live); provably correct.
  WRONG       — it contradicts that ground truth; provably wrong.
  UNANSWERED  — it declined or errored; the surface-area gap.

The headline yield is VERIFIED / asked — a *lower bound* anchored in the
customer's own verified answers, never in a manufactured oracle. Methodology
travels with the number so it sharpens under interrogation, not softens.

The caller is deliberately rawdog: schema only, no certified, no context — "your AI
today, without dst." The lift from governing it is a separate measurement;
this module establishes the floor.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

from services.benchmark.grading import _as_number
from services.benchmark.runner import wilson_ci
from services.contracts.protocols import CacheableBlock, LLMProvider, Message
from services.contracts.warehouse import QueryResult, SchemaSnapshot
from services.observability.cost import ai_cost_usd

Stratum = Literal["verified", "wrong", "unanswered"]

_REL_TOL = 1e-3


class Warehouse(Protocol):
    """The read-only slice of a connector the yield run needs."""

    def execute(
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ) -> QueryResult: ...

    def introspect(self) -> SchemaSnapshot: ...


@dataclass
class GroundTruthQuestion:
    """A business question with its verified answer — as a query to execute live
    (``truth_sql``) or a known value (``truth_value``, e.g. a documented assertion
    or a held-out oracle fact)."""

    question: str
    truth_sql: str | None = None
    truth_value: float | None = None


@dataclass
class YieldRow:
    question: str
    stratum: Stratum
    caller_value: str
    truth_value: str
    caller_sql: str
    note: str = ""
    cost_usd: float = 0.0
    latency_ms: float = 0.0


@dataclass
class YieldReport:
    rows: list[YieldRow] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.rows)

    def _count(self, s: Stratum) -> int:
        return sum(1 for r in self.rows if r.stratum == s)

    @property
    def verified(self) -> int:
        return self._count("verified")

    @property
    def wrong(self) -> int:
        return self._count("wrong")

    @property
    def unanswered(self) -> int:
        return self._count("unanswered")

    @property
    def yield_pct(self) -> float:
        return 100.0 * self.verified / self.total if self.total else 0.0

    @property
    def ci(self) -> tuple[float, float]:
        lo, hi = wilson_ci(self.verified, self.total) if self.total else (0.0, 0.0)
        return 100.0 * lo, 100.0 * hi

    @property
    def cost_per_correct(self) -> float | None:
        total_cost = sum(r.cost_usd for r in self.rows)
        return total_cost / self.verified if self.verified else None

    @property
    def latency_per_correct(self) -> float | None:
        verified = [r for r in self.rows if r.stratum == "verified"]
        return sum(r.latency_ms for r in verified) / len(verified) / 1000 if verified else None


def _fence_strip(text: str) -> str:
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return (m.group(1) if m else text).strip().rstrip(";").strip()


def schema_summary(snapshot: SchemaSnapshot, *, max_cols: int = 24) -> str:
    lines = []
    for t in snapshot.tables:
        cols = ", ".join(c.name for c in t.columns[:max_cols])
        more = f", …(+{len(t.columns) - max_cols})" if len(t.columns) > max_cols else ""
        lines.append(f"{t.name}({cols}{more})")
    return "\n".join(lines)


class RawdogCaller:
    """Current practice: schema + a model, one SELECT per question.

    ``certified`` is the governance rung — the org's definitions (dedup rules, era
    unions, exclusions). With it, this is "the same AI, governed"; without it,
    "your AI today." Same model, same budget — the only difference is whether
    the org's encoded judgment is in the prompt."""

    def __init__(
        self,
        llm: LLMProvider,
        schema: str,
        *,
        model: str,
        dialect: str = "Snowflake",
        certified: str | None = None,
    ):
        self._llm = llm
        self._model = model
        governance = (
            (
                "Your organization's DEFINITIONS — these are authoritative and override "
                f"any naive reading of the schema:\n{certified}\n\n"
            )
            if certified
            else ""
        )
        self._system = (
            f"You are a data analyst with direct read access to a {dialect} warehouse.\n"
            f"{governance}"
            f"Schema:\n{schema}\n\n"
            f"Answer the question by writing ONE read-only {dialect} SELECT. Reply with "
            "ONLY the SQL — no prose, no explanation."
        )

    def sql_for(self, question: str) -> tuple[str, float]:
        result = self._llm.complete(
            system=[CacheableBlock(text=self._system, ttl="1h")],
            messages=[Message(role="user", content=question)],
            model=self._model,
            temperature=0.0,
            max_tokens=1500,
        )
        cost = ai_cost_usd(self._model, result.input_tokens, result.output_tokens) or 0.0
        return _fence_strip(result.text), cost


def _numbers(result: QueryResult) -> list[float]:
    out = []
    for row in result.rows:
        for cell in row:
            n = _as_number(cell)
            if n is not None:
                out.append(n)
    return out


def _primary_number(result: QueryResult) -> float | None:
    nums = _numbers(result)
    return nums[0] if nums else None


def _contains(caller: QueryResult, target: float | None) -> bool:
    """Conservative: the target number must appear in the caller's result.

    A name-only match without the number is NOT verified — keeps the headline a
    lower bound (we'd rather under-count VERIFIED than inflate it)."""
    if target is None:
        return False
    for n in _numbers(caller):
        tol = max(abs(target) * _REL_TOL, 0.5)  # 0.5 abs floor for integer counts
        if abs(n - target) <= tol:
            return True
    return False


def run_answer_yield(
    warehouse: Warehouse,
    caller: RawdogCaller,
    questions: list[GroundTruthQuestion],
) -> YieldReport:
    report = YieldReport()
    for q in questions:
        t0 = time.perf_counter()
        try:
            sql, cost = caller.sql_for(q.question)
        except Exception as exc:  # noqa: BLE001 — a model failure is an unanswered question
            report.rows.append(
                YieldRow(q.question, "unanswered", "", "", "", f"model error: {exc}")
            )
            continue
        try:
            caller_res = warehouse.execute(sql, read_only=True, row_limit=100)
        except Exception as exc:  # noqa: BLE001 — bad SQL ⇒ the org got no answer
            ms = (time.perf_counter() - t0) * 1000
            report.rows.append(
                YieldRow(q.question, "unanswered", "", "", sql, f"sql error: {exc}", cost, ms)
            )
            continue
        target: float | None
        if q.truth_value is not None:
            target = q.truth_value
        else:
            assert q.truth_sql is not None, "a question needs truth_sql or truth_value"
            target = _primary_number(warehouse.execute(q.truth_sql, read_only=True, row_limit=100))
        ms = (time.perf_counter() - t0) * 1000
        caller_val = str(_primary_number(caller_res))
        truth_val = str(target)
        if not caller_res.rows or _primary_number(caller_res) is None:
            stratum: Stratum = "unanswered"
            note = "no usable value returned"
        elif _contains(caller_res, target):
            stratum, note = "verified", "matches your certified answer"
        else:
            stratum, note = "wrong", "differs from your certified answer"
        report.rows.append(
            YieldRow(q.question, stratum, caller_val, truth_val, sql, note, cost, ms)
        )
    return report


def render_terminal(report: YieldReport, *, org: str, warehouse_label: str) -> str:
    lo, hi = report.ci
    out = [
        f"ANSWER YIELD — {org} ({warehouse_label})",
        "",
        f"  Asked:       {report.total} business questions you have certified answers for",
        f"  VERIFIED:    {report.verified}  (provably correct vs your own ground truth)",
        f"  WRONG:       {report.wrong}  (provably contradicts it)",
        f"  UNANSWERED:  {report.unanswered}  (declined / errored)",
        "",
        f"  ANSWER YIELD = {report.yield_pct:.0f}%  (95% CI {lo:.0f}–{hi:.0f}%)",
        "      = verified-correct / asked, the conservative floor anchored in YOUR ground truth",
    ]
    if report.cost_per_correct is not None:
        out.append(f"  cost / correct answer:    ${report.cost_per_correct:.4f}")
    if report.latency_per_correct is not None:
        out.append(f"  latency / correct answer: {report.latency_per_correct:.1f}s")
    out += [
        "",
        "  METHODOLOGY: your ungoverned AI (schema only, no certified) answers each;",
        "  graded ✓/✗ against your certified query for the same question, run live.",
        "",
    ]
    for r in report.rows:
        mark = {"verified": "✓", "wrong": "✗", "unanswered": "·"}[r.stratum]
        out.append(f"  {mark} [{r.stratum:10}] {r.question[:60]}")
        if r.stratum == "wrong":
            out.append(f"       got {r.caller_value}  ·  your truth {r.truth_value}")
        elif r.note:
            out.append(f"       {r.note}")
    return "\n".join(out)
