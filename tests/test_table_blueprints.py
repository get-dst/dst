"""Table blueprints (semantic/tables/): facts about a table, authored as files,
checked by dst, and read on every answer from an exposed table.

The invented world: a CRM copy synced into the warehouse, one row per contact
per sync, with a delete flag that holds on the latest sync only. Counting its
rows counts every contact once per sync, and nothing about the number says so.

Pinned: the file parses strictly and round-trips; an entity on the table
inherits grain and key; a new blueprint marks the lens stale; every plan check
fires on a false declaration and stays quiet on a true one; the coverage line;
the grain reading and its effect on the grade; a live count bounded by the
connection's byte budget; describe shows the blueprint, the verdicts and the
probe's facts and never a hidden column; both prompts carry the versions line;
and end to end, a project with no blueprints plans as before plus warnings,
a false key aborts apply, and the `sql` tool and `query` refuse to call the
inflated count verified.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.connectors.duckdb import DuckDBConnector
from services.contracts.fakes import EchoQueryGenerator
from services.contracts.lens_config import LensConfig
from services.contracts.profile import ColumnProfile, TableProfile
from services.contracts.semantic_model import (
    EntitySource,
    Field,
    SemanticModel,
    TableBlueprint,
)
from services.contracts.shared_semantic import SelectEntity, SelectSpec, SharedEntity
from services.contracts.verification import VerificationCheck, VerificationReport
from services.contracts.warehouse import DryRunResult, QueryResult
from services.lenses.describe import describe_model
from services.project import blueprints as bp
from services.project.compile import compile_lens_model, shared_blueprint_hash
from services.project.plan import plan_semantic, stale_asset_keys
from services.project.probe import ProbeArtifact
from services.runtime import grain
from services.runtime.generator import serialize_model
from services.runtime.intent_generator import serialize_layer
from services.runtime.pipeline import run_query
from services.semantic.files import (
    parse_blueprint_files,
    parse_semantic_file,
    render_blueprint,
    render_semantic_files,
)

client = TestClient(app)

CRM = "crm"
NOW = datetime(2026, 10, 20, tzinfo=UTC)

CONTACTS_YAML = """\
connection: crm
table: crm.contacts
grain: one row per contact per sync
key: [contact_id, _synced_at]
versions:
  sync_column: _synced_at
  latest_only: [_deleted]
clocks:
  created_at: {meaning: when the contact was created, timezone: UTC}
joins:
  - to: crm.accounts
    "on": contacts.account_id = accounts.account_id
    cardinality: one
caveats:
  - a contact deleted in the CRM keeps its earlier rows
