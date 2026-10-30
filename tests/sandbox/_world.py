"""The sandbox repro harness: a trimmed reference catalogue, a tiny warehouse per
test, and the typed lane driven by the decisions a typed-decision provider
recorded, so each wrong-served answer replays without a model in the loop.

Every test here asks one question through the lens-query half of the routed
door exactly as ``format: "structured"`` runs it (``composer=None``, no untyped
escalation) and asserts one rule: a served figure must match a defensible
reading of the question, or the answer must be a clarification or a refusal.
A figure that matches no reading is the defect; nothing else is graded.

The catalogue under ``catalogue/`` is a slice of a complete, correctly authored
layer over an invented plumbing group (three operating companies, a webshop, a
field-service book). The warehouse rows are built per test: a handful of rows
shaped so the reading and the served computation differ, with the readings
computed from the same rows by the question's own SQL.

Decisions replay from the recorded run by slot (``RecordedDecider``); set
``DST_TEST_TYPESAFE_KEY`` to ask the live typed-decision provider instead.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict, deque
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import yaml

from services.connectors.duckdb import DuckDBConnector
from services.contracts.lens_config import LensConfig
from services.contracts.profile import LOW_CARDINALITY_MAX
from services.contracts.protocols import Decider, Decision, Option
from services.contracts.response import QueryResponse
from services.contracts.semantic_model import SemanticModel
from services.project.compile import compile_lens_model
from services.project.loader import load_lens_source
from services.runtime.pipeline import run_query
from services.runtime.typed_resolver import TypedIntentGenerator, TypedResolver
from services.semantic.files import parse_semantic_files

CATALOGUE = Path(__file__).parent / "catalogue"
TODAY = date(2026, 9, 1)  # the day every question was asked on
GROUPS = ["hr", "finance", "support", "sales"]
_MONTH = re.compile(r"^\d{4}-\d{2}$")

# One recorded decision: (slot, chosen, p). p is None for a slot the resolver
# read off the question or the calendar rather than asked; those never reach
# the decider and are listed only so a transcript reads whole.
Recorded = list[tuple[str, str | None, float | None]]


# ── the catalogue ─────────────────────────────────────────────────────────────


def semantic_files() -> dict[str, str]:
    return {
        str(p.relative_to(CATALOGUE)): p.read_text()
        for p in (CATALOGUE / "semantic").rglob("*")
        if p.is_file()
    }


def lens_model(lens: str, entities: list[str]) -> tuple[LensConfig, SemanticModel]:
    """The lens compiled over the named entities only: the same lens.yaml, the
    same shared entities, definitions and relationships, with the selection cut
    to what the question's answer reads. ``expose`` is dropped (it resolves
    against a probe); ``untyped_fallback`` is off, as it was in the run."""
    shared_entities, shared_definitions, shared_relationships = parse_semantic_files(
        semantic_files()
    )
    cfg = yaml.safe_load((CATALOGUE / f"lenses/{lens}/lens.yaml").read_text())
    cfg.pop("expose", None)
    cfg["untyped_fallback"] = False
    sel = cfg["select"]
    sel["entities"] = [e for e in sel["entities"] if _entity_name(e) in entities]
    sel["definitions"] = [
        d
        for d in sel.get("definitions", [])
        if d in shared_definitions and _definition_fits(shared_definitions[d], entities)
    ]
    source = load_lens_source(
        {"lens.yaml": yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)}
    )
    model, _warnings = compile_lens_model(
        config=source.config,
        shared_entities=shared_entities,
        shared_definitions=shared_definitions,
        shared_relationships=shared_relationships,
        local_definitions=[],
        use_when=[],
        sample_queries=[],
        dialect="duckdb",
        groups=GROUPS,
    )
    return source.config, model


def _definition_fits(definition: Any, entities: list[str]) -> bool:
    """A definition about an entity the trimmed lens does not select would
    fail the compile; the full lens selects every one of them."""
    head = str(definition.about or "").split(".")[0]
    return not head or head in entities


def _entity_name(pick: Any) -> str:
    return pick if isinstance(pick, str) else str(pick.get("name"))


# ── the warehouse ─────────────────────────────────────────────────────────────


class World:
    """A DuckDB file holding the tables a test's question reads."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._con: duckdb.DuckDBPyConnection | None = duckdb.connect(str(path))

    def table(self, name: str, columns: dict[str, str], rows: list[tuple[Any, ...]]) -> None:
        assert self._con is not None, "the world is sealed"
        schema, _table = name.split(".")
        self._con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        cols = ", ".join(f'"{c}" {t}' for c, t in columns.items())
        self._con.execute(f"CREATE TABLE {name} ({cols})")
        if rows:
            marks = ", ".join("?" for _ in columns)
            self._con.executemany(f"INSERT INTO {name} VALUES ({marks})", rows)

    def seal(self) -> None:
        """Close the writer: the connector opens the file read-only."""
        if self._con is not None:
            self._con.close()
            self._con = None

    def sql(self, query: str) -> list[tuple[Any, ...]]:
        """A reading computed from the same rows the answer read."""
        self.seal()
        con = duckdb.connect(str(self.path), read_only=True)
        try:
            return con.execute(query).fetchall()
        finally:
            con.close()

    def scalar(self, query: str) -> Any:
        (row,) = self.sql(query)
        return row[0]

    def connector(self) -> DuckDBConnector:
        self.seal()
        return DuckDBConnector(str(self.path), profile=False)

    def facts(
        self, model: SemanticModel
    ) -> tuple[
        dict[str, dict[str, list[str]]],
        dict[str, list[str]],
        dict[str, dict[str, str]],
        dict[str, str],
    ]:
        """What the probe would have recorded for the model's tables: each
        entity's complete value dictionaries (text columns up to the profile's
        low-cardinality cap), the bare-column dictionaries unique to one entity,
        the warehouse column types, and the month-period text fields."""
        self.seal()
        con = duckdb.connect(str(self.path), read_only=True)
        entity_domains: dict[str, dict[str, list[str]]] = {}
        column_types: dict[str, dict[str, str]] = {}
        period_fields: dict[str, str] = {}
        declared_on: dict[str, int] = defaultdict(int)
        try:
            for entity in model.entities:
                table = entity.source.table
                described = con.execute(f"DESCRIBE {table}").fetchall()
                column_types[entity.name] = {str(r[0]).lower(): str(r[1]) for r in described}
                own: dict[str, list[str]] = {}
                for f in entity.fields:
                    declared_on[f.name.lower()] += 1
                    if f.type != "string":
                        continue
                    n = con.execute(f'SELECT count(DISTINCT "{f.name}") FROM {table}').fetchone()
                    if n is None or n[0] == 0 or n[0] > LOW_CARDINALITY_MAX:
                        continue
                    values = [
                        str(r[0])
                        for r in con.execute(
                            f'SELECT DISTINCT "{f.name}" FROM {table} WHERE "{f.name}" IS NOT '
                            f"NULL ORDER BY 1"
                        ).fetchall()
                    ]
                    own[f.name.lower()] = values
                    if entity.name not in period_fields and all(_MONTH.match(v) for v in values):
                        period_fields[entity.name] = f.name
                if own:
                    entity_domains[entity.name] = own
        finally:
            con.close()
        domains: dict[str, list[str]] = {}
        for own in entity_domains.values():
            for column, values in own.items():
                if declared_on[column] == 1:
                    domains[column] = values
        return entity_domains, domains, column_types, period_fields


