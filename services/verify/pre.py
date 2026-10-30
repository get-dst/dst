"""The checks that run on the Cube queries before anything executes.

One function per failure mode, run in ``PRE_ORDER``. Each reads the ask and
the queries and returns one ``Check``; a stop (clarify or refuse) ends the
ask there, and a rewrite (a value spelled the stored way, a window clipped
to the clock, a label grouped with its key, an ambiguous term served on its
default reading) hands the rewritten queries to the next check.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from typing import Any

from services.contracts.cube_query import CubeFilter, CubeQuery, TimeDimension
from services.contracts.failure_modes import FailureMode
from services.contracts.lens_model import OverlayDefinition
from services.cube.lens_view import CubeLensView, norm, toks
from services.runtime.not_for import names_ask
from services.runtime.pipeline import not_computable_refusal
from services.verify.base import (
    Ask,
    Check,
    Rows,
    all_filters,
    copy,
    flat_filters,
    fnum,
    passed,
    used,
)
from services.verify.periods import Period
from services.verify.periods import resolve as periods_of
from services.verify.post import partial_period
from services.verify.words import PERIOD, STOP, has_stem, phrase_in, stem

# Readings of an ambiguous term within this relative distance on every row are
# served on the declared default reading, the others disclosed with their values.
CLOSE_REL = 0.01

PreCheck = Callable[[Ask, list[CubeQuery]], Check]


def _members(qs: list[CubeQuery]) -> list[str]:
    out: list[str] = []
    for q in qs:
        out += [m for m in q.members() if m not in out]
    return out


# ── scope_violation ─────────────────────────────────────────────────────────


def scope_violation(ask: Ask, qs: list[CubeQuery]) -> Check:
    """A member the model declares but the lens's views do not hold."""
    name, mode = "scope", FailureMode.scope_violation
    outside: list[str] = []
    for m in _members(qs):
        if not ask.lens.declared(m):
            continue  # not_declared's to report
        inside = ask.lens.in_scope(m)
        if inside is None:
            return Check(name, mode, "skip", "the lens declares no views")
        if not inside:
            outside.append(m)
    if outside:
        return Check(
            name,
            mode,
            "refuse",
            f"{', '.join(outside)} {'is' if len(outside) == 1 else 'are'} outside this lens's "
            f"views ({', '.join(ask.lens.views)})",
        )
    return passed(name, mode)


# ── hidden_column ───────────────────────────────────────────────────────────


def hidden_column(ask: Ask, qs: list[CubeQuery]) -> Check:
    """A member hidden from every caller outside named groups, asked by a
    caller in none of them."""
    name, mode = "hidden", FailureMode.hidden_column
    groups = set(ask.groups)
    rules = ask.lens.hidden_rules()
    hit: list[str] = []
    for m in _members(qs):
        allowed = rules.get(ask.lens.base(m))
        if allowed is None:
            allowed = ask.lens.withheld_rule(m)
        if allowed is not None and not groups & set(allowed):
            hit.append(m)
    if hit:
        return Check(name, mode, "refuse", f"{', '.join(hit)} is withheld from this caller")
    return passed(name, mode)


# ── not_declared ────────────────────────────────────────────────────────────


def not_declared(ask: Ask, qs: list[CubeQuery]) -> Check:
    """A member the Cube model does not hold."""
    name, mode = "declared", FailureMode.not_declared
    missing = [m for m in _members(qs) if not ask.lens.declared(m)]
    if missing:
        return Check(
            name, mode, "refuse", f"the model holds no member {', '.join(sorted(missing))}"
        )
    return passed(name, mode)


# ── not_computable ──────────────────────────────────────────────────────────


def _nc_entries(ask: Ask) -> list[dict[str, Any]]:
    entries = [e.model_dump() for e in ask.config.not_computable] if ask.config else []
    lens = ask.config.name if ask.config else None
    if lens and lens in ask.lens.overlay.lenses:
        entries += [e.model_dump() for e in ask.lens.overlay.lenses[lens].not_computable]
    return entries


def not_computable(ask: Ask, qs: list[CubeQuery]) -> Check:
    """A question naming a measure the lens declares it cannot compute
    (or an alias): refused before any query, whatever the lane proposed."""
    name, mode = "not_computable", FailureMode.not_declared
    q = norm(ask.question)
    for entry in _nc_entries(ask):
        surfaces = [str(entry.get("measure") or ""), *map(str, entry.get("aliases") or [])]
        for s in surfaces:
            s = " ".join(norm(s).split())
            if s and re.search(rf"\b{re.escape(s)}\b", q):
                return Check(name, mode, "refuse", not_computable_refusal(entry))
    return passed(name, mode)


# ── not_for ─────────────────────────────────────────────────────────────────


def not_for(ask: Ask, qs: list[CubeQuery]) -> Check:
    """An ask this lens declares it does not answer (the lens's ``not_for``,
    a measure's ``meta.dst.not_for``): refused naming the sibling lens."""
    name, mode = "not_for", FailureMode.misrouted
    asks: list[tuple[str, str]] = []
    if ask.config:
        asks += [(e.ask, e.sibling) for e in ask.config.not_for]
        if ask.config.name in ask.lens.overlay.lenses:
            asks += [(e.ask, e.sibling) for e in ask.lens.overlay.lenses[ask.config.name].not_for]
    for q in qs:
        for m in q.measures:
            asks += [(a, "") for a in ask.lens.not_for(m)]
    for text, sibling in asks:
        if text.strip() and names_ask(ask.question, text):
            reason = f"this lens does not answer {text.strip()}"
            return Check(
                name, mode, "refuse", f"{reason}; ask {sibling}" if sibling.strip() else reason
            )
    return passed(name, mode)


