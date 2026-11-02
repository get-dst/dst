"""The doors an agent drives with: its own Cube query, and the facts it reads first.

  POST /v1/lenses/{name}/cube       query_cube: the agent's Cube query + word ledger
  GET  /v1/lenses/{name}/values     the observed values of a member or column
  GET  /v1/lenses/{name}/data_end   where a member's or a table's data ends
  GET  /v1/grain                    an exposed table's declared grain, and its count

None of them calls a model, whoever the caller is: the agent did the reading,
dst runs the checks (``services/verify``), compiles through Cube, executes
through the lens's own connection and signs the receipt. A query a check
stops comes back with the mode and what to change (``retry``), so the agent
can submit it again. Every door goes through ``_authorized_bundle``: the
lens's access, rate limit and audit hold as on the question doors.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from services.api import route_store
from services.api.query import (
    _audience,
    _authorized_bundle,
    _authorized_connector,
    _shaped,
)
from services.api.sql import _grain_lines
from services.auth.deps import get_caller
from services.contracts.cube_query import CubeQuery
from services.contracts.lens_config import LensConfig
from services.contracts.profile import TableProfile
from services.contracts.response import ConsumerAnswer, QueryResponse
from services.contracts.semantic_model import Entity
from services.cube.context import CubeContext
from services.cube.lens_view import CubeLensView
from services.db.session import org_session
from services.governance.credentials import CallerIdentity
from services.governance.policy import authorize
from services.lenses.describe import ExposedTableInfo, _exposed_info
from services.lenses.store import LensBundle, list_published_for_org
from services.observability import usage as ai_usage
from services.observability.logger import log_trace
from services.project.cube_plan import stored_lens_model
from services.resolve.frontier import read_ledger
from services.runtime import assembly
from services.runtime.attribution import Attribution, attributed
from services.runtime.explore_checks import clock_columns
from services.serve import door as cube_door
from services.serve.driver import Retry, retry
from services.verify.base import Ask
from services.verify.pre import hidden_column, not_declared, scope_violation

log = logging.getLogger("dst")

router = APIRouter(prefix="/v1", tags=["query"])

# Values listed by the values door at most.
VALUES_MAX = 200


class CubeBody(BaseModel):
    query: CubeQuery = Field(description="the Cube query (the /v1/load body) to run")
    ledger: list[dict[str, Any]] | None = Field(
        default=None,
        description='how each content word of the question is carried: [{"word": "<as '
        'asked>", "to": "member" | "filter" | "segment" | "period" | "none", "ref": "<the '
        'member, filter value, segment or date range of the query>"}]',
    )
    question: str = Field(
        default="",
        max_length=4000,
        description="the user's question as asked: the ledger is checked against its words, "
        "a scope it did not ask for is refused and its periods are bound to the lens clock. "
        "Without it those checks report UNTESTED",
    )
    route_id: str | None = Field(
        default=None,
        max_length=64,
        description="the request_id route_query returned for the question this query answers: "
        "an ambiguous term's select words are read from the question recorded there, never "
        "from `question`. Without it no select word applies: the term's default reading is "
        "served and the others disclosed",
    )


class DriverAnswer(QueryResponse):
    """The served answer or the non-answer, as every lens door returns it,
    plus ``retry`` on a non-answer: the check that stopped the query, its
    mode, and what to change."""

    retry: Retry | None = None


def _cube_lens(name: str, caller: CallerIdentity, rid: str) -> LensBundle:
    bundle = _authorized_bundle(name, caller, rid)
    if cube_door.serving_core(bundle.config, bundle.semantic_model) != "cube":
        raise HTTPException(
            status_code=400,
            detail=f"lens '{name}' does not serve through Cube; ask it with query or query_metrics",
        )
    return bundle


def _view(name: str, bundle: LensBundle, caller: CallerIdentity, rid: str) -> CubeLensView:
    try:
        _client, view = cube_door.open_view(
            name,
            bundle.config,
            ctx=CubeContext.of(caller, lens=name, request_id=rid),
            stored=stored_lens_model(bundle.semantic_model.cube),
        )
    except cube_door.CubeUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return view


def _recorded_question(caller: CallerIdentity, route_id: str | None) -> str | None:
    """The question route_query recorded under ``route_id`` for this org,
    the text an ambiguous term's select words are read from in driver mode.
    None when nothing was recorded under it: no select word applies."""
    if not route_id:
        return None
    try:
        with org_session(caller.org_id) as session:
            found = route_store.by_id(session, route_id.strip())
    except Exception:  # noqa: BLE001 — an unreadable route selects nothing, loud in the log
        log.exception("recorded route question unavailable")
        return None
    return found.question if found is not None else None


def run_cube(
    name: str, body: CubeBody, caller: CallerIdentity, background: BackgroundTasks
) -> QueryResponse | ConsumerAnswer:
    """The driver's query through the cube lane: every pre-check, Cube's
    compile, the guard, dst's execution, every post-check; no model call."""
    rid = "req_" + uuid.uuid4().hex[:16]
    ledger = read_ledger(body.ledger) if body.ledger is not None else None
    if body.ledger is not None and ledger is None:
        raise HTTPException(
            status_code=422,
            detail='ledger: each entry is {"word", "to", "ref"} with "to" one of member, '
            "filter, segment, period, none",
        )
    bundle = _cube_lens(name, caller, rid)
    try:
        connector = _authorized_connector(bundle, caller)
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"lens connection error: {exc}") from exc
    with ai_usage.no_models():
        try:
            lane = cube_door.open_lane(
                name,
                bundle.config,
                connector=connector,
                dialect=bundle.semantic_model.dialect,
                ctx=CubeContext.of(caller, lens=name, request_id=rid),
                stored=stored_lens_model(bundle.semantic_model.cube),
                today=assembly.business_today_date(bundle.config.timezone),
                org_id=str(caller.org_id),
                caller=caller.name,
                groups=sorted(caller.groups),
                request_id=rid,
            )
        except cube_door.CubeUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        with attributed(
            Attribution(
                principal=caller.name,
                agent=caller.agent,
                request_id=rid,
                org_id=str(caller.org_id),
            )
        ):
            response, trace = cube_door.answer_driver(
                lane,
                body.query,
                ledger,
                question=body.question,
                recorded=_recorded_question(caller, body.route_id),
            )
    trace.agent = caller.agent
    background.add_task(log_trace, trace)
    shaped = _shaped(response, "both", _audience(caller))
    if isinstance(shaped, ConsumerAnswer):
        return shaped
    return DriverAnswer(**shaped.model_dump(), retry=retry(shaped))


