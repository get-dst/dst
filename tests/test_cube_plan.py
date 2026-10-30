"""Every Cube-side plan check, one at a time, on the cube_shop fixture: the
recorded /v1/meta, the overlay ``dst migrate-model`` wrote, and one change
each that the check exists to catch."""

from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml

from services.contracts.cube_meta import MetaDoc
from services.contracts.lens_model import LensModel, parse_overlay
from services.project import cube_plan
from tests import cube_shop


def _check(
    files: dict[str, str] | None = None,
    meta: dict[str, Any] | None = None,
    public: dict[str, Any] | None = None,
) -> cube_plan.CubeCheck:
    doc = MetaDoc.parse(meta or cube_shop.meta_payload())
    pub = MetaDoc.parse(public or cube_shop.public_meta_payload())
    return cube_plan.check(files or cube_shop.project_files(), fetch=lambda _f: (doc, pub))


def _codes(result: cube_plan.CubeCheck, severity: str | None = None) -> list[str]:
    return [i.code for i in result.issues if severity is None or i.severity == severity]


def _member(payload: dict[str, Any], name: str) -> dict[str, Any]:
    cube = next(c for c in payload["cubes"] if c["name"] == name.split(".")[0])
    for group in ("measures", "dimensions", "segments"):
        for m in cube.get(group) or []:
            if m["name"] == name:
                return dict(m)
    raise KeyError(name)


def _set_dst(payload: dict[str, Any], name: str, dst: dict[str, Any]) -> dict[str, Any]:
    """Set meta.dst on a cube member and on every view member aliasing it."""
    out = copy.deepcopy(payload)
    for cube in out["cubes"]:
        for group in ("measures", "dimensions", "segments"):
            for m in cube.get(group) or []:
                if m["name"] == name or m.get("aliasMember") == name:
                    m.setdefault("meta", {})["dst"] = {
                        **(m.get("meta") or {}).get("dst", {}),
                        **dst,
                    }
    return out


def _overlay(files: dict[str, str], edit: Any) -> dict[str, str]:
    data = yaml.safe_load(files["cube/overlay.yaml"])
    edit(data)
    return {**files, "cube/overlay.yaml": yaml.safe_dump(data, sort_keys=False)}


def test_the_migrated_fixture_checks_clean() -> None:
    result = _check()
    assert _codes(result) == []
    assert result.models["sales"].unknown_views == []


def test_cube_withholds_every_hidden_member_from_a_caller_in_no_group() -> None:
    """The recorded meta of a caller in no group lists no hidden or internal
    member on any view: the views list their members, and the one hidden
    member with an exception group sits under an access policy."""
    public = MetaDoc.parse(cube_shop.public_meta_payload())
    model = LensModel.build(
        public, parse_overlay(cube_shop.project_files()["cube/overlay.yaml"]), "sales"
    )
    served = {m.base for m in model.members}
    assert served and not served & {
        "stores.manager_email",
        "stores.legacy_code",
        "orders._synced",
        "orders.is_test",
    }
    planned = MetaDoc.parse(cube_shop.meta_payload())  # the plan context: in group ops
    assert planned.member("sales_lens.stores_manager_email") is not None
    assert public.member("sales_lens.stores_manager_email") is None


def test_a_hidden_member_cube_serves_to_a_caller_in_no_group_is_warned() -> None:
    """A view that re-exposes a hidden member (an ``includes: "*"`` view, or
    a Cube without contextToGroups granting by group): plan says so."""
    assert (
        _codes(_check(public=cube_shop.meta_payload()), "warning")
        == ["hide_unenforced_in_cube"] * 5
    )


def test_an_unknown_meta_dst_key_is_an_error() -> None:
    meta = _set_dst(cube_shop.meta_payload(), "orders.revenue", {"colour": "red"})
    result = _check(meta=meta)
    assert "meta_dst_unknown_key" in _codes(result, "error")
    assert any("colour" in i.message for i in result.errors)


