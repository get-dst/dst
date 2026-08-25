"""SQL rewriting for probing an eval oracle against the live warehouse.

The snapshot plane this module once served is gone — certified answers are the
sole eval mechanism; what remains is the leaf-to-source repointing that apply's
inbound oracle gate and ``dst evals migrate`` use to
execute a case's expected_sql where the physical tables actually live.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp


def rewrite_to_sources(sql: str, dialect: str, leaf_to_table: dict[str, str]) -> str:
    """Repoint leaf-named table references at their fully-qualified LIVE source
    tables, for probing an expected_sql against the warehouse: a case written
    the way a lens reads it (`FROM customers`) must probe where the physical
    table is `db.schema.customers`. ``leaf_to_table`` maps a lowercased leaf
    name to the entity's physical source table. An unaliased table is aliased
    back to its leaf so qualified column references keep binding."""
    tree = sqlglot.parse_one(sql, read=dialect)
    for tbl in tree.find_all(exp.Table):
        table = leaf_to_table.get(tbl.name.lower())
        if table is None:
            continue
        if not tbl.alias:
            tbl.set("alias", exp.TableAlias(this=exp.to_identifier(tbl.name)))
        parts = table.split(".")
        tbl.set("this", exp.to_identifier(parts[-1]))
        tbl.set("db", exp.to_identifier(parts[-2]) if len(parts) > 1 else None)
        tbl.set("catalog", exp.to_identifier(parts[-3]) if len(parts) > 2 else None)
    return tree.sql(dialect=dialect)