@router.post("/lenses/{name}/cube", response_model=DriverAnswer | ConsumerAnswer)
def query_cube(
    name: str,
    body: CubeBody,
    background: BackgroundTasks,
    caller: CallerIdentity = Depends(get_caller),
) -> QueryResponse | ConsumerAnswer:
    """Run an agent's own Cube query, with the word ledger for the question it
    answers. No model is called; every check runs."""
    return run_cube(name, body, caller, background)


# ── values / data_end ───────────────────────────────────────────────────────


class ValuesResult(BaseModel):
    lens: str
    member: str
    values: list[str]
    total: int | None = Field(default=None, description="values the source holds in all")
    complete: bool = Field(description="the list is every value the member holds")
    source: str
    note: str = ""


class DataEndResult(BaseModel):
    lens: str
    member: str
    kind: str | None = Field(default=None, description="event, schedule, measure, table or cube")
    data_end: str | None = None
    clock: str | None = None
    clock_end: str | None = Field(default=None, description="the lens clock: the last loaded day")
    stale: bool | None = None
    future_rows: int | None = None
    stopped: bool | None = None
    streams: list[dict[str, object]] = Field(default_factory=list)
    partial: dict[str, object] | None = None
    columns: dict[str, str | None] = Field(default_factory=dict)
    profiled_at: str | None = None
    note: str = ""


def _member_refusal(
    view: CubeLensView, config: LensConfig, caller: CallerIdentity, member: str
) -> None:
    """The scope, hidden and declared checks a query naming ``member`` meets:
    a member the caller could not query, it cannot read facts about."""
    ask = Ask(question="", lens=view, config=config, groups=sorted(caller.groups))
    q = CubeQuery(dimensions=[member])
    for check in (not_declared, scope_violation, hidden_column):
        c = check(ask, [q])
        if c.stops:
            raise HTTPException(status_code=400, detail=c.detail)


