"""Corpus distiller — verified traces become candidate patterns.

Mines a lens's **verified, successful, un-certified** ``request_log`` traces and
*generalizes* them — never raw retrieval. The ablation rule is a hard constraint:
raw traces are NEVER written to the context store (``context_chunk``); the contract
is cluster → generalize → name → human-approve → prove with an eval.

Pipeline (``distill_rows`` is the pure core; ``distill_lens`` adds the I/O):

1. pull ``request_log`` rows with ``status='ok' AND confidence='verified' AND
   certification='none'`` (the 0016 columns — only graded-trustworthy, non-canned
   traces feed the corpus);
2. cluster by **sqlglot-normalized SQL shape** (literals stripped to placeholders —
   no embeddings): literal-variants of one query share a cluster;
3. clusters ≥ ``min_count``:
   - every exact question with byte-identical SQL repeated ≥ ``min_count`` times →
     a candidate **certified** pair (deterministic, no LLM needed);
   - otherwise the LLM names the pattern → a candidate lens **instruction**;
4. recurring question terms absent from ``semantic_model.definitions`` → candidate
   **definitions** (LLM-extracted, guarded against hallucination: a proposed term
   must literally appear in the questions).

Every output is a ``PatchCandidate`` with ``ticket_id=None`` and
``status="candidate"``, persisted through the ``patch_store`` — one human
approval surface (approve / edit / reject), and a file-owned fix comes back from
approval as a proposed file measured by the eval gate on the apply that lands it
exactly like ticket-drafted patches.

LLM call order (deterministic, for ScriptedLLM tests): one naming call per
instruction-bound cluster (clusters sorted by count desc, then shape), then one
term-extraction call. ``llm=None`` degrades gracefully: certified candidates are
still mined, instructions fall back to a frequency note, definition mining
is skipped.
"""

from __future__ import annotations

import json
from typing import NamedTuple

import sqlglot
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session
from sqlglot import exp

from services.certify import store as certify_store
from services.contracts.protocols import CacheableBlock, LLMProvider, Message
from services.contracts.semantic_model import SemanticModel
from services.lenses import store as lens_store
from services.reviews import patch_store
from services.reviews.patch import PatchCandidate


class TraceRow(NamedTuple):
    """The slice of a request_log row the distiller consumes."""

    question: str
    sql: str


class Cluster(NamedTuple):
    """Traces sharing one literal-free SQL shape."""

    shape: str
    rows: list[TraceRow]


# ── 2. SQL-shape normalization (sqlglot, no embeddings) ──────────────────────


def normalize_sql_shape(sql: str, dialect: str) -> str | None:
    """The canonical, literal-free shape of a statement — the cluster key.

    Parse + regenerate via sqlglot with every literal replaced by a placeholder,
    so ``WHERE n > 1`` and ``where n > 5`` share a shape. ``None`` = unparseable
    (the row is skipped, not guessed at).
    """

    def _strip(node: exp.Expression) -> exp.Expression:
        return exp.Placeholder() if isinstance(node, exp.Literal) else node

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
        return tree.transform(_strip).sql(dialect=dialect)
    except Exception:
        return None


def cluster_traces(rows: list[TraceRow], dialect: str) -> list[Cluster]:
    """Group traces by normalized SQL shape, largest cluster first (ties: by shape)."""
    groups: dict[str, list[TraceRow]] = {}
    for row in rows:
        shape = normalize_sql_shape(row.sql, dialect)
        if shape is not None:
            groups.setdefault(shape, []).append(row)
    return [
        Cluster(shape, members)
        for shape, members in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]


# ── 3a. exact-question + byte-identical-SQL → certified candidates ───────────


def _certified_candidates(cluster: Cluster, lens: str, min_count: int) -> list[PatchCandidate]:
    """Stable repeats inside a cluster: the same question (case/space-insensitive)
    answered by byte-identical SQL ≥ min_count times — certify it."""
    repeats: dict[tuple[str, str], list[TraceRow]] = {}
    for row in cluster.rows:
        repeats.setdefault((row.question.strip().lower(), row.sql), []).append(row)
    out: list[PatchCandidate] = []
    for members in repeats.values():
        if len(members) < min_count:
            continue
        first = members[0]
        out.append(
            PatchCandidate(
                ticket_id=None,
                lens=lens,
                kind="certified",
                target=first.question.strip(),
                diff_before=None,
                diff_after=first.sql,
            )
        )
    return out


# ── 3b. LLM names the recurring pattern → an instruction candidate ───────────