# ── ambiguous_metric ────────────────────────────────────────────────────────


def _clarify_term(t: OverlayDefinition, read: str | None = None, says: str | None = None) -> str:
    options = " or ".join(f"({i + 1}) {r.says}" for i, r in enumerate(t.readings))
    head = re.split(r" Ask| When the readings", t.summary)[0] if t.summary else ""
    lead = f"{t.metric} - {head}".strip(" -")
    if read:
        return f"{lead} The query read {read} ({says}). Which do you mean: {options}?"
    return f"{lead} - the query reads several readings at once. Which do you mean: {options}?"


def _selected(question: str, words: list[str]) -> bool:
    qn = norm(question)
    return any(norm(w) in qn for w in words)


def _reaches(lens: CubeLensView, roots: set[str], cubes: set[str]) -> bool:
    """Whether some root cube reaches every other cube of the query along
    the model's joins (Cube walks a join from the cube that declares it).
    A model that declares no joins at all says nothing either way."""
    graph = {c.name: {j.name for j in c.joins} for c in lens.doc.cubes if not c.is_view}
    if len(cubes) < 2 or not any(graph.values()):
        return True
    for root in roots:
        seen, todo = {root}, [root]
        while todo:
            for nxt in graph.get(todo.pop(), set()) - seen:
                seen.add(nxt)
                todo.append(nxt)
        if cubes <= seen:
            return True
    return False


def counterpart_or_gap(lens: CubeLensView, q: CubeQuery, swaps: dict[str, str]) -> CubeQuery | str:
    """The query with each measure in ``swaps`` (as written → counterpart)
    replaced, only the swapped measures kept. The asked measure's clock —
    wherever it lives, its own cube or one joined to it — maps to the
    counterpart's clock, so the window reads the same period on the
    counterpart's own dates; any other member of the swapped measure's cube
    maps by the same name; a member of another cube stays when the
    counterpart's cube joins to it. Else why the reading cannot be computed
    for this query: a member with no counterpart, no clock to read the
    window on, no join path, a window left on another clock."""
    cmap: dict[str, str] = {}
    clocks: dict[str, str | None] = {}
    for a, b in swaps.items():
        ca, cb = lens.cube_name(a), lens.cube_name(b)
        if ca != cb:
            if ca in cmap and cmap[ca] != cb:
                return f"{a} maps to two cubes"
            cmap[ca] = cb
        ka = lens.clock(a)
        if ka:
            clocks[lens.base(ka)] = lens.clock(b)
    gaps: list[str] = []

    def mm(x: str) -> str | None:
        bx = lens.base(x)
        if bx in clocks:
            kb = clocks[bx]
            if kb is None:
                gaps.append(
                    f"{', '.join(swaps.values())} has no clock that reads the window on {bx}"
                )
            return kb
        c, _, rest = bx.partition(".")
        if c not in cmap:
            return bx
        cand = f"{cmap[c]}.{rest}"
        if cand in lens.cube_members(cmap[c]):
            return cand
        gaps.append(f"{bx} has no counterpart on {cmap[c]}")
        return None

    def mf(f: CubeFilter) -> CubeFilter | None:
        for key in ("and_", "or_"):
            group = getattr(f, key)
            if group is not None:
                sub = [mf(x) for x in group]
                if any(s is None for s in sub):
                    return None
                return CubeFilter.model_validate({key.rstrip("_"): [s.wire() for s in sub if s]})
        if f.member is None:
            return f
        m2 = mm(f.member)
        return None if m2 is None else f.model_copy(update={"member": m2})

    dims = [mm(d) for d in q.dimensions]
    filters = [mf(f) for f in q.filters]
    tds = [mm(t.dimension) for t in q.timeDimensions]
    segs = [mm(s) for s in q.segments]
    if None in dims or None in filters or None in tds or None in segs:
        return "; ".join(dict.fromkeys(gaps)) or "a member has no counterpart"
    out = CubeQuery(
        measures=[swaps[m] for m in q.measures if m in swaps],
        dimensions=[d for d in dims if d],
        filters=[f for f in filters if f],
        timeDimensions=[
            t.model_copy(update={"dimension": d})
            for t, d in zip(q.timeDimensions, tds, strict=True)
            if d
        ],
        segments=[s for s in segs if s],
    )
    roots = {lens.cube_name(m) for m in out.measures}
    cubes = roots | {lens.cube_name(m) for m in out.members()}
    if not _reaches(lens, roots, cubes):
        return f"no join path from {', '.join(sorted(roots))} to {', '.join(sorted(cubes - roots))}"
    windowed = [
        lens.base(t.dimension) for t in out.timeDimensions if t.dateRange or t.compareDateRange
    ]
    for m in out.measures:
        kb = lens.clock(m)
        if (
            windowed
            and kb
            and lens.base(kb) not in windowed
            and not any(lens.base(f.member or "") == lens.base(kb) for f in all_filters(out))
        ):
            return f"the window on {', '.join(windowed)} is not {m}'s clock {kb}"
    return out


