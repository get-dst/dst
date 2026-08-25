"""Patch drafter — from a correction + its trace, the smallest concrete fix.

Pure (no DB), like ``runtime/pipeline.py``: inputs are the ticket (with its
``CorrectionDelta``), the trace (question/SQL + generation tier + verification), the
lens bundle, and the lens scope's stored profiles. Store I/O lives at the API layer
(``services/api/reviews.py``) and in ``patch_store.py`` — that split is what makes
ScriptedLLM tests trivial.

The ``PatchCandidate`` contract + ``patch_store`` are shared seams: the corpus
distiller emits ticket-less candidates (``ticket_id=None``) through
the exact same store and approval surface.

Routing (kind of wrong → smallest fix), in order:

1. certified-tier trace + corrected SQL  → a **certified** update (the generator that
   ran was FixedSQL; only fixing the stored pair changes the served answer).
2. ``correction.target`` set, or ``kind="definition"``
                                        → a **Definition** patch (LLM amends the
   body; before/after diff). Owner follows the definition's provenance (dbt → dbt
   project owner, else the lens owner). An explicit ``target`` names the term
   VERBATIM (P19a) and outranks ``kind`` for EVERY kind — an unknown term drafts a
   NEW definition; the note's vocabulary steers placement only when no target is
   given.
3. ``kind="freshness"``                  → a **reprofile** when a stalled table-profile
   names the table (profile freshness), else a **reindex** of the lens's sources.
4. corrected SQL given                   → a candidate **certified** pair — the
   strongest fix (deterministic next time).
5. any other note                        → an **instruction** addition to the lens's
   ``instructions`` (a recurring domain gap, phrased as guidance for the query model).

Every candidate is ``status="candidate"`` — merging is always human.

A definition patch AMENDS, it never regenerates: the current body survives and the
correction is folded into it, enforced mechanically by ``_amend`` rather than
trusted to the prompt. Rejecting a draft with ``--note`` feeds that note back as a
constraint on the redraft — the loop's only feedback channel, so it must be both
stored and read.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel

from services.contracts.protocols import CacheableBlock, LLMProvider, Message
from services.contracts.semantic_model import Definition
from services.lenses.profile_drift import STALE_TABLE_DAYS, _staleness
from services.lenses.profile_store import StoredProfile
from services.lenses.store import LensBundle
from services.reviews.store import Ticket, Trace

PatchKind = Literal["definition", "instruction", "certified", "reindex", "reprofile"]
PatchStatus = Literal["candidate", "approved", "rejected"]

DEFAULT_OWNER = "lens-owner"


class PatchCandidate(BaseModel):
    """An auto-drafted, human-approvable fix. ``diff_before``/``diff_after`` render as
    the approvable diff; ``target`` names what changes (definition term / lens /
    certified question / table / source) and ``owner`` who should approve it."""

    id: str | None = None  # set once persisted (patch_store.record)
    ticket_id: str | None = None  # None for distilled (ticket-less) candidates
    lens: str
    kind: PatchKind
    target: str
    owner: str = DEFAULT_OWNER
    diff_before: str | None = None
    diff_after: str
    status: PatchStatus = "candidate"
    rejection_note: str | None = None  # why a human declined it — drafter feedback


# ── LLM drafting (strict-JSON prompts, mirroring reviews/judge.py) ────────────

_DEFINITION_SYSTEM = (
    "You maintain the governed business definitions of a semantic model. Given a "
    "definition, a question/SQL/answer trace that used it, and a correction note "
    "saying what is wrong, AMEND the definition so the noted error cannot recur.\n"
    "AMEND means: reproduce the CURRENT BODY IN FULL and change only the part the "
    "note is about. Every other paragraph — SQL formulas, exclusion and dismissal "
    "rules, pointers to sibling terms, worked examples — must come back unchanged. "
    "Never summarize, never shorten, and never write the definition from the note "
    "alone: the note is a correction to one ruling, not a replacement for the term. "
    "Return the whole amended body, paragraphs separated by blank lines.\n"
    'Respond with strict JSON: {"body": "<the whole amended definition body>"}.'
)

_INSTRUCTION_SYSTEM = (
    "You maintain the answer instructions of a governed data lens. Given a "
    "question/SQL/answer trace and a correction note saying what went wrong, write ONE "
    "short imperative instruction for the query model that would have prevented the "
    "mistake (e.g. 'Exclude internal accounts unless asked.'). Respond with strict "
    'JSON: {"instruction": "<one sentence>"}.'
)


def _strict_json_field(raw: str, key: str) -> str | None:
    try:
        value = json.loads(raw.strip()).get(key)
    except (json.JSONDecodeError, AttributeError):
        return None
    text = str(value or "").strip()
    return text or None


def _trace_block(trace: Trace, note: str) -> str:
    return (
        f"Question: {trace.question}\n"
        f"SQL: {trace.sql or '(none)'}\n"
        f"Answer: {trace.answer or '(none)'}\n"
        f"Correction note: {note}"
    )


def _rejection_block(rejections: Sequence[PatchCandidate]) -> str:
    """Prior rejected drafts of this same ticket, as CONSTRAINTS on the redraft.

    A ``rejection_note`` stored on the candidate but consumed by nothing means a
    reviewer who writes "keep the SQL formula and the dismissal rule" gets a second
    draft lossier than the first. The note is the only feedback channel the loop
    has; it must reach the model that redrafts.
    """
    if not rejections:
        return ""
    lines = ["", "PREVIOUS DRAFTS OF THIS SAME FIX WERE REJECTED. Do not repeat them:"]
    for i, prior in enumerate(rejections, 1):
        note = (prior.rejection_note or "").strip() or "(no reason recorded)"
        lines.append(f"{i}. Rejected draft: {prior.diff_after}")
        lines.append(f"   Reviewer's reason: {note}")
    lines.append(
        "Treat each reviewer's reason as a hard constraint on this draft — anything "
        "they said to keep must appear, and anything they called wrong must be gone."
    )
    return "\n".join(lines)


# ── the amend guard ──────────────────────────────────────────────────────────

# How similar a drafted paragraph must be to an existing one to count as that
# ruling's REPLACEMENT rather than as its deletion. Calibrated so the two
# populations separate: a genuine amendment keeps most of the paragraph it edits
# and scores well above this, while a lossy one-sentence redraft of a whole
# ruling scores well below it. 0.5 sits in the gap between them.
_SAME_RULING = 0.5

# The same question one level down, and the half of the problem the paragraph guard
# never covered: a ruling is not always a paragraph. Many real definition pages are
# a SINGLE paragraph carrying three to five rulings as SENTENCES, and there
# paragraph matching protects nothing — one drafted paragraph scoring over the floor
# carries every sentence in the body out with it. The failing shape is a two-ruling
# body and a note that scores just over the paragraph floor against it, taking both
# rulings with it. Calibrated at sentence granularity, where the two populations sit
# further apart than at paragraph granularity: two DISTINCT rulings from one body
# stay below this even at their worst overlap (a formula against its own
# zero-denominator case), while a genuine in-place amendment of one sentence stays
# above it. The error 0.6 can still make is the safe one — an amendment it fails to
# recognize is restored beside its own rewrite, in the diff, where the approving
# human sees both.
_SAME_SENTENCE = 0.6

# Sentence end plus whitespace, never a bare '.': `customers.customer_lifetime_value`
# is one identifier, and splitting inside it invents rulings nobody wrote.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _paragraphs(body: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]


def _sentences(paragraph: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(paragraph) if s.strip()]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _reconcile(
    originals: list[str],
    new: list[str],
    floor: float,
    merge: Callable[[str, str], str] | None = None,
) -> list[str]:
    """``new``, with every original it silently dropped restored in position.

    Each original is matched against its closest unclaimed candidate. At or above
    ``floor`` that candidate IS this ruling, edited, and takes the original's place
    (``merge`` gets the last word, which is how the paragraph pass reconciles the
    sentences inside a matched pair). Below the floor the ruling was dropped and
    comes back verbatim. Unclaimed candidates are genuinely new and append.
    """
    normed = [_norm(c) for c in new]
    out: list[str] = []
    used: set[int] = set()
    for original in originals:
        target = _norm(original)
        ranked = sorted(
            ((SequenceMatcher(None, target, cand).ratio(), i) for i, cand in enumerate(normed)),
            reverse=True,
        )
        best = next(((score, i) for score, i in ranked if i not in used), None)
        if best is not None and best[0] >= floor:
            used.add(best[1])
            drafted = new[best[1]]  # this ruling, as the drafter amended it
            out.append(merge(original, drafted) if merge is not None else drafted)
        else:
            out.append(original)  # the drafter dropped it — it survives verbatim
    out.extend(c for i, c in enumerate(new) if i not in used)  # genuinely new rulings
    return out


def _amend_paragraph(existing: str, drafted: str) -> str:
    """One paragraph's own stack of rulings, reconciled sentence by sentence."""
    return " ".join(_reconcile(_sentences(existing), _sentences(drafted), _SAME_SENTENCE))


