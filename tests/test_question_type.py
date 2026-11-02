"""The question-type step: the rules, the answers, and the ledger's stop-list
and operator words. No database, no model, no Cube.

The worlds are the fake shop (``tests/_cube_fake``) and the jaffle customer
value lens: invented, like every world in this suite.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from services.contracts.failure_modes import QUESTION_TYPE_NAMES, FailureMode
from services.contracts.fakes import ScriptedLLM
from services.contracts.lens_model import LensModel, Overlay
from services.contracts.response import QueryResponse
from services.contracts.trace import TraceLog
from services.evals.runner import run_behavioral
from services.lenses.demo import jaffle_customer_value_bundle
from services.llm.registry import ResolvedModel
from services.observability import usage as ai_usage
from services.project.schema import parse_project_yaml
from services.resolve import question_type as qt
from services.runtime.pipeline import PipelineResult
from services.verify import pre
from tests import _cube_fake as fake
from tests.test_cube_verify import AUG, ask, q

# ── the rules ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("question", "kind", "subject"),
    [
        ("what does SQO mean", "definition", "sqo"),
        ("What does 'active customer' mean exactly?", "definition", "active customer"),
        ("how is win rate calculated", "definition", "win rate"),
        ("how do we calculate churn?", "definition", "churn"),
        ("define churn", "definition", "churn"),
        ("what is the formula for conversion rate", "definition", "conversion rate"),
        ("what counts as an active contract", "definition", "active contract"),
        ("what is an SQO", "definition", "sqo"),
        ("do we have NPS?", "capability", "nps"),
        ("do we track customer satisfaction", "capability", "customer satisfaction"),
        ("is there any data on technician utilisation", "capability", "technician utilisation"),
        ("is it possible to split revenue by region", "capability", "split revenue by region"),
        ("is NPS tracked anywhere", "capability", "nps"),
        ("what can I ask", "capability", ""),
        ("help", "capability", ""),
        ("give me the SQL for revenue by month", "sql", "revenue by month"),
        ("revenue by region last quarter as SQL", "sql", "revenue by region last quarter"),
        ("give me the sql", "sql", ""),
        ("what report shows churn by month", "locate", "churn by month"),
        ("where can I find the margin report", "locate", "margin"),
        ("which dashboard has the webshop numbers", "locate", "webshop"),
        ("which lens holds refund rate", "locate", "refund rate"),
        ("where in dst is churn defined", "locate", "churn"),
        ("show me my last request", "admin", ""),
        ("what did I ask last", "admin", ""),
        ("can you email this to Mika", "not_a_data_question", ""),
        ("send this to the sales channel", "not_a_data_question", ""),
        ("how do I export this to excel", "not_a_data_question", ""),
        ("make me a chart of revenue by month", "not_a_data_question", ""),
        ("schedule this report every monday", "not_a_data_question", ""),
        ("what is the meaning of life", "not_a_data_question", ""),
        ("hello", "not_a_data_question", ""),
        ("thanks!", "not_a_data_question", ""),
        ("tell me a joke", "not_a_data_question", ""),
        ("how many orders did we get last week", "query", ""),
        ("revenue by region in Q3", "query", ""),
        ("share of webshop orders from Uusimaa vs Pirkanmaa this quarter", "query", ""),
        ("open orders by status", "query", ""),
        ("can you show me revenue by month", "query", ""),
        ("can you break down revenue by household and commercial customers", "query", ""),
        ("what is the revenue in Q3", "query", ""),
        ("what is an average order value in Q3", "query", ""),
        ("is 2.3M right?", "query", ""),
        ("export orders by month", "query", ""),
    ],
)
def test_the_rules_decide_each_type(question: str, kind: str, subject: str) -> None:
    typed = qt.classify(question)
    assert typed.type == kind, typed
    assert typed.subject == subject, typed


def test_the_rules_are_a_table_per_language() -> None:
    """The phrasings are data: one entry per language with every rule, so a
    language is added by adding an entry, never a branch."""
    assert [r.lang for r in qt.RULES] == ["en"]
    names = {
        "admin",
        "sql",
        "chat",
        "difference",
        "definition",
        "help",
        "locate",
        "capability",
        "errand",
    }
    for rules in qt.RULES:
        assert names <= set(rules.patterns()), rules.lang


def test_a_difference_ask_carries_both_sides() -> None:
    typed = qt.classify("what's the difference between gross and net revenue")
    assert (typed.type, typed.subject, typed.second) == ("definition", "gross", "net revenue")
    typed = qt.classify("difference between quotes sent and quotes won?")
    assert (typed.subject, typed.second) == ("quotes sent", "quotes won")


def test_an_admin_ask_counts_its_requests() -> None:
    assert qt.classify("my last 3 queries").n == 3
    assert qt.classify("show me my last five requests").n == 5
    # "our last month revenue" is a data question, not the log.
    assert qt.classify("our last month revenue").type == "query"


def test_the_open_case_is_left_open_and_the_held_words_decide_it() -> None:
    assert qt.classify("churn?").type is None
    held = qt.vocabulary(["contract churn", "revenue"])
    assert qt.classify("churn?", held=held).type == "query"
    # Words no lens holds stay open (a column of an exposed table may hold
    # them): never "not about data" on the catalogue's silence.
    assert qt.classify("reshelving events", held=held).type is None
    assert qt.classify("translate the churn page into swedish", held=held).type == "query"


def test_decide_asks_no_model_for_a_decided_question_and_never_in_driver_mode() -> None:
    llm = ScriptedLLM(['{"type": "definition", "subject": "churn"}'])
    model = ResolvedModel(llm, "scripted", "fast-1")
    typed = qt.decide_type("how many orders last week", held=None, model=model)
    assert typed.type == "query" and not typed.by_model
    typed = qt.decide_type("churn?", held=None, model=model)
    assert typed.type == "definition" and typed.by_model and typed.subject == "churn"
    with ai_usage.no_models():
        typed = qt.decide_type("churn?", held=None, model=model)
    assert typed.type == "query" and typed.rule == "open_as_query"
    assert qt.decide_type("churn?", held=None, model=None).type == "query"


def test_a_model_reply_outside_the_types_is_a_query() -> None:
    model = ResolvedModel(ScriptedLLM(["something else"]), "scripted", "fast-1")
    assert qt.consult("churn?", model).type == "query"
    model = ResolvedModel(ScriptedLLM(["not_a_data_question"]), "scripted", "fast-1")
    assert qt.consult("blah blah", model).type == "not_a_data_question"


# ── the catalogue and the answers ───────────────────────────────────────────


def _shop() -> qt.Catalogue:
    model = LensModel.build(fake.doc(), Overlay.model_validate(fake.OVERLAY), "shop")
    return qt.catalogue_of("shop", model, dictionaries=fake.PROBE["dictionaries"])


def _jaffle() -> qt.Catalogue:
    return qt.catalogue_of("customer_value", jaffle_customer_value_bundle().semantic_model)


def _answer(question: str, cat: qt.Catalogue, **kw: Any) -> tuple[QueryResponse, TraceLog]:
    typed = qt.classify(question)
    assert typed.type not in (None, "query"), typed
    return qt.answer(
        typed,
        cat,
        lens=cat.lenses[0],
        request_id="req_t",
        org_id="org",
        caller="ada",
        question=question,
        **kw,
    )


def test_a_definition_is_read_from_the_member_and_runs_nothing() -> None:
    resp, trace = _answer("what does revenue mean", _shop())
    assert resp.status == "ok" and resp.mode is FailureMode.definition
    assert "shop.revenue" in resp.answer and "also called sales" in resp.answer
    assert resp.data is None and resp.sql is None
    assert trace.mode is FailureMode.definition and trace.serving == qt.SERVING
    assert trace.status == "ok"


def test_a_definition_names_dictionary_values_and_a_definitions_page() -> None:
    resp, _ = _answer("what does status mean here", _shop())
    assert "Open, Paid, Refunded" in resp.answer
    resp, _ = _answer("what does repeat customer mean", _jaffle())
    assert resp.status == "ok" and "number_of_orders > 1" in resp.answer
    assert resp.citations and resp.citations[0].type == "definition"


def test_a_definition_never_invented() -> None:
    resp, _ = _answer("what does NPS mean", _shop())
    assert resp.status == "refused" and resp.mode is FailureMode.definition
    assert "nothing in shop defines 'nps'" in resp.answer
    assert "never invented" in resp.answer


def test_a_difference_puts_two_members_side_by_side() -> None:
    resp, _ = _answer("what is the difference between revenue and orders", _shop())
    assert resp.status == "ok"
    assert "shop.revenue" in resp.answer and "shop.count" in resp.answer
    assert "The difference: shop.revenue vs shop.count" in resp.answer
    resp, _ = _answer("is revenue the same as sales", _shop())
    assert "name the same member, shop.revenue" in resp.answer


def test_an_ambiguous_definition_lists_its_readings() -> None:
    resp, _ = _answer("what does value mean", _jaffle())
    assert resp.status == "ok" and "readings:" in resp.answer


def test_capability_says_yes_with_the_member_and_offers_the_query() -> None:
    resp, _ = _answer("do we have revenue by region", _shop())
    assert resp.status == "ok" and resp.mode is FailureMode.capability
    assert resp.answer.startswith("yes:") and "shop.revenue" in resp.answer
    assert "ask it as a question" in resp.answer


def test_capability_quotes_the_not_computable_entry() -> None:
    resp, _ = _answer("do we have lifetime value", _shop())
    assert resp.status == "refused" and resp.mode is FailureMode.capability
    assert "'lifetime value' is declared not computable in shop" in resp.answer
    resp, _ = _answer("is LTV tracked", _shop())
    assert resp.status == "refused"


def test_capability_says_no_naming_what_is_declared() -> None:
    resp, _ = _answer("do we track NPS", _shop())
    assert resp.status == "ok" and resp.answer.startswith("no: no lens holds 'nps'")
    assert "what is declared:" in resp.answer


def test_help_lists_the_lenses_and_their_members() -> None:
    cat = qt.merge([_shop(), _jaffle()])
    resp, _ = _answer("what can I ask", cat)
    assert resp.status == "ok" and "shop" in resp.answer and "customer_value" in resp.answer


def test_locate_cites_the_hand_off_and_dst_keeps_no_inventory() -> None:
    cat = _shop()
    resp, _ = _answer("what report shows revenue by month", cat)
    assert resp.status == "refused" and resp.mode is FailureMode.hand_off
    assert "no hand_off is declared" in resp.answer
    hand_off = [qt.HandOffEntry("Sales dashboard", "dashboard", url="https://bi.example/sales")]
    resp, _ = _answer("what report shows revenue by month", cat, hand_off=hand_off)
    assert resp.status == "ok" and resp.mode is FailureMode.hand_off
    assert "Sales dashboard (dashboard) https://bi.example/sales" in resp.answer
    assert "shop holds shop.revenue" in resp.answer


def test_where_in_dst_names_the_lens_and_member_then_offers_the_query() -> None:
    resp, _ = _answer("which lens holds revenue", qt.merge([_shop(), _jaffle()]))
    assert resp.status == "ok" and resp.mode is FailureMode.hand_off
    assert (
        "shop as shop.revenue" in resp.answer and "customer_value as orders.revenue" in resp.answer
    )


def test_admin_reads_the_callers_own_log() -> None:
    rows = [
        {
            "request_id": "req_1",
            "lens": "shop",
            "question": "orders by status",
            "status": "ok",
            "mode": None,
            "created_at": datetime(2026, 9, 1, 9, 0),
        }
    ]
    resp, _ = _answer("show me my last request", _shop(), last_requests=lambda n: rows[:n])
    assert resp.status == "ok" and resp.mode is FailureMode.admin
    assert "req_1: 'orders by status' -> shop, ok" in resp.answer
    resp, _ = _answer("show me my last request", _shop(), last_requests=lambda n: [])
    assert resp.answer == "no request of yours is on record"
    resp, _ = _answer("show me my last request", _shop())
    assert resp.status == "refused"


def test_a_bare_sql_ask_returns_the_sql_of_the_last_answer() -> None:
    rows = [{"request_id": "req_1", "question": "orders", "sql": "SELECT count(*) FROM o"}]
    resp, _ = _answer("give me the sql", _shop(), last_requests=lambda n: rows)
    assert resp.status == "ok" and resp.mode is FailureMode.sql_request
    assert "SELECT count(*) FROM o" in resp.answer


def test_not_a_data_question_is_refused_naming_the_hand_off() -> None:
    resp, trace = _answer(
        "can you email this to Mika",
        _shop(),
        hand_off=[qt.HandOffEntry("Helpdesk", "ticket", mcp="https://help.example/mcp")],
    )
    assert resp.status == "refused" and resp.mode is FailureMode.not_a_data_question
    assert "not a data question" in resp.answer and "Helpdesk (ticket)" in resp.answer
    assert trace.status == "refused"


def test_the_sql_body_carries_the_sql_and_the_cube_query() -> None:
    body = qt.sql_body("SELECT 1", [{"measures": ["shop.count"]}])
    assert body.startswith("SQL:\nSELECT 1") and '"shop.count"' in body
    assert qt.sql_body(None, None) == "no SQL was produced for this question"


def test_the_catalogue_leaves_hidden_members_out() -> None:
    model = LensModel.build(fake.doc(), Overlay.model_validate(fake.OVERLAY), "hr", views=["staff"])
    names = {m.name for m in qt.catalogue_of("hr", model).members}
    assert "staff.headcount" in names and "staff.salary" not in names
    hr = {m.name for m in qt.catalogue_of("hr", model, groups=["hr"]).members}
    assert "staff.salary" in hr
    # A hidden member defines nothing for a caller it is withheld from.
    resp, _ = qt.answer(
        qt.classify("what does salary mean"),
        qt.catalogue_of("hr", model),
        lens="hr",
        request_id="r",
        org_id="o",
        caller="ada",
        question="what does salary mean",
    )
    assert resp.status == "refused"


# ── hand_off in dst.yaml ────────────────────────────────────────────────────


def test_hand_off_is_a_project_key_needing_a_place() -> None:
    cfg = parse_project_yaml(
        "hand_off:\n  - name: Sales dashboard\n    kind: dashboard\n    url: https://bi.example\n"
        "  - name: Helpdesk\n    kind: ticket\n    mcp: https://help.example/mcp\n"
    )
    assert [h.name for h in cfg.hand_off] == ["Sales dashboard", "Helpdesk"]
    with pytest.raises(ValueError, match="url or an mcp"):
        parse_project_yaml("hand_off:\n  - name: Nowhere\n    kind: report\n")


# ── dst test: expect accepts the types ──────────────────────────────────────


def _case(expect: str) -> Any:
    from services.contracts.eval import EvalCase

    return EvalCase(
        id="c1", lens="shop", question="what does revenue mean", expect=expect, source="authored"
    )


def _served(mode: FailureMode | None, status: str = "ok") -> PipelineResult:
    resp = QueryResponse(lens="shop", status=status, answer="x", mode=mode, request_id="r")  # type: ignore[arg-type]
    trace = TraceLog(
        request_id="r", org_id="o", lens="shop", caller="eval", question="q", status=status
    )  # type: ignore[arg-type]
    return PipelineResult(response=resp, trace=trace)


def test_expect_definition_passes_on_the_mode_whatever_the_status() -> None:
    assert "definition" in QUESTION_TYPE_NAMES
    out = run_behavioral(
        lens="shop",
        cases=[_case("definition")],
        serve=lambda _q: _served(FailureMode.definition, "refused"),
        concurrency=1,
    )
    assert out.results[0].passed and out.results[0].mode == "definition"
    out = run_behavioral(
        lens="shop", cases=[_case("capability")], serve=lambda _q: _served(None), concurrency=1
    )
    assert not out.results[0].passed
    assert "expected a capability answer" in (out.results[0].reason or "")


# ── the ledger: stop-list and operator words ────────────────────────────────


def test_glossing_words_never_stop_the_ledger() -> None:
    query = q(measures=["orders.count"], timeDimensions=[AUG])
    led = [("orders", "member", "orders.count"), ("August", "period", "2026-08")]
    c = pre.uncovered_word(
        ask("I want to know how many orders we can see through August", [query], ledger=led),
        [query],
    )
    assert c.outcome == "pass", c.detail


def test_an_operator_word_binds_over_a_measure_that_carries_it() -> None:
    # "refund rate": the measure's own text carries 'rate'.
    view = fake.view()
    query = q(measures=["orders.count"], timeDimensions=[AUG])
    led = [("orders", "member", "orders.count"), ("August", "period", "2026-08")]
    c = pre.uncovered_word(ask("average orders in August", [query], ledger=led, view=view), [query])
    assert (c.outcome, c.mode) == ("clarify", FailureMode.uncovered_word)
    assert "'average' asks for an average" in c.detail and "orders.count" in c.detail
    # A ratio composed from two measures passes; a lone measure asks, naming candidates.
    two = q(measures=["orders.revenue_net", "orders.revenue"], timeDimensions=[AUG])
    led2 = [
        ("revenue", "member", "orders.revenue"),
        ("net", "member", "orders.revenue_net"),
        ("August", "period", "2026-08"),
    ]
    assert (
        pre.uncovered_word(
            ask("share of net revenue in August", [two], ledger=led2, view=view), [two]
        ).outcome
        == "pass"
    )
    one = q(measures=["orders.revenue"], timeDimensions=[AUG])
    c = pre.uncovered_word(
        ask("share of revenue in August", [one], ledger=led2[:1] + led2[2:], view=view), [one]
    )
    assert c.outcome == "clarify" and "'share' asks for a ratio" in c.detail
    assert "which measure, over what?" in c.detail


def test_a_change_word_binds_over_a_comparison_window() -> None:
    view = fake.view()
    led = [("revenue", "member", "orders.revenue")]
    cmp = q(
        measures=["orders.revenue"],
        timeDimensions=[
            {
                "dimension": "orders.ordered_at",
                "compareDateRange": [["2026-07-01", "2026-07-31"], ["2026-08-01", "2026-08-31"]],
            }
        ],
    )
    assert (
        pre.uncovered_word(
            ask("revenue growth July vs August", [cmp], ledger=led, view=view), [cmp]
        ).outcome
        == "pass"
    )
    one = q(measures=["orders.revenue"], timeDimensions=[AUG])
    c = pre.uncovered_word(ask("did revenue drop in August", [one], ledger=led, view=view), [one])
    assert c.outcome == "clarify" and "'drop' asks for a change" in c.detail