def test_a_level_without_a_clock_is_an_error() -> None:
    files = _overlay(cube_shop.project_files(), lambda d: d["cubes"].pop("stock_monthly"))
    result = _check(files)
    assert _codes(result, "error") == ["level_without_clock"]
    assert "stock_monthly.stock_on_hand" in result.errors[0].message


def test_a_clock_naming_no_time_dimension_is_an_error() -> None:
    meta = _set_dst(cube_shop.meta_payload(), "orders.revenue", {"clock": "paid_on"})
    assert _codes(_check(meta=meta), "error") == ["unknown_member"]


def test_a_default_segment_that_is_not_a_segment_is_an_error() -> None:
    meta = _set_dst(cube_shop.meta_payload(), "orders.revenue", {"default_segment": "ghost"})
    assert _codes(_check(meta=meta), "error") == ["default_segment_missing"]
    meta = _set_dst(cube_shop.meta_payload(), "orders.revenue", {"default_segment": "web_order"})
    assert _codes(_check(meta=meta), "error") == []


def test_overlay_members_must_exist() -> None:
    def edit(d: dict[str, Any]) -> None:
        d["cubes"]["orders"]["pinned_dimensions"] = ["currency"]
        d["definitions"][0]["about"] = "orders.basket_size"

    result = _check(_overlay(cube_shop.project_files(), edit))
    assert sorted(_codes(result, "error")) == ["unknown_member", "unknown_member"]
    assert {"currency" in i.message or "basket_size" in i.message for i in result.errors} == {True}


def test_a_clock_kind_must_name_a_dimension_and_event_or_schedule() -> None:
    ok = _overlay(
        cube_shop.project_files(),
        lambda d: d["cubes"]["orders"].update(clock_kind={"ordered_at": "event"}),
    )
    assert _codes(_check(ok), "error") == []
    ghost = _overlay(
        cube_shop.project_files(),
        lambda d: d["cubes"]["orders"].update(clock_kind={"paid_on": "schedule"}),
    )
    assert _codes(_check(ghost), "error") == ["unknown_member"]
    bad = _overlay(
        cube_shop.project_files(),
        lambda d: d["cubes"]["orders"].update(clock_kind={"ordered_at": "due"}),
    )
    assert _codes(_check(bad)) == ["overlay_invalid"]


def test_an_unknown_overlay_key_fails_the_file() -> None:
    files = _overlay(cube_shop.project_files(), lambda d: d["lenses"]["sales"].update(tz="UTC"))
    assert _codes(_check(files)) == ["overlay_invalid"]


def test_certified_pairs_name_lens_members_and_never_a_hidden_one() -> None:
    pairs = [
        {
            "question": "revenue by region",
            "query": {
                "measures": ["sales_orders.orders_revenue"],
                "dimensions": ["sales_orders.stores_region"],
            },
        },
        {"question": "revenue by colour", "query": {"dimensions": ["sales_orders.orders_colour"]}},
        {
            "question": "orders per manager",
            "query": {
                "measures": ["sales_lens.orders_order_count"],
                "filters": [{"member": "sales_lens.stores_manager_email", "operator": "set"}],
            },
        },
    ]
    files = {
        **cube_shop.project_files(),
        "lenses/sales/certified_answers.yaml": yaml.safe_dump(pairs),
    }
    result = _check(files)
    errors = [(i.code, i.message.split(":")[0]) for i in result.errors]
    assert errors == [
        ("certified_member", "certified 'revenue by colour'"),
        ("certified_hidden", "certified 'orders per manager'"),
    ]


