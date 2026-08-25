"""A lens can declare its business clock, and the whole pipe honours it.

Without a declared clock, a complete month and a partial one get compared as a
"sharp drop" with no period framing; and where UTC events sit under a
locally-dated ERP, "how many signups yesterday" moves by up to the offset's
worth of traffic depending on whose day "yesterday" is.

An authored fact that reaches only one generation tier does not exist, so the
timezone is pinned on BOTH prompts, the compile seam, the composer, and the
validator — the same rails `dialect` rides.
"""

from __future__ import annotations

from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.lenses.store import LensBundle
from services.runtime.generator import serialize_model
from services.runtime.intent_generator import serialize_layer
from services.validate.report import validate_bundle


def _model(tz: str = "") -> SemanticModel:
    return SemanticModel(
        lens="t",
        dialect="duckdb",
        timezone=tz,
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


def test_a_declared_zone_reaches_both_generation_tiers() -> None:
    """One tier rendering it and one dropping it is how a declared fact goes
    missing from half the answers."""
    for prompt in (serialize_layer(_model("Europe/Oslo")), serialize_model(_model("Europe/Oslo"))):
        assert "Europe/Oslo" in prompt
        assert "yesterday" in prompt  # the sentence says what the zone steers


def test_an_undeclared_zone_says_nothing() -> None:
    """No declaration, no line — an invented default clock would be a silent
    assumption, which is the exact class this feature exists to remove."""
    for prompt in (serialize_layer(_model()), serialize_model(_model())):
        assert "Timezone" not in prompt


def test_both_generation_tiers_state_the_current_date() -> None:
    """Computing the clock for GRADING but never telling the GENERATOR means
    'this year' resolves against the model's training prior, and no authoring
    lever reaches the metric pass — explicit instructions, a prepended date and
    exemplars all leave the stale year literal standing. The date is a fact, and
    a fact that reaches one tier does not exist — same rail as the timezone
    above."""
    from datetime import UTC, datetime

    today = datetime.now(UTC).date().isoformat()
    for prompt in (serialize_layer(_model()), serialize_model(_model())):
        assert f"Today: {today}" in prompt
        assert "never against your own prior" in prompt


def test_the_stated_date_is_the_lens_clock_not_utc() -> None:
    # A zone 12+ hours ahead of UTC disagrees with UTC's date for part of every
    # day; the prompt must state the BUSINESS day. (Deterministic assertion:
    # whatever the wall clock, the rendered date equals the zone's date.)
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = "Pacific/Auckland"
    expected = datetime.now(ZoneInfo(tz)).date().isoformat()
    for prompt in (serialize_layer(_model(tz)), serialize_model(_model(tz))):
        assert f"Today: {expected}" in prompt


def test_config_timezone_compiles_into_the_model() -> None:
    cfg = LensConfig(name="t", display_name="T", connections=["wh"], timezone="Europe/Oslo")
    assert cfg.timezone == "Europe/Oslo"
    # The compile seam threads config.timezone -> model.timezone (compile.py);
    # pin the contract default so an omitted field stays undeclared, not None.
    assert SemanticModel(lens="t", dialect="duckdb").timezone == ""


def test_an_invalid_zone_is_an_error_at_validation() -> None:
    """'Europe/Olso' fails nothing loudly at serve time — the model would just
    improvise a clock. It must die at plan."""
    issues = validate_bundle(_bundle(_model("Europe/Olso")), [], []).issues
    assert any(i.code == "invalid_timezone" and i.severity == "error" for i in issues)


def test_a_valid_zone_raises_no_issue() -> None:
    issues = validate_bundle(_bundle(_model("Europe/Oslo")), [], []).issues
    assert not any(i.code == "invalid_timezone" for i in issues)
