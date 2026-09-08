"""The deterministic rails read NAMES, never filter VALUES.

The structured metrics door has no question, so it renders the intent back into
words for the request log. That rendering was then handed to the pipeline AS the
caller's question — and the three deterministic pre-checks (drop-exclusion,
not-computable refusal, ambiguity clarification) are plain word-boundary scans.
So a customer's own row label could speak for the caller:

    filters=[group_account_name in ['Revenue (SE)', 'Revenue (FI, all lines)']]

made every call clarify on the governed ambiguous term "revenue", forever, while
the identical call without filters answered fine. A nonsense value
('revenue test xyz') triggered it too; a value with no governed term in it
('Intercompany sales') did not — the tell that the trigger was substring, not
meaning. The same hole could force a REJECT or a REFUSAL.

The log keeps the values (that is what makes the structured door auditable);
only the rails switch to names.
"""

from __future__ import annotations

from services.api.query import describe_intent, intent_scan_text
from services.contracts.query_intent import IntentFilter, IntentOrder, QueryIntent


def _intent(**kw: object) -> QueryIntent:
    base: dict[str, object] = {
        "metrics": ["amount"],
        "dimensions": ["month", "group_account_name"],
        "filters": [],
        "definitions": [],
        "order_by": [],
    }
    base.update(kw)
    return QueryIntent.model_validate(base)


def test_filter_values_never_reach_the_scan_text() -> None:
    """The exact field repro: a governed term inside a filter VALUE."""
    intent = _intent(
        filters=[
            IntentFilter(
                field="group_account_name", op="in", value=["Revenue (SE)", "Revenue (FI)"]
            )
        ]
    )
    scanned = intent_scan_text(intent)
    assert "revenue" not in scanned.lower(), scanned
    # The field NAME survives — a caller filtering on it is still naming it.
    assert "group_account_name" in scanned

    # ...while the log keeps the values, which is the point of describe_intent.
    logged = describe_intent(intent)
    assert "Revenue (SE)" in logged


def test_nonsense_values_are_not_a_question_either() -> None:
    intent = _intent(filters=[IntentFilter(field="note", op="=", value="revenue test xyz")])
    assert "revenue" not in intent_scan_text(intent).lower()


def test_named_metrics_dimensions_and_definitions_still_scan() -> None:
    """Naming a term IS asking about it — the rails must still see those."""
    intent = _intent(
        metrics=["revenue"],
        dimensions=["value_band"],
        definitions=["lifetime_value"],
        order_by=[IntentOrder(field="revenue", dir="desc")],
    )
    scanned = intent_scan_text(intent).lower()
    assert "revenue" in scanned
    assert "value_band" in scanned
    assert "lifetime_value" in scanned


def test_order_by_direction_is_not_scanned_as_content() -> None:
    """`desc` is syntax, not vocabulary — keep the scan text free of it."""
    intent = _intent(order_by=[IntentOrder(field="month", dir="desc")])
    assert " desc" not in intent_scan_text(intent)
    assert "month" in intent_scan_text(intent)
