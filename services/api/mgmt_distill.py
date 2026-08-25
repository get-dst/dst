"""Corpus distillation trigger. Admin-authed, org-scoped.

``POST /mgmt/lenses/{lens}/distill`` mines the lens's verified request history into
candidate patterns — certified pairs, skill instructions, missing definitions —
through the patch store, so they land on the same approval surface as
ticket-drafted patches (`GET /mgmt/lenses/{lens}/patches`, Reviews UI). Manually
triggered; schedule it from your own cron if you want it periodic.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from services.auth.deps import get_app_session
from services.evals.distill import distill_lens
from services.llm.assist import assist_llm

router = APIRouter(prefix="/mgmt/lenses/{lens}/distill", tags=["evals"])


@router.post("", status_code=201)
def distill(
    lens: str, min_count: int = 3, session: Session = Depends(get_app_session)
) -> list[dict[str, object]]:
    """Run the distiller once; returns the newly recorded patch candidates.

    Pattern naming / term extraction is fast-model prose (DeepSeek-first via
    ``assist_llm``); with no model configured the certified mining still runs and
    skill candidates fall back to a frequency note.
    """
    resolved = assist_llm()
    llm, model = (resolved.llm, resolved.name) if resolved is not None else (None, "unconfigured")
    try:
        candidates = distill_lens(session, llm, lens, min_count=min_count, model=model)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [c.model_dump() for c in candidates]