"""


def _contacts(**overrides: object) -> TableBlueprint:
    data = yaml.safe_load(CONTACTS_YAML)
    data.update(overrides)
    return TableBlueprint.model_validate(data)


# ── the warehouse: four contacts, three syncs; contact 4 deleted on the last ──


def _crm_db(path: Path, *, duplicate: bool = False) -> str:
    """``duplicate``: a later load wrote contact 1 twice into the last sync, so
    the declared grain (one row per contact per sync) no longer holds."""
    path.mkdir(parents=True, exist_ok=True)
    db = str(path / "warehouse.duckdb")
    con = duckdb.connect(db)
    con.execute("CREATE SCHEMA crm")
    con.execute(
        "CREATE TABLE crm.contacts (contact_id INTEGER, account_id INTEGER, email VARCHAR, "
        "created_at TIMESTAMP, _synced_at TIMESTAMP, _deleted BOOLEAN)"
    )
    for sync in range(3):
        stamp = f"2026-10-0{sync + 1} 03:00:00"
        for contact in range(1, 5):
            deleted = sync == 2 and contact == 4
            con.execute(
                "INSERT INTO crm.contacts VALUES (?, ?, ?, '2026-09-01', ?, ?)",
                [contact, 1 + contact % 2, f"c{contact}@example.com", stamp, deleted],
            )
    if duplicate:
        con.execute(
            "INSERT INTO crm.contacts VALUES "
            "(1, 2, 'c1@example.com', '2026-09-01', '2026-10-03 03:00:00', false)"
        )
    con.execute("CREATE TABLE crm.accounts (account_id INTEGER, name VARCHAR)")
    con.execute("INSERT INTO crm.accounts VALUES (1, 'Northwind'), (2, 'Fabrikam')")
    con.execute("CREATE TABLE crm.notes (note_id INTEGER, contact_id INTEGER, body VARCHAR)")
    con.execute("INSERT INTO crm.notes VALUES (1, 1, 'call back'), (2, 2, 'sent quote')")
    con.close()
    return db


def _col(name: str, type_: str, **kw: object) -> ColumnProfile:
    return ColumnProfile(name=name, type=type_, **kw)  # type: ignore[arg-type]


def _crm_profiles(at: datetime = NOW) -> list[TableProfile]:
    """The probe of the CRM copy: counts taken over every row."""
    return [
        TableProfile(
            connection=CRM,
            table="crm.contacts",
            row_count=12,
            profiled_at=at,
            source="sampled",
            columns=[
                _col("contact_id", "INTEGER", distinct_count=4, distinct_is_exact=True),
                _col("account_id", "INTEGER", distinct_count=2, distinct_is_exact=True),
                _col("email", "VARCHAR", distinct_count=4, distinct_is_exact=True),
                _col("created_at", "TIMESTAMP"),
                _col("_synced_at", "TIMESTAMP", distinct_count=3, distinct_is_exact=True),
                _col("_deleted", "BOOLEAN", distinct_count=2, distinct_is_exact=True),
            ],
        ),
        TableProfile(
            connection=CRM,
            table="crm.accounts",
            row_count=2,
            profiled_at=at,
            source="sampled",
            columns=[
                _col(
                    "account_id",
                    "INTEGER",
                    distinct_count=2,
                    distinct_is_exact=True,
                    top_values=["1", "2"],
                ),
                _col("name", "VARCHAR", distinct_count=2, distinct_is_exact=True),
            ],
        ),
        TableProfile(
            connection=CRM,
            table="crm.notes",
            row_count=2,
            profiled_at=at,
            source="sampled",
            columns=[
                _col("note_id", "INTEGER", distinct_count=2, distinct_is_exact=True),
                _col("contact_id", "INTEGER", distinct_count=2, distinct_is_exact=True),
                _col("body", "VARCHAR"),
            ],
        ),
    ]


# ── the file ─────────────────────────────────────────────────────────────────


def test_a_blueprint_file_parses_and_round_trips() -> None:
    parsed = parse_semantic_file("semantic/tables/crm/contacts.yaml", CONTACTS_YAML)
    assert isinstance(parsed, TableBlueprint)
    assert parsed.name == "crm.contacts" and parsed.full
    assert parsed.record_key() == ["contact_id"]
    assert parsed.unique_on() == ["contact_id", "_synced_at"]
    assert parsed.joins[0].on == "contacts.account_id = accounts.account_id"
    again = parse_semantic_file("semantic/tables/x.yaml", render_blueprint(parsed))
    assert again == parsed
    # The canonical render is what plan compares: an authored file plans clean
    # against the stored asset rendered back.
    stored = render_semantic_files([], [], [], [parsed])
    assert list(stored) == ["semantic/tables/crm.contacts.yaml"]
    (row,) = plan_semantic(stored, {"semantic/tables/crm/contacts.yaml": CONTACTS_YAML})
    assert row.status == "unchanged", row.diff


def test_the_unique_on_key_adds_the_sync_column_when_it_is_left_out() -> None:
    blueprint = _contacts(key=["contact_id"])
    assert blueprint.unique_on() == ["contact_id", "_synced_at"]
    assert blueprint.record_key() == ["contact_id"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"grian": "typo"}, "grian"),
        ({"clocks": {"created_at": {"timezone": "Mars/Olympus"}}}, "not an IANA timezone"),
        ({"versions": {"sync_column": "_synced_at", "latest_only": ["_synced_at"]}}, "sync"),
        ({"key": ["contact_id", "contact_id"]}, "names a column twice"),
        ({"joins": [{"to": "crm.accounts", "on": "a = b", "cardinality": "lots"}]}, "cardinality"),
    ],
)
def test_a_false_or_misspelled_field_is_a_named_parse_error(
    change: dict[str, object], message: str
) -> None:
    data = yaml.safe_load(CONTACTS_YAML)
    data.update(change)
    with pytest.raises(ValueError) as exc:
        parse_semantic_file("semantic/tables/contacts.yaml", yaml.safe_dump(data))
    assert "semantic/tables/contacts.yaml" in str(exc.value)
    assert message in str(exc.value)


def test_two_files_for_one_table_are_a_competing_claim() -> None:
    files = {
        "semantic/tables/a.yaml": CONTACTS_YAML,
        "semantic/tables/b.yaml": CONTACTS_YAML.replace("table: crm.contacts", "table: contacts"),
    }
    with pytest.raises(ValueError) as exc:
        parse_blueprint_files(files)
    assert "semantic/tables/a.yaml" in str(exc.value) and "semantic/tables/b.yaml" in str(exc.value)


# ── compile: an entity inherits the table's facts ───────────────────────────


def _entity(name: str = "contacts", **kw: object) -> SharedEntity:
    return SharedEntity(
        name=name,
        source=EntitySource(connection=CRM, table="crm.contacts"),
        fields=[
            Field(name="contact_id", type="integer"),
            Field(name="account_id", type="integer"),
            Field(name="_synced_at", type="timestamp"),
            Field(name="_deleted", type="boolean"),
        ],
        **kw,  # type: ignore[arg-type]
    )


def _compile(
    entities: dict[str, SharedEntity],
    blueprints: dict[str, TableBlueprint],
    *,
    expose: list[dict[str, object]] | None = None,
    select: list[str] | None = None,
    hashes: dict[str, str] | None = None,
) -> SemanticModel:
    config = LensConfig(
        name="crm_lens",
        connections=[CRM],
        select=SelectSpec(entities=[SelectEntity(name=n) for n in (select or list(entities))]),
        expose=expose or [],  # type: ignore[arg-type]
        untyped_fallback=True,
    )
    model, _warnings = compile_lens_model(
        config=config,
        shared_entities=entities,
        shared_definitions={},
        shared_relationships={},
        local_definitions=[],
        use_when=[],
        sample_queries=[],
        dialect="duckdb",
        profiles={CRM: _crm_profiles()},
        blueprints=blueprints,
        asset_hashes=hashes,
    )
    return model


def test_an_entity_inherits_grain_and_key_from_its_tables_blueprint() -> None:
    blueprint = _contacts()
    model = _compile({"contacts": _entity()}, {blueprint.name: blueprint})
    (contacts,) = model.entities
    assert contacts.grain == "one row per contact per sync"
    assert contacts.primary_key == ["contact_id", "_synced_at"]
    assert model.blueprints == {"crm.contacts": blueprint}
    assert model.shared_provenance is not None
    assert model.shared_provenance.assets["blueprint/crm.contacts"] == shared_blueprint_hash(
        blueprint
    )


def test_an_entitys_own_grain_and_key_win_over_the_blueprint() -> None:
    blueprint = _contacts()
    entity = _entity(grain="one row per contact, latest sync", primary_key=["contact_id"])
    (contacts,) = _compile({"contacts": entity}, {blueprint.name: blueprint}).entities
    assert contacts.grain == "one row per contact, latest sync"
    assert contacts.primary_key == ["contact_id"]


def test_a_key_on_a_hidden_column_is_not_inherited() -> None:
    blueprint = _contacts(key=["email"], versions=None)
    entity = _entity(hide=["account_id"])
    entity.fields.append(Field(name="email", type="string"))
    entity.hide = ["email"]
    (contacts,) = _compile({"contacts": entity}, {blueprint.name: blueprint}).entities
    assert contacts.primary_key == []  # a hidden column never comes back as a key
    assert contacts.grain == "one row per contact per sync"


def test_a_blueprint_on_another_connection_is_consumed_but_never_attached() -> None:
    blueprint = _contacts(connection="elsewhere")
    model = _compile({"contacts": _entity()}, {blueprint.name: blueprint})
    assert model.blueprints == {} and model.entities[0].grain is None
    assert model.shared_provenance is not None
    assert "blueprint/crm.contacts" in model.shared_provenance.assets


def test_an_exposed_table_carries_its_blueprint() -> None:
    blueprint = _contacts()
    model = _compile(
        {}, {blueprint.name: blueprint}, expose=[{"connection": CRM, "tables": ["crm.contacts"]}]
    )
    (contacts,) = model.entities
    assert contacts.exposed and contacts.grain == "one row per contact per sync"
    assert model.blueprints["crm.contacts"] == blueprint


def test_a_new_blueprint_on_a_table_the_lens_reads_makes_it_stale() -> None:
    model = _compile({"contacts": _entity()}, {})
    assert model.shared_provenance is not None
    assets = model.shared_provenance.assets
    blueprint = _contacts()
    effective = {**assets, "blueprint/crm.contacts": shared_blueprint_hash(blueprint)}
    assert stale_asset_keys(assets, effective, {}, ["crm.contacts"]) == ["blueprint/crm.contacts"]
    # A blueprint on a table the lens does not read leaves it alone.
    other = {**assets, "blueprint/crm.notes": "h"}
    assert stale_asset_keys(assets, other, {}, ["crm.contacts"]) == []
    # Once compiled with it, the lens is current; an edit makes it stale again.
    current = _compile({"contacts": _entity()}, {blueprint.name: blueprint})
    assert current.shared_provenance is not None
    held = current.shared_provenance.assets
    assert stale_asset_keys(held, effective, {}, ["crm.contacts"]) == []
    edited = {**effective, "blueprint/crm.contacts": "moved"}
    assert stale_asset_keys(held, edited, {}, ["crm.contacts"]) == ["blueprint/crm.contacts"]


def test_both_prompts_carry_the_versions_clock_and_caveat_lines() -> None:
    blueprint = _contacts()
    entity = _entity()
    entity.fields.append(Field(name="created_at", type="timestamp"))
    model = _compile({"contacts": entity}, {blueprint.name: blueprint})
    raw = serialize_model(model)
    layer = serialize_layer(model)
    for prompt in (raw, layer.lower()):
        assert "rows repeat once per sync (_synced_at)" in prompt
        assert "_deleted holds on the latest version only" in prompt
        assert "clock created_at: when the contact was created, stored in utc" in prompt.lower()
        assert "a contact deleted in the crm keeps its earlier rows" in prompt.lower()


# ── the plan checks ──────────────────────────────────────────────────────────


def _scope(*tables: str) -> list[bp.ScopedTable]:
    return [bp.ScopedTable(CRM, t, ("lens crm_lens exposes",)) for t in tables]


def _check(
    blueprints: Sequence[TableBlueprint],
    *,
    profiles: list[TableProfile] | None = None,
    scope: list[bp.ScopedTable] | None = None,
    live: bp.LiveCounter | None = None,
    certified: Sequence[tuple[str, str, str]] = (),
    connections: Sequence[str] = (CRM,),
) -> bp.Findings:
    return bp.check_blueprints(
        {b.name: b for b in blueprints},
        connections=connections,
        profiles={CRM: _crm_profiles() if profiles is None else profiles},
        scope=_scope("crm.contacts") if scope is None else scope,
        fresh=lambda _p: True,
        live=live,
        certified=certified,
    )


def _no_live(_c: str, _t: str, _cols: Sequence[str]) -> grain.KeyCount:
    raise AssertionError("the probe decides; no live count may run")


def test_a_key_the_probe_proves_not_unique_is_an_error() -> None:
    findings = _check([_contacts(key=["contact_id"], versions=None)], live=_no_live)
    (error,) = findings.errors["crm.contacts"]
    assert "key [contact_id] is not unique in crm.contacts: 12 rows, 4 distinct (the probe)" in (
        error
    )
    assert "declare `versions:" in error


def test_a_unique_key_passes_without_a_live_count() -> None:
    accounts = TableBlueprint(
        connection=CRM, table="crm.accounts", grain="one row per account", key=["account_id"]
    )
    findings = _check([accounts], scope=_scope("crm.accounts"), live=_no_live)
    assert not findings.errors and not findings.warnings
    key = next(v for v in findings.verdicts["crm.accounts"] if v.check == "key")
    assert key.status == "pass" and "2 rows, 2 distinct (the probe)" in key.detail


def test_a_composite_key_is_counted_live() -> None:
    asked: list[tuple[str, tuple[str, ...]]] = []

    def live(_c: str, table: str, columns: Sequence[str]) -> grain.KeyCount:
        asked.append((table, tuple(columns)))
        return grain.KeyCount(rows=12, distinct=12, source="live")

    findings = _check([_contacts()], live=live)
    assert asked == [("crm.contacts", ("contact_id", "_synced_at"))]
    assert "crm.contacts" not in findings.errors
    key = next(v for v in findings.verdicts["crm.contacts"] if v.check == "key")
    assert key.status == "pass" and "(a live count)" in key.detail


def test_a_key_that_could_not_be_counted_is_a_warning_with_the_reason() -> None:
    def skipped(_c: str, _t: str, _cols: Sequence[str]) -> grain.KeyCount:
        return grain.KeyCount(skipped="the live count would scan 9 bytes, over the budget")

    findings = _check([_contacts()], live=skipped)
    assert "crm.contacts" not in findings.errors
    (warning,) = [w for w in findings.warnings if "could not verify" in w]
    assert "contact_id, _synced_at is unique in crm.contacts" in warning
    assert "over the budget" in warning


def test_a_column_the_table_lacks_is_an_error_with_the_near_miss() -> None:
    blueprint = _contacts(versions={"sync_column": "_synced", "latest_only": ["_deleted"]})
    findings = _check([blueprint], live=_no_live)
    errors = findings.errors["crm.contacts"]
    assert any(
        "versions.sync_column '_synced' is not a column of crm.contacts" in e
        and "did you mean '_synced_at'" in e
        for e in errors
    ), errors


def test_a_table_the_connection_lacks_and_an_unknown_connection_are_errors() -> None:
    gone = _contacts(table="crm.leads")
    assert "has no such table" in _check([gone]).errors["crm.leads"][0]
    stray = _contacts(connection="marketing")
    errors = _check([stray]).errors["crm.contacts"]
    assert "connection 'marketing', which dst.yaml does not declare" in errors[0]


def test_a_connection_with_no_probe_cannot_verify_and_says_so() -> None:
    findings = _check([_contacts()], profiles=[])
    assert not findings.errors
    assert any("no probe artifact" in w for w in findings.warnings)


def test_a_join_naming_a_missing_column_is_an_error_and_a_true_one_passes() -> None:
    bad = _contacts(
        joins=[
            {
                "to": "crm.accounts",
                "on": "contacts.account_id = accounts.acct",
                "cardinality": "one",
            }
        ]
    )
    errors = _check([bad], live=lambda *_a: grain.KeyCount(rows=12, distinct=12)).errors
    assert any("`on` names 'acct'" in e for e in errors["crm.contacts"]), errors
    good = _check([_contacts()], live=lambda *_a: grain.KeyCount(rows=12, distinct=12))
    joins = next(v for v in good.verdicts["crm.contacts"] if v.check == "joins")
    assert joins.status == "pass"


def test_an_in_scope_table_without_grain_and_key_warns_and_the_coverage_counts() -> None:
    scope = _scope("crm.contacts", "crm.accounts") + [
        bp.ScopedTable(CRM, "crm.notes", ("entity notes",), entity_declares=True)
    ]
    findings = _check(
        [_contacts()], scope=scope, live=lambda *_a: grain.KeyCount(rows=12, distinct=12)
    )
    (warning,) = [w for w in findings.warnings if "declare no grain and key" in w]
    assert "crm.accounts (lens crm_lens exposes)" in warning
    assert "crm.notes" not in warning  # its entity declares grain and primary_key
    assert (findings.covered, findings.in_scope) == (1, 3)
    assert findings.coverage_line() == (
        "table blueprints: 1 of 3 in-scope table(s) declare grain and key"
    )


def test_a_sync_hint_fires_on_a_repeating_key_beside_a_sync_column() -> None:
    findings = _check([], scope=_scope("crm.contacts"))
    (hint,) = [h for h in findings.hints if "sync or delete columns" in h]
    assert "crm.contacts: 'contact_id' repeats (12 rows)" in hint
    assert "_synced_at" in hint and "_deleted" in hint


def test_the_sync_hint_stays_quiet_once_versions_are_declared_or_rows_do_not_repeat() -> None:
    declared = _check([_contacts()], live=lambda *_a: grain.KeyCount(rows=12, distinct=12))
    assert not [h for h in declared.hints if "sync or delete" in h]
    # A soft-delete table: a delete flag, but one row per contact.
    unique = _crm_profiles()
    unique[0] = unique[0].model_copy(update={"row_count": 4})
    quiet = _check([], profiles=unique, scope=_scope("crm.contacts"))
    assert not [h for h in quiet.hints if "sync or delete" in h]


def _with_values(
    profile: TableProfile, column: str, values: list[str], *, complete: bool = True, **kw: object
) -> TableProfile:
    columns = [
        c.model_copy(update={"top_values": values, "values_complete": complete})
        if c.name == column
        else c
        for c in profile.columns
    ]
    return profile.model_copy(update={"columns": columns, **kw})


def _join_profiles() -> list[TableProfile]:
    """One row per contact; the probe holds both sides' contact ids."""
    contacts, accounts, notes = _crm_profiles()
    return [
        _with_values(contacts, "contact_id", ["1", "2", "3", "4"], row_count=4),
        accounts,
        _with_values(notes, "contact_id", ["1", "2"]),
    ]


