"""The frontier lane with scripted generation: what the model is shown, how its
reply is read, and the lane's model choice. No provider is called."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from services.contracts.cube_query import CubeQuery
from services.contracts.lens_config import LensConfig
from services.contracts.protocols import CacheableBlock, LLMResult, Message
from services.llm.registry import ResolvedModel
from services.resolve import frontier
from services.serve import door
from tests import _cube_fake as fake


class Scripted:
    """An LLMProvider that answers with the reply it was given and keeps what
    it was asked."""

    def __init__(self, reply: str, *more: str) -> None:
        self.replies = [reply, *more]
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        system: list[CacheableBlock],
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResult:
        self.calls.append({"system": system, "messages": messages, "model": model})
        reply = self.replies[min(len(self.calls), len(self.replies)) - 1]
        return LLMResult(text=reply, input_tokens=1200, output_tokens=80)


def _model(reply: dict[str, Any] | str, *more: dict[str, Any]) -> tuple[ResolvedModel, Scripted]:
    llm = Scripted(
        reply if isinstance(reply, str) else json.dumps(reply), *(json.dumps(m) for m in more)
    )
    return ResolvedModel(llm, "scripted", "fast-1"), llm


QUERY = {
    "measures": ["orders.count"],
    "dimensions": ["orders.status"],
    "timeDimensions": [
        {"dimension": "orders.ordered_at", "dateRange": ["2026-08-01", "2026-08-31"]}
    ],
}


def test_a_query_reply_becomes_candidates_with_the_ledger() -> None:
    model, llm = _model(
        {
            "answer": "query",
            "queries": [QUERY],
            "note": "orders by status in August",
            "ledger": [
                {"word": "orders", "to": "member", "ref": "orders.count"},
                {"word": "status", "to": "member", "ref": "orders.status"},
                {"word": "August", "to": "period", "ref": "2026-08-01..2026-08-31"},
                {"word": "online", "to": "none", "ref": ""},
            ],
        }
    )
    out = frontier.resolve("orders by status in August", fake.view(), model, today=date(2026, 9, 1))
    assert out.outcome == "query"
    assert [c.query for c in out.candidates] == [CubeQuery.model_validate(QUERY)]
    assert out.ledger is not None
    assert [(b.word, b.kind) for b in out.ledger] == [
        ("orders", "member"),
        ("status", "member"),
        ("August", "period"),
    ]  # a word mapped to nothing is left out: the word check reads it as uncovered
    assert out.spend["input_tokens"] == 1200
    assert llm.calls[0]["model"] == "fast-1"


def test_the_context_shows_the_clock_values_and_overlay_but_not_hidden_members() -> None:
    text = frontier.context(fake.view(), today=date(2026, 9, 1), timezone="Europe/Helsinki")
    assert "TODAY is Tuesday 2026-09-01 (Europe/Helsinki)" in text
    assert "data up to 2026-08-31" in text
    assert '"values":["Open","Paid","Refunded"]' in text
    assert "refund_basis" in text and "lifetime value" in text
    assert "staff.salary" not in text
    assert "staff.salary" in frontier.context(fake.view(), today=date(2026, 9, 1), groups=["hr"])


def test_a_lens_with_views_shows_only_what_its_views_reach() -> None:
    text = frontier.context(fake.view(["shop"]), today=date(2026, 9, 1))
    assert '"view":"shop"' in text and '"cube":"orders"' in text
    assert '"cube":"quotes"' not in text


@pytest.mark.parametrize(
    ("reply", "outcome"),
    [
        ({"answer": "cannot", "reason": "no margin member"}, "cannot"),
        (
            {
                "answer": "clarify",
                "mode": "ambiguous_metric",
                "words": ["q"],
                "question": "which company?",
            },
            "clarify",
        ),
        ("I think the answer is 42", "invalid"),
        (
            {"answer": "query", "queries": [{"measures": ["orders.count"], "sql": "SELECT 1"}]},
            "invalid",
        ),
        ({"answer": "query", "queries": []}, "invalid"),
    ],
)
def test_every_other_reply_is_read_as_what_it_is(reply: Any, outcome: str) -> None:
    model, _ = _model(reply)
    assert frontier.resolve("q", fake.view(), model, today=date(2026, 9, 1)).outcome == outcome


def test_a_fenced_reply_and_extra_queries_are_tolerated() -> None:
    reply = (
        "```json\n"
        + json.dumps({"answer": "query", "queries": [QUERY] * 5, "ledger": []})
        + "\n```"
    )
    out = frontier.read_reply(reply)
    assert out.outcome == "query" and len(out.candidates) == frontier.MAX_QUERIES


def test_the_lane_model_is_the_fast_tier_of_the_lens_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(door.registry, "fast_sibling", lambda ref: f"fast-of:{ref}")
    monkeypatch.setattr(door.registry, "tier", lambda name: f"tier:{name}")
    monkeypatch.setattr(door.registry, "resolve", lambda ref: asked.append(ref))
    door.frontier_model(LensConfig(name="a", model={"provider": "p", "model": "big"}))
    door.frontier_model(LensConfig(name="b"))
    assert asked == ["fast-of:p/big", "tier:fast"]


def test_dst_serving_overrides_the_lens(monkeypatch: pytest.MonkeyPatch) -> None:
    lens = LensConfig(name="a", serving="typed")
    assert door.serving_core(lens) == "typed"
    monkeypatch.setattr(door.settings, "serving", "cube")
    assert door.serving_core(lens) == "cube"


def test_an_order_written_as_one_object_per_member_reads_as_a_query() -> None:
    # Cube reads ``order`` as an object or an array of pairs; a model writing
    # a list of one-key objects means the same and must not fail the reply.
    q = {**QUERY, "order": [{"orders.count": "desc"}]}
    out = frontier.read_reply(json.dumps({"answer": "query", "queries": [q], "ledger": []}))
    assert out.outcome == "query"
    assert out.candidates[0].query.order == [("orders.count", "desc")]


def test_a_ledger_ref_that_is_not_a_string_is_read_leaf_by_leaf() -> None:
    reply = {
        "answer": "query",
        "queries": [QUERY],
        "ledger": [
            {"word": "August", "to": "period", "ref": ["2026-08-01", "2026-08-31"]},
            {"word": "status", "to": "member", "ref": {"member": "orders.status"}},
            {"word": "orders", "to": "member", "ref": None},
            {"word": "paid", "to": "filter", "ref": 3},
        ],
    }
    out = frontier.read_reply(json.dumps(reply))
    assert out.outcome == "query"
    assert out.ledger is not None
    assert [(b.word, b.target) for b in out.ledger] == [
        ("August", "2026-08-01"),
        ("August", "2026-08-31"),
        ("status", "orders.status"),
        ("orders", ""),
        ("paid", "3"),
    ]


def test_a_ledger_that_does_not_read_leaves_the_query_and_no_ledger() -> None:
    # Never a fault: the queries stand and the word check, finding no
    # ledger, asks back under uncovered_word.
    reply = {"answer": "query", "queries": [QUERY], "ledger": ["orders", {"to": "member"}]}
    out = frontier.read_reply(json.dumps(reply))
    assert out.outcome == "query" and out.ledger is None


def test_the_question_s_relative_period_reaches_the_model_as_dates() -> None:
    model, llm = _model({"answer": "cannot", "reason": "x"})
    frontier.resolve("orders this week", fake.view(), model, today=date(2026, 9, 1))
    turn = llm.calls[0]["messages"][0].content
    assert "'this week' is 2026-08-31..2026-09-06 (ISO week, Monday to Sunday)" in turn
    assert "not over (today is 2026-09-01, data loaded through 2026-08-31)" in turn
    # the system block carries no per-question period, so it stays cached
    system = llm.calls[0]["system"][0].text
    assert "2026-09-06" not in system
    frontier.resolve("orders in August 2026", fake.view(), model, today=date(2026, 9, 1))
    assert "PERIODS" not in llm.calls[1]["messages"][0].content


def _certified(root: Any, text: str) -> None:
    lens_dir = root / "lenses" / "shop"
    lens_dir.mkdir(parents=True, exist_ok=True)
    (lens_dir / "certified_answers.yaml").write_text(text)


def test_certified_pairs_with_a_cube_query_are_the_lane_s_exemplars(tmp_path: Any) -> None:
    _certified(
        tmp_path,
        """
