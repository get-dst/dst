"""Per-lens golden eval — execution-accuracy backstop.

Runs a lens's reference question→SQL pairs (its sample_queries by default) through the
guard + warehouse and scores them: ``ok`` (ran, returned rows), ``empty``,
``guard_rejected``, or ``error``. Dialect-agnostic — it checks executability, not SQL
text — so it gates model/context changes without brittle string matching. Tiny per-lens
scope is what makes a maintainable golden set feasible here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.contracts.protocols import Connector
from services.contracts.semantic_model import SemanticModel
from services.runtime import sql_guard


@dataclass
class CaseResult:
    question: str
    status: str  # ok | empty | guard_rejected | error
    detail: str = ""


@dataclass
class EvalReport:
    lens: str
    total: int
    passed: int  # status == "ok"
    results: list[CaseResult] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.passed / self.total if self.total else 0.0


def evaluate_model(
    model: SemanticModel,
    connector: Connector,
    cases: list[tuple[str, str]] | None = None,
) -> EvalReport:
    """Score `cases` (question, sql); defaults to the model's sample_queries."""
    pairs = cases if cases is not None else [(s.question, s.sql) for s in model.sample_queries]
    results: list[CaseResult] = []
    for question, sql in pairs:
        guard = sql_guard.check(sql, model)
        if not guard.ok or not guard.sql:
            results.append(CaseResult(question, "guard_rejected", guard.reason or ""))
            continue
        try:
            res = connector.execute(guard.sql, read_only=True, row_limit=1000)
            results.append(CaseResult(question, "ok" if res.rows else "empty"))
        except Exception as exc:  # noqa: BLE001 — any execution failure is a graded outcome
            results.append(CaseResult(question, "error", str(exc)))
    passed = sum(1 for r in results if r.status == "ok")
    return EvalReport(lens=model.lens, total=len(results), passed=passed, results=results)