def test_a_text_month_string_warns_and_an_unprobed_dictionary_hints() -> None:
    meta = _set_dst(
        cube_shop.meta_payload(), "orders.channel", {"dictionary": ["2026-01", "2026-02"]}
    )
    meta = _set_dst(meta, "orders.status", {"dictionary": True})
    result = _check(meta=meta)
    assert "text_month_dimension" in _codes(result, "warning")
    assert _codes(result, "hint") == ["dictionary_unprobed"]
    probed = {
        **cube_shop.project_files(),
        cube_plan.CUBE_PROBE_PATH: '{"dictionaries": {"orders.status": ["open", "paid"]}}',
    }
    assert "dictionary_unprobed" not in _codes(_check(probed, meta=meta))


def test_keys_read_names_every_key_carried_and_its_reader() -> None:
    keys = _check().keys["sales"]
    carried = {(k["on"], k["key"]) for k in keys}
    assert {("measure", "clock"), ("dimension", "hide"), ("segment", "meaning")} <= carried
    unread = {k["key"] for k in keys if str(k["reader"]).startswith("not read")}
    assert "value_aliases" in unread  # carried, read by no lane yet: said, never implied


def test_a_lens_yaml_views_list_wins_over_the_overlay() -> None:
    files = cube_shop.project_files()
    files["lenses/sales/lens.yaml"] += "views: [sales_orders]\n"
    result = _check(files)
    assert [v.name for v in result.models["sales"].views] == ["sales_orders"]


@pytest.mark.parametrize(
    ("query", "members"),
    [
        ({"measures": ["a.x"], "order": {"a.x": "desc"}}, ["a.x"]),
        (
            {
                "timeDimensions": [{"dimension": "a.t", "granularity": "month"}],
                "filters": [{"or": [{"member": "a.s", "operator": "equals"}]}],
                "order": [["a.y", "asc"]],
            },
            ["a.t", "a.s", "a.y"],
        ),
    ],
)
def test_query_members(query: dict[str, Any], members: list[str]) -> None:
    assert cube_plan.query_members(query) == members


def test_fetch_meta_reads_extended_meta_as_the_plan_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.config import settings
    from tests.test_cube_client import SECRET, FakeCube

    monkeypatch.setattr(settings, "cube_url", None)
    monkeypatch.setattr(settings, "cube_api_secret_env", None)
    monkeypatch.setenv("CUBEJS_API_SECRET", SECRET)
    fake = FakeCube(meta=cube_shop.meta_payload())
    files = cube_shop.project_files()
    files["dst.yaml"] = files["dst.yaml"].replace(
        "  api_secret_env: CUBEJS_API_SECRET",
        "  api_secret_env: CUBEJS_API_SECRET\n  plan_context: {groups: [ops]}",
    )
    meta, public = cube_plan.fetch_meta(files, transport=fake.transport())
    assert meta.cube("sales_orders") is not None and public.cube("sales_orders") is not None
    assert [r.url.params.get("extended") for r in fake.requests] == ["true", "true"]
    assert [c["dst"]["groups"] for c in fake.claims] == [["ops"], []]


