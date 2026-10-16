"""Drift comparator + pre-deploy validation (unit + API)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.semantic_model import Definition
from services.db.session import org_session
from services.definitions import drift
from services.definitions.standards import OrgStandard
from services.lenses import store
from services.lenses.demo import jaffle_customer_value_bundle
from services.validate.report import validate_bundle

client = TestClient(app)


# ---- drift (pure) ----


def test_drift_flags_conflict_with_org_standard() -> None:
    defs = [Definition(term="repeat_customer", body="2+ orders", sql_expr="orders >= 2")]
    standards = [OrgStandard(term="repeat_customer", body="more than 1 order")]
    conflicts = drift.compare(defs, standards, [])
    assert len(conflicts) == 1
    assert conflicts[0].kind == "org_standard"
    assert conflicts[0].term == "repeat_customer"


def test_drift_no_conflict_when_identical() -> None:
    defs = [Definition(term="x", body="same", sql_expr="a")]
    standards = [OrgStandard(term="x", body="same", sql_expr="a")]
    assert drift.compare(defs, standards, []) == []


def test_drift_flags_other_lens() -> None:
    defs = [Definition(term="active", body="logged in last 7d")]
    other = [("marketing", [Definition(term="active", body="logged in last 30d")])]
    conflicts = drift.compare(defs, [], other)
    assert len(conflicts) == 1 and conflicts[0].source == "marketing"


# ---- validate (pure) ----


def test_validate_demo_lens_passes() -> None:
    bundle = jaffle_customer_value_bundle()
    report = validate_bundle(bundle, [], [])
    assert report.ok, [i.message for i in report.issues if i.severity == "error"]
    # demo lens has at least one sample query, probed in-scope
    assert report.probes and all(p.ok for p in report.probes)


def test_validate_flags_unlisted_connection() -> None:
    bundle = jaffle_customer_value_bundle()
    bundle.semantic_model.entities[0].source.connection = "ghost"
    report = validate_bundle(bundle, [], [])
    assert not report.ok
    assert any(i.code == "entity_connection_unlisted" for i in report.issues)


def test_validate_flags_dangling_wiki_citation() -> None:
    """A [[term]] a lens cites but does not carry is a rule the model is told to
    consult and will never be shown: a lens built with a dangling
    [[reporting conventions]] applies clean through every other warning."""
    bundle = jaffle_customer_value_bundle()
    bundle.semantic_model.definitions.append(
        Definition(term="churn risk", body="High when [[repeat customer]] is false.")
    )
    report = validate_bundle(bundle, [], [])
    dangling = [i for i in report.issues if i.code == "definition_cites_dangling"]
    assert not dangling, "the demo lens carries repeat_customer — separator folding failed"

    bundle.semantic_model.definitions[-1].body = "Governed by [[retention policy]]."
    report = validate_bundle(bundle, [], [])
    dangling = [i for i in report.issues if i.code == "definition_cites_dangling"]
    assert len(dangling) == 1, [i.code for i in report.issues]
    assert "retention policy" in dangling[0].message


def test_validate_flags_out_of_scope_sample_query() -> None:
    from services.contracts.semantic_model import SampleQuery

    bundle = jaffle_customer_value_bundle()
    bundle.semantic_model.sample_queries.append(
        SampleQuery(question="leak", sql="SELECT * FROM secret_table")
    )
    report = validate_bundle(bundle, [], [])
    assert not report.ok
    assert any(i.code == "probe_out_of_scope" for i in report.issues)


def test_validate_warns_that_a_metric_lens_generates_on_the_blind_first_pass() -> None:
    """A metric layer silently moves generation onto the leaner intent prompt, and
    an author reading lens.yaml cannot see it — the same invisible condition that
    silently drops authored `dimensions:` work. Apply says it out loud, naming what
    THIS lens loses, and points at the verb that shows both prompts."""
    bundle = jaffle_customer_value_bundle()
    assert any(e.metrics for e in bundle.semantic_model.entities)  # premise of the warning
    issue = next(
        i for i in validate_bundle(bundle, [], []).issues if i.code == "intent_tier_escalation_only"
    )
    assert issue.severity == "warning"  # honest, never a blocker
    assert "instructions" in issue.message  # the asset with no code backstop behind it
    assert "dst lens prompt" in issue.message
    assert "escalates" in issue.message


def test_validate_stays_quiet_when_there_is_no_first_pass_to_lose_anything_on() -> None:
    bundle = jaffle_customer_value_bundle()
    for e in bundle.semantic_model.entities:
        e.metrics = []
    codes = [i.code for i in validate_bundle(bundle, [], []).issues]
    assert "intent_tier_escalation_only" not in codes


def test_validate_flags_unknown_metric_ref() -> None:
    from services.contracts.semantic_model import Metric

    bundle = jaffle_customer_value_bundle()
    bundle.semantic_model.entities[0].metrics.append(
        Metric(name="bad_ratio", type="ratio", numerator="total_clv", denominator="ghost")
    )
    report = validate_bundle(bundle, [], [])
    assert not report.ok
    assert any(i.code == "metric_ref_invalid" and "ghost" in i.message for i in report.issues)


def test_validate_flags_a_metric_reaching_into_another_entity() -> None:
    """Shipped and broken before this gate: bitcoin_transactions.dollars_spent was
    `quantity * bitcoin_prices.price`, and the compiler emitted a SELECT over
    bitcoin_transactions alone — the price table never joined.

    A metric on `customers` reaching `orders` is the UNSOUND direction: orders ->
    customers is declared many_to_one, so one customer has many orders and the hop
    would duplicate every customer row. The compiler refuses it, so validate does."""
    from services.contracts.semantic_model import Metric

    bundle = jaffle_customer_value_bundle()
    bundle.semantic_model.entities[0].metrics.append(
        Metric(name="reaching", agg="sum", expr="customers.lifetime_value * orders.amount")
    )
    report = validate_bundle(bundle, [], [])
    assert not report.ok
    assert any(
        i.code == "metric_expr_cross_entity" and "orders.amount" in i.message for i in report.issues
    )


def test_a_cross_entity_metric_over_a_SAFE_declared_join_is_accepted() -> None:
    """The breaking half of that gate: a lens declaring a many_to_one join and a
    metric across it applied fine before the gate existed, and the gate then
    aborted every apply with no warning period.

    Reaching across a join the compiler CAN emit is not a defect: the compiled
    SQL equals hand-written SQL to the last digit. The metric here lives on
    `orders` and reaches
    `customers` over the declared many_to_one: each order has exactly one
    customer, nothing fans out, so nothing is rejected."""
    from services.contracts.semantic_model import Metric

    bundle = jaffle_customer_value_bundle()
    orders = next(e for e in bundle.semantic_model.entities if e.name == "orders")
    orders.metrics.append(
        Metric(name="reaching", agg="sum", expr="orders.amount * customers.number_of_orders")
    )
    report = validate_bundle(bundle, [], [])
    assert not [i for i in report.issues if i.code == "metric_expr_cross_entity"], [
        i.message for i in report.issues
    ]


def test_the_cross_entity_message_names_the_metric_the_ref_and_the_fix() -> None:
    """The gate stays, so a stranger who hits it must be able to act on it: which
    metric, which column, WHY this lens cannot compute it, and what to change."""
    from services.contracts.semantic_model import Metric

    bundle = jaffle_customer_value_bundle()
    orders = next(e for e in bundle.semantic_model.entities if e.name == "orders")
    orders.metrics.append(
        Metric(name="reaching", agg="sum", expr="orders.amount * customers.number_of_orders")
    )
    for join in bundle.semantic_model.joins:  # the same join, cardinality unstated
        join.relationship = None
    issue = next(
        i for i in validate_bundle(bundle, [], []).issues if i.code == "metric_expr_cross_entity"
    )
    assert "'reaching'" in issue.message and "'orders'" in issue.message  # which metric
    assert "customers.number_of_orders" in issue.message  # which column
    assert "duplicates every 'orders' row" in issue.message  # why
    assert "relationship" in issue.message and "moving the metric" in issue.message  # the fix


def test_a_cross_entity_metric_with_no_declared_join_is_rejected() -> None:
    from services.contracts.semantic_model import Metric

    bundle = jaffle_customer_value_bundle()
    bundle.semantic_model.joins = []
    orders = next(e for e in bundle.semantic_model.entities if e.name == "orders")
    orders.metrics.append(
        Metric(name="reaching", agg="sum", expr="orders.amount * customers.number_of_orders")
    )
    issue = next(
        i for i in validate_bundle(bundle, [], []).issues if i.code == "metric_expr_cross_entity"
    )
    assert "no declared join path" in issue.message


def test_validate_leaves_struct_access_alone() -> None:
    """`payload.value` is a struct field, not a broken entity reference; only a
    qualifier that NAMES ANOTHER ENTITY is a defect."""
    from services.contracts.semantic_model import Metric

    bundle = jaffle_customer_value_bundle()
    bundle.semantic_model.entities[0].metrics.append(
        Metric(name="structy", agg="sum", expr="payload.value")
    )
    codes = [i.code for i in validate_bundle(bundle, [], []).issues]
    assert "metric_expr_cross_entity" not in codes


def test_validate_flags_metric_ref_cycle() -> None:
    from services.contracts.semantic_model import Metric

    bundle = jaffle_customer_value_bundle()
    bundle.semantic_model.entities[0].metrics += [
        Metric(name="a", type="derived", expr="{b} + 1"),
        Metric(name="b", type="derived", expr="{a} + 1"),
    ]
    report = validate_bundle(bundle, [], [])
    assert not report.ok
    assert any(i.code == "metric_ref_invalid" for i in report.issues)


def test_validate_flags_unparseable_expansion() -> None:
    from services.contracts.semantic_model import Metric

    bundle = jaffle_customer_value_bundle()
    bundle.semantic_model.entities[0].metrics.append(
        Metric(name="broken", type="derived", expr="{total_clv} +")
    )
    report = validate_bundle(bundle, [], [])
    assert not report.ok
    assert any(i.code == "metric_sql_invalid" for i in report.issues)


# ---- API ----


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


@needs_db
def test_validate_api_and_standards_crud() -> None:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('VTest') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    h = {"Authorization": f"Bearer {raw}"}
    try:
        with org_session(org) as s:
            store.create_lens(s, jaffle_customer_value_bundle())

        # set an org standard that conflicts with the lens's repeat_customer
        up = client.put(
            "/mgmt/standards/repeat_customer",
            headers=h,
            json={"term": "repeat_customer", "body": "a totally different meaning"},
        )
        assert up.status_code == 200, up.text
        assert client.get("/mgmt/standards", headers=h).json()[0]["term"] == "repeat_customer"

        r = client.post("/mgmt/lenses/customer_value/validate", headers=h)
        assert r.status_code == 200, r.text
        report = r.json()
        assert report["ok"] is True  # drift is a warning, not an error
        assert any(c["term"] == "repeat_customer" for c in report["conflicts"])
        assert any(i["code"] == "definition_drift" for i in report["issues"])

        assert client.delete("/mgmt/standards/repeat_customer", headers=h).status_code == 204
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM semantic_asset WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


# ---- definition shapes that compile and serve wrong (pure) ----


def _codes(report: object) -> dict[str, str]:
    return {i.code: i.severity for i in report.issues}  # type: ignore[attr-defined]


def test_validate_rejects_a_definition_whose_sql_is_not_a_predicate() -> None:
    """A `sql:` that is arithmetic is ANDed into WHERE and serves nonsense; a
    plan error names the shape and the fix."""
    bundle = jaffle_customer_value_bundle()
    bundle.semantic_model.definitions.append(
        Definition(term="order_share", body="orders over all", sql_expr="orders.amount / 100")
    )
    report = validate_bundle(bundle, [], [])
    assert not report.ok
    assert _codes(report).get("definition_not_predicate") == "error"
    msg = next(i.message for i in report.issues if i.code == "definition_not_predicate")
    assert "order_share" in msg and "arithmetic" in msg


def test_validate_rejects_a_literal_and_a_numeric_case_as_predicates() -> None:
    bundle = jaffle_customer_value_bundle()
    bundle.semantic_model.definitions += [
        Definition(term="one", body="x", sql_expr="1"),
        Definition(term="flag", body="x", sql_expr="CASE WHEN orders.amount > 1 THEN 1 ELSE 0 END"),
    ]
    report = validate_bundle(bundle, [], [])
    terms = {i.subject for i in report.issues if i.code == "definition_not_predicate"}
    assert terms == {"one", "flag"}


def test_validate_accepts_real_predicates() -> None:
    bundle = jaffle_customer_value_bundle()
    bundle.semantic_model.definitions += [
        Definition(term="big", body="x", sql_expr="orders.amount > 100"),
        Definition(term="done", body="x", sql_expr="orders.status IN ('completed', 'shipped')"),
        Definition(term="recent", body="x", sql_expr="NOT orders.order_date IS NULL"),
        Definition(term="paid", body="x", sql_expr="COALESCE(orders.is_paid, false)"),
    ]
    report = validate_bundle(bundle, [], [])
    assert "definition_not_predicate" not in _codes(report)


def test_validate_rejects_a_predicate_on_a_ratio_metric() -> None:
    """The on-time-rate shape: a definition about a ratio metric carrying the
    predicate the ratio already encodes — ANDed under the division it pins
    every row to 100%."""
    from services.contracts.semantic_model import Metric

    bundle = jaffle_customer_value_bundle()
    orders = next(e for e in bundle.semantic_model.entities if e.name == "orders")
    orders.metrics.append(
        Metric(name="completed_orders", agg="count", filters=["orders.status = 'completed'"])
    )
    orders.metrics.append(
        Metric(
            name="completion_rate",
            type="ratio",
            numerator="completed_orders",
            denominator="order_count",
        )
    )
    bundle.semantic_model.definitions.append(
        Definition(
            term="completion rate",
            body="completed over all",
            about="orders.completion_rate",
            sql_expr="orders.status = 'completed'",
        )
    )
    report = validate_bundle(bundle, [], [])
    assert not report.ok
    assert _codes(report).get("definition_filters_ratio") == "error"
    msg = next(i.message for i in report.issues if i.code == "definition_filters_ratio")
    assert "completion_rate" in msg and "numerator" in msg
    # The same binding WITHOUT a predicate is the sanctioned shape.
    bundle.semantic_model.definitions[-1].sql_expr = None
    assert "definition_filters_ratio" not in _codes(validate_bundle(bundle, [], []))


def test_validate_also_treats_a_percent_metric_and_a_dividing_derived_as_ratios() -> None:
    from services.contracts.semantic_model import Metric

    bundle = jaffle_customer_value_bundle()
    orders = next(e for e in bundle.semantic_model.entities if e.name == "orders")
    orders.metrics.append(
        Metric(name="aov_share", type="derived", expr="{average_order_value} / {revenue}")
    )
    bundle.semantic_model.definitions.append(
        Definition(term="share", body="x", about="orders.aov_share", sql_expr="orders.amount > 0")
    )
    report = validate_bundle(bundle, [], [])
    assert _codes(report).get("definition_filters_ratio") == "error"


# ---- a status column with several done states (pure) ----


def _status_profile(values: list[str], *, complete: bool = True) -> dict[str, object]:
    from services.contracts.profile import ColumnProfile, TableProfile

    return {
        "orders": TableProfile(
            connection="duck",
            table="orders",
            columns=[
                ColumnProfile(name="order_id", type="VARCHAR"),
                ColumnProfile(
                    name="status",
                    type="VARCHAR",
                    is_low_cardinality=True,
                    top_values=values,
                    values_complete=complete,
                ),
            ],
        )
    }


def test_status_column_with_several_done_values_warns_and_names_them() -> None:
    from services.validate.report import status_done_family_warnings

    sm = jaffle_customer_value_bundle().semantic_model
    issues = status_done_family_warnings(
        sm,
        _status_profile(["Open", "Completed", "Invoiced", "Cancelled"]),  # type: ignore[arg-type]
    )
    assert [i.code for i in issues] == ["status_done_ambiguous"]
    assert [i.severity for i in issues] == ["warning"]
    msg = issues[0].message
    assert "'Completed'" in msg and "'Invoiced'" in msg and "'Open'" not in msg
    assert "about: orders.status" in msg


def test_status_warning_is_silent_when_a_definition_owns_the_column() -> None:
    from services.validate.report import status_done_family_warnings

    sm = jaffle_customer_value_bundle().semantic_model
    sm.definitions.append(
        Definition(
            term="done order",
            body="an order that is invoiced",
            about="orders.status",
            sql_expr="orders.status = 'Invoiced'",
        )
    )
    profiles = _status_profile(["Open", "Completed", "Invoiced"])
    assert status_done_family_warnings(sm, profiles) == []  # type: ignore[arg-type]


def test_the_definition_the_status_warning_asks_for_is_not_a_double_truth() -> None:
    from services.validate.report import definition_double_truth, status_done_family_warnings

    sm = jaffle_customer_value_bundle().semantic_model
    profiles = _status_profile(["Open", "Completed", "Invoiced"])
    assert [i.code for i in status_done_family_warnings(sm, profiles)] == [  # type: ignore[arg-type]
        "status_done_ambiguous"
    ]
    done = Definition(
        term="done order",
        body="an order that is invoiced or completed",
        about="orders.status",
        sql_expr="orders.status IN ('Invoiced', 'Completed')",
    )
    # Writing exactly what the warning asked for silences it and trips nothing else.
    assert definition_double_truth(done, sm.entities) is None
    sm.definitions.append(done)
    assert status_done_family_warnings(sm, profiles) == []  # type: ignore[arg-type]
    # The exemption is the status column's: a plain field still carries two truths.
    big = Definition(term="big", body="b", about="orders.amount", sql_expr="orders.amount > 100")
    assert definition_double_truth(big, sm.entities) is not None
    # ...and a status definition whose SQL never reads the column is still one.
    off = Definition(term="off", body="b", about="orders.status", sql_expr="orders.amount > 100")
    assert definition_double_truth(off, sm.entities) is not None


def test_status_warning_is_silent_on_one_done_value_or_a_partial_dictionary() -> None:
    from services.validate.report import status_done_family_warnings

    sm = jaffle_customer_value_bundle().semantic_model
    one = _status_profile(["Open", "Completed", "Cancelled"])
    assert status_done_family_warnings(sm, one) == []  # type: ignore[arg-type]
    partial = _status_profile(["Open", "Completed", "Invoiced"], complete=False)
    assert status_done_family_warnings(sm, partial) == []  # type: ignore[arg-type]
    # A non-status column holding the same words is not a status column.
    other = _status_profile(["Completed", "Invoiced"])
    other["orders"].columns[1].name = "notes"  # type: ignore[attr-defined]
    assert status_done_family_warnings(sm, other) == []  # type: ignore[arg-type]


def test_regular_expression_matches_are_predicates() -> None:
    """A `sql:` using `~`, REGEXP or SIMILAR TO was refused as a derivation:
    sqlglot parses regex matches as functions, not predicates."""
    from services.runtime.compiler import is_predicate

    for sql, dialect in (
        ("name ~ '^ACME'", "postgres"),
        ("name ~* 'acme'", "postgres"),
        ("name REGEXP 'acme'", "duckdb"),
        ("name RLIKE 'acme'", "snowflake"),
        ("REGEXP_LIKE(name, 'acme')", "snowflake"),
        ("REGEXP_CONTAINS(name, 'acme')", "bigquery"),
        ("name SIMILAR TO 'ac%'", "postgres"),
        ("NOT closed", "postgres"),
        ("closed_at IS NULL", "postgres"),
        ("amount BETWEEN 1 AND 2", "postgres"),
        ("name LIKE 'a%'", "postgres"),
        ("starts_with(name, 'a') AND amount > 1", "postgres"),
    ):
        assert is_predicate(sql, dialect), sql
    for sql in ("amount * 2", "'done'", "closed_at", "COALESCE(amount, 0)"):
        assert not is_predicate(sql, "postgres") or sql == "closed_at", sql
