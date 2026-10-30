"""The cube_shop fixture's warehouse: a small bakery chain, generated.

``python tests/fixtures/cube_shop/make_data.py OUT.duckdb`` writes the
warehouse the fixture project (``project/``) describes, deterministically,
in a storage format Cube Core's bundled DuckDB opens. ``--probe`` also
rewrites ``project/profiles/warehouse.probe.json`` (the converter reads its
value lists).

The data carries what the monitor is meant to see: the web feed stops on
2026-07-31 while the till feed runs to the clock (``feed_stopped``), and the
till's September is a fraction of its usual volume (``partial_period``).

A live Cube over it, for the ``DST_TEST_CUBE_URL`` lanes::

    python tests/fixtures/cube_shop/make_data.py /tmp/cube_shop/shop.duckdb
    docker run -d --name dst-test-cube -p 4100:4000 \\
      -e CUBEJS_DEV_MODE=false -e CUBEJS_API_SECRET=<secret> -e CUBEJS_DB_TYPE=duckdb \\
      -e DST_DUCKDB_PATH=/data/shop.duckdb \\
      -v $PWD/tests/fixtures/cube_shop/project/cube/model:/cube/conf/model:ro \\
      -v /tmp/cube_shop:/data:ro -v $PWD/deploy/cube/cube.js:/cube/conf/cube.js:ro \\
      cubejs/cube:$(cat deploy/cube.version)
    DST_TEST_CUBE_URL=http://localhost:4100 DST_TEST_CUBE_SECRET=<secret> pytest ...

The same data on BigQuery, for the ``DST_TEST_CUBE_BQ_URL`` lane: ``--bigquery
PROJECT KEY.json`` loads it into the dataset ``bakery`` the model names (load
jobs, which BigQuery does not bill), and ``--bigquery-project OUT PROJECT``
writes the fixture project for that warehouse, its Cube model included.
``tests/test_cube_bigquery.py`` says how to run Cube over it.
"""

from __future__ import annotations

import json
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
START = date(2025, 1, 1)
CLOCK_END = date(2026, 9, 14)  # the last day the till feed loaded
WEB_STOPS = date(2026, 7, 31)
STORES = [
    (1, "Harbour", "Coast", date(2019, 4, 1), "harbour@example.com", "H1"),
    (2, "Old Town", "South", date(2020, 2, 1), "oldtown@example.com", "O2"),
    (3, "Mill Hill", "North", date(2021, 6, 1), "millhill@example.com", "M3"),
    (4, "Pier Road", "Coast", date(2022, 9, 1), "pier@example.com", "P4"),
]
PRODUCTS = [("rye loaf", 6.5), ("cinnamon bun", 3.9), ("sourdough", 7.2), ("coffee", 3.2)]


