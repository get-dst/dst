"""`dst demo` for a deployment other people reach, and `dst prune-log`.

The bundled lens grants nobody by default; the flags that make it a public demo
(a warehouse path, a token by env ref, a timeout, a caller group, a daily
quota) land on the connection and the published lens, and re-running updates
in place. The path is probed before anything lands — a dead path is exit 2
and no half-published lens.
"""

from __future__ import annotations

import argparse
import json
import shutil
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from services.cli.main import _demo, _prune_log
from services.config import settings
from services.db.session import org_session
from services.lenses import connection_store, store
from services.lenses.demo import LENS_NAME


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
_admin = create_engine(settings.database_admin_url)


@pytest.fixture
def org() -> Iterator[uuid.UUID]:
    with _admin.begin() as c:
        oid = c.execute(text("INSERT INTO org (name) VALUES ('CliDemo') RETURNING id")).scalar_one()
    try:
        yield uuid.UUID(str(oid))
    finally:
        with _admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})


def _ns(org: uuid.UUID, **flags: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "org_id": str(org),
        "path": None,
        "secret_env": None,
        "statement_timeout_ms": None,
        "allow_group": None,
        "per_caller_rpd": None,
    }
    base.update(flags)
    return argparse.Namespace(**base)


def _published(org: uuid.UUID) -> dict[str, object]:
    with org_session(org) as session:
        row = session.execute(
            text("SELECT published_json FROM lens WHERE name = :n"), {"n": LENS_NAME}
        ).scalar_one()
    return row if isinstance(row, dict) else json.loads(row)


@needs_db
def test_default_is_the_bundled_file_admins_only(
    org: uuid.UUID, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _demo(_ns(org)) == 0
    assert "published" in capsys.readouterr().out
    with org_session(org) as session:
        rec = connection_store.get_connection(session, "jaffle")
    assert rec is not None and rec.config == {"path": settings.duckdb_jaffle_path}
    cfg = _published(org)["config"]
    assert cfg["access"]["allow"] == []
    assert cfg["rate_limit"]["per_caller_rpd"] == 0


@needs_db
def test_public_flags_land_and_rerun_updates_in_place(org: uuid.UUID, tmp_path: Path) -> None:
    own = tmp_path / "own.duckdb"
    shutil.copy(settings.duckdb_jaffle_path, own)
    assert (
        _demo(
            _ns(
                org,
                path=str(own),
                statement_timeout_ms=500,
                allow_group=["demo"],
                per_caller_rpd=40,
            )
        )
        == 0
    )
    with org_session(org) as session:
        rec = connection_store.get_connection(session, "jaffle")
    assert rec is not None
    assert rec.config == {"path": str(own), "statement_timeout_ms": 500}
    cfg = _published(org)["config"]
    assert [a["group"] for a in cfg["access"]["allow"]] == ["demo"]
    assert cfg["rate_limit"]["per_caller_rpd"] == 40
    # Re-run with different flags: same lens, updated, still published.
    assert _demo(_ns(org, path=str(own), allow_group=["demo", "everyone"], per_caller_rpd=5)) == 0
    cfg = _published(org)["config"]
    assert [a["group"] for a in cfg["access"]["allow"]] == ["demo", "everyone"]
    assert cfg["rate_limit"]["per_caller_rpd"] == 5
    with org_session(org) as session:
        rec = connection_store.get_connection(session, "jaffle")
        assert rec is not None and rec.config == {"path": str(own)}
        assert store.lens_exists(session, LENS_NAME)


@needs_db
def test_dead_path_is_a_refusal_not_a_half_publish(
    org: uuid.UUID, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _demo(_ns(org, path=str(tmp_path / "missing.duckdb"))) == 2
    assert "cannot read" in capsys.readouterr().err
    with org_session(org) as session:
        assert connection_store.get_connection(session, "jaffle") is None
        assert not store.lens_exists(session, LENS_NAME)


@needs_db
def test_unset_secret_env_is_named(
    org: uuid.UUID, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DEMO_MD_TOKEN", raising=False)
    assert _demo(_ns(org, path="md:dst_demo", secret_env="DEMO_MD_TOKEN")) == 2
    assert "DEMO_MD_TOKEN is not set" in capsys.readouterr().err


@needs_db
def test_prune_log_keeps_recent_rows(org: uuid.UUID, capsys: pytest.CaptureFixture[str]) -> None:
    def row(age_days: int) -> None:
        with org_session(org) as session:
            session.execute(
                text(
                    "INSERT INTO request_log (org_id, request_id, lens, caller, question, "
                    "status, created_at) VALUES (:o, :r, 'l', 'c', 'q', 'ok', :t)"
                ),
                {
                    "o": org,
                    "r": str(uuid.uuid4()),
                    "t": datetime.now(UTC) - timedelta(days=age_days),
                },
            )

    row(1)
    row(10)
    row(40)
    assert _prune_log(argparse.Namespace(keep_days=30, org_id=str(org))) == 0
    out = capsys.readouterr().out
    assert "CliDemo: 1 row(s)" in out
    with org_session(org) as session:
        left = session.execute(text("SELECT count(*) FROM request_log")).scalar_one()
    assert left == 2
    assert _prune_log(argparse.Namespace(keep_days=0, org_id=str(org))) == 2
    assert _prune_log(argparse.Namespace(keep_days=30, org_id=str(uuid.uuid4()))) == 2