def test_a_join_hint_fires_on_a_matching_key_and_not_once_declared() -> None:
    notes = TableBlueprint(
        connection=CRM, table="crm.notes", grain="one row per note", key=["note_id"]
    )
    contacts = _contacts(key=["contact_id"], versions=None)
    profiles = _join_profiles()
    scope = _scope("crm.notes", "crm.contacts")
    findings = _check([notes, contacts], profiles=profiles, scope=scope, live=_no_live)
    assert any(
        "crm.notes.contact_id looks like a join to crm.contacts.contact_id (2 of 2 probed "
        "value(s) of it are values of the key)" in h
        for h in findings.hints
    ), findings.hints
    joined = notes.model_copy(
        update={
            "joins": [
                TableBlueprint.model_validate(
                    {
                        "connection": CRM,
                        "table": "crm.notes",
                        "joins": [
                            {
                                "to": "crm.contacts",
                                "on": "notes.contact_id = contacts.contact_id",
                                "cardinality": "one",
                            }
                        ],
                    }
                ).joins[0]
            ]
        }
    )
    quiet = _check([joined, contacts], profiles=profiles, scope=scope, live=_no_live)
    assert not [h for h in quiet.hints if "looks like a join" in h]


def _join_check(
    profiles: list[TableProfile], *blueprints: TableBlueprint, scope: list[str] | None = None
) -> list[str]:
    notes = TableBlueprint(
        connection=CRM, table="crm.notes", grain="one row per note", key=["note_id"]
    )
    contacts = _contacts(key=["contact_id"], versions=None, joins=[])
    chosen = {b.name: b for b in (notes, contacts, *blueprints)}
    tables = scope or ["crm.notes", "crm.contacts"]
    return _check(
        list(chosen.values()), profiles=profiles, scope=_scope(*tables), live=_no_live
    ).hints


