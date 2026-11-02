"""`dst migrate-model`: the cube_shop catalogue → its Cube model, byte for byte
against the golden output under the fixture's cube/, and the re-run rules:
idempotent, never over a hand edit, never over a file it did not write."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from services.project.migrate_cube import MANIFEST, REPORT, Converter
from tests.cube_shop import PROJECT

GOLDEN = PROJECT / "cube"


def _golden() -> dict[str, str]:
    return {
        p.relative_to(GOLDEN).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(GOLDEN.rglob("*"))
        if p.is_file() and p.name != MANIFEST
    }


def _dst(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "services.cli.main", "migrate-model", *args],
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    shutil.copytree(PROJECT, root, ignore=shutil.ignore_patterns("cube"))
    return root


def test_the_converter_writes_the_golden_model() -> None:
    assert Converter(PROJECT).files() == _golden()


def test_the_mapping_on_the_shapes_cube_cannot_say_alone() -> None:
    files = Converter(PROJECT).files()
    orders = yaml.safe_load(files["model/orders.yml"])["cubes"][0]
    # versions dedup + population baked into the cube's sql; the key is the grain
    assert (
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY _synced DESC) = 1"
        in (orders["sql"])
    )
    assert orders["sql"].endswith("WHERE NOT is_test")
    assert orders["meta"]["dst"]["key"] == ["order_id"]
    dims = {d["name"]: d for d in orders["dimensions"]}
    assert "tips_eur_value" in dims  # a field named like a metric: one namespace per cube
    assert dims["_synced"]["public"] is False and dims["_synced"]["meta"]["dst"]["internal"]
    assert dims["ordered_at"]["meta"]["dst"]["clock"]["timezone"] == "Europe/Helsinki"
    measures = {m["name"]: m for m in orders["measures"]}
    assert measures["average_order_value"]["sql"] == "{revenue} / NULLIF({order_count}, 0)"
    assert measures["shipped_revenue"]["meta"]["dst"]["clock"] == "shipped_on"
    stock = yaml.safe_load(files["model/stock_monthly.yml"])["cubes"][0]
    month = next(d for d in stock["dimensions"] if d["name"] == "month")
    assert month["type"] == "time" and month["sql"] == "CAST({CUBE}.month || '-01' AS DATE)"
    assert stock["measures"][0]["meta"]["dst"] == {"additivity": "level", "snapshot_grain": "month"}
    companion = yaml.safe_load(files["model/order_lines_by_order_id.yml"])["cubes"][0]
    assert companion["meta"]["dst"]["sources"] == ["order_lines"]
    assert "GROUP BY order_id" in companion["sql"]
    stores = yaml.safe_load(files["model/stores.yml"])["cubes"][0]
    email = next(d for d in stores["dimensions"] if d["name"] == "manager_email")
    assert email["public"] is False and email["meta"]["dst"]["hide"] == {"except_groups": ["ops"]}
    region = next(d for d in stores["dimensions"] if d["name"] == "region")
    assert {"alias": "seaside", "value": "Coast"} in region["meta"]["dst"]["value_aliases"]
    views = yaml.safe_load(files["model/views/sales.yml"])["views"]
    assert [v["name"] for v in views] == [
        "sales_lens",
        "sales_order_lines",
        "sales_orders",
        "sales_stock_monthly",
        "sales_order_lines_by_order_id",
    ]
    # the companion cube joins as its parent does, at the distinct grain
    assert companion["joins"] == [
        {
            "name": "orders",
            "relationship": "many_to_one",
            "sql": "{CUBE}.order_id = {orders}.order_id",
        }
    ]
    by_name = {v["name"]: v for v in views}
    # two hops: order_lines reaches the stores dimensions through orders
    paths = [c["join_path"] for c in by_name["sales_order_lines"]["cubes"]]
    assert paths == ["order_lines", "order_lines.orders", "order_lines.orders.stores"]
    assert [c["join_path"] for c in by_name["sales_order_lines_by_order_id"]["cubes"]][-1] == (
        "order_lines_by_order_id.orders.stores"
    )
    overlay = yaml.safe_load(files["overlay.yaml"])
    assert overlay["lenses"]["sales"]["views"] == [v["name"] for v in views]
    assert [d["metric"] for d in overlay["ambiguous"]] == ["margin"]


def test_views_list_their_members_and_withhold_hidden_ones() -> None:
    """A view's ``includes: "*"`` serves a cube's ``public: false`` members:
    every view lists its members, internal ones and a hide with no exception
    never, and a hide with exception groups only under an access policy."""
    views = yaml.safe_load(Converter(PROJECT).files()["model/views/sales.yml"])["views"]
    for view in views:
        listed = {
            f"{c['join_path'].rsplit('.', 1)[-1]}.{m}" for c in view["cubes"] for m in c["includes"]
        }
        assert all(isinstance(c["includes"], list) for c in view["cubes"]), view["name"]
        assert not listed & {"orders._synced", "orders.is_test", "stores.legacy_code"}, view["name"]
        if "stores.manager_email" in listed:
            assert view["access_policy"] == [
                {"group": "*", "member_level": {"excludes": ["stores_manager_email"]}},
                {"group": "ops", "member_level": {"includes": "*"}},
            ], view["name"]
        else:
            assert "access_policy" not in view, view["name"]
    assert sum("access_policy" in v for v in views) == 5  # every view reaching stores


def test_a_group_policy_lets_each_group_read_only_its_own_exceptions() -> None:
    policy = Converter.access_policy({"t_salary": ["hr"], "t_email": ["hr", "ops"]})
    assert policy == [
        {"group": "*", "member_level": {"excludes": ["t_email", "t_salary"]}},
        {"group": "hr", "member_level": {"includes": "*"}},
        {"group": "ops", "member_level": {"includes": "*", "excludes": ["t_salary"]}},
    ]


def test_the_report_names_every_item_with_its_failure_mode() -> None:
    report = Converter(PROJECT).files()[REPORT]
    assert "| orders | field tips_eur shares its name with a metric |" in report
    assert "| not_declared |" in report and "| wrong_grain |" in report
    assert "| lens sales | untyped_fallback | DROPPED" in report


def test_a_rerun_is_idempotent_and_refuses_a_hand_edit(project: Path) -> None:
    first = _dst("--dir", str(project))
    assert first.returncode == 0, first.stderr
    out = project / "cube"
    written = {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert {p.relative_to(out).as_posix() for p in written} == set(_golden()) | {
        MANIFEST,
        "cube.js",
    }

    again = _dst("--dir", str(project))
    assert again.returncode == 0 and "unchanged" in again.stdout
    assert {p: p.read_bytes() for p in out.rglob("*") if p.is_file()} == written
    assert _dst("--dir", str(project), "--dry-run").returncode == 0

    edited = out / "model" / "orders.yml"
    edited.write_text(edited.read_text() + "# authored\n")
    (project / "semantic/entities/orders.yaml").write_text(
        (project / "semantic/entities/orders.yaml").read_text().replace("label: tips", "label: tip")
    )
    assert _dst("--dir", str(project), "--dry-run").returncode == 1
    refused = _dst("--dir", str(project))
    assert refused.returncode == 1 and "edited after" in refused.stderr
    assert edited.read_text().endswith("# authored\n")  # nothing written

    forced = _dst("--dir", str(project), "--force")
    assert forced.returncode == 0
    assert "# authored" not in edited.read_text() and "title: tip\n" in edited.read_text()


def test_a_file_it_never_wrote_is_left_alone_and_a_dropped_one_removed(project: Path) -> None:
    assert _dst("--dir", str(project)).returncode == 0
    out = project / "cube"
    mine = out / "model" / "handwritten.yml"
    mine.write_text("cubes: []\n")
    (project / "semantic/entities/stock_monthly.yaml").unlink()
    (project / "semantic/relationships/stock_monthly__stores.yaml").unlink()
    lens = project / "lenses/sales/lens.yaml"
    lens.write_text(lens.read_text().replace(", stock_monthly]", "]"))
    dry = _dst("--dir", str(project), "--dry-run")
    assert dry.returncode == 2 and "stock_monthly.yml" in dry.stdout
    assert _dst("--dir", str(project)).returncode == 0
    assert not (out / "model" / "stock_monthly.yml").exists()
    assert mine.read_text() == "cubes: []\n"


def test_no_catalogue_is_a_named_refusal(tmp_path: Path) -> None:
    result = _dst("--dir", str(tmp_path))
    assert result.returncode == 1 and "no semantic/entities/" in result.stderr


# ── what the lane and the monitor read, carried by the migration ──────────


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _books(root: Path) -> Path:
    """Two companies' books on two tables keyed by company, an ad feed per
    platform, and invoices with a due date the probe saw after it ran."""
    _write(
        root,
        "semantic/entities/sales_monthly.yaml",
        """name: sales_monthly
