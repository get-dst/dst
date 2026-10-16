"""Profile age is a fact every rail reads (``probe.max_age``).

A probe artifact is a snapshot: its dictionaries prove a literal absent only as
of the probe, and ``data_as_of`` dates the warehouse to that same probe. Nothing
at serve time read ``profiled_at``, so a stale snapshot proved a NEW value absent
(a false clarification from a guard whose contract forbids false positives)
and dated a fresh warehouse to the last probe. Pinned here, rail by rail: the
duration knob and its zero, the PROFILE line on the answer, the freshness and
date-coverage abstentions, the value guard consulting no dictionary, the
settings seam apply lands, observe and readiness carrying the age, the push-time
warning, and the test sweep refusing to call a stale lens verified. Within
max_age nothing changes.
"""

from __future__ import annotations

import argparse
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
from sqlalchemy import create_engine, text

from services.config import settings
from services.contracts.fakes import ScriptedLLM
from services.contracts.lens_config import LensConfig
from services.contracts.profile import ColumnProfile, TableProfile
from services.contracts.semantic_model import Entity, EntitySource, Field, SemanticModel
from services.db.session import org_session
from services.lenses import connection_store, profile_store
from services.lenses.profile_age import STALE_REASON, ProfileAge, connection_ages, profile_age
from services.lenses.store import LensBundle
from services.observability import observe
from services.project import probe_settings
from services.project.probe import ProbeArtifact, probe_age_warnings
from services.project.schema import ProbeConfig, parse_duration, parse_project_yaml
from services.runtime import assembly
from services.runtime.generator import GroundedSQLGenerator
from services.runtime.pipeline import run_query
from services.runtime.verification import date_coverage_check, freshness_check

CUSTOMERS = "customers"
STALE = ProfileAge(age=timedelta(days=12), max_age=timedelta(days=7))
FRESH = ProfileAge(age=timedelta(days=2), max_age=timedelta(days=7))


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


# ── the knob ─────────────────────────────────────────────────────────────────


def test_durations_parse_strictly() -> None:
    assert parse_duration("7d") == timedelta(days=7)
    assert parse_duration("48h") == timedelta(hours=48)
    assert parse_duration("30m") == timedelta(minutes=30)
    assert parse_duration("0") == timedelta(0)
    for bad in ("7", "week", "-1d", "1.5d", ""):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_the_probe_section_defaults_and_zero_disables() -> None:
    cfg = parse_project_yaml("name: t\n").probe
    assert cfg.max_age == "7d" and cfg.recent_partitions == 7
    assert cfg.max_age_delta == timedelta(days=7)
    cfg = parse_project_yaml("name: t\nprobe:\n  max_age: 0\n  recent_partitions: 0\n").probe
    assert cfg.max_age_delta is None  # the age rails are off
    assert profile_age([_profile(days_ago=400)], None).stale is False
    assert probe_age_warnings({"profiles/w.probe.json": _artifact(days_ago=400)}, None) == []
    with pytest.raises(ValueError, match="needs a unit"):
        parse_project_yaml("name: t\nprobe: {max_age: 7}\n")
    with pytest.raises(ValueError):
        parse_project_yaml("name: t\nprobe: {max_agee: 7d}\n")


def test_age_is_the_newest_profile_in_scope() -> None:
    profiles = [_profile(days_ago=30, table="old"), _profile(days_ago=2, table="new")]
    assert profile_age(profiles, timedelta(days=7)).stale is False
    assert profile_age(profiles, timedelta(days=7), tables={"old"}).stale is True
    assert profile_age([], timedelta(days=7)).age is None
    assert STALE.line() == "PROFILE: last probed 12 days ago (max 7) — run dst probe"


# ── the answer, the checks, the guard ────────────────────────────────────────


def _model(stale_after_days: int | None = None) -> SemanticModel:
    return SemanticModel(
        lens="sales",
        dialect="duckdb",
        stale_after_days=stale_after_days,
        entities=[
            Entity(
                name="customers",
                source=EntitySource(connection="warehouse", table=CUSTOMERS),
                fields=[Field(name="country", type="string"), Field(name="amount", type="number")],
            )
        ],
    )