def _amend(existing: str, drafted: str) -> str:
    """The drafted body with every existing ruling it silently dropped restored.

    A definition body is a stack of rulings: the formula, the dismissal rule, the
    pointer at a sibling term. The drafter was handed the whole body and asked for
    the whole body back, and it answered with a summary — one sentence in place of
    four rulings, and a rejection note naming what to keep made the SECOND draft
    lossier still. Prompting alone cannot make that safe, so amendment is enforced
    here instead of asked for.

    Rulings are reconciled at BOTH granularities they are written at — paragraphs
    first, then sentences inside each matched pair. A draft can rewrite any ruling
    and add any ruling, but cannot delete one, whether the author separated their
    rulings with a blank line or a full stop. A governance artifact loses nothing to
    a model that decided a rule was not worth restating; a deletion a curator really
    wants is one edit to the file, which is where authored truth lives anyway.
    """
    originals = _paragraphs(existing)
    new = _paragraphs(drafted)
    if not originals:
        return drafted.strip()
    if not new:
        return existing.strip()
    return "\n\n".join(_reconcile(originals, new, _SAME_RULING, merge=_amend_paragraph))


def _draft_definition_body(
    llm: LLMProvider | None,
    definition: Definition | None,
    trace: Trace,
    note: str,
    model: str,
    *,
    term: str,
    rejections: Sequence[PatchCandidate] = (),
) -> str:
    """The amended definition body — LLM-drafted, falling back to the note itself.

    Whatever comes back goes through ``_amend`` against the current body, so the
    fallback cannot flatten a four-ruling definition into the note either.
    """
    drafted = note
    if llm is not None:
        current = (
            f"Term: {definition.term}\nCurrent body:\n{definition.body}"
            if definition is not None
            else f"Term: {term} (new — not yet defined)"
        )
        res = llm.complete(
            system=[CacheableBlock(_DEFINITION_SYSTEM)],
            messages=[
                Message(
                    "user", f"{current}\n{_trace_block(trace, note)}{_rejection_block(rejections)}"
                )
            ],
            model=model,
            temperature=0.0,
            max_tokens=1200,  # a whole amended body, not one sentence
        )
        drafted = _strict_json_field(res.text, "body") or note
    return _amend(definition.body, drafted) if definition is not None else drafted