source: {connection: wh, table: fin.sales_monthly}
default_time_field: month_start
primary_key: [month_start, company, stream]
fields:
  - {name: month_start, type: date}
  - {name: company, type: string}
  - {name: stream, type: string}
  - {name: amount_eur, type: number}
metrics:
  - {name: sales_eur, agg: sum, expr: sales_monthly.amount_eur}
""",
    )
    _write(
        root,
        "semantic/entities/cash_monthly.yaml",
        """name: cash_monthly
source: {connection: wh, table: fin.cash_monthly}
default_time_field: month_start
primary_key: [month_start, company, stream]
fields:
  - {name: month_start, type: date}
  - {name: company, type: string}
  - {name: stream, type: string}
  - {name: cash_eur, type: number}
metrics:
  - {name: cash_collected_eur, agg: sum, expr: cash_monthly.cash_eur}
""",
    )
    _write(
        root,
        "semantic/entities/ads.yaml",
        """name: ads
source: {connection: wh, table: ads.daily}
default_time_field: day
primary_key: [day, platform, campaign]
fields:
  - {name: day, type: date}
  - {name: platform, type: string}
  - {name: campaign, type: string}
  - {name: spend_eur, type: number}
metrics:
  - {name: spend, agg: sum, expr: ads.spend_eur}
