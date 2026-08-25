"""Derived bindings: which shared assets a certified answer's SQL touches.

Never authored — computed at apply/certify time by sqlglot extraction against
the lens's compiled model, and stored on the row as ``{asset_key: content_hash}``
using the SAME hashes the lens's SharedProvenance carries, so certified
staleness and lens staleness can never disagree. Two extraction rules:
entities bind at TABLE level (column-level is not attempted; CTE names are not
source tables; aliases resolve to the real table), and shared DEFINITIONS bind
by sql_expr containment — the SQL canonically embeds the definition's
expression (the stale-sample idiom), so a changed governed definition flags
(and re-tests) exactly the answers that implement it. A binding MISS
means a re-verify flag doesn't fire — never a wrong answer (the sql_guard +
read-only credential stay the execution boundary).
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from services.contracts.semantic_model import SemanticModel


def source_tables(sql: str, dialect: str) -> set[str]:
    """The physical tables a query reads, lowercased, qualified as written
    (``db.schema.table`` when the SQL qualifies, bare otherwise). A CTE name
    shadows only UNQUALIFIED references — a schema-qualified reference always
    names the physical table (a CTE can never be qualified), so it always
    counts: hiding a qualified table behind a same-named CTE must not blind
    the boundary gate. Raises on unparseable SQL."""
    tables: set[str] = set()
    for stmt in sqlglot.parse(sql, read=dialect):
        if stmt is None:
            continue
        cte_names = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}
        for tbl in stmt.find_all(exp.Table):
            if not tbl.name:
                continue
            qualified = bool(tbl.db or tbl.catalog)
            if not qualified and tbl.name.lower() in cte_names:
                continue
            tables.add(".".join(p.lower() for p in (tbl.catalog, tbl.db, tbl.name) if p))
    return tables


def _ref_matches(ref: str, table: str) -> bool:
    """Does one referenced table name denote the modeled ``table``? One name
    denotes the other when its segments are a TAIL of the other's: exact match,
    a bare side against the other's last segment (BI SQL saying ``customers``
    against a model's ``shop.customers``, and vice versa), and a
    connection-relative name against the compiled catalog-qualified one
    (``marts.orders`` is ``acme-prod.marts.orders`` — the compiler stamps the
    catalog the connection pins, so the shorter authored form still binds).

    Two DIFFERENT qualifications still never match, which is the property the
    read allow-list rests on: ``hr.orders`` is not ``analytics.orders``, and
    ``other-project.marts.orders`` is not ``acme-prod.marts.orders``. Only a
    name that omits leading qualifiers matches — never one that contradicts
    them."""
    a = ref.lower().split(".")
    b = table.lower().split(".")
    n = min(len(a), len(b))
    return a[-n:] == b[-n:]


def _matches(referenced: set[str], table: str) -> bool:
    """A modeled table is touched when any referenced name denotes it. When two
    entities share a bare table name, a bare reference binds both — over-flagging
    re-verify is accepted; the gate below is what must never over-trust."""
    return any(_ref_matches(t, table) for t in referenced)


def foreign_tables(sql: str, model: SemanticModel) -> list[str]:
    """Referenced source tables no entity models — the lens-boundary check for
    certified SQL (a certified answer must not smuggle ungoverned tables past
    the lens). Qualification-aware: a qualified reference matches only its own
    qualification (or a model table with none); shadowing a qualified table
    with a CTE does not hide it. Raises on unparseable SQL — the shape gate
    runs first."""
    modeled = [e.source.table for e in model.entities]
    return sorted(
        t for t in source_tables(sql, model.dialect) if not any(_ref_matches(t, m) for m in modeled)
    )


def _canon(fragment: str, dialect: str) -> str | None:
    """Qualifier-stripped canonical SQL text (the stale-sample idiom): definition
    exprs are qualified (``customers.number_of_orders``) while certified SQL is
    often bare — containment must match across that difference. None when the
    fragment can't be parsed (nothing to compare)."""
    try:
        node = sqlglot.parse_one(fragment, read=dialect)
        for col in node.find_all(exp.Column):
            col.set("table", None)
        return node.sql(dialect=dialect).lower()
    except Exception:  # noqa: BLE001 — unparseable fragments can't be compared
        return None


def certified_bindings(sql: str, model: SemanticModel) -> dict[str, str]:
    """``{asset_key: content_hash}`` for the shared assets the SQL touches:
    entities whose source table it reads, plus shared definitions whose sql_expr
    it canonically embeds (the answer IMPLEMENTS that definition — a change to
    it is exactly what must re-test the answer). Hashes read from the model's
    own shared_provenance. Empty for a model without provenance (nothing to be
    stale against) or unparseable SQL (the apply gate rejects those before they
    land; pre-gate rows just never flag)."""
    provenance = model.shared_provenance.assets if model.shared_provenance else {}
    if not provenance:
        return {}
    try:
        referenced = source_tables(sql, model.dialect)
    except Exception:  # noqa: BLE001 — a miss flags nothing; it must never sink an apply
        return {}
    out = {
        key: provenance[key]
        for e in model.entities
        if (key := f"entity/{e.name}") in provenance and _matches(referenced, e.source.table)
    }
    canon_sql = _canon(sql, model.dialect)
    if canon_sql:
        for d in model.definitions:
            key = f"definition/{d.term}"
            expr = (d.sql_expr or "").strip()
            if key not in provenance or not expr:
                continue
            frag = _canon(expr, model.dialect)
            if frag and frag in canon_sql:
                out[key] = provenance[key]
    return out


def restamp_bindings(
    sql: str, stored: dict[str, str] | None, model: SemanticModel
) -> dict[str, str]:
    """The GREEN-test re-stamp: membership may GROW, never shrink. Recomputing
    membership outright would let one false-negative pass sever a binding
    permanently — a definition whose expr moved away from the certified SQL's
    text no longer canonically embeds, so its key would drop and staleness
    would go dark for exactly the pair that diverged. So: every stored key
    stays (its hash refreshed from provenance; an asset that left the
    provenance keeps the stored hash — permanently stale, loudly, until a
    human re-certifies or retires), while newly-detected keys from the fresh
    compute join (more staleness sensitivity is always safe, and it heals an
    under-captured first stamp). Rows with no bindings yet backfill whole."""
    fresh = certified_bindings(sql, model)
    if not stored:
        return fresh
    provenance = model.shared_provenance.assets if model.shared_provenance else {}
    for key, old in stored.items():
        if key not in fresh:
            fresh[key] = provenance.get(key, old)
    return fresh
