"""Shape guarantee: SQL composing a dropped shared-layer metric from raw columns
refuses instead of serving unverified. A lens carrying only `session_count` used
to serve a self-composed conversion rate (COUNT GROUPed BY the converted flag,
division in prose).

Both directions are load-bearing, filter_guard's twin lesson most of all: a
shape a SELECTED metric legitimately owns — bare, sliced by a selected field,
or broken down by an unrelated dimension — is NEVER flagged."""

from __future__ import annotations

import pytest

from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.runtime.shape_guard import composed_excluded_shapes, refusal_reason

_CONVERTED = Metric(
    name="converted_sessions",
    agg="count",
    expr="sessions.session_id",
    filters=["sessions.converted = true"],
)
_RATE = Metric(
    name="conversion_rate",
    type="ratio",
    numerator="converted_sessions",
    denominator="session_count",
)


def _act6e_model(
    *,
    selected: list[Metric] | None = None,
    shapes: list[Metric] | None = None,
) -> SemanticModel:
    """The lens under test: sessions entity whose selection carries only
    `session_count`; the shared layer also defines the governed numerator and
    the conversion_rate ratio, both dropped."""
    return SemanticModel(
        lens="web",
        dialect="duckdb",
        entities=[
            Entity(
                name="sessions",
                source=EntitySource(connection="wh", table="web_sessions"),
                fields=[
                    Field(name="session_id", type="string"),
                    Field(name="converted", type="boolean"),
                    Field(name="device", type="string"),
                ],
                metrics=(
                    selected
                    if selected is not None
                    else [Metric(name="session_count", agg="count", expr="sessions.session_id")]
                ),
            )
        ],
        excluded_metrics=["conversion_rate", "converted_sessions"],
        excluded_metric_shapes={
            "sessions": shapes if shapes is not None else [_CONVERTED.model_copy(), _RATE]
        },
    )


_ACT6E_SQL = (
    "SELECT sessions.converted, COUNT(sessions.session_id) AS session_count "
    "FROM web_sessions AS sessions GROUP BY sessions.converted"
)


def test_act6e_grouped_flag_composes_the_ratio() -> None:
    """The composition shape: the denominator's aggregation GROUPed BY the
    numerator's filter column hands the model both operands to divide in prose."""
    assert composed_excluded_shapes(_ACT6E_SQL, _act6e_model()) == ["conversion_rate"]


def test_group_by_ordinal_still_composes() -> None:
    sql = "SELECT converted, COUNT(session_id) AS n FROM web_sessions GROUP BY 1"
    assert composed_excluded_shapes(sql, _act6e_model()) == ["conversion_rate"]


def test_numerator_denominator_pair_composes() -> None:
    sql = (
        "SELECT COUNT(CASE WHEN sessions.converted = true THEN sessions.session_id END) AS c, "
        "COUNT(sessions.session_id) AS n FROM web_sessions AS sessions"
    )
    assert composed_excluded_shapes(sql, _act6e_model()) == ["conversion_rate"]


def test_explicit_division_composes() -> None:
    sql = (
        "SELECT COUNT(CASE WHEN sessions.converted THEN sessions.session_id END) * 1.0 "
        "/ COUNT(sessions.session_id) AS rate FROM web_sessions AS sessions"
    )
    assert composed_excluded_shapes(sql, _act6e_model()) == ["conversion_rate"]


@pytest.mark.parametrize(
    "sql",
    [
        # the selected metric itself, bare
        "SELECT COUNT(sessions.session_id) AS n FROM web_sessions AS sessions",
        # broken down by an unrelated dimension
        "SELECT sessions.device, COUNT(sessions.session_id) FROM web_sessions AS sessions "
        "GROUP BY sessions.device",
        # sliced by the flag as an ordinary filter — a selected metric over a
        # selected field, NOT a composition (the twin lesson: an unfiltered
        # selected twin legitimizes its narrowings)
        "SELECT COUNT(sessions.session_id) FROM web_sessions AS sessions "
        "WHERE sessions.converted = true",
        # no aggregation at all
        "SELECT sessions.device FROM web_sessions AS sessions",
        # a different aggregation entirely
        "SELECT COUNT(DISTINCT sessions.session_id) FROM web_sessions AS sessions",
    ],
)
def test_selected_shapes_are_never_flagged(sql: str) -> None:
    assert composed_excluded_shapes(sql, _act6e_model()) == []


