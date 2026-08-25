"""The reading-disclosure floor beneath the clarify rail.

Aliases cannot enumerate language: paraphrases carrying no alias string
produced three different populations with a 4x spread, all status ok, no
disclosure. When the served SQL identifiably used ONE declared reading of an
ambiguous term, the answer now says which — every rail miss becomes a
disclosed reading instead of a silent one. Detection is structural (the
mapping tails name columns/tables; the SQL references them or not), never
linguistic, and the disclosure text carries no digits so it can never trip
numeric grounding.
"""

from __future__ import annotations

import re

from services.contracts.semantic_model import (
    Definition,
    Entity,
    EntitySource,
    Field,
    SemanticModel,
)
from services.runtime.pipeline import ambiguity_disclosure


def _model() -> SemanticModel:
    return SemanticModel(
        lens="sdr",
        dialect="bigquery",
        entities=[
            Entity(
                name="leads",
                source=EntitySource(connection="c", table="p.d.leads"),
                fields=[
                    Field(name="is_eligible_outbound_lead", type="boolean"),
                    Field(name="has_phone", type="boolean"),
                    Field(name="prospect_status", type="string"),
                    Field(name="account_id", type="string"),
                ],
            )
        ],
        definitions=[
            Definition(
                term="sdr_metrics",
                body="Four eligibility pools exist.",
                status="ambiguous",
                possible_mappings=[
                    "eligible outbound pool - leads.is_eligible_outbound_lead",
                    "callable list - leads.has_phone",
                    "prospect accounts - leads.prospect_status",
                ],
            )
        ],
    )


def test_one_matched_reading_is_disclosed_with_the_alternatives() -> None:
    note = ambiguity_disclosure(
        "SELECT COUNT(*) FROM p.d.leads AS leads WHERE leads.is_eligible_outbound_lead = TRUE",
        _model(),
    )
    assert note is not None
    assert "eligible outbound pool" in note
    assert "callable list" in note and "prospect accounts" in note
    assert "sdr_metrics" in note


def test_the_disclosure_carries_no_digits() -> None:
    # A disclosure must never become a numeric claim the grounding check
    # fails — the fix for one withheld-prose cause must not create another.
    note = ambiguity_disclosure(
        "SELECT COUNT(*) FROM p.d.leads AS leads WHERE leads.has_phone = TRUE", _model()
    )
    assert note is not None and re.search(r"\d", note) is None


def test_sql_touching_no_mapping_is_not_annotated() -> None:
    note = ambiguity_disclosure(
        "SELECT COUNT(DISTINCT leads.account_id) FROM p.d.leads AS leads", _model()
    )
    assert note is None


def test_sql_matching_two_readings_stays_silent() -> None:
    # Two matches = no unambiguous attribution; a guessed disclosure would be
    # a new lie. Under-disclose, never mis-disclose.
    note = ambiguity_disclosure(
        "SELECT COUNT(*) FROM p.d.leads AS leads "
        "WHERE leads.is_eligible_outbound_lead = TRUE AND leads.has_phone = TRUE",
        _model(),
    )
    assert note is None


def test_unparseable_sql_never_crashes_the_serve() -> None:
    assert ambiguity_disclosure("NOT SQL ((", _model()) is None


def test_a_question_naming_the_reading_is_not_annotated() -> None:
    """The clarify rail's escape hatch, mirrored: an explicit ask already
    disclosed itself — annotating every 'eligible outbound pool' question
    would make the disclosure noise."""
    note = ambiguity_disclosure(
        "SELECT COUNT(*) FROM p.d.leads AS leads WHERE leads.is_eligible_outbound_lead = TRUE",
        _model(),
        question="How big is the eligible outbound pool right now?",
    )
    assert note is None
