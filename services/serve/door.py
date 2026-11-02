"""The cube lane behind the lens doors.

``serving_core`` says which core a lens serves on (``DST_SERVING`` wins over
the lens's own ``serving:``; unset, a lens of a Cube-backed project serves
through Cube and every other typed). ``open_lane`` builds the lane for one request:
the Cube client as this caller, the meta Cube shows them, the project's
overlay and probe file. ``answer_question`` is the question door (the
frontier lane, then the lane's checks); ``answer_query`` is the structured
door (the caller's own Cube query, no model call).
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from services.certify import binding as certify_binding
from services.config import settings
from services.contracts.cube_meta import MetaDoc
from services.contracts.cube_probe import (
    CubeProbe,
    MeasureFacts,
    PartialPeriod,
    Stream,
    date_meaning,
    time_kind,
)
from services.contracts.cube_query import Binding, Candidate, CubeQuery
from services.contracts.failure_modes import FailureMode
from services.contracts.lens_config import LensConfig
from services.contracts.lens_model import LensModel, Overlay, parse_overlay
from services.contracts.protocols import Connector
from services.contracts.response import QueryResponse
from services.contracts.semantic_model import SemanticModel
from services.contracts.trace import TraceLog
from services.cube.client import CubeClient, CubeError, configured
from services.cube.context import CubeContext
from services.cube.lens_view import CubeLensView
from services.llm import registry
from services.llm.registry import ResolvedModel
from services.observability import cost
from services.observability import usage as ai_usage
from services.resolve import frontier
from services.runtime.pipeline import LENS_DATA_REFUSAL
from services.serve import lanes
from services.serve.cube_lane import CubeLane
from services.verify.base import Check

log = logging.getLogger("dst.cube.door")

OVERLAY_FILE = Path("cube") / "overlay.yaml"
PROBE_FILE = Path("profiles") / "cube.probe.json"
FRESHNESS_FILE = Path("profiles") / "cube.freshness.json"


class CubeUnavailable(RuntimeError):
    """No Cube configured, or Cube did not answer the meta call: the door
    answers 503 naming Cube."""


def serving_core(config: LensConfig, model: SemanticModel | None = None) -> str:
    """``DST_SERVING``, else the lens's ``serving:``, else ``cube`` for a lens
    apply stamped against a Cube model and ``typed`` for every other."""
    return lanes.lane(config, cube_backed=model is not None and model.cube is not None)


def project_root() -> Path:
    return Path(settings.project_file).resolve().parent


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc if isinstance(doc, dict) else {}


def load_probe(root: Path, overlay: Overlay | None = None, doc: MetaDoc | None = None) -> CubeProbe:
    """The probe's and the monitor's files as one set of facts: the probe's
    dictionaries and per-dimension data ends (``cube.probe.json``), the
    monitor's per-cube feeds and per-measure clocks and partial periods
    (``cube.freshness.json``). Each date is an event or a schedule as the
    overlay's ``clock_kind`` declares, else as its name or the meta's title
    and description say (``date_meaning``), else as its future rows say
    (``time_kind``). Missing files leave the checks that read them
    reporting skip."""
    probe, fresh = _read(root / PROBE_FILE), _read(root / FRESHNESS_FILE)
    cubes: dict[str, Any] = {}

    def skipped(cube: str, what: str, exc: ValidationError) -> None:
        # One unreadable record never takes the door down: it is skipped,
        # logged, and said on its own cube, whose checks disclose it.
        why = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        warning = f"the monitor's {what} of {cube} does not read and was skipped ({why})"
        log.warning("%s", warning)
        cubes.setdefault(cube, {"time_dimensions": {}}).setdefault("warnings", []).append(warning)

    for cube, rec in (fresh.get("cubes") or {}).items():
        rec = rec if isinstance(rec, dict) else {}
        cubes[cube] = {"clock": rec.get("clock"), "time_dimensions": {}, "streams": []}
        for i, stream in enumerate(rec.get("streams") or []):
            try:
                cubes[cube]["streams"].append(Stream.model_validate(stream).model_dump())
            except ValidationError as exc:
                skipped(cube, f"stream {i + 1}", exc)
    measures: dict[str, Any] = {}
    for member, rec in (fresh.get("measures") or {}).items():
        if not isinstance(rec, dict):
            rec = {}
        cube = str(rec.get("cube") or member.partition(".")[0])
        try:
            facts = MeasureFacts.model_validate(rec)
        except ValidationError:
            # A partial stream with a bad value: keep the measure, drop the record.
            parts = rec.get("partial_streams")
            kept = []
            for i, part in enumerate(parts if isinstance(parts, list) else []):
                try:
                    kept.append(PartialPeriod.model_validate(part).model_dump())
                except ValidationError as exc:
                    skipped(cube, f"partial period {i + 1} of {member}", exc)
            try:
                facts = MeasureFacts.model_validate({**rec, "partial_streams": kept})
            except ValidationError as exc:
                skipped(cube, f"record of {member}", exc)
                continue
        measures[member] = facts.model_dump()
    declared = overlay.cubes if overlay is not None else {}
    for member, end in (probe.get("data_ends") or {}).items():
        cube, _, dim = member.partition(".")
        future = bool(end.get("future_rows"))
        ov = declared.get(cube)
        m = doc.member(member) if doc is not None else None
        meaning = date_meaning(dim, m.title, m.description) if m else date_meaning(dim)
        kind = time_kind(ov.clock_kind.get(dim) if ov else None, future, meaning)
        cubes.setdefault(cube, {"time_dimensions": {}})["time_dimensions"][dim] = {
            "kind": kind,
            "data_end": end.get("data_end"),
            "future_rows": 1 if future else 0,
        }
    return CubeProbe.model_validate(
        {
            "clock_end": fresh.get("clock_end") or probe.get("clock_end"),
            "stale_after_days": fresh.get("stale_after_days") or 3,
            "dictionaries": probe.get("dictionaries") or {},
            "cubes": cubes,
            "measures": measures,
        }
    )


def load_overlay(root: Path) -> Overlay:
    """``cube/overlay.yaml``, strictly (an unknown key fails loud), or an
    empty overlay when the project has none."""
    path = root / OVERLAY_FILE
    if not path.exists():
        return Overlay()
    return parse_overlay(path.read_text(encoding="utf-8"), str(OVERLAY_FILE))


def _pairs(root: Path, lens: str) -> list[dict[str, Any]]:
    """The lens's certified pairs as its file holds them; none when it does
    not parse (warned, never a fault)."""
    path = root / "lenses" / lens / "certified_answers.yaml"
    if not path.exists():
        return []
    try:
        entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except yaml.YAMLError as exc:
        log.warning("%s does not parse, so the lane has no exemplars: %s", path, exc)
        return []
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def render_pair_query(entry: dict[str, Any], binding: dict[str, object] | None) -> CubeQuery:
    """A pair's Cube query, a template's with ``binding`` written in through
    the certified slot validation. ValueError on a slot that does not
    validate, ValidationError on a query that is not a Cube query."""
    raw = entry["query"]
    if entry.get("slots"):
        specs, errors = certify_binding.parse_slots(entry["slots"])
        if errors:
            raise ValueError("; ".join(errors))
        raw = certify_binding.render_query(raw, specs, dict(binding or {}))
    return CubeQuery.model_validate(raw)


def load_exemplars(root: Path, lens: str) -> list[tuple[str, CubeQuery]]:
    """The lens's certified pairs that carry a Cube ``query``, as the frontier
    lane's and the driver's exemplars (question → Cube query). A template is
    rendered with its first ``sample_bindings`` entry and says it is one; a
    template without a sample, and a pair without a query, are none; a query
    that does not read (apply refuses one) is skipped with a warning, never
    a fault."""
    out: list[tuple[str, CubeQuery]] = []
    for e in _pairs(root, lens):
        if not isinstance(e.get("query"), dict):
            continue
        question = str(e.get("question") or "")
        if not question:
            continue
        sample: dict[str, object] | None = None
        if e.get("slots") or "{" in question:
            samples = e.get("sample_bindings")
            if not (e.get("slots") and isinstance(samples, list) and samples):
                continue
            sample = samples[0] if isinstance(samples[0], dict) else None
            if sample is None:
                continue
        try:
            query = render_pair_query(e, sample)
        except (ValueError, ValidationError):
            log.warning("the query of certified pair '%s' does not read", question[:60])
            continue
        if sample is not None:
            rendered = certify_binding.render_question(question, sample)
            question = f"{rendered} (a template: {question})"
        out.append((question, query))
    return out


def certified_query(root: Path, lens: str, question: str, sql: str) -> dict[str, Any] | None:
    """The project file's pair behind an approved answer, when it carries a
    Cube ``query``: the same question and the same SQL (whitespace aside), so
    the query is the companion of exactly what was approved."""

    def norm(text: object) -> str:
        return " ".join(str(text or "").split())

    for e in _pairs(root, lens):
        if (
            isinstance(e.get("query"), dict)
            and norm(e.get("question")) == norm(question)
            and norm(e.get("sql")) == norm(sql)
        ):
            return e
    return None


def frontier_model(config: LensConfig) -> ResolvedModel | None:
    """The lane's model: the fast tier of the lens's own provider, else the
    install's fast tier."""
    ref = config.model.model_ref()
    return registry.resolve(registry.fast_sibling(ref) if ref else registry.tier("fast"))


