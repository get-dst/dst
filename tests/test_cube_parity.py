"""The same lens built two ways — from its entity YAML, and from the Cube model
``dst migrate-model`` wrote for it (as a real Cube served it, recorded) plus
the overlay — describes itself the same way.

Every field of ``describe_lens`` and the metric catalog is compared. What the
Cube model cannot carry is pinned here by name, each with why, so a new
divergence fails and a closed one fails too until its pin is removed:

- types: Cube has one ``number`` (integer or not) and one ``time`` (date or
  timestamp), so a time metric's grains are Cube's and every numeric column
  is a callable dimension (the entity model groups by integers only);
- the field named like a metric is ``tips_eur_value`` (one namespace per cube);
- a definition's body is its summary (the overlay carries no page prose);
- a text ``YYYY-MM`` month is a ``time`` dimension, bound by period, not by
  a value dictionary;
- a label equal to the metric's name reads as no label;
- an internal column is on no view (a view lists its members, and never an
  internal one), so the Cube build lists no internal field.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from services.contracts.lens_model import LensModel, parse_overlay
from services.cube.client import CubeClient
from services.cube.context import CubeContext
from services.lenses.describe import LensDescription, describe_model
from services.lenses.describe_cube import CUBE_GRAINS, describe_lens_model, measure_sql
from services.monitor import cube as monitor
from services.project.compile import compile_lens_model
from services.project.loader import load_lens_source, split_by_lens, split_semantic
from services.semantic.files import parse_semantic_files
from tests import cube_shop
from tests.test_monitor_cube import CLOCK, CassetteCube

CUBE_TYPE = {"integer": "number", "date": "time", "timestamp": "time"}
# The text YYYY-MM month is a time dimension in Cube: a period binds to it, so
# it has no value dictionary there.
TEXT_MONTH = ("stock_monthly", "month")
RENAMED = {("orders", "tips_eur"): "tips_eur_value"}
INTERNAL = {("orders", "_synced"), ("orders", "is_test"), ("stores", "legacy_code")}


def _domains_entity() -> dict[str, dict[str, list[str]]]:
    import json

    probe = json.loads((cube_shop.PROJECT / "profiles" / "warehouse.probe.json").read_text())
    table_entity = {
        "bakery.orders_raw": "orders",
        "bakery.order_lines": "order_lines",
        "bakery.stores": "stores",
        "bakery.stock_monthly": "stock_monthly",
    }
    out: dict[str, dict[str, list[str]]] = {}
    for t in probe["tables"]:
        for c in t["columns"]:
            if c["values_complete"] and c["top_values"] and c["name"] != "manager_email":
                out.setdefault(table_entity[t["table"]], {})[c["name"].lower()] = c["top_values"]
    return out


def _domains_cube(model: LensModel) -> dict[str, dict[str, list[str]]]:
    fake = CassetteCube()
    client = CubeClient("http://cube.test", None, transport=httpx.MockTransport(fake.handle))
    ctx = CubeContext.operator()
    probe = monitor.probe([model], lambda q: client.load(q, ctx).data, clock_end=CLOCK)
    out: dict[str, dict[str, list[str]]] = {}
    for base, values in probe["dictionaries"].items():
        cube, column = base.split(".", 1)
        if not cube.endswith("_by_order_id"):
            out.setdefault(cube, {})[column.lower()] = values
    return out


def _builds() -> tuple[LensDescription, LensDescription]:
    files = cube_shop.project_files()
    entities, definitions, relationships = parse_semantic_files(split_semantic(files))
    source = load_lens_source(split_by_lens(files)["sales"])
    model, _warnings = compile_lens_model(
        config=source.config,
        shared_entities=entities,
        shared_definitions=definitions,
        shared_relationships=relationships,
        local_definitions=source.local_definitions,
        use_when=source.use_when,
        sample_queries=source.sample_queries,
        dialect="duckdb",
        groups=["ops"],
    )
    yaml_build = describe_model(
        "sales",
        display_name="Sales",
        description=source.config.description,
        model=model,
        domains=_domains_entity(),
    )
    lens_model = LensModel.build(
        cube_shop.meta(), parse_overlay(files["cube/overlay.yaml"]), "sales"
    )
    cube_build = describe_lens_model(
        lens_model,
        name="sales",
        display_name="Sales",
        description=source.config.description,
        dialect="duckdb",
        sql=measure_sql(cube_shop.meta_payload()),
        domains=_domains_cube(lens_model),
    )
    return yaml_build, cube_build


def _name(entity: str, column: str) -> str:
    return RENAMED.get((entity, column), column)


def _dim_name(entity: str, ref: str) -> str:
    owner, _, column = ref.rpartition(".")
    return f"{owner}.{_name(owner, column)}" if owner else _name(entity, column)


def test_lens_header_and_entities_match() -> None:
    a, b = _builds()
    for key in ("name", "display_name", "description", "dialect", "sample_questions"):
        assert getattr(a, key) == getattr(b, key), key
    assert [e.name for e in a.entities] == [e.name for e in b.entities]
    for ea, eb in zip(a.entities, b.entities, strict=True):
        assert ea.description == eb.description
        assert [
            (
                _name(ea.name, f.name),
                "time" if (ea.name, f.name) == TEXT_MONTH else CUBE_TYPE.get(f.type, f.type),
                f.description,
            )
            for f in ea.fields
            if (ea.name, f.name) not in INTERNAL
        ] == [(f.name, f.type, f.description) for f in eb.fields], ea.name
    internal = {(e.name, f.name) for e in a.entities for f in e.fields} & INTERNAL
    assert internal == INTERNAL  # the pin names columns the entity build does list
    # text YYYY-MM months became a time dimension: the one type that moved
    month = next(f for e in b.entities for f in e.fields if f.name == "month")
    assert month.type == "time"


def test_definitions_match_but_for_the_page_prose() -> None:
    a, b = _builds()
    assert sorted(d.term for d in a.definitions) == sorted(d.term for d in b.definitions)
    by_term = {d.term: d for d in b.definitions}
    for d in a.definitions:
        assert (d.status, d.possible_mappings) == (
            by_term[d.term].status,
            by_term[d.term].possible_mappings,
        ), d.term
        assert by_term[d.term].body and by_term[d.term].body != d.body  # summary, not prose


def _metric_fields(m: Any) -> dict[str, Any]:
    return {
        "aliases": m.aliases,
        "description": m.description,
        "type": m.type,
        "agg": m.agg,
        "additivity": m.additivity,
        "window_reading": m.window_reading,
        "unit": m.unit,
        "currency": m.currency,
        "format": m.format,
        "depends_on": m.depends_on,
        "filters_summary": m.filters_summary,
        "certified_answer_ids": m.certified_answer_ids,
    }


def test_the_metric_catalog_matches_with_the_gaps_pinned() -> None:
    a, b = _builds()
    cube = {(m.entity, m.name): m for m in b.metrics}
    assert set(cube) == {(m.entity, m.name) for m in a.metrics}
    for ma in a.metrics:
        mb = cube[(ma.entity, ma.name)]
        assert _metric_fields(ma) == _metric_fields(mb), ma.name
        assert (ma.label if ma.label != ma.name else None) == mb.label, ma.name
        if ma.time_field and ma.grains:
            assert mb.time_field == ma.time_field and mb.grains == CUBE_GRAINS
        elif ma.time_field:  # a text month: no grains as a string, Cube's as a time
            assert mb.time_field == ma.time_field and mb.grains == CUBE_GRAINS
        wanted = {_dim_name(ma.entity, d) for d in ma.dimensions}
        assert wanted <= set(mb.dimensions), ma.name  # every join reached, two hops too
        numeric = {
            f"{e.name}.{f.name}" if e.name != ma.entity else f.name
            for e in b.entities
            for f in e.fields
            if f.type == "number"
        }
        assert set(mb.dimensions) - wanted <= numeric, ma.name


def test_the_dimension_catalog_matches_values_included() -> None:
    a, b = _builds()
    cube = {(d.entity, d.name): d for d in b.dimensions}
    for da in a.dimensions:
        db = cube[(da.entity, _name(da.entity, da.name))]
        assert db.aliases == da.aliases, da.name
        if (da.entity, da.name) == TEXT_MONTH:
            assert (db.type, db.values) == ("time", None) and da.values
            continue
        assert db.values == da.values, da.name
        assert db.type == CUBE_TYPE.get(da.type, da.type), da.name
    extra = set(cube) - {(d.entity, _name(d.entity, d.name)) for d in a.dimensions}
    assert {cube[k].type for k in extra} == {"number"}  # Cube groups by any number


def _values(dims: Sequence[Any]) -> Mapping[str, Any]:
    return {
        f"{d.entity}.{d.name}": d.values
        for d in dims
        if d.values and (d.entity, d.name) != TEXT_MONTH
    }


def test_both_builds_bind_the_same_value_dictionaries() -> None:
    a, b = _builds()
    assert _values(a.dimensions) == {
        k: v for k, v in _values(b.dimensions).items() if k in _values(a.dimensions)
    }
    assert _values(a.dimensions)  # a comparison of nothing proves nothing