def _not_hinted(hints: list[str]) -> str:
    (line,) = [h for h in hints if h.startswith("join candidates not hinted")]
    return line


def test_no_join_is_hinted_to_a_table_the_probe_shows_empty() -> None:
    contacts, accounts, notes = _join_profiles()
    empty = contacts.model_copy(update={"row_count": 0})
    hints = _join_check([empty, accounts, notes])
    assert not [h for h in hints if "looks like a join" in h]
    assert "and 1 where the probe shows one of the tables empty" in _not_hinted(hints)


def test_no_join_is_hinted_on_a_name_alone() -> None:
    contacts, accounts, notes = _crm_profiles()  # no dictionary of contact_id anywhere
    hints = _join_check([contacts.model_copy(update={"row_count": 4}), accounts, notes])
    assert not [h for h in hints if "looks like a join" in h]
    assert "1 column(s) named like another table's key" in _not_hinted(hints)


def test_no_join_is_hinted_when_the_values_do_not_overlap() -> None:
    contacts, accounts, notes = _join_profiles()
    disjoint = _with_values(notes, "contact_id", ["7", "8"])
    hints = _join_check([contacts, accounts, disjoint])
    assert not [h for h in hints if "looks like a join" in h]
    # Most of the values missing from a complete key dictionary is no join either.
    mostly = _with_values(notes, "contact_id", ["1", "7", "8"])
    assert not [h for h in _join_check([contacts, accounts, mostly]) if "looks like a" in h]
    # A partial key dictionary cannot rule a value out: one shared value hints.
    partial = _with_values(contacts, "contact_id", ["1", "3"], complete=False, row_count=4)
    assert [h for h in _join_check([partial, accounts, mostly]) if "looks like a" in h]


def test_tables_of_two_declared_systems_are_never_paired() -> None:
    profiles = _join_profiles()
    legacy = TableBlueprint(
        connection=CRM,
        table="crm.notes",
        grain="one row per note",
        key=["note_id"],
        system="legacy CRM extract",
    )
    current = _contacts(key=["contact_id"], versions=None, joins=[], system="CRM")
    assert not [h for h in _join_check(profiles, legacy, current) if "looks like" in h]
    # The same system, spelled differently, still pairs; one undeclared side too.
    same = current.model_copy(update={"system": "legacy_crm  extract"})
    assert [h for h in _join_check(profiles, legacy, same) if "looks like" in h]
    assert [h for h in _join_check(profiles, legacy) if "looks like" in h]


def test_join_hints_are_ranked_and_capped_and_the_rest_counted() -> None:
    contacts, accounts, notes = _join_profiles()
    profiles = [contacts, accounts]
    tables = ["crm.contacts"]
    blueprints = []
    # Five keyed tables named so a notes column matches each; one overlaps less.
    for n in range(5):
        table = f"crm.owner{n}s"
        profiles.append(
            TableProfile(
                connection=CRM,
                table=table,
                row_count=3,
                profiled_at=NOW,
                source="sampled",
                columns=[
                    _col(
                        "id",
                        "INTEGER",
                        distinct_count=3,
                        distinct_is_exact=True,
                        top_values=["1", "2", "3"] if n else ["1", "9", "10"],
                        values_complete=bool(n),  # owner0's dictionary is partial
                    )
                ],
            )
        )
        blueprints.append(TableBlueprint(connection=CRM, table=table, key=["id"]))
        tables.append(table)
    notes = notes.model_copy(
        update={
            "columns": [
                *notes.columns,
                *(
                    _col(f"owner{n}_id", "INTEGER", top_values=["1", "2"], values_complete=True)
                    for n in range(5)
                ),
            ]
        }
    )
    hints = _join_check([*profiles, notes], *blueprints, scope=["crm.notes", *tables])
    listed = [h for h in hints if h.startswith("crm.notes.") and "looks like a join" in h]
    assert len(listed) == bp.JOIN_HINTS_PER_TABLE
    assert not any("owner0" in h for h in listed)  # 1 of 2 values: ranked last
    assert (
        "crm.notes: 3 more likely join(s) with fewer of its values in the key are not listed"
        in "\n".join(hints)
    )


def test_a_join_in_certified_sql_that_no_blueprint_declares_is_a_hint() -> None:
    notes = TableBlueprint(
        connection=CRM, table="crm.notes", grain="one row per note", key=["note_id"]
    )
    sql = (
        "SELECT a.name, count(*) FROM crm.notes n JOIN crm.contacts c "
        "ON n.contact_id = c.contact_id JOIN crm.accounts a ON c.account_id = a.account_id "
        "GROUP BY a.name"
    )
    findings = _check(
        [notes, _contacts()],
        scope=_scope("crm.notes"),
        certified=[(CRM, "duckdb", sql)],
        live=lambda *_a: grain.KeyCount(rows=12, distinct=12),
    )
    joins = [h for h in findings.hints if h.startswith("certified SQL joins")]
    # notes-contacts is undeclared; contacts-accounts is declared in contacts' blueprint.
    assert joins == [
        "certified SQL joins crm.notes and crm.contacts, and no blueprint declares that "
        "join — add it under `joins:` with its cardinality"
    ], joins


# ── the grain reading on an answer ───────────────────────────────────────────


def _unstamp(lines: Sequence[str]) -> list[str]:
    """Lines with the count's timestamp replaced, so a test pins the words."""
    return [re.sub(r"\d{4}-\d\d-\d\d \d\d:\d\d UTC", "<when>", line) for line in lines]


@pytest.fixture(autouse=True)
def _no_cached_counts() -> Iterator[None]:
    grain.clear_live_counts()
    yield
    grain.clear_live_counts()


def test_rows_matching_the_key_read_as_one_row_per_key() -> None:
    accounts = TableBlueprint(connection=CRM, table="crm.accounts", key=["account_id"])
    r = grain.reading(
        "crm.accounts", accounts, grain.KeyCount(rows=2, distinct=2, source="probe", taken_at=NOW)
    )
    assert r.status == "one_per_key"
    assert r.line == (
        "crm.accounts: 2 rows, 2 distinct account_id (per the probe of 2026-10-20 00:00 UTC), "
        "one row per account_id"
    )


def test_rows_repeating_per_sync_name_the_sync_and_the_latest_only_columns() -> None:
    r = grain.reading(
        "crm.contacts",
        _contacts(),
        grain.KeyCount(rows=12, distinct=4, unique=12, source="live", taken_at=NOW),
    )
    assert r.status == "repeats"
    assert r.line == (
        "crm.contacts: 12 rows for 4 distinct contact_id (counted live 2026-10-20 00:00 UTC): "
        "rows repeat per "
        "_synced_at, and _deleted holds on the latest version only; a count or sum over it "
        "is right only when it reads the latest version"
    )


def test_counts_contradicting_the_declared_key_say_so() -> None:
    blueprint = _contacts(key=["contact_id"], versions=None)
    r = grain.reading(
        "crm.contacts", blueprint, grain.KeyCount(rows=12, distinct=4, source="probe")
    )
    assert (
        r.status == "contradicts" and "but its blueprint declares one row per contact_id" in r.line
    )


def test_no_declared_key_and_no_count_both_say_could_not_verify() -> None:
    bare = grain.reading("crm.notes", None, None)
    assert bare.status == "unverified"
    assert bare.line.startswith("could not verify the grain of crm.notes: no key is declared")
    skipped = grain.reading("crm.contacts", _contacts(), grain.KeyCount(skipped="no connection"))
    assert skipped.line == "could not verify the grain of crm.contacts: no connection"


def test_a_hidden_key_column_is_never_named() -> None:
    blueprint = _contacts(key=["email"], versions=None)
    r = grain.reading(
        "crm.contacts",
        blueprint,
        grain.KeyCount(rows=12, distinct=4, source="probe"),
        hidden=frozenset({"email"}),
    )
    assert "email" not in r.line and "the declared key" in r.line


def _verified() -> VerificationReport:
    return VerificationReport(grade="verified", checks=[VerificationCheck(name="x", status="pass")])