def _row_key(row: dict[str, Any], q: CubeQuery, back: dict[str, str] | None = None) -> str:
    import json

    ms = set(q.measures)
    k: dict[str, Any] = {}
    for col, v in row.items():
        if col in ms:
            continue
        c2 = col
        for a, b in (back or {}).items():
            if col == b or col.startswith(b + "."):
                c2 = a + col[len(b) :]
        k[c2] = v
    return json.dumps(k, sort_keys=True, default=str)


def _rel(a: float, b: float) -> float:
    return 0.0 if a == b else abs(a - b) / max(abs(a), abs(b))


def _show(rows: Rows, cols: list[str], skip: set[str]) -> str:
    cells = []
    for r in rows[:12]:
        key = ", ".join(f"{c}={v}" for c, v in r.items() if c not in skip)
        mv = ", ".join(f"{c}={r.get(c)}" for c in cols if c in r)
        cells.append(f"{key}: {mv}" if key else mv)
    return "; ".join(cells) + (f"; ... ({len(rows)} rows)" if len(rows) > 12 else "")


def _figure_notes(ask: Ask, q: CubeQuery) -> str:
    """What would ride these figures were they served: the caveats on what
    the query reads and a partial period in its window. A figure printed in
    a disclosure travels with them, an answer or a clarification alike."""
    notes = ask.lens.caveats(q.members())
    part = partial_period(ask, [q])
    if part.outcome == "disclose":
        notes.append(part.detail)
    return f" (caveats on these figures: {'; '.join(notes)})" if notes else ""


def _computable(q: CubeQuery) -> CubeQuery:
    """A reading's query for comparison: no order, no limit, the window
    clipped to the lens clock as serving will clip it."""
    return q.model_copy(update={"order": [], "limit": None})


