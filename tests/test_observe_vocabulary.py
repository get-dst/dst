"""The observe vocabulary: a decline is not an error.

kpis() counted `status <> 'ok'` as "errors", so every surface (CLI header,
dashboard cards, per-caller table) reported governed refusals and
clarifications as pipeline faults. Refusing and clarifying are the product
KEEPING its promise; branding them faults is the observability layer committing
the misattribution the whole suite exists to prevent.

These pin the split: `errors` counts faults only, `declined` counts governed
declines, `outcomes` carries the full per-status decomposition.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text

from services.config import settings
from services.db.session import org_session
from services.observability import observe


def _reachable(url: str) -> bool:
    try:
        with create_engine(url).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _reachable(settings.database_admin_url), reason="Postgres not reachable"
)


@pytest.fixture
def org() -> Iterator[object]:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        org_id = c.execute(
            text("INSERT INTO org (name) VALUES ('ObsVocab') RETURNING id")
        ).scalar_one()
    yield org_id
    with admin.begin() as c:
        c.execute(text("DELETE FROM request_log WHERE org_id = :o"), {"o": org_id})
        c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org_id})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org_id})


@needs_db
def test_declines_are_not_errors(org: object) -> None:
    """The outcome mix in miniature: mostly declines, one fault.

    Seeded with direct INSERTs, not log_trace — log_trace is fail-open by
    design, and a seed that can fail silently makes the test vacuous."""
    with org_session(org) as session:
        for caller, status in [
            ("tobias", "ok"),
            ("tobias", "refused"),
            ("tobias", "refused"),
            ("tobias", "clarification"),
            ("nora", "ok"),
            ("nora", "rejected"),
            ("nora", "error"),
        ]:
            session.execute(
                text(
                    "INSERT INTO request_log (org_id, request_id, lens, caller, "
                    "question, status) VALUES (:o, :r, 'reporting', :c, "
                    "'how many tickets by priority?', :s)"
                ),
                {"o": org, "r": f"req-{uuid.uuid4()}", "c": caller, "s": status},
            )

    with org_session(org) as session:
        k = observe.kpis(session)
        callers = {c["caller"]: c for c in observe.caller_report(session)}

    assert k["queries"] == 7
    assert k["errors"] == 1, "errors must count status='error' ONLY"
    assert k["declined"] == 4, "refused+clarification+rejected are declines, not errors"
    assert k["outcomes"] == {
        "ok": 2,
        "refused": 2,
        "clarification": 1,
        "rejected": 1,
        "error": 1,
    }

    # Per-caller: three declines and zero faults must never read as a 75%
    # "error rate"; the other caller holds the one real error.
    assert callers["tobias"]["errors"] == 0 and callers["tobias"]["declined"] == 3
    assert callers["nora"]["errors"] == 1 and callers["nora"]["declined"] == 1


@needs_db
def test_confidence_histogram_reports_the_tier_split(org: object) -> None:
    """An operator choosing 'only serve verified' as a gate could
    not see what the gate would cost — the split existed on every row and
    nowhere in the rollup. Served answers only; declines carry no tier."""
    with org_session(org) as session:
        for confidence, status in [
            ("verified", "ok"),
            ("partial", "ok"),
            ("partial", "ok"),
            ("unverified", "ok"),
            (None, "refused"),  # a decline: no tier, never in the histogram
        ]:
            session.execute(
                text(
                    "INSERT INTO request_log (org_id, request_id, lens, caller, "
                    "question, status, confidence) VALUES (:o, :r, 'reporting', 'a', "
                    "'q?', :s, :cf)"
                ),
                {"o": org, "r": f"req-{uuid.uuid4()}", "s": status, "cf": confidence},
            )
    with org_session(org) as session:
        k = observe.kpis(session)
    assert k["confidence_histogram"] == {"verified": 1, "partial": 2, "unverified": 1}


@needs_db
def test_admin_sql_passthrough_is_not_governed_traffic(org: object) -> None:
    """Admin passthrough rows counted as 'queries' made the governed rollup
    overstate usage and cost. Probe rows (generator_tier =
    'probe') stay fully audited in request_log but leave every governed
    counter; they get their own sql_probes block, split admin vs lens-scoped."""
    with org_session(org) as session:
        for lens, tier, question, wh_cost in [
            ("reporting", None, "how many tickets?", 0.01),
            ("reporting", "grounded", "how many tickets by priority?", 0.02),
            ("sql:bigquery", "probe", "SELECT COUNT(*) FROM secret_prod_table", 0.50),
            ("sql:bigquery", "probe", "SELECT SUM(x) FROM t", 0.25),
            ("reporting", "probe", "SELECT 1", 0.05),
        ]:
            session.execute(
                text(
                    "INSERT INTO request_log (org_id, request_id, lens, caller, "
                    "question, status, generator_tier, wh_cost_usd) "
                    "VALUES (:o, :r, :l, 'admin', :q, 'ok', :t, :w)"
                ),
                {
                    "o": org,
                    "r": f"req-{uuid.uuid4()}",
                    "l": lens,
                    "q": question,
                    "t": tier,
                    "w": wh_cost,
                },
            )

    with org_session(org) as session:
        k = observe.kpis(session)
        callers = {c["caller"]: c for c in observe.caller_report(session)}
        rows = observe.recent_requests(session)

    # Governed counters exclude every probe row.
    assert k["queries"] == 2
    assert k["warehouse_cost_usd"] == 0.03
    # The probes are counted as what they are, admin passthrough split out.
    assert k["sql_probes"] == {
        "queries": 3,
        "admin_sql": 2,
        "lens_scoped": 1,
        "warehouse_cost_usd": 0.8,
    }
    # The caller report tells the same story as the headline.
    assert callers["admin"]["queries"] == 2

    # Every row is distinguishable by an explicit field, not a name prefix.
    kinds = {r["question"]: r["kind"] for r in rows}
    assert kinds["how many tickets?"] == "answer"
    assert kinds["SELECT COUNT(*) FROM secret_prod_table"] == "admin_sql"
    assert kinds["SELECT 1"] == "sql_probe"


@needs_db
def test_audit_statement_is_windowed_honest_and_traceable(org: object) -> None:
    """The Statement (the audit page's payload): yield and verified computed
    over the window, deltas only when BOTH windows carried traffic (a delta
    against an empty prior window is an invented trend), probe rows excluded,
    per-lens rollup carrying owner/degraded/gate score."""
    from services.lenses import store as lens_store
    from services.lenses.demo import jaffle_customer_value_bundle

    with org_session(org) as session:
        lens_store.create_lens(session, jaffle_customer_value_bundle())
        rows = [
            # current window: 3 ok (2 verified), 1 clarification, 1 error
            ("customer_value", "ok", "verified", None, "2 days"),
            ("customer_value", "ok", "verified", None, "3 days"),
            ("customer_value", "ok", "partial", None, "4 days"),
            ("customer_value", "clarification", None, None, "5 days"),
            ("customer_value", "error", None, None, "6 days"),
            # a probe row in-window: audited, never counted
            ("sql:bigquery", "ok", None, "probe", "1 day"),
            # prior window: 2 asked, 1 ok+verified
            ("customer_value", "ok", "verified", None, "40 days"),
            ("customer_value", "refused", None, None, "41 days"),
        ]
        for lens, status, conf, tier, age in rows:
            session.execute(
                text(
                    "INSERT INTO request_log (org_id, request_id, lens, caller, question, "
                    "status, confidence, generator_tier, ai_cost_usd, wh_cost_usd, created_at) "
                    "VALUES (:o, :r, :l, 'a', 'q?', :s, :c, :t, 0.01, 0.02, "
                    "NOW() - CAST(:age AS interval))"
                ),
                {
                    "o": org,
                    "r": f"req-{uuid.uuid4()}",
                    "l": lens,
                    "s": status,
                    "c": conf,
                    "t": tier,
                    "age": age,
                },
            )

    with org_session(org) as session:
        d = observe.audit_statement(session, days=30)

    assert d["asked"] == 5  # the probe row never counts
    assert d["answered"] == 3
    assert d["clarified"] == 1 and d["faults"] == 1
    assert d["yield_pct"] == 60.0
    assert d["verified_pct"] == round(100 * 2 / 3, 1)
    # both windows carried traffic → deltas are measured, and negative here
    assert d["yield_delta_pp"] == round(60.0 - 50.0, 1)
    assert isinstance(d["series"], list) and sum(p["asked"] for p in d["series"]) == 5
    (lens_row,) = [r for r in d["lenses"] if r["lens"] == "customer_value"]
    assert lens_row["asked"] == 5 and lens_row["answered"] == 3
    assert lens_row["verified_pct"] == round(100 * 2 / 3, 1)
    assert "sql:bigquery" not in [r["lens"] for r in d["lenses"]]


@needs_db
def test_audit_statement_omits_deltas_without_prior_traffic(org: object) -> None:
    with org_session(org) as session:
        session.execute(
            text(
                "INSERT INTO request_log (org_id, request_id, lens, caller, question, status, "
                "confidence) VALUES (:o, :r, 'reporting', 'a', 'q?', 'ok', 'verified')"
            ),
            {"o": org, "r": f"req-{uuid.uuid4()}"},
        )
    with org_session(org) as session:
        d = observe.audit_statement(session, days=30)
    assert d["yield_pct"] == 100.0
    assert d["yield_delta_pp"] is None  # no prior traffic — a delta would be invented
    assert d["verified_delta_pp"] is None


@needs_db
def test_a_lens_query_count_excludes_probe_rows(org: object) -> None:
    """The console half: the Lenses index's query_count is governed
    answers — a caller probing raw SQL inside the lens's scope must not read
    as the lens 'being queried'."""
    from services.lenses import store as lens_store
    from services.lenses.demo import jaffle_customer_value_bundle

    with org_session(org) as session:
        lens_store.create_lens(session, jaffle_customer_value_bundle())
        for tier in [None, "probe", "probe"]:
            session.execute(
                text(
                    "INSERT INTO request_log (org_id, request_id, lens, caller, "
                    "question, status, generator_tier) VALUES (:o, :r, "
                    "'customer_value', 'a', 'q?', 'ok', :t)"
                ),
                {"o": org, "r": f"req-{uuid.uuid4()}", "t": tier},
            )
    with org_session(org) as session:
        summary = next(s for s in lens_store.list_lenses(session) if s.name == "customer_value")
    assert summary.query_count == 1


@needs_db
def test_governed_basis_counts_the_ledger_and_names_the_unmeasured(org: object) -> None:
    """The headline number: % of answers whose figure came from a
    certified or declared definition. Pre-ledger rows (NULL tag) are `unknown`:
    counted beside the percentage and left OUT of its denominator, so a window
    of old rows reads untested, never 0% governed. Declines carry no tag."""
    from services.lenses import store as lens_store
    from services.lenses.demo import jaffle_customer_value_bundle

    with org_session(org) as session:
        lens_store.create_lens(session, jaffle_customer_value_bundle())
        rows = [
            ("ok", "certified", "1 day"),
            ("ok", "declared", "2 days"),
            ("ok", "mixed", "3 days"),
            ("ok", "inferred", "4 days"),
            ("ok", None, "5 days"),  # written before the ledger existed
            ("refused", None, "6 days"),  # a decline: no figure, no tag
            ("ok", "inferred", "40 days"),  # prior window: 0% governed
        ]
        for status, tag, age in rows:
            session.execute(
                text(
                    "INSERT INTO request_log (org_id, request_id, lens, caller, question, "
                    "status, resolution_tag, created_at) VALUES (:o, :r, 'customer_value', "
                    "'a', 'q?', :s, :t, NOW() - CAST(:age AS interval))"
                ),
                {"o": org, "r": f"req-{uuid.uuid4()}", "s": status, "t": tag, "age": age},
            )

    with org_session(org) as session:
        d = observe.audit_statement(session, days=30)
        k = observe.kpis(session)
        recent = observe.recent_requests(session)

    assert d["answered"] == 5
    assert d["resolution_unknown"] == 1
    assert d["declared_pct"] == 50.0  # 2 of the 4 graded answers
    assert d["declared_delta_pp"] == 50.0  # prior window: 0 of 1
    assert d["resolution_histogram"] == {
        "certified": 1,
        "declared": 1,
        "mixed": 1,
        "inferred": 1,
        "unknown": 1,
    }
    (lens_row,) = [r for r in d["lenses"] if r["lens"] == "customer_value"]
    assert lens_row["declared_pct"] == 50.0
    assert k["resolution_histogram"]["unknown"] == 1
    assert {r["resolution_tag"] for r in recent} >= {"certified", "declared", None}


@needs_db
def test_governed_basis_is_untested_when_nothing_was_graded(org: object) -> None:
    with org_session(org) as session:
        session.execute(
            text(
                "INSERT INTO request_log (org_id, request_id, lens, caller, question, status) "
                "VALUES (:o, :r, 'reporting', 'a', 'q?', 'ok')"
            ),
            {"o": org, "r": f"req-{uuid.uuid4()}"},
        )
    with org_session(org) as session:
        d = observe.audit_statement(session, days=30)
    assert d["declared_pct"] is None and d["resolution_unknown"] == 1


@needs_db
def test_typed_share_and_dictionary_gaps_are_counted(org: object) -> None:
    """Typed serving on the statement: % of answers where every slot typed
    (NULL = before typed serving, counted apart), and the slots that keep
    clarifying for want of a value the question could not type."""
    from services.lenses import store as lens_store
    from services.lenses.demo import jaffle_customer_value_bundle

    with org_session(org) as session:
        lens_store.create_lens(session, jaffle_customer_value_bundle())
        rows = [
            ("ok", True, None),
            ("ok", True, None),
            ("ok", False, None),
            ("ok", None, None),  # served before typed serving
            ("clarification", None, {"kind": "unresolved_slot", "term": "customer_name"}),
            ("clarification", None, {"kind": "unresolved_slot", "term": "customer_name"}),
            ("clarification", None, {"kind": "unresolved_slot", "term": "month"}),
            ("clarification", None, {"kind": "ambiguous_term", "term": "revenue"}),
        ]
        for status, typed, clar in rows:
            session.execute(
                text(
                    "INSERT INTO request_log (org_id, request_id, lens, caller, question, "
                    "status, typed, clarification) VALUES (:o, :r, 'customer_value', 'a', "
                    "'q?', :s, :t, CAST(:c AS jsonb))"
                ),
                {
                    "o": org,
                    "r": f"req-{uuid.uuid4()}",
                    "s": status,
                    "t": typed,
                    "c": json.dumps(clar) if clar else None,
                },
            )
    with org_session(org) as session:
        d = observe.audit_statement(session, days=30)
    assert d["typed_pct"] == round(100 * 2 / 3, 1)  # 2 typed of the 3 that were graded
    assert d["typed_unknown"] == 1
    assert d["unresolved_slots"] == [
        {"slot": "customer_name", "count": 2},
        {"slot": "month", "count": 1},
    ]
    (lens_row,) = [r for r in d["lenses"] if r["lens"] == "customer_value"]
    assert lens_row["typed_pct"] == round(100 * 2 / 3, 1)


@needs_db
def test_eval_trend_carries_the_latest_calibration(org: object) -> None:
    from services.evals import store as eval_store

    with org_session(org) as session:
        first = eval_store.create_run(session, "customer_value", "test", score=1.0)
        eval_store.set_calibration(session, first, {"old": {"n": 1}})
        second = eval_store.create_run(session, "customer_value", "test", score=1.0)
        eval_store.set_calibration(session, second, {"vote:m:k=5 [lens]": {"n": 20}})
        eval_store.create_run(session, "customer_value", "test", score=0.5)  # no table
    with org_session(org) as session:
        (row,) = [t for t in observe.eval_trend(session) if t["lens"] == "customer_value"]
    assert row["calibration"] == {"vote:m:k=5 [lens]": {"n": 20}}