def test_the_fold_never_calls_an_inflated_or_unchecked_count_verified() -> None:
    def fold(status: grain.GrainStatus) -> VerificationReport:
        return grain.fold(_verified(), [grain.GrainReading("t", status, "line")])

    assert fold("one_per_key").grade == "verified"
    assert fold("repeats").grade == "partial"
    assert fold("unverified").grade == "partial"
    assert fold("contradicts").grade == "unverified"
    statuses = {c.status for c in fold("repeats").checks if c.name == "grain"}
    assert statuses == {"fail"}
    # Never upgrades.
    low = VerificationReport(grade="unverified", checks=[])
    assert grain.fold(low, [grain.GrainReading("t", "one_per_key", "l")]).grade == "unverified"


def test_a_live_count_runs_against_the_table(tmp_path: Path) -> None:
    connector = DuckDBConnector(_crm_db(tmp_path))
    counted = grain.live_count(connector, "crm.contacts", ["contact_id"], connection=CRM)
    assert (counted.rows, counted.distinct, counted.source) == (12, 4, "live")
    both = grain.live_count(connector, "crm.contacts", ["contact_id", "_synced_at"], connection=CRM)
    assert (both.rows, both.distinct) == (12, 12)


class _Billed:
    """A warehouse that bills scanned bytes: the dry run says how many."""

    kind = "bigquery"
    sample_bytes_budget = 1_000

    def __init__(self, estimate: int) -> None:
        self.estimate = estimate
        self.ran: list[str] = []

    def dry_run(self, sql: str) -> DryRunResult:
        return DryRunResult(bytes_estimated=self.estimate, valid=True)

    def execute(
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ) -> QueryResult:
        self.ran.append(sql)
        return QueryResult(columns=["n", "d"], rows=[[10, 10]])


def test_a_live_count_over_the_byte_budget_is_skipped_and_said() -> None:
    over = _Billed(estimate=5_000_000)
    counted = grain.live_count(over, "p.crm.contacts", ["contact_id"], connection=CRM)  # type: ignore[arg-type]
    assert not counted.counted and over.ran == []
    assert counted.skipped == (
        "the live count would scan 5,000,000 bytes, over the connection's 1,000-byte "
        "sampling budget (config: sample_bytes_budget), so it was not run"
    )
    under = _Billed(estimate=10)
    counted = grain.live_count(under, "p.crm.contacts", ["contact_id"], connection=CRM)  # type: ignore[arg-type]
    assert counted.counted and under.ran and "`p.crm.contacts`" in under.ran[0]


def test_the_probe_decides_only_when_its_counts_are_exact_and_fresh() -> None:
    contacts = _crm_profiles()[0]
    assert grain.probe_count(contacts, ["contact_id"], fresh=True).counted
    assert "older than probe.max_age" in str(
        grain.probe_count(contacts, ["contact_id"], fresh=False).skipped
    )
    sampled = contacts.model_copy(update={"sampled_rows": 5})
    assert "from a sample" in str(grain.probe_count(sampled, ["contact_id"], fresh=True).skipped)
    assert "several" in str(
        grain.probe_count(contacts, ["contact_id", "_synced_at"], fresh=True).skipped
    )


# ── serving: an EXPOSED answer over the CRM copy ─────────────────────────────


def _exposed_model(blueprints: dict[str, TableBlueprint]) -> SemanticModel:
    return _compile({}, blueprints, expose=[{"connection": CRM, "tables": ["crm.*"]}])


def _ask(  # type: ignore[no-untyped-def]
    model: SemanticModel, db: str, sql: str, question: str = "How many contacts?", **kw: object
):
    return run_query(
        question=question,
        lens_name="crm_lens",
        org_id="org-1",
        caller="analyst",
        semantic_model=model,
        connector=DuckDBConnector(db),
        generator=EchoQueryGenerator(sql),
        composer=None,
        **kw,  # type: ignore[arg-type]
    )


def test_an_exposed_count_over_a_synced_table_is_never_called_verified(tmp_path: Path) -> None:
    db = _crm_db(tmp_path)
    blueprint = _contacts()
    res = _ask(
        _exposed_model({blueprint.name: blueprint}), db, "SELECT count(*) AS n FROM contacts"
    )
    assert res.trace.status == "ok", res.trace.error
    assert res.response.data is not None
    grain_lines = [n for n in res.response.degraded if n.startswith("GRAIN: ")]
    assert _unstamp(grain_lines) == [
        "GRAIN: crm.contacts: 12 rows for 4 distinct contact_id (counted live <when>): rows repeat "
        "per _synced_at, and _deleted holds on the latest version only; a count or sum over "
        "it is right only when it reads the latest version"
    ], res.response.degraded
    assert res.response.confidence != "verified"
    assert any(c.name == "grain" and c.status == "fail" for c in res.response.verification.checks)


def test_a_contradicted_key_grades_unverified(tmp_path: Path) -> None:
    db = _crm_db(tmp_path)
    false_key = _contacts(key=["contact_id"], versions=None)
    res = _ask(
        _exposed_model({false_key.name: false_key}), db, "SELECT count(*) AS n FROM contacts"
    )
    assert res.response.confidence == "unverified"


def test_an_exposed_table_with_no_blueprint_could_not_be_verified(tmp_path: Path) -> None:
    db = _crm_db(tmp_path)
    res = _ask(_exposed_model({}), db, "SELECT count(*) AS n FROM notes", "How many notes?")
    lines = [n for n in res.response.degraded if n.startswith("GRAIN: ")]
    assert lines == [
        "GRAIN: could not verify the grain of crm.notes: no key is declared for it "
        "(semantic/tables/), so whether its rows repeat is not known"
    ]
    assert res.response.confidence != "verified"


def test_fresh_probe_counts_decide_without_a_live_count(tmp_path: Path) -> None:
    db = _crm_db(tmp_path)
    accounts = TableBlueprint(connection=CRM, table="crm.accounts", key=["account_id"])
    profiles = {p.table: p for p in _crm_profiles()}
    res = _ask(
        _exposed_model({accounts.name: accounts}),
        db,
        "SELECT count(*) AS n FROM accounts",
        "How many accounts?",
        table_profiles=profiles,
    )
    lines = [n for n in res.response.degraded if n.startswith("GRAIN: ")]
    assert lines == [
        "GRAIN: crm.accounts: 2 rows, 2 distinct account_id (per the probe of 2026-10-20 "
        "00:00 UTC), one row per account_id"
    ]


# ── describe ─────────────────────────────────────────────────────────────────


def test_describe_shows_the_blueprint_the_verdicts_and_the_probe_facts() -> None:
    blueprint = _contacts(
        key=["contact_id"], versions=None, clocks={"created_at": {"meaning": "created"}}
    )
    model = _compile(
        {},
        {blueprint.name: blueprint},
        expose=[{"connection": CRM, "tables": ["crm.contacts"], "hide": ["contacts.email"]}],
    )
    described = describe_model(
        "crm_lens",
        display_name="CRM",
        description="",
        model=model,
        profiles={p.table: p for p in _crm_profiles()},
    )
    (table,) = described.exposed_tables
    assert (
        table.blueprint is not None and table.blueprint["grain"] == "one row per contact per sync"
    )
    verdicts = {c.check: c for c in table.checks}
    assert verdicts["table"].verdict == "pass"
    assert verdicts["key"].verdict == "fail" and "12 rows, 4 distinct" in verdicts["key"].detail
    assert table.facts is not None and table.facts.rows == 12
    assert [c.name for c in table.facts.columns] == [
        "contact_id",
        "account_id",
        "created_at",
        "_synced_at",
        "_deleted",
    ]
    assert "email" not in str(table.model_dump())
    assert described.note is not None and "GRAIN line" in described.note