def ambiguous_metric(ask: Ask, qs: list[CubeQuery]) -> Check:
    """A measure that is one reading of an ambiguous term the question does
    not settle. A term with a declared ``default`` serves that reading
    however far the others are, and discloses them beside it — with their
    values when they compute, by name when they do not. Otherwise every
    reading is computed: within ``CLOSE_REL`` on every row the reading marked
    ``default: true`` (else the one the query read) is served and the others
    disclosed with their values; further apart (or not computable) the
    caller is asked."""
    name, mode = "ambiguous", FailureMode.ambiguous_metric
    lens = ask.lens
    terms = lens.ambiguous_terms()
    notes: list[str] = []
    out: list[CubeQuery] = []
    for q in qs:
        cur = q
        for t in terms:
            rds = t.readings
            hit: dict[int, dict[str, int]] = {}
            for i, rd in enumerate(rds):
                for m in cur.measures:
                    if lens.base(m) in rd.members:
                        hit.setdefault(i, {})[m] = rd.members.index(lens.base(m))
            if not hit:
                continue
            ri = next(iter(hit))
            if _selected(ask.question, rds[ri].select_words):
                continue  # the question names this reading: served as asked
            first = next(iter(hit[ri]))
            clar = _clarify_term(t, first, rds[ri].says)
            declared = next((i for i, r in enumerate(rds) if r.says == t.default), None)
            dflt = (
                declared
                if declared is not None
                else next((i for i, r in enumerate(rds) if r.default), ri)
            )
            if declared is None and ask.run is None:
                return Check(name, mode, "clarify", clar)
            if len(hit) > 1:
                # The query already reads several readings: compare in place.
                if (
                    len(hit) < len(rds)
                    or dflt not in hit
                    or not all(set(v.values()) <= set(hit[dflt].values()) for v in hit.values())
                ):
                    return Check(name, mode, "clarify", _clarify_term(t))
                rows = ask.run(_computable(clip(lens, cur)[0])) if ask.run else []
                by_pos = {j: {p: m for m, p in v.items()} for j, v in hit.items()}
                worst = 0.0
                for row in rows:
                    for p, mem in by_pos[dflt].items():
                        a0 = fnum(row.get(mem))
                        for j in by_pos:
                            if p in by_pos[j]:
                                worst = max(worst, _rel(a0, fnum(row.get(by_pos[j][p]))))
                if worst > CLOSE_REL and declared is None:
                    return Check(
                        name, mode, "clarify", f"{clar} (the readings differ by up to {worst:.1%})"
                    )
                drop = [m for j, v in hit.items() if j != dflt for m in v]
                shown = "; ".join(
                    ", ".join(f"{c}={v}" for c, v in row.items() if c not in by_pos[dflt].values())
                    for row in rows[:12]
                )
                if shown:
                    shown += _figure_notes(ask, cur.model_copy(update={"measures": drop}))
                notes.append(
                    f"ambiguous_metric disclosed: {t.metric} - the query reads every reading; "
                    + (
                        f"served on the declared default reading ({rds[dflt].says}); they differ "
                        f"by up to {worst:.2%}"
                        if declared is not None
                        else f"they differ by at most {worst:.2%} (within {CLOSE_REL:.0%}); "
                        f"served on the default reading ({rds[dflt].says})"
                    )
                    + (f"; the others: {shown}" if shown else "")
                )
                cur = cur.model_copy(
                    update={
                        "measures": [m for m in cur.measures if m not in drop],
                        "order": [(k, d) for k, d in cur.order if k not in drop],
                    }
                )
                continue
            pos = hit[ri]
            base_q = clip(lens, cur)[0]
            vals: dict[int, tuple[CubeQuery, Rows | None, dict[str, str], dict[str, str]]] = {}
            uncomputed: list[tuple[str, str]] = []
            for j, rd in enumerate(rds):
                swaps = {m: rd.members[p] for m, p in pos.items() if p < len(rd.members)}
                got_q: CubeQuery | str
                if j == ri:
                    got_q = base_q.model_copy(update={"measures": list(pos)})
                elif len(swaps) == len(pos):
                    got_q = counterpart_or_gap(lens, base_q, swaps)
                else:
                    got_q = "it has no counterpart for every measure of the query"
                if isinstance(got_q, str) and declared is not None and j != dflt:
                    uncomputed.append((rd.says, got_q))  # named beside the default
                    continue
                if isinstance(got_q, str):
                    return Check(
                        name,
                        mode,
                        "clarify",
                        f"{clar} (the reading '{rd.says}' cannot be computed for this query: "
                        f"{got_q})",
                    )
                qj = _computable(got_q)
                got = ask.run(qj) if ask.run is not None else None
                back: dict[str, str] = {}
                if j != ri:
                    olds = list(base_q.dimensions) + [x.dimension for x in base_q.timeDimensions]
                    news = list(qj.dimensions) + [x.dimension for x in qj.timeDimensions]
                    back = dict(zip(olds, news, strict=False))
                vals[j] = (qj, got, back, swaps)
            base_rows = {_row_key(r, vals[ri][0]): r for r in vals[ri][1] or []}
            worst = 0.0
            for j, (qj, got, back, swaps) in vals.items():
                if j == ri or got is None:
                    continue
                other = {_row_key(r, qj, back): r for r in got}
                for k in set(base_rows) | set(other):
                    for m in pos:
                        a = fnum((base_rows.get(k) or {}).get(m))
                        b = fnum((other.get(k) or {}).get(swaps[m]))
                        worst = max(worst, _rel(a, b))
            if worst > CLOSE_REL and declared is None:
                return Check(
                    name, mode, "clarify", f"{clar} (the readings differ by up to {worst:.1%})"
                )
            others = []
            for j in vals:
                if j == dflt:
                    continue
                qj, got, _back, swaps = vals[j]
                cols = list(swaps.values()) if j != ri else list(pos)
                others.append(
                    f"on '{rds[j].says}' the figures are "
                    + _show(got, cols, set(swaps.values()) | set(pos))
                    + _figure_notes(ask, qj)
                    if got is not None
                    else f"'{rds[j].says}' is another reading (not computed here)"
                )
            others += [
                f"'{says}' is another reading (not computable beside it: {why})"
                for says, why in uncomputed
            ]
            served_as = "default reading" if rds[dflt].default else "reading the query read"
            computed = any(rows is not None for j, (_q, rows, _b, _s) in vals.items() if j != ri)
            head = (
                f"served on the declared default reading ({rds[dflt].says})"
                + (f"; the readings differ by up to {worst:.2%}" if computed else "")
                if declared is not None
                else f"the readings differ by at most {worst:.2%} (within {CLOSE_REL:.0%}); "
                f"served on the {served_as} ({rds[dflt].says})"
            )
            notes.append(f"ambiguous_metric disclosed: {t.metric} - {head}; " + "; ".join(others))
            if dflt != ri:
                rest = [m for m in cur.measures if m not in pos]
                swaps = {m: rds[dflt].members[p] for m, p in pos.items()}
                qd_or_gap = counterpart_or_gap(lens, cur, swaps)
                if isinstance(qd_or_gap, str):
                    return Check(
                        name,
                        mode,
                        "clarify",
                        f"{clar} (the reading '{rds[dflt].says}' cannot be served for this "
                        f"query: {qd_or_gap})",
                    )
                qd = qd_or_gap
                om = {**{lens.base(a): b for a, b in swaps.items()}, **vals[dflt][2]}
                order = [
                    (om.get(lens.base(k), om.get(k, k)), d) for k, d in q.order if k not in rest
                ]
                qd = qd.model_copy(update={"order": order, "limit": q.limit})
                if rest:
                    out.append(cur.model_copy(update={"measures": rest}))
                cur = qd
        out.append(cur)
    if notes:
        return Check(name, mode, "disclose", "; ".join(notes), queries=out)
    return passed(name, mode)


# ── uncovered_word ──────────────────────────────────────────────────────────


def scope_words(lens: CubeLensView) -> dict[str, tuple[str | None, str | None]]:
    """``{phrase: (axis, value | None)}``: the values of every scope-word
    dimension as phrases; a company value also by each of its words that
    names no other company; and the group words (axis None)."""
    out: dict[str, tuple[str | None, str | None]] = {
        norm(g): (None, None) for g in lens.overlay.scope.group_names
    }
    entity_like = lens.scope_axes()
    companies: dict[str, str] = {}
    for mem, dom in lens.probe.dictionaries.items():
        ax = mem.split(".")[-1]
        if ax not in lens.scope_word_axes():
            continue
        if ax in entity_like:
            companies.update({v: ax for v in dom})
        else:
            for v in dom:
                out.setdefault(norm(v), (ax, v))
    words = {e: toks(e) for e in companies}
    for e, ts in words.items():
        out.setdefault(" ".join(ts), (companies[e], e))
        for t in ts:
            if len(t) > 2 and sum(t in o for o in words.values()) == 1:
                out.setdefault(t, (companies[e], e))
    return out


