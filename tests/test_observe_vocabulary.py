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
