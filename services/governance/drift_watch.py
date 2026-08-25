"""Server-side schema drift: the applied baseline vs the warehouse, with a ticket.

The file-first `dst drift` verb is the authoring loop's surface; this module is
the SERVING side of the same comparison. The baseline here is what `dst apply`
landed from the probe artifact — the stored table profiles — because that is
the warehouse state the published layer was deployed against. Two callers:

- the serve-time backstop: a warehouse execution error on a governed
  query runs the same diff immediately — breaking drift files ONE
  deduplicated review ticket naming the affected entities, definitions and
  certified answers (the queue is the push channel), so the FIRST binder
  error becomes the incident instead of the eighty-first user complaint.

Deduplication is the fingerprint: sha256 over (connection, sorted schema
deltas). The review table's partial unique index on (org_id, fingerprint)
makes one-ticket-per-incident a database guarantee. No LLM anywhere here —
drift is a fact, and the ticket describes it deterministically.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from services.contracts.correction import CorrectionDelta
from services.contracts.semantic_model import Definition
from services.contracts.shared_semantic import SharedEntity
from services.lenses import profile_store
from services.lenses.connections import resolve_connector
from services.lenses.profiler_catalog import catalog_profiles
from services.project import warehouse_drift as wd
from services.reviews import store as reviews_store
from services.reviews.store import Ticket

log = logging.getLogger("dst")


@dataclass
class DriftReport:
    connection: str
    fingerprint: str
    findings: list[wd.Finding]

    @property
    def breaking(self) -> bool:
        return any(wd.is_breaking(f) for f in self.findings)

    def summary(self) -> str:
        """One deterministic paragraph naming what changed and what reads it."""
        heads = "; ".join(f"{f.table}: {f.kind} `{f.detail}`" for f in self.findings[:5])
        more = f" (+{len(self.findings) - 5} more)" if len(self.findings) > 5 else ""
        named: dict[str, list[str]] = {"entity": [], "definition": [], "certified": []}
        for f in self.findings:
            for r in f.refs:
                if r.name not in named[r.kind]:
                    named[r.kind].append(r.name)
        affected = "; ".join(
            f"{label}: {', '.join(names)}"
            for label, names in (
                ("entities", named["entity"]),
                ("definitions", named["definition"]),
                ("certified answers", named["certified"]),
            )
            if names
        )
        tail = f" — affects {affected}" if affected else " — nothing in the layer reads it"
        return f"schema drift on connection '{self.connection}': {heads}{more}{tail}"


def fingerprint(connection: str, parts: list[str]) -> str:
    """The deterministic identity of a standing incident on one connection."""
    digest = hashlib.sha256(("|".join([connection, *sorted(parts)])).encode()).hexdigest()
    return digest[:16]


def _layer(
    session: Session,
) -> tuple[dict[str, SharedEntity], dict[str, Definition]]:
    """The org's applied shared layer, keyed by its canonical file path — the
    pointer a finding carries so the fix starts in the right file."""
    from services.semantic import store as semantic_store

    entities: dict[str, SharedEntity] = {}
    definitions: dict[str, Definition] = {}
    for asset in semantic_store.list_assets(session):
        try:
            if asset.kind == "entity":
                entities[f"semantic/entities/{asset.name}.yaml"] = SharedEntity.model_validate(
                    asset.body
                )
            else:
                definitions[f"semantic/definitions/{asset.name}.md"] = Definition.model_validate(
                    asset.body
                )
        except ValueError:
            continue  # one malformed stored asset must not blind the diff
    return entities, definitions


def _certified(session: Session) -> list[wd.CertifiedRef]:
    """Every lens's active certified answers as the reference surface the diff
    crosses against — the approved SQL is the binding (see wd.CertifiedRef)."""
    from services.certify import store as certify_store
    from services.lenses import store as lens_store

    out: list[wd.CertifiedRef] = []
    for lens in lens_store.lens_names(session):
        for answer in certify_store.list_for_lens(session, lens):
            if certify_store.is_active(answer.status):
                out.append(
                    wd.CertifiedRef(
                        question=answer.question,
                        sql=answer.sql,
                        path=f"lenses/{lens}/certified_answers.yaml",
                    )
                )
    return out


def check_connection(
    session: Session, connection: str, org_id: uuid.UUID | None
) -> DriftReport | None:
    """Live catalog vs the applied baseline for one connection.

    None = no drift, or nothing to compare against (a connection whose profiles
    were never applied has no recorded baseline — that absence surfaces through
    `dst plan`'s UNARMED line, not as a phantom all-clear here)."""
    stored = profile_store.list_profiles(session, connection)
    if not stored:
        return None
    connector = resolve_connector(connection, org_id)
    current = catalog_profiles(connector, connection)
    entities, definitions = _layer(session)
    baseline = [s.profile for s in stored]
    drift = wd.baseline_drift(baseline, current)
    if not drift:
        return None
    findings = wd.cross_reference(drift, entities, definitions, _certified(session))
    return DriftReport(
        connection=connection,
        fingerprint=fingerprint(connection, [f"{d.table}:{d.kind}:{d.detail}" for d in drift]),
        findings=findings,
    )


def file_ticket(
    session: Session,
    report: DriftReport,
    *,
    origin: str,
    lens: str = "",
    caller: str = "",
    request_id: str | None = None,
) -> tuple[Ticket, bool]:
    """The report as ONE review ticket — (ticket, created). Dedup by fingerprint."""
    return reviews_store.create_incident_ticket(
        session,
        fingerprint=report.fingerprint,
        request_id=request_id or f"drift:{report.connection}:{report.fingerprint}",
        lens=lens,
        caller=caller or origin,
        origin=origin,
        correction=CorrectionDelta(kind="scope", note=report.summary()),
    )


# ── the caller-facing cause class ────────────────────────────────────────────
#
# Fixed strings only: the template these feed exists so raw engine shrapnel
# ("Binder Error: …") never reaches a business user again. A
# pattern that matches nothing degrades to the generic class — never to the
# raw text, and never to a model's paraphrase (no LLM on the error path).
# A dst-side CONFIGURATION limit is not a data problem: a
# bytesBilledLimitExceeded rejection classified into a drift bucket tells the
# user a column is missing and files a false data-team ticket — the classic
# silent-failure shape, a dst limit wearing another team's vocabulary. The
# config class is matched FIRST and closed-world: only it, never a drift
# bucket, and the serve backstop neither runs the schema diff nor opens a
# data-team ticket for it (`CONFIG_LIMIT_CAUSE`).
# The remediation names the FILE first and the env var second, in that order,
# because that is the order they should be reached for: a cost cap is a
# reviewed deployment fact, and a message that offers only the env var teaches
# an export nobody else's checkout has, for a value dst.yaml already accepts.
CONFIG_LIMIT_CAUSE = (
    "the query exceeds dst's configured warehouse cost cap — not a data "
    "problem; raise it per connection in dst.yaml (`config: {max_bytes_billed: "
    "…}`, the reviewed home), or globally with DST_BIGQUERY_MAX_BYTES_BILLED, "
    "or narrow the question"
)

# An engine error about the SQL's own vocabulary — unknown function, bad cast,
# wrong arguments — is dst's GENERATION fault: e.g. a generated YEAR() against
# BigQuery, which would otherwise dry-run into a data-team ticket no data
# engineer can action (the config-limit shape above, one layer down). It gets
# no ticket, no degraded mark, and an answer that names the right owner.
GENERATION_FAULT_CAUSE = (
    "dst generated SQL that is invalid for this warehouse's dialect — a dst "
    "generation defect, not a data problem"
)

_CAUSE_CLASSES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"bytesBilledLimitExceeded|bytes billed|maximum_bytes_billed"
            r"|quota exceeded|billing.{0,20}limit",
            re.IGNORECASE,
        ),
        CONFIG_LIMIT_CAUSE,
    ),
    (
        # Before the column/table classes: "No matching signature for
        # function X for argument types" would otherwise never be reached.
        re.compile(
            r"function not found|no matching signature|unknown function"
            r"|unrecognized function|invalid cast|could not cast"
            r"|wrong number of arguments|invalid argument type",
            re.IGNORECASE,
        ),
        GENERATION_FAULT_CAUSE,
    ),
    (
        re.compile(r"\bcolumns?\b|\bfields?\b", re.IGNORECASE),
        "a column the query needs is missing or renamed",
    ),
    (
        re.compile(r"\btables?\b|\brelations?\b|\bviews?\b", re.IGNORECASE),
        "a table the query needs is missing or renamed",
    ),
    (
        re.compile(
            r"\bpermission\b|access denied|\bforbidden\b|\bunauthorized\b|not authorized",
            re.IGNORECASE,
        ),
        "the warehouse refused access",
    ),
    (
        re.compile(r"\btimeout\b|\btimed? out\b|\bcancell?ed\b", re.IGNORECASE),
        "the warehouse timed out",
    ),
    (
        re.compile(r"\bconnect(ion)?\b|\bnetwork\b|\bhost\b", re.IGNORECASE),
        "the warehouse could not be reached",
    ),
    (re.compile(r"\bsyntax\b|\bparse\b", re.IGNORECASE), "the SQL did not parse on the warehouse"),
]


def cause_class(raw_error: str) -> str:
    """A raw engine error → its one-line cause class, deterministically."""
    for pattern, cause in _CAUSE_CLASSES:
        if pattern.search(raw_error):
            return cause
    return "query execution failure"