def test_excluded_unfiltered_simple_metric_flags_the_bare_form() -> None:
    """The mirror case: the curator kept the governed numerator and dropped the
    bare count — the bare form now composes the dropped metric, while the
    guarded form stays the selected metric's own shape."""
    model = _act6e_model(
        selected=[_CONVERTED.model_copy()],
        shapes=[
            Metric(name="session_count", agg="count", expr="sessions.session_id"),
            _RATE.model_copy(),
        ],
    )
    bare = "SELECT COUNT(sessions.session_id) AS n FROM web_sessions AS sessions"
    assert composed_excluded_shapes(bare, model) == ["session_count"]
    guarded = (
        "SELECT COUNT(CASE WHEN sessions.converted = true THEN sessions.session_id END) AS c "
        "FROM web_sessions AS sessions"
    )
    assert composed_excluded_shapes(guarded, model) == []


def test_ratio_of_two_selected_metrics_is_not_flagged() -> None:
    """Both components selected → two selected metrics side by side; the
    composition boundary only covers ungoverned ingredients."""
    model = _act6e_model(
        selected=[
            Metric(name="session_count", agg="count", expr="sessions.session_id"),
            _CONVERTED.model_copy(),
        ],
        shapes=[_RATE.model_copy()],
    )
    sql = (
        "SELECT COUNT(CASE WHEN sessions.converted = true THEN sessions.session_id END) AS c, "
        "COUNT(sessions.session_id) AS n FROM web_sessions AS sessions"
    )
    assert composed_excluded_shapes(sql, model) == []


def test_pre_shape_bundle_is_a_no_op() -> None:
    model = _act6e_model(shapes=[])
    model.excluded_metric_shapes = {}
    assert composed_excluded_shapes(_ACT6E_SQL, model) == []


def test_unparseable_sql_is_not_this_modules_problem() -> None:
    assert composed_excluded_shapes("SELECT WHERE FROM (((", _act6e_model()) == []


# ── the decline-with-path refusal text ───────────────────────────────────────


def test_refusal_names_metric_and_the_file_first_path() -> None:
    reason = refusal_reason(["conversion_rate"], None)
    assert "'conversion_rate'" in reason
    assert "add 'conversion_rate' to this lens's selection in lens.yaml" in reason
    assert "certify this answer" in reason


def test_sibling_hint_appears_only_when_exactly_one_lens_carries_it() -> None:
    one = refusal_reason(["conversion_rate"], lambda _m: ["growth"])
    assert "lens 'growth' carries 'conversion_rate'" in one
    for carriers in ([], ["growth", "marketing"]):
        reason = refusal_reason(["conversion_rate"], lambda _m: carriers)  # noqa: B023
        assert "carries" not in reason


def test_broken_carrier_lookup_never_breaks_the_refusal() -> None:
    def boom(_m: str) -> list[str]:
        raise RuntimeError("registry down")

    reason = refusal_reason(["conversion_rate"], boom)
    assert "'conversion_rate'" in reason and "carries" not in reason


def test_named_entry_point_shares_the_contract_with_its_own_lead_in() -> None:
    """One doctrine, two entry points: the by-name refusal is
    the SAME builder — same fix, same sibling hint — differing only in how the
    boundary was hit."""
    named = refusal_reason(["conversion_rate"], lambda _m: ["growth"], named=True)
    assert "the question asks for 'conversion_rate' by name" in named
    assert "add 'conversion_rate' to this lens's selection in lens.yaml" in named
    assert "certify this answer" in named
    assert "lens 'growth' carries 'conversion_rate'" in named
    composed = refusal_reason(["conversion_rate"], lambda _m: ["growth"])
    assert "would compose 'conversion_rate' from raw columns" in composed
    # everything after the lead-in is byte-identical — the shared contract
    tail = named.split("by name", 1)[1]
    assert tail == composed.split("from raw columns", 1)[1]
