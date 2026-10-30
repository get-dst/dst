"""Correction delta — the wrong-vs-right structure on a review ticket.

A ticket that carries one of these is a *correction*: not just "this answer was wrong"
but which kind of wrong and, when the caller/owner knows it, the corrected answer/SQL.
``corrected_sql`` is the strongest signal — a ruling can certify it. ``target``
pins WHICH term the correction is about, so the fix the reviewer authors lands
on the right definition page rather than on whichever page shares the most words.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

CorrectionKind = Literal["definition", "scope", "number", "freshness", "other"]


class CorrectionDelta(BaseModel):
    kind: CorrectionKind
    note: str  # what the caller/owner says is wrong
    corrected_answer: str | None = None
    corrected_sql: str | None = None
    # P19a: the definition term this correction is about — used VERBATIM by the
    # drafter (an unknown term drafts a NEW definition), and AUTHORITATIVE over
    # `kind`: naming a term is the routing decision, whatever kind of wrong it was
    # filed as. Absent → note-based routing.
    target: str | None = None
