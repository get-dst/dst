"""A column hidden from every caller except named groups.

`hide: [{column: contact_email, except_groups: [support, sales]}]` withholds the
column exactly like a plain `hide` from every caller outside those groups, on
every door: the allow-list guard, describe, the catalog, value dictionaries,
the generation prompt, the structured door, certified answers (listed, run,
matched, counted) and the request log. A caller inside a group reads it, and
the receipt names the exception. Groups come from the caller's verified
credential (set by an admin), never from the request.

The first half also pins why every hide on such a column used to fail apply:
`hide` matched `fields` only, while a bare dimension puts its column in scope
too — so a column modelled as a dimension could not be hidden at all.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.certify import store as certify_store
from services.config import settings
from services.contracts.fakes import ScriptedLLM, fake_llm_providers
from services.contracts.lens_config import AccessRule, LensConfig
from services.contracts.profile import ColumnProfile, TableProfile
from services.contracts.protocols import LLMResult
from services.contracts.response import Receipt
from services.contracts.semantic_model import Dimension, HideRule, SemanticModel
from services.contracts.shared_semantic import SharedEntity, SharedRelationship
from services.db.session import org_session
from services.lenses import connection_store, store
from services.lenses.demo import jaffle_customer_value_config, jaffle_shared_assets
from services.lenses.repo import render_lens_repo
from services.lenses.store import load_published_lens
from services.project import caller_groups
from services.project.apply import _certified_entry_errors
from services.project.compile import CompileError, compile_lens_model
from services.project.schema import parse_project_yaml
from services.runtime import receipt as receipt_mod
from services.runtime import sql_guard, value_guard
from services.runtime.intent_generator import serialize_layer
from services.semantic.files import parse_semantic_files, render_semantic_files

client = TestClient(app)

# ── the reported rejection, reproduced on an invented entity ─────────────────

ACCOUNTS: dict[str, Any] = {
    "name": "accounts",
    "source": {"connection": "wh", "table": "crm.accounts"},
    "primary_key": ["account_id"],
    "fields": [
        {"name": "account_id", "type": "string"},
        {"name": "region", "type": "string"},
    ],
    "dimensions": [{"name": "region"}, {"name": "contact_email"}],
    "metrics": [{"name": "account_count", "agg": "count_distinct", "expr": "accounts.account_id"}],
}


def _compile(
    entity: dict[str, Any], groups: tuple[str, ...] = ()
) -> tuple[SemanticModel, list[str]]:
    config = LensConfig.model_validate(
        {
            "name": "cs",
            "display_name": "cs",
            "connections": ["wh"],
            "select": {"entities": ["accounts"], "definitions": []},
        }
    )
    return compile_lens_model(
        config=config,
        shared_entities={"accounts": SharedEntity.model_validate(entity)},
        shared_definitions={},
        shared_relationships={},
        local_definitions=[],
        use_when=[],
        sample_queries=[],
        dialect="duckdb",
        groups=groups,
    )


def test_a_column_modelled_as_a_dimension_can_be_hidden() -> None:
    """The reported shape: the contact column is a bare dimension (in scope,
    flagged as personal data), and `hide` on it failed apply with "no field
    matches it". Declared as a field too, it failed with "its dimension still
    reads it". The dimension IS the column; both shapes now hide it."""
    model, warnings = _compile(ACCOUNTS)
    assert "contact_email" in model.allowed_columns()["crm.accounts"]
    assert any("serves contact_email" in w for w in warnings)  # a bare dimension is served

    for entity in (
        {**ACCOUNTS, "hide": ["contact_email"]},
        {**ACCOUNTS, "hide": ["*_email"]},
        {
            **ACCOUNTS,
            "hide": ["accounts.contact_email"],
            "fields": [*ACCOUNTS["fields"], {"name": "contact_email", "type": "string"}],
        },
    ):
        model, warnings = _compile(entity)
        accounts = model.entities[0]
        assert model.allowed_columns()["crm.accounts"] == {"account_id", "region"}
        assert [d.name for d in accounts.dimensions] == ["region"]
        assert "contact_email" not in {f.name for f in accounts.fields}
        assert not any("sensitive_columns" in w for w in warnings)