def open_view(
    name: str,
    config: LensConfig,
    *,
    ctx: CubeContext,
    client: CubeClient | None = None,
    root: Path | None = None,
    stored: LensModel | None = None,
) -> tuple[CubeClient, CubeLensView]:
    """The Cube client as this caller and the lens's view of the model as
    Cube shows it to them (``/v1/meta``), with the overlay and the probe's
    and the monitor's facts. What describe, values and data_end read, and
    what the lane's checks read. ``stored`` is the lens model apply stamped
    (every view member, read as a caller in every exception group): what
    tells a member Cube withheld from this caller apart from a name the
    view never had."""
    if client is None:
        cfg = configured()
        if cfg is None:
            raise CubeUnavailable("this lens serves through Cube, and no Cube is configured")
        client = CubeClient.from_config(cfg)
    try:
        doc = client.meta(ctx)
    except CubeError as exc:
        raise CubeUnavailable(f"Cube did not answer /v1/meta: {exc.detail}") from exc
    root = root or project_root()
    overlay = load_overlay(root)
    view = CubeLensView.build(
        doc,
        overlay=overlay,
        probe=load_probe(root, overlay, doc),
        lens=name,
        views=list(config.views),
        full=stored,
    )
    return client, view


def open_lane(
    name: str,
    config: LensConfig,
    *,
    connector: Connector,
    dialect: str,
    ctx: CubeContext,
    today: date,
    org_id: str,
    caller: str,
    groups: list[str],
    request_id: str,
    client: CubeClient | None = None,
    root: Path | None = None,
    stored: LensModel | None = None,
) -> CubeLane:
    client, view = open_view(name, config, ctx=ctx, client=client, root=root, stored=stored)
    root = root or project_root()
    return CubeLane(
        lens=name,
        config=config,
        view=view,
        client=client,
        ctx=ctx,
        connector=connector,
        dialect=dialect,
        today=today,
        org_id=org_id,
        caller=caller,
        groups=groups,
        request_id=request_id,
        exemplars=load_exemplars(root, name),
    )


