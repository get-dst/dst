"""The query response shape — returned identically by REST and MCP.

Dual-shape: prose `answer` for humans + structured fields for agents.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from services.contracts.verification import VerificationReport


class Citation(BaseModel):
    type: Literal["definition", "sql", "context"]
    ref: str


class CertifiedProvenance(BaseModel):
    """Who approved the served SQL and when — present iff certification == 'certified'."""

    cert_id: str
    certified_by: str
    certified_at: str
    # Set iff certified_match == "parameterized" — the validated slot
    # values this serve bound into the approved SQL shape (canonical spellings).
    bound_values: dict[str, str] | None = None


class DataPayload(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[list[object]] = Field(default_factory=list)
    # The query's FULL row count, and whether `rows` carries fewer than that
    # (the lens's max_rows_to_return cap). A silently short payload turns
    # correct SQL into a wrong answer — a 271-row result coming back as exactly
    # 200 rows with nothing on the wire saying so, where the only tell is the
    # round number. Callers that page or total MUST read these.
    row_count: int | None = None
    # False when the engine-side fetch cap bit: `row_count` is then a FLOOR ("at
    # least this many"), not the total. Reporting the cap as an exact count is how
    # a 21,056-row answer came back claiming 5000 — a caller that pages or totals
    # must read this before trusting `row_count`.
    row_count_exact: bool = True
    truncated: bool = False


class TruncationInfo(BaseModel):
    """The row cap bit: the payload carries `returned` of the query's `total`
    result rows. `total` is None when the engine-side fetch cap bit — the true
    count is then unknown and must never be stated as a number. On the ENVELOPE
    (not only inside `data`) so a `format: "prose"` caller, whose data block is
    dropped, still holds the fact deterministically."""

    returned: int
    total: int | None = None


class ClarificationRequest(BaseModel):
    """The question cannot be answered as asked: ask, don't guess.

    Two kinds, told apart by ``kind``. ``ambiguous_term``: a governed term has
    several meanings — `term` names it, `options` are its canonical
    possible_mappings; re-ask with the chosen meaning spelled out.
    ``unknown_value``: a filter value the question used does not exist in the
    column it filters (investigated against the warehouse, not guessed) —
    `term` names the COLUMN, `options` are the values it actually holds; re-ask
    with the stored value you mean, or read the absence as the answer.
    `question` is ready to relay verbatim either way. Defaulted so every
    response stored before the field existed keeps validating as what it was."""

    term: str
    question: str
    options: list[str] = Field(default_factory=list)
    kind: Literal["ambiguous_term", "unknown_value"] = "ambiguous_term"


class Receipt(BaseModel):
    """A portable, verifiable record of what served — the trust artifact that
    survives being pasted out of the answer's context.

    `trust_summary` made trust legible through the agent hop; the receipt makes
    it CHECKABLE after it: anyone in the org can POST it back to
    /v1/verify-receipt and learn whether these exact claims were really served,
    by this server, against the logged trace. `digest` is HMAC-SHA256 over the
    other fields keyed by DST_SECRET_KEY; None = the server had no key — an
    unsigned receipt says so, it never fakes a signature."""

    request_id: str
    lens: str
    served_at: str  # ISO instant, UTC
    certification: Literal["certified", "assisted", "none"] = "none"
    cert_id: str | None = None
    confidence: str | None = None
    # SHA-256 of the exact SQL that served — pins the receipt to the query,
    # without carrying the query (receipts travel further than SQL should).
    sql_sha256: str | None = None
    data_as_of: str | None = None
    digest: str | None = None


class QueryResponse(BaseModel):
    lens: str
    # What this response IS, so a caller never has to read English to find out.
    # Same vocabulary the trace has always carried (contracts/trace.py), minus
    # "deny" — an authorization denial is an HTTP error and never becomes a body:
    #   ok            an answer: SQL ran, `data` and `verification` are populated
    #   refused       the lens DECLINED — the question is not answerable from its
    #                 data and it named the gap. A governed outcome, not a fault;
    #                 refusing beats a low-confidence answer, and this is the
    #                 field that keeps that promise legible.
    #   clarification the lens needs an ambiguous governed term pinned down first
    #   rejected      no in-scope SQL could be formed (the guard, or a parse error)
    #   error         SQL never executed: the warehouse or the provider failed
    # Only "ok" carries an answer. Defaults to "ok" so every existing caller,
    # and every response built elsewhere, keeps validating unchanged.
    status: Literal["ok", "refused", "clarification", "rejected", "error"] = "ok"
    answer: str
    data: DataPayload | None = None
    sql: str | None = None
    definition_used: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    # The graded trust signal: derived from the named checks in
    # `verification`, never asserted on its own. Certified answers are the top tier.
    confidence: Literal["verified", "partial", "unverified"] | None = None
    verification: VerificationReport | None = None
    # "certified" = served from an approved question→SQL pair; "assisted" = certified
    # exemplars guided generation; "none" = unguided. "certified_failed" = an
    # approved answer's SQL ERRORED at execution — the approval names a query the
    # warehouse no longer runs, and the badge must not stand beside the error as
    # plain `certified`: a trust decoration may never outrank the fault beside it.
    certification: Literal["certified", "assisted", "none", "certified_failed"] = "none"
    # How the certified serve matched, present iff certification == "certified"
    # "exact" = the approved question itself (≥0.95 embedding match, a
    # normalized-exact page match, or the certified door); "equivalent" = a
    # 0.90–0.95 near-miss a cheap-model gate confirmed asks the same thing;
    # "parameterized" = an approved TEMPLATE bound with this
    # question's validated slot values (see certified_provenance.bound_values).
    # An agent can quote an "exact" number as approved; "equivalent" warrants
    # showing the approved question's wording alongside; "parameterized" means
    # approved SQL shape, YOUR values — name them when presenting.
    certified_match: Literal["exact", "equivalent", "parameterized"] | None = None
    certified_provenance: CertifiedProvenance | None = None
    # Set ⇒ this is NOT a data answer: an ambiguous governed term needs the caller
    # to pick a meaning first (answer carries the clarify question for old clients).
    clarification: ClarificationRequest | None = None
    # Pre-rendered one-liner an agent can quote verbatim to the end user; set on
    # certified answers so trust survives the hop through the agent.
    trust_summary: str | None = None
    # The scope's measured freshness floor (oldest table last-update), ISO date —
    # from the table profile, never asserted.
    data_as_of: str | None = None
    # "fallback" = the composed prose failed the numeric_grounding gate, a single
    # retry failed it again, and the prose was WITHHELD: `answer` is a
    # code-generated frame over the data block, never model prose. Absent on
    # every other serve (including certified, which never composes freely).
    # A degraded-but-true answer is an outcome; an invented figure is not.
    composition: Literal["fallback"] | None = None
    # Set iff the payload carries fewer rows than the query's result. Truncation
    # is a stamped fact, not a hope: the pipeline's code-appended prose line and
    # numeric_grounding's truncated mode key off the same decision that sets
    # this. None = the payload is complete (or this is not a data answer).
    truncated: TruncationInfo | None = None
    # Governance capabilities that could NOT run for this answer, each a
    # "DEGRADED: …" line an agent can relay verbatim. Empty on a healthy serve.
    # Today's one entry is certified matching with no usable embedder: matching
    # is pgvector cosine over a question vector, so a dead embedder turns the
    # certified door off entirely, silently, while every response still looks
    # like an ordinary generated answer. A caller
    # that quotes `certification: "none"` as "no approved answer covers this"
    # MUST read this first: it may mean "nothing was checked".
    degraded: list[str] = Field(default_factory=list)
    # Signed record of this serve (see Receipt) — present on data answers; a
    # refusal or clarification makes no data claim to attest.
    receipt: Receipt | None = None
    request_id: str