""",
    )
    _write(
        root,
        "semantic/entities/invoices.yaml",
        """name: invoices
source: {connection: wh, table: fin.invoices}
default_time_field: issued_on
primary_key: [invoice_id]
fields:
  - {name: invoice_id, type: string}
  - {name: issued_on, type: date}
  - {name: due_on, type: date}
  - {name: stream, type: string}
metrics:
  - {name: invoice_count, agg: count}
""",
    )
    tables = {
        "fin.sales_monthly": ("dbt", ["month_start", "company", "stream"]),
        "fin.cash_monthly": ("dbt", ["month_start", "company", "stream"]),
        "ads.daily": ("Alpha Ads / Beta Ads exports", ["day", "platform", "campaign"]),
        "fin.invoices": ("dbt", ["invoice_id"]),
    }
    for table, (system, key) in tables.items():
        _write(
            root,
            f"semantic/tables/{table.replace('.', '/')}.yaml",
            yaml.safe_dump({"connection": "wh", "table": table, "system": system, "key": key}),
        )
    _write(
        root,
        "lenses/books/lens.yaml",
        "name: books\nconnections: [wh]\n"
        "select:\n  entities: [sales_monthly, cash_monthly, ads, invoices]\n",
    )

    def col(name: str, values: list[str] | None = None, mx: str | None = None) -> dict[str, object]:
        return {
            "name": name,
            "top_values": values,
            "values_complete": values is not None,
            "is_low_cardinality": values is not None,
            "max": mx,
        }

    companies = ["Harbour Bakeries Oy", "Mill Bakery AB"]
    probe = {
        "tables": [
            {
                "table": "fin.sales_monthly",
                "profiled_at": "2026-09-01T06:00:00Z",
                "columns": [
                    col("month_start", mx="2026-08-01"),
                    col("company", companies),
                    col("stream", ["Counter", "Wholesale"]),
                ],
            },
            {
                "table": "fin.cash_monthly",
                "profiled_at": "2026-09-01T06:00:00Z",
                "columns": [
                    col("month_start", mx="2026-08-01"),
                    col("company", companies),
                    col("stream", ["Counter"] + ["Wholesale"]),
                ],
            },
            {
                "table": "ads.daily",
                "profiled_at": "2026-09-01T06:00:00Z",
                "columns": [
                    col("day", mx="2026-08-31"),
                    col("platform", ["alpha_ads", "beta_ads"]),
                    col("campaign", ["spring", "summer", "autumn"]),
                ],
            },
            {
                "table": "fin.invoices",
                "profiled_at": "2026-09-01T06:00:00Z",
                "columns": [
                    col("issued_on", mx="2026-08-31"),
                    col("due_on", mx="2026-09-30"),
                    col("stream", ["Counter", "Wholesale"]),
                ],
            },
        ]
    }
    _write(root, "profiles/warehouse.probe.json", json.dumps(probe))
    return root


def test_feed_axes_schedule_dates_and_company_names_reach_the_overlay(tmp_path: Path) -> None:
    root = _books(tmp_path / "books")
    overlay = yaml.safe_load(Converter(root).files()["overlay.yaml"])
    cubes = overlay["cubes"]
    # company and stream split two tables each by the same values: the books.
    assert cubes["sales_monthly"]["feed_axes"] == ["company", "stream"]
    assert cubes["cash_monthly"]["feed_axes"] == ["company", "stream"]
    # A stream that keys one table and is a column of another is a feed there too.
    assert cubes["invoices"]["feed_axes"] == ["stream"]
    # The blueprint's system names every platform; campaigns are a breakdown.
    assert cubes["ads"]["feed_axes"] == ["platform"]
    # A due date with rows after the probe ran is a schedule; the clock is left
    # to the monitor.
    assert cubes["invoices"]["clock_kind"] == {"due_on": "schedule"}
    assert "clock_kind" not in cubes["sales_monthly"]
    assert overlay["scope"]["names"] == {
        "Harbour Bakeries Oy": ["harbour bakeries oy", "harbour bakeries"],
        "Mill Bakery AB": ["mill bakery ab", "mill bakery"],
    }
    # The overlay parses under the closed schema.
    from services.contracts.lens_model import parse_overlay

    assert parse_overlay(Converter(root).files()["overlay.yaml"]).cubes["ads"].feed_axes


def test_a_view_whose_cubes_share_no_member_name_is_written_unprefixed(tmp_path: Path) -> None:
    """`prefix: true` exists so `month`/`entity`/the join key from two cubes
    never collide; a view with nothing to collide lists its members bare,
    so a query names `<view>.<member>` and the access policy names the
    member. The fixture's joined views share their join key and keep the
    prefix; a lens over one entity, or two joined on differently named
    keys, does not."""
    root = tmp_path / "venues"
    _write(
        root,
        "semantic/entities/tickets.yaml",
        """name: tickets