_NAME_SYSTEM = (
    "You distill recurring question-to-SQL patterns from a governed data lens's "
    "verified request history into reusable guidance. Given questions that all "
    "resolved to one SQL shape (literals replaced by ?), name the pattern and write "
    "ONE short imperative instruction telling the query model how to answer this "
    "family of questions. Respond with strict JSON: "
    '{"name": "<2-4 word pattern name>", "instruction": "<one sentence>"}.'
)

_MAX_SAMPLE_QUESTIONS = 5


def _strict_json(raw: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw.strip())
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _instruction_candidate(
    llm: LLMProvider | None, cluster: Cluster, lens: str, model: str
) -> PatchCandidate:
    questions = list(dict.fromkeys(r.question.strip() for r in cluster.rows))
    name, instruction = "", ""
    if llm is not None:
        sample = "\n".join(f"- {q}" for q in questions[:_MAX_SAMPLE_QUESTIONS])
        res = llm.complete(
            system=[CacheableBlock(_NAME_SYSTEM)],
            messages=[
                Message(
                    "user",
                    f"SQL shape: {cluster.shape}\nQuestions ({len(cluster.rows)} verified):\n"
                    f"{sample}",
                )
            ],
            model=model,
            temperature=0.0,
            max_tokens=300,
        )
        parsed = _strict_json(res.text)
        name = str(parsed.get("name") or "").strip()
        instruction = str(parsed.get("instruction") or "").strip()
    if not instruction:  # no LLM / bad JSON → still a useful, honest candidate
        instruction = f"Recurring pattern ({len(cluster.rows)} verified traces): {questions[0]}"
    header = f"[{name}] " if name else ""
    return PatchCandidate(
        ticket_id=None,
        lens=lens,
        kind="instruction",
        target=lens,
        diff_before=None,
        diff_after=f"{header}{instruction}\nExample SQL shape: {cluster.shape}",
    )


# ── 4. observed-but-undefined terms → definition candidates ──────────────────

_TERMS_SYSTEM = (
    "You maintain the governed business definitions of a data lens. Given the "
    "questions callers repeatedly ask and the terms already defined, identify up to "
    "three business terms that appear verbatim in the questions but are NOT yet "
    "defined, and draft a one-sentence definition for each. Skip table or column "
    "names and generic words. Respond with strict JSON: "
    '{"terms": [{"term": "<term>", "body": "<draft definition>"}]}.'
)


def _defined_terms(semantic_model: SemanticModel) -> set[str]:
    out: set[str] = set()
    for d in semantic_model.definitions:
        out.add(d.term.lower())
        out.add(d.term.lower().replace("_", " "))
    return out


def _definition_candidates(
    llm: LLMProvider | None,
    rows: list[TraceRow],
    semantic_model: SemanticModel,
    lens: str,
    model: str,
) -> list[PatchCandidate]:
    if llm is None or not rows:
        return []
    questions = list(dict.fromkeys(r.question.strip() for r in rows))
    defined = _defined_terms(semantic_model)
    res = llm.complete(
        system=[CacheableBlock(_TERMS_SYSTEM)],
        messages=[
            Message(
                "user",
                "Questions:\n"
                + "\n".join(f"- {q}" for q in questions)
                + "\nAlready defined: "
                + (", ".join(sorted(defined)) or "(none)"),
            )
        ],
        model=model,
        temperature=0.0,
        max_tokens=500,
    )
    raw_terms = _strict_json(res.text).get("terms")
    if not isinstance(raw_terms, list):
        return []
    corpus = " ".join(q.lower() for q in questions)
    out: list[PatchCandidate] = []
    for item in raw_terms:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        body = str(item.get("body") or "").strip()
        lowered = term.lower()
        if not term or not body:
            continue
        if lowered in defined or lowered.replace("_", " ") in defined:
            continue
        if lowered.replace("_", " ") not in corpus:  # hallucination guard
            continue
        out.append(
            PatchCandidate(
                ticket_id=None,
                lens=lens,
                kind="definition",
                target=term,
                diff_before=None,
                diff_after=body,
            )
        )
    return out


# ── the distiller ─────────────────────────────────────────────────────────────


