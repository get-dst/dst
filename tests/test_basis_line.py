"""Every generated answer names the meaning it computed — the middle voice.

Two people ask for "revenue" in their own words and get 17,376,061.96
(`net_revenue`) and 25,864,593.89 (`total_revenue`) — 49% apart, both
`verified` — while no human-facing surface says which meaning answered, even
though `definition_used` is set on both responses. Refusing cleanly when
nothing is computable but guessing silently when something is: this is the
disclosure half of the fix; the apply-time half is `competing_metric_claim`.

The line depends on correct attribution: without it, a revenue query could be
labelled `total_marketing_spend` — a confidently mislabelled answer, strictly
worse than a silent one.
"""

from __future__ import annotations

from services.contracts.semantic_model import (
    Definition,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.runtime.pipeline import _basis_line

MODEL = SemanticModel(
    lens="commercial",
    dialect="duckdb",
    entities=[
        Entity(
            name="order_items",
            source=EntitySource(connection="wh", table="ops.order_items"),
            fields=[Field(name="line_total", type="number")],
            metrics=[
                Metric(
                    name="total_revenue",
                    agg="sum",
                    expr="order_items.line_total",
                    type="simple",
                    description="sum of all line totals",
                )
            ],
        )
    ],
    definitions=[
        Definition(
            term="net_revenue",
            body="Long body text.",
            summary="Money actually collected, minus anything refunded",
        )
    ],
)


def test_a_governed_definition_speaks_in_the_authors_words() -> None:
    line = _basis_line("net_revenue", MODEL)
    assert line is not None
    assert "`net_revenue`" in line
    # The MEANING, not just the identifier — `definition: net_revenue` already
    # existed in the CLI meta line and reconciled nobody.
    assert "Money actually collected, minus anything refunded" in line
    assert "governed definition" in line


def test_an_entity_metric_names_its_entity_and_description() -> None:
    line = _basis_line("total_revenue", MODEL)
    assert line is not None
    assert "`total_revenue`" in line and "`order_items`" in line
    assert "sum of all line totals" in line
    assert "entity metric" in line


def test_the_competing_metric_pair_is_distinguishable_at_a_glance() -> None:
    """Two answers to the same business word, side by side, must explain the gap
    between them without anyone reading SQL."""
    net = _basis_line("net_revenue", MODEL)
    total = _basis_line("total_revenue", MODEL)
    assert net != total
    assert "collected" in str(net) and "line totals" in str(total)


def test_no_attribution_stays_silent() -> None:
    """Fabricating a basis is worse than silence."""
    assert _basis_line(None, MODEL) is None


def test_an_unknown_term_states_only_what_is_known() -> None:
    line = _basis_line("mystery", MODEL)
    assert line == "Computed as `mystery`."