def test_fetch_meta_reads_as_every_exception_group_when_no_plan_context_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cube withholds a hidden member from a caller outside its exception
    groups, plan too: with no plan_context, plan reads as all of them."""
    from services.config import settings
    from tests.test_cube_client import SECRET, FakeCube

    monkeypatch.setattr(settings, "cube_url", None)
    monkeypatch.setattr(settings, "cube_api_secret_env", None)
    monkeypatch.setenv("CUBEJS_API_SECRET", SECRET)
    fake = FakeCube(meta=cube_shop.meta_payload())
    cube_plan.fetch_meta(cube_shop.project_files(), transport=fake.transport())
    assert [c["dst"]["groups"] for c in fake.claims] == [["ops"], []]


def test_fetch_meta_with_no_cube_is_provider_down(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.config import settings
    from services.cube.client import CubeError

    monkeypatch.setattr(settings, "cube_url", None)
    files = cube_shop.project_files()
    files["dst.yaml"] = files["dst.yaml"].split("cube:")[0]
    with pytest.raises(CubeError) as err:
        cube_plan.fetch_meta(files)
    assert err.value.mode == "provider_down"


def test_every_closed_meta_dst_key_names_its_reader() -> None:
    """A key in the closed schema with no row in KEY_READERS would print as
    "not read" whatever reads it; ``as_of`` and ``label_of`` included."""
    from services.contracts.cube_meta import CubeDst, DimensionDst, MeasureDst, SegmentDst
    from services.contracts.lens_model import KEY_READERS

    for on, schema in (
        ("measure", MeasureDst),
        ("dimension", DimensionDst),
        ("cube", CubeDst),
        ("segment", SegmentDst),
    ):
        assert set(schema.model_fields) == set(KEY_READERS[on]), on
    assert KEY_READERS["measure"]["as_of"].startswith("verify")
    assert KEY_READERS["dimension"]["label_of"].startswith("verify")


def test_an_unlisted_hide_is_a_hint_for_everyone_and_a_warning_with_exceptions() -> None:
    """Cube lists no member it withholds from every caller, so a hide for
    everyone that the meta lacks is only a hint; a hide with exception groups
    plan reads as those groups, so a missing one is a misspelling."""

    def edit(d: dict[str, Any]) -> None:
        d["hide"] += [
            {"cube": "stores", "dimension": "pin_code", "except_groups": []},
            {"cube": "stores", "dimension": "phone", "except_groups": ["ops"]},
        ]

    result = _check(_overlay(cube_shop.project_files(), edit))
    assert [(i.severity, i.code) for i in result.issues] == [
        ("hint", "hide_unlisted"),
        ("warning", "unknown_member"),
    ]


def test_a_definition_about_a_member_cube_withholds_is_a_hint_not_an_abort() -> None:
    """A `public: false` member is listed by Cube to no caller, so the meta
    cannot show it; the overlay's hide entry says it is withheld, not
    misspelt. The definition stays, served to the callers who may see it."""

    def edit(d: dict[str, Any]) -> None:
        d["definitions"][0]["about"] = "stores.manager_email"

    meta = copy.deepcopy(cube_shop.meta_payload())
    for cube in meta["cubes"]:
        cube["dimensions"] = [
            m
            for m in cube.get("dimensions") or []
            if "manager_email" not in (m["name"], m.get("aliasMember") or "")
            and not m["name"].endswith("manager_email")
        ]
    result = _check(_overlay(cube_shop.project_files(), edit), meta=meta)
    assert _codes(result, "error") == []
    hints = [i for i in result.issues if i.code == "definition_about_hidden"]
    assert len(hints) == 1 and hints[0].severity == "hint"
    assert "stores.manager_email" in hints[0].message and "ops" in hints[0].message

    # A definition about a member nobody hides is still a misspelling.
    def misspelt(d: dict[str, Any]) -> None:
        d["definitions"][0]["about"] = "stores.manager_mail"

    assert "unknown_member" in _codes(
        _check(_overlay(cube_shop.project_files(), misspelt)), "error"
    )


def test_member_caveats_must_name_a_member_of_the_cube() -> None:
    ok = _overlay(
        cube_shop.project_files(),
        lambda d: d["cubes"]["orders"].update(member_caveats={"revenue": ["allocated"]}),
    )
    assert _codes(_check(ok), "error") == []
    ghost = _overlay(
        cube_shop.project_files(),
        lambda d: d["cubes"]["orders"].update(member_caveats={"basket_size": ["allocated"]}),
    )
    assert _codes(_check(ghost), "error") == ["unknown_member"]


def test_feed_measures_must_name_a_measure_of_the_cube() -> None:
    ok = _overlay(
        cube_shop.project_files(),
        lambda d: d["cubes"]["orders"].update(feed_measures=["revenue"]),
    )
    assert _codes(_check(ok), "error") == []
    ghost = _overlay(
        cube_shop.project_files(),
        lambda d: d["cubes"]["orders"].update(feed_measures=["channel"]),
    )
    assert _codes(_check(ghost), "error") == ["unknown_member"]