# ── the decider ───────────────────────────────────────────────────────────────


class RecordedDecider:
    """Replays, slot by slot, the decisions a typed-decision provider made for
    the question in the recorded run, with the probability it measured. The
    slot is read off the decision's context, since the seam does not name it;
    a slot the run never recorded takes ``none`` where that is allowed and
    fails loudly where it is not, so a divergence is never a quiet pass."""

    typed_decisions = True

    def __init__(self, recorded: Recorded) -> None:
        self._queues: dict[str, deque[tuple[str | None, float]]] = defaultdict(deque)
        for slot, chosen, p in recorded:
            if p is not None:
                self._queues[slot].append((chosen, p))
        self.asked: list[tuple[str, str | None]] = []
        self.unrecorded: list[tuple[str, list[str]]] = []

    @staticmethod
    def slot_of(context: str, options: list[Option]) -> str:
        names = {o.name for o in options}
        if names <= {"aggregate", "ranking", "listing", "none"}:
            return "shape"
        if names <= {"engaged", "not_engaged"}:
            return "definition"
        if names <= {"stated", "unstated"}:
            return "filter_value"
        if context.startswith("Which entity"):
            return "entity"
        if "governed metric" in context:
            return "metric"
        if "broken down over time" in context:
            return "grain"
        if context.startswith("Which stored value") or "true/false field" in context:
            return "filter_value"
        if "How does the question compare" in context:
            return "filter_op"
        if "restrict" in context:
            return "filter"
        if "broken down by a dimension" in context or context.startswith("Which fields"):
            return "dimension"
        raise AssertionError(f"decision context not mapped to a slot: {context!r}")

    def decide(
        self, *, question: str, context: str, options: list[Option], allow_none: bool
    ) -> Decision:
        slot = self.slot_of(context, options)
        names = [o.name for o in options]
        queue = self._queues[slot]
        if queue:
            chosen, p = queue.popleft()
        else:
            # A slot the run did not record: either never reached (a round's
            # fan-out asks ahead; the transcript lists only what was used) or
            # a divergence. ``none`` either way — a required slot then declines
            # the question, a non-answer the run never served, never a figure.
            chosen, p = None, 0.99
            self.unrecorded.append((slot, names))
        if chosen is not None and chosen not in names:
            raise AssertionError(
                f"recorded {slot} pick {chosen!r} is not among the options {names}"
            )
        self.asked.append((slot, chosen))
        return Decision(
            chosen=chosen, probs=_probs(chosen, p, names, allow_none), provider="typesafe:recorded"
        )

    def decide_many(
        self, question: str, specs: dict[str, tuple[str, list[Option], bool]]
    ) -> dict[str, Decision]:
        return {
            key: self.decide(question=question, context=ctx, options=opts, allow_none=allow)
            for key, (ctx, opts, allow) in specs.items()
        }