def _profile(
    days_ago: float, table: str = CUSTOMERS, connection: str = "warehouse"
) -> TableProfile:
    at = datetime.now(UTC) - timedelta(days=days_ago)
    return TableProfile(
        connection=connection,
        table=table,
        profiled_at=at,
        last_updated_logical=at,
        columns=[
            ColumnProfile(
                name="country", type="VARCHAR", top_values=["FI", "DK"], values_complete=True
            )
        ],
    )


def _artifact(days_ago: float, connection: str = "warehouse") -> str:
    at = datetime.now(UTC) - timedelta(days=days_ago)
    return ProbeArtifact(
        connection=connection, probed_at=at, tables=[_profile(days_ago, connection=connection)]
    ).model_dump_json()


def _warehouse(tmp_path: Path) -> Any:
    from services.connectors.duckdb import DuckDBConnector

    con = duckdb.connect(str(tmp_path / "wh.duckdb"))
    con.execute("CREATE TABLE IF NOT EXISTS customers (country VARCHAR, amount DECIMAL(10,2))")
    con.execute("DELETE FROM customers")
    con.execute("INSERT INTO customers VALUES ('FI', 100.0), ('DK', 250.0)")
    con.close()
    return DuckDBConnector(str(tmp_path / "wh.duckdb"))


def _serve(tmp_path: Path, age: ProfileAge | None) -> Any:
    return run_query(
        question="total amount",
        lens_name="sales",
        org_id="org",
        caller="t",
        semantic_model=_model(stale_after_days=3),
        connector=_warehouse(tmp_path),
        generator=GroundedSQLGenerator(
            ScriptedLLM(['{"sql": "SELECT SUM(amount) AS total FROM customers"}'])
        ),
        composer=None,
        data_as_of=(date.today() - timedelta(days=10)).isoformat(),
        profile_age=age,
    )


def test_a_stale_profile_puts_a_profile_line_on_the_answer_and_its_trace(
    tmp_path: Path,
) -> None:
    res = _serve(tmp_path, STALE)
    assert res.trace.status == "ok"
    line = "PROFILE: last probed 12 days ago (max 7) — run dst probe"
    # The API door copies `degraded` onto the trace for every outcome, the
    # same way the UNTYPED and EXPOSED lines reach it (services/api/query.py).
    assert line in res.response.degraded
    assert res.response.verification is not None
    fresh = res.response.verification.check("freshness")
    assert fresh is not None and fresh.status == "skip" and fresh.reason == STALE_REASON


def test_within_max_age_nothing_changes(tmp_path: Path) -> None:
    for age in (FRESH, None):
        res = _serve(tmp_path, age)
        assert not any(n.startswith("PROFILE:") for n in res.response.degraded)
        assert res.response.verification is not None
        fresh = res.response.verification.check("freshness")
        # The lens declares 3 days and the data is 10 days old: the check FAILS
        # exactly as before — a stamp within max_age is a measurement.
        assert fresh is not None and fresh.status == "fail"


def test_freshness_and_coverage_skip_on_a_stale_profile_never_fail() -> None:
    from services.lenses.profile_enrich import EntityCoverage

    model = _model(stale_after_days=3)
    old = (date.today() - timedelta(days=30)).isoformat()
    assert freshness_check(old, model).status == "fail"
    skipped = freshness_check(old, model, profile_stale=True)
    assert skipped.status == "skip" and skipped.reason == STALE_REASON
    # No contract declared stays the structural skip, stale or not.
    assert freshness_check(old, _model(), profile_stale=True).reason == (
        "lens declares no stale_after_days"
    )
    coverage = {
        "customers": EntityCoverage(
            table=CUSTOMERS,
            column="created_at",
            start=date(2024, 1, 1),
            end=date(2024, 6, 30),
            declared=True,
        )
    }
    sql = "SELECT SUM(amount) FROM customers WHERE created_at >= '2026-01-01'"
    assert date_coverage_check("sales in 2026", sql, model, coverage).status == "fail"
    skipped = date_coverage_check("sales in 2026", sql, model, coverage, profile_stale=True)
    assert skipped.status == "skip" and skipped.reason == STALE_REASON


