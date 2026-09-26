"""Numbers reach the person as the machine holds them.

A hero's win rate composed as 0.47085201793721976, a median minute as
13.666441441441439: the composer was handed the result rows as `str(cell)` and
told to copy the digits exactly, and the reconcile pass then rewrote whatever
rounding the model did make back to the same raw double. Presentation is dst's:
the rows the model reads carry ONE rendering of each number — a declared rate in
[0, 1] as a one-decimal percentage, other fractions at four significant figures,
integers with separators — and reconcile snaps prose to that same rendering. The
machine-readable rows in the response are untouched: `data` stays exact.
"""

from __future__ import annotations

from decimal import Decimal

from services.contracts.protocols import GeneratedQuery
from services.contracts.semantic_model import (
    Dimension,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.contracts.warehouse import QueryResult
from services.runtime.answer import certified_frame, column_styles, compose_prompt, render_cell
from services.runtime.faithfulness import reconcile, render_number

_MODEL = SemanticModel(
    lens="dota",
    dialect="duckdb",
    entities=[
        Entity(
            name="hero_stats",
            source=EntitySource(connection="wh", table="hero_stats"),
            fields=[
                Field(name="hero_id", type="integer"),
                Field(name="hero_name", type="string"),
                Field(name="year", type="integer"),
                Field(name="is_win", type="boolean"),
                Field(name="completion_minute", type="number"),
                Field(name="gold", type="number"),
            ],
            dimensions=[Dimension(name="hero_name"), Dimension(name="hero_id", type="integer")],
            metrics=[
                Metric(name="games", agg="count"),
                Metric(
                    name="wins", agg="sum", expr="CASE WHEN hero_stats.is_win THEN 1 ELSE 0 END"
                ),
                Metric(
                    name="win_rate",
                    type="ratio",
                    numerator="wins",
                    denominator="games",
                    format="percent",
                ),
                Metric(name="kd_ratio", type="ratio", numerator="wins", denominator="games"),
                Metric(name="median_minute", agg="avg", expr="hero_stats.completion_minute"),
                Metric(name="gold_total", agg="sum", expr="hero_stats.gold", format="currency"),
            ],
        )
    ],
)

_RAW_RATE = 0.47085201793721976
_RAW_MINUTE = 13.666441441441439


def test_render_number_is_one_presentation_per_value() -> None:
    # a declared rate in [0, 1] is a one-decimal percentage
    assert render_number(_RAW_RATE, percent=True) == "47.1%"
    assert render_number(0.5789473684210527, percent=True) == "57.9%"
    assert render_number(1.0, percent=True) == "100.0%"
    assert render_number(0, percent=True) == "0.0%"
    # …and a ratio past 1 is a number, not a percentage
    assert render_number(1.7, percent=True) == "1.7"
    # other fractions: four significant figures, fixed notation, no trailing zeros
    assert render_number(_RAW_MINUTE) == "13.67"
    assert render_number(_RAW_RATE) == "0.4709"
    assert render_number(0.5) == "0.5"
    assert render_number(0.000012) == "0.000012"
    assert "e" not in render_number(1.234567e-7)
    assert render_number(-13.666) == "-13.67"
    assert render_number(Decimal("38.813651137594796")) == "38.81"
    # integers keep their separators; integral floats are integers
    assert render_number(12345) == "12,345"
    assert render_number(12345.0) == "12,345"
    # at separator scale a fraction keeps cents, as before
    assert render_number(1234.5678) == "1,234.57"
    assert render_number(88759841.48000014) == "88,759,841.48"


def test_render_cell_leaves_text_bools_and_identifiers_as_stored() -> None:
    assert render_cell("0.47085201793721976", "number") == "0.47085201793721976"  # a code
    assert render_cell(True, "percent") == "True"
    assert render_cell(None, "number") == "NULL"
    assert render_cell(1234567, "stored") == "1234567"  # an id, never 1,234,567
    assert render_cell(_RAW_MINUTE, "money") == "13.67"


def test_column_styles_come_from_the_declarations() -> None:
    styles = column_styles(
        _MODEL,
        ["hero_name", "win_rate", "kd_ratio", "median_minute", "gold_total", "hero_id", "year"],
    )
    assert styles == ["number", "percent", "percent", "number", "money", "stored", "stored"]
    # identifier-shaped names the model never declared stay as stored too
    assert column_styles(_MODEL, ["match_id", "id", "n"]) == ["stored", "stored", "number"]


def test_the_composer_reads_the_presentation_and_the_rows_stay_exact() -> None:
    result = QueryResult(
        columns=["hero_name", "win_rate", "median_minute", "games", "hero_id"],
        rows=[["Anti-Mage", _RAW_RATE, _RAW_MINUTE, 12345, 1234567]],
    )
    _, user = compose_prompt(
        question="Anti-Mage's win rate in the Divine bracket?",
        generated=GeneratedQuery(sql="SELECT ..."),
        result=result,
        semantic_model=_MODEL,
    )
    assert "Anti-Mage | 47.1% | 13.67 | 12,345 | 1234567" in user
    assert str(_RAW_RATE) not in user and str(_RAW_MINUTE) not in user
    # the API's rows are the machine's: untouched
    assert result.rows == [["Anti-Mage", _RAW_RATE, _RAW_MINUTE, 12345, 1234567]]


def test_reconcile_snaps_a_rounded_claim_to_the_presentation_not_the_raw_double() -> None:
    result = QueryResult(columns=["median_minute"], rows=[[_RAW_MINUTE]])
    assert reconcile("The median minute is 13.67.", result) == "The median minute is 13.67."
    # a verbatim quote of the residue rewrites to the same presentation
    assert reconcile(f"The median minute is {_RAW_MINUTE}.", result) == (
        "The median minute is 13.67."
    )
    rate = QueryResult(columns=["win_rate"], rows=[[_RAW_RATE]])
    assert reconcile("The win rate is 47.1%.", rate) == "The win rate is 47.1%."
    assert reconcile("The win rate is 0.47.", rate) == "The win rate is 0.4709."


def test_the_certified_frame_renders_the_same_presentation() -> None:
    result = QueryResult(
        columns=["hero_name", "win_rate", "median_minute", "gold_total", "hero_id"],
        rows=[["Anti-Mage", _RAW_RATE, _RAW_MINUTE, 1234.5, 1234567], ["Lion", None, 9.0, 2, 7]],
    )
    frame = certified_frame("win rates?", result, _MODEL)
    assert "Anti-Mage | 47.1% | 13.67 | 1234.50 | 1234567" in frame
    assert "Lion | NULL | 9 | 2.00 | 7" in frame