def test_describe_never_names_a_hidden_key_column() -> None:
    blueprint = _contacts(key=["email"], versions=None, clocks={}, joins=[])
    model = _compile(
        {},
        {blueprint.name: blueprint},
        expose=[{"connection": CRM, "tables": ["crm.contacts"], "hide": ["contacts.email"]}],
    )
    described = describe_model(
        "crm_lens",
        display_name="CRM",
        description="",
        model=model,
        profiles={p.table: p for p in _crm_profiles()},
    )
    (table,) = described.exposed_tables
    assert table.blueprint is not None and table.blueprint["key"] == "withheld"
    assert "email" not in str(table.model_dump())


def test_describe_without_a_probe_says_it_could_not_verify() -> None:
    blueprint = _contacts()
    model = _exposed_model({blueprint.name: blueprint})
    described = describe_model("crm_lens", display_name="CRM", description="", model=model)
    contacts = next(t for t in described.exposed_tables if t.table == "crm.contacts")
    assert [c.verdict for c in contacts.checks] == ["unverified"]
    assert contacts.facts is None


def _wide_lens(tables: int) -> tuple[SemanticModel, dict[str, TableProfile]]:
    """An invented warehouse of *tables* event tables, each with a blueprint
    and a long caveat, all exposed by one lens."""
    profiles = [
        TableProfile(
            connection=CRM,
            table=f"events.stream_{n:04d}",
            row_count=1000 + n,
            profiled_at=NOW,
            source="sampled",
            columns=[
                _col("event_id", "INTEGER", distinct_count=1000 + n, distinct_is_exact=True),
                *(
                    _col(f"attr_{k}", "VARCHAR", top_values=[f"v{k}_{v}" for v in range(12)])
                    for k in range(8)
                ),
            ],
        )
        for n in range(tables)
    ]
    blueprints = {
        p.table: TableBlueprint(
            connection=CRM,
            table=p.table,
            grain="one row per event",
            key=["event_id"],
            caveats=["late events land up to a day after they happen " * 3],
        )
        for p in profiles
    }
    config = LensConfig(
        name="wide",
        connections=[CRM],
        expose=[{"connection": CRM, "tables": ["events.*"]}],  # type: ignore[list-item]
        untyped_fallback=True,
    )
    model, _warnings = compile_lens_model(
        config=config,
        shared_entities={},
        shared_definitions={},
        shared_relationships={},
        local_definitions=[],
        use_when=[],
        sample_queries=[],
        dialect="duckdb",
        profiles={CRM: profiles},
        blueprints=blueprints,
    )
    return model, {p.table: p for p in profiles}


def test_describe_summarises_each_exposed_table_and_describes_one_in_full() -> None:
    from services.lenses.describe import DESCRIBE_MAX_CHARS

    model, profiles = _wide_lens(60)
    full = describe_model("wide", display_name="W", description="", model=model, profiles=profiles)
    assert len(full.model_dump_json()) > DESCRIBE_MAX_CHARS  # what a driver saw cut off
    kw = {"display_name": "W", "description": "", "model": model, "profiles": profiles}
    summary = describe_model("wide", **kw, exposed="summary", max_chars=DESCRIBE_MAX_CHARS)  # type: ignore[arg-type]
    assert len(summary.model_dump_json()) <= DESCRIBE_MAX_CHARS
    assert summary.elided is None and len(summary.exposed_tables) == 60
    first = summary.exposed_tables[0]
    assert first.detail == "summary" and first.grain == "one row per event"
    assert first.key == ["event_id"] and first.rows == 1000 and first.column_count == 9
    assert first.flags == ["1 caveat(s)"] and first.columns == [] and first.blueprint is None
    assert summary.note is not None and "with `table`" in summary.note
    one = describe_model("wide", **kw, exposed="summary", table="stream_0007")  # type: ignore[arg-type]
    (table,) = one.exposed_tables
    assert table.detail == "full" and table.facts is not None and len(table.columns) == 9
    assert table.blueprint is not None and table.blueprint["caveats"]
    with pytest.raises(KeyError):
        describe_model("wide", **kw, table="events.nope")  # type: ignore[arg-type]


def test_a_description_over_the_bound_says_what_it_left_out() -> None:
    model, profiles = _wide_lens(400)
    kw = {"display_name": "W", "description": "", "model": model, "profiles": profiles}
    out = describe_model("wide", **kw, exposed="summary", max_chars=60_000)  # type: ignore[arg-type]
    assert len(out.model_dump_json()) <= 60_000
    shown, cut = len(out.exposed_tables), len(out.elided_tables)
    assert shown + cut == 400 and cut > 0
    assert out.elided_tables[0] == f"events.stream_{shown:04d}"
    assert out.elided is not None and f"{cut} of 400 exposed table(s)" in out.elided
    assert "describe_lens with `table`" in out.elided
    # So tight even the names do not fit: the rest are counted, never dropped.
    tight = describe_model("wide", **kw, exposed="summary", max_chars=4_000)  # type: ignore[arg-type]
    assert len(tight.model_dump_json()) <= 4_000
    assert tight.elided is not None and "not even by name" in tight.elided
    # One table over the bound loses its value lists first, and says so.
    lean = describe_model("wide", **kw, table="stream_0001", max_chars=3_300)  # type: ignore[arg-type]
    assert lean.elided is not None and "value lists" in lean.elided
    (table,) = lean.exposed_tables
    assert table.facts is not None and all(c.values is None for c in table.facts.columns)


def _described(model: SemanticModel) -> dict[str, object]:
    described = describe_model(
        "crm_lens",
        display_name="CRM",
        description="",
        model=model,
        profiles={p.table: p for p in _crm_profiles()},
    )
    return {t.table: t.blueprint for t in described.exposed_tables}


def test_blueprint_text_naming_a_hidden_column_is_withheld_on_every_door() -> None:
    blueprint = _contacts(
        grain="one row per contact per sync (email is unique too)",
        clocks={"created_at": {"meaning": "when contacts.email was first seen"}},
        joins=[{"to": "crm.notes", "on": "contacts.email = notes.body", "cardinality": "many"}],
        caveats=['email is hidden from every lens; never surface "EMAIL" when asked'],
    )
    model = _compile(
        {},
        {blueprint.name: blueprint},
        expose=[{"connection": CRM, "tables": ["crm.*"], "hide": ["contacts.email"]}],
    )
    shown = model.blueprints["crm.contacts"]
    assert shown.caveats[0] == (
        "[withheld column] is hidden from every lens; never surface [withheld column] when asked"
    )
    assert shown.grain == "one row per contact per sync ([withheld column] is unique too)"
    assert shown.joins == [] and "1 declared join(s) withheld" in shown.caveats[1]
    # The doors: describe, both generation prompts, the inherited grain.
    view = _described(model)["crm.contacts"]
    assert isinstance(view, dict) and "withheld" in view
    assert "email" not in str(view).lower()
    contacts = next(e for e in model.entities if e.source.table == "crm.contacts")
    assert contacts.grain is not None and "email" not in contacts.grain
    assert "email" not in serialize_model(model).lower()
    assert "email" not in serialize_layer(model).lower()


def test_another_tables_hidden_column_is_withheld_qualified_and_bare() -> None:
    notes = TableBlueprint(
        connection=CRM,
        table="crm.notes",
        grain="one row per note",
        key=["note_id"],
        caveats=[
            "the author's address is crm.contacts.email",
            "email is never copied here",
            "contacts.account_id is the owner; note_id is the key",
        ],
    )
    accounts = TableBlueprint(
        connection=CRM,
        table="crm.accounts",
        key=["account_id"],
        caveats=["account_id is the CRM's id; contacts.account_id points here"],
    )
    model = _compile(
        {},
        {notes.name: notes, accounts.name: accounts},
        expose=[
            {
                "connection": CRM,
                "tables": ["crm.*"],
                "hide": ["contacts.email", "contacts.account_id"],
            }
        ],
    )
    assert model.blueprints["crm.notes"].caveats == [
        "the author's address is [withheld column]",
        "[withheld column] is never copied here",
        "[withheld column] is the owner; note_id is the key",
    ]
    # A column the table itself reads stays named; the hiding table's
    # qualified form is still withheld.
    assert model.blueprints["crm.accounts"].caveats == [
        "account_id is the CRM's id; [withheld column] points here"
    ]


