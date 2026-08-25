"""The gate scores cases through a bounded pool.

Strictly-serial execution made a full apply a measured 15-30 minutes of
back-to-back provider round-trips (flat ~2.7s inter-call gap, zero overlap).
Cases are independent — isolated run_query, no shared mutable state — so a
bounded pool is pure wall-clock. Pinned here: calls actually overlap, the
bound holds, results keep case order, and concurrency=1 is exactly the
serial path (which the rest of the suite runs on via the conftest pin —
scripted fakes are ordered state).
"""

from __future__ import annotations

import threading
import time
from typing import Any

from services.contracts.eval import EvalCase
from services.contracts.fakes import FakeConnector, ScriptedLLM
from services.contracts.protocols import GeneratedQuery
from services.contracts.semantic_model import Entity, EntitySource, Field, SemanticModel
from services.contracts.warehouse import QueryResult
from services.evals.runner import run_behavioral
from services.runtime.answer import AnswerComposer
from services.runtime.assembly import AssembledInputs


class _SlowRecordingGenerator:
    """Thread-safe: fixed SQL, a real sleep, and (start, end) call windows."""

    model = "slow"

    def __init__(self, sql: str, delay: float) -> None:
        self._sql, self._delay = sql, delay
        self.windows: list[tuple[float, float]] = []
        self._lock = threading.Lock()

    def generate(self, **_kw: Any) -> GeneratedQuery:
        start = time.monotonic()
        time.sleep(self._delay)
        with self._lock:
            self.windows.append((start, time.monotonic()))
        return GeneratedQuery(sql=self._sql)


def _model() -> SemanticModel:
    return SemanticModel(
        lens="t",
        dialect="duckdb",
        entities=[
            Entity(
                name="orders",
                source=EntitySource(connection="c", table="orders"),
                fields=[Field(name="n", type="number")],
            )
        ],
    )


def _run(n_cases: int, concurrency: int) -> tuple[list[str], _SlowRecordingGenerator]:
    model = _model()
    assembled = AssembledInputs(
        model=model,
        prose=[],
        certified=None,
        certification="none",
        certified_sql=None,
        data_as_of=None,
        counts={},
    )
    gen = _SlowRecordingGenerator("SELECT SUM(orders.n) AS total FROM orders AS orders", 0.12)
    cases = [
        EvalCase(id=f"c{i}", lens="t", question=f"q {i}?", source="authored", expect="answer")
        for i in range(n_cases)
    ]
    outcome = run_behavioral(
        connector=FakeConnector(result=QueryResult(columns=["total"], rows=[[7]])),
        lens="t",
        cases=cases,
        assemble_for=lambda _q: assembled,
        generators_for=lambda _a: (gen, None),
        composer=AnswerComposer(ScriptedLLM(["Seven."])),
        concurrency=concurrency,
    )
    return [r.case_id for r in outcome.results], gen


def _max_overlap(windows: list[tuple[float, float]]) -> int:
    events = [(s, 1) for s, _e in windows] + [(e, -1) for _s, e in windows]
    depth = best = 0
    for _t, d in sorted(events):
        depth += d
        best = max(best, depth)
    return best


def test_cases_actually_overlap_under_the_pool() -> None:
    ids, gen = _run(8, concurrency=4)
    assert _max_overlap(gen.windows) > 1


def test_the_bound_holds() -> None:
    _ids, gen = _run(12, concurrency=3)
    assert _max_overlap(gen.windows) <= 3


def test_results_keep_case_order_regardless_of_completion_order() -> None:
    ids, _gen = _run(8, concurrency=4)
    assert ids == [f"c{i}" for i in range(8)]
    ids1, _g = _run(4, concurrency=1)
    assert ids1 == [f"c{i}" for i in range(4)]


def test_concurrency_one_is_strictly_serial() -> None:
    _ids, gen = _run(5, concurrency=1)
    assert _max_overlap(gen.windows) == 1
