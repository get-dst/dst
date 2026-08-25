"""Reference resolver — every SQL fragment in a compiled model must resolve.

dbt's ``ref()`` doctrine applied to the semantic layer: entity names
are THE logical namespace, and an entity's members (fields ∪ dimensions ∪
metrics) are its interface. Every fragment (metric.expr, metric.filters,
dimension.expr, definition.sql_expr, join.on — plus primary_key /
default_time_field / agg_time_field member names and ratio/derived metric
refs) must resolve against declared structure or the compile ERRORS, naming
the fragment, the unknown ref, and the candidates. Table-qualified references
(authors pasting from BI SQL) are accepted and REWRITTEN to entity form —
kindness in, canonical out; the rewrite lands in the compiled bundle only.
Unqualified columns resolve only in single-entity models. Pure: model in,
(model, errors, warnings) out — the caller decides whether errors raise
(compile) or report (validate).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

# The bare-vs-qualified table matching convention is the certified-bindings one:
# a bare name denotes a qualified one's last segment, two different
# qualifications never match.
from services.certify.bindings import _ref_matches
from services.contracts.semantic_model import Entity, SemanticModel
from services.runtime.identifiers import is_reserved

# Derived-metric sibling refs ("{revenue} - {cost}") — the compiler's grammar.
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
# Sentinel identifier a placeholder becomes so the fragment parses as SQL.
_SENTINEL_PREFIX = "__kph_"
_SENTINEL = re.compile(r"__kph_(\w+)__")


@dataclass
class _Ctx:
    dialect: str
    entities: list[Entity]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.by_name: dict[str, Entity] = {e.name.lower(): e for e in self.entities}
        self.members: dict[str, dict[str, str]] = {
            e.name.lower(): {
                m.lower(): m
                for m in [f.name for f in e.fields]
                + [d.name for d in e.dimensions]
                + [mt.name for mt in e.metrics]
            }
            for e in self.entities
        }

    @property
    def entity_names(self) -> str:
        return ", ".join(sorted(e.name for e in self.entities))

    def error(self, msg: str) -> None:
        if msg not in self.errors:
            self.errors.append(msg)


def _no_member(label: str, ref: str, entity: Entity, name: str, ctx: _Ctx) -> None:
    members = ", ".join(sorted(ctx.members[entity.name.lower()].values()))
    ctx.error(
        f"{label}: '{ref}' — no member '{name}' on entity '{entity.name}'; "
        f"members of {entity.name}: {members}"
    )


def _check_member_name(label: str, entity: Entity, name: str, ctx: _Ctx) -> None:
    if name.lower() not in ctx.members[entity.name.lower()]:
        _no_member(label, name, entity, name, ctx)


def _metric_names(entity: Entity) -> str:
    return ", ".join(sorted(m.name for m in entity.metrics))


def _resolve_column(col: exp.Column, label: str, ctx: _Ctx) -> bool:
    """Resolve one column ref; True when the node was rewritten in place.

    Scope-aware for subqueries: a ref bound by a subquery's OWN relations —
    its alias or bare table — is that scope's SQL, not an entity ref, and
    resolves there; a bare column inside a subquery
    with its own FROM belongs to that FROM. The resolver used to reject both
    ('no entity ‹alias›' / 'unqualified'), which left the unaliased —
    silently-correlating — form as the only accepted spelling."""
    qualifier = [p for p in (col.catalog, col.db, col.table) if p]
    name = col.name
    enclosing = _ancestor_selects(col)
    if not qualifier:
        if name.startswith(_SENTINEL_PREFIX):
            return False  # a derived-metric placeholder, checked separately
        if any(_select_relations(s) for s in enclosing):
            return False  # the subquery's own FROM owns its bare columns
        if len(ctx.entities) == 1:
            entity = ctx.entities[0]
            if name.lower() not in ctx.members[entity.name.lower()]:
                _no_member(label, name, entity, name, ctx)
            return False
        owners = [e.name for e in ctx.entities if name.lower() in ctx.members[e.name.lower()]]
        if owners:
            candidates = ", ".join(f"{o}.{name}" for o in sorted(owners))
            ctx.error(
                f"{label}: column '{name}' is unqualified — qualify it; candidates: {candidates}"
            )
        else:
            ctx.error(
                f"{label}: column '{name}' is unqualified — qualify it as <entity>.{name}; "
                f"entities in this lens: {ctx.entity_names}"
            )
        return False
    ref = ".".join(qualifier)
    written = f"{ref}.{name}"
    if len(qualifier) == 1 and any(ref.lower() in _select_binds(s) for s in enclosing):
        return False  # a subquery-local alias/table — that scope's SQL, not an entity ref
    named = ctx.by_name.get(ref.lower()) if len(qualifier) == 1 else None
    if named is not None:
        if name.lower() not in ctx.members[named.name.lower()]:
            _no_member(label, written, named, name, ctx)
        return False
    # Not an entity name: a table-qualified ref (BI SQL) — rewrite to entity form.
    sources = [e for e in ctx.entities if _ref_matches(ref.lower(), e.source.table)]
    if len(sources) > 1:
        names = ", ".join(sorted(e.name for e in sources))
        ctx.error(
            f"{label}: '{written}' — table '{ref}' is the source of entities {names}; "
            "qualify by entity name"
        )
        return False
    if not sources:
        ctx.error(
            f"{label}: '{written}' — no entity '{ref}'; entities in this lens: {ctx.entity_names}"
        )
        return False
    entity = sources[0]
    if name.lower() not in ctx.members[entity.name.lower()]:
        _no_member(label, written, entity, name, ctx)
        return False
    # Delimited when the entity name is a keyword: this rewrite lands inside an
    # AUTHORED expression that ships straight into generated SQL, so an entity
    # named `order` would put `order.amount` in front of the warehouse.
    col.set(
        "table",
        exp.to_identifier(entity.name, quoted=True)
        if is_reserved(entity.name, ctx.dialect)
        else exp.to_identifier(entity.name),
    )
    col.set("db", None)
    col.set("catalog", None)
    ctx.warnings.append(
        f"{label}: '{written}' rewritten to '{entity.name}.{name}' (source table → entity name)"
    )
    return True


def _select_relations(select: exp.Select) -> list[exp.Expression]:
    """A subquery's own FROM + JOIN relations. The FROM arg key is version-fluid
    in sqlglot ("from_" since 30.x) — read both spellings."""
    relations: list[exp.Expression] = []
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is not None:
        relations.append(from_.this)
    for join in select.args.get("joins") or []:
        relations.append(join.this)
    return relations


def _select_binds(select: exp.Select) -> set[str]:
    """The names a subquery's own FROM/JOIN relations bind, lowercased.

    An alias binds itself. A bare single-part table binds its own name. A
    MULTI-part path with no alias binds NOTHING here — on BigQuery a quoted
    path gets no implicit last-component alias, which is the whole trap this
    guard exists for."""
    bound: set[str] = set()
    for rel in _select_relations(select):
        alias = getattr(rel, "alias", "")
        if alias:
            bound.add(alias.lower())
        elif isinstance(rel, exp.Table) and not (rel.db or rel.catalog):
            bound.add(rel.name.lower())
    return bound


def _self_alias_subqueries(node: exp.Expression, label: str, ctx: _Ctx) -> bool:
    """Break silent outer-correlation in filter subqueries; True when rewritten.

    The one filter shape the resolver accepts without an explicit alias —
    ``entity.col = (SELECT MAX(entity.col) FROM `p.d.entity_table`)`` — is
    semantically wrong on BigQuery: the backticked path binds nothing, so the
    inner ``entity.col`` correlates to the OUTER table and the pin degenerates
    to always-true (a "current" metric silently sums every daily snapshot
    instead of the latest). When the unaliased table IS the entity's own source,
    the author plainly meant the inner table: self-alias it to the entity name
    and say so. Any other unbound entity ref inside a subquery is legitimate
    outer correlation and is left alone."""
    rewrote = False
    for col in node.find_all(exp.Column):
        qualifier = [p for p in (col.catalog, col.db, col.table) if p]
        if len(qualifier) != 1:
            continue
        entity = ctx.by_name.get(qualifier[0].lower())
        if entity is None:
            continue
        selects = _ancestor_selects(col)
        if not selects or any(entity.name.lower() in _select_binds(s) for s in selects):
            continue  # not in a subquery, or the name is bound where it stands
        for select in selects:  # innermost first — alias the closest matching table
            table = _unaliased_source_table(select, entity)
            if table is None:
                continue
            table.set(
                "alias",
                exp.TableAlias(
                    this=exp.to_identifier(
                        entity.name, quoted=is_reserved(entity.name, ctx.dialect)
                    )
                ),
            )
            ctx.warnings.append(
                f"{label}: subquery table '{table.sql(dialect=ctx.dialect)}' self-aliased — "
                f"an unaliased qualified path does not bind '{entity.name}' inside the "
                f"subquery, so '{entity.name}.{col.name}' would have correlated to the "
                "outer query (an always-true filter)"
            )
            rewrote = True
            break
    return rewrote


def _ancestor_selects(node: exp.Expression) -> list[exp.Select]:
    """Enclosing SELECTs, innermost first — a fragment's root is a predicate,
    so every one of these is a subquery scope."""
    out: list[exp.Select] = []
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.Select):
            out.append(parent)
        parent = parent.parent
    return out


def _unaliased_source_table(select: exp.Select, entity: Entity) -> exp.Table | None:
    """This subquery's unaliased multi-part FROM/JOIN table matching *entity*'s
    source — the one safe to self-alias — or None."""
    for rel in _select_relations(select):
        if not isinstance(rel, exp.Table) or rel.alias or not (rel.db or rel.catalog):
            continue
        path = ".".join(p for p in (rel.catalog, rel.db, rel.name) if p)
        if _ref_matches(path.lower(), entity.source.table):
            return rel
    return None


def _resolve_fragment(frag: str, label: str, ctx: _Ctx, *, placeholders: bool = False) -> str:
    """Resolve every column ref in one SQL fragment; returns the fragment,
    rewritten when a source-table qualifier was translated to entity form.
    An unparseable fragment is returned untouched — parse validity is
    validate's own check (metric_filter_invalid), not resolution."""
    text = _PLACEHOLDER.sub(rf"{_SENTINEL_PREFIX}\1__", frag) if placeholders else frag
    try:
        node = sqlglot.parse_one(text, read=ctx.dialect)
    except Exception:  # noqa: BLE001 — unparseable fragments are reported elsewhere
        return frag
    if not isinstance(node, exp.Expression):
        return frag
    before = len(ctx.errors)
    rewrote = _self_alias_subqueries(node, label, ctx)
    for col in node.find_all(exp.Column):
        rewrote = _resolve_column(col, label, ctx) or rewrote
    if len(ctx.errors) > before or not rewrote:
        return frag
    out = node.sql(dialect=ctx.dialect)
    return _SENTINEL.sub(r"{\1}", out) if placeholders else out


