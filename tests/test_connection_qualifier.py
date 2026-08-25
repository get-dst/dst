"""The connection owns the catalog; the entity file owns the table.

A BigQuery project (a Snowflake database) is a property of the CONNECTION —
dst.yaml pins it, the client is built with it — and repeating it inside every
entity's `source.table` was the one thing that stopped one `semantic/` tree from
serving two environments: staging and production read different projects, so the
same commit could not address both. The compiler now stamps the connection's
catalog onto a connection-relative table, the way SQLMesh normalizes a model name
against its gateway's default catalog.

Both halves are pinned here, because the second one fails SILENTLY. Qualification
must happen (or a short name reaches the warehouse unresolved), and the
qualified name must still bind everything keyed by the AUTHORED name: profiles
(freshness stamps, value domains), certified-answer bindings, the read
allow-list. A short name that stopped binding its profile would not error — the
answer would simply lose its `data_as_of`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text

from services.certify.bindings import _ref_matches, foreign_tables
from services.config import settings
from services.contracts.lens_config import LensConfig
from services.contracts.profile import TableProfile
from services.contracts.shared_semantic import SelectSpec, SharedEntity
from services.db.session import org_session
from services.lenses import connection_store, profile_enrich
from services.project.apply import _default_qualifiers
from services.project.compile import compile_lens_model, default_qualifier, qualify_table
from services.project.schema import ConnectionDecl

ORDERS = SharedEntity.model_validate(
    {
        "name": "orders",
        "source": {"connection": "wh", "table": "marts.orders"},
        "fields": [{"name": "order_id", "type": "string"}, {"name": "amount", "type": "number"}],
    }
)


def _compile(qualifiers: dict[str, str] | None, *, table: str = "marts.orders"):
    entity = ORDERS.model_copy(
        update={"source": ORDERS.source.model_copy(update={"table": table})}, deep=True
    )
    model, _warnings = compile_lens_model(
        config=LensConfig(
            name="board",
            display_name="Board",
            connections=["wh"],
            select=SelectSpec.model_validate({"entities": [{"name": "orders"}]}),
        ),
        shared_entities={"orders": entity},
        shared_definitions={},
        local_definitions=[],
        use_when=["board numbers"],
        sample_queries=[],
        dialect="bigquery",
        default_qualifiers=qualifiers,
    )
    return model.entities[0].source.table


# ── the rule ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("conn_type", "config", "expected"),
    [
        ("bigquery", {"project": "acme-prod", "dataset": "marts"}, "acme-prod"),
        ("snowflake", {"database": "ANALYTICS"}, "ANALYTICS"),
        ("bigquery", {"dataset": "marts"}, None),  # project left to the SA JSON
        ("bigquery", {"project": "   "}, None),
        ("postgres", {"schema": "public"}, None),  # addresses schema.table already
        ("duckdb", {"path": ":memory:"}, None),
    ],
)
def test_only_a_declared_catalog_qualifies(conn_type, config, expected) -> None:
    assert default_qualifier(conn_type, config) == expected


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("marts.orders", "acme-prod.marts.orders"),
        ("other-project.marts.orders", "other-project.marts.orders"),  # explicit wins
        ("orders", "orders"),  # a catalog can't stand in for the missing schema
    ],
)
def test_qualify_fills_the_catalog_and_nothing_else(table, expected) -> None:
    assert qualify_table(table, "acme-prod") == expected


def test_no_qualifier_leaves_every_shape_untouched() -> None:
    assert qualify_table("marts.orders", None) == "marts.orders"


# ── the compile seam ─────────────────────────────────────────────────────────


def test_the_compiled_entity_carries_the_connections_catalog() -> None:
    assert _compile({"wh": "acme-prod"}) == "acme-prod.marts.orders"


def test_an_unpinned_connection_compiles_the_authored_name() -> None:
    assert _compile(None) == "marts.orders"


def test_the_same_entity_file_serves_two_environments() -> None:
    """The whole point: one tree, two physical addresses, no file edited."""
    assert _compile({"wh": "acme-staging"}) == "acme-staging.marts.orders"
    assert _compile({"wh": "acme-prod"}) == "acme-prod.marts.orders"


def test_an_entity_reaching_into_another_project_is_left_alone() -> None:
    assert _compile({"wh": "acme-prod"}, table="raw-vendor.stripe.charges") == (
        "raw-vendor.stripe.charges"
    )


# ── what the qualified name must still bind ──────────────────────────────────


def test_the_qualified_entity_binds_the_profile_its_short_name_authored() -> None:
    """Profiles are keyed by the name introspection returns — always fully
    qualified for BigQuery and Snowflake. Compiling the catalog in is what makes
    the freshness stamp reachable; authored short, it silently was not."""
    stamp = datetime(2026, 8, 24, 6, 0, tzinfo=UTC)
    profile = TableProfile(
        connection="wh", table="acme-prod.marts.orders", last_updated_logical=stamp
    )
    assert profile_enrich.data_as_of([profile], {_compile({"wh": "acme-prod"})}) == stamp


@pytest.mark.parametrize(
    ("ref", "table", "matches"),
    [
        ("marts.orders", "acme-prod.marts.orders", True),  # authored short, compiled long
        ("acme-prod.marts.orders", "acme-prod.marts.orders", True),
        ("orders", "acme-prod.marts.orders", True),  # bare, as BI SQL writes it
        ("other-project.marts.orders", "acme-prod.marts.orders", False),  # contradicts
        ("hr.orders", "analytics.orders", False),  # the allow-list's whole point
    ],
)
def test_a_shorter_name_denotes_the_same_table_a_different_one_never_does(
    ref, table, matches
) -> None:
    assert _ref_matches(ref, table) is matches


def test_certified_sql_written_against_the_short_name_is_not_foreign() -> None:
    """Certified SQL is authored, so it is authored against the portable name.
    Reading it as a foreign table would reject the answer at the lens boundary."""
    config = LensConfig(
        name="board",
        display_name="Board",
        connections=["wh"],
        select=SelectSpec.model_validate({"entities": [{"name": "orders"}]}),
    )
    model, _ = compile_lens_model(
        config=config,
        shared_entities={"orders": ORDERS},
        shared_definitions={},
        local_definitions=[],
        use_when=["board numbers"],
        sample_queries=[],
        dialect="bigquery",
        default_qualifiers={"wh": "acme-prod"},
    )
    assert foreign_tables("SELECT amount FROM marts.orders", model) == []
    assert foreign_tables("SELECT amount FROM other-project.marts.orders", model) == [
        "other-project.marts.orders"
    ]


# ── where the qualifier comes from, on both rails ────────────────────────────


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


@pytest.fixture()
def org():
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('QualifierTest') RETURNING id")
        ).scalar_one()
    yield oid
    with admin.begin() as c:
        c.execute(text("DELETE FROM connection WHERE org_id = :o"), {"o": oid})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})
    admin.dispose()


@needs_db
def test_the_declaration_outranks_the_record_the_way_the_dialect_does(org) -> None:
    """Apply lands connections before any lens compiles, so the DRY RUN has to
    read the push's own dst.yaml — including the push that CHANGES the project
    pin, and the push that removes it. Plan qualifying differently from the apply
    it predicts is the failure this rail exists to prevent."""
    with org_session(org) as session:
        connection_store.create_connection(
            session, "wh", "bigquery", {"project": "acme-prod", "dataset": "marts"}, None
        )
        session.commit()
    with org_session(org) as session:  # the org GUC is transaction-local
        assert _default_qualifiers(session) == {"wh": "acme-prod"}

        moved = {"wh": ConnectionDecl(type="bigquery", config={"project": "acme-staging"})}
        assert _default_qualifiers(session, declared=moved) == {"wh": "acme-staging"}

        unpinned = {"wh": ConnectionDecl(type="bigquery", config={"dataset": "marts"})}
        assert _default_qualifiers(session, declared=unpinned) == {}