- question: orders by status
  sql: SELECT status, COUNT(*) FROM shop.orders GROUP BY 1
  query: {measures: [orders.count], dimensions: [orders.status], order: {orders.count: desc}}
- question: revenue last month
  sql: SELECT SUM(amount) FROM shop.orders
- question: orders in {period}
  sql: SELECT COUNT(*) FROM shop.orders WHERE {period.start} <= ordered_at
  slots: {period: {type: date_range}}
  sample_bindings: [{period: 2026-Q2}, {period: 2026}]
  query:
    measures: [orders.count]
    timeDimensions: [{dimension: orders.ordered_at, dateRange: "{period}"}]
- question: orders in {period} with no sample
  sql: SELECT COUNT(*) FROM shop.orders WHERE {period.start} <= ordered_at
  slots: {period: {type: date_range}}
  query: {measures: [orders.count]}
""",
    )
    pairs = door.load_exemplars(tmp_path, "shop")
    # a pair without a query is none; a template is rendered with its first
    # sample binding and says it is one; a template without a sample is none
    assert [(q, cq.measures, cq.order) for q, cq in pairs[:1]] == [
        ("orders by status", ["orders.count"], [("orders.count", "desc")])
    ]
    assert len(pairs) == 2
    question, template = pairs[1]
    assert question == "orders in 2026-Q2 (a template: orders in {period})"
    assert template.timeDimensions[0].dateRange == ["2026-04-01", "2026-06-30"]
    assert door.load_exemplars(tmp_path, "other") == []
    model, llm = _model({"answer": "cannot", "reason": "x"})
    frontier.resolve("q", fake.view(), model, today=date(2026, 9, 1), exemplars=pairs)
    system = llm.calls[0]["system"][0]
    assert '"question": "orders by status"' in system.text and system.ttl == "1h"


def test_a_certified_query_must_be_a_cube_query_beside_its_sql() -> None:
    from services.project.loader import load_lens_source

    ok = "- question: q\n  sql: SELECT 1\n  query: {measures: [orders.count]}\n"
    src = load_lens_source({"lens.yaml": "name: a\n", "certified_answers.yaml": ok})
    assert src.certified_answers is not None
    assert src.certified_answers[0]["query"] == {"measures": ["orders.count"]}
    for bad in (
        "- question: q\n  sql: SELECT 1\n  query: {measures: [orders.count], sql: x}\n",
        "- question: q\n  query: {measures: [orders.count]}\n",
    ):
        with pytest.raises(ValueError, match="query"):
            load_lens_source({"lens.yaml": "name: a\n", "certified_answers.yaml": bad})


NAMED = {
    "answer": "clarify",
    "mode": "ambiguous_metric",
    "words": ["revenue"],
    "question": "revenue net of refunds by order date or by refund date?",
}


def test_a_clarify_that_names_a_mode_and_the_words_is_the_lane_s_clarify() -> None:
    model, llm = _model(NAMED)
    out = frontier.resolve("revenue in August", fake.view(), model, today=date(2026, 9, 1))
    assert (out.outcome, out.mode, out.words) == ("clarify", "ambiguous_metric", ["revenue"])
    assert len(llm.calls) == 1


@pytest.mark.parametrize(
    "unnamed",
    [
        {"answer": "clarify", "question": "which company?"},
        {"answer": "clarify", "mode": "feeling_unsure", "words": ["revenue"], "question": "?"},
        {"answer": "clarify", "mode": "ambiguous_metric", "words": ["margin"], "question": "?"},
    ],
)
def test_a_clarify_without_a_mode_is_asked_again_once(unnamed: dict[str, Any]) -> None:
    model, llm = _model(unnamed, {"answer": "query", "queries": [QUERY], "ledger": []})
    out = frontier.resolve("revenue in August", fake.view(), model, today=date(2026, 9, 1))
    assert out.outcome == "query"
    assert len(llm.calls) == 2
    again = llm.calls[1]["messages"]
    assert [m.role for m in again] == ["user", "assistant", "user"]
    assert "ambiguous_metric" in again[2].content and "answer" in again[2].content
    assert out.spend["input_tokens"] == 2400  # both calls are the answer's spend


def test_a_second_clarify_without_a_mode_is_a_lane_fault_not_a_clarify() -> None:
    unnamed = {"answer": "clarify", "question": "which company?"}
    model, llm = _model(unnamed, unnamed)
    out = frontier.resolve("revenue in August", fake.view(), model, today=date(2026, 9, 1))
    assert out.outcome == "invalid" and "mode" in out.reason
    assert len(llm.calls) == 2


def test_a_definition_about_a_hidden_member_reaches_only_those_who_may_see_it() -> None:
    lens = fake.view(
        definitions=[
            {"metric": "pay", "about": "staff.salary", "summary": "Monthly pay per person."}
        ]
    )
    assert "Monthly pay per person" not in frontier.context(lens, today=date(2026, 9, 1))
    assert "Monthly pay per person" in frontier.context(lens, today=date(2026, 9, 1), groups=["hr"])