def _bundle() -> LensBundle:
    return LensBundle(
        config=LensConfig(name="sales", display_name="Sales", connections=["warehouse"]),
        semantic_model=_model(),
    )


def test_a_stale_dictionary_is_not_consulted_but_a_fresh_one_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past max_age every dictionary counts as partial: value_domains is empty,
    so the pre-query refusal cannot fire on a value the warehouse gained since
    the probe. Matching dictionaries (entity_domains) stay — a name they hold
    is still a name."""
    monkeypatch.setattr(assembly, "embed_question_degraded", lambda q: (None, None))
    monkeypatch.setattr(
        assembly, "certified_lookup", lambda *a, **k: (None, [], "none", None, None)
    )
    profiles = [_profile(days_ago=12)]
    for age, consulted in ((STALE, False), (FRESH, True), (None, True)):
        monkeypatch.setattr(
            assembly, "profile_facts", lambda b, o, _a=age: (profiles, None, _a, {})
        )
        got = assembly.assemble(_bundle(), "sales in Finland", "org")
        assert got.profile_age is age
        assert bool(got.value_domains) is consulted, age
        assert got.entity_domains == {"customers": {"country": ["FI", "DK"]}}


# ── the seam, observe, readiness, the sweep ──────────────────────────────────


def _cleanup(*orgs: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        for org in orgs:
            for table in ("table_profile", "connection", "setting", "request_log"):
                c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


@pytest.fixture
def org() -> Any:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        org_id = c.execute(
            text("INSERT INTO org (name) VALUES ('ProbeAgeTest') RETURNING id")
        ).scalar_one()
    yield org_id
    _cleanup(org_id)


@needs_db
def test_the_probe_section_lands_per_org_and_serving_reads_it(org: uuid.UUID) -> None:
    """apply stores the section under the org's setting row; a default section
    stores nothing; files win on the way back to defaults."""
    with org_session(org) as session:
        assert probe_settings.load(session) == ProbeConfig()
        assert probe_settings.apply(session, ProbeConfig()) == ([], [])
        applied, warnings = probe_settings.apply(
            session, ProbeConfig(max_age="48h", recent_partitions=3)
        )
    assert applied == ["probe: max_age 48h, 3 recent partitions"] and warnings == []
    with org_session(org) as session:
        assert probe_settings.load(session).max_age_delta == timedelta(hours=48)
        assert probe_settings.apply(session, ProbeConfig(max_age="48h", recent_partitions=3)) == (
            [],
            [],
        )
        applied, _ = probe_settings.apply(session, ProbeConfig(max_age="0"))
        assert applied == ["probe: age rails off, 7 recent partitions"]
    with org_session(org) as session:
        assert probe_settings.load(session).max_age_delta is None


@needs_db
def test_profile_facts_measures_the_age_against_the_org_setting(org: uuid.UUID) -> None:
    with org_session(org) as session:
        connection_store.create_connection(session, "warehouse", "duckdb", {}, None)
        profile_store.upsert_profile(session, _profile(days_ago=12))
    _profiles, _as_of, age, _stamps = assembly.profile_facts(_bundle(), org)
    assert age is not None and age.stale and age.max_age == timedelta(days=7)
    with org_session(org) as session:
        probe_settings.apply(session, ProbeConfig(max_age="30d"))
    _profiles, _as_of, age, _stamps = assembly.profile_facts(_bundle(), org)
    assert age is not None and not age.stale


@needs_db
def test_observe_and_ready_carry_the_age(org: uuid.UUID) -> None:
    from fastapi.testclient import TestClient

    from services.app import app

    with org_session(org) as session:
        connection_store.create_connection(session, "warehouse", "duckdb", {}, None)
        connection_store.create_connection(session, "lake", "duckdb", {}, None)
        profile_store.upsert_profile(session, _profile(days_ago=12))
        profile_store.upsert_profile(session, _profile(days_ago=1, connection="lake"))
    with org_session(org) as session:
        status = connection_ages(session)
        kpis = observe.kpis(session)
    assert status == {
        "oldest_age_days": 12.0,
        "max_age_days": 7.0,
        "stale_connections": ["warehouse"],
    }
    assert kpis["profiles"] == status
    body = TestClient(app).get("/ready").json()
    profiles = body["profiles"]
    assert profiles["oldest_age_days"] is not None and profiles["oldest_age_days"] >= 12.0
    assert "ProbeAgeTest/warehouse" in profiles["stale_connections"]
    assert "ProbeAgeTest/lake" not in profiles["stale_connections"]
    assert body["status"] in ("ready", "degraded")  # never keyed off the profile age


def test_observe_prints_the_profile_line(capsys: pytest.CaptureFixture[str]) -> None:
    from services.lenses.profile_age import headline

    assert headline({"oldest_age_days": 12.0, "max_age_days": 7.0, "stale_connections": ["w"]}) == (
        "profiles: oldest 12 d (max 7 d) — stale: w"
    )
    assert headline({"oldest_age_days": 2.5, "max_age_days": None, "stale_connections": []}) == (
        "profiles: oldest 2.5 d (no max_age)"
    )
    assert headline({"oldest_age_days": None}) is None


def test_plan_and_apply_warn_on_a_stale_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {"profiles/warehouse.probe.json": _artifact(days_ago=12)}
    assert probe_age_warnings(files, timedelta(days=7)) == [
        "profiles/warehouse.probe.json: last probed 12 days ago (max 7) — run `dst probe` "
        "and commit the result"
    ]
    assert probe_age_warnings(files, timedelta(days=30)) == []
    assert probe_age_warnings({"profiles/warehouse.json": "{}"}, timedelta(days=7)) == []

    from services.cli import main as cli

    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "warehouse.probe.json").write_text(
        files["profiles/warehouse.probe.json"]
    )
    (tmp_path / "dst.yaml").write_text("name: t\nprobe: {max_age: 10d}\n")
    for verb in ("plan", "apply"):
        cli._project_files(argparse.Namespace(dir=str(tmp_path)), verb)
        assert "last probed 12 days ago (max 10)" in capsys.readouterr().err
    (tmp_path / "dst.yaml").write_text("name: t\nprobe: {max_age: 0}\n")
    cli._project_files(argparse.Namespace(dir=str(tmp_path)), "plan")
    assert "last probed" not in capsys.readouterr().err


@needs_db
def test_the_sweep_skips_a_stale_lens_and_exits_4(
    org: uuid.UUID, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`dst test --all` cannot call a lens verified when the guards behind the
    verdict abstained: the lens is SKIPped with the reason and the run exits 4,
    the same not-green a CI gate already branches on."""
    from services.cli import main as cli
    from services.lenses import store as lens_store
    from services.llm import registry

    with org_session(org) as session:
        connection_store.create_connection(session, "warehouse", "duckdb", {}, None)
        profile_store.upsert_profile(session, _profile(days_ago=12))
    monkeypatch.setattr(cli, "_adopt_project_env", lambda args: None)
    monkeypatch.setattr(cli, "_test_scope", lambda args: [(org, "ProbeAgeTest")])
    monkeypatch.setattr(registry, "resolve", lambda ref: SimpleNamespace(llm=None, name="m"))
    monkeypatch.setattr(
        lens_store, "list_published", lambda session: [("sales", "Sales", "", _bundle())]
    )
    args = argparse.Namespace(
        all=True, lens=None, org=None, url=None, token=None, as_json=False, tag=None, dir="."
    )
    assert cli._test(args) == 4
    out = capsys.readouterr()
    assert "SKIP" in out.out and "ProbeAgeTest/sales: profiles last probed 12 days ago" in out.out
    assert "nothing was verified for ProbeAgeTest/sales" in out.err