def test_a_dimension_computing_from_a_hidden_column_still_fails() -> None:
    entity = {
        **ACCOUNTS,
        "dimensions": [{"name": "email_domain", "expr": "split_part(contact_email, '@', 2)"}],
        "fields": [*ACCOUNTS["fields"], {"name": "contact_email", "type": "string"}],
        "hide": ["contact_email"],
    }
    with pytest.raises(CompileError) as exc:
        _compile(entity)
    assert "its dimension email_domain still reads it" in str(exc.value)


# ── group entries: compile ────────────────────────────────────────────────────

GROUPED = {**ACCOUNTS, "hide": [{"column": "contact_email", "except_groups": ["support", "sales"]}]}


def test_group_names_are_checked_against_the_declared_groups() -> None:
    with pytest.raises(CompileError) as exc:
        _compile(GROUPED, groups=("support",))
    msg = str(exc.value)
    assert "except for group(s) ['sales']" in msg and "it declares ['support']" in msg
    with pytest.raises(CompileError) as exc:
        _compile(GROUPED)
    assert "it declares none" in str(exc.value)
    _compile(GROUPED, groups=("sales", "support"))


def test_a_group_entry_needs_a_group_and_a_match() -> None:
    with pytest.raises(ValueError, match="except_groups needs at least one group"):
        SharedEntity.model_validate({**ACCOUNTS, "hide": [{"column": "x", "except_groups": []}]})
    with pytest.raises(CompileError) as exc:
        _compile(
            {**ACCOUNTS, "hide": [{"column": "phone", "except_groups": ["support"]}]},
            groups=("support",),
        )
    assert "hides 'phone' but no field or dimension matches it" in str(exc.value)


def test_the_stored_model_withholds_and_a_group_caller_reads() -> None:
    model, _ = _compile(GROUPED, groups=("support", "sales"))
    # stored (what every door reads without a caller): withheld
    assert model.allowed_columns()["crm.accounts"] == {"account_id", "region"}
    assert [d.name for d in model.entities[0].dimensions] == ["region"]
    [gc] = model.group_columns
    assert (gc.entity, gc.column, gc.groups) == ("accounts", "contact_email", ["sales", "support"])

    for groups in ([], ["marketing"], ["Support"]):  # names match exactly
        view = model.for_groups(groups)
        assert "contact_email" not in view.allowed_columns()["crm.accounts"]
        assert "contact_email" not in serialize_layer(view)
        assert view.column_grants == [] and view.group_columns == []

    view = model.for_groups(["support"])
    assert "contact_email" in view.allowed_columns()["crm.accounts"]
    assert "contact_email" in {d.name for d in view.entities[0].dimensions}
    assert "contact_email" in serialize_layer(view)
    assert view.column_grants == ["accounts.contact_email (group support)"]
    assert view.group_columns == []  # a view carries nothing it may not read
    # the stored model is untouched by a view
    assert "contact_email" not in model.allowed_columns()["crm.accounts"]


def test_deny_wins_when_another_entry_still_hides_the_column() -> None:
    entity = {
        **ACCOUNTS,
        "hide": [
            {"column": "contact_email", "except_groups": ["support"]},
            {"column": "*_email", "except_groups": ["sales"]},
        ],
    }
    model, _ = _compile(entity, groups=("support", "sales"))
    assert "contact_email" not in model.for_groups(["support"]).allowed_columns()["crm.accounts"]
    assert (
        "contact_email" in model.for_groups(["support", "sales"]).allowed_columns()["crm.accounts"]
    )
    # a plain pattern over the same column: nobody reads it, and it is no group column
    entity["hide"] = [*entity["hide"], "contact_email"]
    model, _ = _compile(entity, groups=("support", "sales"))
    assert model.group_columns == []
    view = model.for_groups(["support", "sales"])
    assert "contact_email" not in view.allowed_columns()["crm.accounts"]


