"""Pre-deploy validation: deterministic checks + scope probes over a lens bundle.

Checks confirm the lens is internally consistent (connections, fields, definitions,
access) and drift-free; probes confirm its sample queries stay within the semantic
model's table/column scope (via sql_guard).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel

from services.contracts.semantic_model import Definition, Entity, Metric, SemanticModel
from services.definitions import drift
from services.definitions.standards import OrgStandard
from services.lenses.store import LensBundle
from services.llm import registry
from services.runtime import sql_guard

Severity = Literal["error", "warning"]


class Issue(BaseModel):
    severity: Severity
    code: str
    message: str
    # The part of ``message`` that varies between issues of this code — the term,
    # the entity, the term plus whatever detail the wording carries. Set it and a
    # class of repeats can collapse to one counted line without losing anything
    # (``collapse_warnings``); leave it None and the message always prints whole.
    subject: str | None = None


class ProbeResult(BaseModel):
    question: str
    ok: bool
    reason: str | None = None


class ValidationReport(BaseModel):
    ok: bool  # no errors (warnings allowed)
    issues: list[Issue]
    probes: list[ProbeResult]
    conflicts: list[drift.DriftConflict]


# One line per class, restating what the per-issue wording says, with {n} and the
# {subjects} that carry every issue's varying part. A code absent from here never
# collapses — its message prints whole, however many times it fires.
# The decorative-constraint lint — the class mechanism behind the whole
# prose-governance family. A rule written as prose ("never sum money columns
# across currencies", in an entity description) is honoured for one phrasing and
# violated for the next, and the violation is served with no disclosure; the same
# rule as a structured population_filter holds even under adversarial pressure.
# The mechanism that works is the one implemented in code — so prose that TRIES
# to be a rule gets told, at apply, that it is decorative and which structured
# field enforces it. Imperative + a query verb, conservatively: benign prose
# ("customers never churn twice") must not fire.
_IMPERATIVE_PROSE = re.compile(
    r"\b(never|do not|don'?t|must not|must always|always|only ever)\b"
    r"[^.;\n]{0,60}?\b(sum|aggregat\w*|join|quer\w*|filter\w*|group|mix|combin\w*"
    r"|use[d]? for|analysis|analytics)",
    re.IGNORECASE,
)


def _lint_imperative_prose(e: Entity, issues: list[Issue]) -> None:
    surfaces = [("description", e.description or ""), *(("use_cases", u) for u in e.use_cases)]
    for where, text_ in surfaces:
        m = _IMPERATIVE_PROSE.search(text_)
        if m is None:
            continue
        issues.append(
            Issue(
                severity="warning",
                code="constraint_in_prose",
                message=(
                    f"entity '{e.name}' states a rule in {where} prose ('{m.group(0)}…') — "
                    "prose steers generation NON-deterministically (obeyed for "
                    "one phrasing, violated for the next); encode it structurally — "
                    "population_filter for a scope bound, pinned_dimensions for "
                    "must-pin/group dimensions, metric filters for metric meaning — or "
                    "accept that it is advisory"
                ),
                subject=e.name,
            )
        )
        return  # one line per entity — the fix is the same whichever sentence fired


def _lint_twin_metric_filters(e: Entity, dialect: str | None, issues: list[Issue]) -> None:
    """Metrics sharing one canonical aggregation with DIFFERING mandatory
    filters: the standard snapshot pattern (current_* pins the
    latest date, total_* pins end-of-month) over one column. Legal and useful —
    but the serve-time guard can only tell the twins apart through the
    question's wording, and a question that names neither is rejected. Warn at
    apply so the author knows the resolution contract, instead of learning it
    from a rejection three cuts later."""
    import sqlglot as _sqlglot

    from services.runtime.compiler import metric_sql

    groups: dict[str, list[Metric]] = {}
    for metric in e.metrics:
        if metric.type != "simple":
            continue
        try:
            canon = _sqlglot.parse_one(
                metric_sql(metric, e, inline_filters=False), read=dialect
            ).sql(dialect=dialect)
        except Exception:  # noqa: BLE001 — a malformed metric fails elsewhere
            continue
        groups.setdefault(canon, []).append(metric)
    for _canon_text, twins in groups.items():
        filter_sets = {tuple(sorted(m.filters)) for m in twins}
        if len(twins) < 2 or len(filter_sets) < 2:
            continue
        names = ", ".join(f"'{m.name}'" for m in twins)
        issues.append(
            Issue(
                severity="warning",
                code="twin_metric_filters",
                message=(
                    f"entity '{e.name}': metrics {names} govern the same expression with "
                    "different mandatory filters — a question resolves to ONE of them by "
                    "its wording (e.g. 'current …' vs 'total …'); a question naming "
                    "neither is rejected with the conflict spelled out. Make the metric "
                    "names distinct words users actually say, or give the metrics "
                    "distinct expressions"
                ),
                subject=e.name,
            )
        )
        return  # one warning per entity names the class; the fix is the same


_COLLAPSED: dict[str, str] = {
    "definition_not_enforceable": (
        "{n} definitions have no `sql:` expression in their frontmatter (prose-only — "
        "they guide prose answers but can't be enforced or verified in generated SQL; "
        "add `sql:` to make definition_applied checkable): {subjects}"
    ),
    "definition_about_dangling": (
        "{n} definitions are about a member that is not in this lens's compiled model: {subjects}"
    ),
    "definition_double_truth": (
        "{n} definitions carry both sql_expr and about — enforceable SQL belongs on "
        "the entity; keep the about binding and move the SQL into a "
        "metric/dimension: {subjects}"
    ),
    "entity_no_fields": "{n} entities have no modeled fields: {subjects}",
    "definition_drift": "{n} definitions differ from a standard or another lens: {subjects}",
}


# `[[term]]` cross-references authored into definition bodies.
WIKI_LINK = re.compile(r"\[\[([^\]]+)\]\]")


def _fold(term: str) -> str:
    """Citations are written as prose (`[[month-end balance]]`) against terms
    authored either spaced or snake_cased — compare them on one form."""
    return re.sub(r"[\s_-]+", " ", term).strip().lower()


# Heads that carry no business meaning on their own: `order_count` and
# `customer_count` are not competing claims, they are two counts. `revenue`,
# `margin`, `churn` are.
_GENERIC_HEADS = frozenset(
    {"count", "total", "amount", "sum", "avg", "average", "rate", "ratio", "pct", "percent", "n"}
)


def _head(name: str) -> str:
    return _fold(name).split(" ")[-1] if name else ""


def _competing_metrics(sm: SemanticModel) -> list[Issue]:
    """Two metrics that answer to one business word — the defect that lets two
    people ask the same question and get answers far apart, both marked verified.

    The shape: `definitions/revenue.md` governs `net_revenue` (captured payments
    minus refunds) while `order_items.total_revenue` sits ungoverned, its own
    description calling it *"the canonical revenue figure"* (gross line totals).
    Both are `verified`; the question's wording picks between them — "total
    revenue" matches the identifier `total_revenue`. Nothing warns, at authoring
    time or at answer time, and neither certification nor `dst test` can see it:
    selection is stable per phrasing, so an eval case on any one wording passes
    forever.

    Two checks, deliberately different severities:

    * **Same name, twice → error.** dbt refuses two models with one name; a lens
      accepted two metrics called `total_revenue` in silence. Unambiguous, so it
      blocks.
    * **Same head noun, one governed → warning.** A heuristic, so it advises. It
      names both and points at `status: ambiguous` + `possible_mappings`, which
      already exist — the machinery for "ask which reading is meant" was there
      all along, and nothing detected when it was needed.
    """
    issues: list[Issue] = []
    metrics = [(m.name, e.name) for e in sm.entities for m in e.metrics]

    by_name: dict[str, list[str]] = {}
    for name, entity in metrics:
        by_name.setdefault(_fold(name), []).append(entity)
    for name, owners in sorted(by_name.items()):
        if len(owners) > 1:
            issues.append(
                Issue(
                    severity="error",
                    code="duplicate_metric",
                    message=(
                        f"metric '{name}' is defined on more than one entity "
                        f"({', '.join(sorted(owners))}) — a question naming it cannot "
                        f"resolve to one meaning; rename one or model it once"
                    ),
                    subject=name,
                )
            )

    # A claimant is anything that answers to the word: a metric name OR a governed
    # term. Counting only metrics misses the case this check exists for — in the
    # example above `net_revenue` is a DEFINITION and `total_revenue` a metric, and
    # one metric alone never trips a "two claimants" test. A fixture built to prove
    # the check would not have caught that; a real layer does.
    governed = {_fold(d.term) for d in sm.definitions}
    metric_names = {_fold(n) for n, _ in metrics}
    for head in sorted({_head(n) for n in metric_names | governed} - _GENERIC_HEADS - {""}):
        claimants = sorted({n for n in metric_names | governed if _head(n) == head})
        if len(claimants) < 2:
            continue
        blessed = [c for c in claimants if c in governed]
        if not blessed:
            continue  # nobody governs this word: an authoring gap, not a conflict
        loose = [c for c in claimants if c not in governed]
        if not loose:
            continue  # every claimant is governed — the author has ruled
        issues.append(
            Issue(
                severity="warning",
                code="competing_metric_claim",
                message=(
                    f"'{head}' is claimed by {', '.join(claimants)}: "
                    f"{', '.join(blessed)} governed by a definition, "
                    f"{', '.join(loose)} not. A question asking for '{head}' picks one "
                    f"silently and two people can get different numbers — govern the "
                    f"others, or mark the term `status: ambiguous` with "
                    f"`possible_mappings` so dst asks which is meant"
                ),
                subject=head,
            )
        )
    return issues


def collapse_warnings(issues: Sequence[Issue]) -> list[str]:
    """Warning messages with same-class repeats folded into one counted line.

    An apply emits one identically-shaped line per prose-only definition — a
    dozen or more of them on a real layer — and a reader who learns to skip the
    whole warnings block skips the one warning that matters.
    Two or more issues of a collapsible code become a single line naming the
    count and every subject; a class with one member keeps its own full message,
    which is always the better wording. Nothing is dropped: the collapsed line
    restates the class explanation and lists each subject with its detail.
    Order is first-appearance, so a collapsed class sits where its first member
    was.
    """
    subjects: dict[str, list[str]] = {}
    for issue in issues:
        if issue.code in _COLLAPSED and issue.subject:
            subjects.setdefault(issue.code, []).append(issue.subject)
    collapsed = {code for code, subs in subjects.items() if len(subs) > 1}
    out: list[str] = []
    emitted: set[str] = set()
    for issue in issues:
        if issue.code not in collapsed:
            out.append(issue.message)
            continue
        if issue.code in emitted:
            continue
        emitted.add(issue.code)
        subs = subjects[issue.code]
        out.append(_COLLAPSED[issue.code].format(n=len(subs), subjects=", ".join(subs)))
    return out


def definition_double_truth(d: Definition, entities: Sequence[Entity]) -> Issue | None:
    """ONE rule, ONE wording for both lint paths — compiled bundles below and
    the shared-asset lint at apply (R11: a definition no lens selects never
    reaches a compiled model). sql_expr + about are two INDEPENDENT enforceable
    claims that can silently drift apart (the double-truth smell). Exempt:
    about pointing at a metric — there the metric owns the SQL and the
    definition mirrors it by construction (the dbt-import pattern)."""
    if not (d.sql_expr or "").strip() or not d.about:
        return None
    entity_name, _, member = d.about.partition(".")
    entity = next((e for e in entities if e.name == entity_name), None)
    if entity is not None and member and any(m.name == member for m in entity.metrics):
        return None
    return Issue(
        severity="warning",
        code="definition_double_truth",
        message=f"definition '{d.term}' carries both sql_expr and about "
        f"('{d.about}') — enforceable SQL belongs on the entity; keep the "
        "about binding and move the SQL into a metric/dimension",
        subject=f"{d.term} (about '{d.about}')",
    )


def _check_bundle(
    bundle: LensBundle,
    standards: list[OrgStandard],
    other_lenses: list[tuple[str, list[Definition]]],
) -> tuple[list[Issue], list[drift.DriftConflict]]:
    issues: list[Issue] = []
    sm = bundle.semantic_model
    allowed_connections = set(bundle.config.connections)

    # A lens that publishes green and then 503s on every question is the worst
    # failure shape here: authoring looks finished and nothing answers. Publish
    # is the gate — if the configured providers cannot serve this lens's model,
    # the lens does not go live.
    # Only when NOTHING is configured is it a warning — that install is
    # uniformly unable to answer (a keyless CI apply is a real workflow), and
    # /ready already says so; the silent case is the one where providers ARE
    # configured and this lens is the odd one out.
    model_ref = bundle.config.model.model_ref()
    detail = registry.unservable_detail(model_ref)
    if detail is not None:
        issues.append(
            Issue(
                severity="warning" if not registry.specs() else "error",
                code="lens_model_unservable",
                message=f"this lens cannot answer here: {detail}",
            )
        )

    if not sm.entities:
        issues.append(Issue(severity="error", code="no_entities", message="lens has no entities"))

    for e in sm.entities:
        _lint_imperative_prose(e, issues)
        _lint_twin_metric_filters(e, sm.dialect, issues)
        if e.source.connection not in allowed_connections:
            issues.append(
                Issue(
                    severity="error",
                    code="entity_connection_unlisted",
                    message=f"entity '{e.name}' uses connection '{e.source.connection}' "
                    f"not in lens connections {sorted(allowed_connections)}",
                )
            )
        if not e.fields:
            issues.append(
                Issue(
                    severity="warning",
                    code="entity_no_fields",
                    message=f"entity '{e.name}' has no modeled fields",
                    subject=e.name,
                )
            )

    issues.extend(_competing_metrics(sm))

    if sm.timezone:
        # An invalid zone must die at plan, not at serve: generation would render
        # the bad string into every prompt, and "Europe/Olso" fails nothing
        # loudly — the model just improvises a clock.
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(sm.timezone)
        except Exception:  # noqa: BLE001 — any failure to resolve is the same finding
            issues.append(
                Issue(
                    severity="error",
                    code="invalid_timezone",
                    message=(
                        f"timezone '{sm.timezone}' is not a resolvable IANA zone "
                        f"(expected e.g. Europe/Oslo, UTC, America/New_York)"
                    ),
                    subject=sm.timezone,
                )
            )

    if sm.stale_after_days is not None and sm.stale_after_days < 1:
        # Same rule as the zone: a nonsense freshness contract must die at plan.
        # Zero or negative would make every serve stale (or the check vacuous) —
        # a contract that can never pass is a typo, not a policy.
        issues.append(
            Issue(
                severity="error",
                code="invalid_stale_after",
                message=(
                    f"stale_after_days must be a positive number of days, got {sm.stale_after_days}"
                ),
                subject=str(sm.stale_after_days),
            )
        )

    seen: set[str] = set()
    cited_ok = {_fold(d.term) for d in sm.definitions}
    for d in sm.definitions:
        if d.term in seen:
            issues.append(
                Issue(
                    severity="error",
                    code="duplicate_definition",
                    message=f"definition '{d.term}' is defined more than once",
                )
            )
        seen.add(d.term)
        if d.about:
            entity_name, _, member = d.about.partition(".")
            target = next((e for e in sm.entities if e.name == entity_name), None)
            members = (
                {f.name for f in target.fields}
                | {dim.name for dim in target.dimensions}
                | {mt.name for mt in target.metrics}
                if target
                else set()
            )
            if target is None or (member and member not in members):
                issues.append(
                    Issue(
                        severity="warning",
                        code="definition_about_dangling",
                        message=f"definition '{d.term}' is about '{d.about}', which is "
                        "not in this lens's compiled model",
                        subject=f"{d.term} (about '{d.about}')",
                    )
                )
        # `[[term]]` prose cross-references are authored (running balance cites
        # [[net transaction amount]]) but nothing checked they land INSIDE the lens.
        # A lens can select a definition whose citation it never carries — the model
        # is told to consult a rule this lens will never show it — and without this
        # check a lens with a dangling citation applies clean.
        for ref in WIKI_LINK.findall(d.body):
            if _fold(ref) not in cited_ok:
                issues.append(
                    Issue(
                        severity="warning",
                        code="definition_cites_dangling",
                        message=f"definition '{d.term}' cites [[{ref}]], which this lens "
                        "does not carry — select it, or drop the citation",
                        subject=f"{d.term} (cites '{ref}')",
                    )
                )
        double_truth = definition_double_truth(d, sm.entities)
        if double_truth is not None:
            issues.append(double_truth)
        if not (d.sql_expr or "").strip() and d.status != "ambiguous":
            # Ambiguous glossary terms are exempt: refusing to compile to SQL is
            # their job, not a gap.
            issues.append(
                Issue(
                    severity="warning",
                    code="definition_not_enforceable",
                    # Name the key AUTHORS TYPE (`sql:` in the page frontmatter)
                    # — a warning naming only the internal field sent people
                    # grepping for a key their files don't use.
                    message=f"definition '{d.term}' has no `sql:` expression in its "
                    "frontmatter (alias sql_expr); it guides prose answers but can't "
                    "be enforced or verified in generated SQL — add one to make "
                    "definition_applied checkable",
                    subject=d.term,
                )
            )
        if d.status == "ambiguous" and not d.aliases:
            # DEAD GOVERNANCE: the clarify rail matches the question against the
            # term's surface forms, and with no aliases the only reachable form
            # is the identifier itself — which authors type while testing and
            # users never do, so the questions that should have prompted a
            # clarification are answered silently, at verified.
            # A declaration that reads as governance while behaving as
            # documentation must say so at apply, not in an incident review.
            issues.append(
                Issue(
                    severity="warning",
                    code="ambiguous_term_unreachable",
                    message=f"ambiguous term '{d.term}' has no aliases — the "
                    "clarification only triggers when a question contains "
                    f"'{d.term.replace('_', ' ')}' verbatim, which users asking in "
                    "business English never type; add `aliases:` (e.g. the phrasings "
                    "from its possible_mappings) so the rail is reachable",
                    subject=d.term,
                )
            )

    # Metric filters are compiled verbatim into WHERE clauses — they must at least
    # parse as boolean expressions in the lens dialect.
    import sqlglot

    from services.runtime.compiler import (
        CompileError,
        metric_entity_refs,
        metric_sql,
        unsafe_join_reason,
    )

    for entity in sm.entities:
        for metric in entity.metrics:
            for frag in metric.filters:
                try:
                    sqlglot.parse_one(f"SELECT 1 WHERE {frag}", read=sm.dialect)
                except Exception:
                    issues.append(
                        Issue(
                            severity="error",
                            code="metric_filter_invalid",
                            message=f"metric '{metric.name}' filter does not parse as "
                            f"{sm.dialect} SQL: {frag!r}",
                        )
                    )
            # A metric whose expr/filters name ANOTHER entity's column is only
            # computable if the compiler can put that entity in the FROM clause:
            # it joins over the declared path (e.g. `dollars_spent =
            # quantity * prices.price` across a declared many_to_one). What it
            # cannot do is invent a join, or take one that duplicates the base
            # rows and inflates the aggregate. Ask the compiler's own predicate,
            # so authoring-time and compile-time can never disagree, and reject
            # ONLY what it would refuse.
            for reached, refs in metric_entity_refs(metric, entity, sm).items():
                reason = unsafe_join_reason(sm, entity.name, reached)
                if reason is None:
                    continue
                issues.append(
                    Issue(
                        severity="error",
                        code="metric_expr_cross_entity",
                        message=f"metric '{metric.name}' on entity '{entity.name}' "
                        f"references {', '.join(refs)}, and this lens cannot join "
                        f"'{reached}' in to compute it: {reason}. Fix it by declaring (or "
                        f"correcting the `relationship:` of) that join on '{entity.name}', "
                        f"by moving the metric onto '{reached}', or by pre-joining the "
                        f"value into '{entity.name}' upstream.",
                    )
                )
            if metric.type == "simple":
                continue
            # Ratio/derived compile by expansion: sibling refs must resolve without
            # cycles, and the expanded SQL gets the same parse gate as filters.
            try:
                expanded = metric_sql(metric, entity)
            except CompileError as exc:
                issues.append(Issue(severity="error", code="metric_ref_invalid", message=str(exc)))
                continue
            try:
                sqlglot.parse_one(f"SELECT {expanded}", read=sm.dialect)
            except Exception:
                issues.append(
                    Issue(
                        severity="error",
                        code="metric_sql_invalid",
                        message=f"metric '{metric.name}' expands to SQL that does not "
                        f"parse as {sm.dialect}: {expanded!r}",
                    )
                )

    if not bundle.config.access.allow:
        issues.append(
            Issue(
                severity="warning",
                code="no_callers",
                message="no callers on the allow-list (admin-only until callers are added)",
            )
        )

    # A metric layer moves generation onto the intent tier, whose first pass
    # renders a leaner prompt — so some authored assets ride only the ESCALATION
    # prompt. That is a deliberate tiering (governance on that path is carried by
    # code, not prose: manual §6.3), but it was invisible from lens.yaml, and an
    # invisible condition on whether authored content reaches the model is the
    # bug that cost four agents their `dimensions:` work. Say it once, per lens,
    # naming only what THIS lens actually loses.
    from services.runtime.preview import escalation_only

    if dropped := escalation_only(sm):
        issues.append(
            Issue(
                severity="warning",
                code="intent_tier_escalation_only",
                message=f"this lens has metrics, so generation's first pass is the "
                f"metric-layer prompt — {', '.join(dropped)} reach the model only if that "
                f"pass fails and generation escalates to raw SQL. "
                f'`dst lens prompt {bundle.config.name} "<question>"` renders both.',
            )
        )

    # Reference-resolution parity: the same resolution the compile path enforces, so a
    # wizard-published bundle can't carry unknown refs either. validate is read-only —
    # a rewritable (table-qualified) ref is a warning here, never a mutation.
    from services.semantic.resolve import resolve_model

    _resolved, ref_errors, ref_warnings = resolve_model(sm)
    for msg in ref_errors:
        issues.append(Issue(severity="error", code="unresolved_reference", message=msg))
    for msg in ref_warnings:
        issues.append(Issue(severity="warning", code="reference_rewritten", message=msg))

    conflicts = drift.compare(sm.definitions, standards, other_lenses)
    for c in conflicts:
        issues.append(
            Issue(
                severity="warning",
                code="definition_drift",
                message=f"definition '{c.term}' differs from {c.source}",
                subject=f"{c.term} (vs {c.source})",
            )
        )
    return issues, conflicts


def _probe_sample_queries(bundle: LensBundle) -> list[ProbeResult]:
    """Confirm each sample query stays within scope (deterministic, no LLM)."""
    results: list[ProbeResult] = []
    for sq in bundle.semantic_model.sample_queries:
        guard = sql_guard.check(sq.sql, bundle.semantic_model)
        results.append(ProbeResult(question=sq.question, ok=guard.ok, reason=guard.reason))
    return results


def validate_bundle(
    bundle: LensBundle,
    standards: list[OrgStandard],
    other_lenses: list[tuple[str, list[Definition]]],
) -> ValidationReport:
    issues, conflicts = _check_bundle(bundle, standards, other_lenses)
    probes = _probe_sample_queries(bundle)
    for p in probes:
        if not p.ok:
            issues.append(
                Issue(
                    severity="error",
                    code="probe_out_of_scope",
                    message=f"sample query '{p.question}' failed scope check: {p.reason}",
                )
            )
    ok = not any(i.severity == "error" for i in issues)
    return ValidationReport(ok=ok, issues=issues, probes=probes, conflicts=conflicts)
