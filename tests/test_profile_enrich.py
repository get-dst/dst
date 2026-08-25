"""Profile facts feed generation — enum literals + partition
hints land in the system prompt; answers state a measured data_as_of.
Compact shape stats (null rate, distinct count, ranges, row counts) too."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.fakes import ScriptedLLM, fake_llm_providers
from services.contracts.profile import ColumnProfile, PartitioningProfile, TableProfile
from services.contracts.semantic_model import (
    Entity,
    EntitySource,
    Field,
    FieldType,
    SemanticModel,
)
from services.db.session import org_session
from services.lenses import profile_enrich, profile_store
from services.lenses import store as lens_store
from services.lenses.demo import jaffle_customer_value, jaffle_customer_value_bundle
from services.runtime.generator import GroundedSQLGenerator, serialize_model

client = TestClient(app)


def _profiles() -> list[TableProfile]:
    return [
        TableProfile(
            connection="jaffle",
            table="customers",
            row_count=100,
            partitioning=PartitioningProfile(column="first_order_date", kind="time"),
            last_updated_logical=datetime(2026, 6, 8, tzinfo=UTC),
            columns=[
                ColumnProfile(
                    name="customer_name",
                    type="VARCHAR",
                    description="Full display name",
                    description_source="sampled",
                ),
                ColumnProfile(
                    name="number_of_orders",
                    type="BIGINT",
                    top_values=["0", "1", "2", "3"],
                    values_complete=True,
                    is_low_cardinality=True,
                ),
            ],
        )
    ]


def test_enrich_model_lands_in_the_system_prompt() -> None:
    model = profile_enrich.enrich_model(jaffle_customer_value(), _profiles())
    prompt = serialize_model(model)
    assert "Values: '0', '1', '2', '3'" in prompt  # the enum glossary
    assert "Partitioned by first_order_date" in prompt  # the pruning hint
    assert "Full display name" in prompt  # sampled description fills the gap
    # Row count rides on the entity description, before the pruning hint.
    assert "One row per customer. (~100 rows) Partitioned by first_order_date" in prompt


def test_enrich_is_a_noop_without_matching_profiles() -> None:
    model = jaffle_customer_value()
    assert profile_enrich.enrich_model(model, []) is model


# ── compact shape stats reach the field/entity descriptions ──────────────────


def _enrich_one(
    cp: ColumnProfile,
    *,
    field_type: FieldType = "string",
    field_desc: str | None = "Base",
    row_count: int | None = None,
    entity_desc: str | None = None,
    table_desc: str | None = None,
) -> tuple[str | None, str | None]:
    """Enrich a one-field model with one profile; return (field desc, entity desc)."""
    model = SemanticModel(
        lens="l",
        dialect="duckdb",
        entities=[
            Entity(
                name="t",
                source=EntitySource(connection="c", table="t"),
                description=entity_desc,
                fields=[Field(name=cp.name, type=field_type, description=field_desc)],
            )
        ],
    )
    prof = TableProfile(
        connection="c", table="t", description=table_desc, row_count=row_count, columns=[cp]
    )
    enriched = profile_enrich.enrich_model(model, [prof])
    return enriched.entities[0].fields[0].description, enriched.entities[0].description


def test_null_rate_renders_at_the_threshold_only() -> None:
    assert _enrich_one(ColumnProfile(name="f", type="VARCHAR", null_rate=0.04))[0] == "Base"
    assert _enrich_one(ColumnProfile(name="f", type="VARCHAR", null_rate=0.0))[0] == "Base"
    got = _enrich_one(ColumnProfile(name="f", type="VARCHAR", null_rate=0.05))[0]
    assert got == "Base — ~5% null"
    got = _enrich_one(ColumnProfile(name="f", type="VARCHAR", null_rate=0.123))[0]
    assert got == "Base — ~12% null"
    # a stat with no description to hang on becomes the description
    got = _enrich_one(ColumnProfile(name="f", type="VARCHAR", null_rate=0.5), field_desc=None)[0]
    assert got == "~50% null"


def test_distinct_count_renders_only_without_top_values() -> None:
    exact = ColumnProfile(name="f", type="VARCHAR", distinct_count=12000, distinct_is_exact=True)
    assert _enrich_one(exact)[0] == "Base — distinct: 12000"
    got = _enrich_one(
        ColumnProfile(
            name="f",
            type="VARCHAR",
            distinct_count=2,
            top_values=["a", "b"],
            values_complete=True,
        )
    )[0]
    assert got == "Base — Values: 'a', 'b'"  # literals beat the count


def test_estimates_never_reach_the_prompt_unqualified() -> None:
    """A sampled cardinality is a lower bound and a sampled value list is partial —
    a model told `Values: 'a','b'` writes `IN ('a','b')` and silently drops rows."""
    sampled = ColumnProfile(name="f", type="VARCHAR", distinct_count=12000)
    assert _enrich_one(sampled)[0] == "Base — distinct: >=12000"
    partial = ColumnProfile(
        name="f",
        type="VARCHAR",
        distinct_count=2,
        top_values=["a", "b"],  # values_complete=False
    )
    assert _enrich_one(partial)[0] == "Base — Values (partial): 'a', 'b'"


def test_range_renders_only_for_ordered_types() -> None:
    cp = ColumnProfile(name="f", type="DATE", min="2020-01-01", max="2026-06-30")
    ordered: tuple[FieldType, ...] = ("date", "timestamp", "number", "integer")
    for ft in ordered:
        assert _enrich_one(cp, field_type=ft)[0] == "Base — range: 2020-01-01..2026-06-30"
    unordered: tuple[FieldType, ...] = ("string", "boolean", "json")
    for ft in unordered:
        assert _enrich_one(cp, field_type=ft)[0] == "Base"
    # one bound alone is not a range
    one_bound = ColumnProfile(name="f", type="DATE", min="2020-01-01")
    assert _enrich_one(one_bound, field_type="date")[0] == "Base"


def test_warehouse_table_description_fills_a_blank_entity() -> None:
    cp = ColumnProfile(name="f", type="VARCHAR")
    # Same rule as columns: hand-authored wins, the table comment fills the blank.
    assert _enrich_one(cp, table_desc="Daily fact grain")[1] == "Daily fact grain"
    assert _enrich_one(cp, table_desc="Daily fact grain", entity_desc="Authored")[1] == "Authored"
    # Notes append after the filled description.
    assert (
        _enrich_one(cp, table_desc="Daily fact grain", row_count=100)[1]
        == "Daily fact grain (~100 rows)"
    )


def test_row_count_formats_compactly() -> None:
    cp = ColumnProfile(name="f", type="VARCHAR")
    assert _enrich_one(cp, row_count=100)[1] == "(~100 rows)"
    assert _enrich_one(cp, row_count=2_500)[1] == "(~2.5k rows)"
    assert _enrich_one(cp, row_count=1_000_000)[1] == "(~1M rows)"
    assert _enrich_one(cp, row_count=1_234_567)[1] == "(~1.2M rows)"
    assert _enrich_one(cp, row_count=3_000_000_000)[1] == "(~3B rows)"
    assert _enrich_one(cp)[1] is None  # no row_count, no note


def test_stats_compose_on_one_field() -> None:
    cp = ColumnProfile(
        name="f",
        type="BIGINT",
        top_values=["1", "2"],
        values_complete=True,
        distinct_count=2,
        distinct_is_exact=True,
        null_rate=0.12,
        min="1",
        max="42",
    )
    got = _enrich_one(cp, field_type="integer")[0]
    assert got == "Base — Values: '1', '2' — ~12% null — range: 1..42"


def test_data_as_of_is_the_oldest_scope_table() -> None:
    profiles = _profiles() + [
        TableProfile(
            connection="jaffle",
            table="orders",
            last_updated_logical=datetime(2026, 6, 1, tzinfo=UTC),
        )
    ]
    as_of = profile_enrich.data_as_of(profiles, {"customers", "orders"})
    assert as_of is not None and as_of.date().isoformat() == "2026-06-01"
    only_customers = profile_enrich.data_as_of(profiles, {"customers"})
    assert only_customers is not None and only_customers.date().isoformat() == "2026-06-08"
    assert profile_enrich.data_as_of(profiles, {"unknown"}) is None


def _reachable(url: str) -> bool:
    try:
        with create_engine(url).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _reachable(settings.database_admin_url), reason="Postgres not reachable"
)


@needs_db
def test_answer_carries_data_as_of(monkeypatch: pytest.MonkeyPatch) -> None:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('AsOf') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    gen_json = '{"sql": "SELECT count(*) AS n FROM customers", "definition_used": null}'
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.runtime.assembly.IntentSQLGenerator",
        lambda llm, model, temperature=0.0: GroundedSQLGenerator(
            llm, model=model, temperature=temperature
        ),
    )
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _key, **_: ScriptedLLM([gen_json, "There are 100 customers."]),
    )
    try:
        with org_session(org) as s:
            lens_store.create_lens(s, jaffle_customer_value_bundle())
            lens_store.publish(s, "customer_value")
            for p in _profiles():
                profile_store.upsert_profile(s, p)
        r = client.post(
            "/v1/lenses/customer_value/query",
            json={"q": "how many customers?"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["data_as_of"] == "2026-06-08"
    finally:
        with admin.begin() as c:
            for table in ("request_log", "table_profile", "lens", "admin_token"):
                c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org})  # noqa: S608
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})
