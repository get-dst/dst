"""The LensModel contract: one lens over a Cube meta document plus the overlay."""

from __future__ import annotations

import copy

import pytest

from services.contracts.cube_meta import MetaDoc
from services.contracts.lens_model import LensModel, Overlay, parse_overlay
from tests.test_cube_client import META

OVERLAY = """
version: 1
cubes:
  orders:
    default_time_dimension: ordered_at
    use_cases: [Use when a question asks about orders]
  customers:
    caveats: [one row per customer]
lenses:
  sales:
    views: [orders_view, ghost_view]
    timezone: Europe/Helsinki
    stale_after_days: 3
    not_for: [{ask: churn, sibling: success}]
    definitions: [paid order]
definitions:
  - {metric: paid order, about: orders, summary: an order already paid}
  - {metric: island, about: islands, summary: not this lens}
ambiguous:
  - metric: revenue
    status: ambiguous
    possible_mappings: [gross, net]
    audiences: {finance: net}
hide:
  - {cube: customers, dimension: country, except_groups: [ops]}
"""


def _model(views: list[str] | None = None) -> LensModel:
    return LensModel.build(MetaDoc.parse(META), parse_overlay(OVERLAY), "sales", views=views)


def test_members_resolve_to_the_cube_member_behind_the_view() -> None:
    model = _model()
    assert [v.name for v in model.views] == ["orders_view"]
    assert model.unknown_views == ["ghost_view"]
    revenue = model.member("orders_view.revenue")
    assert revenue is not None
    assert (revenue.base, revenue.kind, revenue.type) == ("orders.revenue", "measure", "sum")
    assert revenue.measure is not None and revenue.measure.aliases == ["sales"]
    # A view member without meta reads its cube member's meta.dst.
    status = model.member("orders_view.status")
    assert status is not None and status.dimension is not None
    assert status.dimension.dictionary is True
    assert model.member("orders.revenue") is revenue  # by cube name, when unambiguous
    assert {c.name for c in model.cubes} == {"orders", "customers"}


def test_the_overlay_slice_is_the_lens_own() -> None:
    model = _model()
    assert model.policy.timezone == "Europe/Helsinki"
    assert [d.metric for d in model.definitions] == ["paid order"]
    assert [d.metric for d in model.ambiguous] == ["revenue"]
    assert [(h.cube, h.dimension) for h in model.hide] == [("customers", "country")]
    orders = model.cube("orders")
    assert orders is not None and orders.overlay.default_time_dimension == "ordered_at"
    assert orders.dst is not None and orders.dst.key == ["id"]


def test_clock_is_declared_never_guessed() -> None:
    model = _model()
    revenue, count = model.member("orders_view.revenue"), model.member("orders_view.count")
    assert revenue is not None and count is not None
    assert model.clock_of(revenue) == "orders.ordered_at"  # meta.dst.clock
    assert model.clock_of(count) == "orders.ordered_at"  # the overlay's default
    bare = LensModel.build(MetaDoc.parse(META), Overlay(), "sales", views=["orders_view"])
    bare_count = bare.member("orders_view.count")
    assert bare_count is not None and bare.clock_of(bare_count) is None


def test_hidden_holds_except_for_the_named_groups() -> None:
    model = _model()
    country = model.member("orders_view.country")
    assert country is not None
    assert model.hidden(country) and model.hidden(country, ["sales"])
    assert not model.hidden(country, ["ops"])


def test_versions_move_with_what_the_lens_reads_only() -> None:
    model = _model()
    assert model.model_version == MetaDoc.parse(META).model_version(["orders_view"])
    meta = copy.deepcopy(META)
    meta["cubes"][2]["measures"][0]["aggType"] = "sum"  # islands: not in the lens
    assert LensModel.build(MetaDoc.parse(meta), parse_overlay(OVERLAY), "sales") == model
    other = OVERLAY.replace("Europe/Helsinki", "Europe/Oslo")
    moved = LensModel.build(MetaDoc.parse(META), parse_overlay(other), "sales")
    assert moved.model_version == model.model_version
    assert moved.overlay_version != model.overlay_version


def test_lens_yaml_views_win_and_the_model_round_trips() -> None:
    model = _model(views=["orders_view"])
    assert model.unknown_views == []
    assert LensModel.model_validate_json(model.model_dump_json()) == model


def test_an_unknown_overlay_key_names_the_file_and_the_key() -> None:
    with pytest.raises(ValueError, match="cube/overlay.yaml") as err:
        parse_overlay(OVERLAY.replace("timezone: Europe/Helsinki", "tz: Europe/Helsinki"))
    assert "tz" in str(err.value)


def test_value_aliases_read_the_pair_list_and_a_declared_dictionary() -> None:
    meta = copy.deepcopy(META)
    meta["cubes"][0]["dimensions"][0]["meta"]["dst"] = {
        "dictionary": ["paid", "open"],
        "value_aliases": [{"alias": "settled", "value": "paid"}],
    }
    doc = MetaDoc.parse(meta)
    assert doc.overlay_errors == []
    status = doc.member("orders.status")
    assert status is not None and status.dst is not None
    assert status.dst.declared_values == ["paid", "open"]  # type: ignore[union-attr]
    assert status.dst.value_aliases == {"paid": ["settled"]}  # type: ignore[union-attr]


def test_a_view_segment_resolves_to_its_cube_segment_without_an_alias() -> None:
    from tests import cube_shop

    overlay = parse_overlay((cube_shop.PROJECT / "cube" / "overlay.yaml").read_text())
    model = LensModel.build(cube_shop.meta(), overlay, "sales")
    web = model.member("sales_lens.orders_web_order")
    assert web is not None and web.base == "orders.web_order"
    assert web.segment is not None and web.segment.term == "web_order"
