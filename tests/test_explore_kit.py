"""The explore kit lists the exposed tables a driver may read, ranked by how
many of the question's words each carries (its name, its columns, their
descriptions, its blueprint's grain), bounded, with the rest counted. The
invented world: a bakery's warehouse, many tables, a question about one."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from services.api import route as route_api
from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import SemanticModel
from services.lenses.store import LensBundle
from services.serve import driver


def _table(name: str, columns: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "name": name.rsplit(".", 1)[-1],
        "source": {"connection": "wh", "table": name},
        "exposed": True,
        "fields": [{"name": c, "type": "string"} for c in columns],
        **extra,
    }


def _bundle(lens: str, tables: list[dict[str, Any]], **sm: Any) -> LensBundle:
    return LensBundle(
        config=LensConfig(name=lens, connections=["wh"], serving="cube"),
        semantic_model=SemanticModel.model_validate(
            {"lens": lens, "dialect": "duckdb", "entities": tables, **sm}
        ),
    )


def test_tables_are_scored_by_the_question_words_they_carry() -> None:
    assert driver.table_words("oven repair cost by bakery", ["ops.oven_repairs", "cost_eur"]) == [
        "oven",
        "repair",
        "cost",
    ]
    assert driver.table_words("oven repair cost", ["hr.staff", "salary"]) == []


def test_the_kit_ranks_by_relevance_not_lens_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "services.runtime.assembly.profile_facts",
        lambda bundle, org: ([], None, None, {}),
    )
    filler = [_table(f"ops.table_{i:02d}", ["id", "amount"]) for i in range(45)]
    bundles = [
        _bundle("aardvark", filler),
        _bundle(
            "zebra",
            [_table("ops.oven_repairs", ["oven_id", "repaired_on", "cost_eur"])],
            blueprints={
                "ops.oven_repairs": {
                    "connection": "wh",
                    "table": "ops.oven_repairs",
                    "grain": "one row per repair visit",
                    "key": ["oven_id", "repaired_on"],
                }
            },
        ),
    ]
    caller = SimpleNamespace(org_id="org")
    kit = route_api._explore_kit(caller, bundles, "what did oven repairs cost last month")  # type: ignore[arg-type]
    assert kit.tables[0].table == "ops.oven_repairs"
    assert kit.tables[0].matched == ["oven", "repairs", "cost"]
    assert len(kit.tables) == route_api.EXPLORE_TABLES_MAX
    assert kit.elided == 46 - route_api.EXPLORE_TABLES_MAX


def test_beside_covering_lenses_the_kit_lists_only_tables_holding_as_much(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A covered ask still sees an exposed table that holds its words: the
    kit beside covering lenses lists the tables carrying at least as many of
    the question's words as the best lens names, bounded; none when no table
    does."""
    monkeypatch.setattr(
        "services.runtime.assembly.profile_facts",
        lambda bundle, org: ([], None, None, {}),
    )
    tables = [
        _table("ops.oven_targets", ["oven_id", "target_loaves", "baker"]),
        _table("ops.flour_deliveries", ["delivered_on", "kilos"]),
        *(_table(f"ops.oven_log_{i}", ["oven_id", "target_loaves"]) for i in range(9)),
    ]
    caller = SimpleNamespace(org_id="org")
    kit = route_api._explore_kit(
        caller,  # type: ignore[arg-type]
        [_bundle("ovens", tables)],
        "which bakers are behind their oven targets",
        at_least=2,
        limit=route_api.EXPLORE_BESIDE_MAX,
    )
    assert kit.tables[0].table == "ops.oven_targets"
    assert "ops.flour_deliveries" not in {t.table for t in kit.tables}
    assert len(kit.tables) == route_api.EXPLORE_BESIDE_MAX
    assert kit.elided == 10 - route_api.EXPLORE_BESIDE_MAX
    assert "beside" in kit.note
    none = route_api._explore_kit(
        caller,  # type: ignore[arg-type]
        [_bundle("ovens", tables)],
        "which bakers are behind their oven targets",
        at_least=4,
        limit=route_api.EXPLORE_BESIDE_MAX,
    )
    assert none.tables == [] and none.elided == 0
