"""Run the proving-ground benchmark end to end.

    # full run, baseline vs dst, live LLM (needs ANTHROPIC_API_KEY):
    uv run python -m services.benchmark --data path/to/generator/csvs

    # strip features from a second dst lane to measure what they buy:
    uv run python -m services.benchmark --data path/to/generator/csvs \
        --strip context,repair

    # no-network self-test of the harness itself:
    uv run python -m services.benchmark --data path/to/generator/csvs --offline

Lanes always include ``baseline`` (no dst) and ``dst`` (full); each
``--strip`` adds one more lane named ``dst-without-<features>``.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from services.contracts.fakes import HashEmbedder, ScriptedLLM
from services.contracts.protocols import ContextChunk, Embedder, LLMProvider
from services.llm.retry import RetryingLLM

from .agentic import Memo
from .certified import CertifiedIndex
from .conventions import memo_file
from .expand import expand_from_oracle
from .lanes import BaselineLane, DstFeatures, DstLane
from .pressure import pressure_twins
from .questions import Question, load_callers, load_questions, unresolved
from .runner import Lane, render_markdown, run_benchmark, summarize, write_report
from .warehouse import DuckDBWarehouse, SnowflakeWarehouse, Warehouse, coerce
from .world import load_oracle, load_world, scope_from_certified_defs

STARTER_QUESTIONS = Path(__file__).parent / "questions" / "starter.yaml"
_MAX_CHUNK_CHARS = 6000

# The instructions rung: a lens author's standing orders. Generic answering
# practice — deliberately NOT derived from any benchmark miss.
LENS_INSTRUCTIONS = (
    "When a question asks WHICH or WHO (top customer, best rep), return the "
    "display name (join to the dimension table), never an internal ID. "
    "When a question asks for a single total, return exactly one row with one "
    "number. Monetary answers are euros as plain numbers. If a specific "
    "entity (a customer, a rep) may or may not exist, query for it — an empty "
    "or zero result is a valid answer; do not refuse."
)


def _load_context(specs: list[str]) -> list[ContextChunk]:
    """Markdown files/dirs → one ContextChunk per file (the 'dst artifact' layer)."""
    chunks: list[ContextChunk] = []
    for spec in specs:
        path = Path(spec)
        files = sorted(path.glob("**/*.md")) if path.is_dir() else [path]
        for f in files:
            if not f.exists():
                raise FileNotFoundError(f"--context path not found: {f}")
            chunks.append(
                ContextChunk(
                    text=f.read_text(encoding="utf-8")[:_MAX_CHUNK_CHARS],
                    source=f.name,
                )
            )
    return chunks


def _agentic_lanes(
    args: argparse.Namespace,
    llm: LLMProvider,
    warehouse: Path | Warehouse,
    context_chunks: list[ContextChunk],
    caller_registry: dict[str, str] | None,
    certified_index: CertifiedIndex | None,
    principals: dict[str, str] | None,
) -> list[Lane]:
    """The caller-simulation arms, one set per budget level.

    agent-raw (uninformed control) · agent-memo · agent-memo-plus · agent-memo-xM
    · agent-dst. Every arm shares the loop, the shape contract and the
    budget; MEMO is rendered from the same lens the governed arm asks, so the
    arms differ in delivery mechanism, not in information. Budgets are per level
    because a 3.5 ms SQL tool and an 80 s governed ask only compare at equal
    *cost*. The uninformed control is there to show the memo effect, never as a
    headline: accuracy against it measures the presence of a file.
    """
    from .agentic import AgenticBaselineLane, AgenticDstLane, AgenticMemoLane, Budget
    from .conventions import MEMO_BYTES

    inner = _lens(args, llm, warehouse, context_chunks, caller_registry, certified_index)
    cap = args.memo_bytes or MEMO_BYTES
    memo_text = memo_file(inner, certified_sql=not args.no_conventions_sql, cap=cap)
    budgets = [Budget.parse(b) for b in args.budget] or [Budget()]
    schema = coerce(warehouse).schema_summary()
    print(f"agentic arms: {len(budgets)} budget level(s); MEMO {len(memo_text)}/{cap} bytes")
    # Name the asymmetry every run instead of leaving it in a docstring: MEMO is
    # capped at the published evaluation's size, the lens's retrieved prose is not, so the
    # memo arms can be information-capped relative to the governed arm. Raise
    # --memo-bytes to that figure for an information-parity run.
    lens_bytes = sum(len(c.text) for c in inner.prose_context)
    if lens_bytes > cap:
        print(
            f"  NOTE: the lens carries {lens_bytes} bytes of prose context vs MEMO's "
            f"{cap} — the memo arms are capped by {lens_bytes - cap} bytes. "
            f"Use --memo-bytes {lens_bytes} for information parity."
        )
    out: list[Lane] = []
    for budget in budgets:
        tag = f"@{budget.label}" if len(budgets) > 1 else ""
        out.append(
            AgenticBaselineLane(
                llm,
                warehouse,
                schema,
                model=args.model,
                budget=budget,
                principals=principals,
                name=f"agent-raw{tag}",
            )
        )
        for variant, memos in _memo_variants(memo_text, args.memo_sessions).items():
            out.append(
                AgenticMemoLane(
                    llm,
                    warehouse,
                    schema,
                    memos,
                    model=args.model,
                    budget=budget,
                    principals=principals,
                    name=f"{variant}{tag}",
                )
            )
        out.append(
            AgenticDstLane(
                inner,
                llm,
                model=args.model,
                budget=budget,
                # The same cap the memo arms get: --memo-bytes must move both
                # sides, or raising it would quietly starve the governed arm.
                describe_text=memo_file(inner, certified_sql=False, cap=cap),
                principals=principals,
                name=f"agent-dst{tag}",
            )
        )
    return out


def _memo_variants(text: str, sessions: int) -> dict[str, list[Memo]]:
    """The three memo arms. Any arm measured without MEMO is uninterpretable."""
    return {
        "agent-memo": [Memo(text)],  # static CONVENTIONS.md
        "agent-memo-plus": [Memo(text, mutable=True)],  # the agent may append
        # M independent sessions, each maintaining its own copy — what actually
        # happens when several teams point their own agents at one warehouse.
        f"agent-memo-x{sessions}": [Memo(text, mutable=True) for _ in range(sessions)],
    }


def _lens(
    args: argparse.Namespace,
    llm: LLMProvider,
    warehouse: Path | Warehouse,
    context_chunks: list[ContextChunk],
    caller_registry: dict[str, str] | None,
    certified_index: CertifiedIndex | None,
) -> DstLane:
    return DstLane(
        llm,
        warehouse,
        model=args.model,
        context_chunks=context_chunks,
        caller_registry=caller_registry,
        certified_index=certified_index,
        instructions=LENS_INSTRUCTIONS,
        name="lens",
    )


def _run_drift(
    args: argparse.Namespace,
    llm: LLMProvider,
    warehouse: Path | Warehouse,
    context_chunks: list[ContextChunk],
    caller_registry: dict[str, str] | None,
    certified_index: CertifiedIndex | None,
    questions: list[Question],
    principals: dict[str, str] | None,
) -> int:
    """Experiment B: one question, S sessions × C callers × K consumer stacks.

    Metric drift is widely claimed as a problem and rarely measured directly.
    The consumer STACK varies (one model per stack), not just the seed — that is
    the production condition, and reseeding one model would measure sampling
    noise instead.
    """
    from .agentic import AgenticBaselineLane, AgenticDstLane, AgenticMemoLane, Budget
    from .drift import render_drift, run_drift

    target = next((q for q in questions if q.id == args.drift), None)
    if target is None:
        print(f"--drift: no question with id {args.drift!r}", file=sys.stderr)
        return 2
    models = [m.strip() for m in (args.drift_models or args.model).split(",") if m.strip()]
    # The question's OWN caller first: a drift run over two callers who happen to
    # share a convention measures nothing — the caller axis goes inert and only
    # the session axis moves.
    ordered = [args.drift_caller or target.caller] + sorted(caller_registry or {})
    callers: list[str | None] = list(dict.fromkeys(c for c in ordered if c))[:2] or [None]
    print(f"drift callers: {callers}")
    schema = coerce(warehouse).schema_summary()
    budget = Budget.parse(args.budget[0]) if args.budget else Budget()

    def stacks(build: Callable[[str], Lane]) -> dict[str, Callable[[], Lane]]:
        return {model: (lambda m=model: build(m)) for model in models}  # type: ignore[misc]

    def raw(model: str) -> Lane:
        return AgenticBaselineLane(
            llm, warehouse, schema, model=model, budget=budget, principals=principals
        )

    def memo(model: str) -> Lane:
        lens = _lens(args, llm, warehouse, context_chunks, caller_registry, certified_index)
        # A FRESH memo per session — that independence is the thing being counted.
        return AgenticMemoLane(
            llm,
            warehouse,
            schema,
            [Memo(memo_file(lens), mutable=True)],
            model=model,
            budget=budget,
            principals=principals,
        )

    def governed(model: str) -> Lane:
        lens = _lens(args, llm, warehouse, context_chunks, caller_registry, certified_index)
        return AgenticDstLane(lens, llm, model=model, budget=budget, principals=principals)

    runs = run_drift(
        {"agent-raw": stacks(raw), "agent-memo": stacks(memo), "agent-dst": stacks(governed)},
        target,
        callers=callers,
        sessions=args.drift_sessions,
    )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = render_drift(target, runs)
    (out_dir / "drift.md").write_text(report, encoding="utf-8")
    (out_dir / "drift.json").write_text(
        json.dumps([asdict(r) for r in runs], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print()
    print(report)
    print(f"wrote {out_dir / 'drift.md'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="services.benchmark")
    ap.add_argument("--data", required=True, help="generator CSV root (contains crm/, finance/, …)")
    ap.add_argument("--oracle", help="oracle JSON (default: <data>/_oracle.json)")
    ap.add_argument("--questions", default=str(STARTER_QUESTIONS))
    ap.add_argument("--db", help="DuckDB artifact path (default: temp file)")
    ap.add_argument("--out", default="benchmark-out", help="report directory")
    ap.add_argument("--model", default="deepseek-v4-flash")
    ap.add_argument(
        "--warehouse",
        choices=["duckdb", "snowflake"],
        default="duckdb",
        help="execute and introspect against this warehouse (snowflake: SNOWFLAKE_* env "
        "credentials; the world's schemas must be pre-loaded — the harness never writes "
        "to a customer-shaped warehouse)",
    )
    ap.add_argument(
        "--strip",
        action="append",
        default=[],
        help="comma-separated features for an extra stripped dst lane (repeatable)",
    )
    ap.add_argument("--no-baseline", action="store_true", help="skip the no-dst lane")
    ap.add_argument("--no-dst", action="store_true", help="skip the full-dst lane")
    ap.add_argument(
        "--stale-days",
        type=int,
        default=None,
        help="freshness experiment: age the world's measured data_as_of this many "
        "days; the dst lane declares a fixed 2-day contract (strip `freshness` "
        "for the undeclared control). Adds the silent-stale section to the report",
    )
    ap.add_argument(
        "--context",
        action="append",
        default=[],
        help="markdown file/dir of business context for the dst lane(s) (repeatable)",
    )
    ap.add_argument(
        "--context-profile",
        action="store_true",
        help="add a profiled data dictionary (categorical values, row counts) as context",
    )
    ap.add_argument(
        "--offline",
        action="store_true",
        help="use a scripted LLM (harness self-test; answers will be wrong on purpose)",
    )
    ap.add_argument(
        "--expand",
        action="store_true",
        help="add mechanically-expanded per-entity questions from the oracle maps (~100 total)",
    )
    ap.add_argument(
        "--repeat", type=int, default=1, help="repetitions per question (provider variance)"
    )
    ap.add_argument("--sample", type=int, help="N calibration questions (discriminating runs full)")
    ap.add_argument("--sample-disc", type=int, help="also sample the discriminating tier to N")
    ap.add_argument("--sample-seed", type=int, default=17)
    ap.add_argument(
        "--workers",
        type=int,
        default=4,
        help="parallel question runs per lane (1 = sequential; "
        "context-heavy lanes at 8 can exceed provider token-per-minute limits)",
    )
    ap.add_argument(
        "--quick",
        action="store_true",
        help="fast iteration preset: 12 discriminating + 5 calibration, parallel "
        "(promote to the full set once the change looks right)",
    )
    ap.add_argument(
        "--ladder",
        action="store_true",
        help="run the artifact climb: baseline → dst-structural → +dictionary → +definitions "
        "(the last rung needs --context files, e.g. the world's definitions.md)",
    )
    ap.add_argument(
        "--agentic",
        action="store_true",
        help="add the three caller-simulation arms: agent-baseline (raw SQL), "
        "agent-conventions (raw SQL + the lens as a prose file — the MEMO control), "
        "agent-dst (describe + governed ask)",
    )
    ap.add_argument(
        "--budget",
        action="append",
        default=[],
        help="agentic budget CALLS[:SECONDS[:TOKENS]], repeatable — one set of arms "
        "per level, because 'equal budget' is a curve, not a number (default 8:600)",
    )
    ap.add_argument(
        "--no-conventions-sql",
        action="store_true",
        help="omit certified SQL bodies from MEMO",
    )
    ap.add_argument(
        "--memo-bytes",
        type=int,
        help="MEMO size (default 4096 — the published evaluation's size, arXiv 2604.25149). "
        "Raise it to the lens's prose-context size for an information-parity run.",
    )
    ap.add_argument(
        "--memo-sessions",
        type=int,
        default=3,
        help="M for the MEMO×M arm: independent sessions each maintaining its own copy",
    )
    ap.add_argument(
        "--pressure",
        action="append",
        default=[],
        choices=["override", "false-premise", "escalate"],
        help="experiment A: add a pressure twin of every question (repeatable) — "
        "same oracle, caller argues against the convention in-band",
    )
    ap.add_argument(
        "--drift",
        help="experiment B: question id to measure metric drift on "
        "(distinct answers across sessions × callers × consumer stacks)",
    )
    ap.add_argument("--drift-sessions", type=int, default=5)
    ap.add_argument(
        "--drift-caller",
        help="caller to lead the drift run's caller axis (default: the question's)",
    )
    ap.add_argument(
        "--drift-models",
        help="comma-separated models = the heterogeneous consumer stacks for --drift "
        "(default: --model alone). Varying the stack is the point; reseeding one "
        "model measures sampling noise.",
    )
    args = ap.parse_args(argv)

    data_root = Path(args.data)
    oracle_path = Path(args.oracle) if args.oracle else data_root / "_oracle.json"
    db_path = Path(args.db) if args.db else Path(tempfile.mkdtemp()) / "proving_ground.duckdb"

    tables = load_world(data_root, db_path)
    if args.warehouse == "snowflake":
        # Data must be pre-loaded into these schemas (a deliberate separate
        # step — the harness never writes to a customer-shaped warehouse).
        datasets = tuple(sorted(p.name for p in data_root.iterdir() if p.is_dir()))
        wh: Warehouse = SnowflakeWarehouse.from_env(dict(os.environ), schemas=datasets)
        print(f"warehouse: snowflake, schemas {', '.join(d.upper() for d in datasets)}")
    else:
        wh = DuckDBWarehouse(db_path)
    oracle = load_oracle(oracle_path)
    questions = load_questions(Path(args.questions), oracle)
    if args.expand:
        questions += expand_from_oracle(oracle)
    if args.quick:
        args.sample_disc = args.sample_disc or 12
        args.sample = 5 if args.sample is None else args.sample
    if args.sample is not None or args.sample_disc:
        # The discriminating tier is the headline — it runs in full unless
        # --sample-disc/--quick caps it; --sample N draws the calibration floor.
        rng = random.Random(args.sample_seed)
        disc = [q for q in questions if q.tier == "discriminating"]
        calib = [q for q in questions if q.tier == "calibration"]
        if args.sample_disc and args.sample_disc < len(disc):
            keep = set(rng.sample(range(len(disc)), args.sample_disc))
            disc = [q for i, q in enumerate(disc) if i in keep]
        if args.sample is not None and args.sample < len(calib):
            keep = set(rng.sample(range(len(calib)), args.sample))
            calib = [q for i, q in enumerate(calib) if i in keep]
        questions = disc + calib
        print(f"sampled: {len(disc)} discriminating + {len(calib)} calibration")
    if args.pressure:
        # Experiment A: the same questions, with the caller arguing. Same oracle
        # — an arm that caves grades wrong, so compliance needs no new grader.
        base = list(questions)
        for kind in args.pressure:
            questions += pressure_twins(base, kind=kind)
        print(f"pressure twins: {len(questions) - len(base)} ({', '.join(args.pressure)})")
    orphans = unresolved(questions, oracle)
    if orphans:
        # A stale fixture dies here, in milliseconds — never mid-run as a
        # KeyError after half the budget is spent.
        print("questions with no oracle fact (fixture drift?):", file=sys.stderr)
        for line in orphans:
            print(f"  {line}", file=sys.stderr)
        return 2
    print(f"world: {len(tables)} tables → {db_path}")
    print(
        f"oracle: {len(oracle)} facts · questions: {len(questions)}"
        + (f" × {args.repeat} reps" if args.repeat > 1 else "")
    )

    llm: LLMProvider
    if args.offline:
        llm = ScriptedLLM(["SELECT 1"])
    elif args.model.startswith("deepseek"):
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            print("DEEPSEEK_API_KEY not set", file=sys.stderr)
            return 2
        from services.llm.deepseek_provider import DeepSeekProvider

        llm = DeepSeekProvider(api_key=api_key)
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print(
                "ANTHROPIC_API_KEY not set (use --offline for a harness self-test)", file=sys.stderr
            )
            return 2
        from services.llm.anthropic_provider import AnthropicProvider

        llm = AnthropicProvider(api_key=api_key)
    if not args.offline:
        # a 429 must never kill an 800-run ladder — ladder policy, not serving's
        llm = RetryingLLM(llm, attempts=5, base_delay=15.0)

    caller_registry: dict[str, str] | None = None
    principals: dict[str, str] | None = None
    callers_path = data_root / "callers.yaml"
    if callers_path.exists():
        caller_registry, principals = load_callers(callers_path)
        print(f"caller registry: {len(caller_registry)} callers (runtime-context rung available)")

    certified_index = None
    seed_path = data_root / "certified_seed.yaml"
    if seed_path.exists():
        embedder: Embedder
        if not args.offline and os.environ.get("VOYAGE_API_KEY"):
            from services.context.embedder import VoyageEmbedder

            embedder = VoyageEmbedder(api_key=os.environ["VOYAGE_API_KEY"])
            note = "voyage"
        else:
            embedder = HashEmbedder()
            note = "hash — exact-text matches only"
        certified_index = CertifiedIndex.from_yaml(seed_path, embedder)
        certified_index.warm([q.question for q in questions])  # one batched embed call
        print(f"certified store: seeded from {seed_path.name} ({note})")

    file_chunks = _load_context(args.context)
    profile_chunk = (
        ContextChunk(text=wh.profile_context(), source="profiled-data-dictionary")
        if (args.context_profile or args.ladder)
        else None
    )
    context_chunks = file_chunks + ([profile_chunk] if profile_chunk else [])
    if context_chunks:
        print(f"context: {len(context_chunks)} artifact(s) for the dst lane(s)")

    lanes: list[Lane] = []
    if args.ladder:
        lanes.append(BaselineLane(llm, wh, wh.schema_summary(), model=args.model))
        lanes.append(DstLane(llm, wh, model=args.model, name="dst-structural"))
        assert profile_chunk is not None
        lanes.append(
            DstLane(
                llm,
                wh,
                model=args.model,
                context_chunks=[profile_chunk],
                name="dst-dictionary",
            )
        )
        if file_chunks:
            lanes.append(
                DstLane(
                    llm,
                    wh,
                    model=args.model,
                    context_chunks=[profile_chunk] + file_chunks,
                    name="dst-definitions",
                )
            )
        if file_chunks:
            lanes.append(
                DstLane(
                    llm,
                    wh,
                    model=args.model,
                    context_chunks=[profile_chunk] + file_chunks,
                    instructions=LENS_INSTRUCTIONS,
                    name="dst-instructed",
                )
            )
        if file_chunks and caller_registry:
            lanes.append(
                DstLane(
                    llm,
                    wh,
                    model=args.model,
                    context_chunks=[profile_chunk] + file_chunks,
                    caller_registry=caller_registry,
                    instructions=LENS_INSTRUCTIONS,
                    name="dst-runtime",
                )
            )
        if file_chunks and caller_registry and certified_index:
            lanes.append(
                DstLane(
                    llm,
                    wh,
                    model=args.model,
                    context_chunks=[profile_chunk] + file_chunks,
                    caller_registry=caller_registry,
                    certified_index=certified_index,
                    instructions=LENS_INSTRUCTIONS,
                    name="dst-certified",
                )
            )
        if file_chunks and caller_registry and certified_index:
            # The scoped-lens rung: only the tables the certified names. Smaller
            # context — the cost/latency/accuracy trade the lens model claims.
            scope = scope_from_certified_defs(
                Path(args.context[0]).read_text(encoding="utf-8"), wh.tables()
            )
            print(f"scoped lens: {len(scope)} of {len(tables)} tables")
            lanes.append(
                DstLane(
                    llm,
                    wh,
                    model=args.model,
                    context_chunks=[profile_chunk] + file_chunks,
                    caller_registry=caller_registry,
                    certified_index=certified_index,
                    instructions=LENS_INSTRUCTIONS,
                    table_scope=scope,
                    name="dst-scoped",
                )
            )
    else:
        if not args.no_baseline:
            lanes.append(BaselineLane(llm, wh, wh.schema_summary(), model=args.model))
        if not args.no_dst:
            lanes.append(
                DstLane(
                    llm,
                    wh,
                    model=args.model,
                    context_chunks=context_chunks,
                    caller_registry=caller_registry,
                    certified_index=certified_index,
                    instructions=LENS_INSTRUCTIONS,
                    stale_days=args.stale_days,
                )
            )
        for spec in args.strip:
            names = [s.strip() for s in spec.split(",") if s.strip()]
            lanes.append(
                DstLane(
                    llm,
                    wh,
                    model=args.model,
                    features=DstFeatures().stripped(names),
                    context_chunks=context_chunks,
                    stale_days=args.stale_days,
                    name="dst-without-" + "+".join(names),
                )
            )
    if args.agentic:
        lanes += _agentic_lanes(
            args, llm, wh, context_chunks, caller_registry, certified_index, principals
        )
    if args.drift:
        return _run_drift(
            args,
            llm,
            wh,
            context_chunks,
            caller_registry,
            certified_index,
            questions,
            principals,
        )
    if not lanes:
        print("nothing to run: all lanes disabled", file=sys.stderr)
        return 2

    workers = 1 if args.offline else max(1, args.workers)  # ScriptedLLM is stateful
    # pass^k is only honest when the CALLER varies as well as the seed.
    rotate = sorted(caller_registry) if (caller_registry and args.repeat > 1) else None
    results = run_benchmark(
        lanes, questions, oracle, repeat=args.repeat, workers=workers, callers=rotate
    )
    json_path, md_path = write_report(results, Path(args.out))
    print()
    print(render_markdown(results))
    print(f"wrote {json_path} and {md_path}")
    return 0 if args.offline or all(s.accuracy > 0 for s in summarize(results)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