def test_a_view_strips_a_model_stored_without_the_compile() -> None:
    """A bundle posted straight to the management API skips the compile: the
    view still withholds what the remaining entries name."""
    model = SemanticModel.model_validate(
        {
            "lens": "cs",
            "dialect": "duckdb",
            "entities": [
                {
                    **ACCOUNTS,
                    "fields": [*ACCOUNTS["fields"], {"name": "contact_email", "type": "string"}],
                    "hide": [{"column": "contact_email", "except_groups": ["support"]}],
                }
            ],
        }
    )
    view = model.for_groups([])
    assert "contact_email" not in {f.name for f in view.entities[0].fields}
    assert "contact_email" not in {d.name for d in view.entities[0].dimensions}


def test_dictionaries_follow_the_view() -> None:
    entity = {
        **GROUPED,
        "fields": [*ACCOUNTS["fields"], {"name": "contact_email", "type": "string"}],
    }
    model, _ = _compile(entity, groups=("support", "sales"))
    profile = TableProfile(
        connection="wh",
        table="crm.accounts",
        columns=[
            ColumnProfile(
                name="contact_email",
                type="VARCHAR",
                is_low_cardinality=True,
                top_values=["a@example.com", "b@example.com"],
                distinct_count=2,
                distinct_is_exact=True,
                values_complete=True,
            ),
            ColumnProfile(
                name="region",
                type="VARCHAR",
                is_low_cardinality=True,
                top_values=["north", "south"],
                distinct_count=2,
                distinct_is_exact=True,
                values_complete=True,
            ),
        ],
    )
    plain = value_guard.entity_domains(model.for_groups([]), [profile])
    assert "contact_email" not in plain.get("accounts", {})
    granted = value_guard.entity_domains(model.for_groups(["sales"]), [profile])
    assert "contact_email" in granted.get("accounts", {})


# ── pre-approved SQL: the guard trusts its tables, never past `hide` ──────────


def test_trusted_sql_reading_a_withheld_column_is_refused() -> None:
    model, _ = _compile(GROUPED, groups=("support", "sales"))
    plain, support = model.for_groups([]), model.for_groups(["support"])
    for sql in (
        "SELECT contact_email FROM crm.accounts",
        "SELECT a.contact_email FROM crm.accounts a",
        "WITH x AS (SELECT * FROM crm.accounts) SELECT x.contact_email FROM x",
        "SELECT count(*) FROM crm.accounts WHERE contact_email LIKE '%@example.com'",
    ):
        refused = sql_guard.check(sql, plain, trust_tables=True)
        assert not refused.ok and "accounts.contact_email" in (refused.reason or ""), sql
        assert sql_guard.withheld_reads(sql, plain) == ["accounts.contact_email"], sql
        assert sql_guard.check(sql, support, trust_tables=True).ok, sql
        assert sql_guard.withheld_reads(sql, support) == [], sql
        assert sql_guard.granted_reads(sql, support) == ["accounts.contact_email (group support)"]
    # a template that does not parse is read word by word, never skipped
    template = "SELECT contact_email FROM crm.accounts WHERE region = {region} {{"
    assert sql_guard.withheld_reads(template, plain) == ["accounts.contact_email"]
    assert sql_guard.withheld_reads("SELECT region FROM crm.accounts", plain) == []


