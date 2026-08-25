"""Answer receipts: build and check the signed record of a serve.

The trust chain had one unverifiable hop left. `trust_summary` makes trust
legible through the agent (the agent quotes it), and the trace makes it
auditable from inside Observe — but a number pasted into a slide, a ticket, or
another agent's context carried nothing a skeptic could check. The receipt is
that check: a small signed block that rides every data answer, verifiable
later by anyone in the org via POST /v1/verify-receipt (signature AND
cross-check against the logged trace).

Signing is HMAC-SHA256 over canonical JSON (sorted keys, no whitespace, digest
field excluded), keyed by DST_SECRET_KEY — the same key list and rotation
contract as stored-secret encryption: first key signs, every key verifies.
Deliberately stateless: nothing new is persisted; verification recomputes the
signature and reads the request_log row that serving already writes.

No key configured → the receipt ships with digest=None. Unsigned is a state
the verify door reports out loud ("unsigned"); fabricating a digest, or
refusing to serve for lack of one, would both be worse.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Literal

from services.contracts.response import Receipt
from services.security.crypto import signing_keys

SignatureState = Literal["valid", "invalid", "unsigned", "unkeyed"]


def _canonical(receipt: Receipt) -> bytes:
    payload = receipt.model_dump(exclude={"digest"})
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _sign(data: bytes, key: str) -> str:
    return hmac.new(key.encode(), data, hashlib.sha256).hexdigest()


def sql_hash(sql: str | None) -> str | None:
    return hashlib.sha256(sql.encode()).hexdigest() if sql else None


def build(
    *,
    request_id: str,
    lens: str,
    certification: Literal["certified", "assisted", "none"],
    cert_id: str | None,
    confidence: str | None,
    sql: str | None,
    data_as_of: str | None,
) -> Receipt:
    receipt = Receipt(
        request_id=request_id,
        lens=lens,
        served_at=datetime.now(UTC).isoformat(timespec="seconds"),
        certification=certification,
        cert_id=cert_id,
        confidence=confidence,
        sql_sha256=sql_hash(sql),
        data_as_of=data_as_of,
    )
    keys = signing_keys()
    if keys:
        receipt.digest = _sign(_canonical(receipt), keys[0])
    return receipt


def check_signature(receipt: Receipt) -> SignatureState:
    """Recompute the HMAC. `unsigned` = the receipt carries no digest;
    `unkeyed` = it does but THIS server holds no key to check it with —
    a config gap named as itself, never reported as forgery."""
    if receipt.digest is None:
        return "unsigned"
    keys = signing_keys()
    if not keys:
        return "unkeyed"
    data = _canonical(receipt)
    for key in keys:
        if hmac.compare_digest(_sign(data, key), receipt.digest):
            return "valid"
    return "invalid"
