"""A name the question states binds in a dimension of thousands of values, in both lanes.

The failure: "How many matches has Team Spirit played on the current patch?"
failed the slot lane with "Which value of 'team_name'… supply it as a binding",
while the same kind of question served with ``team_name = 'Team Spirit'``. The
two lanes build one resolver from the same stored profile; neither could bind
the name, because the probe kept a dimension's whole dictionary only up to 500
values and ``team_name`` holds 1,662. The served answer came from the raw-SQL
escalation, and ``dst query`` never printed its UNTYPED line, so it read as a
typed binding.

Pinned here: the probe keeps a declared dimension's whole dictionary well past a
thousand values, a question naming one of them verbatim types (the slot lane
passes and the live generator binds the filter, from the same dictionaries), and
``dst query`` prints the disclosures a served answer carries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest

from services.certify.store import CertifiedAnswer
from services.connectors.duckdb import DuckDBConnector
from services.contracts.fakes import ScriptedDecider
from services.contracts.profile import ColumnSampleSpec, TableSampleSpec
from services.contracts.protocols import Decision
from services.contracts.semantic_model import (
    Dimension,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.evals import slot_lane
from services.runtime import value_guard
from services.runtime.typed_resolver import TypedIntentGenerator, TypedResolver, named_values

TEAMS = 1_200
QUESTION = "How many matches has Team Spirit played?"

MODEL = SemanticModel(
    lens="pro",
    dialect="duckdb",
    entities=[
        Entity(
            name="team_matches",
            source=EntitySource(connection="wh", table="team_matches"),
            fields=[Field(name="match_id", type="integer"), Field(name="team_name", type="string")],
            dimensions=[Dimension(name="team_name")],
            metrics=[Metric(name="matches_played", agg="count", expr="team_matches.match_id")],
        )
    ],
)


class _Prefers(ScriptedDecider):
    """Order-proof: the first preferred name on offer, else none."""

    def __init__(self, *prefer: str) -> None:
        super().__init__([])
        self._prefer = prefer

    def decide(self, *, question, context, options, allow_none):  # type: ignore[no-untyped-def]
        names = [o.name for o in options]
        chosen = next((p for p in self._prefer if p in names), None)
        return Decision(chosen=chosen, probs={chosen or "none": 1.0}, provider="v")


def _probed(tmp_path: Path) -> Any:
    """The table profile `dst probe` writes for a warehouse with TEAMS team names."""
    path = tmp_path / "wh.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute(
            "CREATE TABLE team_matches AS SELECT i AS match_id, "
            f"CASE WHEN i % {TEAMS} = 7 THEN 'Team Spirit' ELSE 'Team ' || (i % {TEAMS}) END "
            f"AS team_name FROM range({TEAMS * 4}) AS t(i)"
        )
    (profile,) = DuckDBConnector(str(path)).sample_profile(
        [
            TableSampleSpec(
                table="team_matches",
                row_count=TEAMS * 4,
                columns=[ColumnSampleSpec(name="team_name", type="VARCHAR", matchable=True)],
            )
        ]
    )
    return profile


def test_the_probe_keeps_a_dimensions_whole_dictionary_past_a_thousand_values(
    tmp_path: Path,
) -> None:
    (column,) = _probed(tmp_path).columns
    assert column.values_complete
    assert len(column.top_values) == TEAMS
    assert "Team Spirit" in column.top_values


def test_a_name_the_question_states_binds_in_the_slot_lane_and_live(tmp_path: Path) -> None:
    profiles = [_probed(tmp_path)]
    # Both lanes read these, through the same assembly seam.
    domains = value_guard.value_domains(MODEL, profiles)
    per_entity = value_guard.entity_domains(MODEL, profiles)

    def resolver() -> TypedResolver:
        return TypedResolver(
            _Prefers("aggregate", "matches_played", "team_name"),
            domains=domains,
            entity_domains=per_entity,
        )

    gold = {
        "method": "construction",
        "tag": "declared",
        "slots": [{"kind": "metric", "name": "matches_played", "source": "declared"}],
    }
    answer = CertifiedAnswer("a1", "pro", QUESTION, "SELECT 1", "t", "2026-01-01", resolution=gold)
    (case,) = slot_lane.run_slot_lane([answer], MODEL, lambda _a: resolver()).cases
    assert case.typed, case.evidence
    assert case.verdict == "passed", case.evidence

    live = TypedIntentGenerator(resolver()).generate(
        question=QUESTION, semantic_model=MODEL, prose_context=[], dialect="duckdb"
    )
    assert live.clarification is None, live.clarification
    assert "team_matches.team_name = 'Team Spirit'" in live.sql


def test_matching_a_long_dictionary_finds_exactly_what_the_short_one_does() -> None:
    """The matcher skips values the question cannot contain before reaching for
    a regex; what it finds is unchanged, word bounds included."""
    teams = [f"Team {i}" for i in range(5_000)] + ["Team Spirit", "Spirit", "IN", "carry"]
    domains = {"team_name": teams}
    assert named_values(QUESTION, domains) == [{"team_name": "Team Spirit"}]
    assert named_values("Team Spirits of the past", domains) == []  # a name has no plural
    assert named_values("Spirit against Team Spirit", domains) == [
        {"team_name": "Spirit"},
        {"team_name": "Team Spirit"},
    ]
    assert named_values("which carries in IN", domains) == [
        {"team_name": "carry"},
        {"team_name": "IN"},
    ]


# ── dst query prints what the answer discloses ───────────────────────────────


def _run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    import sys

    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


def test_dst_query_prints_an_untyped_disclosure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The escalation's UNTYPED line rides `degraded`; a CLI that dropped it made
    a raw-SQL answer look like a typed binding."""
    untyped = (
        "UNTYPED: served by raw-SQL generation — the question did not type: "
        "Which value of 'team_name' should the answer filter on?"
    )

    def fake_post(url, headers=None, json=None, timeout=None, params=None):  # type: ignore[no-untyped-def]
        return httpx.Response(
            200,
            json={
                "lens": "pro",
                "answer": "Team Spirit played 275 matches.",
                "sql": "SELECT COUNT(*) FROM team_matches WHERE team_name = 'Team Spirit'",
                "confidence": "verified",
                "certification": "none",
                "degraded": [untyped],
            },
            request=httpx.Request("POST", "http://x"),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    assert _run_cli(monkeypatch, ["query", "pro", QUESTION, "--token", "dstadm_t"]) == 0
    assert untyped in capsys.readouterr().out