def distill_rows(
    llm: LLMProvider | None,
    rows: list[TraceRow],
    bundle: lens_store.LensBundle,
    *,
    min_count: int = 3,
    model: str = "claude-haiku-4-5",
) -> list[PatchCandidate]:
    """Pure core: verified traces → candidate patterns (no DB; ScriptedLLM-testable)."""
    lens = bundle.config.name
    candidates: list[PatchCandidate] = []
    for cluster in cluster_traces(rows, bundle.semantic_model.dialect):
        if len(cluster.rows) < min_count:
            continue
        certified = _certified_candidates(cluster, lens, min_count)
        if certified:
            candidates.extend(certified)
        else:
            candidates.append(_instruction_candidate(llm, cluster, lens, model))
    candidates.extend(_definition_candidates(llm, rows, bundle.semantic_model, lens, model))
    return candidates


def _verified_rows(session: Session, lens: str) -> list[TraceRow]:
    """The distillable corpus: ok + verified + not served from the certified store."""
    rows = session.execute(
        sa_text(
            "SELECT question, sql FROM request_log "
            "WHERE lens = :lens AND status = 'ok' AND confidence = 'verified' "
            "AND certification = 'none' AND sql IS NOT NULL "
            "ORDER BY created_at, id"
        ),
        {"lens": lens},
    ).all()
    return [TraceRow(question=r[0], sql=r[1]) for r in rows]


def _load_bundle(session: Session, lens: str) -> lens_store.LensBundle:
    row = lens_store.get_lens(session, lens)
    if row is None:
        raise LookupError(f"lens '{lens}' not found")
    raw = row.get("draft") or row.get("published")
    if raw is None:
        raise LookupError(f"lens '{lens}' has no bundle")
    return lens_store.LensBundle.model_validate(raw)


def template_candidates(
    answers: list[certify_store.CertifiedAnswer], lens: str, dialect: str
) -> list[PatchCandidate]:
    """DETECTION only: certified pairs that share one literal-free SQL shape
    and differ only in literals are one template wearing N frozen costumes.
    The candidate carries the cluster; the PARAMETERIZING judgment (slot
    names/types, the placeholder question) stays with the reviewing
    human/agent — the dst-certify skill teaches it.
    Templates themselves (slots set) never re-cluster."""
    plain = [a for a in answers if certify_store.is_active(a.status) and not a.slots]
    groups: dict[str, list[certify_store.CertifiedAnswer]] = {}
    for a in plain:
        shape = normalize_sql_shape(a.sql, dialect)
        if shape is not None:
            groups.setdefault(shape, []).append(a)
    out: list[PatchCandidate] = []
    for _shape, members in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(members) < 2 or len({m.sql for m in members}) < 2:
            # One member is no family; identical SQL is a stable repeat (the
            # trace distiller's certified candidate), not a literal family.
            continue
        members = sorted(members, key=lambda m: m.question)
        lines = [
            f"# {len(members)} certified answers share one SQL shape and differ only in",
            "# literals — one template with slots + sample_bindings covers the family",
            "# (the dst-certify skill walks the authoring; humans approve).",
        ]
        for m in members:
            lines += [f"- question: {m.question}", f"  sql: {m.sql}"]
        out.append(
            PatchCandidate(
                lens=lens,
                kind="certified",
                target=f"template: {members[0].question}",
                diff_after="\n".join(lines),
            )
        )
    return out


def distill_lens(
    session: Session,
    llm: LLMProvider | None,
    lens: str,
    *,
    min_count: int = 3,
    model: str = "claude-haiku-4-5",
) -> list[PatchCandidate]:
    """Distill a lens's verified request history into persisted patch candidates.

    Pulls the corpus, runs ``distill_rows``, skips candidates already proposed
    (certified/definition: same kind+target; instruction: same kind+target+text —
    re-runs are idempotent), records the rest via ``patch_store`` and returns them
    with ids.
    Raises ``LookupError`` when the lens (or its bundle) doesn't exist. Never touches
    ``context_chunk`` — distilled output goes through the human gate, not retrieval.
    """
    bundle = _load_bundle(session, lens)
    candidates = distill_rows(
        llm, _verified_rows(session, lens), bundle, min_count=min_count, model=model
    )
    # Same-shape/different-literal certified clusters → ONE template
    # candidate each (detection only; authoring judgment stays with the skill).
    candidates += template_candidates(
        certify_store.list_for_lens(session, lens), lens, bundle.semantic_model.dialect
    )
    existing = patch_store.list_for_lens(session, lens)
    seen = {(c.kind, c.target, c.diff_after if c.kind == "instruction" else None) for c in existing}
    recorded: list[PatchCandidate] = []
    for cand in candidates:
        key = (cand.kind, cand.target, cand.diff_after if cand.kind == "instruction" else None)
        if key in seen:
            continue
        seen.add(key)
        cand.id = patch_store.record(session, cand)
        recorded.append(cand)
    return recorded