def _scope_bound(
    lens: CubeLensView,
    phrase: str,
    ax: str | None,
    ent: str | None,
    q: CubeQuery,
    sw: dict[str, tuple[str | None, str | None]],
) -> bool | None:
    """True | False | None (untestable): whether the query binds a scope
    word. Bound by an equals filter on a scope axis carrying it, a breakdown
    by the axis, or a used cube or member whose text carries it; a group word
    by a used cube holding several companies (an entity dimension not
    narrowed to one) or named for the group."""
    members, _, _, _ = used(q, lens)
    axes = lens.scope_word_axes()
    for f in q.filters:
        if f.member and lens.base(f.member).split(".")[-1] in axes and f.operator == "equals":
            if any(phrase_in(v, phrase) for v in f.values or []):
                return True
    wanted = {ax} if ax else lens.scope_axes()
    if any(lens.base(d).split(".")[-1] in wanted for d in q.dimensions):
        return True
    cubes = {lens.cube_name(m) for m in members}
    company_words = [p for p, (a, _) in sw.items() if a is not None and a in lens.scope_axes()]
    if ax is None:
        for c in cubes:
            ed = lens.entity_dimension(c)
            narrowed = any(
                f.member
                and lens.base(f.member) == ed
                and f.operator == "equals"
                and len(f.values or []) == 1
                for f in q.filters
            )
            if any(phrase_in(c, g) for g in lens.overlay.scope.group_names) or (
                ed and not narrowed
            ):
                return True
        named = [c for c in cubes if any(phrase_in(lens.cube_text(c), p) for p in company_words)]
        return False if named else None
    if any(phrase_in(lens.cube_text(c), phrase) for c in cubes) or any(
        phrase_in(lens.member_text(m), phrase) for m in members
    ):
        return True
    if ax in lens.scope_axes():
        # A cube that holds several companies, read for all of them, does not
        # answer for one.
        if any(lens.entity_dimension(c) for c in cubes):
            return False
        others = [
            p
            for p, (a, e) in sw.items()
            if a in lens.scope_axes()
            and e != ent
            and any(phrase_in(lens.cube_text(c), p) for c in cubes)
        ]
        return False if others else None
    return False


def uncovered_word(ask: Ask, qs: list[CubeQuery]) -> Check:
    """Every content word of the question is carried by something the
    queries use: the ledger names it, or a used member, cube, filter value,
    breakdown value or segment carries it. A scope word (a company, a revenue
    stream, the group) must be bound structurally. No ledger: clarify, never
    a pass on nothing."""
    name, mode = "words", FailureMode.uncovered_word
    lens = ask.lens
    qs = ask.asked or qs
    if ask.ledger is None:
        return Check(
            name,
            mode,
            "clarify",
            "the answer carries no word ledger, so the words of the question cannot be "
            "checked against the query - which part of the question matters?",
        )
    U = [used(q, lens) for q in qs]
    allmem: set[str] = set().union(*[u[0] for u in U]) if U else set()
    allvals = [v for u in U for v in u[1]]
    allsegs: set[str] = set().union(*[u[2] for u in U]) if U else set()
    window = any(u[3] for u in U)

    def entry_ok(kind: str, ref: str) -> bool:
        if kind == "period":
            return window
        r = ref.strip()
        if r in allmem or lens.base(r) in allmem:
            return True
        if any(r == m.split(".")[0] or r == m.split(".")[-1] for m in allmem):
            return True
        if any(
            norm(r) == norm(v)
            or (len(r) > 2 and norm(r) in norm(v))
            or (len(v) > 2 and norm(v) in norm(r))
            for v in allvals
        ):
            return True
        if r in allsegs:
            return True
        m0 = re.split(r"\s|=|<|>", r)[0]
        return m0 in allmem or lens.base(m0) in allmem

    sw = scope_words(lens)
    scoped = {stem(t) for ph in sw if phrase_in(ask.question, ph) for t in toks(ph)}
    content = [
        (stem(t), t)
        for t in toks(ask.question)
        if t not in STOP and not re.fullmatch(r"[0-9%'\-]+", t) and stem(t) not in scoped
    ]
    uncovered: list[str] = []
    for st, t in content:
        entries = [b for b in ask.ledger if any(stem(x) == st for x in toks(b.word))]
        ok = any(entry_ok(b.kind, b.target) for b in entries)
        if t in PERIOD:
            if not (window or ok):
                uncovered.append(t)
            continue
        if ok:
            continue
        if not (
            any(has_stem(lens.member_text(m), st) for m in allmem)
            or any(has_stem(lens.cube_text(lens.cube_name(m)), st) for m in allmem)
            or any(has_stem(v, st) for v in allvals)
            or any(has_stem(sg, st) for sg in allsegs)
        ):
            uncovered.append(t)
    unbound: list[str] = []
    untested: list[str] = []
    for ph, (ax, ent) in sw.items():
        if not phrase_in(ask.question, ph):
            continue
        if any(phrase_in(o, ph) and o != ph and phrase_in(ask.question, o) for o in sw):
            continue  # a longer scope phrase the question also says judges it
        res = [_scope_bound(lens, ph, ax, ent, q, sw) for q in qs]
        if True in res:
            continue
        (unbound if False in res else untested).append(ph)
    uncovered = list(dict.fromkeys(uncovered))
    unbound = list(dict.fromkeys(unbound))
    if uncovered or unbound:
        parts = []
        if uncovered:
            parts.append(f"nothing in the query carries {', '.join(repr(w) for w in uncovered)}")
        if unbound:
            parts.append(
                f"the scope {', '.join(repr(w) for w in unbound)} is not bound by a filter, "
                "breakdown or member of the query"
            )
        it = "it" if len(uncovered + unbound) == 1 else "them"
        return Check(
            name, mode, "clarify", "; ".join(parts) + f" - what should the answer do with {it}?"
        )
    if untested:
        return Check(
            name,
            mode,
            "pass",
            "UNTESTED: the query's cubes do not say which company they hold, so the scope "
            + ", ".join(repr(p) for p in untested)
            + " cannot be checked",
        )
    return passed(name, mode)


