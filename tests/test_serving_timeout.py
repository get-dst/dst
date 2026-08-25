"""The SERVING path had no timeout anywhere.

A lens's first query after its first certified apply could hang indefinitely,
unlogged, while every other endpoint on the same server answered normally. Apply-side
embedder cold start is documented; the query side simply waited forever. Every
per-request model/embedding call is bounded now — generously, and configurably.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest

from services.config import settings
from services.context import store as ctx_store
from services.contracts.fakes import FakeConnector, ScriptedLLM
from services.contracts.protocols import GeneratedQuery
from services.lenses.demo import jaffle_customer_value
from services.runtime import assembly
from services.runtime.answer import AnswerComposer
from services.runtime.bounded import ServingTimeout, call_bounded
from services.runtime.generator import GroundedSQLGenerator
from services.runtime.pipeline import run_query


@pytest.fixture
def wedge() -> Iterator[threading.Event]:
    """A stall the test can end: every wedged double waits on it, and the
    fixture releases it on teardown so no daemon thread outlives the test."""
    event = threading.Event()
    yield event
    event.set()


@pytest.fixture(autouse=True)
def _short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shipped bound is 600s (twice the slowest legitimate generation); these
    # tests assert the mechanism, not the number.
    monkeypatch.setattr(settings, "serving_timeout_s", 0.2)


def test_call_bounded_returns_the_value() -> None:
    assert call_bounded("fast call", lambda: 41 + 1) == 42


def test_call_bounded_raises_when_the_call_stalls(wedge: threading.Event) -> None:
    started = time.perf_counter()
    with pytest.raises(ServingTimeout) as exc:
        call_bounded("question embedding", lambda: wedge.wait(30))
    elapsed = time.perf_counter() - started
    assert elapsed < 5  # the request stops waiting; it does not ride the stall out
    # The error names the stage, the bound, and the knob that changes it.
    assert "question embedding" in str(exc.value)
    assert "serving timeout" in str(exc.value)
    assert "DST_SERVING_TIMEOUT_S" in str(exc.value)


def test_call_bounded_propagates_the_callables_own_error() -> None:
    """The bound must not swallow or reshape a real provider failure."""

    def _boom() -> None:
        raise ValueError("bad api key")

    with pytest.raises(ValueError, match="bad api key"):
        call_bounded("SQL generation", _boom)


def test_zero_disables_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "serving_timeout_s", 0)
    caller = threading.current_thread()
    assert call_bounded("SQL generation", lambda: threading.current_thread()) is caller


class _WedgedGenerator:
    """A provider that accepted the request and never answered."""

    model = "wedged"

    def __init__(self, wedge: threading.Event) -> None:
        self._wedge = wedge

    def generate(self, **_kwargs: object) -> GeneratedQuery:
        self._wedge.wait(30)
        return GeneratedQuery(sql="SELECT 1")


class _WedgedComposer:
    model = "wedged"

    def __init__(self, wedge: threading.Event) -> None:
        self._wedge = wedge

    def compose(self, **_kwargs: object) -> None:
        self._wedge.wait(30)


def test_wedged_generation_errors_instead_of_hanging(wedge: threading.Event) -> None:
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(),
        generator=_WedgedGenerator(wedge),
        composer=AnswerComposer(ScriptedLLM()),
    )
    assert res.trace.status == "error"
    assert "SQL generation" in (res.trace.error or "")
    assert "serving timeout" in (res.trace.error or "")
    assert res.response.data is None
    # …and the caller is told, rather than left holding an open connection.
    assert "could not be executed" in res.response.answer
    assert "generate" in res.trace.latency_ms


def test_wedged_composition_errors_instead_of_hanging(wedge: threading.Event) -> None:
    llm = ScriptedLLM(['{"sql": "SELECT count(*) AS n FROM customers", "definition_used": null}'])
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(),
        generator=GroundedSQLGenerator(llm),
        composer=_WedgedComposer(wedge),  # type: ignore[arg-type]
    )
    assert res.trace.status == "error"
    assert "answer composition" in (res.trace.error or "")
    assert res.trace.sql and "customers" in res.trace.sql  # the SQL that did run is kept


def test_wedged_embedder_degrades_instead_of_stalling(
    wedge: threading.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hang: the per-request embedder is CONSTRUCTED per
    call and a local embedder's constructor downloads model weights. Retrieval
    fails open (no context, no certified match) and says why on /ready — it never
    holds the request open."""

    def _wedged_resolve() -> object:
        wedge.wait(30)
        raise AssertionError("unreachable — the bound fires first")

    monkeypatch.setattr("services.llm.registry.resolve_embedder", _wedged_resolve)
    ctx_store.LAST_SERVING_ERROR.clear()
    started = time.perf_counter()
    assert assembly.embed_question("how many customers?") is None
    assert time.perf_counter() - started < 5
    assert "serving timeout" in ctx_store.LAST_SERVING_ERROR.get("error", "")