def _exposed_column(bundle: LensBundle, member: str) -> tuple[Entity, str | None] | None:
    """``(entity, column)`` for ``table.column``, ``entity.column`` or a bare
    table name among the lens's exposed tables (as this caller reads them)."""
    for e in bundle.semantic_model.entities:
        if not e.exposed:
            continue
        for name in (e.source.table, e.name):
            if member == name:
                return e, None
            if member.startswith(name + "."):
                column = member[len(name) + 1 :]
                if any(f.name.lower() == column.lower() for f in e.fields):
                    return e, column
    return None


def _table_profile(
    bundle: LensBundle, caller: CallerIdentity, table: str
) -> tuple[TableProfile | None, bool]:
    profiles, _as_of, age, _stamps = assembly.profile_facts(bundle, caller.org_id)
    hit = next((p for p in profiles if p.table == table), None)
    return hit, age is None or not age.stale


def lens_values(name: str, member: str, prefix: str, caller: CallerIdentity) -> ValuesResult:
    rid = "req_" + uuid.uuid4().hex[:16]
    bundle = _authorized_bundle(name, caller, rid)
    want = prefix.strip().lower()
    if cube_door.serving_core(bundle.config, bundle.semantic_model) == "cube":
        view = _view(name, bundle, caller, rid)
        _member_refusal(view, bundle.config, caller, member)
        values = view.dictionary(member)
        if values is None:
            return ValuesResult(
                lens=name,
                member=member,
                values=[],
                complete=False,
                source="probe",
                note=f"the probe holds no values of {member}: it is not a dictionary "
                "dimension, or has not been probed — could not verify a value against it",
            )
        hits = [str(v) for v in values if str(v).lower().startswith(want)]
        return ValuesResult(
            lens=name,
            member=member,
            values=hits[:VALUES_MAX],
            total=len(values),
            complete=len(hits) <= VALUES_MAX,
            source="probe",
        )
    found = _exposed_column(bundle, member)
    if found is None or found[1] is None:
        raise HTTPException(
            status_code=404,
            detail=f"lens '{name}' has no exposed column '{member}': name it table.column",
        )
    entity, column = found
    profile, fresh = _table_profile(bundle, caller, entity.source.table)
    col = next(
        (
            c
            for c in (profile.columns if profile else [])
            if c.name.lower() == (column or "").lower()
        ),
        None,
    )
    if col is None or not col.top_values:
        return ValuesResult(
            lens=name,
            member=member,
            values=[],
            complete=False,
            source="profile",
            note=f"the probe holds no value list of {member} — could not verify a value "
            "against it; read it with sql",
        )
    hits = [v for v in col.top_values if v.lower().startswith(want)]
    return ValuesResult(
        lens=name,
        member=member,
        values=hits[:VALUES_MAX],
        total=col.distinct_count,
        complete=col.values_complete and fresh and len(hits) <= VALUES_MAX,
        source="profile",
        note="" if fresh else "the probe is older than probe.max_age: values may have changed",
    )


def lens_data_end(name: str, member: str, caller: CallerIdentity) -> DataEndResult:
    rid = "req_" + uuid.uuid4().hex[:16]
    bundle = _authorized_bundle(name, caller, rid)
    if cube_door.serving_core(bundle.config, bundle.semantic_model) == "cube":
        view = _view(name, bundle, caller, rid)
        probe = view.probe
        stale = None
        clock = probe.clock()
        if clock is not None:
            today = assembly.business_today_date(bundle.config.timezone)
            stale = (today - clock).days > probe.stale_after_days
        out = DataEndResult(lens=name, member=member, clock_end=probe.clock_end, stale=stale)
        facts = probe.cubes.get(member)
        if facts is not None and "." not in member:
            out.kind, out.clock = "cube", facts.clock
            out.streams = [s.model_dump(exclude_defaults=True) for s in facts.streams]
            return out
        _member_refusal(view, bundle.config, caller, member)
        if view.measure(member) is not None:
            out.kind, out.clock = "measure", view.clock(member)
            if out.clock and (t := view.time_facts(out.clock)) is not None:
                out.data_end, out.future_rows, out.stopped = t.data_end, t.future_rows, t.stopped
            mf = view.measure_facts(member)
            if mf is not None and mf.partial is not None:
                out.partial = mf.partial.model_dump(exclude_defaults=True)
        elif (t := view.time_facts(member)) is not None:
            out.kind, out.data_end = t.kind, t.data_end
            out.future_rows, out.stopped = t.future_rows, t.stopped
        if out.data_end is None:
            out.note = f"the monitor holds no data end for {member} — could not verify it"
        return out
    found = _exposed_column(bundle, member)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail=f"lens '{name}' has no exposed table or column '{member}'",
        )
    entity, column = found
    profile, fresh = _table_profile(bundle, caller, entity.source.table)
    out = DataEndResult(lens=name, member=member, kind="table")
    if profile is None:
        out.note = f"the probe holds no profile of {entity.source.table} — could not verify"
        return out
    out.profiled_at = profile.profiled_at.isoformat()
    clocks = clock_columns(entity, bundle.semantic_model)
    cols = [column] if column else clocks
    for c in profile.columns:
        if any(c.name.lower() == x.lower() for x in cols):
            out.columns[c.name] = c.max
    ends = [v for v in out.columns.values() if v]
    out.data_end = max(ends) if ends else None
    if out.data_end is None:
        out.note = (
            f"no dated column of {entity.source.table} has a probed maximum — could not verify"
        )
    elif not fresh:
        out.note = "the probe is older than probe.max_age: the data may run later"
    return out


