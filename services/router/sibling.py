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
5. ``by_sources`` — a question naming a source system some lens declares
   (lens.yaml ``sources``) goes only to the lenses declaring it; when none
   the caller may use does, it is uncovered for that reason.
6. ``metric_name_owner`` — a question naming a metric (its name or label)
   that one candidate alone declares belongs to that candidate, ahead of
   similarity: the lens that declares the figure is the one that computes it.
   Aliases are not read here: they widen the metric slot inside a lens, never
   the questions a lens takes.
7. ``shared_metric`` — a metric several candidates declare is theirs to split:
   a declared name only one of them carries, or the only one that can split
   over time when the question asks it, picks one; otherwise the question is
   asked which lens, never answered by a silent pick.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import replace

from services.router import CoverageProfile
from services.runtime.not_for import names_ask
from services.runtime.typed_resolver import asks_breakdown_over_time, split_over_time

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


# Words that say how a figure aggregates, not what it measures: a metric
# called just "total" or "count" names no figure a question could mean alone.
_AGG_WORDS = frozenset({"avg", "average", "count", "max", "mean", "min", "number", "sum", "total"})


def _phrase(text: str) -> str:
    """A declared name or a question as words: case folded, underscores and
    hyphens read as spaces."""
    return normalise(re.sub(r"[_\-]+", " ", text))


def _longest(declared: dict[str, set[str]]) -> dict[str, set[str]]:
    """The named phrases no longer named phrase contains ("net takings" over
    "takings")."""
    return {
        n: owners
        for n, owners in declared.items()
        if not any(n != o and _contains(o, n) for o in declared)
    }


def named_metrics(question: str, candidates: Iterable[CoverageProfile]) -> dict[str, set[str]]:
    """Each metric phrase (name or label) the question names, the longest
    reading only, mapped to the candidates declaring it. A phrase of
    aggregation words alone names no figure."""
    q = _phrase(question)
    declared: dict[str, set[str]] = {}
    for p in candidates:
        for phrase in p.metric_names:
            n = _phrase(phrase)
            if not set(n.split()) <= _AGG_WORDS and _contains(q, n):
                declared.setdefault(n, set()).add(p.lens)
    return _longest(declared)


def metric_name_owner(question: str, candidates: Iterable[CoverageProfile]) -> str | None:
    """The one candidate declaring a metric phrase the question names, else None.

    A phrase two candidates declare separates nothing (the same metric selected
    by siblings); a phrase inside a longer one the question also names yields to
    it ("net revenue" over "revenue"); a question naming a metric the owner
    does not declare names no owner; underscores read as spaces."""
    named = named_metrics(question, candidates)
    owners = {lens for owners in named.values() if len(owners) == 1 for lens in owners}
    if len(owners) != 1:
        return None
    owner = owners.pop()
    # Every metric named must be one the owner declares (shared or its own).
    return owner if all(owner in o for o in named.values()) else None


def shared_metric(
    question: str, candidates: list[CoverageProfile]
) -> tuple[str | None, str | None]:
    """``(lens, None)`` or ``(None, reason)`` when two or more candidates
    declare every metric the question names; ``(None, None)`` when they do not
    (the question names no metric, or its metrics are no lens's together).

    Among those owners a table, entity or metric name only one declares picks
    it; so does a split over time only one of them can make. Otherwise the
    reason asks which lens, naming them, or says none of them can split the
    figure over time."""
    named = named_metrics(question, candidates)
    if not named:
        return None, None
    common = set.intersection(*named.values())
    if len(common) < 2:
        return None, None
    owners = [p for p in candidates if p.lens in common]
    lenses = ", ".join(sorted(common))
    figure = " and ".join(sorted(named))
    if asks_breakdown_over_time(question):
        timed = [p for p in owners if p.time_fields]
        if not timed:
            return None, (
                f"{lenses} measure {figure}, and none of them can split it "
                f"{split_over_time(question)}: they declare no time field"
            )
        owners = timed
        if len(owners) == 1:
            return owners[0].lens, None
    named_owner = unique_name_owner(question, owners)
    if named_owner is not None:
        return named_owner, None
    lenses = ", ".join(sorted(p.lens for p in owners))
    return None, f"several lenses measure {figure}: {lenses}. Name the lens to ask"


def named_sources(question: str, known: Iterable[str]) -> list[str]:
    """The declared source systems the question names as whole words, the
    longest reading only ("door scanners" over "scanners"), as declared."""
    q = _phrase(question)
    spelled = {_phrase(s): s for s in known if _phrase(s)}
    hits = {n: {n} for n in spelled if _contains(q, n)}
    return sorted(spelled[n] for n in _longest(hits))


def by_sources(
    question: str, candidates: list[CoverageProfile], known: Iterable[str]
) -> tuple[list[CoverageProfile], str | None]:
    """The candidates declaring every source system the question names, out of
    ``known`` (every system any lens declares). None naming one: all of them.
    None declaring it: no candidate, and the reason says which system."""
    named = named_sources(question, known)
    if not named:
        return candidates, None
    want = {_phrase(s) for s in named}
    kept = [p for p in candidates if want <= {_phrase(s) for s in p.sources}]
    if kept:
        return kept, None
    systems = " and ".join(named)
    return [], (
        f"the question names {systems}, and no lens you can use reads "
        f"{'it' if len(named) == 1 else 'them together'}"
    )