def test_plan_warns_when_a_blueprint_names_a_hidden_column() -> None:
    noisy = _contacts(caveats=["email is hidden; never surface it"])
    scope = [bp.ScopedTable(CRM, "crm.contacts", ("lens crm_lens exposes",), hidden=("email",))]
    findings = _check([noisy], scope=scope, live=lambda *_a: grain.KeyCount(rows=12, distinct=12))
    (warning,) = [w for w in findings.warnings if "names a column a lens hides" in w]
    assert "caveat 1 names email" in warning and "[withheld column]" in warning
    clean = _check(
        [_contacts()], scope=scope, live=lambda *_a: grain.KeyCount(rows=12, distinct=12)
    )
    assert not [w for w in clean.warnings if "names a column a lens hides" in w]


# ── the CLI renders the new rows ─────────────────────────────────────────────


def test_plan_output_prints_warnings_and_the_coverage_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from services.cli.project import _render_plan, _summarize_plan

    plan: list[dict[str, object]] = [
        {"scope": "semantic", "path": "semantic/tables/crm.contacts.yaml", "status": "create"},
        {"scope": "semantic", "path": "semantic/tables", "status": "warning", "warning": "W1"},
        {"scope": "semantic", "path": "semantic/tables", "status": "hint", "hint": "H1"},
        {
            "scope": "semantic",
            "path": "semantic/tables",
            "status": "coverage",
            "coverage": {"covered": 1, "in_scope": 3},
            "note": "table blueprints: 1 of 3 in-scope table(s) declare grain and key",
        },
    ]
    _summarize_plan(plan)
    out = capsys.readouterr().out
    assert "W1" in out and "H1" not in out
    assert "Plan: 1 to add, 1 hint(s)." in out
    assert "Table blueprints: 1 of 3 in-scope table(s) declare grain and key." in out
    lines: list[str] = []
    assert _render_plan(plan, lines.append) == 0
    assert "warning: W1" in lines and "hint: H1" in lines
    assert "table blueprints: 1 of 3 in-scope table(s) declare grain and key" in lines


# ── end to end: plan, apply, the sql tool, describe ──────────────────────────


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
def org(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr("services.llm.registry.resolve", lambda _ref: None)
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('BlueprintTest') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": oid, "h": hash_token(raw)},
        )
    yield oid, {"Authorization": f"Bearer {raw}"}
    with admin.begin() as c:
        for table in (
            "request_log",
            "eval_run",
            "eval_case",
            "certified_answer",
            "lens_version",
            "lens",
            "semantic_asset",
            "table_profile",
            "setting",
            "connection",
            "admin_token",
        ):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": oid})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})
    admin.dispose()


def _project(db: str, *, blueprints: dict[str, str] | None = None) -> dict[str, str]:
    """A CRM project: the connection, a probe artifact, one lens exposing the
    copy, and whatever blueprints the test brings."""
    now = datetime.now(UTC) - timedelta(hours=1)
    files = {
        "dst.yaml": yaml.safe_dump(
            {"connections": {CRM: {"type": "duckdb", "config": {"path": db}}}}
        ),
        "profiles/crm.probe.json": ProbeArtifact(
            connection=CRM, probed_at=now, tables=_crm_profiles(now)
        ).model_dump_json(),
        "lenses/crm_lens/lens.yaml": yaml.safe_dump(
            {
                "name": "crm_lens",
                "connections": [CRM],
                "untyped_fallback": True,
                "expose": [{"connection": CRM, "tables": ["crm.*"], "hide": ["contacts.email"]}],
            }
        ),
    }
    files.update(blueprints or {})
    return files


def _plan(headers: dict[str, str], files: dict[str, str]) -> list[dict[str, object]]:
    r = client.post("/mgmt/project/plan", headers=headers, json={"files": files})
    assert r.status_code == 200, r.text
    return list(r.json())


def _apply(headers: dict[str, str], files: dict[str, str]) -> list[dict[str, object]]:
    r = client.post("/mgmt/project/apply", headers=headers, json={"files": files})
    assert r.status_code == 200, r.text
    return list(r.json())


def _table_rows(plan: list[dict[str, object]]) -> list[dict[str, object]]:
    return [e for e in plan if e.get("path") == "semantic/tables"]