def answer_question(
    lane: CubeLane, question: str, model: ResolvedModel | None
) -> tuple[QueryResponse, TraceLog]:
    """A question through the frontier lane and every check."""
    stop = lane.question_refusal(question)
    if stop is not None:
        return lane.non_answer(question, stop, lane="frontier")
    if model is None:
        return lane.non_answer(
            question,
            Check(
                "model",
                FailureMode.provider_down,
                "refuse",
                "no configured provider serves this lens's fast tier, so the question cannot "
                "be read into a Cube query",
            ),
            lane="frontier",
        )
    with ai_usage.collecting() as calls:
        result = frontier.resolve(
            question,
            lane.view,
            model,
            today=lane.today,
            timezone=lane.config.timezone,
            groups=lane.groups,
            exemplars=lane.exemplars,
        )
    if result.outcome == "query":
        resp, trace = lane.serve(
            question, result.candidates, ledger=result.ledger, lane="frontier", note=result.note
        )
    elif result.outcome == "clarify":
        resp, trace = lane.non_answer(
            question,
            Check(
                "frontier",
                FailureMode(result.mode or "ambiguous_metric"),
                "clarify",
                result.question,
            ),
            lane="frontier",
        )
    elif result.outcome == "cannot":
        # The lane read the lens and found nothing that answers: said as the
        # lens's own data refusal, which the routed door hands to the next
        # lens covering the question (a declared not_computable term is a
        # check of its own and never reaches here).
        resp, trace = lane.non_answer(
            question,
            Check(
                "frontier",
                FailureMode.not_declared,
                "refuse",
                f"{LENS_DATA_REFUSAL}{result.reason}",
            ),
            lane="frontier",
        )
    else:
        # A reply that is not a Cube query is the lane's fault, not an outcome.
        resp, trace = lane.non_answer(
            question,
            Check(
                "frontier",
                None,
                "refuse",
                f"the lane's reply was not a Cube query: {result.reason}",
            ),
            lane="frontier",
        )
    # The row is charged what the lane's calls billed, priced as their ai_call
    # rows are (prompt-cache tokens included), so observe and ai_call agree.
    tokens_in, tokens_out, cost_usd = ai_usage.totals(calls)
    if not calls:
        # A provider that reports no usage record: its tokens, priced by model.
        tokens_in = int(result.spend.get("input_tokens", 0))
        tokens_out = int(result.spend.get("output_tokens", 0))
        cost_usd = cost.ai_cost_usd(
            model.name,
            tokens_in,
            tokens_out,
            cache_read_tokens=int(result.spend.get("cache_read_tokens", 0)),
        )
    trace.ai_input_tokens = tokens_in or None
    trace.ai_output_tokens = tokens_out or None
    trace.ai_cost_usd = cost_usd
    return resp, trace


