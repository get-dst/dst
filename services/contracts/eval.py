"""Eval harness contracts — EvalCase, EvalRun, EvalResult.

All three are control-plane rows, persisted in Postgres via
``services/evals/store.py`` with org-scoped RLS. (The warehouse-native
``__dst`` eval plane that once mirrored results was removed — certified
answers are the sole value-level eval mechanism.)
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, field_validator, model_validator
from pydantic import Field as PField

# LEGACY READ PATH — delete after the release that follows migration 0031.
#
# Behavioral expectations ride the eval_case
# table's expected_answer column as a marker string ("expect:clarify
# term=value"), which avoided a migration at the time. Migration 0031 gave them
# ``expect``/``term`` columns and NULLed the markers, and NOTHING WRITES MARKERS
# ANY MORE. Decoding stays one release longer because a marker can still arrive
# from outside this wheel: a DB an older wheel wrote after being migrated, or a
# cases.yaml exported before 0031 and re-imported. Once every deployment is past
# 0031, drop this function, the _decode_behavioral validator, and their tests.
_EXPECT_PREFIX = "expect:"


def parse_behavioral_marker(text: str | None) -> tuple[str | None, str | None]:
    """Decode a legacy expected_answer marker into ``(expect, term)`` —
    ``(None, None)`` when *text* is prose (or None), i.e. a real oracle."""
    if not text or not text.startswith(_EXPECT_PREFIX):
        return None, None
    kind, _, term = text.removeprefix(_EXPECT_PREFIX).partition(" term=")
    if kind not in ("clarify", "refuse"):
        return None, None
    return kind, (term.strip() or None)


class EvalCase(BaseModel):
    """Definition of a single evaluation case stored on the control plane.

    Two shapes: a VALUE case carries expected_sql (no longer scored anywhere —
    certified answers are the regression suite; ``dst evals migrate`` converts,
    and the health runner still uses the SQL as its judge oracle until then);
    a BEHAVIORAL case carries
    ``expect: clarify | refuse | answer`` (+ optional ``term``) and is scored
    on response SHAPE by ``run_behavioral``. ``answer`` pins the
    opposite failure: a question the lens MUST answer with data — the
    regression where a lens starts refusing answerable questions was
    previously invisible unless the question was certified. The two shapes
    are mutually exclusive. ``expected_answer`` is a prose oracle no scorer
    reads (migration 0031 took the behavioral marker back off it).
    """

    id: str
    lens: str
    question: str
    expected_sql: str | None = None
    expected_answer: str | None = None
    expect: Literal["clarify", "refuse", "answer"] | None = None
    term: str | None = None
    snapshot_ref: str | None = None
    source: Literal["certified", "sample_query", "harvested", "authored"]
    status: Literal["candidate", "approved", "retired"] = "candidate"
    created_by: str = ""
    # Free-form classification (persona:cfo, intent:discriminator, …) — the
    # vocabulary is a project convention, deliberately not enforced here: dst
    # owns the slot, the customer owns the taxonomy. Lets a
    # battery be scored per intent/persona instead of only per lens.
    tags: list[str] = PField(default_factory=list)

    @field_validator("source", mode="before")
    @classmethod
    def _legacy_file_source(cls, v: object) -> object:
        # Applies once stamped file-authored cases as "file" (never a valid
        # literal) — with eval_gate on, loading them 500'd EVERY apply until
        # the gate was turned off. Coerce, don't crash.
        return "authored" if v == "file" else v

    @model_validator(mode="after")
    def _decode_behavioral(self) -> EvalCase:
        # LEGACY READ PATH (see parse_behavioral_marker): a pre-0031 row or an
        # exported-before-0031 file still carries the marker in expected_answer;
        # lift it into the typed fields so every consumer sees one shape. Delete
        # with the marker decoder.
        if self.expect is None:
            expect, term = parse_behavioral_marker(self.expected_answer)
            if expect is not None:
                self.expect = expect  # type: ignore[assignment]
                self.term = self.term or term
                self.expected_answer = None
        return self


class EvalRun(BaseModel):
    """Metadata for one eval run stored on the control plane.

    ``telemetry_ref`` once pointed at the warehouse-native ``__dst.eval_runs``
    mirror; that plane is gone and the column is inert (kept for old rows).
    """

    id: str
    lens: str
    lens_version: str | None = None
    started_at: str  # ISO-8601 timestamp
    mode: Literal["regression", "health", "behavioral"]
    score: float | None = None
    passed: int = 0
    failed: int = 0
    errored: int = 0
    telemetry_ref: str | None = None


class EvalResult(BaseModel):
    """Per-case outcome for one eval run.

    Modelled here so the runner, scorer, and telemetry layer share a typed contract.
    Persisted per run in the control-plane ``eval_result`` table (migration 0046)
    — the drill-down behind the accuracy trend. ``question`` is denormalized
    from the case so a persisted result reads on its own.
    """

    run_id: str
    case_id: str
    question: str = ""
    passed: bool
    grade: str | None = None
    checks: dict[str, Any] = PField(default_factory=dict)
    actual_sql: str | None = None
    actual_value: str | None = None
    reason: str | None = None
