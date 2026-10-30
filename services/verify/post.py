"""The checks on what the data holds: declared freshness and partial periods
(read from the probe file before a query runs, so a window with nothing
loaded is never executed), and the result checks after it ran: an empty
filtered result, an empty bucket ranked, a tie cut at the limit, a level
measure read across snapshots, a comparison's windows set side by side.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

from services.contracts.cube_probe import PartialPeriod
from services.contracts.cube_query import CubeFilter, CubeQuery
from services.contracts.failure_modes import FailureMode
from services.cube.lens_view import GRAINS, CubeLensView
from services.verify.base import Ask, Check, Rows, all_filters, equals_values, fnum, passed, windows

# Rows read past a limit to see whether the last row included ties the next.
TIE_EXTRA = 50


# ── data_gap: declared per-source freshness ─────────────────────────────────


def streams_read(lens: CubeLensView, q: CubeQuery, m: str) -> list[tuple[str, str | None, bool]]:
    """``[(label, end, stopped)]`` the feeds a measure reads under this query:
    its cube's feed streams narrowed by the measure's own pins and the query's
    equals filters; a cube without feed axes is one stream on its clock."""
    rec = lens.measure_facts(m)
    if rec is None:
        return []
    cube = lens.probe.cubes.get(rec.cube)
    if cube is not None and cube.streams:
        out = []
        for st in cube.streams:
            ok = True
            for ax, v in st.values.items():
                pin = rec.streams_pinned.get(ax)
                qf = equals_values(q, lens, f"{rec.cube}.{ax}")
                if (pin and str(v) not in pin) or (qf is not None and str(v) not in qf):
                    ok = False
            if ok:
                label = ", ".join(f"{k}={v}" for k, v in st.values.items()) + f" ({cube.clock})"
                out.append((label, st.end, st.stopped))
        return out
    clock = rec.clock
    t = lens.time_facts(clock) if clock else None
    if t is None or t.kind != "event":
        return []
    return [(str(clock), t.data_end, t.stopped)]


def data_gap(ask: Ask, q: CubeQuery) -> Check:
    """Every feed a measure reads, against every window it is read over:
    a feed ending inside the window is disclosed, and the query is refused
    only when no measure holds loaded data in any window (a window after the
    clock, or past a stopped feed's end). A balance read at each feed's data
    end says so."""
    name, mode = "freshness", FailureMode.data_gap
    lens = ask.lens
    clock_end = lens.probe.clock_end
    if not clock_end:
        return Check(name, mode, "skip", "no probe file records where the data ends")
    if not any(lens.measure_facts(m) for m in q.measures):
        return Check(name, mode, "skip", "the probe file records none of the measures read")
    stale = lens.probe.stale_after_days
    notes: list[str] = []
    hard_all = True
    for m in q.measures:
        sts = streams_read(lens, q, m)
        if not sts:
            hard_all = False
            continue
        for start, end in windows(q, lens):
            endc = min(end, clock_end)
            lim = (date.fromisoformat(endc) - timedelta(days=stale)).isoformat()
            hard = True
            for label, e, stopped in sts:
                if start and start > clock_end:
                    msg = (
                        f"data_gap: {start}..{end} is after the last loaded day {clock_end}; "
                        f"nothing is loaded for it ({label})"
                    )
                elif e is None or (start and start > e):
                    msg = f"data_gap: {label} has no rows in {start}..{end} (data ends {e})" + (
                        "; the feed has stopped" if stopped else ""
                    )
                    hard = hard and stopped
                elif e < lim:
                    msg = (
                        f"data_gap: {label} data ends {e}, the window {start}..{endc} runs past "
                        f"it; figures after {e} are missing"
                        if start
                        else f"data_gap: {label} data ends {e}; as of {endc} the figure is as of "
                        f"{e}"
                    ) + (" (the feed has stopped)" if stopped else "")
                    hard = False
                else:
                    hard = False
                    continue
                if msg not in notes:
                    notes.append(msg)
            hard_all = hard_all and hard
    asof: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for m in q.measures:
        dim = lens.as_of(m)
        if dim:
            ends = tuple(
                sorted(
                    {
                        f"{lbl.split(' (')[0]} as of {e}"
                        for lbl, e, _ in streams_read(lens, q, m)
                        if e
                    }
                )
            )
            asof.setdefault((dim, ends), []).append(m)
    for (dim, ends), ms in asof.items():
        verb = "is a position" if len(ms) == 1 else "are positions"
        notes.append(
            f"as_of: {', '.join(ms)} {verb} at each feed's data end ({dim}), not today"
            + (": " + "; ".join(ends) if ends else "")
        )
    for cube in dict.fromkeys(rec.cube for m in q.measures if (rec := lens.measure_facts(m))):
        facts = lens.probe.cubes.get(cube)
        for warning in facts.warnings if facts is not None else []:
            notes.append(f"freshness: {warning}; what it recorded is not checked")
    if notes and hard_all:
        return Check(name, mode, "refuse", "; ".join(notes))
    if notes:
        return Check(name, mode, "disclose", "; ".join(notes))
    return passed(name, mode)


# ── data_gap: partial periods ───────────────────────────────────────────────


def _partial_hits(lens: CubeLensView, q: CubeQuery, m: str) -> list[tuple[PartialPeriod, bool]]:
    rec = lens.measure_facts(m)
    if rec is None:
        return []
    parts = [rec.partial] if rec.partial else []
    for p in rec.partial_streams:
        ok = True
        for ax, v in p.values.items():
            qf = equals_values(q, lens, f"{rec.cube}.{ax}")
            pin = rec.streams_pinned.get(ax)
            if (qf is not None and str(v) not in qf) or (pin and str(v) not in pin):
                ok = False
        if ok:
            parts.append(p)
    out = []
    for start, end in windows(q, lens):
        for p in parts:
            out.append((p, end >= p.partial_from and (start is None or start <= p.partial_through)))
    return out


def partial_period(ask: Ask, qs: list[CubeQuery]) -> Check:
    """A window that includes a period the monitor found only partly loaded
    is served and disclosed: which period, how complete, judged by which
    measure, and — when the same measure is read over complete periods
    beside it — that the comparison sets a partial period against complete
    ones. dst owns the fact; how to frame a partial period is the caller's
    judgment, so it never refuses."""
    name, mode = "partial_period", FailureMode.data_gap
    lens = ask.lens
    seen: dict[tuple[str, str, str], list[tuple[bool, PartialPeriod]]] = {}
    for q in qs:
        for m in q.measures:
            for p, inc in _partial_hits(lens, q, m):
                key = (lens.base(m), p.period, json.dumps(p.values, sort_keys=True))
                seen.setdefault(key, []).append((inc, p))
    notes: list[str] = []
    for (m, period, _), hits in seen.items():
        p = hits[0][1]
        if not any(inc for inc, _ in hits):
            continue
        st = f" ({', '.join(f'{k}={v}' for k, v in p.values.items())})" if p.values else ""
        notes.append(
            f"data_gap: partial period - {m}{st} for {period} is loaded at "
            f"{p.partial_ratio:.0%} of its trailing {p.trailing_months}-month median, judged by "
            f"{p.judged_by or m} ({p.value:,.0f} vs {p.trailing_median:,.0f}, loaded to "
            f"{p.partial_through}{'; ' + p.evidence if p.evidence else ''}): a window that "
            f"includes {period} understates it"
            + (
                f"; the comparison sets {period} against complete periods"
                if any(not inc for inc, _ in hits)
                else ""
            )
        )
    if notes:
        return Check(name, mode, "disclose", "; ".join(notes))
    return passed(name, mode)


# ── empty_result ────────────────────────────────────────────────────────────


def _dict_filters(lens: CubeLensView, q: CubeQuery) -> list[str]:
    return [
        f"{f.member} = {', '.join(f.values or [])}"
        for f in all_filters(q)
        if f.member and f.operator == "equals" and lens.dictionary(f.member) is not None
    ]


def empty_result(ask: Ask, q: CubeQuery, rows: Rows) -> Check:
    """A query filtered on dictionary values whose every measure came back
    empty or zero: nothing was recorded, which is not a measured 0."""
    name, mode = "empty_result", FailureMode.empty_result
    filters = _dict_filters(ask.lens, q)
    if not filters or not q.measures:
        return passed(name, mode)
    if all(all(fnum(r.get(m)) == 0 for m in q.measures if m in r) for r in rows):
        return Check(
            name,
            mode,
            "refuse",
            f"no rows match {'; '.join(filters)} in the window - nothing was recorded, which is "
            "not the same as a measured 0",
        )
    return passed(name, mode)


# ── wrong_grain: an empty bucket ranked ─────────────────────────────────────


def null_bucket(ask: Ask, q: CubeQuery, rows: Rows) -> Check:
    """A ranked result whose group dimension holds an empty or NULL bucket:
    the bucket is not a member of the group and never ranks. The ranked
    query is re-run with the dimension set; the bucket is reported apart."""
    name, mode = "null_bucket", FailureMode.wrong_grain
    lens = ask.lens
    dims = [d for d in q.dimensions if not lens.is_time(d)]
    if not q.order or not dims or not rows:
        return passed(name, mode)
    bad = [r for r in rows if any(r.get(d) in (None, "") for d in dims)]
    if not bad:
        return passed(name, mode)
    if ask.run is None:
        return Check(name, mode, "skip", "no executor to re-rank without the empty bucket")
    q2 = q.model_copy(deep=True)
    for d in dims:
        if any(r.get(d) in (None, "") for r in bad):
            q2.filters.append(CubeFilter(member=d, operator="set"))
            if any(r.get(d) == "" for r in bad):
                q2.filters.append(CubeFilter(member=d, operator="notEquals", values=[""]))
    empty = ", ".join(d for d in dims if any(b.get(d) in (None, "") for b in bad))
    shown = "; ".join(", ".join(f"{k}={v}" for k, v in b.items()) for b in bad)
    return Check(
        name,
        mode,
        "disclose",
        f"unassigned bucket (empty {empty}) is not ranked: {shown}",
        queries=[q2],
        rows=ask.run(q2),
    )


# ── partial_ask: a tie cut at the limit ─────────────────────────────────────


def _cell(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) or re.fullmatch(r"-?\d+(\.\d+)?", str(v or "")):
        return round(fnum(v), 9)
    return v


def tie_at_limit(ask: Ask, q: CubeQuery, rows: Rows) -> Check:
    """An ordered, limited query whose last row ties the first row past the
    limit on every ranking measure: re-run past the limit and every tied row
    is included, disclosed, never cut silently. A dimension in the order is a
    tiebreak, not a rank, unless the order holds no measure."""
    name, mode = "tie_at_limit", FailureMode.partial_ask
    lim = q.limit
    if not lim or not q.order or len(rows) < lim:
        return passed(name, mode)
    if ask.run is None:
        return Check(name, mode, "skip", "no executor to read past the limit")
    ext = ask.run(q.model_copy(update={"limit": lim + TIE_EXTRA}))
    keys = [k for k, _ in q.order if k in q.measures] or [k for k, _ in q.order]

    def kv(row: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(_cell(row.get(k)) for k in keys)

    if len(ext) <= lim or kv(ext[lim - 1]) != kv(ext[lim]):
        return passed(name, mode)
    n = lim
    while n < len(ext) and kv(ext[n]) == kv(ext[lim - 1]):
        n += 1
    tv = ", ".join(f"{k}={ext[lim - 1].get(k)}" for k in keys)
    capped = f" (only {TIE_EXTRA} rows past the limit were read)" if n == len(ext) else ""
    return Check(
        name,
        mode,
        "disclose",
        f"tie at the limit: rows {lim + 1}-{n} tie row {lim} on {tv}; the limit {lim} is "
        f"extended to {n} so no tied row is cut{capped}",
        rows=ext[:n],
    )


# ── wrong_grain: the level snapshot pin ─────────────────────────────────────


def snapshot_split(
    lens: CubeLensView, q: CubeQuery
) -> list[tuple[CubeQuery, str | None, str | None]]:
    """``[(query, pin, coarse grain)]``: a level measure (a stock: headcount,
    a contract book) summed over a window counts each snapshot once per
    period, so it is read at its snapshot grain on its own and reduced to the
    latest snapshot per bucket (``snapshot_reduce``). Flow measures stay as
    asked; a query already at or finer than the snapshot grain needs no pin."""
    lv = [m for m in q.measures if lens.level_grain(m)]
    if not lv:
        return [(q, None, None)]
    grain = lens.level_grain(lv[0]) or "month"
    tds = q.timeDimensions
    coarse: str | None = None
    if tds:
        g = tds[0].granularity
        if g in GRAINS and GRAINS.index(g) <= GRAINS.index(grain):
            return [(q, None, None)]
        dim = tds[0].dimension
        coarse = g
    else:
        dims = lens.time_dimensions(lens.cube_name(lv[0]))
        if not dims:
            return [(q, None, None)]
        dim = next((d for d in dims if d.endswith((".month", ".snapshot_date"))), dims[0])
    out: list[tuple[CubeQuery, str | None, str | None]] = []
    flow = [m for m in q.measures if m not in lv]
    if flow:
        out.append((q.model_copy(update={"measures": flow}), None, None))
    q2 = q.model_copy(deep=True)
    q2.measures = lv
    q2.limit = None
    if q2.timeDimensions:
        q2.timeDimensions[0].granularity = grain  # type: ignore[assignment]
    else:
        from services.contracts.cube_query import TimeDimension

        q2.timeDimensions = [TimeDimension(dimension=dim, granularity=grain)]  # type: ignore[arg-type]
    out.append((q2, f"{dim}.{grain}", coarse))
    return out


def _bucket(v: Any, grain: str | None) -> Any:
    if not grain or not v:
        return None
    d = date.fromisoformat(str(v)[:10])
    if grain == "year":
        return d.year
    if grain == "quarter":
        return (d.year, (d.month - 1) // 3)
    if grain == "month":
        return (d.year, d.month)
    if grain == "week":
        return tuple(d.isocalendar()[:2])
    return d


def snapshot_reduce(q: CubeQuery, rows: Rows, pin: str, coarse: str | None) -> Rows:
    """The latest snapshot per remaining dimension tuple (and per coarse
    bucket when the ask had a coarser grain)."""
    keydims = list(q.dimensions) + (
        ["compareDateRange"] if any("compareDateRange" in r for r in rows) else []
    )
    best: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        k = (*(row.get(d) for d in keydims), _bucket(row.get(pin), coarse))
        if k not in best or str(row.get(pin) or "") > str(best[k].get(pin) or ""):
            best[k] = row
    return list(best.values())


def snapshot_pin(ask: Ask, q: CubeQuery) -> Check:
    """The level pin as a check: disclosed when it reshapes a query."""
    name, mode = "snapshot_pin", FailureMode.wrong_grain
    pins = [p for _, p, _ in snapshot_split(ask.lens, q) if p]
    if pins:
        return Check(
            name,
            mode,
            "disclose",
            f"level: read at {pins[0]} and kept to the latest snapshot per bucket",
        )
    return passed(name, mode)


# ── data_gap: a comparison with one side empty ──────────────────────────────


def empty_window(ask: Ask, q: CubeQuery, rows: Rows) -> Check:
    """A comparison one of whose windows holds no value for any measure
    (nothing recorded yet for the current quarter, say): served for the
    windows that hold data, the empty one named, never a comparison against
    a blank."""
    name, mode = "empty_window", FailureMode.data_gap
    td = next((t for t in q.timeDimensions if t.compareDateRange), None)
    if td is None:
        return passed(name, mode)
    empty = []
    for window in td.compareDateRange or []:
        lab = " - ".join(str(x)[:10] for x in window) if isinstance(window, list) else str(window)
        mine = [r for r in rows if r.get("compareDateRange") == lab]
        if not any(r.get(m) not in (None, "") for r in mine for m in q.measures):
            empty.append(lab)
    if empty:
        return Check(
            name,
            mode,
            "disclose",
            f"data_gap: nothing is recorded for {', '.join(empty)} - "
            f"{', '.join(q.measures)} {'is' if len(q.measures) == 1 else 'are'} empty there, so "
            "the comparison has that side missing, not a change to it",
        )
    return passed(name, mode)


# ── compare: windows side by side ───────────────────────────────────────────


def compare_pivot(q: CubeQuery, rows: Rows) -> Rows:
    """A ``compareDateRange`` result (one row set per window, each row tagged
    with its window) as one row per group with each window's measures as
    columns, ``<measure> (<start> - <end>)``; a group missing from a window
    reads 0 there."""
    if not any(t.compareDateRange for t in q.timeDimensions):
        return rows
    labels: list[str] = []
    for r in rows:
        lab = str(r.get("compareDateRange"))
        if lab not in labels:
            labels.append(lab)
    td = next(t for t in q.timeDimensions if t.compareDateRange)
    for window in td.compareDateRange or []:
        lab = " - ".join(str(x)[:10] for x in window) if isinstance(window, list) else str(window)
        if lab not in labels:
            labels.append(lab)
    dims = [d for d in q.dimensions]
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = json.dumps([r.get(d) for d in dims], default=str)
        g = groups.setdefault(key, {d: r.get(d) for d in dims})
        for m in q.measures:
            g[f"{m} ({r.get('compareDateRange')})"] = r.get(m)
    out = []
    for g in groups.values():
        for lab in labels:
            for m in q.measures:
                g.setdefault(f"{m} ({lab})", 0)
        out.append(g)
    return out
