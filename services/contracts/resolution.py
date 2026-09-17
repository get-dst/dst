"""The resolution ledger: where each part of an answer's meaning came from.

A served figure resolves to a metric (or definition), a grain, some dimensions,
some filters and a time window. Each of those is a SLOT, and each slot names its
SOURCE: ``certified`` (inside SQL a human approved), ``declared`` (a name in the
semantic model — the generator picked from the closed set), ``inferred`` (the
generator invented it: an aggregate no metric covers, a literal outside a
complete value dictionary, a date window) or ``unknown`` (attribution could not
run, and says so rather than guessing).

The answer-level ``tag`` is derived from the MEASURE slots only (metric +
definition): it says whether the figure's meaning was governed. Scope slots
(grain, dimension, filter, window) are recorded and graded in ``dst test`` but
never move the tag — the population story is governed separately, and folding a
literal date window into the headline would make every generated answer
``mixed`` and the number unreadable.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ``supplied``: the value came from the caller's bindings — the caller's
# assertion, distinct from declared (the model's closed set) and inferred (a
# generator's guess). Filters only; it never moves the tag.
Source = Literal["certified", "declared", "inferred", "unknown", "supplied"]
SlotKind = Literal["metric", "definition", "dimension", "grain", "filter", "window"]
Tag = Literal["certified", "declared", "mixed", "inferred", "unknown"]

MEASURE_KINDS: frozenset[str] = frozenset({"metric", "definition"})


class Slot(BaseModel):
    kind: SlotKind
    # A declared name (revenue, orders.status, order_date) — or, for an inferred
    # slot, the SQL text the generator wrote (SUM(o.amount), status = 'x').
    name: str
    source: Source
    # Grain unit, filter/window predicate text, definition term detail.
    value: str | None = None


class DecisionRecord(BaseModel):
    """One measured closed-set decision that shaped this answer (the regime's
    audit line): which slot was decided, what was chosen, the probability the
    decider MEASURED (None = nothing measured) and the policy verdict."""

    slot: str  # lens | certified_equivalent | metric | grain | filter_value
    chosen: str | None
    p: float | None = None
    runner_up: float | None = None
    margin: float | None = None
    verdict: str  # act | clarify | decline
    provider: str
    # calls / input_tokens / output_tokens / latency_s — the decision's share
    # of what its provider call(s) cost; summed per answer, priced per provider.
    usage: dict[str, float] | None = None


class Resolution(BaseModel):
    # construction = the intent tier picked names from the model and the compiler
    # resolved them; attributed = read back from the served SQL.
    method: Literal["construction", "attributed"]
    slots: list[Slot] = Field(default_factory=list)
    tag: Tag
    # Every measured decision on the way to this answer — one record, no
    # second column: probabilities attach to the ledger, not beside it.
    decisions: list[DecisionRecord] = Field(default_factory=list)
    # True when every slot was a typed decision the policy acted on, a
    # deterministic parse, or a caller-supplied binding, and no raw-SQL
    # escalation ran. None on answers served before typed serving existed.
    typed: bool | None = None


def derive_tag(slots: list[Slot], certification: str = "none") -> Tag:
    """The headline tag, from the measure slots alone (see module doc)."""
    if certification == "certified":
        return "certified"
    if any(s.source == "unknown" for s in slots):
        return "unknown"
    sources = {s.source for s in slots if s.kind in MEASURE_KINDS}
    if not sources:
        return "inferred"
    if sources <= {"declared", "certified"}:
        return "declared"
    if "declared" in sources or "certified" in sources:
        return "mixed"
    return "inferred"