# ── unasked_filter ──────────────────────────────────────────────────────────


def _names_scope(lens: CubeLensView, question: str, value: str) -> bool:
    words = lens.scope_names().get(value, [norm(value)])
    return any(phrase_in(question, w) for w in words if w.strip())


def unasked_filter(ask: Ask, qs: list[CubeQuery]) -> Check:
    """A query read for one company or country the question does not name,
    when the group-level figure exists: (i) an equals filter on a scope axis
    to one value whose removal brings in other values (probed); (ii) a
    measure of a single-company cube that declares its group-level member."""
    name, mode = "unasked_scope", FailureMode.unasked_filter
    lens = ask.lens
    axes = lens.scope_axes()
    for q in ask.asked or qs:
        for f in flat_filters(q):
            mem = f.member or ""
            ax = lens.base(mem).split(".")[-1]
            if ax not in axes or f.operator != "equals" or len(f.values or []) != 1:
                continue
            v = str((f.values or [""])[0])
            if _names_scope(lens, ask.question, v) or not q.measures:
                continue
            if ask.run is None:
                return Check(name, mode, "skip", "no executor to probe the unfiltered figure")
            probe = CubeQuery(
                measures=list(q.measures),
                dimensions=[mem],
                filters=[x for x in q.filters if x is not f],
                timeDimensions=[
                    t.model_copy(update={"granularity": None}) for t in q.timeDimensions
                ],
            )
            rows = ask.run(probe)
            others = sorted(
                {
                    str(r.get(mem))
                    for r in rows
                    if str(r.get(mem)) != v and any(fnum(r.get(m)) for m in probe.measures)
                }
            )
            if others:
                led = [b.word for b in ask.ledger or [] if v.lower() in b.target.lower()]
                tied = (
                    f" (the ledger ties {', '.join(map(repr, led))} to it)"
                    if led
                    else " (no word of the ledger is tied to it)"
                )
                return Check(
                    name,
                    mode,
                    "clarify",
                    f"the query reads {mem} = '{v}' only, but the question does not name it"
                    f"{tied}; without the filter the figure also holds {', '.join(others)}. "
                    f"All of them (the group), or '{v}' only?",
                )
        for m in q.measures:
            c = lens.cube_name(m)
            es = (lens.overlay.cubes.get(c) or None) and lens.overlay.cubes[c].entity_scope
            if not es:
                continue
            gl = es.group_level.get(lens.base(m))
            if gl and not _names_scope(lens, ask.question, es.entity):
                return Check(
                    name,
                    mode,
                    "clarify",
                    f"{m} holds {es.entity} only ({c}), but the question does not name a "
                    f"company; the group-level figure is {gl}. The group, or {es.entity} only?",
                )
    return passed(name, mode)


# ── unbound_value ───────────────────────────────────────────────────────────


def unbound_value(ask: Ask, qs: list[CubeQuery]) -> Check:
    """Every equals filter value on a dictionary dimension is one of its
    observed values. A unique case-insensitive or value-alias match is
    rewritten and disclosed; anything else asks (never a silent 0). A
    ``notEquals`` on an absent value excludes nothing and is left alone."""
    name, mode = "values", FailureMode.unbound_value
    lens = ask.lens
    notes: list[str] = []
    out: list[CubeQuery] = []
    for q in qs:
        q = copy(q)
        for f in all_filters(q):
            if not f.member or f.operator != "equals":
                continue
            dom = lens.dictionary(f.member)
            if dom is None:
                continue
            new: list[str] = []
            for v in f.values or []:
                if v in dom:
                    new.append(v)
                    continue
                ci = [d for d in dom if d.lower() == v.lower()]
                al = lens.value_alias(f.member, v)
                if len(ci) == 1 or (al is not None and al in dom):
                    fixed = ci[0] if len(ci) == 1 else str(al)
                    notes.append(f"value: '{v}' read as {f.member} = '{fixed}'")
                    new.append(fixed)
                    continue
                return Check(
                    name,
                    mode,
                    "clarify",
                    f"'{v}' is not a value of {f.member}; its values are: "
                    f"{', '.join(dom[:25])}. Which one do you mean?",
                )
            f.values = new
        out.append(q)
    if notes:
        return Check(name, mode, "disclose", "; ".join(notes), queries=out)
    return passed(name, mode)


