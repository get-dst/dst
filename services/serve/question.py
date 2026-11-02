"""The question-type step at the doors: what each door hands the step, and
what it gets back.

``services/resolve/question_type`` decides the type and writes the answers
from a catalogue; this module builds that catalogue from the published
bundles (a Cube lens's stored ``LensModel`` with the probe's dictionaries,
a typed lens's ``SemanticModel``), reads the project's ``hand_off:`` and
the caller's own request log, and runs the step for one door.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from services.contracts.failure_modes import FailureMode
from services.contracts.response import QueryResponse
from services.contracts.trace import TraceLog
from services.governance.credentials import CallerIdentity
from services.lenses.store import LensBundle
from services.llm import registry
from services.llm.registry import ResolvedModel
from services.observability import observe
from services.observability import usage as ai_usage
from services.project.cube_plan import stored_lens_model
from services.project.source import project_config
from services.resolve import question_type as qt
from services.resolve.question_type import Catalogue, HandOffEntry, Typed
from services.serve import door as cube_door

log = logging.getLogger("dst.question_type")


def catalogue(name: str, bundle: LensBundle, groups: Sequence[str] = ()) -> Catalogue:
    """One lens as the step reads it. A Cube lens whose stored model does
    not read is catalogued from its semantic model (entities exposed beside
    it), never skipped."""
    sm = bundle.semantic_model
    description = bundle.config.description or ""
    if cube_door.serving_core(bundle.config, sm) == "cube" and sm.cube is not None:
        try:
            model = stored_lens_model(sm.cube)
        except Exception:
            log.exception("stored lens model of %s unreadable for the question-type step", name)
            model = None
        if model is not None:
            try:
                dictionaries = cube_door.load_probe(cube_door.project_root()).dictionaries
            except Exception:
                dictionaries = {}
            return qt.catalogue_of(
                name, model, dictionaries=dictionaries, groups=groups, description=description
            )
    return qt.catalogue_of(name, sm, groups=groups, description=description)


def hand_off() -> list[HandOffEntry]:
    cfg = project_config()
    if cfg is None:
        return []
    return [HandOffEntry(h.name, h.kind, h.url, h.mcp) for h in cfg.hand_off]


def fast_model(
    caller: CallerIdentity | None, bundle: LensBundle | None = None
) -> ResolvedModel | None:
    """The model the no-driver door may consult: the lens's provider's fast
    tier, else the install's. None for a driver (dst reads nothing for an
    agent), and None when no provider is configured."""
    if caller is not None and caller.driver:
        return None
    if ai_usage.models_off():
        return None
    if bundle is not None:
        return cube_door.frontier_model(bundle.config)
    ref = registry.tier("fast")
    return registry.resolve(ref) if ref else None


def step(
    question: str,
    cat: Catalogue,
    *,
    lens: str,
    request_id: str,
    org_id: str,
    caller: str,
    agent: str | None = None,
    model: ResolvedModel | None = None,
    log_reader: bool = True,
    driver: bool = False,
) -> tuple[Typed, tuple[QueryResponse, TraceLog] | None]:
    """Decide the type and, for every type but ``query`` and ``sql``, write
    the answer. ``driver``: the caller is an agent, so the rules decide alone
    and no model is consulted whatever ``model`` says. The consult's tokens
    (when a model was asked) are charged to the answer's row; a query's
    consult is on its ai_call row."""
    with ai_usage.collecting() as calls, ai_usage.purpose("question_type"):
        typed = qt.decide_type(question, held=cat.held, model=None if driver else model)
    typed.original = question
    if typed.type == "query" or (typed.type == "sql" and typed.subject):
        return typed, None
    answered = qt.answer(
        typed,
        cat,
        lens=lens,
        request_id=request_id,
        org_id=org_id,
        caller=caller,
        question=question,
        hand_off=hand_off(),
        last_requests=_reader(org_id, caller) if log_reader else None,
        agent=agent,
    )
    return typed, _charged(answered, calls)


def _reader(org_id: str, caller: str) -> qt.LastRequests:
    def read(n: int) -> list[dict[str, Any]]:
        try:
            return observe.last_requests(org_id, caller, n)
        except Exception:  # noqa: BLE001 — the log being down is said, never a fault
            log.exception("the request log could not be read for %s", caller)
            return []

    return read


def _charged(
    answered: tuple[QueryResponse, TraceLog], calls: list[Any]
) -> tuple[QueryResponse, TraceLog]:
    resp, trace = answered
    tokens_in, tokens_out, cost_usd = ai_usage.totals(calls)
    if calls:
        trace.ai_input_tokens, trace.ai_output_tokens, trace.ai_cost_usd = (
            tokens_in or None,
            tokens_out or None,
            cost_usd,
        )
    return resp, trace


def as_sql_answer(typed: Typed, response: QueryResponse, trace: TraceLog) -> None:
    """A SQL ask whose data question served: the compiled SQL and the Cube
    query become the answer body, under ``sql_request``; a non-answer keeps
    the mode the data question got. The row keeps the question as asked."""
    trace.question = typed.original or trace.question
    if response.status != "ok":
        return
    response.answer = qt.sql_body(response.sql, trace.cube_query)
    response.mode = FailureMode.sql_request
    trace.answer = response.answer
    trace.mode = FailureMode.sql_request