@needs_db
def test_a_project_with_no_blueprints_plans_as_before_plus_warnings(org, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _oid, headers = org
    files = _project(_crm_db(tmp_path))
    plan = _plan(headers, files)
    rest = [e for e in plan if e.get("path") != "semantic/tables"]
    assert not [e for e in rest if e.get("status") == "invalid"], rest
    assert next(e for e in rest if e.get("lens") == "crm_lens")["status"] == "create"
    rows = _table_rows(plan)
    assert {r["status"] for r in rows} <= {"warning", "hint", "coverage"}, rows
    warning = next(r for r in rows if r["status"] == "warning")
    assert "3 in-scope table(s) declare no grain and key" in str(warning["warning"])
    coverage = next(r for r in rows if r["status"] == "coverage")
    assert coverage["coverage"] == {"covered": 0, "in_scope": 3}
    out = _apply(headers, files)
    assert out[-1].get("action") != "aborted", out
    tables = next(r for r in out if r.get("scope") == "tables")
    assert tables["errors"] == [] and tables["applied"] == [
        "table blueprints: 0 of 3 in-scope table(s) declare grain and key"
    ]
    # Re-planned after apply: the lens is unchanged and the same warning stands.
    again = _plan(headers, files)
    assert next(e for e in again if e.get("lens") == "crm_lens")["status"] == "unchanged"
    assert [r["status"] for r in _table_rows(again)] == [r["status"] for r in rows]


@needs_db
def test_a_false_key_fails_plan_and_aborts_apply(org, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _oid, headers = org
    false_key = CONTACTS_YAML.replace("key: [contact_id, _synced_at]", "key: [contact_id]")
    false_key = false_key.split("versions:")[0] + "clocks:" + false_key.split("clocks:")[1]
    files = _project(_crm_db(tmp_path), blueprints={"semantic/tables/contacts.yaml": false_key})
    plan = _plan(headers, files)
    row = next(e for e in plan if e.get("path") == "semantic/tables/contacts.yaml")
    assert row["status"] == "invalid", row
    assert "key [contact_id] is not unique in crm.contacts: 12 rows, 4 distinct" in str(
        row["error"]
    )
    out = _apply(headers, files)
    assert out[-1]["action"] == "aborted", out
    tables = next(r for r in out if r.get("scope") == "tables")
    assert tables["action"] == "rejected" and tables["errors"], tables


@needs_db
def test_a_true_blueprint_lands_and_answers_carry_the_grain(org, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    oid, headers = org
    db = _crm_db(tmp_path)
    files = _project(db, blueprints={"semantic/tables/contacts.yaml": CONTACTS_YAML})
    plan = _plan(headers, files)
    assert not [e for e in plan if e.get("status") == "invalid"], plan
    coverage = next(r for r in _table_rows(plan) if r["status"] == "coverage")
    assert coverage["coverage"] == {"covered": 1, "in_scope": 3}
    out = _apply(headers, files)
    assert out[-1].get("action") != "aborted", out
    assert any("created blueprint/crm.contacts" in a for r in out for a in r.get("applied") or [])

    exported = client.get("/mgmt/project/export", headers=headers).json()["files"]
    assert "semantic/tables/crm.contacts.yaml" in exported
    again = _plan(headers, files)
    assert (
        next(e for e in again if e.get("path") == "semantic/tables/contacts.yaml")["status"]
        == "unchanged"
    )
    assert next(e for e in again if e.get("lens") == "crm_lens")["status"] == "unchanged"

    sql = client.post(
        "/v1/sql",
        headers=headers,
        json={"lens": "crm_lens", "sql": "SELECT count(*) AS n FROM contacts"},
    )
    assert sql.status_code == 200, sql.text
    assert sql.json()["rows"] == [[12]]
    # A versioned key is counted live: the probe cannot say the key plus the
    # sync column is still unique.
    assert _unstamp(sql.json()["grain"]) == [
        "crm.contacts: 12 rows for 4 distinct contact_id (counted live <when>): rows repeat per "
        "_synced_at, and _deleted holds on the latest version only; a count or sum over it "
        "is right only when it reads the latest version"
    ]

    described = client.get("/v1/lenses/crm_lens", headers=headers).json()
    summary = next(t for t in described["exposed_tables"] if t["table"] == "crm.contacts")
    assert summary["detail"] == "summary" and summary["rows"] == 12
    assert summary["versions"]["sync_column"] == "_synced_at" and summary["columns"] == []
    one = client.get("/v1/lenses/crm_lens", params={"table": "contacts"}, headers=headers)
    assert one.status_code == 200, one.text
    (contacts,) = one.json()["exposed_tables"]
    assert contacts["detail"] == "full"
    assert contacts["blueprint"]["versions"]["sync_column"] == "_synced_at"
    assert contacts["facts"]["rows"] == 12
    assert "email" not in str(contacts)
    assert {c["check"] for c in contacts["checks"]} >= {"table", "columns", "key", "joins"}
    missing = client.get("/v1/lenses/crm_lens", params={"table": "nope"}, headers=headers)
    assert missing.status_code == 404 and "exposes no table named 'nope'" in missing.text


# ── the scaffold: a slot map, and the reference block ───────────────────────


def test_init_scaffolds_the_blueprint_skill_and_its_reference_block(tmp_path: Path) -> None:
    from tests.test_cli_init import _init

    root = _init(tmp_path)
    skill = (root / ".claude/skills/dst-blueprints/SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: dst-blueprints\n")
    for slot in ("semantic/tables/", "`grain`", "`key`", "`versions`", "`clocks.<column>`"):
        assert slot in skill, slot
    assert "never fails a plan" in skill and "byte budget" in skill
    readme = (root / "semantic/README.md").read_text(encoding="utf-8")
    assert "Reference: table blueprint (semantic/tables/*.yaml)" in readme
    assert "# versions: null  # set when rows repeat once per sync" in readme
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert ".claude/skills/dst-blueprints/" in agents and "tables/<table>.yaml" in agents


# ── a live count is cached per org, connection, table and key ───────────────


class _Counting(DuckDBConnector):
    """A DuckDB connector that counts the statements it runs."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.statements: list[str] = []

    def execute(  # type: ignore[override]
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ) -> QueryResult:
        self.statements.append(sql)
        return super().execute(sql, read_only=read_only, row_limit=row_limit)


def test_a_live_count_is_reused_until_its_ttl_and_then_taken_again(tmp_path: Path) -> None:
    connector = _Counting(_crm_db(tmp_path))
    clock = [NOW]
    ttl = timedelta(hours=6)

    def count() -> grain.KeyCount:
        return grain.cached_live_count(
            "org-1",
            connector,
            "crm.contacts",
            ["contact_id"],
            connection=CRM,
            ttl=ttl,
            now=lambda: clock[0],
        )

    first = count()
    assert first.counted and first.taken_at == NOW and len(connector.statements) == 1
    clock[0] = NOW + timedelta(hours=5)
    hit = count()
    assert hit == first and len(connector.statements) == 1  # reused, stamp unchanged
    # Another org naming its connection alike never reads this org's count.
    other = grain.cached_live_count(
        "org-2",
        connector,
        "crm.contacts",
        ["contact_id"],
        connection=CRM,
        ttl=ttl,
        now=lambda: clock[0],
    )
    assert other.counted and len(connector.statements) == 2
    clock[0] = NOW + timedelta(hours=6, minutes=1)
    again = count()
    assert again.taken_at == clock[0] and len(connector.statements) == 3


def test_answers_reuse_one_live_count_and_say_when_it_was_taken(tmp_path: Path) -> None:
    db = _crm_db(tmp_path)
    blueprint = _contacts()
    model = _exposed_model({blueprint.name: blueprint})
    counter = _Counting(db)

    def ask() -> list[str]:
        res = run_query(
            question="How many contacts?",
            lens_name="crm_lens",
            org_id="org-1",
            caller="analyst",
            semantic_model=model,
            connector=counter,
            generator=EchoQueryGenerator("SELECT count(*) AS n FROM contacts"),
            composer=None,
        )
        return [n for n in res.response.degraded if n.startswith("GRAIN: ")]

    first, second = ask(), ask()
    counts = [q for q in counter.statements if "SELECT DISTINCT" in q]
    assert len(counts) == 1, counter.statements
    assert first == second and re.search(r"counted live \d{4}-\d\d-\d\d \d\d:\d\d UTC", first[0])


def test_the_default_ttl_is_one_hour_when_the_probe_clock_is_off() -> None:
    assert grain.LIVE_COUNT_TTL_DEFAULT == timedelta(hours=1)


# ── a stored blueprint the warehouse moved under ─────────────────────────────


def test_a_versioned_grain_that_no_longer_holds_grades_unverified(tmp_path: Path) -> None:
    db = _crm_db(tmp_path, duplicate=True)
    blueprint = _contacts()
    res = _ask(
        _exposed_model({blueprint.name: blueprint}), db, "SELECT count(*) AS n FROM contacts"
    )
    (line,) = [n for n in res.response.degraded if n.startswith("GRAIN: ")]
    assert "13 rows for 12 distinct contact_id, _synced_at" in line
    assert "its declared grain no longer holds" in line
    assert res.response.confidence == "unverified"


@needs_db
def test_a_stored_blueprint_that_stops_holding_warns_and_never_aborts(org, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _oid, headers = org
    blueprints = {"semantic/tables/contacts.yaml": CONTACTS_YAML}
    out = _apply(headers, _project(_crm_db(tmp_path / "a"), blueprints=blueprints))
    assert out[-1].get("action") != "aborted", out

    # The warehouse moves under the stored blueprint (here: the connection now
    # points at a copy with a duplicated row); the push does not touch it.
    moved = _project(_crm_db(tmp_path / "b", duplicate=True), blueprints=blueprints)
    out = _apply(headers, moved)
    assert out[-1].get("action") != "aborted", out
    tables = next(r for r in out if r.get("scope") == "tables")
    assert tables["errors"] == [], tables
    (warning,) = [w for w in tables["warnings"] if w.startswith("DRIFT: ")]
    assert "crm.contacts: its blueprint no longer holds (key [contact_id, _synced_at] is not " in (
        warning
    )
    assert "lenses reading it: crm_lens; answers that read it grade unverified" in warning
    # Plan says the same, as a warning on the blueprint's own file.
    plan = _plan(headers, moved)
    assert not [e for e in plan if e.get("status") == "invalid"], plan
    (row,) = [e for e in plan if str(e.get("warning", "")).startswith("DRIFT: ")]
    assert row["path"] == "semantic/tables/contacts.yaml" and row["warning"] == warning
    # And the answers that read the table say it on their grain line.
    sql = client.post(
        "/v1/sql",
        headers=headers,
        json={"lens": "crm_lens", "sql": "SELECT count(*) AS n FROM contacts"},
    )
    assert "its declared grain no longer holds" in sql.json()["grain"][0], sql.json()

    # The same false grain, introduced by this push (the blueprint changed), aborts.
    changed = dict(moved)
    changed["semantic/tables/contacts.yaml"] = CONTACTS_YAML.replace(
        "grain: one row per contact per sync", "grain: one row per contact for each sync"
    )
    out = _apply(headers, changed)
    assert out[-1]["action"] == "aborted", out
    assert next(r for r in out if r.get("scope") == "tables")["action"] == "rejected"
