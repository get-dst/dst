"""Deterministic ambiguity resolution — the clarify rail's earliest exit.

A clarify is the right default for a genuinely unresolved ambiguous term. It is
the WRONG outcome for the two cases where the ambiguity is already resolved at
the earliest deterministic point, before any generator:

1. **The question names one mapping's own decisive term.** "What was net
   invoiced revenue in Q1?" contains the finance mapping's meaning verbatim —
   asking "did you mean net invoiced revenue?" back is the rail misfiring on
   its own vocabulary. The old escape hatch required the FULL meaning label
   ("net invoiced revenue (Finance / CFO world)") to appear in the question,
   which no caller ever types.
2. **The author declared who resolves it.** A definition can carry
   ``audiences: {cfo: net invoiced revenue, head of sales: bookings}`` — the
   per-role rule that otherwise lives only in the page's prose, where the
   deterministic pre-check cannot read it. A question carrying the principal's
   identity ("On behalf of the CFO: …" — the identity rides the question text)
   resolves to the declared mapping.

Both channels are literal word-boundary matches on author-declared strings —
zero model judgment, exactly the aliases discipline: the author widens what
questions SAY, never what the rail JUDGES. A term resolves only when the
channels agree on EXACTLY ONE mapping; naming two readings, or a question whose
wording picks one mapping while its audience picks another, still clarifies —
a wrong auto-pick is strictly worse than a clarify. And a resolution is never
silent: the pipeline pins it into the generation prompt as a context chunk
(source ``ambiguity-resolution: …``), which rides the trace's context_refs and
the citations, and an audience-resolved answer names the reading in prose.

One deliberate exclusion: a decisive term that IS a trigger surface (a mapping
labeled just "revenue", or a label the author also listed as an alias) never
resolves — the phrase that raises the ambiguity cannot also settle it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from services.contracts.semantic_model import Definition, SemanticModel


def _norm(text: str) -> str:
    """One normal form for questions, meaning labels and audience phrases:
    casefolded, underscores read as spaces, whitespace collapsed."""
    return re.sub(r"\s+", " ", text.replace("_", " ")).casefold().strip()


def _contains(phrase: str, text_norm: str) -> bool:
    return bool(phrase) and re.search(rf"\b{re.escape(phrase)}\b", text_norm) is not None


def surfaces(d: Definition) -> list[str]:
    """The trigger LEXICON: the term plus its authored aliases, normalized."""
    return [s for s in (_norm(x) for x in (d.term, *d.aliases)) if s]


def triggered(question: str, d: Definition) -> bool:
    """Would this term's clarify rail fire on the question? The clarify
    trigger's exact idiom: literal word-boundary match on the lexicon."""
    q = question.lower()
    return any(re.search(rf"\b{re.escape(s)}\b", q) for s in surfaces(d))


def mapping_label(mapping: str) -> str:
    """The meaning half of a possible_mapping ('meaning - where it lives')."""
    head, sep, _ = mapping.rpartition(" - ")
    return (head if sep else mapping).strip()


def decisive_terms(mapping: str) -> list[str]:
    """The phrases that pick this mapping when a question contains one: the
    meaning label, and the label with parenthetical qualifiers dropped — a
    label authored as 'net invoiced revenue (Finance / CFO world)' is decided
    by 'net invoiced revenue', which is what a caller actually types."""
    head = mapping_label(mapping)
    bare = re.sub(r"\([^)]*\)", " ", head)
    return [t for t in dict.fromkeys((_norm(head), _norm(bare))) if t]


def audience_mappings(d: Definition, target: str) -> list[str]:
    """The possible_mappings an ``audiences:`` value selects — by the same
    normalized word-boundary containment, against each mapping's label. A
    target must select exactly one to ever act (validate warns otherwise)."""
    t = _norm(target)
    return [m for m in d.possible_mappings if _contains(t, _norm(mapping_label(m)))]


@dataclass(frozen=True)
class Resolution:
    """One ambiguous term, deterministically resolved for one question."""

    term: str
    mapping: str  # the full possible_mappings entry the term resolved to
    via: str  # audit prose: how it resolved
    audience: str | None  # the matched audience phrase, None when the question named it
    others: list[str]  # the meaning labels NOT taken


def resolve(question: str, model: SemanticModel) -> list[Resolution]:
    """Every ambiguous term the question triggers AND already settles.

    A term resolves iff its decisive-term and audience channels, combined,
    select exactly one mapping. Zero selections is a genuinely unresolved ask;
    two or more is still contested — both stay on the clarify rail. A
    question-named pick outranks the audience phrase in the audit line (the
    caller's own wording is the stronger signal), but a conflict between the
    two channels never resolves."""
    q = _norm(question)
    out: list[Resolution] = []
    for d in model.definitions:
        if d.status != "ambiguous" or len(d.possible_mappings) < 2:
            continue
        if not triggered(question, d):
            continue
        lexicon = set(surfaces(d))
        named = [
            m
            for m in d.possible_mappings
            if any(t not in lexicon and _contains(t, q) for t in decisive_terms(m))
        ]
        by_audience: dict[str, str] = {}  # mapping -> the audience phrase that picked it
        for phrase, target in d.audiences.items():
            if not _contains(_norm(phrase), q):
                continue
            picked = audience_mappings(d, target)
            if len(picked) == 1:
                by_audience.setdefault(picked[0], phrase)
        chosen = list(dict.fromkeys(named + list(by_audience)))
        if len(chosen) != 1:
            continue
        mapping = chosen[0]
        matched_phrase = by_audience.get(mapping)
        via = (
            "the question names this reading"
            if named
            else f"declared audience '{matched_phrase}' resolves it"
        )
        out.append(
            Resolution(
                term=d.term,
                mapping=mapping,
                via=via,
                audience=None if named else matched_phrase,
                others=[mapping_label(m) for m in d.possible_mappings if m != mapping],
            )
        )
    return out


def pin(model: SemanticModel, resolved: list[Resolution]) -> SemanticModel:
    """The resolved reading, compiled into THIS request's model: the definition
    serves as active with the mapping as its meaning, so the generation prompt
    steers to it and the clarify rail (which reads status) stands down. Request
    -scoped — the shared page is never touched."""
    by_term = {r.term: r for r in resolved}
    return model.model_copy(
        update={
            "definitions": [
                (
                    d.model_copy(
                        update={
                            "status": "active",
                            "body": f"for this question, '{d.term}' means: "
                            f"{by_term[d.term].mapping}",
                            "possible_mappings": [],
                        }
                    )
                    if d.term in by_term
                    else d
                )
                for d in model.definitions
            ]
        }
    )


def disclosure_line(r: Resolution) -> str:
    """The reading said out loud, in ambiguity_disclosure's own idiom — for
    audience-resolved answers, where the caller's wording did not name it."""
    others = "; ".join(r.others)
    return (
        f"Reading applied: '{r.term}' was read as {mapping_label(r.mapping)} — "
        f"{r.via}; other declared readings exist ({others}) and give different "
        f"numbers; name one to use it."
    )