def resolve_model(model: SemanticModel) -> tuple[SemanticModel, list[str], list[str]]:
    """The resolver pass: ``(model_with_rewrites, errors, warnings)``.

    Errors mean unknown references (or an entity-name/source-table collision);
    warnings mean table-qualified refs rewritten to entity form. The input
    model is never mutated — rewrites land on the returned copy only."""
    if not model.entities:
        return model, [], []  # nothing to resolve against (validate flags no_entities)
    out = model.model_copy(deep=True)
    ctx = _Ctx(dialect=model.dialect, entities=out.entities)

    # Spec risk 2: an entity name equal to a DIFFERENT entity's source table
    # would make `<name>.<col>` ambiguous — error, never silently shadow.
    for a in out.entities:
        for b in out.entities:
            if a is not b and _ref_matches(a.name.lower(), b.source.table):
                ctx.error(
                    f"entity '{a.name}' collides with the source table of entity "
                    f"'{b.name}' ('{b.source.table}') — entity names are the logical "
                    f"namespace; rename entity '{a.name}'"
                )

    for e in out.entities:
        for pk in e.primary_key:
            _check_member_name(f"entity {e.name} primary_key", e, pk, ctx)
        if e.default_time_field:
            _check_member_name(f"entity {e.name} default_time_field", e, e.default_time_field, ctx)
        for d in e.dimensions:
            if d.expr:
                d.expr = _resolve_fragment(d.expr, f"dimension {d.name}", ctx)
        for m in e.metrics:
            if m.agg_time_field:
                _check_member_name(f"metric {m.name} agg_time_field", e, m.agg_time_field, ctx)
            m.filters = [_resolve_fragment(f, f"metric {m.name} filter", ctx) for f in m.filters]
            if m.type == "simple" and m.expr:
                m.expr = _resolve_fragment(m.expr, f"metric {m.name}", ctx)
            elif m.type == "ratio":
                metric_names = {mt.name for mt in e.metrics}
                for side, ref in (("numerator", m.numerator), ("denominator", m.denominator)):
                    if ref and ref not in metric_names:
                        ctx.error(
                            f"metric {m.name}: {side} '{ref}' is not a metric on entity "
                            f"'{e.name}'; metrics of {e.name}: {_metric_names(e)}"
                        )
            elif m.type == "derived" and m.expr:
                metric_names = {mt.name for mt in e.metrics}
                for ref in _PLACEHOLDER.findall(m.expr):
                    if ref not in metric_names:
                        ctx.error(
                            f"metric {m.name}: '{{{ref}}}' references no metric on entity "
                            f"'{e.name}'; metrics of {e.name}: {_metric_names(e)}"
                        )
                m.expr = _resolve_fragment(m.expr, f"metric {m.name}", ctx, placeholders=True)
    for j in out.joins:
        j.on = _resolve_fragment(j.on, f"join {j.left} -> {j.right}", ctx)
    for defn in out.definitions:
        if (defn.sql_expr or "").strip():
            defn.sql_expr = _resolve_fragment(defn.sql_expr or "", f"definition {defn.term}", ctx)
    return out, ctx.errors, ctx.warnings