# ── window_misbound ─────────────────────────────────────────────────────────


def clip(lens: CubeLensView, q: CubeQuery) -> tuple[CubeQuery, list[str]]:
    """Event-dated rows after the lens clock cannot have happened yet: every
    event time dimension with future rows is clipped to the clock, and a
    measure whose clock has future rows but is not in the query gets a clock
    filter. Schedule dates (due, end, expiry) are left alone."""
    end = lens.probe.clock_end
    if not end:
        return q, []
    q = copy(q)
    notes: list[str] = []
    for td in q.timeDimensions:
        t = lens.time_facts(td.dimension)
        if t is None or t.kind != "event" or not t.future_rows:
            continue

        def cut(r: Any, td: TimeDimension = td, rows: int = t.future_rows, mx: Any = t.max) -> Any:
            if isinstance(r, list) and str(r[0])[:10] > end:
                return r  # nothing of it is loaded: the freshness check names it
            if isinstance(r, list) and str(r[-1])[:10] > end:
                notes.append(
                    f"clipped: {td.dimension} window ends at the lens clock {end} ({rows} rows "
                    f"are dated after it, latest {mx})"
                )
                return [r[0], end]
            return r

        if isinstance(td.dateRange, list):
            td.dateRange = cut(td.dateRange)
        if td.compareDateRange:
            td.compareDateRange = [cut(r) for r in td.compareDateRange]
    for f in q.filters:
        t = lens.time_facts(f.member) if f.member else None
        if t is None or t.kind != "event" or not t.future_rows or not f.values:
            continue
        last = len(f.values) - 1 if f.operator == "inDateRange" else 0
        if f.operator not in ("inDateRange", "beforeDate", "beforeOrOnDate"):
            continue
        if f.values[last][:10] > end and (last == 0 or f.values[0][:10] <= end):
            notes.append(
                f"clipped: the {f.member} filter ends at the lens clock {end} "
                f"({t.future_rows} rows are dated after it)"
            )
            f.values = [*f.values[:last], end]
            if f.operator == "beforeDate":
                f.operator = "beforeOrOnDate"
    for td in q.timeDimensions:
        t = lens.time_facts(td.dimension)
        if t is None or t.kind != "event" or not t.future_rows:
            continue
        if td.dateRange is None and not td.compareDateRange and not _bounded(lens, q, td.dimension):
            # Read over all time: the rows after the clock are never counted.
            q.filters.append(
                CubeFilter(member=td.dimension, operator="beforeOrOnDate", values=[end])
            )
            notes.append(
                f"clipped: {td.dimension} read up to the lens clock {end} ({t.future_rows} rows "
                "are dated after it)"
            )
    have = {lens.cube_name(td.dimension) for td in q.timeDimensions}
    for m in q.measures:
        clock = lens.clock(m)
        t = lens.time_facts(clock) if clock else None
        if (
            clock
            and t is not None
            and clock.split(".")[0] not in have
            and t.kind == "event"
            and t.future_rows
            and lens.base(m) == m
        ):
            q.filters.append(CubeFilter(member=clock, operator="beforeOrOnDate", values=[end]))
            have.add(clock.split(".")[0])
            notes.append(
                f"clipped: {m} read up to the lens clock {end} on {clock} ({t.future_rows} rows "
                "are dated after it)"
            )
    return q, notes


def _bounded(lens: CubeLensView, q: CubeQuery, dimension: str) -> bool:
    """A top-level filter already ends the dimension's dates."""
    return any(
        f.member
        and lens.base(f.member) == lens.base(dimension)
        and f.operator in ("beforeDate", "beforeOrOnDate", "inDateRange")
        for f in q.filters
    )


def _agrees(w: list[str], p: Period, through: date, *, like_for_like: bool = False) -> bool:
    """A window reads the period: its days exactly, or, for a period not over
    yet, from its first day to any day from ``through`` (today, or the last
    loaded day) to its end. Beside such a to-date window (``like_for_like``),
    a past period may be read to the same point: from its first day, ending
    inside it."""
    if w == p.window:
        return True
    if p.current:
        return w[0] == p.window[0] and through.isoformat() <= w[1] <= p.window[1]
    return like_for_like and w[0] == p.window[0] and w[0] <= w[1] <= p.window[1]


def _windows_of(q: CubeQuery) -> list[list[str]]:
    out: list[list[str]] = []
    for td in q.timeDimensions:
        for r in [td.dateRange, *(td.compareDateRange or [])]:
            if isinstance(r, list) and r:
                out.append([str(r[0])[:10], str(r[-1])[:10]])
    return out


