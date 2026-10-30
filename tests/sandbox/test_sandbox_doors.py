"""The doors a typed-only install goes through: apply, and the certified rail
of the lens query — against the database, as the server runs them.

Two facts from the same run. First, an install whose only provider is a
typed-decision provider (no chat tier anywhere, ``untyped_fallback`` off on
every lens) must still apply: typed serving needs no model that writes
text. Second, a question whose parts the lens certifies must be served from
the certified answers, not clarified past them.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import ProviderConfig, settings
from services.contracts.fakes import HashEmbedder, ScriptedLLM, fake_llm_providers
from services.db.session import org_session
from services.lenses import connection_store
from tests.sandbox._world import CATALOGUE, RecordedDecider, World, semantic_files
from tests.test_query_api import needs_db

client = TestClient(app)
LVI, SHOP, SE = "Nordlund LVI Oy", "Nordlund Verkkokauppa", "Bergström Rör AB"
REVENUE = "intermediate.int_revenue_by_entity_month"
FAKTUROR = "raw_fortnox_bergstrom.fakturor"


@pytest.fixture()
def org(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[object, dict[str, str], World]]:
    from services.project import apply as apply_engine

    monkeypatch.setattr(apply_engine, "_embedder", lambda: HashEmbedder())
    world = World(tmp_path / "doors.duckdb")
    world.table(
        REVENUE,
        {
            "month": "VARCHAR",
            "entity": "VARCHAR",
            "revenue_stream": "VARCHAR",
            "revenue_eur": "DOUBLE",
            "fx_method": "VARCHAR",
        },
        [
            ("2026-07", LVI, "Service contracts", 270_000.0, "average"),
            ("2026-08", LVI, "Service contracts", 280_000.0, "average"),
            ("2026-08", LVI, "Intercompany", 30_000.0, "average"),
            ("2026-07", SHOP, "Webshop", 2_500_000.0, "average"),
            ("2026-08", SHOP, "Webshop", 2_600_000.0, "average"),
            ("2026-07", SE, "Contracting", 160_000.0, "average"),
            ("2026-08", SE, "Contracting", 46_877.0, "average"),
        ],
    )
    world.table(
        FAKTUROR,
        {
            "fakturanummer": "BIGINT",
            "kundnummer": "BIGINT",
            "fakturadatum": "DATE",
            "forfallodatum": "DATE",
            "belopp_inkl_moms": "DOUBLE",
            "belopp_exkl_moms": "DOUBLE",
            "moms": "DOUBLE",
            "valuta": "VARCHAR",
            "status": "VARCHAR",
            "projekt": "VARCHAR",
            "rakenskapsar": "VARCHAR",
        },
        [
            (
                1007,
                7,
                date(2026, 7, 10),
                date(2026, 7, 24),
                2_212_500.0,
                1_770_000.0,
                442_500.0,
                "SEK",
                "Betald",
                None,
                "2026",
            ),
            (
                1008,
                7,
                date(2026, 8, 10),
                date(2026, 8, 24),
                643_386.8,
                514_709.44,
                128_677.36,
                "SEK",
                "Betald",
                None,
                "2026",
            ),
        ],
    )
    world.seal()
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        c.execute(text("DELETE FROM embedding_meta"))
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('SandboxDoors') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": oid, "h": hash_token(raw)},
        )
    with org_session(oid) as s:
        connection_store.create_connection(
            s, "warehouse", "duckdb", {"path": str(world.path)}, None
        )
        s.commit()
    yield oid, {"Authorization": f"Bearer {raw}"}, world
    with admin.begin() as c:
        for table in (
            "eval_run",
            "eval_case",
            "certified_answer",
            "lens_version",
            "lens",
            "semantic_asset",
            "connection",
            "admin_token",
        ):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": oid})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})
        c.execute(text("DELETE FROM embedding_meta"))
    admin.dispose()


def _lens_files(
    lens: str, entities: list[str], *, eval_gate: str, certified: bool
) -> dict[str, str]:
    """The catalogue's files for one lens, its selection cut to ``entities``,
    ``untyped_fallback`` off as in the run."""
    files = semantic_files()
    cfg = yaml.safe_load((CATALOGUE / f"lenses/{lens}/lens.yaml").read_text())
    cfg.pop("expose", None)
    cfg["untyped_fallback"] = False
    cfg["eval_gate"] = eval_gate
    cfg["select"]["entities"] = [
        e for e in cfg["select"]["entities"] if (e if isinstance(e, str) else e["name"]) in entities
    ]
    kept = set()
    for path in files:
        if path.startswith("semantic/definitions/"):
            about = str(yaml.safe_load(files[path].split("---")[1]).get("about") or "")
            if not about or about.split(".")[0] in entities:
                kept.add(Path(path).stem)
    cfg["select"]["definitions"] = [d for d in cfg["select"].get("definitions", []) if d in kept]
    files[f"lenses/{lens}/lens.yaml"] = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)
    if certified:
        answers = yaml.safe_load((CATALOGUE / f"lenses/{lens}/certified_answers.yaml").read_text())
        wanted = (
            "share of group revenue from Sweden in {period}",
            "Bergström Rör revenue in SEK by month in {period}",
        )
        files[f"lenses/{lens}/certified_answers.yaml"] = yaml.safe_dump(
            [a for a in answers if a["question"] in wanted], sort_keys=False, allow_unicode=True
        )
    return files


def _apply(headers: dict[str, str], files: dict[str, str]) -> list[dict[str, object]]:
    r = client.post("/mgmt/project/apply", headers=headers, json={"files": files})
    assert r.status_code == 200, r.text
    return r.json()


def _aborted(out: list[dict[str, object]]) -> bool:
    return bool(out) and out[-1].get("action") == "aborted"


@needs_db
def test_apply_lands_a_lens_on_a_typed_decision_provider_alone(
    org, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """The product fact: with a typed-decision provider as the only provider
    — no chat tier, ``untyped_fallback`` off on every lens — ``dst apply``
    rejects every lens. Typed serving writes no text and needs no chat model;
    an install that serves typed only must apply.

    Failure mode to carry: a publish gate that requires a chat tier a
    typed-only lens never uses."""
    _oid, headers, _world = org
    monkeypatch.setattr(
        settings, "providers", {"jev": ProviderConfig(type="typesafe", api_key="k")}
    )
    files = _lens_files(
        "group_finance",
        ["group_revenue_monthly", "bergstrom_invoices"],
        eval_gate="block",
        certified=False,
    )
    files["lenses/group_finance/evals/cases.yaml"] = yaml.safe_dump(
        [
            {
                "question": "what is the full-year revenue forecast",
                "expect": "refuse",
                "status": "approved",
            }
        ]
    )
    out = _apply(headers, files)
    row = next(e for e in out if e.get("lens") == "group_finance")
    assert row.get("errors", []) == [] and not _aborted(out), out


Q14 = "Bergström Rör revenue in SEK and EUR for August, and share of group revenue"


@needs_db
def test_q14_a_certified_share_is_served_not_clarified(
    org, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """Asked "Bergström Rör revenue in SEK and EUR for August, and share of group
    revenue" came back as a clarification — "the question asks for a share,
    and se_revenue_sek is not one; declare a ratio" — while the lens certifies
    "share of group revenue from Sweden in {period}" and "Bergström Rör revenue
    in SEK by month in {period}". The certified rail must serve the parts it
    certifies; a clarification past a certified answer is the defect, so a
    non-answer does not pass here.

    Failure mode to carry: a multi-part question clarified although certified
    answers cover its parts."""
    _oid, headers, world = org
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    # A chat tier is declared, as in the run, and never reached by a typed
    # serve: a scripted stand-in takes any call apply's self-test makes.
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _key, **_: ScriptedLLM(['{"sql": "SELECT 1 AS n"}']),
    )
    out = _apply(
        headers,
        _lens_files(
            "group_finance",
            ["group_revenue_monthly", "bergstrom_invoices"],
            eval_gate="off",
            certified=True,
        ),
    )
    row = next(e for e in out if e.get("lens") == "group_finance")
    assert row.get("errors", []) == [] and not _aborted(out), out
    monkeypatch.setattr("services.runtime.assembly.typed_decider", lambda: RecordedDecider([]))
    monkeypatch.setattr("services.runtime.pipeline.typed_decider", lambda: RecordedDecider([]))

    r = client.post(
        "/v1/lenses/group_finance/query",
        headers=headers,
        json={"q": Q14, "format": "structured"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    share = round(
        float(
            world.scalar(
                f"SELECT SUM(CASE WHEN entity = '{SE}' THEN revenue_eur ELSE 0 END) / "
                f"SUM(revenue_eur) "
                f"FROM {REVENUE} WHERE revenue_stream <> 'Intercompany' AND month = '2026-08'"
            )
        ),
        4,
    )
    assert body["status"] == "ok", f"{body['status']}: {body['answer']}"
    served = [
        round(float(v), 4)
        for row in body["data"]["rows"]
        for v in row
        if isinstance(v, int | float)
    ]
    assert share in served, f"the certified share {share} is not among {served}: {body['answer']}"