def answer_driver(
    lane: CubeLane,
    query: CubeQuery,
    ledger: list[Binding] | None,
    *,
    question: str = "",
    recorded: str | None = None,
) -> tuple[QueryResponse, TraceLog]:
    """A driver's own Cube query and word ledger: every check, no model call.
    With the question, the ledger is checked against its words, the scope it
    did not ask for is refused and its periods are bound; without it those
    checks report skip, never pass. ``recorded``: the question route_query
    recorded for this request; an ambiguous term's select words are read
    from it alone, since ``question`` is text the agent wrote."""
    text = question.strip()
    stop = lane.question_refusal(text) if text else None
    if stop is not None:
        return lane.non_answer(text, stop, queries=[query], ledger=ledger, lane="driver")
    return lane.serve(
        text or _members_text(query),
        [Candidate(query=query, ledger=ledger or [], lane="driver")],
        ledger=ledger,
        lane="driver",
        structured=not text,
        untested="no question text was sent, so the ledger was not checked against its words"
        if not text
        else None,
        recorded=recorded,
    )


def _members_text(query: CubeQuery) -> str:
    return " ".join(m.split(".")[-1].replace("_", " ") for m in query.members())


def answer_certified(
    lane: CubeLane, query: CubeQuery, *, text: str, cert_id: str
) -> tuple[QueryResponse, TraceLog]:
    """A certified pair's Cube query through every check the lane runs, Cube's
    compile and dst's execution, served and signed as certified. Its words
    need no ledger: the pair is the approved reading of its question."""
    lane.cert_id = cert_id
    stop = lane.question_refusal(text)
    if stop is not None:
        return lane.non_answer(text, stop, queries=[query], lane="certified")
    return lane.serve(
        text,
        [Candidate(query=query, lane="certified")],
        ledger=[],
        lane="certified",
        structured=True,
        untested="a certified pair: its query is the approved reading of its question",
    )


def answer_query(lane: CubeLane, query: CubeQuery, *, text: str) -> tuple[QueryResponse, TraceLog]:
    """A caller's own Cube query: the checks that read members and data, no
    model call, no question words to account for."""
    stop = lane.question_refusal(text)
    if stop is not None:
        return lane.non_answer(text, stop, queries=[query], lane="structured")
    return lane.serve(
        text,
        [Candidate(query=query, lane="structured")],
        ledger=[],
        lane="structured",
        structured=True,
    )
