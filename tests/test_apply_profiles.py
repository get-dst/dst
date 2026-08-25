"""Apply lands the committed probe artifact in the store serving reads.

`dst probe` writes value dictionaries into the project; THIS is where they
reach the server — apply upserts each
pushed ``profiles/<conn>.probe.json`` into table_profile, the store
`assembly.profile_facts` already reads, so the literals land in the prompt with
zero runtime change. Pinned: the ingest and the enrich payoff, the no-downgrade
guard (a REST-refreshed server outranks an old commit), the two warn-and-skip
paths (unapplied connection, truncated artifact — advisory enrichment must
never abort an apply), and the selection rule (the schema-only drift baseline
in the same directory is never ingested)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from services.config import settings
from services.contracts.profile import ColumnProfile, TableProfile
from services.contracts.semantic_model import Entity, EntitySource, Field, SemanticModel
from services.db.session import org_session
from services.lenses import connection_store, profile_enrich, profile_store
from services.project.apply import apply_profiles
from services.project.probe import ProbeArtifact

CUSTOMERS = "ops.customers"


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


def _org() -> object:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        return c.execute(
            text("INSERT INTO org (name) VALUES ('ApplyProfilesTest') RETURNING id")
        ).scalar_one()


def _cleanup(*orgs: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        for org in orgs:
            c.execute(text("DELETE FROM table_profile WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM connection WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _artifact(
    connection: str = "jaffle", probed_at: datetime | None = None, values: bool = True
) -> str:
    at = probed_at or datetime.now(UTC)
    column = ColumnProfile(
        name="country",
        type="VARCHAR",
        top_values=["FI", "DK"] if values else None,
        values_complete=values,
    )
    return ProbeArtifact(
        connection=connection,
        probed_at=at,
        tables=[
            TableProfile(connection=connection, table=CUSTOMERS, profiled_at=at, columns=[column])
        ],
        entities={CUSTOMERS: ["customers"]},
    ).model_dump_json()


@needs_db
def test_the_artifact_lands_and_the_literals_reach_the_prompt() -> None:
    """The DoD end to end minus HTTP: ingest, then the exact enrichment serving
    runs — the sampled 'FI' must sit next to the field the generator prompts
    with, or the whole track changed nothing."""
    org = _org()
    try:
        with org_session(org) as session:
            connection_store.create_connection(session, "jaffle", "duckdb", {"path": ":m:"}, None)
            applied, warnings = apply_profiles(session, {"profiles/jaffle.probe.json": _artifact()})
            assert applied == ["profiles 'jaffle': landed 1 table(s)"] and warnings == []
            profiles = [s.profile for s in profile_store.list_profiles(session, "jaffle")]
        model = SemanticModel(
            lens="l",
            dialect="duckdb",
            entities=[
                Entity(
                    name="customers",
                    source=EntitySource(connection="jaffle", table=CUSTOMERS),
                    fields=[Field(name="country", type="string")],
                )
            ],
        )
        enriched = profile_enrich.enrich_model(model, profiles)
        assert enriched.entities[0].fields[0].description == "Values: 'FI', 'DK'"
    finally:
        _cleanup(org)


@needs_db
def test_a_newer_stored_profile_is_never_downgraded_by_an_old_commit() -> None:
    """Nightly REST refreshes outrank a stale artifact from a branch cut last
    week — per table, so the fresh half of a mixed artifact still lands."""
    org = _org()
    old = datetime.now(UTC) - timedelta(days=7)
    try:
        with org_session(org) as session:
            connection_store.create_connection(session, "jaffle", "duckdb", {"path": ":m:"}, None)
            apply_profiles(session, {"profiles/jaffle.probe.json": _artifact()})
            applied, warnings = apply_profiles(
                session, {"profiles/jaffle.probe.json": _artifact(probed_at=old, values=False)}
            )
            assert applied == ["profiles 'jaffle': landed 0 table(s), kept 1 newer server-side"]
            assert warnings == []
            stored = profile_store.get_profile(session, "jaffle", CUSTOMERS)
            assert stored is not None
            assert stored.profile.columns[0].top_values == ["FI", "DK"]  # not blanked
    finally:
        _cleanup(org)


@needs_db
def test_the_two_warn_and_skip_paths_never_abort() -> None:
    """Advisory enrichment: an artifact for a connection this server does not
    hold, and a truncated file, each cost a warning — never the apply."""
    org = _org()
    try:
        with org_session(org) as session:
            applied, warnings = apply_profiles(
                session,
                {
                    "profiles/ghost.probe.json": _artifact(connection="ghost"),
                    "profiles/broken.probe.json": "{truncated",
                },
            )
            assert applied == []
            assert any("connection 'ghost' is not applied" in w for w in warnings)
            assert any("profiles/broken.probe.json" in w for w in warnings)
            assert profile_store.list_profiles(session, "ghost") == []
    finally:
        _cleanup(org)


@needs_db
def test_only_probe_artifacts_are_ingested() -> None:
    """The schema-only drift baseline shares the directory; swallowing it would
    overwrite every value dictionary with a literal-free skeleton."""
    org = _org()
    try:
        with org_session(org) as session:
            connection_store.create_connection(session, "jaffle", "duckdb", {"path": ":m:"}, None)
            applied, warnings = apply_profiles(
                session,
                {"profiles/jaffle.json": _artifact()},  # baseline path, probe content
            )
            assert applied == [] and warnings == []
            assert profile_store.list_profiles(session, "jaffle") == []
    finally:
        _cleanup(org)