def test_a_filter_through_a_join_never_reaches_a_withheld_column() -> None:
    """Filters resolve through declared joins: a value on the joined entity's
    group-hidden column has no dictionary on the caller's view, and the guard
    refuses the column through the join on generated and trusted SQL alike."""
    config = LensConfig.model_validate(
        {
            "name": "cs",
            "display_name": "cs",
            "connections": ["wh"],
            "select": {"entities": ["accounts", "tickets"], "definitions": []},
        }
    )
    tickets = SharedEntity.model_validate(
        {
            "name": "tickets",
            "source": {"connection": "wh", "table": "crm.tickets"},
            "primary_key": ["ticket_id"],
            "fields": [
                {"name": "ticket_id", "type": "string"},
                {"name": "account_id", "type": "string"},
            ],
            "metrics": [{"name": "ticket_count", "agg": "count"}],
        }
    )
    model, _ = compile_lens_model(
        config=config,
        shared_entities={"accounts": SharedEntity.model_validate(GROUPED), "tickets": tickets},
        shared_definitions={},
        shared_relationships={
            "tickets__accounts": SharedRelationship(
                left="tickets",
                right="accounts",
                on="tickets.account_id = accounts.account_id",
                relationship="many_to_one",
            )
        },
        local_definitions=[],
        use_when=[],
        sample_queries=[],
        dialect="duckdb",
        groups=("support", "sales"),
    )
    sql = (
        "SELECT count(*) FROM crm.tickets t JOIN crm.accounts a ON t.account_id = a.account_id "
        "WHERE a.contact_email = 'a@example.com'"
    )
    plain = model.for_groups([])
    for trusted in (False, True):
        assert not sql_guard.check(sql, plain, trust_tables=trusted).ok, trusted
        assert sql_guard.check(sql, model.for_groups(["sales"]), trust_tables=trusted).ok
    accounts = next(e for e in plain.entities if e.name == "accounts")
    assert "contact_email" not in {f.name for f in accounts.fields}


def test_certification_accepts_a_group_column_and_refuses_a_hidden_one() -> None:
    entity = {
        **GROUPED,
        "fields": [*ACCOUNTS["fields"], {"name": "phone", "type": "string"}],
        "hide": [*GROUPED["hide"], "phone"],
    }
    model, _ = _compile(entity, groups=("support", "sales"))
    ok = {
        "question": "contact emails per region",
        "sql": "SELECT region, contact_email FROM crm.accounts",
    }
    assert _certified_entry_errors(ok, model) == []
    hidden = {"question": "phones", "sql": "SELECT phone FROM crm.accounts"}
    [error] = _certified_entry_errors(hidden, model)
    assert "accounts.phone" in error and "hidden by the lens" in error


# ── receipts, groups declaration ──────────────────────────────────────────────


def test_a_receipt_without_column_access_signs_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(receipt_mod, "signing_keys", lambda: ["k"])
    args = {
        "request_id": "r",
        "lens": "l",
        "certification": "none",
        "cert_id": None,
        "confidence": None,
        "sql": "SELECT 1",
        "data_as_of": None,
    }
    before = receipt_mod.build(**args)  # type: ignore[arg-type]
    assert "column_access" not in receipt_mod._canonical(before).decode()
    granted = receipt_mod.build(**args, column_access=["a.b (group g)"])  # type: ignore[arg-type]
    assert receipt_mod.check_signature(granted) == "valid"
    forged = Receipt(**{**granted.model_dump(), "column_access": None})
    assert receipt_mod.check_signature(forged) == "invalid"


def test_a_group_entry_round_trips_through_the_entity_file() -> None:
    entity = SharedEntity.model_validate(GROUPED)
    files = render_semantic_files([entity], [], [])
    [(path, body)] = files.items()
    assert "except_groups" in body
    parsed, _defs, _rels = parse_semantic_files({path: body})
    assert parsed["accounts"].hide == entity.hide
    # the authoring seam names a typo'd key inside the entry
    typo = body.replace("except_groups", "except_group")
    with pytest.raises(ValueError, match="except_group"):
        parse_semantic_files({path: typo})


def test_dst_yaml_declares_groups() -> None:
    assert parse_project_yaml("groups: [support, sales]\n").groups == ["support", "sales"]
    assert parse_project_yaml("groups: support\n").groups == ["support"]
    assert parse_project_yaml("name: x\n").groups == []


# ── every door, over the API ──────────────────────────────────────────────────


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

LENS = "customer_value"
NAME_SQL = "SELECT customer_name FROM customers ORDER BY customer_name LIMIT 3"


