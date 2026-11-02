"""Data quality through Cube: dictionaries, data ends, feeds and partial periods.

Everything here is read through Cube's own ``/v1/load`` as a view member, never
from the model files and never by dst's SQL: what the monitor measures is the
data a governed answer would read.

``probe`` (``dst probe --cube``) fills ``profiles/cube.probe.json``:

- the value dictionary of every dimension that declares ``meta.dst.dictionary``
  (a dimension-only query, at most ``max_values``; complete when fewer came);
- the data end of every time dimension of the lens views: the latest day on or
  before the clock, and whether rows are dated after it. A dimension whose
  latest dates are all first-of-month days is month-grained (a text month
  cast to a date) and ends on its month's last day, capped at the clock;
- per cube clock (``meta.dst.clock`` / the overlay's ``default_time_dimension``)
  and per feed, the data end of each combination of the overlay's
  ``feed_axes`` (one table holding several systems' rows).

``monitor`` (``dst monitor``) adds the freshness file
(``profiles/cube.freshness.json``) and the findings, one failure mode each:

- ``stale``: a cube clock ending more than ``stale_after_days`` before the clock;
- ``feed_stopped`` (data_gap): one feed ending that long before while the
  cube's newest feed is fresh;
- ``future_rows`` (data_gap), per time dimension: rows dated after the clock
  on an event date (declared, or its name or description says so:
  ``date_meaning``), which every window clips; on a date nothing declares or
  explains, those rows make it a schedule date (``time_kind``), said once in
  the finding. No freshness, feed or partial period is read on a schedule
  date. Each dimension's kind and its basis land under ``time_dimensions``;
- ``partial_period`` (data_gap): per additive flow measure (sum, count,
  count_distinct on its clock; levels never) and per feed, recorded too on
  every other measure of the cube on that clock when the measure is one of
  the cube's judges (the overlay's ``feed_measures``, else its row counts), the latest month
  against the median of the ``TRAILING`` full months before it, pro-rated to
  the days the month is loaded: partial when the ratio is below
  ``PARTIAL_RATIO`` AND below every complete month in the series' own history
  (at least ``MIN_HISTORY`` months judged, a median of at least
  ``MIN_MEDIAN``), so a lumpy series' normal dips never count, nor a month
  that dipped alike a year earlier. A month that volume flags is then a
  partial load only on evidence beyond volume (``_Loading``): the feed's
  declared load stamp, an open period, its day coverage;
- ``unbound_value_new`` / ``unbound_value_vanished`` (unbound_value): a
  declared dictionary list that the warehouse no longer matches.

Every query spends from one budget; what the budget leaves unread is listed
under ``unread`` in the artifact and said on the command line, never dropped.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from services.contracts.cube_probe import NO_VALUE, date_meaning, kind_basis, time_kind
from services.contracts.cube_query import CubeFilter, CubeQuery, TimeDimension
from services.contracts.failure_modes import FailureMode
from services.contracts.lens_model import LensMember, LensModel

TRAILING = 6
PARTIAL_RATIO = 0.70
MIN_HISTORY = 6
MIN_MEDIAN = 20.0
HISTORY_MONTHS = 24
# A period loaded to its end has rows on its last days.
LAST_DAYS = 7

KIND_MODE: dict[str, FailureMode] = {
    "stale": FailureMode.stale,
    "feed_stopped": FailureMode.data_gap,
    "future_rows": FailureMode.data_gap,
    "partial_period": FailureMode.data_gap,
    "unbound_value_new": FailureMode.unbound_value,
    "unbound_value_vanished": FailureMode.unbound_value,
    "provider_down": FailureMode.provider_down,
}


class BudgetSpent(RuntimeError):
    pass


@dataclass
class Budget:
    max_queries: int
    used: int = 0
    unread: list[str] = field(default_factory=list)

    def take(self, what: str) -> bool:
        if self.used >= self.max_queries:
            self.unread.append(what)
            return False
        self.used += 1
        return True


class Loader:
    """``/v1/load`` as one context, under one budget. ``load`` is any callable
    taking a CubeQuery and returning the rows (the CubeClient's, or a fake)."""

    def __init__(self, load: Any, budget: Budget) -> None:
        self._load = load
        self.budget = budget
        self.errors: list[str] = []

    def rows(self, what: str, query: CubeQuery) -> list[dict[str, Any]] | None:
        if not self.budget.take(what):
            return None
        try:
            return list(self._load(query))
        except Exception as exc:  # noqa: BLE001 — a failed read is a finding, never a crash
            detail = getattr(exc, "detail", None) or f"{type(exc).__name__}: {exc}"
            self.errors.append(f"{what}: {detail}")
            return None


def _day(value: object) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def _month_end(d: date) -> date:
    nxt = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def _month_add(d: date, k: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + k, 12)
    return date(y, m + 1, 1)


def _end_of(day: date) -> str:
    return f"{day.isoformat()}T23:59:59.999"


# ── which member to ask through ───────────────────────────────────────────


def _via(models: Iterable[LensModel]) -> dict[str, LensMember]:
    """cube member → the first view member aliasing it, over every lens."""
    out: dict[str, LensMember] = {}
    for model in models:
        for m in model.members:
            out.setdefault(m.base, m)
    return out


def _in_view(models: Iterable[LensModel], view: str, base: str) -> str | None:
    for model in models:
        if (name := model.view_member_for(base, view)) is not None:
            return name
    return None


def _clocks(models: list[LensModel]) -> dict[str, str]:
    """cube → its clock (a cube member), declared only: the overlay's
    default_time_dimension, else the first measure's ``meta.dst.clock``."""
    out: dict[str, str] = {}
    for model in models:
        for cube in model.cubes:
            if cube.overlay.default_time_dimension:
                td = cube.overlay.default_time_dimension
                out.setdefault(cube.name, td if "." in td else f"{cube.name}.{td}")
        for m in model.measures():
            if m.measure and m.measure.clock and m.cube not in out:
                out[m.cube] = (
                    m.measure.clock if "." in m.measure.clock else f"{m.cube}.{m.measure.clock}"
                )
    return out


def _declared_kinds(models: list[LensModel]) -> dict[str, str]:
    """cube member → the overlay's ``clock_kind`` for it."""
    out: dict[str, str] = {}
    for model in models:
        for cube in model.cubes:
            for dim, kind in cube.overlay.clock_kind.items():
                out.setdefault(f"{cube.name}.{dim}", kind)
    return out


def _axis_value(value: object) -> str:
    """A feed axis value as the stream records it: ``NO_VALUE`` for none."""
    return NO_VALUE if value is None else str(value)


def _feed_axes(models: list[LensModel]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for model in models:
        for cube in model.cubes:
            if cube.overlay.feed_axes:
                out.setdefault(cube.name, list(cube.overlay.feed_axes))
    return out


# ── the probe ─────────────────────────────────────────────────────────────


def _data_end(
    loader: Loader,
    member: str,
    clock_end: date,
    extra: list[CubeFilter] | None = None,
    dims: list[str] | None = None,
) -> dict[str, Any] | None:
    td = TimeDimension(dimension=member, granularity="day")
    before = CubeFilter(member=member, operator="beforeOrOnDate", values=[_end_of(clock_end)])
    rows = loader.rows(
        f"data end {member}",
        CubeQuery(
            dimensions=dims or [],
            timeDimensions=[td],
            filters=[before, *(extra or [])],
            order=[(member, "desc")],
            limit=3,
        ),
    )
    if rows is None:
        return None
    days = [d for d in (_day(r.get(f"{member}.day") or r.get(member)) for r in rows) if d]
    out: dict[str, Any] = {"data_end": None, "grain": "day"}
    if days:
        # Month-grained: the latest dates are consecutive months' first days.
        monthly = (
            len(days) > 1
            and all(d.day == 1 for d in days)
            and all(_month_add(b, 1) == a for a, b in zip(days, days[1:], strict=False))
        )
        out["grain"] = "month" if monthly else "day"
        out["data_end"] = (min(_month_end(days[0]), clock_end) if monthly else days[0]).isoformat()
    after = CubeFilter(member=member, operator="afterDate", values=[_end_of(clock_end)])
    future = loader.rows(
        f"future rows {member}",
        CubeQuery(
            dimensions=dims or [], timeDimensions=[td], filters=[after, *(extra or [])], limit=1
        ),
    )
    out["future_rows"] = None if future is None else bool(future)
    return out


def probe(
    models: list[LensModel],
    load: Any,
    *,
    clock_end: date,
    max_queries: int = 200,
    max_values: int = 500,
    now: datetime | None = None,
) -> dict[str, Any]:
    """``profiles/cube.probe.json``: dictionaries and data ends through Cube."""
    loader = Loader(load, Budget(max_queries))
    via = _via(models)
    dictionaries: dict[str, list[str]] = {}
    complete: dict[str, bool] = {}
    for base, member in sorted(via.items()):
        dim = member.dimension
        if member.kind != "dimension" or dim is None or dim.dictionary in (None, False):
            continue
        if any(model.hidden(member) for model in models if model.member(member.name)):
            continue  # a hidden column's values are never collected
        rows = loader.rows(
            f"dictionary {base}",
            CubeQuery(dimensions=[member.name], order=[(member.name, "asc")], limit=max_values + 1),
        )
        if rows is None:
            continue
        values = sorted({str(r[member.name]) for r in rows if r.get(member.name) is not None})
        complete[base] = len(rows) <= max_values
        dictionaries[base] = values[:max_values]
    ends: dict[str, dict[str, Any]] = {}
    for base, member in sorted(via.items()):
        if member.kind == "dimension" and member.type == "time":
            got = _data_end(loader, member.name, clock_end)
            if got is not None:
                ends[base] = got
    streams: dict[str, list[dict[str, Any]]] = {}
    clocks = _clocks(models)
    for cube, axes in sorted(_feed_axes(models).items()):
        clock = clocks.get(cube)
        if clock is None or clock not in via:
            continue
        view = via[clock].view
        names = [_in_view(models, view, f"{cube}.{a}") for a in axes]
        if any(n is None for n in names):
            continue
        axis_members = [str(n) for n in names]
        combos = loader.rows(f"feeds {cube}", CubeQuery(dimensions=axis_members, limit=101))
        if combos is None:
            continue
        clock_member = via[clock].name
        for combo in combos[:100]:
            labels = {a: _axis_value(combo.get(n)) for a, n in zip(axes, axis_members, strict=True)}
            # Rows with no value on an axis are their own stream, read where
            # the axis is not set (left unpinned, they read every row).
            pin = [
                CubeFilter(member=n, operator="notSet")
                if combo.get(n) is None
                else CubeFilter(member=n, operator="equals", values=[str(combo.get(n))])
                for n in axis_members
            ]
            got = _data_end(loader, clock_member, clock_end, extra=pin)
            if got is not None:
                streams.setdefault(cube, []).append({"values": labels, "end": got["data_end"]})
    return {
        "probed_at": (now or datetime.now(UTC)).isoformat(),
        "clock_end": clock_end.isoformat(),
        "model_versions": {m.lens: m.model_version for m in models},
        "dictionaries": dictionaries,
        "dictionaries_complete": complete,
        "data_ends": ends,
        "streams": streams,
        "queries": loader.budget.used,
        "max_queries": max_queries,
        "unread": loader.budget.unread,
        "errors": loader.errors,
    }


# ── the monitor ───────────────────────────────────────────────────────────


def _finding(kind: str, member: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"mode": str(KIND_MODE[kind]), "kind": kind, "member": member, "detail": detail, **extra}


def monitor(
    models: list[LensModel],
    load: Any,
    *,
    clock_end: date,
    stale_after_days: int = 3,
    max_queries: int = 200,
    max_values: int = 500,
    probed: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """``(cube.probe.json, cube.freshness.json, findings)``. ``probed`` is a
    probe already run this pass (else one is)."""
    probe_doc = (
        dict(probed)
        if probed is not None
        else probe(
            models,
            load,
            clock_end=clock_end,
            max_queries=max_queries,
            max_values=max_values,
            now=now,
        )
    )
    left = max(max_queries - int(probe_doc.get("queries") or 0), 0)
    loader = Loader(load, Budget(left))
    via = _via(models)
    findings: list[dict[str, Any]] = []
    stale_before = clock_end - timedelta(days=stale_after_days)
    ends: dict[str, dict[str, Any]] = probe_doc.get("data_ends") or {}
    clocks = _clocks(models)
    kinds = _declared_kinds(models)
    # Every time dimension is an event or a schedule on the evidence: the
    # overlay's declaration, else what its name or prose says, else its
    # future rows. Future rows on an event date are a finding (the lane
    # clips every window on it); on an undeclared, unexplained date they
    # make it a schedule, said once, so a mistyped event is one line away.
    time_dims: dict[str, dict[str, Any]] = {}
    for dim, end_rec in sorted(ends.items()):
        lm = via.get(dim)
        meaning = date_meaning(dim.split(".", 1)[1], lm.title, lm.description) if lm else None
        future = bool(end_rec.get("future_rows"))
        said = kinds.get(dim)
        kind = time_kind(said, future, meaning)
        basis = kind_basis(said, meaning)
        time_dims[dim] = {"kind": kind, "basis": basis, "future_rows": future}
        if not future:
            continue
        cube_name, _, td = dim.partition(".")
        if kind == "event":
            findings.append(
                _finding(
                    "future_rows",
                    dim,
                    f"rows dated after {clock_end.isoformat()} on an event date "
                    f"({'declared' if basis == 'declared' else 'its name or description says'}"
                    f" so): they cannot have happened yet, so every window on {dim} is "
                    "clipped to the lens clock, never counted",
                    kind_basis=basis,
                )
            )
        elif basis == "future_rows":
            findings.append(
                _finding(
                    "future_rows",
                    dim,
                    f"rows dated after {clock_end.isoformat()}: read as a schedule date "
                    f"(due or end, never clipped); if they are events dated wrong, "
                    f"declare cubes.{cube_name}.clock_kind: {{{td}: event}} in the overlay",
                    kind_basis=basis,
                )
            )
    schedules = {m for m, t in time_dims.items() if t["kind"] == "schedule"}
    cubes: dict[str, dict[str, Any]] = {}
    for cube, clock in sorted(clocks.items()):
        end = (ends.get(clock) or {}).get("data_end")
        kind = (time_dims.get(clock) or {}).get("kind") or time_kind(kinds.get(clock), False)
        entry: dict[str, Any] = {"clock": clock, "data_end": end, "kind": kind}
        if kind == "schedule":
            # A due or end date says when something is due, not how far the
            # data is loaded: no freshness, feed or partial period is read on it.
            cubes[cube] = entry
            continue
        # A month-grained clock is current through the last complete month.
        due = stale_before
        if (ends.get(clock) or {}).get("grain") == "month":
            due = min(stale_before, date(clock_end.year, clock_end.month, 1) - timedelta(days=1))
        streams = (probe_doc.get("streams") or {}).get(cube) or []
        if streams:
            entry["streams"] = streams
            newest = max((s["end"] for s in streams if s.get("end")), default=None)
            for s in streams:
                if s.get("end") and newest and newest >= stale_before.isoformat() > s["end"]:
                    s["stopped"] = True
                    findings.append(
                        _finding(
                            "feed_stopped",
                            clock,
                            f"ends {s['end']} while the newest feed of {cube} runs to {newest}",
                            stream=s["values"],
                        )
                    )
        if end and end < due.isoformat():
            findings.append(
                _finding(
                    "stale",
                    clock,
                    f"data ends {end}, more than {stale_after_days} day(s) before "
                    f"{clock_end.isoformat()}",
                )
            )
        cubes[cube] = entry
    measures: dict[str, dict[str, Any]] = {}
    axes_by_cube = _feed_axes(models)
    for base, member in sorted(via.items()):
        if member.kind != "measure" or member.type not in ("sum", "count", "count_distinct"):
            continue
        if member.measure and member.measure.additivity == "level":
            continue
        model = next(m for m in models if m.member(member.name) is not None)
        measure_clock = model.clock_of(member)
        if measure_clock is None:
            continue
        clock = measure_clock
        if clock in schedules or (
            clock not in time_dims and time_kind(kinds.get(clock), False) == "schedule"
        ):
            continue  # how many contracts end in a month is no measure of loading
        end = _day((ends.get(clock) or {}).get("data_end"))
        clock_member = _in_view(models, member.view, clock)
        if end is None or clock_member is None:
            continue
        axes = [a for a in axes_by_cube.get(member.cube, []) if clock.split(".")[0] == member.cube]
        axis_members = [_in_view(models, member.view, f"{member.cube}.{a}") for a in axes]
        if any(a is None for a in axis_members):
            axes, axis_members = [], []
        start = _month_add(end, -HISTORY_MONTHS)
        rows = loader.rows(
            f"partial {base}",
            CubeQuery(
                measures=[member.name],
                dimensions=[str(a) for a in axis_members],
                timeDimensions=[
                    TimeDimension(
                        dimension=clock_member,
                        granularity="month",
                        dateRange=[start.isoformat(), _end_of(clock_end)],
                    )
                ],
            ),
        )
        if rows is None:
            continue
        series: dict[tuple[object, ...], dict[str, float]] = {}
        for r in rows:
            key = tuple(r.get(str(a)) for a in axis_members)
            month = str(r.get(f"{clock_member}.month") or "")[:7]
            series.setdefault(key, {})[month] = float(r.get(member.name) or 0)
        rec: dict[str, Any] = {"cube": member.cube, "clock": clock}
        streams = cubes.get(member.cube, {}).get("streams") or []
        overlay = next((c.overlay for m in models for c in m.cubes if c.name == member.cube), None)
        loaded_at = overlay.loaded_at if overlay else None
        matures = overlay.matures_after.get(base.split(".", 1)[-1]) if overlay else None
        evidence = _Loading(
            loader,
            member.name,
            clock_member,
            [str(a) for a in axis_members],
            clock_end,
            month_grained=(ends.get(clock) or {}).get("grain") == "month",
            loaded_at=_in_view(models, member.view, f"{member.cube}.{loaded_at}")
            if loaded_at
            else None,
        )
        for key, values in sorted(series.items(), key=lambda kv: str(kv[0])):
            labels = {a: _axis_value(v) for a, v in zip(axes, key, strict=True)}
            stream = next(
                (
                    s
                    for s in streams
                    if all(str(s["values"].get(a)) == str(v) for a, v in labels.items())
                ),
                None,
            )
            through = _day((stream or {}).get("end")) or end
            judged = _partial(values, start, through)
            if judged is None:
                continue
            why, verified = evidence.why_partial(key, judged, through)
            if why is None:
                # Loaded to the period's end: a low month, not a partial load.
                # Declared to mature, and not yet matured: complete, still maturing.
                final = _month_end(date.fromisoformat(judged["partial_from"])) + timedelta(
                    days=matures or 0
                )
                if matures is None or final < clock_end:
                    continue
                why = (
                    f"complete; values still maturing ({base}: final {matures} days after the "
                    "period ends)"
                )
                judged["maturing"] = base
            judged["evidence"] = why
            judged["load_evidence"] = verified and "maturing" not in judged
            if labels:
                rec.setdefault("partial_streams", []).append({"values": labels, **judged})
            else:
                rec["partial"] = judged
            findings.append(
                _finding(
                    "partial_period",
                    base,
                    f"{judged['period']} is {judged['partial_ratio']:.0%} of the trailing "
                    f"{TRAILING}-month median ({judged['value']:,.0f} vs "
                    f"{judged['trailing_median']:,.0f}), loaded to {judged['partial_through']}"
                    f"; {judged['evidence']}",
                    **({"stream": labels} if labels else {}),
                )
            )
        measures[base] = rec
    _judge_feeds(models, via, measures)
    for base, values in sorted((probe_doc.get("dictionaries") or {}).items()):
        dim_member = via.get(base)
        declared = (
            dim_member.dimension.declared_values
            if dim_member is not None and dim_member.dimension
            else None
        )
        if declared is None or not (probe_doc.get("dictionaries_complete") or {}).get(base):
            continue
        new = sorted(set(values) - set(declared))
        gone = sorted(set(declared) - set(values))
        if new:
            findings.append(_finding("unbound_value_new", base, ", ".join(new[:20])))
        if gone:
            findings.append(_finding("unbound_value_vanished", base, ", ".join(gone[:20])))
    for error in [*probe_doc.get("errors", []), *loader.errors]:
        findings.append(_finding("provider_down", "cube", error))
    freshness = {
        "as_of": (now or datetime.now(UTC)).isoformat(),
        "clock_end": clock_end.isoformat(),
        "stale_after_days": stale_after_days,
        "model_versions": {m.lens: m.model_version for m in models},
        "cubes": cubes,
        "time_dimensions": time_dims,
        "measures": measures,
        "findings": findings,
        "queries": int(probe_doc.get("queries") or 0) + loader.budget.used,
        "max_queries": max_queries,
        "unread": [*probe_doc.get("unread", []), *loader.budget.unread],
    }
    return probe_doc, freshness, findings


class _Loading:
    """Whether a period the volume rule finds short is a partial load: the
    evidence beyond volume, read only for a period the rule flags. The
    feed's declared load stamp decides when there is one (loaded after the
    period ended: complete); else an open period is partial; else its day
    coverage — rows on its last days, its second half loaded like its first.
    With none of it readable, volume alone flags, and says so."""

    def __init__(
        self,
        loader: Loader,
        measure: str,
        clock: str,
        axes: list[str],
        clock_end: date,
        *,
        month_grained: bool,
        loaded_at: str | None,
    ) -> None:
        self.loader, self.measure, self.clock, self.axes = loader, measure, clock, axes
        self.clock_end, self.month_grained, self.loaded_at = clock_end, month_grained, loaded_at
        self._read: dict[str, dict[tuple[object, ...], dict[date, float]] | None] = {}

    def _by_day(
        self, what: str, query: CubeQuery, member: str, value: str | None
    ) -> dict[tuple[object, ...], dict[date, float]] | None:
        if what not in self._read:
            rows = self.loader.rows(f"{what} {self.measure}", query)
            out: dict[tuple[object, ...], dict[date, float]] | None = None
            if rows is not None:
                out = {}
                for r in rows:
                    d = _day(r.get(f"{member}.day") or r.get(member))
                    if d is not None:
                        key = tuple(r.get(a) for a in self.axes)
                        series = out.setdefault(key, {})
                        series[d] = series.get(d, 0.0) + float((r.get(value) or 0) if value else 1)
            self._read[what] = out
        return self._read[what]

    def why_partial(
        self, key: tuple[object, ...], judged: dict[str, Any], through: date
    ) -> tuple[str | None, bool]:
        """``(why, verified)``: why the period is a partial load, or None
        when the evidence says it is loaded to its end; ``verified`` False
        when no evidence could be read and volume alone flags it."""
        first = date.fromisoformat(judged["partial_from"])
        last = _month_end(first)
        window = [first.isoformat(), _end_of(last)]
        if self.loaded_at is not None:
            stamps = self._by_day(
                "last load",
                CubeQuery(
                    dimensions=self.axes,
                    timeDimensions=[TimeDimension(dimension=self.loaded_at, granularity="day")],
                    filters=[CubeFilter(member=self.clock, operator="inDateRange", values=window)],
                ),
                self.loaded_at,
                None,
            )
            if stamps is not None and stamps.get(key):
                loaded = max(stamps[key])
                if loaded > last:
                    return None, True
                return (
                    f"last loaded {loaded.isoformat()}, before the period ends {last.isoformat()}",
                    True,
                )
        if self.clock_end < last:
            return (
                f"the period is still open: loaded to {through.isoformat()} of {last.isoformat()}",
                True,
            )
        if self.month_grained:
            return "judged on volume alone: a month-grained clock has no day coverage", False
        days = self._by_day(
            "day coverage",
            CubeQuery(
                measures=[self.measure],
                dimensions=self.axes,
                timeDimensions=[
                    TimeDimension(dimension=self.clock, granularity="day", dateRange=window)
                ],
            ),
            self.clock,
            self.measure,
        )
        if days is None:
            return "judged on volume alone: the day coverage could not be read", False
        got = {d: v for d, v in (days.get(key) or {}).items() if v}
        tail = last - timedelta(days=LAST_DAYS - 1)
        if not any(d >= tail for d in got):
            return (
                f"no rows on the period's last {LAST_DAYS} days (loaded to {through.isoformat()})",
                True,
            )
        mid = first + timedelta(days=(last - first).days // 2)
        early = sum(v for d, v in got.items() if d <= mid) / ((mid - first).days + 1)
        late = sum(v for d, v in got.items() if d > mid) / (last - mid).days
        if late < PARTIAL_RATIO * early:
            return (
                f"the period's second half is loaded at {late / early:.0%} of its first's daily "
                "rate",
                True,
            )
        return None, True


def _judge_feeds(
    models: list[LensModel], via: Mapping[str, LensMember], measures: dict[str, dict[str, Any]]
) -> None:
    """A feed's partial period is every measure's on that feed: the periods
    the cube's judges (its ``feed_measures``, else its row counts) find
    partial are recorded on each other measure of the cube on the same
    clock, ``judged_by`` the judge. A level is never a flow, so never one."""
    declared: dict[str, list[str]] = {}
    for model in models:
        for lc in model.cubes:
            if lc.overlay.feed_measures:
                declared.setdefault(lc.name, [f"{lc.name}.{m}" for m in lc.overlay.feed_measures])
    by_cube: dict[str, list[LensMember]] = {}
    for _base, member in sorted(via.items()):
        if member.kind == "measure":
            by_cube.setdefault(member.cube, []).append(member)
    for cube, members in by_cube.items():
        judges = declared.get(cube) or [m.base for m in members if m.type == "count"]
        for judge in judges:
            rec = measures.get(judge)
            if rec is None or not (rec.get("partial") or rec.get("partial_streams")):
                continue
            for member in members:
                if member.base == judge or (
                    member.measure and member.measure.additivity == "level"
                ):
                    continue
                model = next(m for m in models if m.member(member.name) is not None)
                if model.clock_of(member) != rec["clock"]:
                    continue
                target = measures.setdefault(
                    member.base, {"cube": cube, "clock": rec["clock"], "type": member.type}
                )
                if rec.get("partial") and not target.get("partial"):
                    target["partial"] = {**rec["partial"], "judged_by": judge}
                have = [p.get("values") for p in target.get("partial_streams") or []]
                for p in rec.get("partial_streams") or []:
                    if p.get("values") not in have:
                        target.setdefault("partial_streams", []).append({**p, "judged_by": judge})


def _partial(values: Mapping[str, float], start: date, through: date) -> dict[str, Any] | None:
    months: list[date] = []
    m = start
    while m <= through:
        months.append(m)
        m = _month_add(m, 1)
    vals = [values.get(x.strftime("%Y-%m"), 0.0) for x in months]
    while vals and vals[0] == 0:  # a series starts at its first month with a value
        vals.pop(0)
        months.pop(0)
    if len(vals) < TRAILING + 2 or any(v < 0 for v in vals):
        return None
    own: list[float] = []
    for i in range(TRAILING, len(months) - 1):
        med = statistics.median(vals[i - TRAILING : i])
        if med >= MIN_MEDIAN:
            own.append(vals[i] / med)
    if len(own) < MIN_HISTORY:
        return None
    last = months[-1]
    nxt = _month_add(last, 1)
    covered = ((through - last).days + 1) / (nxt - last).days
    med = statistics.median(vals[-1 - TRAILING : -1])
    if med < MIN_MEDIAN or covered <= 0:
        return None
    ratio = vals[-1] / (med * covered)
    if not (ratio < PARTIAL_RATIO and ratio < min(own)):
        return None
    ly = len(vals) - 13  # the same month a year earlier, scaled to this year's level
    if ly >= TRAILING and vals[ly] > 0:
        med_ly = statistics.median(vals[ly - TRAILING : ly])
        if med_ly >= MIN_MEDIAN and vals[-1] / (vals[ly] * med / med_ly * covered) >= PARTIAL_RATIO:
            return None  # a seasonal low: the same month dipped alike last year
    return {
        "period": last.strftime("%Y-%m"),
        "partial_from": last.isoformat(),
        "partial_through": through.isoformat(),
        "partial_ratio": round(ratio, 3),
        "trailing_median": round(med, 2),
        "value": round(vals[-1], 2),
        "trailing_months": TRAILING,
        "history_min_ratio": round(min(own), 3),
    }


def lens_findings(model: LensModel, findings: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The findings on members of this lens's cubes (Cube-wide ones too)."""
    cubes = {c.name for c in model.cubes}
    return [f for f in findings if f["member"] == "cube" or f["member"].split(".")[0] in cubes]


def summary(freshness: Mapping[str, Any], *, limit: int = 50) -> dict[str, Any]:
    """The ``data_quality`` block: when the monitor ran, its counts by mode
    and kind, and the findings themselves up to ``limit``."""
    findings = list(freshness.get("findings") or [])
    by_mode: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for f in findings:
        by_mode[f["mode"]] = by_mode.get(f["mode"], 0) + 1
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
    return {
        "as_of": freshness.get("as_of"),
        "clock_end": freshness.get("clock_end"),
        "findings": len(findings),
        "by_mode": dict(sorted(by_mode.items())),
        "by_kind": dict(sorted(by_kind.items())),
        "unread": len(freshness.get("unread") or []),
        "latest": findings[:limit],
    }
