"""A hidden column never passes the guard, however the statement reaches it.

`hide` is withheld two ways: an entity's own `hide`, and an `expose:` grant's
`hide`, which compiles onto the exposed entity's `hide`. Both hold on every
door, trusted SQL (a certified answer, `trust_tables`) included: trust skips
the table allow-list, never `hide`.

The matrix is query shapes x {entity hide, expose hide} x {alone, beside a
same-named table} x {trusted, untrusted} x every dialect. The twin is
`archive.patrons`, which shares `bakery.patrons`'s bare name and lets its own
`patron_phone` be read: what one table allows must never admit another's
column. A shape reaches the hidden column through a star in a CTE or
derived table, a set operation, a column rename, a lateral or correlated
subquery, a join key, a struct path, a whole-row read, and so on. Each is
refused, or — only where every table is exposed, so the guard rewrites stars
into the allowed columns — served with no hidden column and no star in it. On
DuckDB every statement the guard lets through is also run against a table
whose hidden column holds a sentinel, which must never come back. The controls
are the same shapes reading only allowed columns: they still pass.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import duckdb
import pytest

from services.contracts.lens_config import ExposeGrant, LensConfig
from services.contracts.profile import ColumnProfile, TableProfile
from services.contracts.semantic_model import EntitySource, Field, SemanticModel
from services.contracts.shared_semantic import SelectEntity, SelectSpec, SharedEntity
from services.project.compile import compile_lens_model
from services.project.expose import resolve_expose
from services.runtime import sql_guard
from services.runtime.identifiers import DIALECTS

WH = "warehouse"
HIDDEN = "patron_phone"
SENTINEL = "555-0199-sentinel"

_PATRON_FIELDS = [
    Field(name="patron_id", type="integer"),
    Field(name="town", type="string"),
    Field(name=HIDDEN, type="string"),
]
_ORDERS = SharedEntity(
    name="orders",
    source=EntitySource(connection=WH, table="bakery.orders"),
    fields=[
        Field(name="order_id", type="integer"),
        Field(name="patron_id", type="integer"),
        Field(name="amount", type="number"),
    ],
)


# A table sharing `bakery.patrons`'s bare name, whose `patron_phone` (and an
# `archived_on` the bakery table does not have) may be read.
_ARCHIVE_FIELDS = [*_PATRON_FIELDS, Field(name="archived_on", type="date")]
KINDS = ("entity", "expose", "entity+twin", "expose+twin")


def _model(kind: str, dialect: str) -> SemanticModel:
    """`patrons` with `patron_phone` hidden — by its entity (`entity`) or by
    the `expose:` grant that reads it (`expose`) — beside a plain `orders`;
    `+twin` adds `archive.patrons`, declared or exposed the same way, with
    nothing hidden."""
    base, _, twin = kind.partition("+")
    if base == "entity":
        shared = {
            "orders": _ORDERS,
            "patrons": SharedEntity(
                # Beside the twin, the bare name is the twin's table too and
                # cannot name an entity: both are named qualified.
                name="bakery_patrons" if twin else "patrons",
                source=EntitySource(connection=WH, table="bakery.patrons"),
                fields=_PATRON_FIELDS,
                hide=[HIDDEN],
            ),
        }
        if twin:
            shared["archive_patrons"] = SharedEntity(
                name="archive_patrons",
                source=EntitySource(connection=WH, table="archive.patrons"),
                fields=_ARCHIVE_FIELDS,
            )
        config = LensConfig(
            name="bakery",
            connections=[WH],
            select=SelectSpec(entities=[SelectEntity(name=e.name) for e in shared.values()]),
        )
        shared = {e.name: e for e in shared.values()}
    else:
        shared = {"orders": _ORDERS}
        grants = [ExposeGrant(connection=WH, tables=["bakery.patrons"], hide=[f"patrons.{HIDDEN}"])]
        if twin:
            grants.append(ExposeGrant(connection=WH, tables=["archive.patrons"]))
        config = LensConfig(
            name="bakery",
            connections=[WH],
            select=SelectSpec(entities=[SelectEntity(name="orders")]),
            expose=grants,
        )
    profiles = [
        TableProfile(
            connection=WH,
            table=table,
            profiled_at=datetime(2026, 10, 1, tzinfo=UTC),
            columns=[ColumnProfile(name=f.name, type="VARCHAR") for f in fields],
        )
        for table, fields in (
            ("bakery.patrons", _PATRON_FIELDS),
            ("archive.patrons", _ARCHIVE_FIELDS),
        )
    ]
    model, _warnings = compile_lens_model(
        config=config,
        shared_entities=shared,
        shared_definitions={},
        shared_relationships={},
        local_definitions=[],
        use_when=[],
        sample_queries=[],
        dialect=dialect,
        profiles={WH: profiles},
    )
    return model


# Shapes that read the hidden column. `dialects` narrows one to the engines
# whose syntax it is (`#3` is a comment in BigQuery and MySQL).
LEAKS: dict[str, str] = {
    "direct": "SELECT patron_phone FROM bakery.patrons",
    "qualified": "SELECT p.patron_phone FROM bakery.patrons p",
    "cte_star": "WITH x AS (SELECT * FROM bakery.patrons) SELECT x.patron_phone FROM x",
    "cte_star_bare": "WITH x AS (SELECT * FROM bakery.patrons) SELECT patron_phone FROM x",
    "cte_star_beside_a_declared_table": (
        "WITH x AS (SELECT * FROM bakery.patrons) SELECT x.patron_phone, o.amount FROM x "
        "JOIN bakery.orders o ON o.patron_id = x.patron_id"
    ),
    "cte_table_star": "WITH x AS (SELECT p.* FROM bakery.patrons p) SELECT x.patron_phone FROM x",
    "nested_cte": (
        "WITH a AS (SELECT * FROM bakery.patrons), b AS (SELECT * FROM a) "
        "SELECT b.patron_phone FROM b"
    ),
    "shadowing_cte": (
        "WITH patrons AS (SELECT * FROM bakery.patrons) SELECT patrons.patron_phone FROM patrons"
    ),
    "derived": "SELECT d.patron_phone FROM (SELECT * FROM bakery.patrons) d",
    "derived_unaliased": "SELECT patron_phone FROM (SELECT * FROM bakery.patrons)",
    "derived_nested": (
        "SELECT d.patron_phone FROM (SELECT * FROM (SELECT * FROM bakery.patrons) i) d"
    ),
    "outer_star": "SELECT * FROM bakery.patrons",
    "outer_star_cte": "WITH x AS (SELECT * FROM bakery.patrons) SELECT * FROM x",
    "outer_table_star_cte": "WITH x AS (SELECT * FROM bakery.patrons) SELECT x.* FROM x",
    "outer_star_derived": "SELECT * FROM (SELECT * FROM bakery.patrons) d",
    "union_branch": (
        "WITH x AS (SELECT town FROM bakery.patrons UNION ALL SELECT patron_phone "
        "FROM bakery.patrons) SELECT x.town FROM x"
    ),
    "union_top": (
        "SELECT town FROM bakery.patrons UNION ALL SELECT patron_phone FROM bakery.patrons"
    ),
    "union_star_by_position": (
        "WITH x AS (SELECT patron_id, town, town AS t3 FROM bakery.patrons UNION ALL "
        "SELECT * FROM bakery.patrons) SELECT x.t3 FROM x"
    ),
    "cte_column_rename": "WITH x(a, b, c) AS (SELECT * FROM bakery.patrons) SELECT x.c FROM x",
    "derived_column_rename": "SELECT d.c FROM (SELECT * FROM bakery.patrons) AS d(a, b, c)",
    "where": (
        "WITH x AS (SELECT * FROM bakery.patrons) SELECT x.town FROM x "
        "WHERE x.patron_phone LIKE '555%'"
    ),
    "group_having": (
        "WITH x AS (SELECT * FROM bakery.patrons) SELECT x.town, COUNT(*) AS n FROM x "
        "GROUP BY x.town HAVING MAX(x.patron_phone) > '5'"
    ),
    "order_by_bare": (
        "WITH x AS (SELECT * FROM bakery.patrons) SELECT x.town FROM x ORDER BY patron_phone"
    ),
    "aggregate": (
        "WITH x AS (SELECT * FROM bakery.patrons) SELECT COUNT(DISTINCT x.patron_phone) AS n FROM x"
    ),
    "window": (
        "WITH x AS (SELECT * FROM bakery.patrons) SELECT x.town, "
        "ROW_NUMBER() OVER (PARTITION BY x.patron_phone) AS r FROM x"
    ),
    "qualify": (
        "WITH x AS (SELECT * FROM bakery.patrons) SELECT x.town FROM x "
        "QUALIFY ROW_NUMBER() OVER (ORDER BY x.patron_phone) = 1"
    ),
    "join_on": (
        "WITH x AS (SELECT * FROM bakery.patrons) SELECT o.amount FROM bakery.orders o "
        "JOIN x ON x.patron_phone = '1'"
    ),
    "join_using": (
        "SELECT o.amount FROM bakery.orders o JOIN bakery.patrons p USING (patron_phone)"
    ),
    "join_using_cte": (
        "WITH x AS (SELECT * FROM bakery.patrons), y AS (SELECT * FROM bakery.patrons) "
        "SELECT x.town FROM x JOIN y USING (patron_phone)"
    ),
    "natural_join": "SELECT o.amount FROM bakery.orders o NATURAL JOIN bakery.patrons p",
    "lateral": (
        "SELECT o.amount, l.patron_phone FROM bakery.orders o, LATERAL (SELECT * FROM "
        "bakery.patrons p WHERE p.patron_id = o.patron_id) l"
    ),
    "correlated_scalar": (
        "SELECT o.amount, (SELECT x.patron_phone FROM (SELECT * FROM bakery.patrons) x "
        "WHERE x.patron_id = o.patron_id) AS v FROM bakery.orders o"
    ),
    "correlated_exists": (
        "SELECT o.amount FROM bakery.orders o WHERE EXISTS (SELECT * FROM bakery.patrons p "
        "WHERE p.patron_id = o.patron_id AND p.patron_phone = '1')"
    ),
    "in_subquery": (
        "WITH x AS (SELECT * FROM bakery.patrons) SELECT o.amount FROM bakery.orders o "
        "WHERE CAST(o.patron_id AS VARCHAR) IN (SELECT x.patron_phone FROM x)"
    ),
    "in_subquery_star": (
        "SELECT o.amount FROM bakery.orders o WHERE CAST(o.patron_id AS VARCHAR) IN "
        "(SELECT * FROM (SELECT patron_phone FROM bakery.patrons) z)"
    ),
    "in_subquery_star_by_position": (
        "SELECT o.amount FROM bakery.orders o WHERE CAST(o.patron_id AS VARCHAR) IN "
        "(SELECT * EXCLUDE (patron_id, town) FROM bakery.patrons)"
    ),
    "scalar_derived_star": (
        "SELECT (SELECT * FROM (SELECT patron_phone FROM bakery.patrons) z LIMIT 1) AS v"
    ),
    "scalar_star": (
        "SELECT o.amount, (SELECT * FROM bakery.patrons p WHERE p.patron_id = o.patron_id) "
        "AS v FROM bakery.orders o"
    ),
    "exists_cte": (
        "SELECT o.amount FROM bakery.orders o WHERE EXISTS (WITH x AS (SELECT * FROM "
        "bakery.patrons) SELECT 1 FROM x WHERE x.patron_phone = '1')"
    ),
    "recursive_cte": (
        "WITH RECURSIVE r AS (SELECT * FROM bakery.patrons UNION ALL SELECT * FROM r "
        "WHERE FALSE) SELECT r.patron_phone FROM r"
    ),
    "star_exclude": (
        "WITH x AS (SELECT * EXCLUDE (town) FROM bakery.patrons) SELECT x.patron_phone FROM x"
    ),
    "star_replace": (
        "WITH x AS (SELECT * REPLACE (town AS patron_id) FROM bakery.patrons) "
        "SELECT x.patron_phone FROM x"
    ),
    "star_like": "SELECT * LIKE 'patron%' FROM bakery.patrons",
    "columns_regex": "SELECT COLUMNS('patron_.*') FROM bakery.patrons",
    "columns_in_cte": (
        "WITH x AS (SELECT COLUMNS('.*') FROM bakery.patrons) SELECT x.patron_phone FROM x"
    ),
    "whole_row": "SELECT TO_JSON(p) FROM bakery.patrons p",
    "whole_row_table": "SELECT TO_JSON(patrons) FROM bakery.patrons",
    "whole_row_cte": "WITH x AS (SELECT * FROM bakery.patrons) SELECT TO_JSON(x) FROM x",
    "struct_of_star": "SELECT TO_JSON(STRUCT_PACK(p.*)) FROM bakery.patrons p",
    "row_to_json_star": "SELECT ROW_TO_JSON(p.*) FROM bakery.patrons p",
    "function_of_star": "SELECT HASH(*) FROM bakery.patrons",
    "object_construct": "SELECT OBJECT_CONSTRUCT(*) FROM bakery.patrons",
    "struct_path": "SELECT patron_phone.area FROM bakery.patrons",
    "struct_path_qualified": "SELECT p.patron_phone.area FROM bakery.patrons p",
    "bracket": "SELECT p.patron_phone['area'] FROM bakery.patrons p",
    "json_arrow": "SELECT p.patron_phone ->> 'area' FROM bakery.patrons p",
    "unpivot": "SELECT u.v FROM bakery.patrons UNPIVOT (v FOR k IN (patron_phone, town)) u",
    "upper_case": "WITH x AS (SELECT * FROM bakery.patrons) SELECT x.PATRON_PHONE FROM x",
    "extra_qualifier": "SELECT patron_phone FROM warehouse.bakery.patrons",
    "as_struct": "SELECT ARRAY(SELECT AS STRUCT * FROM bakery.patrons)",
    "positional": "SELECT #3 FROM bakery.patrons",
    "positional_in_cte": "WITH x AS (SELECT #3 AS c FROM bakery.patrons) SELECT x.c FROM x",
    "positional_dollar": "SELECT $3 FROM bakery.patrons",
}
_ONLY: dict[str, tuple[str, ...]] = {
    "positional": ("duckdb",),
    "positional_in_cte": ("duckdb",),
    "positional_dollar": ("snowflake",),
}
CONTROLS: dict[str, str] = {
    "direct": "SELECT p.town, COUNT(*) AS n FROM bakery.patrons p GROUP BY p.town",
    "count_star": "SELECT COUNT(*) AS n FROM bakery.patrons",
    "cte_star": "WITH x AS (SELECT * FROM bakery.patrons) SELECT x.town FROM x",
    "cte_star_bare": "WITH x AS (SELECT * FROM bakery.patrons) SELECT town FROM x",
    "cte_count": "WITH x AS (SELECT * FROM bakery.patrons) SELECT COUNT(*) AS n FROM x",
    "cte_chain": (
        "WITH a AS (SELECT * FROM bakery.patrons), b AS (SELECT a.town, a.patron_id FROM a) "
        "SELECT b.town, COUNT(*) AS n FROM b GROUP BY b.town"
    ),
    "derived": "SELECT d.town FROM (SELECT * FROM bakery.patrons) d",
    "join": (
        "SELECT p.town, SUM(o.amount) AS total FROM bakery.orders o JOIN bakery.patrons p "
        "ON p.patron_id = o.patron_id GROUP BY p.town"
    ),
    "join_using": ("SELECT o.amount FROM bakery.orders o JOIN bakery.patrons p USING (patron_id)"),
    "exists": (
        "SELECT o.amount FROM bakery.orders o WHERE EXISTS (SELECT * FROM bakery.patrons p "
        "WHERE p.patron_id = o.patron_id)"
    ),
    "lateral": (
        "SELECT o.amount, l.town FROM bakery.orders o, LATERAL (SELECT * FROM bakery.patrons "
        "p WHERE p.patron_id = o.patron_id) l"
    ),
    "union": (
        "WITH x AS (SELECT * FROM bakery.patrons) SELECT x.town FROM x UNION ALL "
        "SELECT p.town FROM bakery.patrons p"
    ),
    "star_exclude": (
        "WITH x AS (SELECT * EXCLUDE (town) FROM bakery.patrons) SELECT x.patron_id FROM x"
    ),
}

# Only where every table read is exposed does the guard rewrite a star into the
# table's allowed columns instead of refusing it — so on the expose side these
# may pass, as long as what is served names no hidden column and keeps no star
# (a rename or a union then no longer lines up, and the warehouse refuses it).
_REWRITTEN = {
    "outer_star",
    "outer_star_cte",
    "outer_table_star_cte",
    "outer_star_derived",
    "union_star_by_position",
    "cte_column_rename",
    "derived_column_rename",
    "whole_row_cte",
    "as_struct",
}


def _cases(shapes: dict[str, str]) -> list[tuple[str, str, str, bool, str]]:
    return [
        (name, kind, dialect, trusted, sql)
        for name, sql in shapes.items()
        for kind in KINDS
        for trusted in (False, True)
        for dialect in _ONLY.get(name, DIALECTS)
    ]


def _ids(case: tuple[str, str, str, bool, str]) -> str:
    name, kind, dialect, trusted, _sql = case
    return f"{name}-{kind}-{dialect}-{'trusted' if trusted else 'untrusted'}"


def _duckdb_rows(sql: str) -> str | None:
    """What ``sql`` returns from a DuckDB holding the hidden sentinel, or None
    when the statement does not run."""
    con = duckdb.connect()
    try:
        con.execute("CREATE SCHEMA bakery")
        con.execute(
            "CREATE TABLE bakery.patrons (patron_id INTEGER, town VARCHAR, patron_phone VARCHAR)"
        )
        con.execute(
            "CREATE TABLE bakery.orders (order_id INTEGER, patron_id INTEGER, amount DOUBLE)"
        )
        con.execute("CREATE SCHEMA archive")
        con.execute(
            "CREATE TABLE archive.patrons (patron_id INTEGER, town VARCHAR, "
            "patron_phone VARCHAR, archived_on DATE)"
        )
        con.execute(f"INSERT INTO bakery.patrons VALUES (1, 'Harbor', '{SENTINEL}')")
        con.execute("INSERT INTO archive.patrons VALUES (2, 'Quay', '555-0100', '2026-01-01')")
        con.execute("INSERT INTO bakery.orders VALUES (10, 1, 4.5)")
        try:
            return repr(con.execute(sql).fetchall())
        except duckdb.Error:
            return None
    finally:
        con.close()


_MODELS: dict[tuple[str, str], SemanticModel] = {}


def _cached(kind: str, dialect: str) -> SemanticModel:
    if (kind, dialect) not in _MODELS:
        _MODELS[(kind, dialect)] = _model(kind, dialect)
    return _MODELS[(kind, dialect)]


@pytest.mark.parametrize("case", _cases(LEAKS), ids=_ids)
def test_a_hidden_column_never_passes(case: tuple[str, str, str, bool, str]) -> None:
    name, kind, dialect, trusted, sql = case
    model = _cached(kind, dialect)
    result = sql_guard.check(sql, model, trust_tables=trusted)
    if result.ok and dialect == "duckdb":
        rows = _duckdb_rows(result.sql or "")
        assert rows is None or SENTINEL not in rows, f"served {result.sql!r} -> {rows}"
    if kind.startswith("expose") and name in _REWRITTEN:
        served = (result.sql or "").lower().replace("count(*)", "")
        assert not result.ok or (HIDDEN not in served and "*" not in served), result.sql
        return
    assert not result.ok, f"served {result.sql!r}"
    # Refused on scope or `hide`, never mistaken for a syntax fault, unless the
    # dialect has no such syntax at all.
    assert result.kind == "policy" or result.reason


@pytest.mark.parametrize("case", _cases(CONTROLS), ids=_ids)
def test_a_query_reading_allowed_columns_still_passes(
    case: tuple[str, str, str, bool, str],
) -> None:
    _name, kind, dialect, trusted, sql = case
    result = sql_guard.check(sql, _cached(kind, dialect), trust_tables=trusted)
    if not result.ok and result.kind == "syntax":
        pytest.skip(f"{dialect} has no such syntax: {result.reason}")
    assert result.ok, result.reason
    assert HIDDEN not in (result.sql or "").lower()


def test_an_expose_hide_is_the_exposed_entitys_hide() -> None:
    """The grant's hide rides on the entity — exactly the withheld column, so
    every door that reads `hide` withholds it."""
    model = _cached("expose", "duckdb")
    patrons = next(e for e in model.entities if e.name == "patrons")
    assert patrons.exposed and not patrons.exposed_whole
    assert patrons.hide == [f"patrons.{HIDDEN}"]
    assert HIDDEN not in {f.name for f in patrons.fields}
    # A grant pattern broader than one table withholds exactly what it matched.
    config = LensConfig(
        name="bakery",
        connections=[WH],
        expose=[ExposeGrant(connection=WH, tables=["bakery.*"], hide=["*.patron_*"])],
    )
    profile = TableProfile(
        connection=WH,
        table="bakery.patrons",
        profiled_at=datetime(2026, 10, 1, tzinfo=UTC),
        columns=[
            ColumnProfile(name=n, type="VARCHAR") for n in ("patron_[x]", "patron_id", "town")
        ],
    )
    entities, errors = resolve_expose(config, {WH: [profile]})
    assert errors == []
    (entity,) = entities
    assert [f.name for f in entity.fields] == ["town"]
    assert sorted(entity.hide) == ["patrons.patron_[[]x]", "patrons.patron_id"]  # type: ignore[type-var]


@pytest.mark.parametrize("kind", ["entity", "expose"])
def test_certified_sql_reading_a_hidden_column_is_withheld(kind: str) -> None:
    """`withheld_reads` — what keeps a certified answer off a caller — resolves
    through a star the same way the guard does."""
    model = _cached(kind, "duckdb")
    assert sql_guard.withheld_reads(LEAKS["cte_star"], model) == [f"patrons.{HIDDEN}"]
    assert sql_guard.withheld_reads(LEAKS["direct"], model) == [f"patrons.{HIDDEN}"]
    assert sql_guard.withheld_reads(CONTROLS["cte_star"], model) == []


def test_the_refusal_names_the_column_or_the_star() -> None:
    model = _cached("entity", "duckdb")
    untrusted = sql_guard.check(LEAKS["cte_star"], model)
    assert untrusted.reason == f"column 'patrons.{HIDDEN}' is out of lens scope"
    trusted = sql_guard.check(LEAKS["cte_star"], model, trust_tables=True)
    assert trusted.reason == f"column 'patrons.{HIDDEN}' is hidden by the lens and cannot be read"
    star = sql_guard.check(LEAKS["whole_row"], model, trust_tables=True)
    assert star.reason and "star or whole-row read over 'patrons'" in (star.reason or "")
    assert re.search(r"select explicit columns$", star.reason or "")


def test_an_exposed_table_applied_before_its_hide_was_recorded_fails_closed() -> None:
    """A lens applied by an earlier release stored its exposed table without
    the grant's `hide` on it, only `exposed_whole: false`. Until it is applied
    again, trusted SQL may read only the columns the table lists."""
    model = _cached("expose", "duckdb").model_copy(deep=True)
    patrons = next(e for e in model.entities if e.name == "patrons")
    patrons.hide = []
    for trusted in (False, True):
        assert not sql_guard.check(LEAKS["direct"], model, trust_tables=trusted).ok
        assert not sql_guard.check(LEAKS["cte_star"], model, trust_tables=trusted).ok
        assert sql_guard.check(CONTROLS["cte_star"], model, trust_tables=trusted).ok
    assert sql_guard.withheld_reads(LEAKS["direct"], model) == [f"patrons.{HIDDEN}"]


def test_a_shape_the_scope_walk_cannot_read_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """When sqlglot cannot build or walk the scopes, the hidden column is read
    by name and any star over a hiding entity is refused — never let through."""

    def unreadable(_stmt: object) -> None:
        raise ValueError("no scopes")

    monkeypatch.setattr(sql_guard, "build_scope", unreadable)
    model = _cached("entity", "duckdb")
    for trusted in (False, True):
        assert not sql_guard.check(LEAKS["cte_star"], model, trust_tables=trusted).ok
        assert not sql_guard.check(LEAKS["whole_row"], model, trust_tables=trusted).ok
        assert not sql_guard.check(
            LEAKS["in_subquery_star_by_position"], model, trust_tables=trusted
        ).ok
        assert sql_guard.check(CONTROLS["count_star"], model, trust_tables=trusted).ok


# What `archive.patrons` lets be read and `bakery.patrons` does not hold: read
# on the bakery table, it is out of scope however the twin is ordered.
TWIN_LEAKS = {
    "qualified": "SELECT p.archived_on FROM bakery.patrons p",
    "bare": "SELECT archived_on FROM bakery.patrons",
    "beside_the_twin": (
        "SELECT p.archived_on, a.town FROM bakery.patrons p JOIN archive.patrons a "
        "ON a.patron_id = p.patron_id"
    ),
    "cte": "WITH x AS (SELECT archived_on FROM bakery.patrons) SELECT x.archived_on FROM x",
}
TWIN_CONTROLS = {
    "qualified": "SELECT a.archived_on, a.patron_phone FROM archive.patrons a",
    "bare": "SELECT archived_on FROM archive.patrons",
    "join": (
        "SELECT p.town, a.archived_on FROM bakery.patrons p JOIN archive.patrons a "
        "ON a.patron_id = p.patron_id"
    ),
}


@pytest.mark.parametrize("reverse", [False, True], ids=["twin_last", "twin_first"])
@pytest.mark.parametrize("kind", ["entity+twin", "expose+twin"])
@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_same_named_table_never_lends_its_columns(kind: str, dialect: str, reverse: bool) -> None:
    """Two tables sharing a bare name keep their own column scopes: neither
    table's allowed columns admit a read of the other's, in either order."""
    model = _cached(kind, dialect).model_copy(deep=True)
    if reverse:
        model.entities.reverse()
    for name, sql in TWIN_LEAKS.items():
        result = sql_guard.check(sql, model)
        assert not result.ok, f"{name}: served {result.sql!r}"
    for name, sql in TWIN_CONTROLS.items():
        result = sql_guard.check(sql, model)
        assert result.ok, f"{name}: {result.reason}"


@pytest.mark.parametrize("reverse", [False, True], ids=["twin_last", "twin_first"])
def test_a_star_over_an_exposed_twin_is_its_own_columns(reverse: bool) -> None:
    """Every table exposed: a star becomes the columns of the table it reads,
    never those of a table sharing its bare name."""
    model = _cached("expose+twin", "duckdb").model_copy(deep=True)
    if reverse:
        model.entities.reverse()
    archive = sql_guard.check("SELECT * FROM archive.patrons", model)
    assert archive.ok and "archived_on" in (archive.sql or ""), archive
    bakery = sql_guard.check("SELECT * FROM bakery.patrons", model)
    served = (bakery.sql or "").lower()
    assert not bakery.ok or ("archived_on" not in served and HIDDEN not in served), bakery


# A column the lens never modeled, read bare where a CTE or derived table
# projects it: the projection is the read, so it cannot admit itself.
UNMODELED = {
    "cte": "WITH x AS (SELECT archived_on FROM bakery.patrons) SELECT x.archived_on FROM x",
    "cte_unused": "WITH x AS (SELECT archived_on FROM bakery.patrons) SELECT COUNT(*) AS n FROM x",
    "derived": "SELECT d.archived_on FROM (SELECT archived_on FROM bakery.patrons) d",
    "nested": (
        "WITH a AS (SELECT archived_on FROM bakery.patrons), b AS (SELECT a.archived_on FROM a) "
        "SELECT b.archived_on FROM b"
    ),
    "in_subquery_derived": (
        "SELECT o.amount FROM bakery.orders o WHERE EXISTS (SELECT 1 FROM "
        "(SELECT archived_on FROM bakery.patrons) d WHERE d.archived_on IS NULL)"
    ),
}
UNMODELED_CONTROLS = {
    "cte": "WITH x AS (SELECT town FROM bakery.patrons) SELECT x.town FROM x",
    "cte_alias": (
        "WITH x AS (SELECT town AS place FROM bakery.patrons) SELECT place, COUNT(*) AS n "
        "FROM x GROUP BY place"
    ),
    "correlated": (
        "SELECT o.amount FROM bakery.orders o WHERE EXISTS (SELECT 1 FROM bakery.patrons p "
        "WHERE p.patron_id = o.patron_id AND amount > 1)"
    ),
    "correlated_derived": (
        "SELECT o.amount FROM bakery.orders o WHERE EXISTS (SELECT 1 FROM "
        "(SELECT p.town FROM bakery.patrons p WHERE p.patron_id = order_id) d)"
    ),
}


@pytest.mark.parametrize("kind", ["entity", "expose"])
@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_unmodeled_column_never_admits_itself(kind: str, dialect: str) -> None:
    model = _cached(kind, dialect)
    for name, sql in UNMODELED.items():
        result = sql_guard.check(sql, model)
        assert not result.ok, f"{name}: served {result.sql!r}"
    for name, sql in UNMODELED_CONTROLS.items():
        result = sql_guard.check(sql, model)
        assert result.ok, f"{name}: {result.reason}"