def _probs(chosen: str | None, p: float, names: list[str], allow_none: bool) -> dict[str, float]:
    labels = list(names)
    if allow_none and "none" not in labels:
        labels.append("none")
    pick = chosen if chosen is not None else "none"
    if pick not in labels:
        labels.append(pick)
    others = [n for n in labels if n != pick]
    rest = max(0.0, 1.0 - p)
    share = min(rest / len(others), p * 0.9) if others else 0.0
    out = {n: share for n in others}
    out[pick] = p
    return out


def decider(recorded: Recorded) -> Decider:
    key = os.environ.get("DST_TEST_TYPESAFE_KEY")
    if key:
        from services.llm.typesafe import TypesafeDecider

        return TypesafeDecider(key)
    return RecordedDecider(recorded)


# ── the door ──────────────────────────────────────────────────────────────────


def ask(
    question: str,
    lens: str,
    entities: list[str],
    world: World,
    recorded: Recorded,
    *,
    bindings: dict[str, str] | None = None,
) -> QueryResponse:
    """The question through the lens-query half of the routed door as
    ``format: "structured"`` runs it: typed resolution over the lens, the
    compiled SQL executed, rows served with no prose composed and no untyped
    escalation (``untyped_fallback`` off)."""
    _config, model = lens_model(lens, entities)
    entity_domains, domains, column_types, period_fields = world.facts(model)
    resolver = TypedResolver(
        decider(recorded),
        domains=domains,
        entity_domains=entity_domains,
        period_fields=period_fields,
        bindings=bindings,
        today=TODAY,
    )
    res = run_query(
        question=question,
        lens_name=lens,
        org_id="org-sandbox",
        caller="analyst",
        semantic_model=model,
        connector=world.connector(),
        generator=TypedIntentGenerator(resolver, column_types=column_types),
        composer=None,
        escalate_generator=None,
    )
    return res.response


# ── the one rule ──────────────────────────────────────────────────────────────

NON_ANSWERS = ("clarification", "refused", "rejected")


def served_rows(resp: QueryResponse) -> list[list[Any]]:
    assert resp.data is not None, f"an ok answer with no data: {resp.answer}"
    return [[_norm(v) for v in row] for row in resp.data.rows]


def _norm(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, int) and not isinstance(v, bool):
        return float(v)
    return v


def norm(v: Any) -> Any:
    return _norm(v)


def describe(resp: QueryResponse) -> str:
    rows = resp.data.rows[:6] if resp.data is not None else None
    return f"status={resp.status} answer={resp.answer!r} sql={resp.sql!r} rows={rows!r}"


def assert_reading_or_non_answer(
    resp: QueryResponse, served: Any, readings: list[Any], *, what: str
) -> None:
    """The rule every case is graded by: a figure must match a defensible
    reading, or the answer must say it cannot give one."""
    if resp.status in NON_ANSWERS:
        return
    assert resp.status == "ok", describe(resp)
    assert served in readings, (
        f"{what}: served {served!r} matches no defensible reading {readings!r} — {describe(resp)}"
    )