source: {connection: wh, table: box.tickets}
default_time_field: sold_on
primary_key: [ticket_id]
fields:
  - {name: ticket_id, type: string}
  - {name: venue_ref, type: string}
  - {name: sold_on, type: date}
  - {name: buyer_email, type: string}
hide:
  - {column: buyer_email, except_groups: [support]}
metrics:
  - {name: tickets_sold, agg: count}
""",
    )
    _write(
        root,
        "semantic/entities/venues.yaml",
        """name: venues
source: {connection: wh, table: box.venues}
primary_key: [id]
fields:
  - {name: id, type: string}
  - {name: city, type: string}
  - {name: owner_email, type: string}
hide:
  - {column: owner_email, except_groups: [ops]}
metrics:
  - {name: venue_count, agg: count}
""",
    )
    _write(
        root,
        "semantic/relationships/tickets__venues.yaml",
        "left: tickets\nright: venues\n'on': tickets.venue_ref = venues.id\ntype: left\n"
        "relationship: many_to_one\n",
    )
    _write(
        root,
        "lenses/box/lens.yaml",
        "name: box\nconnections: [wh]\nselect:\n  entities: [tickets, venues]\n",
    )
    _write(
        root,
        "lenses/solo/lens.yaml",
        "name: solo\nconnections: [wh]\nselect:\n  entities: [venues]\n",
    )
    files = Converter(root).files()
    box = {v["name"]: v for v in yaml.safe_load(files["model/views/box.yml"])["views"]}
    for name in ("box_lens", "box_tickets"):
        view = box[name]
        assert all("prefix" not in c for c in view["cubes"]), name
        assert view["access_policy"][0] == {
            "group": "*",
            "member_level": {"excludes": ["buyer_email", "owner_email"]},
        }
        assert {p["group"]: p["member_level"] for p in view["access_policy"]}["ops"] == {
            "includes": "*",
            "excludes": ["buyer_email"],
        }
    solo = yaml.safe_load(files["model/views/solo.yml"])["views"][0]
    assert solo["cubes"] == [
        {"join_path": "venues", "includes": ["id", "city", "owner_email", "venue_count"]}
    ]
    # The fixture's joined views share their join key: prefixed, as before.
    sales = yaml.safe_load(Converter(PROJECT).files()["model/views/sales.yml"])["views"]
    assert all(c["prefix"] is True for v in sales for c in v["cubes"])
    assert "stores_manager_email" in sales[0]["access_policy"][0]["member_level"]["excludes"]


def test_views_follow_the_cubes_as_authored(project: Path) -> None:
    assert _dst("--dir", str(project)).returncode == 0
    out = project / "cube"
    orders = out / "model" / "orders.yml"
    doc = yaml.safe_load(orders.read_text())
    cube = doc["cubes"][0]
    cube["dimensions"] = [d for d in cube["dimensions"] if d["name"] != "status"]
    cube["measures"].append({"name": "big_orders", "type": "count", "filters": []})
    orders.write_text(yaml.safe_dump(doc, sort_keys=False))
    _write(
        out,
        "model/order_notes.yml",
        yaml.safe_dump(
            {
                "cubes": [
                    {
                        "name": "order_notes",
                        "sql_table": "bakery.order_notes",
                        "meta": {"dst": {"sources": ["orders"]}},
                        "dimensions": [{"name": "note", "sql": "{CUBE}.note", "type": "string"}],
                        "measures": [{"name": "note_count", "type": "count"}],
                    }
                ]
            }
        ),
    )
    authored = orders.read_text()
    again = _dst("--dir", str(project))
    assert again.returncode == 0, again.stderr
    assert "authored, kept" in again.stdout
    assert orders.read_text() == authored
    views = yaml.safe_load((out / "model/views/sales.yml").read_text())["views"]
    scope = {c["join_path"]: c["includes"] for c in views[0]["cubes"]}
    assert scope["orders"][-1] == "big_orders" and "status" not in scope["orders"]
    assert scope["order_notes"] == ["note", "note_count"]
    # Kept as the author's: a third run changes nothing.
    assert _dst("--dir", str(project), "--dry-run").returncode == 0


def test_on_bigquery_a_date_or_datetime_clock_is_cast_to_a_timestamp(tmp_path: Path) -> None:
    """Cube's BigQuery dialect bounds a window with TIMESTAMP(?), and BigQuery
    compares no DATE or DATETIME with a TIMESTAMP: those clocks are cast, a
    TIMESTAMP column stays bare so its partitions still prune."""
    from tests.fixtures.cube_shop.make_data import bigquery_project

    root = bigquery_project(tmp_path / "bq", "a-project")
    model = root / "cube" / "model"

    def sql(cube: str, dim: str) -> str:
        doc = yaml.safe_load((model / f"{cube}.yml").read_text())
        return next(d["sql"] for d in doc["cubes"][0]["dimensions"] if d["name"] == dim)

    assert sql("orders", "ordered_at") == "{CUBE}.ordered_at"  # TIMESTAMP
    assert sql("orders", "shipped_on") == "TIMESTAMP({CUBE}.shipped_on)"  # DATE
    assert sql("orders", "_synced") == "TIMESTAMP({CUBE}._synced)"  # DATETIME
    assert sql("stock_monthly", "month") == "TIMESTAMP(CAST({CUBE}.month || '-01' AS DATE))"
    # the same project on DuckDB is written as before
    assert Converter(PROJECT).files() == _golden()


def _ferry(root: Path) -> Path:
    """Bookings with a payer and a passenger, both persons: one pair joined
    once per role."""
    _write(
        root,
        "semantic/entities/bookings.yaml",
        """name: bookings