def _bind_periods(ask: Ask, qs: list[CubeQuery]) -> tuple[list[CubeQuery], list[str], str | None]:
    """``(queries, notes, question)``: every window bound to the period dst
    resolved for the question's phrase. When some period is read by no
    window and there are as many windows as periods, the windows are rebound
    in date order; any other count is the question to ask back. A period not
    over yet is said to be partial."""
    periods = periods_of(ask.question, ask.today)
    if not periods or ask.today is None:
        return qs, [], None
    clock = ask.lens.probe.clock()
    through = min(ask.today, clock) if clock else ask.today
    seen: list[list[str]] = []
    for q in qs:
        seen += [w for w in _windows_of(q) if w not in seen]
    to_date = any(
        p.current and w != p.window and _agrees(w, p, through) for p in periods for w in seen
    )
    missing = [
        p for p in periods if not any(_agrees(w, p, through, like_for_like=to_date) for w in seen)
    ]
    notes = [
        f"{p.label()}, partial: today is {ask.today.isoformat()}"
        + (f", data loaded through {clock.isoformat()}" if clock else "")
        for p in periods
        if p.current
    ]
    if not missing:
        return qs, notes, None
    if len(seen) != len(periods):
        said = ", ".join(p.label() for p in missing)
        return (
            qs,
            notes,
            f"{said}, and the query reads "
            + (", ".join("..".join(w) for w in seen) if seen else "no window for it")
            + ". Which dates should the answer read?",
        )
    # As many windows as periods, some off: a model that misread the date
    # shifts every window alike, so they are bound in date order.
    pairs = list(zip(sorted(seen), periods, strict=True))
    swap = {tuple(w): p.window for w, p in pairs}

    def bound(r: Any) -> Any:
        if isinstance(r, list) and r:
            return swap.get((str(r[0])[:10], str(r[-1])[:10]), r)
        return r

    out: list[CubeQuery] = []
    for q in qs:
        q2 = copy(q)
        for td in q2.timeDimensions:
            td.dateRange = bound(td.dateRange)
            if td.compareDateRange:
                td.compareDateRange = [bound(r) for r in td.compareDateRange]
        out.append(q2)
    notes = [
        f"window: {p.label()} (the query read {'..'.join(w)})" for w, p in pairs if w != p.window
    ] + notes
    return out, notes, None


def window_misbound(ask: Ask, qs: list[CubeQuery]) -> Check:
    """Windows bound the way the data can answer them: each relative period
    of the question on the dates dst resolved for it (``periods``), a window
    on a measure's declared clock (a figure counted by decision date is not
    windowed by the date the quote was sent), clipped to the lens clock when
    the clock holds future-dated rows."""
    name, mode = "window", FailureMode.window_misbound
    lens = ask.lens
    qs, period_notes, asked = _bind_periods(ask, qs)
    if asked is not None:
        return Check(name, mode, "clarify", asked)
    for q in qs:
        windowed = [
            lens.base(t.dimension) for t in q.timeDimensions if t.dateRange or t.compareDateRange
        ]
        if not windowed:
            continue
        for m in q.measures:
            clock = lens.clock(m)
            if (
                clock
                and lens.base(clock) not in windowed
                and not any(lens.base(f.member or "") == lens.base(clock) for f in all_filters(q))
            ):
                return Check(
                    name,
                    mode,
                    "clarify",
                    f"{m} is counted on {clock}, but the query windows it on "
                    f"{', '.join(windowed)}. Should the window apply to {clock}?",
                )
    notes: list[str] = list(period_notes)
    out: list[CubeQuery] = []
    for q in qs:
        q2, n = clip(lens, q)
        out.append(q2)
        notes += n
    if notes:
        return Check(name, mode, "disclose", "; ".join(dict.fromkeys(notes)), queries=out)
    return passed(name, mode)


# ── wrong_grain (label vs key) ──────────────────────────────────────────────


def label_key(ask: Ask, qs: list[CubeQuery]) -> Check:
    """Rank by key, show the label: a dimension that labels a keyed row
    (``meta.dst.label_of``) is grouped with its key, so two rows sharing a
    name never merge into one."""
    name, mode = "label_key", FailureMode.wrong_grain
    lens = ask.lens
    notes: list[str] = []
    out: list[CubeQuery] = []
    for q in qs:
        dims = list(q.dimensions)
        have = {lens.base(d) for d in dims}
        for d in list(dims):
            k = lens.label_key(d)
            if not k or k in have:
                continue
            via = d.split(".")[0]
            written = k
            if via != lens.cube_name(d):
                view = lens.doc.cube(via)
                written = next(
                    (x for x in (view.member_names() if view else []) if lens.base(x) == k), k
                )
            dims.insert(dims.index(d), written)
            have.add(k)
            notes.append(
                f"key: {d} is a label, not an identity - grouped by {written} as well, so equal "
                "names never merge"
            )
        out.append(q.model_copy(update={"dimensions": dims}))
    if notes:
        return Check(name, mode, "disclose", "; ".join(notes), queries=out)
    return passed(name, mode)


# The fixed order. The question-level refusals come first so a lane's model
# call is never spent on a question the lens refuses whatever the query.
PRE_ORDER: tuple[PreCheck, ...] = (
    not_computable,
    not_for,
    scope_violation,
    hidden_column,
    not_declared,
    ambiguous_metric,
    uncovered_word,
    unasked_filter,
    unbound_value,
    window_misbound,
    label_key,
)
QUESTION_ONLY: tuple[PreCheck, ...] = (not_computable, not_for)
