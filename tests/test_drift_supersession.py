"""Drift binds *replaces-this*, not only *reads-this*.

The failure this pins: `ops.orders` gains `discount_amount`, the real column
behind a definition that had been deriving the quantity all along. That
definition reads order_items, products, payments and refunds — so every
reads-this rule correctly finds nothing and drift correctly reports "nothing in
semantic/ reads this table", while the metric silently goes from a wrong
derivation to serving zero. Someone can run drift, see the new column, and have
nothing connect it to the definition it obsoletes.

The structural point: **a derivation exists precisely because the real column
did not**, so the asset a new column supersedes systematically does not read the
changed table. The binding that survives is the NAME.
"""

from __future__ import annotations

from services.contracts.semantic_model import Definition
from services.project.warehouse_drift import ProfileDrift, cross_reference, render

DISCOUNTS = {
    "semantic/definitions/discounts.md": Definition(
        term="discount_amount",
        body="The gap between list value and what we collected.",
        sources=["ops.order_items", "ops.products", "ops.payments", "ops.refunds"],
    )
}


def _added(table: str, column: str) -> list[ProfileDrift]:
    return [ProfileDrift(table=table, kind="column_added", detail=column)]


def test_f0_the_named_definition_is_flagged_despite_reading_other_tables() -> None:
    findings = cross_reference(_added("ops.orders", "discount_amount"), {}, DISCOUNTS)
    refs = findings[0].refs
    assert [r.name for r in refs] == ["discount_amount"]
    assert "NAMED for this new column" in refs[0].why
    # The author sees which tables the stale derivation reads, so the review is
    # one glance, not an investigation.
    assert "ops.order_items" in refs[0].why
    rendered = "\n".join(render(findings[0]))
    assert "review whether it is superseded" in rendered
    assert "nothing in semantic/ reads this table" not in rendered


def test_an_unrelated_new_column_still_binds_nothing() -> None:
    """The rule must not turn every new column into noise — no name match, no ref."""
    findings = cross_reference(_added("ops.orders", "warehouse_zone"), {}, DISCOUNTS)
    assert findings[0].refs == []
    assert "nothing in semantic/ reads this table" in render(findings[0])[0]


def test_a_dropped_column_does_not_claim_supersession() -> None:
    """Supersession is a column ARRIVING. A dropped column matching a term is a
    different (reads-this) story and must not borrow this sentence."""
    drift = [ProfileDrift(table="ops.orders", kind="column_dropped", detail="discount_amount")]
    findings = cross_reference(drift, {}, DISCOUNTS)
    assert all("NAMED for this new column" not in r.why for r in findings[0].refs)


def test_a_reads_this_binding_is_not_double_listed() -> None:
    """A definition that already lists the changed table in `sources` binds via
    the existing rule; the supersession rule must not add a second row for it."""
    defs = {
        "semantic/definitions/discounts.md": Definition(
            term="discount_amount",
            body="x",
            sources=["ops.orders"],  # reads the table the column landed on
        )
    }
    findings = cross_reference(_added("ops.orders", "discount_amount"), {}, defs)
    assert len(findings[0].refs) == 1


def test_fold_matching_bridges_spellings() -> None:
    defs = {"semantic/definitions/d.md": Definition(term="Discount Amount", body="x", sources=[])}
    findings = cross_reference(_added("ops.orders", "discount_amount"), {}, defs)
    assert [r.name for r in findings[0].refs] == ["Discount Amount"]