source: {connection: wh, table: ferry.bookings}
primary_key: [booking_id]
fields:
  - {name: booking_id, type: string}
  - {name: payer_id, type: string}
  - {name: passenger_id, type: string}
  - {name: fare_eur, type: number}
metrics:
  - {name: fare_total, agg: sum, expr: bookings.fare_eur}
""",
    )
    _write(
        root,
        "semantic/entities/persons.yaml",
        """name: persons
source: {connection: wh, table: ferry.persons}
primary_key: [id]
fields:
  - {name: id, type: string}
  - {name: city, type: string}
metrics:
  - {name: person_count, agg: count}
""",
    )
    for role in ("payer", "passenger"):
        _write(
            root,
            f"semantic/relationships/bookings__persons__{role}.yaml",
            f'left: bookings\nright: persons\n"on": bookings.{role}_id = persons.id\n'
            f"relationship: many_to_one\nrole: {role}\n",
        )
    _write(
        root,
        "lenses/sailings/lens.yaml",
        "name: sailings\nconnections: [wh]\nselect:\n  entities: [bookings, persons]\n",
    )
    return root


def test_a_role_relationship_writes_an_extends_cube_per_role(tmp_path: Path) -> None:
    files = Converter(_ferry(tmp_path / "ferry")).files()
    payer = yaml.safe_load(files["model/persons_payer.yml"])["cubes"][0]
    assert payer["extends"] == "persons" and payer["meta"]["dst"]["role"] == "payer"
    assert "dimensions" not in payer  # every member is inherited from persons
    bookings = yaml.safe_load(files["model/bookings.yml"])["cubes"][0]
    assert bookings["joins"] == [
        {
            "name": "persons_passenger",
            "relationship": "many_to_one",
            "sql": "{CUBE}.passenger_id = {persons_passenger}.id",
        },
        {
            "name": "persons_payer",
            "relationship": "many_to_one",
            "sql": "{CUBE}.payer_id = {persons_payer}.id",
        },
    ]
    views = yaml.safe_load(files["model/views/sailings.yml"])["views"]
    scope = {c["join_path"]: c["includes"] for c in views[0]["cubes"]}
    assert scope["persons_payer"] == ["id", "city"] == scope["persons_passenger"]
    fact = {c["join_path"]: c["includes"] for c in views[1]["cubes"]}
    assert set(fact) == {"bookings", "bookings.persons_payer", "bookings.persons_passenger"}
    assert fact["bookings.persons_payer"] == ["id", "city"]
    assert "persons_payer" in files["MIGRATION.md"] or "role copies" in files["MIGRATION.md"]


def test_a_hidden_population_filter_column_never_arrives_as_a_public_dimension(
    tmp_path: Path,
) -> None:
    """The population filter is baked into the cube's sql, and the hidden
    column it reads is `public: false` (matched as the compile matches a
    `hide` pattern), so no view lists it."""
    root = tmp_path / "library"
    _write(
        root,
        "semantic/entities/loans.yaml",
        """name: loans