def _bundle() -> store.LensBundle:
    """The demo lens, with `customer_name` standing in for a contact column:
    a dimension, hidden except for the support and sales groups."""
    entities, definitions, relationships = jaffle_shared_assets()
    by_name = {e.name: e for e in entities}
    customers = by_name["customers"]
    customers.dimensions = [*customers.dimensions, Dimension(name="customer_name")]
    customers.hide = [HideRule(column="customer_name", except_groups=["support", "sales"])]
    config = jaffle_customer_value_config()
    config.access.allow = [AccessRule(group="everyone")]
    config.logging.log_samples = True
    model, _ = compile_lens_model(
        config=config,
        shared_entities=by_name,
        shared_definitions={d.term: d for d in definitions},
        shared_relationships={r.name: r for r in relationships},
        local_definitions=[],
        use_when=[],
        sample_queries=[],
        dialect="duckdb",
        groups=["support", "sales"],
    )
    return store.LensBundle(config=config, semantic_model=model)


class _RecordingLLM(ScriptedLLM):
    """Keeps every prompt it was sent."""

    def __init__(self, responses: list[str]) -> None:
        super().__init__(responses)
        self.prompts: list[str] = []

    def complete(self, **kwargs: Any) -> LLMResult:
        system = kwargs.get("system") or []
        messages = kwargs.get("messages") or []
        self.prompts.append(
            "\n".join(getattr(b, "text", str(b)) for b in system)
            + "\n"
            + "\n".join(str(getattr(m, "content", m)) for m in messages)
        )
        return super().complete(**kwargs)


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch) -> Any:
    llm = _RecordingLLM(["There are some customers."])
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr("services.llm.anthropic_provider.AnthropicProvider", lambda _k, **_: llm)
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(
            text("INSERT INTO org (name) VALUES ('ColGroups') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    ah = {"Authorization": f"Bearer {raw}"}
    with org_session(org) as session:
        store.create_lens(session, _bundle())
        store.publish(session, LENS)
        cert = certify_store.create(
            session, LENS, "who are our customers?", NAME_SQL, None, "maija"
        )
        certify_store.create(
            session,
            LENS,
            "how many customers?",
            "SELECT count(*) AS n FROM customers",
            None,
            "maija",
        )
    keys: dict[str, str] = {}
    for name, groups in (("plain", []), ("support", ["support"]), ("marketing", ["marketing"])):
        r = client.post("/mgmt/callers", json={"name": name, "groups": groups}, headers=ah)
        assert r.status_code == 201, r.text
        keys[name] = client.post(f"/mgmt/callers/{name}/keys", headers=ah).json()["key"]
    try:
        yield {"org": org, "admin": raw, "keys": keys, "cert": cert, "llm": llm}
    finally:
        with admin.begin() as c:
            for table in ("request_log", "audit_log", "certified_answer", "api_key", "caller"):
                c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org})  # noqa: S608
            c.execute(text("DELETE FROM lens_version WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _h(key: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", **extra}


# The headers a caller could try to claim a group with. None is read.
SPOOF = {"X-dst-Groups": "support", "X-Dst-Group": "support", "X-dst-Agent": "support"}


def _unprivileged(world: Any) -> list[tuple[str, dict[str, str]]]:
    k = world["keys"]
    return [
        ("no groups", _h(k["plain"])),
        ("another group", _h(k["marketing"])),
        ("spoofed headers", _h(k["plain"], **SPOOF)),
        ("admin token", _h(world["admin"])),
    ]


@needs_db
def test_describe_and_catalog(world: Any) -> None:
    for label, headers in _unprivileged(world):
        for path in (f"/v1/lenses/{LENS}", f"/v1/lenses/{LENS}/catalog"):
            r = client.get(path, headers=headers)
            assert r.status_code == 200, (label, r.text)
            assert "customer_name" not in r.text, (label, path)
    r = client.get(f"/v1/lenses/{LENS}", headers=_h(world["keys"]["support"]))
    assert "customer_name" in r.text
    r = client.get(f"/v1/lenses/{LENS}/catalog", headers=_h(world["keys"]["support"]))
    assert "customer_name" in {d["name"] for d in r.json()["dimensions"]}


@needs_db
def test_sql_door(world: Any) -> None:
    for label, headers in _unprivileged(world):
        r = client.post("/v1/sql", json={"lens": LENS, "sql": NAME_SQL}, headers=headers)
        assert r.status_code == 400 and "out of lens scope" in r.text, (label, r.text)
    r = client.post(
        "/v1/sql", json={"lens": LENS, "sql": NAME_SQL}, headers=_h(world["keys"]["support"])
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["rows"]) == 3


@needs_db
def test_structured_door_receipt_and_request_log(world: Any) -> None:
    body = {"entity": "customers", "fields": ["customer_name"], "limit": 3, "format": "structured"}
    for label, headers in _unprivileged(world):
        r = client.post(f"/v1/lenses/{LENS}/metrics", json=body, headers=headers)
        assert r.status_code in (200, 400, 422), (label, r.text)
        if r.status_code == 200:
            assert r.json()["status"] != "ok" and not r.json().get("data"), (label, r.text)
    r = client.post(f"/v1/lenses/{LENS}/metrics", json=body, headers=_h(world["keys"]["support"]))
    assert r.status_code == 200, r.text
    answer = r.json()
    assert answer["status"] == "ok" and answer["data"]["rows"], answer
    assert answer["receipt"]["column_access"] == ["customers.customer_name (group support)"]
    verdict = client.post(
        "/v1/verify-receipt", json=answer["receipt"], headers=_h(world["keys"]["support"])
    ).json()
    assert verdict["trace_found"] and verdict["mismatches"] == []
    # the log outlives the request and is read outside the group: no values on it
    with create_engine(settings.database_admin_url).connect() as c:
        sample, logged = c.execute(
            text("SELECT sample, answer FROM request_log WHERE request_id = :r"),
            {"r": answer["request_id"]},
        ).one()
    assert sample is None
    assert logged.startswith("[withheld: the answer read customers.customer_name")
    # certifying that request keeps the SQL, never the marker as the answer's prose
    r = client.post(
        f"/mgmt/lenses/{LENS}/certified/from-request/{answer['request_id']}",
        json={"allow_untyped": True},
        headers=_h(world["admin"]),
    )
    assert r.status_code == 201, r.text
    with org_session(world["org"]) as session:
        stored = certify_store.get(session, r.json()["id"])
    assert stored is not None and stored.verified_prose is None


@needs_db
def test_certified_library_run_and_count(world: Any) -> None:
    cert = world["cert"]
    counts = {}
    for label, headers in _unprivileged(world):
        r = client.get(f"/v1/lenses/{LENS}/certified", headers=headers)
        assert r.status_code == 200, r.text
        assert cert not in {e["cert_id"] for e in r.json()["certified"]}, label
        assert "customer_name" not in r.text, label
        r = client.post(f"/v1/lenses/{LENS}/certified/{cert}/run", headers=headers)
        assert r.status_code == 404, (label, r.text)
        d = client.get(f"/v1/lenses/{LENS}", headers=headers).json()
        counts[label] = d["certified_count"]
        assert "who are our customers?" not in d["certified_examples"], label
    assert set(counts.values()) == {1}
    support = _h(world["keys"]["support"])
    r = client.get(f"/v1/lenses/{LENS}/certified", headers=support)
    assert cert in {e["cert_id"] for e in r.json()["certified"]}
    assert client.get(f"/v1/lenses/{LENS}", headers=support).json()["certified_count"] == 2
    r = client.post(
        f"/v1/lenses/{LENS}/certified/{cert}/run", json={"format": "structured"}, headers=support
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["rows"]
    assert r.json()["receipt"]["column_access"] == ["customers.customer_name (group support)"]


@needs_db
def test_generation_prompts_never_carry_the_column(world: Any) -> None:
    llm = world["llm"]
    for label, headers in _unprivileged(world):
        llm.prompts.clear()
        client.post(
            f"/v1/lenses/{LENS}/query", json={"question": "who are our customers?"}, headers=headers
        )
        assert llm.prompts, label
        assert not [p for p in llm.prompts if "customer_name" in p], label
    llm.prompts.clear()
    client.post(
        f"/v1/lenses/{LENS}/query",
        json={"question": "who are our customers?"},
        headers=_h(world["keys"]["support"]),
    )
    assert any("customer_name" in p for p in llm.prompts)


@needs_db
def test_groups_are_set_by_an_admin_only(world: Any) -> None:
    plain = world["keys"]["plain"]
    r = client.put("/mgmt/callers/plain/groups", json={"groups": ["support"]}, headers=_h(plain))
    assert r.status_code in (401, 403), r.text
    r = client.post("/mgmt/callers", json={"name": "x", "groups": ["support"]}, headers=_h(plain))
    assert r.status_code in (401, 403), r.text
    # an admin grants it, and the same key reads the column from then on
    r = client.put(
        "/mgmt/callers/plain/groups", json={"groups": ["sales"]}, headers=_h(world["admin"])
    )
    assert r.status_code == 200 and r.json()["groups"] == ["sales"], r.text
    r = client.post("/v1/sql", json={"lens": LENS, "sql": NAME_SQL}, headers=_h(plain))
    assert r.status_code == 200, r.text
    assert (
        client.put(
            "/mgmt/callers/nobody/groups", json={"groups": []}, headers=_h(world["admin"])
        ).status_code
        == 404
    )


@needs_db
def test_declared_groups_ride_the_setting(world: Any) -> None:
    with org_session(world["org"]) as session:
        assert caller_groups.load(session) == []
        assert caller_groups.apply(session, ["support", "sales"]) == ["groups: sales, support"]
        assert caller_groups.load(session) == ["sales", "support"]
        assert caller_groups.apply(session, ["sales", "support"]) == []
        session.execute(text("DELETE FROM setting WHERE key = 'groups'"))


# ── plan and apply: the group names are dst.yaml's ────────────────────────────


@pytest.fixture()
def project(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr("services.llm.registry.resolve", lambda _ref: None)
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('ColGroupsApply') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": oid, "h": hash_token(raw)},
        )
    with org_session(oid) as s:
        connection_store.create_connection(
            s, "jaffle", "duckdb", {"path": settings.duckdb_jaffle_path}, None
        )
    try:
        yield oid, {"Authorization": f"Bearer {raw}"}
    finally:
        with admin.begin() as c:
            for table in (
                "eval_run",
                "eval_case",
                "certified_answer",
                "lens_version",
                "lens",
                "semantic_asset",
                "setting",
                "connection",
                "admin_token",
            ):
                c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": oid})  # noqa: S608
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})


