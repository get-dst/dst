"""Deterministic sibling separation — what the router settles before any model.

A catalogue that grows by subject grows siblings: a daily mart and a trend mart
over one subject, movements beside cohorts beside a KPI snapshot. Their coverage
profiles overlap by construction (the same entity, the same `use_when` asks), so
similarity cannot tell them apart and a decider shown both guesses. Each rule
here is a fact the lenses already declare, applied exactly:

1. ``certified_owner`` — a question that IS a lens's certified question belongs
   to that lens; nothing about it needs deciding.
2. ``distinct_profiles`` — an anchor two lenses carry verbatim scores both at
   1.000 and separates nothing; only what a lens carries alone is scored.
3. ``unique_name_owner`` — between two candidates, a question that names a
   table, entity or metric only one of them declares belongs to that one.
4. ``drop_not_for`` — a lens that says it is not for this ask is not a candidate.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import replace

from services.router import CoverageProfile
from services.runtime.not_for import names_ask

_WS = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Case and whitespace folded, trailing punctuation dropped."""
    return _WS.sub(" ", text).strip().lower().rstrip("?.! ").strip()


def _contains(haystack: str, phrase: str) -> bool:
    """Word-boundary containment of a normalised phrase in a normalised text."""
    return bool(phrase) and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", haystack) is not None


def certified_owner(question: str, profiles: Iterable[CoverageProfile]) -> str | None:
    """The one lens whose certified questions include this question, else None
    (no owner, or two lenses certify the same text — that is the decider's)."""
    q = normalise(question)
    owners = {p.lens for p in profiles if q in {normalise(c) for c in p.certified}}
    return owners.pop() if len(owners) == 1 else None


def distinct_profiles(profiles: list[CoverageProfile]) -> list[CoverageProfile]:
    """Each profile with the anchors another lens carries verbatim removed. A lens
    whose every anchor is shared is left with none: it is reached by its certified
    questions or by name, never by text that describes its siblings equally."""
    shared = Counter(a for p in profiles for a in set(p.anchors))
    return [replace(p, anchors=[a for a in p.anchors if shared[a] < 2]) for p in profiles]


def drop_not_for(question: str, profiles: list[CoverageProfile]) -> list[CoverageProfile]:
    """Profiles none of whose ``not_for`` asks the question names — the same
    match the lens's own door refuses on (``services/runtime/not_for``)."""
    return [p for p in profiles if not any(names_ask(question, a) for a in p.not_for)]


def _name_forms(name: str) -> set[str]:
    low = name.strip().lower()
    forms = {low, low.replace("_", " ")}
    if "." in low:  # schema.table: the bare table name is what people type
        tail = low.rsplit(".", 1)[1]
        forms |= {tail, tail.replace("_", " ")}
    return {f for f in forms if f}


def unique_name_owner(question: str, candidates: list[CoverageProfile]) -> str | None:
    """The single candidate the question names by a declared name (table, entity,
    metric) no other candidate declares; None when zero or several do."""
    q = normalise(question)
    forms = {p.lens: set().union(*map(_name_forms, p.names)) for p in candidates}
    owners = []
    for lens, own in forms.items():
        others = set().union(*(f for other, f in forms.items() if other != lens))
        if any(_contains(q, f) for f in own - others):
            owners.append(lens)
    return owners[0] if len(owners) == 1 else None