source: {connection: wh, table: library.loans}
default_time_field: loaned_on
primary_key: [loan_id]
population_filter: "loans.borrower_email NOT LIKE '%@library.example'"
hide: ["*_email"]
fields:
  - {name: loan_id, type: integer}
  - {name: loaned_on, type: date}
  - {name: branch, type: string}
  - {name: borrower_email, type: string}
metrics:
  - {name: loan_count, agg: count}
""",
    )
    _write(
        root,
        "lenses/reading/lens.yaml",
        "name: reading\nconnections: [wh]\nselect:\n  entities: [loans]\n",
    )
    files = Converter(root).files()
    loans = yaml.safe_load(files["model/loans.yml"])["cubes"][0]
    assert loans["sql"].endswith("WHERE borrower_email NOT LIKE '%@library.example'")
    dims = {d["name"]: d for d in loans["dimensions"]}
    assert dims["borrower_email"]["public"] is False
    assert dims["borrower_email"]["meta"]["dst"]["hide"] == {"except_groups": []}
    views = yaml.safe_load(files["model/views/reading.yml"])["views"]
    listed = {m for v in views for c in v["cubes"] for m in c["includes"]}
    assert "borrower_email" not in listed and "branch" in listed


def test_meta_dst_carries_what_a_level_means_on_each_table_shape(tmp_path: Path) -> None:
    """A level on a creation-dated table (a key unique per row) is marked so
    the lane reads it whole when no period is asked; a ratio of flows never
    carries `additivity: level`, whatever the entity file says."""
    root = tmp_path / "fleet"
    _write(
        root,
        "semantic/entities/rentals.yaml",
        """name: rentals
