"""The background LLM description pass fills undocumented columns only —
never overwriting a warehouse/dbt/sampled description — and uses sampled example values
as context. The merge logic is pure (no DB); the store integration rides the
catalog/sampling tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.config import settings
from services.contracts.fakes import ScriptedLLM
from services.contracts.profile import ColumnProfile, TableProfile
from services.lenses import profile_enrich_lm, profile_store
from services.lenses.profile_enrich_lm import _describe_one, _needs_description


def _profile() -> TableProfile:
    return TableProfile(
        connection="jaffle",
        table="orders",
        description=None,
        columns=[
            ColumnProfile(name="id", type="INT"),  # gap
            ColumnProfile(name="status", type="VARCHAR", top_values=["placed", "shipped"]),  # gap
            ColumnProfile(
                name="total",
                type="DECIMAL",
                description="order total in USD",
                description_source="warehouse",
            ),  # already documented
            ColumnProfile(name="customer_email", type="VARCHAR"),  # gap
        ],
    )


def test_needs_description_skips_the_already_documented() -> None:
    by = {c.name: c for c in _profile().columns}
    assert _needs_description(by["id"])
    assert _needs_description(by["status"])
    assert _needs_description(by["customer_email"])
    assert not _needs_description(by["total"])  # warehouse description already present


def test_describe_one_fills_gaps_only() -> None:
    profile = _profile()
    gap_names = {c.name for c in profile.columns if _needs_description(c)}
    llm = ScriptedLLM(
        [
            '{"table": "one row per order", "columns": ['
            '{"name": "id", "description": "surrogate order key"},'
            '{"name": "status", "description": "fulfilment stage"},'
            '{"name": "total", "description": "SHOULD NOT OVERWRITE"},'
            '{"name": "customer_email", "description": "the buyer contact"}]}'
        ]
    )
    out = _describe_one(llm, "fake-model", profile, gap_names)
    by = {c.name: c for c in out.columns}
    assert by["id"].description == "surrogate order key" and by["id"].description_source == "llm"
    assert by["status"].description == "fulfilment stage"
    # an existing warehouse description is preserved, not clobbered
    assert by["total"].description == "order total in USD"
    assert by["total"].description_source == "warehouse"
    assert by["customer_email"].description == "the buyer contact"
    assert out.description == "one row per order"  # table-level gap filled too


def test_describe_one_bad_json_is_noop() -> None:
    profile = _profile()
    gap_names = {c.name for c in profile.columns if _needs_description(c)}
    out = _describe_one(ScriptedLLM(["not json"]), "fake-model", profile, gap_names)
    assert out is profile  # identity-equal → the caller skips the upsert


def _stored_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        profile_store,
        "list_profiles",
        lambda session, name: [SimpleNamespace(profile=_profile())],
    )


def test_off_switch_makes_zero_llm_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """DST_LLM_DESCRIPTIONS=false: the provider seam is never even resolved."""
    monkeypatch.setattr(settings, "llm_descriptions", False)
    _stored_profiles(monkeypatch)

    def _provider() -> None:
        raise AssertionError("a provider was resolved with DST_LLM_DESCRIPTIONS off")

    monkeypatch.setattr(profile_enrich_lm, "assist_llm", _provider)
    out = profile_enrich_lm.run_description_pass(None, "jaffle")  # type: ignore[arg-type]
    assert [p.table for p in out] == ["orders"]  # stored profiles still returned


def test_default_on_reaches_the_provider_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is open by default — otherwise the off-switch test passes vacuously."""
    assert settings.llm_descriptions is True
    _stored_profiles(monkeypatch)
    resolved: list[bool] = []

    def _provider() -> None:
        resolved.append(True)
        return None  # keyless install → no-op after the gate

    monkeypatch.setattr(profile_enrich_lm, "assist_llm", _provider)
    profile_enrich_lm.run_description_pass(None, "jaffle")  # type: ignore[arg-type]
    assert resolved