def _draft_instruction(
    llm: LLMProvider | None,
    trace: Trace,
    note: str,
    model: str,
    *,
    rejections: Sequence[PatchCandidate] = (),
) -> str:
    if llm is None:
        return note
    res = llm.complete(
        system=[CacheableBlock(_INSTRUCTION_SYSTEM)],
        messages=[Message("user", f"{_trace_block(trace, note)}{_rejection_block(rejections)}")],
        model=model,
        temperature=0.0,
        max_tokens=200,
    )
    return _strict_json_field(res.text, "instruction") or note


# ── routing helpers ──────────────────────────────────────────────────────────


def _resolve_definition(
    bundle: LensBundle, trace: Trace, note: str, target: str | None = None
) -> Definition | None:
    """The definition the correction is about.

    An explicit ``target`` is authoritative (P19a): the exact term, no vocabulary
    matching — ``None`` back means "no definition by that name; draft a NEW one".
    Without a target, fall back to bag-of-words: a term named in the note, else
    the definition the trace used.
    """
    definitions = bundle.semantic_model.definitions
    if target is not None:
        return next((d for d in definitions if d.term.lower() == target.lower()), None)
    lowered = note.lower()
    for d in definitions:
        if d.term.lower() in lowered or d.term.replace("_", " ").lower() in lowered:
            return d
    return next((d for d in definitions if d.term == trace.definition_used), None)