source: {connection: wh, table: fleet.rentals}
default_time_field: started_at
primary_key: [rental_id]
fields:
  - {name: rental_id, type: integer}
  - {name: started_at, type: date}
  - {name: returned_at, type: date}
metrics:
  - {name: rentals_started, agg: count}
  - {name: rentals_returned, agg: count, expr: rentals.returned_at, agg_time_field: returned_at}
  - {name: return_rate, type: ratio, numerator: rentals_returned,
     denominator: rentals_started, additivity: level}
  - {name: open_rentals, agg: count, filters: ["rentals.returned_at IS NULL"], additivity: level}
""",
    )
    _write(
        root,
        "semantic/entities/fleet_snapshot.yaml",
        """name: fleet_snapshot
source: {connection: wh, table: fleet.fleet_snapshot}
default_time_field: snapshot_date
primary_key: [snapshot_date, bike_id]
fields:
  - {name: snapshot_date, type: date}
  - {name: bike_id, type: integer}
metrics:
  - {name: bikes_in_fleet, agg: count_distinct, expr: fleet_snapshot.bike_id, additivity: level}
""",
    )
    _write(
        root,
        "lenses/fleet/lens.yaml",
        "name: fleet\nconnections: [wh]\nselect:\n  entities: [rentals, fleet_snapshot]\n",
    )

    def table(name: str, rows: int, cols: dict[str, int]) -> dict[str, object]:
        return {
            "table": name,
            "profiled_at": "2026-09-01T06:00:00Z",
            "row_count": rows,
            "columns": [
                {"name": c, "type": "INTEGER", "distinct_count": n, "distinct_is_exact": True}
                for c, n in cols.items()
            ],
        }

    probe = {
        "tables": [
            table("fleet.rentals", 6, {"rental_id": 6}),
            table("fleet.fleet_snapshot", 30, {"snapshot_date": 10, "bike_id": 3}),
        ]
    }
    _write(root, "profiles/warehouse.probe.json", json.dumps(probe))
    files = Converter(root).files()
    rentals = {
        m["name"]: m for m in yaml.safe_load(files["model/rentals.yml"])["cubes"][0]["measures"]
    }
    assert rentals["open_rentals"]["meta"]["dst"] == {
        "additivity": "level",
        "snapshot_grain": "started_at",
        "creation_dated": True,
    }
    assert "meta" not in rentals["return_rate"]
    snapshot = yaml.safe_load(files["model/fleet_snapshot.yml"])["cubes"][0]["measures"][0]
    assert snapshot["meta"]["dst"] == {"additivity": "level", "snapshot_grain": "snapshot_date"}