def build(out: Path) -> dict[str, list[tuple[object, ...]]]:
    rnd = random.Random(7)
    stores = [s for s in STORES]
    orders: list[tuple[object, ...]] = []
    lines: list[tuple[object, ...]] = []
    oid = 0
    day = START
    while day <= CLOCK_END:
        for channel in ("counter", "web"):
            if channel == "web" and day > WEB_STOPS:
                continue
            n = 6 if channel == "counter" else 2
            if channel == "counter" and day >= date(2026, 9, 1):
                n = 1  # September is loading short
            for _ in range(n):
                oid += 1
                store = rnd.choice(stores)[0]
                status = rnd.choices(["paid", "refunded", "open"], [90, 6, 4])[0]
                ordered = datetime(day.year, day.month, day.day, rnd.randint(7, 18), 0)
                basket = [rnd.choice(PRODUCTS) for _ in range(rnd.randint(1, 3))]
                amount = round(sum(p for _, p in basket), 2)
                discount = round(amount * 0.1, 2) if rnd.random() < 0.2 else 0.0
                tips = round(rnd.random(), 2)
                shipped = day + timedelta(days=1) if channel == "web" else day
                versions = 2 if rnd.random() < 0.15 else 1
                for v in range(versions):
                    synced = ordered + timedelta(hours=1 + v * 12)
                    # an older sync carries a stale amount: only the latest counts
                    stale = amount if v == versions - 1 else round(amount * 0.5, 2)
                    orders.append(
                        (
                            oid,
                            store,
                            channel,
                            status,
                            ordered,
                            shipped,
                            stale,
                            discount,
                            tips,
                            synced,
                            oid % 997 == 0,
                        )
                    )
                for i, (product, price) in enumerate(basket, 1):
                    lines.append((oid, i, product, 1, price, discount))
        day += timedelta(days=1)
    stock: list[tuple[object, ...]] = []
    month = date(2025, 1, 1)
    while month <= date(2026, 8, 1):
        for s in stores:
            stock.append((month.strftime("%Y-%m"), s[0], rnd.randint(80, 160)))
        month = date(month.year + month.month // 12, month.month % 12 + 1, 1)

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    con = duckdb.connect(":memory:")
    # Cube Core's bundled DuckDB is older than this one: write a storage
    # version it opens.
    con.execute(f"ATTACH '{out}' AS wh (STORAGE_VERSION 'v1.0.0')")
    con.execute("CREATE SCHEMA wh.bakery")
    con.execute(
        "CREATE TABLE wh.bakery.stores (store_id INTEGER, store_name VARCHAR, region VARCHAR, "
        "opened_on DATE, manager_email VARCHAR, legacy_code VARCHAR)"
    )
    con.executemany("INSERT INTO wh.bakery.stores VALUES (?, ?, ?, ?, ?, ?)", stores)
    con.execute(
        "CREATE TABLE wh.bakery.orders_raw (order_id INTEGER, store_id INTEGER, channel VARCHAR, "
        "status VARCHAR, ordered_at TIMESTAMP, shipped_on DATE, amount_eur DOUBLE, "
        "discount_eur DOUBLE, tips_eur DOUBLE, _synced TIMESTAMP, is_test BOOLEAN)"
    )
    con.executemany("INSERT INTO wh.bakery.orders_raw VALUES (?,?,?,?,?,?,?,?,?,?,?)", orders)
    con.execute(
        "CREATE TABLE wh.bakery.order_lines (order_id INTEGER, line_number INTEGER, "
        "product VARCHAR, qty INTEGER, line_total_eur DOUBLE, order_discount_eur DOUBLE)"
    )
    con.executemany("INSERT INTO wh.bakery.order_lines VALUES (?,?,?,?,?,?)", lines)
    con.execute(
        "CREATE TABLE wh.bakery.stock_monthly "
        "(month VARCHAR, store_id INTEGER, units_on_hand INTEGER)"
    )
    con.executemany("INSERT INTO wh.bakery.stock_monthly VALUES (?,?,?)", stock)
    con.execute("DETACH wh")
    con.close()
    return {"stores": stores, "orders": orders, "lines": lines, "stock": stock}


def probe(out: Path) -> dict[str, object]:
    """The few probe facts the converter reads: per column its complete value
    list when the column is enum-sized."""
    con = duckdb.connect(str(out), read_only=True)
    tables = []
    for table in (
        "bakery.stores",
        "bakery.orders_raw",
        "bakery.order_lines",
        "bakery.stock_monthly",
    ):
        cols = con.execute(f"DESCRIBE {table}").fetchall()
        columns = []
        for name, ctype, *_ in cols:
            distinct = con.execute(f'SELECT count(DISTINCT "{name}") FROM {table}').fetchone()
            n = int(distinct[0]) if distinct else 0
            low = ctype == "VARCHAR" and n <= 25
            values = (
                [
                    r[0]
                    for r in con.execute(
                        f'SELECT DISTINCT "{name}" FROM {table} '
                        f'WHERE "{name}" IS NOT NULL ORDER BY 1'
                    ).fetchall()
                ]
                if low
                else []
            )
            columns.append(
                {
                    "name": name,
                    "type": ctype,
                    "distinct_count": n,
                    "is_low_cardinality": low,
                    "values_complete": low,
                    "top_values": values,
                }
            )
        tables.append({"connection": "warehouse", "table": table, "columns": columns})
    con.close()
    return {"connection": "warehouse", "tables": tables}


# BigQuery's three time types, one per clock: the order clock is a TIMESTAMP,
# the sync and a store's opening DATETIMEs, the shipping day a DATE.
BQ_SCHEMA = {
    "stores": [
        ("store_id", "INT64"),
        ("store_name", "STRING"),
        ("region", "STRING"),
        ("opened_on", "DATETIME"),
        ("manager_email", "STRING"),
        ("legacy_code", "STRING"),
    ],
    "orders_raw": [
        ("order_id", "INT64"),
        ("store_id", "INT64"),
        ("channel", "STRING"),
        ("status", "STRING"),
        ("ordered_at", "TIMESTAMP"),
        ("shipped_on", "DATE"),
        ("amount_eur", "FLOAT64"),
        ("discount_eur", "FLOAT64"),
        ("tips_eur", "FLOAT64"),
        ("_synced", "DATETIME"),
        ("is_test", "BOOL"),
    ],
    "order_lines": [
        ("order_id", "INT64"),
        ("line_number", "INT64"),
        ("product", "STRING"),
        ("qty", "INT64"),
        ("line_total_eur", "FLOAT64"),
        ("order_discount_eur", "FLOAT64"),
    ],
    "stock_monthly": [("month", "STRING"), ("store_id", "INT64"), ("units_on_hand", "INT64")],
}


def load_bigquery(project: str, key_file: str, data: dict[str, list[tuple[object, ...]]]) -> None:
    """The built rows into ``PROJECT.bakery``, each table replaced."""
    from google.cloud import bigquery
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(key_file)  # type: ignore[no-untyped-call]
    client = bigquery.Client(credentials=creds, project=project)
    dataset = bigquery.Dataset(f"{project}.bakery")
    dataset.location = "EU"
    client.create_dataset(dataset, exists_ok=True)
    rows = {
        "stores": data["stores"],
        "orders_raw": data["orders"],
        "order_lines": data["lines"],
        "stock_monthly": data["stock"],
    }
    for table, schema in BQ_SCHEMA.items():
        records = [
            {
                name: (
                    value.isoformat(sep=" ")
                    if isinstance(value, datetime)
                    else value.isoformat()
                    if isinstance(value, date)
                    else value
                )
                for (name, _), value in zip(schema, row, strict=True)
            }
            for row in rows[table]
        ]
        config = bigquery.LoadJobConfig(
            schema=[bigquery.SchemaField(n, t) for n, t in schema],
            write_disposition="WRITE_TRUNCATE",
        )
        client.load_table_from_json(
            records, f"{project}.bakery.{table}", job_config=config
        ).result()


def bigquery_project(out: Path, project: str) -> Path:
    """The fixture project on BigQuery at *out*: its connection a BigQuery
    one, its probe typed as BigQuery types the loaded columns, and its Cube
    model written again by ``dst migrate-model`` for that warehouse."""
    import shutil

    from services.project.migrate_cube import plan_migration, write_migration

    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(HERE / "project", out, ignore=shutil.ignore_patterns("cube"))
    dst_yaml = out / "dst.yaml"
    dst_yaml.write_text(
        dst_yaml.read_text(encoding="utf-8").replace(
            "type: duckdb\n    config: {path: shop.duckdb, read_only: true}",
            f"type: bigquery\n    config: {{project: {project}, datasets: [bakery]}}\n"
            "    secret_env: DST_API_KEY_WAREHOUSE",
        ),
        encoding="utf-8",
    )
    probe_path = out / "profiles" / "warehouse.probe.json"
    artifact = json.loads(probe_path.read_text(encoding="utf-8"))
    for table in artifact["tables"]:
        types = dict(BQ_SCHEMA[table["table"].split(".", 1)[1]])
        for column in table["columns"]:
            column["type"] = types[column["name"]]
    probe_path.write_text(json.dumps(artifact, indent=1) + "\n", encoding="utf-8")
    write_migration(plan_migration(out, out / "cube"))
    return out


if __name__ == "__main__":
    if sys.argv[1] == "--bigquery-project":
        bigquery_project(Path(sys.argv[2]), sys.argv[3])
        sys.exit(0)
    if sys.argv[1] == "--bigquery":
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            load_bigquery(sys.argv[2], sys.argv[3], build(Path(tmp) / "shop.duckdb"))
        sys.exit(0)
    target = Path(sys.argv[1])
    build(target)
    if "--probe" in sys.argv:
        artifact = HERE / "project" / "profiles" / "warehouse.probe.json"
        artifact.write_text(json.dumps(probe(target), indent=1) + "\n", encoding="utf-8")