def _files(groups: list[str] | None) -> dict[str, str]:
    entities, definitions, relationships = jaffle_shared_assets()
    customers = next(e for e in entities if e.name == "customers")
    customers.dimensions = [*customers.dimensions, Dimension(name="customer_name")]
    customers.hide = [HideRule(column="customer_name", except_groups=["support"])]
    files = dict(render_semantic_files(entities, definitions, relationships))
    bundle = store.LensBundle(
        config=jaffle_customer_value_config(),
        semantic_model=SemanticModel(lens=LENS, dialect="duckdb"),
    )
    for path, content in render_lens_repo(bundle).items():
        files[f"lenses/{LENS}/{path}"] = content
    if groups is not None:
        files["dst.yaml"] = f"groups: [{', '.join(groups)}]\n"
    return files


@needs_db
def test_plan_and_apply_check_the_names_against_dst_yaml(project: Any) -> None:
    oid, headers = project
    for groups in (None, ["sales"]):
        files = _files(groups)
        plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
        row = next(e for e in plan if e.get("lens") == LENS)
        assert row["status"] == "invalid", row
        assert any("except for group(s) ['support']" in e for e in row["errors"]), row
        out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
        assert out[-1]["action"] == "aborted", out

    files = _files(["support", "sales"])
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    row = next(e for e in plan if e.get("lens") == LENS)
    assert row["status"] != "invalid", row
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert out[-1].get("action") != "aborted", out
    assert any(r.get("scope") == "groups" for r in out), out
    bundle = load_published_lens(oid, LENS)
    assert bundle is not None
    model = bundle.semantic_model
    assert "customer_name" not in model.allowed_columns()["customers"]
    assert "customer_name" in model.for_groups(["support"]).allowed_columns()["customers"]
    # a later push without dst.yaml compiles against the stored names
    files.pop("dst.yaml")
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    assert next(e for e in plan if e.get("lens") == LENS)["status"] != "invalid"
