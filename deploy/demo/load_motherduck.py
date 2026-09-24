"""Load a local DuckDB file into MotherDuck as one database.

    MOTHERDUCK_TOKEN=... python deploy/demo/load_motherduck.py \
        --source fixtures/jaffle_shop.duckdb --database dst_demo

Uses MotherDuck's `CREATE DATABASE <name> FROM '<file>'`, which copies the file's
schemas and tables up as they are. The remote name must differ from the file's
stem (a MotherDuck rule), and this needs a read-write token — the read-scaling
token the demo serves with cannot create a database. Run it from any machine
with the `duckdb` package; the demo container is not required.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import duckdb


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--source", default="fixtures/jaffle_shop.duckdb", help="local .duckdb file")
    p.add_argument("--database", default="dst_demo", help="MotherDuck database to create")
    p.add_argument(
        "--replace", action="store_true", help="drop the MotherDuck database first if it exists"
    )
    args = p.parse_args()

    token = os.environ.get("MOTHERDUCK_TOKEN")
    if not token:
        print("MOTHERDUCK_TOKEN is not set (a read-write token)", file=sys.stderr)
        return 2
    source = Path(args.source)
    if not source.is_file():
        print(f"no such file: {source}", file=sys.stderr)
        return 2
    if source.stem == args.database:
        print("the MotherDuck database name must differ from the file's name", file=sys.stderr)
        return 2

    con = duckdb.connect("md:", config={"motherduck_token": token})
    if args.replace:
        con.execute(f'DROP DATABASE IF EXISTS "{args.database}"')
    con.execute(f"CREATE DATABASE \"{args.database}\" FROM '{source.resolve()}'")
    tables = con.execute(
        "SELECT schema_name, table_name FROM duckdb_tables() "
        "WHERE database_name = ? AND NOT internal ORDER BY 1, 2",
        [args.database],
    ).fetchall()
    print(f"md:{args.database}: {len(tables)} table(s)")
    for schema, table in tables:
        print(f"  {schema}.{table}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
