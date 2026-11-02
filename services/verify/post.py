"""The checks on what the data holds: declared freshness and partial periods
(read from the probe file before a query runs, so a window with nothing
loaded is never executed), and the result checks after it ran: an empty
filtered result, an empty bucket ranked, a tie cut at the limit, a level
measure read across snapshots, a comparison's windows set side by side.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from services.contracts.cube_probe import PartialPeriod
from services.contracts.cube_query import CubeFilter, CubeQuery, TimeDimension
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
        if lens.level_grain(m):
            # A stock is read at its latest snapshot in or before the window
            # (``snapshot_split``), which says when that is: a window after
            # the data end is not a gap in a level.
            continue
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
    """A window that includes a period the monitor found short is served
    and disclosed: which period, how complete, judged by which measure, what
    the evidence says (partially loaded; complete with values still
    maturing; or volume alone) and — when the same measure is read over
    complete periods beside it — that the comparison sets it against
    complete ones. dst owns the fact; how to frame the period is the
    caller's judgment, so it never refuses."""
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
        ratio = (
            f"{p.partial_ratio:.0%} of its trailing {p.trailing_months}-month median, judged by "
            f"{p.judged_by or m} ({p.value:,.0f} vs {p.trailing_median:,.0f})"
        )
        # The wording follows the evidence: "partially loaded" only on load
        # evidence; a complete period declared to mature says so; volume
        # alone states the ratio and claims nothing about the load.
        if p.maturing:
            kind, body = (
                "maturing",
                f"is complete (loaded to {p.partial_through}) but its values are still "
                f"maturing ({p.maturing}): at {ratio}",
            )
        elif p.load_evidence:
            kind, body = (
                "partial",
                f"is partially loaded: at {ratio}, loaded to {p.partial_through}; {p.evidence}",
            )
        else:
            kind, body = (
                "partial",
                f"is at {ratio}, loaded to {p.partial_through}"
                + (f"; {p.evidence}" if p.evidence else ""),
            )
        notes.append(
            f"data_gap: {kind} period - {m}{st} for {period} {body}: a window that includes "
            f"{period} understates it"
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


def _dict_filters(lens: CubeLensView, q: CubeQuery) -> list[CubeFilter]:
    return [
        f
        for f in all_filters(q)
        if f.member and f.operator == "equals" and lens.dictionary(f.member) is not None
    ]


def _unverified(ask: Ask, q: CubeQuery, filters: list[CubeFilter]) -> list[str]:
    """Why a zero could not be told from nothing recorded: a filter value
    the dictionary lacks, no window asked, a measure with no recorded data
    end, or a window running past a feed's data end. Empty: a measured 0."""
    lens = ask.lens
    why: list[str] = []
    for f in filters:
        known = {str(v).lower() for v in lens.dictionary(f.member or "") or []}
        bad = [v for v in f.values or [] if str(v).lower() not in known]
        if bad:
            why.append(f"{', '.join(bad)} is not a value of {f.member} the probe holds")
    spans = [(s, e) for s, e in windows(q, lens) if s]
    if not spans:
        why.append("no window was asked, so a zero over all loaded data cannot be told apart")
    for m in q.measures:
        sts = streams_read(lens, q, m)
        if not sts:
            why.append(f"the probe records no data end for {m}")
            continue
        for label, end, _stopped in sts:
            for _s, e in spans:
                if end is None:
                    why.append(f"{label} has no recorded data end")
                elif end < e:
                    why.append(f"{label} data ends {end}, before the window ends {e}")
    return list(dict.fromkeys(why))


def empty_result(ask: Ask, q: CubeQuery, rows: Rows) -> Check:
    """A query filtered on dictionary values whose every measure came back
    empty or zero. Over a window every feed read is loaded through, with
    every filter value in its dictionary, that is a measured 0: served with
    the window stated and disclosed. Otherwise nothing was recorded, which
    is not a measured 0, and the query is refused saying what could not be
    verified."""
    name, mode = "empty_result", FailureMode.empty_result
    filters = _dict_filters(ask.lens, q)
    if not filters or not q.measures:
        return passed(name, mode)
    if not all(all(fnum(r.get(m)) == 0 for m in q.measures if m in r) for r in rows):
        return passed(name, mode)
    shown = "; ".join(f"{f.member} = {', '.join(f.values or [])}" for f in filters)
    why = _unverified(ask, q, filters)
    if why:
        return Check(
            name,
            mode,
            "refuse",
            f"no rows match {shown} in the window - nothing was recorded, which is not the "
            f"same as a measured 0 ({'; '.join(why)})",
        )
    spans = ", ".join(f"{s}..{e}" for s, e in windows(q, ask.lens))
    return Check(
        name,
        mode,
        "disclose",
        f"empty_result: a measured 0 - no rows match {shown} in {spans}; every feed read is "
        "loaded through the window",
    )


# ── data_gap: a window nothing is loaded for came back empty ────────────────


def unloaded_window(ask: Ask, q: CubeQuery, rows: Rows) -> Check:
    """A flow read over a window that starts after every feed's data end
    (``today`` with the data loaded through yesterday) and came back with no
    rows, or none with a value: nothing was loaded for it, which is not a
    measured 0, so it is refused naming where the data ends. A value in the
    rows wins over the probe: the data is there, whatever it recorded."""
    name, mode = "loaded", FailureMode.data_gap
    lens = ask.lens
    flows = [m for m in q.measures if not lens.level_grain(m)]
    if not flows:
        return passed(name, mode)
    if any(fnum(r.get(m)) != 0 for r in rows for m in flows if r.get(m) not in (None, "")):
        return passed(name, mode)
    notes: list[str] = []
    for m in flows:
        sts = streams_read(lens, q, m)
        if not sts:
            continue
        for start, end in windows(q, lens):
            if start and all(e is None or e < start for _, e, _ in sts):
                ends = "; ".join(f"{lbl} data ends {e}" for lbl, e, _ in sts)
                notes.append(
                    f"data_gap: nothing is loaded for {start}..{end} ({ends}); {m} came back "
                    "empty there, which is not a measured 0"
                )
    if notes:
        return Check(name, mode, "refuse", "; ".join(dict.fromkeys(notes)))
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


@dataclass
class Split:
    """One query the lane runs for a level measure: ``pin`` the time column
    the rows are reduced on (``<dim>.<grain>``), ``coarse`` the asked grain
    when it was coarser than the snapshot grain, ``note`` what the SQL did
    (the disclosure), ``mode`` the mode that note carries (``data_gap`` when
    the asked window held no snapshot), ``day`` the one day the query was
    pinned to (the clock's data end), after which an empty result is a
    fault in the probe, not a figure."""

    query: CubeQuery
    pin: str | None = None
    coarse: str | None = None
    note: str = ""
    mode: FailureMode = FailureMode.wrong_grain
    day: str | None = None


def snapshot_split(lens: CubeLensView, q: CubeQuery) -> list[Split]:
    """A level measure (a stock: headcount, a contract book, accounts active
    on a day) summed over a window counts each snapshot once per period, so
    it is read on its own and reduced to the latest snapshot per bucket
    (``snapshot_reduce``). With a window, at its snapshot grain within the
    window. With NO window, pinned to the one day the clock's data ends
    (the latest snapshot), at day grain: a month bucket would add or union
    every snapshot of the open month. A window that starts after the data
    end (``today``, with snapshots through yesterday) reads that latest
    snapshot too and says so. Flow measures stay as asked; a query already
    at or finer than the snapshot grain needs no pin."""
    lv = [m for m in q.measures if lens.level_grain(m)]
    if not lv:
        return [Split(q)]
    grain = lens.level_grain(lv[0]) or "month"
    tds = q.timeDimensions
    if not tds and all(lens.creation_dated(m) for m in lv):
        # One row per record: the table's newest day is no snapshot of it.
        # With no period asked, every row is the current state.
        return [Split(q)]
    coarse: str | None = None
    windowed = bool(tds and (tds[0].dateRange or tds[0].compareDateRange))
    if tds:
        g = tds[0].granularity
        if g in GRAINS and GRAINS.index(g) <= GRAINS.index(grain):
            return [Split(q)]
        dim = tds[0].dimension
        coarse = g
    else:
        clock = lens.clock(lv[0])
        dims = lens.time_dimensions(lens.cube_name(lv[0]))
        if clock and clock in dims:
            dim = clock
        elif dims:
            dim = next((d for d in dims if d.endswith((".month", ".snapshot_date"))), dims[0])
        else:
            return [Split(q)]
    out: list[Split] = []
    flow = [m for m in q.measures if m not in lv]
    if flow:
        out.append(Split(q.model_copy(update={"measures": flow})))
    q2 = q.model_copy(deep=True)
    q2.measures = lv
    q2.limit = None
    facts = lens.time_facts(dim)
    data_end = facts.data_end if facts else None
    reads = ", ".join(lv)
    per = f"per {', '.join(q.dimensions)}" if q.dimensions else "one row"
    if windowed and not tds[0].compareDateRange:
        rng = tds[0].dateRange
        start = str(rng[0])[:10] if isinstance(rng, list) and rng else None
        if start and data_end and start > data_end:
            # The asked window holds no snapshot yet: the latest one is the
            # figure, said as such (today on a level = the latest snapshot).
            q2.timeDimensions = [
                TimeDimension(dimension=dim, dateRange=[data_end, data_end], granularity="day")
            ]
            end = str(rng[-1])[:10] if isinstance(rng, list) else start
            note = (
                f"level: {start}..{end} holds no snapshot of {reads} yet ({dim} data ends "
                f"{data_end}); the SQL reads {dim} on {data_end}, the latest snapshot, at day "
                f"grain, {per}"
            )
            out.append(Split(q2, f"{dim}.day", None, note, FailureMode.data_gap, data_end))
            return out
    if windowed:
        q2.timeDimensions[0].granularity = grain  # type: ignore[assignment]
        note = f"level: read at {dim}.{grain} and kept to the latest snapshot per bucket"
        out.append(Split(q2, f"{dim}.{grain}", coarse, note))
        return out
    if data_end:
        q2.timeDimensions = [
            TimeDimension(dimension=dim, dateRange=[data_end, data_end], granularity="day")
        ]
        note = (
            f"level: no period asked, so the SQL reads {reads} on {data_end}, the latest loaded "
            f"day of {dim}, at day grain ({per}); a bucket of the open month would add or union "
            "every snapshot in it"
        )
        out.append(Split(q2, f"{dim}.day", None, note, FailureMode.wrong_grain, data_end))
        return out
    q2.timeDimensions = [TimeDimension(dimension=dim, granularity="day")]
    note = (
        f"level: no period asked and no data end recorded for {dim}, so the SQL reads {reads} "
        f"at day grain over every day and the latest day is kept ({per})"
    )
    out.append(Split(q2, f"{dim}.day", None, note))
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
    """The level pin as a check: disclosed when it reshapes a query, saying
    what the SQL that ran did."""
    name, mode = "snapshot_pin", FailureMode.wrong_grain
    pinned = [sp for sp in snapshot_split(ask.lens, q) if sp.pin]
    if pinned:
        return Check(name, pinned[0].mode, "disclose", pinned[0].note)
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
