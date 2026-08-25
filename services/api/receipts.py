"""POST /v1/verify-receipt — the consumer-side check on an answer receipt.

An answer's receipt travels further than its context: into slides, tickets,
other agents' transcripts. This door lets anyone in the org hold one up and
ask "did this server really serve these claims?" — two independent checks,
both deterministic, no LLM:

1. **Signature** — recompute the HMAC over the receipt's canonical form
   against every configured DST_SECRET_KEY (`valid` / `invalid` /
   `unsigned` — the receipt carries no digest / `unkeyed` — this server has
   no key to check with; a config gap is named as itself, never as forgery).
2. **Trace cross-check** — read the request_log row serving already wrote
   and compare the receipt's claims (lens, confidence, certification, the
   served SQL's hash) field by field; every disagreement is listed.

`ok` means both held: valid signature, trace found, zero mismatches.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text

from services.auth.deps import get_caller
from services.contracts.response import Receipt
from services.db.session import org_session
from services.governance.credentials import CallerIdentity
from services.runtime import receipt as receipt_mod

router = APIRouter(prefix="/v1", tags=["receipts"])


class ReceiptVerdict(BaseModel):
    ok: bool
    signature: receipt_mod.SignatureState
    trace_found: bool
    # Each entry names the field whose logged value disagrees with the receipt.
    mismatches: list[str]
    # The logged serve, for the human reading the verdict — what question this
    # receipt actually belongs to. None when no trace was found.
    question: str | None = None
    lens: str | None = None
    caller: str | None = None


@router.post("/verify-receipt")
def verify_receipt(body: Receipt, caller: CallerIdentity = Depends(get_caller)) -> ReceiptVerdict:
    """Check an answer's receipt: recompute its signature and cross-check every
    claim against the logged trace. `ok` = valid signature + trace found + zero
    mismatches; anything less names exactly what failed."""
    signature = receipt_mod.check_signature(body)
    with org_session(caller.org_id) as session:
        row = session.execute(
            text(
                "SELECT lens, caller, question, sql, confidence, certification "
                "FROM request_log WHERE request_id = :r"
            ),
            {"r": body.request_id},
        ).first()
    if row is None:
        return ReceiptVerdict(ok=False, signature=signature, trace_found=False, mismatches=[])
    logged_lens, logged_caller, question, logged_sql, logged_conf, logged_cert = row
    mismatches = [
        name
        for name, claimed, logged in (
            ("lens", body.lens, logged_lens),
            ("confidence", body.confidence, logged_conf),
            ("certification", body.certification, logged_cert or "none"),
            ("sql_sha256", body.sql_sha256, receipt_mod.sql_hash(logged_sql)),
        )
        if claimed != logged
    ]
    return ReceiptVerdict(
        ok=signature == "valid" and not mismatches,
        signature=signature,
        trace_found=True,
        mismatches=mismatches,
        question=question,
        lens=logged_lens,
        caller=logged_caller,
    )
