"""The generation-input assembly seam.

One function builds everything question-dependent that generation sees — retrieved
the certified-definitions page, profile enrichment,
certified serve/exemplar decisions — and one selects the generator tiering over the
result. The serving path (`services/api/query.py`) and the eval suites call the SAME
seam, so evals grade the pipeline production actually runs (manual §6.7 item 3).

Every step is best-effort fail-open exactly as it was in query.py — a missing
embedder or a broken profile never blocks an answer; live failures surface via
`context.store.LAST_SERVING_ERROR` → /ready.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from services.benchmark.contamination import normalize
from services.certdefs import load_certified_defs, select_context
from services.certify import binding as certify_binding
from services.certify import store as certify_store
from services.context import store as ctx_store
from services.contracts.lens_config import LensConfig
from services.contracts.profile import TableProfile
from services.contracts.protocols import (
    CacheableBlock,
    ContextChunk,
    LLMProvider,
    Message,
    QueryGenerator,
)
from services.contracts.semantic_model import SampleQuery, SemanticModel
from services.db.session import org_session
from services.lenses import profile_enrich, profile_store
from services.lenses.store import LensBundle
from services.llm import registry
from services.llm.assist import assist_llm
from services.runtime import value_guard
from services.runtime.bounded import call_bounded
from services.runtime.generator import FixedSQLGenerator, GroundedSQLGenerator
from services.runtime.intent_generator import IntentSQLGenerator

log = logging.getLogger("dst")

# Certified-answer match thresholds (cosine similarity). At/above EXACT we serve the
# approved SQL directly (skip the LLM); at/above ASSIST we inject matches as exemplars.
# In the EQUIV..EXACT band a cheap model decides whether the question is a paraphrase
# of the approved one — if so we serve it deterministically (a re-worded question
# should still hit the approved answer, not a fresh generation).
CERTIFIED_EXACT = 0.95
CERTIFIED_EQUIV = 0.90
CERTIFIED_ASSIST = 0.83


@dataclass(frozen=True)
class CertifiedBands:
    exact: float
    equiv: float
    assist: float


_DEFAULT_BANDS = CertifiedBands(CERTIFIED_EXACT, CERTIFIED_EQUIV, CERTIFIED_ASSIST)

# Bands are EMBEDDER-RELATIVE — small local models spread cosines where hosted
# models compress them, so one universal set of constants strands the serve
# bands on some embedders (paraphrases that should serve never clear the
# threshold). Presets are calibrated per model dst itself pins; unknown
# embedders get the defaults. dst-owned, never a knob.
_BAND_PRESETS: dict[str, CertifiedBands] = {
    "BAAI/bge-small-en-v1.5": CertifiedBands(exact=0.93, equiv=0.80, assist=0.78),
}


def certified_bands() -> CertifiedBands:
    """Serve bands for the CONFIGURED embedder (specs lookup only — no client
    is constructed). Rebuilt per call, like the registry itself."""
    name = registry.embedding_model_name()
    return _BAND_PRESETS.get(name or "", _DEFAULT_BANDS)


# The temporal-qualifier veto's extraction lives in services/runtime/timewindow
# (shared with the window_applied verification check); re-exported
# here because the veto is applied in certified_lookup below.
from services.runtime.timewindow import temporal_mismatch, temporal_terms  # noqa: E402, F401

_EQUIV_SYSTEM = (
    "Two analytics questions are given (A is approved, B is being asked). Answer with "
    "ONLY 'yes' or 'no': would the SAME SQL query correctly answer BOTH — same metric, "
    "grain, filters and entities? Wording, order, and politeness don't matter; a "
    "different number, time window, entity, or aggregation means 'no'."
)

# The bind gate — one cheap structured call per template hit. The
# model only PROPOSES values; services/certify/binding.py validates every one
# deterministically before anything reaches SQL, and any miss falls through to
# generation (the safe failure mode).
_BIND_SYSTEM = (
    "An APPROVED analytics question TEMPLATE (with typed {slot} placeholders) and an "
    "incoming question are given. Decide whether the incoming question asks the SAME "
    "thing as the template — same metric, grain, filters, entities — with only the "
    "declared slot values differing. Respond with ONLY a JSON object, no prose:\n"
    '{"match": true, "bindings": {"<slot>": "<value>", ...}}\n'
    'or {"match": false, "bindings": {}}\n'
    "Slot value grammar (canonical, exact — no other forms):\n"
    "- date_range: YYYY | YYYY-Qn | YYYY-MM | YYYY-MM-DD/YYYY-MM-DD "
    "(e.g. Q3 2026 -> 2026-Q3, July 2026 -> 2026-07)\n"
    "- date: YYYY-MM-DD\n"
    "- number: plain digits\n"
    "- enum: exactly one of the values listed for that slot\n"
    'A relative period you cannot resolve ("last quarter"), a value outside the '
    'grammar, or any doubt -> {"match": false, "bindings": {}}.'
)


@dataclass(frozen=True)
class AssembledInputs:
    """Everything question-dependent the pipeline consumes. `counts` is the
    assembly's own visibility — eval output records it so a leaner-than-production
    run (no embedder, no profiles) is visible instead of silent."""

    model: SemanticModel
    prose: list[ContextChunk]
    certified: certify_store.CertifiedAnswer | None
    certification: Literal["certified", "assisted", "none"]
    certified_sql: str | None
    data_as_of: str | None
    counts: dict[str, int]
    # Which band served a certified answer: "exact" ≥ CERTIFIED_EXACT,
    # "equivalent" = the paraphrase-gated EQUIV..EXACT band, "parameterized"
    # "parameterized" = a template bound via the bind gate. None otherwise.
    certified_match: Literal["exact", "equivalent", "parameterized"] | None = None
    # The validated canonical slot values a template serve bound.
    certified_bound: dict[str, str] | None = None
    # Capabilities that could NOT run for this question — today, certified
    # matching with no usable embedder. Rides onto the response and the trace
    # (services/api/query.py) so a degraded serve says so instead of looking
    # like an ordinary generated answer.
    degraded: tuple[str, ...] = ()
    # column name -> its complete value dictionary (value_guard.value_domains):
    # the pipeline's deterministic check that a filter literal exists at all.
    value_domains: dict[str, list[str]] = field(default_factory=dict)


def certified_equivalent(llm: LLMProvider, model: str, question: str, approved: str) -> bool:
    """Cheap-model gate: is ``question`` a paraphrase of the ``approved`` question?

    Fails closed (``False``) on any provider error — a missed promotion just means
    generation runs, never that a wrong approved SQL is served.
    """
    try:
        res = llm.complete(
            system=[CacheableBlock(_EQUIV_SYSTEM)],
            messages=[Message("user", f"A: {approved}\nB: {question}")],
            model=model,
            temperature=0.0,
            max_tokens=4,
        )
    except Exception:
        log.exception("certified equivalence check failed")
        return False
    return res.text.strip().lower().startswith("y")


def certified_bind(
    llm: LLMProvider, model: str, question: str, answer: certify_store.CertifiedAnswer
) -> dict[str, str] | None:
    """The template bind gate — does ``question`` ask the template's
    question for specific slot values, and what are they?

    One cheap structured call proposes ``{match, bindings}``; every proposed
    value is then validated deterministically against the slot types. Fails
    closed (``None``) on provider error, unparseable output, ``match: false``,
    or ANY validation miss — a mis-bound certified answer would be a
    confidently wrong number wearing the trust badge, so the safe failure mode
    is falling through to generation.
    """
    specs, spec_errors = certify_binding.parse_slots(answer.slots)
    if spec_errors:
        log.warning("template %s has invalid slots — never served: %s", answer.id, spec_errors)
        return None
    slot_lines = "\n".join(
        f"- {name}: {spec.type}" + (f" (values: {', '.join(spec.values)})" if spec.values else "")
        for name, spec in specs.items()
    )
    user = (
        f"Template question: {answer.question}\nSlots:\n{slot_lines}\n\n"
        f"Incoming question: {question}"
    )
    try:
        res = llm.complete(
            system=[CacheableBlock(_BIND_SYSTEM)],
            messages=[Message("user", user)],
            model=model,
            temperature=0.0,
            max_tokens=300,
        )
    except Exception:
        log.exception("certified bind gate failed")
        return None
    try:
        obj = json.loads(re.sub(r"^```[a-zA-Z]*|```$", "", res.text.strip()).strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("match") is not True:
        return None
    raw = obj.get("bindings")
    if not isinstance(raw, dict):
        return None
    canonical, errors = certify_binding.validate_binding(specs, raw)
    if errors:
        log.info("bind gate proposed invalid values for %s: %s", answer.id, errors)
        return None
    return canonical


MATCHING_DOWN = (
    "DEGRADED: certified matching did not run for this question ({why}) — the answer "
    "was GENERATED, not matched against the certified corpus, and no certified answer "
    "could have been served no matter how exactly it was asked"
)
NO_EMBEDDER = "this install has no usable embedding provider"


def embed_question(question: str) -> list[float] | None:
    """The question vector, or None when this install cannot produce one.
    ``embed_question_degraded`` returns the same vector WITH the reason —
    callers on the serving path want that one."""
    return embed_question_degraded(question)[0]


def embed_question_degraded(question: str) -> tuple[list[float] | None, str | None]:
    """(vector, why-it-is-missing). The reason is the point: matching is pgvector
    cosine over this vector, so no vector means ``certified_lookup`` returns
    "none" and the certified door — by far the fastest serving path — cannot
    fire at all. Without the reason, that is indistinguishable from a question
    the corpus simply does not cover, so a reaped model cache can hide behind it
    indefinitely. The caller puts the reason on the response and the trace."""

    # Bounded: resolve AND embed run under the serving timeout — resolution
    # constructs the embedder, and a local embedder's constructor downloads
    # model weights, which can hang a live query for the length of the download.
    # Fail-open as ever: no vector means no retrieval and no certified match,
    # visible in the log and on /ready via LAST_SERVING_ERROR — never a stall.
    def _embed() -> list[float] | None:
        embedder = registry.resolve_embedder()
        return None if embedder is None else embedder.embed([question])[0]

    try:
        vec = call_bounded("question embedding", _embed)
    except Exception as exc:
        log.exception("question embedding failed")
        ctx_store.LAST_SERVING_ERROR["error"] = f"question embedding failed: {exc}"
        return None, str(exc)
    if vec is None:
        return None, NO_EMBEDDER
    ctx_store.LAST_SERVING_ERROR.clear()
    return vec, None


def certified_lookup(
    lens: str,
    qvec: list[float] | None,
    org_id: uuid.UUID | str,
    question: str,
    *,
    serve: bool = True,
    exclude_id: str | None = None,
) -> tuple[
    certify_store.CertifiedAnswer | None,
    list[SampleQuery],
    Literal["certified", "assisted", "none"],
    Literal["exact", "equivalent", "parameterized"] | None,
    dict[str, str] | None,
]:
    """Returns (certified_answer, exemplars, mode, match, bound).

    Serves the approved answer on an exact match, or on a near-miss (EQUIV..EXACT)
    that a cheap model confirms is a paraphrase of the approved question; otherwise
    folds near matches in as exemplars. ``match`` names which band served
    ``bound`` carries a template serve's validated slot values.
    A TEMPLATE hit never serves on score alone — the bind gate is
    mandatory at every band ≥ EQUIV (a 0.95 hit against the sample-bound anchor
    can still be a different quarter), and templates never fold as exemplars
    (placeholder SQL would teach placeholder syntax). With ``serve=False`` the
    serve branches are skipped and only exemplars fold (the eval suites test
    generation, not the bypass); ``exclude_id`` keeps an answer under test out
    of its own exemplars.
    """
    if qvec is None:
        return None, [], "none", None, None
    try:
        with org_session(org_id) as session:
            hits = certify_store.search(session, lens, qvec, k=5)
    except Exception:
        log.exception("certified lookup failed for lens %s", lens)
        return None, [], "none", None, None
    hits = [h for h in hits if h.answer.id != exclude_id]
    if not hits:
        return None, [], "none", None, None
    bands = certified_bands()
    if serve and hits[0].score >= bands.equiv:
        top = hits[0].answer
        if top.slots:
            if (pair := assist_llm()) is not None:
                bound = certified_bind(pair.llm, pair.name, question, top)
                if bound is not None:
                    return top, [], "certified", "parameterized", bound
        elif temporal_mismatch(question, top.question):
            # A "this quarter" answer must never serve a "last quarter" ask —
            # cosine cannot see the qualifier. Fall through: the pair still
            # folds as an exemplar below, and generation handles the asked
            # window (the matcher is the wrong path here, not generation).
            pass
        elif hits[0].score >= bands.exact:
            return top, [], "certified", "exact", None
        elif (pair := assist_llm()) is not None:
            if certified_equivalent(pair.llm, pair.name, question, top.question):
                return top, [], "certified", "equivalent", None
    exemplars = [
        SampleQuery(question=h.answer.question, sql=h.answer.sql)
        for h in hits
        if h.score >= bands.assist and not h.answer.slots
    ]
    return (
        (None, exemplars, "assisted", None, None) if exemplars else (None, [], "none", None, None)
    )


def certified_sql(certified_dir: str | None, question: str) -> str | None:
    """Strong binding: if the question exactly matches a certified-definition page's
    question, serve that page's approved SQL deterministically — the era-union /
    dedup query the semantic model's naive entity would otherwise lose to. Exact
    (normalized) match only: high precision, no chance of serving the wrong metric."""
    if not certified_dir:
        return None
    try:
        metrics = load_certified_defs(Path(certified_dir))
    except Exception:
        log.exception("certified load failed for %s", certified_dir)
        return None
    norm = normalize(question)
    for m in metrics:
        if m.question and m.sql and normalize(m.question) == norm:
            return m.sql
    return None


def certified_context(certified_dir: str | None, question: str) -> ContextChunk | None:
    """Certified definitions as a context source. If the lens declares a
    certified-definitions directory, inject the per-question pages (rules + SQL exemplars)
    — the strongest lever there is on a large, unfamiliar warehouse. Best-effort: a bad
    path never breaks a query. (Today the storage is a server-side directory.)"""
    if not certified_dir:
        return None
    try:
        metrics = load_certified_defs(Path(certified_dir))
        text = select_context(metrics, question)
        return ContextChunk(text=text, source="certified") if text else None
    except Exception:
        log.exception("certified load failed for %s", certified_dir)
        return None


def profile_facts(
    bundle: LensBundle, org_id: uuid.UUID | str
) -> tuple[list[TableProfile], str | None]:
    """Stored table profiles for the lens's connection + the scope's data_as_of
    Best-effort: no profile, no enrichment."""
    if not bundle.config.connections:
        return [], None
    try:
        with org_session(org_id) as session:
            stored = profile_store.list_profiles(session, bundle.config.connections[0])
        profiles = [s.profile for s in stored]
        tables = {e.source.table for e in bundle.semantic_model.entities}
        as_of = profile_enrich.data_as_of(profiles, tables)
        return profiles, as_of.date().isoformat() if as_of else None
    except Exception:
        log.exception("profile lookup failed for lens %s", bundle.config.name)
        return [], None


# The answer contract — the output-grid default. A recurring class of misses is
# CORRECT values in the WRONG grid (year groupings rendered as dates, working
# columns left in the final SELECT), and the only other defense is per-project
# convention prose. Rides every generation prompt as
# the last context block (adjacent to the question); lens.yaml
# `model.answer_contract: off` removes it. Prompt-side only — no SQL guard.
# The source label is a chunk-stream marker, not provenance: the composer
# excludes it from citations (an instruction is not a source the answer drew on).
ANSWER_CONTRACT_SOURCE = "answer contract"
ANSWER_CONTRACT = (
    "The final SELECT must project exactly the quantities the question names plus "
    "their grouping keys — nothing else; working columns stay in CTEs. Return "
    "results at the grain the question asks: a year grouping is the year value, "
    "not a date (sub-year periods follow the project's conventions, if any). "
    "Keep full numeric precision unless the question specifies rounding."
)


def assemble(
    bundle: LensBundle,
    question: str,
    org_id: uuid.UUID | str,
    *,
    serve_certified: bool = True,
    exclude_certified_id: str | None = None,
) -> AssembledInputs:
    """The per-question input assembly production serves: retrieval, certified page,
    profile enrichment, exemplar folding, certified serve decisions.
    Evals call it with ``serve_certified=False`` (the bypass would trivially pass)
    and the answer under test excluded from its own exemplars."""
    name = bundle.config.name
    qvec, embed_failure = embed_question_degraded(question)
    chunk = certified_context(bundle.config.model.certified_dir, question)
    prose = [chunk] if chunk is not None else []
    model = bundle.semantic_model
    # The stored table profile feeds generation — enum literals, sampled
    # descriptions, partition-pruning hints land in the (cached) system prompt.
    profiles, as_of = profile_facts(bundle, org_id)
    domains: dict[str, list[str]] = {}
    if profiles:
        model = profile_enrich.enrich_model(model, profiles)
        domains = value_guard.value_domains(model, profiles)

    # Certified-answer repository: serve an approved SQL directly on an exact match,
    # or fold near-matches in as few-shot exemplars to guide generation.
    certified, exemplars, certification, match, bound = certified_lookup(
        name, qvec, org_id, question, serve=serve_certified, exclude_id=exclude_certified_id
    )
    if exemplars:
        model = model.model_copy(update={"sample_queries": list(model.sample_queries) + exemplars})
    csql = certified_sql(bundle.config.model.certified_dir, question) if serve_certified else None
    n_chunks = len(prose)  # retrieval visibility only — the contract below is not context
    if bundle.config.model.answer_contract != "off":
        prose = [*prose, ContextChunk(text=ANSWER_CONTRACT, source=ANSWER_CONTRACT_SOURCE)]
    return AssembledInputs(
        model=model,
        prose=prose,
        certified=certified,
        certification=certification,
        certified_match=match,
        certified_bound=bound,
        certified_sql=csql,
        data_as_of=as_of,
        degraded=() if embed_failure is None else (MATCHING_DOWN.format(why=embed_failure),),
        value_domains=domains,
        counts={
            "context_chunks": n_chunks,
            "certified_exemplars": len(exemplars),
            "profiled_tables": len(profiles),
        },
    )


def generator_tier(assembled: AssembledInputs) -> Literal["certified", "intent", "grounded"]:
    """Which generation path this question takes — ``select_generators``' tiering
    decision as a plain value, so it can be stamped on the trace and shown by the
    prompt preview WITHOUT constructing a provider. The tier picks the prompt: the
    intent tier's first pass sees the leaner metric-layer serialization, not
    `generator.serialize_model`."""
    if assembled.certified_sql is not None or assembled.certified is not None:
        return "certified"
    if any(e.metrics for e in assembled.model.entities):
        return "intent"
    return "grounded"


GeneratorsFor = Callable[["AssembledInputs"], tuple[QueryGenerator, "QueryGenerator | None"]]


def _eval_generators_for(bundle: LensBundle, model: registry.ResolvedModel) -> GeneratorsFor:
    def generators_for(assembled: AssembledInputs) -> tuple[QueryGenerator, QueryGenerator | None]:
        generator, escalate, _, _ = select_generators(assembled, model, config=bundle.config)
        return generator, escalate

    return generators_for


def eval_harness(
    bundle: LensBundle, org_id: uuid.UUID | str, model: registry.ResolvedModel
) -> tuple[Callable[[certify_store.CertifiedAnswer], AssembledInputs], GeneratorsFor]:
    """The parity harness for the certified suite: per-question
    assembly exactly as serving, with the serve bypasses off (testing the bypass
    would trivially pass) and the answer under test excluded from its own
    exemplars (with parity it would match itself at ~1.0 and grade copying)."""

    def assemble_for(answer: certify_store.CertifiedAnswer) -> AssembledInputs:
        question = answer.question
        if answer.slots and answer.sample_bindings:
            # Assemble on the sample-bound question — the concrete
            # question the suite actually asks (retrieval must match it).
            specs, spec_errors = certify_binding.parse_slots(answer.slots)
            if not spec_errors:
                canonical, errors = certify_binding.validate_binding(
                    specs, answer.sample_bindings[0]
                )
                if not errors:
                    question = certify_binding.render_question(answer.question, dict(canonical))
        return assemble(
            bundle,
            question,
            org_id,
            serve_certified=False,
            exclude_certified_id=answer.id,
        )

    return assemble_for, _eval_generators_for(bundle, model)


def question_harness(
    bundle: LensBundle, org_id: uuid.UUID | str, model: registry.ResolvedModel
) -> tuple[Callable[[str], AssembledInputs], GeneratorsFor]:
    """The parity harness keyed by question text — for the
    runner lanes over eval cases, which aren't certified answers: no
    self-exclusion, serve bypass off."""

    def assemble_q(question: str) -> AssembledInputs:
        return assemble(bundle, question, org_id, serve_certified=False)

    return assemble_q, _eval_generators_for(bundle, model)


def select_generators(
    assembled: AssembledInputs,
    model: registry.ResolvedModel,
    *,
    config: LensConfig,
) -> tuple[
    QueryGenerator,
    QueryGenerator | None,
    Literal["certified", "assisted", "none"],
    Literal["exact", "equivalent", "parameterized"] | None,
]:
    """Production's generator tiering over assembled inputs: certified bypasses serve
    fixed SQL; a metric layer compiles a structured intent on the fast sibling with
    grounded escalation; otherwise grounded fast-first with smart escalation.
    The fourth element is the certified match band.

    ``model`` is a resolved client AND its wire name, one value, and this
    function reads NO model name off ``config``. It used to take a bare client
    and name the calls from `config.model.model_ref()`: whenever the two came
    from different resolutions — a lens pinned to `deepseek/deepseek-v4-pro`
    applied on an Anthropic-only server, where the client fell back to the
    smart tier — it sent a real request to Anthropic naming a DeepSeek model.
    The config still supplies the lens's JUDGMENT (answer mode → temperature);
    which model answers is the install's, and it travels with the client."""
    # Both names come from the resolution that produced the client. The cheap
    # first tier is that provider's fast model — taking the sibling of the
    # lens's own ref would be the same misroute one tier down.
    model_name = model.name
    primary_model = registry.wire_name(registry.fast_sibling(model.ref))
    llm = model.llm
    # The lens's answer mode sets how loosely it generates (sampling temperature).
    gen_temp = config.model.generation_temperature()
    if assembled.certified_sql is not None:
        # Strong certified binding — the metric's approved era-union SQL, served
        # directly on a normalized-exact question match.
        return FixedSQLGenerator(assembled.certified_sql), None, "certified", "exact"
    if assembled.certified is not None:
        served_sql = assembled.certified.sql
        if assembled.certified.slots and assembled.certified_match == "parameterized":
            # An approved template bound with this question's validated
            # values — deterministic render, the bind gate already validated.
            specs, spec_errors = certify_binding.parse_slots(assembled.certified.slots)
            assert not spec_errors, spec_errors  # the bind gate refused invalid slots
            served_sql = certify_binding.render_sql(
                served_sql, specs, assembled.certified_bound or {}, assembled.model.dialect
            )
        # Certified match — serve the approved SQL directly, no LLM generation.
        return (
            FixedSQLGenerator(served_sql),
            None,
            assembled.certification,
            (assembled.certified_match),
        )
    if generator_tier(assembled) == "intent":
        # Metric layer exists → compile a structured intent; fall back to raw SQL on repair.
        return (
            IntentSQLGenerator(llm, model=primary_model, temperature=gen_temp),
            GroundedSQLGenerator(llm, model=model_name, temperature=gen_temp),
            assembled.certification,
            None,
        )
    return (
        GroundedSQLGenerator(llm, model=primary_model, temperature=gen_temp),
        GroundedSQLGenerator(llm, model=model_name, temperature=gen_temp),
        assembled.certification,
        None,
    )
