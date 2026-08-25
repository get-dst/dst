"""stale_after_days — the declared freshness contract rides every rail.

Staleness must be a plain date comparison against a DECLARED contract, never
inferred. The measured floor (`data_as_of`) already rides every answer; this
pins the other half — how old is too old for THIS domain — to the same rails
`dialect` and `timezone` ride: both generation tiers, the compile seam, the
validator, and the serve-time `freshness` check (skip, never a false fail,
when nothing was declared or measured).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.contracts.verification import VerificationCheck
from services.lenses.store import LensBundle
from services.runtime.generator import serialize_model
from services.runtime.intent_generator import serialize_layer
from services.runtime.verification import _with_grade, freshness_check
from services.validate.report import validate_bundle


def _model(days: int | None = None) -> SemanticModel:
    return SemanticModel(
        lens="t",
        dialect="duckdb",
        stale_after_days=days,
        entities=[
            Entity(
                name="orders",
                source=EntitySource(connection="wh", table="ops.orders"),
                fields=[Field(name="amount", type="number")],
                metrics=[Metric(name="revenue", agg="sum", expr="orders.amount", type="simple")],
            )
        ],
    )


def _bundle(model: SemanticModel) -> LensBundle:
    return LensBundle(
        config=LensConfig(name="t", display_name="T", connections=["wh"]),
        semantic_model=model,
    )


def _days_ago(n: int) -> str:
    return (datetime.now(UTC).date() - timedelta(days=n)).isoformat()


def test_a_declared_contract_reaches_both_generation_tiers() -> None:
    for prompt in (serialize_layer(_model(7)), serialize_model(_model(7))):
        assert "stale 7 days" in prompt
        assert "never" in prompt  # the sentence forbids asserting currency


def test_an_undeclared_contract_says_nothing() -> None:
    for prompt in (serialize_layer(_model()), serialize_model(_model())):
        assert "Freshness" not in prompt


def test_config_compiles_into_the_model() -> None:
    cfg = LensConfig(name="t", display_name="T", connections=["wh"], stale_after_days=7)
    assert cfg.stale_after_days == 7
    # Contract default: omitted stays undeclared.
    assert SemanticModel(lens="t", dialect="duckdb").stale_after_days is None


def test_a_nonpositive_contract_is_an_error_at_validation() -> None:
    issues = validate_bundle(_bundle(_model(0)), [], []).issues
    assert any(i.code == "invalid_stale_after" and i.severity == "error" for i in issues)
    issues = validate_bundle(_bundle(_model(7)), [], []).issues
    assert not any(i.code == "invalid_stale_after" for i in issues)


# ── the serve-time check: deterministic, skip-not-fail on missing inputs ─────


def test_fresh_data_passes() -> None:
    assert freshness_check(_days_ago(2), _model(7)).status == "pass"


def test_stale_data_fails_with_the_contract_in_the_reason() -> None:
    check = freshness_check(_days_ago(30), _model(7))
    assert check.status == "fail"
    assert check.reason is not None and "stale after 7 days" in check.reason


def test_undeclared_and_unmeasured_are_skips_never_fails() -> None:
    assert freshness_check(_days_ago(400), _model()).status == "skip"
    assert freshness_check(None, _model(7)).status == "skip"
    assert freshness_check("not-a-date", _model(7)).status == "skip"


def test_a_skip_never_costs_the_grade() -> None:
    checks = [
        VerificationCheck(name="numeric_grounding", status="pass", reason=None),
        freshness_check(None, _model(7)),
    ]
    assert _with_grade(checks, "none", "pass").grade == "verified"


def test_stale_caps_generated_and_certified_at_partial() -> None:
    stale = freshness_check(_days_ago(30), _model(7))
    checks = [VerificationCheck(name="numeric_grounding", status="pass", reason=None), stale]
    # Generated: stale is "some other fail" → partial, never unverified (the SQL
    # is right; the data is old — a different failure class than a wrong answer).
    assert _with_grade(checks, "none", "pass").grade == "partial"
    # Certified: approval vouches for the SQL, not for month-old data.
    assert _with_grade(checks, "certified", "pass").grade == "partial"