def _stalled_table(profiles: list[StoredProfile]) -> tuple[str, str] | None:
    """(table, detail) for the first stalled table in the lens scope, if any."""
    for stored in profiles:
        drift = _staleness(stored.profile, stale_after_days=STALE_TABLE_DAYS)
        if drift is not None:
            return drift.table, drift.detail
    return None


# ── the drafter ──────────────────────────────────────────────────────────────


def draft_patch(
    llm: LLMProvider | None,
    ticket: Ticket,
    trace: Trace,
    bundle: LensBundle,
    profiles: list[StoredProfile],
    *,
    model: str = "claude-sonnet-4-6",
    rejections: Sequence[PatchCandidate] = (),
) -> PatchCandidate | None:
    """The smallest concrete change that would fix the ticket's correction.

    ``rejections`` are this ticket's already-rejected candidates, newest first: their
    ``rejection_note`` becomes an explicit constraint on the redraft (P20 stored the
    note; nothing read it, so every redraft repeated the mistake). The API layer
    loads them — this module stays pure.

    Returns ``None`` when the ticket carries no correction (nothing to draft from)
    or no route applies (an empty note with nothing corrected).
    """
    correction = ticket.correction
    if correction is None:
        return None
    lens = ticket.lens
    corrected_sql = (correction.corrected_sql or "").strip() or None

    # 1. Certified tier: the served SQL was a stored pair — patch the pair itself.
    if trace.certification == "certified" and corrected_sql:
        return PatchCandidate(
            ticket_id=ticket.ticket_id,
            lens=lens,
            kind="certified",
            target=trace.question,
            diff_before=trace.sql,
            diff_after=corrected_sql,
        )

    # 2. Wrong/missing definition → a Definition patch with a before/after diff.
    target = (correction.target or "").strip() or None
    if (target is not None or correction.kind == "definition") and not corrected_sql:
        # Prose-only correction → a Definition patch. When the caller supplied
        # corrected SQL, fall through to the certified branch instead: a
        # definition's machine-readable sql_expr is what generation follows,
        # and a prose-only patch would change the words but not the behavior
        # (the loop must close on conduct, not prose).
        #
        # An explicit TARGET beats `kind`: {kind: "scope", target: "<an existing
        # definition>"} would otherwise draft a lens instruction, while the
        # identical prose under kind: "definition" targets correctly — so the fix a
        # curator asked for lands in a different artifact class than the one they
        # named. `target` names a definition term, and naming one IS the routing
        # decision; `kind` only decides when nothing is named.
        definition = _resolve_definition(bundle, trace, correction.note, target=target)
        term = (
            definition.term if definition is not None else target or trace.definition_used or "term"
        )
        body = _draft_definition_body(
            llm, definition, trace, correction.note, model, term=term, rejections=rejections
        )
        return PatchCandidate(
            ticket_id=ticket.ticket_id,
            lens=lens,
            kind="definition",
            target=term,
            owner=DEFAULT_OWNER,
            diff_before=definition.body if definition is not None else None,
            diff_after=body,
        )

    # 3. Stale data → re-profile the stalled table when named, else re-index.
    if correction.kind == "freshness":
        stalled = _stalled_table(profiles)
        if stalled is not None:
            table, detail = stalled
            return PatchCandidate(
                ticket_id=ticket.ticket_id,
                lens=lens,
                kind="reprofile",
                target=table,
                diff_after=f"Re-profile {table}: {detail}",
            )
        source = bundle.config.connections[0] if bundle.config.connections else lens
        return PatchCandidate(
            ticket_id=ticket.ticket_id,
            lens=lens,
            kind="reindex",
            target=source,
            diff_after=f"Re-index context sources for '{source}': {correction.note}",
        )

    # 4. A corrected answer → a candidate certified pair (deterministic next time).
    if corrected_sql:
        return PatchCandidate(
            ticket_id=ticket.ticket_id,
            lens=lens,
            kind="certified",
            target=trace.question,
            diff_before=trace.sql,
            diff_after=corrected_sql,
        )

    # 5. A recurring domain gap → an instruction appended to the lens's `instructions`.
    if correction.note.strip():
        return PatchCandidate(
            ticket_id=ticket.ticket_id,
            lens=lens,
            kind="instruction",
            target=lens,
            diff_before=bundle.config.instructions,
            diff_after=_draft_instruction(
                llm, trace, correction.note, model, rejections=rejections
            ),
        )
    return None
