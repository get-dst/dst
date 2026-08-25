"""`dst evals migrate` output must be executable and reviewed:
leaf table names repoint at their live sources, and only APPROVED value cases
are promoted into the serving corpus."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml

from services.cli.main import _evals_migrate


def _scaffold(tmp_path: Path) -> Path:
    from services.cli.init import run_init

    root = tmp_path / "proj"
    ns = argparse.Namespace(dir=str(root), name=None, warehouse=None, example=None, yes=True)
    assert run_init(ns) == 0
    return root


def test_migrate_qualifies_leaf_tables_to_live_sources(tmp_path: Path) -> None:
    # Eval-plane SQL names leaf tables by design; certified SQL executes live
    # and failed resolution until hand-qualified.
    root = _scaffold(tmp_path)
    (root / "semantic" / "entities").mkdir(parents=True, exist_ok=True)
    # A leaf name the scaffold does not already claim — a leaf mapped by two
    # entities is deliberately left unqualified.
    (root / "semantic" / "entities" / "repeat_buyers.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "repeat_buyers",
                "source": {"connection": "jaffle", "table": "main.repeat_buyers"},
                "fields": [{"name": "customer_id", "type": "string"}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    lens_dir = root / "lenses" / "customer_value"
    (lens_dir / "evals" / "cases.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "question": "How many repeat buyers?",
                    "expected_sql": "SELECT COUNT(*) FROM repeat_buyers",
                    "status": "approved",
                }
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert _evals_migrate(argparse.Namespace(dir=str(root))) == 0
    entries = yaml.safe_load((lens_dir / "certified_answers.yaml").read_text(encoding="utf-8"))
    migrated = [e for e in entries if e.get("source") == "evals:migrated"]
    assert len(migrated) == 1
    assert "main.repeat_buyers" in migrated[0]["sql"]  # qualified, alias-preserving
    assert "AS repeat_buyers" in migrated[0]["sql"]


def test_migrate_never_promotes_unapproved_cases(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Certified answers SERVE on match; a candidate case is unreviewed SQL.
    root = _scaffold(tmp_path)
    lens_dir = root / "lenses" / "customer_value"
    cases_path = lens_dir / "evals" / "cases.yaml"
    cases_path.write_text(
        yaml.safe_dump(
            [{"question": "How many repeat customers?", "expected_sql": "SELECT 19"}],
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert _evals_migrate(argparse.Namespace(dir=str(root))) == 0
    out = capsys.readouterr().out
    assert "only approved cases migrate" in out
    cert_path = lens_dir / "certified_answers.yaml"
    entries = yaml.safe_load(cert_path.read_text(encoding="utf-8")) if cert_path.exists() else []
    assert not [e for e in entries or [] if e.get("source") == "evals:migrated"]
    kept = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    assert kept and kept[0]["question"] == "How many repeat customers?"
