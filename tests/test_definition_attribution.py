"""`definition_used` must name a term the SQL actually applied.

The failure this pins: "net revenue by region" serves correct SQL — captured
payments minus refunds, grouped by region, touching marketing_spend nowhere —
and reports

    definition_used: 'total_marketing_spend'

Attribution falls back to a qualifier-insensitive compare so a definition written
`payments.amount` still matches `p.amount` when `p` IS payments. But stripping the
qualifier entirely makes `marketing_spend.amount` match ANY sum of ANY column
called `amount`, and `amount` sits on payments, refunds and marketing_spend alike.
The longest match then wins outright.

Three harms, ascending: the label is wrong; governed-share reporting counts
column-name collisions as governance; and surfacing `definition_used` to the
user — the fix for two people getting different numbers for "revenue" — turns a
silent wrong into a confidently mislabelled one.
"""

from __future__ import annotations

from services.contracts.semantic_model import (
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.runtime.pipeline import attributed_definition

# The served SQL, trimmed to the shape that matters.
NET_REVENUE_BY_REGION = """
SELECT r.region_name,
       COALESCE(p_sum.total_captured, 0) - COALESCE(ref_sum.total_refunded, 0) AS net_revenue
FROM ops.regions AS r
LEFT JOIN (SELECT c.region_code, SUM(p.amount) AS total_captured
           FROM ops.payments AS p
           JOIN ops.orders AS o ON p.order_id = o.order_id
           JOIN ops.customers AS c ON o.customer_id = c.customer_id
           GROUP BY 1) AS p_sum ON p_sum.region_code = r.region_code
LEFT JOIN (SELECT c.region_code, SUM(rf.amount) AS total_refunded
           FROM ops.refunds AS rf
           JOIN ops.orders AS o ON rf.order_id = o.order_id
           JOIN ops.customers AS c ON o.customer_id = c.customer_id
           GROUP BY 1) AS ref_sum ON ref_sum.region_code = r.region_code
"""


def _model() -> SemanticModel:
    """Three entities whose amount column shares a name — the real collision."""

    def entity(name: str, table: str, metric: str) -> Entity:
        return Entity(
            name=name,
            source=EntitySource(connection="wh", table=table),
            fields=[Field(name="amount", type="number")],
            metrics=[Metric(name=metric, agg="sum", expr=f"{name}.amount", type="simple")],
        )

    return SemanticModel(
        lens="commercial",
        dialect="duckdb",
        entities=[
            entity("payments", "ops.payments", "total_captured_payments"),
            entity("refunds", "ops.refunds", "total_refunded"),
            entity("marketing_spend", "ops.marketing_spend", "total_marketing_spend"),
        ],
    )


def test_a_metric_on_an_absent_table_is_never_attributed() -> None:
    """The regression, verbatim: marketing_spend appears nowhere in this SQL."""
    assert attributed_definition(NET_REVENUE_BY_REGION, _model()) != "total_marketing_spend"


def test_the_metric_whose_table_is_present_still_attributes() -> None:
    """The filter must only ever REJECT — an alias for a table that IS there
    still matches, which is the qualified-vs-bare case the fallback exists for
    (`payments.amount` against `p.amount`)."""
    assert attributed_definition(NET_REVENUE_BY_REGION, _model()) in {
        "total_captured_payments",
        "total_refunded",
    }


def test_a_bare_column_expression_constrains_nothing() -> None:
    """An expression that qualifies no table cannot be filtered on tables, so
    behaviour there is unchanged — the guard is a filter, never a widener."""
    model = _model()
    model.entities[0].metrics = [Metric(name="bare_sum", agg="sum", expr="amount", type="simple")]
    assert attributed_definition("SELECT SUM(amount) FROM ops.payments", model) == "bare_sum"


def test_select_alias_breaks_sibling_metric_ties() -> None:
    """Sibling metrics differing only in `filters` compile to IDENTICAL bare
    expressions — declaration order used to decide the tie, citing the wrong
    metric's description under correct SQL. The
    compiler emits the chosen metric's name as the SELECT alias; that fact
    outranks expression length."""
    entity = Entity(
        name="molecule",
        source=EntitySource(connection="wh", table="lab.molecule"),
        fields=[Field(name="molecule_id", type="string"), Field(name="label", type="string")],
        metrics=[
            Metric(
                name="carcinogenic_count",
                agg="count",
                expr="molecule.molecule_id",
                type="simple",
                filters=["molecule.label = '+'"],
            ),
            Metric(
                name="non_carcinogenic_count",
                agg="count",
                expr="molecule.molecule_id",
                type="simple",
                filters=["molecule.label = '-'"],
            ),
        ],
    )
    model = SemanticModel(lens="lab", dialect="duckdb", entities=[entity])
    sql = (
        'SELECT COUNT(molecule.molecule_id) AS "non_carcinogenic_count" '
        "FROM lab.molecule AS molecule WHERE molecule.label = '-'"
    )
    assert attributed_definition(sql, model) == "non_carcinogenic_count"


def test_unknown_self_report_never_reaches_basis() -> None:
    from services.runtime.pipeline import _known_term

    model = _model()
    assert _known_term("total_captured_payments", model) == "total_captured_payments"
    assert _known_term("new_customers_hallucinated", model) is None
    assert _known_term(None, model) is None


def test_intent_term_names_only_a_lone_selection() -> None:
    from services.contracts.query_intent import QueryIntent
    from services.runtime.intent_generator import intent_term

    assert intent_term(QueryIntent(metrics=["churned_customers"])) == "churned_customers"
    assert intent_term(QueryIntent(metrics=["a", "b"])) is None
    assert intent_term(QueryIntent(definitions=["active_customer"])) == "active_customer"
    assert intent_term(None) is None