@router.get("/lenses/{name}/values", response_model=ValuesResult)
async def values(
    name: str,
    member: str,
    prefix: str = "",
    caller: CallerIdentity = Depends(get_caller),
) -> ValuesResult:
    """The observed values of a member (a Cube lens: the probe's dictionary)
    or of an exposed column (``table.column``: the stored profile), those
    starting with ``prefix``. No model, no warehouse."""
    with ai_usage.no_models():
        return await run_in_threadpool(lens_values, name, member, prefix, caller)


@router.get("/lenses/{name}/data_end", response_model=DataEndResult)
async def data_end(
    name: str,
    member: str,
    caller: CallerIdentity = Depends(get_caller),
) -> DataEndResult:
    """Where a member's data ends (a Cube lens: the monitor's facts — a time
    dimension's last date, a measure's clock and partial period, a cube's
    feeds), or an exposed table's or column's (the probe's maxima)."""
    with ai_usage.no_models():
        return await run_in_threadpool(lens_data_end, name, member, caller)


# ── grain ───────────────────────────────────────────────────────────────────


class GrainResult(BaseModel):
    lens: str
    table: ExposedTableInfo
    grain: list[str] = Field(
        default_factory=list, description="rows against distinct key, or 'could not verify' and why"
    )


def table_grain(table: str, lens: str | None, caller: CallerIdentity) -> GrainResult:
    rid = "req_" + uuid.uuid4().hex[:16]
    names = (
        [lens]
        if lens
        else sorted(
            n
            for n, _d, _x, b in list_published_for_org(caller.org_id)
            if authorize(caller, b.config)[0]
        )
    )
    for name in names:
        bundle = _authorized_bundle(name, caller, rid)
        hit = _exposed_column(bundle, table)
        if hit is None or hit[1] is not None:
            continue
        entity = hit[0]
        sm = bundle.semantic_model
        profiles, _as_of, age, _stamps = assembly.profile_facts(bundle, caller.org_id)
        info = _exposed_info(
            entity, sm, {p.table: p for p in profiles}, age is None or not age.stale
        )
        try:
            connector = _authorized_connector(bundle, caller)
        except Exception:  # noqa: BLE001 — without a connection the check says so
            return GrainResult(
                lens=name,
                table=info,
                grain=[f"GRAIN {entity.source.table}: could not verify: no live connection"],
            )
        return GrainResult(
            lens=name,
            table=info,
            grain=_grain_lines(sm, [entity.source.table], connector, caller),
        )
    raise HTTPException(
        status_code=404,
        detail=f"no lens you may use exposes a table named '{table}'"
        + (f" (looked in {lens})" if lens else ""),
    )


@router.get("/grain", response_model=GrainResult)
async def grain(
    table: str,
    lens: str | None = None,
    caller: CallerIdentity = Depends(get_caller),
) -> GrainResult:
    """An exposed table's blueprint (grain, key, versions, clocks, caveats),
    each check's verdict, the probe's facts, and rows against distinct key."""
    with ai_usage.no_models():
        return await run_in_threadpool(table_grain, table, lens, caller)
