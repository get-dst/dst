"""The Spider ladder: dst's artifact climb on Spider 2.0.

Runs the lite **local** subset (135 SQLite-class tasks shipped as JSON tables —
free, no cloud credentials) through the same lanes as the proving ground:
baseline → structural → +profile dictionary → +external knowledge (the 13
tasks that carry docs). Published Spider 2.0 leaderboards define the external
rawdog anchor; each rung's lift is the measurement.

Grading is an **approximation of the official scorer** (documented divergence;
their evaluate.py applies per-instance condition_cols): we require every row
of an expected result CSV to appear in the candidate result, matching on the
expected columns' values with float tolerance, order ignored; instances with
multiple acceptable results pass on any variant. Subset-first by design —
``--sample`` defaults small; promote only when confident.
"""

from __future__ import annotations

import csv
import io
import json
import random
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import duckdb

from .grading import Grade, _as_number, first_failed_stage, prose_signature, stage_statuses
from .lanes import LaneAnswer
from .questions import Question
from .runner import Lane, QuestionResult

_CACHE = Path("/tmp/spider2-duckdb")
_DB_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class SpiderTask:
    instance_id: str
    db: str
    question: Question
    knowledge: str | None  # external-knowledge doc text, when the task has one
    expected: list[list[list[str]]]  # acceptable results: each = header row + data rows


def materialize_db(repo: Path, db_name: str) -> Path:
    """JSON tables → a cached DuckDB file per database (thread-safe: parallel
    workers materializing the same db serialize on a per-db lock)."""
    _CACHE.mkdir(exist_ok=True)
    out = _CACHE / f"{db_name}.duckdb"
    with _LOCKS_GUARD:
        lock = _DB_LOCKS.setdefault(db_name, threading.Lock())
    with lock:
        if out.exists():
            return out
        return _build_db(repo, db_name, out)


def _build_db(repo: Path, db_name: str, out: Path) -> Path:
    # The real data ships separately (README: Drive zip → spider2-localdb/);
    # the in-repo JSON files are schema DESCRIPTORS (descriptions + sample
    # rows — a future description tier), not rows.
    src = repo / "spider2-lite" / "resource" / "databases" / "spider2-localdb" / f"{db_name}.sqlite"
    if not src.exists():
        raise FileNotFoundError(
            f"{src} missing — download the local databases per spider2-lite/README.md"
        )
    tmp = out.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    try:
        # Tables land in a constant `spider` schema: the harness excludes
        # DuckDB's default `main`, and a schema named after the db collides
        # with the catalog name (the file stem — e.g. db "IPL" → ambiguous).
        con.execute("INSTALL sqlite; LOAD sqlite")
        con.execute('CREATE SCHEMA "spider"')
        src_lit = str(src).replace("'", "''")
        con.execute(f"ATTACH '{src_lit}' AS src (TYPE sqlite, READ_ONLY)")
        tables = [
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_catalog = 'src' AND table_type = 'BASE TABLE'"
            ).fetchall()
        ]
        for table in tables:
            try:
                con.execute(f'CREATE TABLE "spider"."{table}" AS SELECT * FROM src."{table}"')
            except duckdb.Error:
                # SQLite loose typing (e.g. '' in an INTEGER column): degrade
                # just this table to all-VARCHAR rather than failing the db.
                con.execute("SET GLOBAL sqlite_all_varchar=true")
                con.execute(f'DROP TABLE IF EXISTS "spider"."{table}"')
                con.execute(f'CREATE TABLE "spider"."{table}" AS SELECT * FROM src."{table}"')
                con.execute("SET GLOBAL sqlite_all_varchar=false")
        con.execute("DETACH src")
    finally:
        con.close()
    tmp.rename(out)  # atomic: readers never see a half-built db
    return out


def _expected_results(repo: Path, instance_id: str) -> list[list[list[str]]]:
    gold = repo / "spider2-lite" / "evaluation_suite" / "gold" / "exec_result"
    variants = sorted(gold.glob(f"{instance_id}.csv")) + sorted(gold.glob(f"{instance_id}_*.csv"))
    out = []
    for v in variants:
        rows = list(csv.reader(io.StringIO(v.read_text(encoding="utf-8"))))
        if rows:
            out.append(rows)
    return out


def load_spider_tasks(repo: Path, *, sample: int, seed: int = 17) -> list[SpiderTask]:
    lines = (repo / "spider2-lite" / "spider2-lite.jsonl").read_text(encoding="utf-8").splitlines()
    raw = [json.loads(line) for line in lines if line.strip()]
    local = [t for t in raw if t["instance_id"].startswith("local")]
    rng = random.Random(seed)
    # Knowledge-bearing tasks are scarce (13/135) — keep them all, sample the rest.
    with_doc = [t for t in local if t["external_knowledge"]]
    plain = [t for t in local if not t["external_knowledge"]]
    if sample < len(plain):
        keep = set(rng.sample(range(len(plain)), sample))
        plain = [t for i, t in enumerate(plain) if i in keep]
    combined = with_doc + plain
    rng.shuffle(combined)  # subsets (--limit) must mix doc and plain tasks
    tasks: list[SpiderTask] = []
    for t in combined:
        expected = _expected_results(repo, t["instance_id"])
        if not expected:
            continue  # no gold result shipped — not gradable locally
        knowledge = None
        if t["external_knowledge"]:
            doc = repo / "spider2-lite" / "resource" / "documents" / t["external_knowledge"]
            if doc.exists():
                knowledge = doc.read_text(encoding="utf-8")[:8000]
        tasks.append(
            SpiderTask(
                instance_id=t["instance_id"],
                db=t["db"],
                question=Question(
                    id=t["instance_id"],
                    category="spider-knowledge" if knowledge else "spider",
                    question=t["question"],
                    oracle_path=[t["instance_id"]],
                    kind="scalar",  # unused: spider grading is exec-match
                    tier="discriminating",
                ),
                knowledge=knowledge,
                expected=expected,
            )
        )
    return tasks


def _cell_match(expected: str, got: object) -> bool:
    e = expected.strip()
    g = "" if got is None else str(got).strip()
    if e == g:
        return True
    en, gn = _as_number(e), _as_number(g)
    return en is not None and gn is not None and abs(en - gn) <= max(1e-6, abs(en) * 1e-4)


def grade_exec(answer: LaneAnswer, expected_variants: list[list[list[str]]]) -> Grade:
    if answer.error and not answer.rows:
        return Grade(False, f"lane error: {answer.error[:80]}")
    if not answer.rows:
        return Grade(False, "empty result")
    for variant in expected_variants:
        header, *exp_rows = variant
        if not exp_rows:
            continue
        if _variant_matches(answer.rows, exp_rows):
            return Grade(True, f"matched expected result ({len(exp_rows)} rows)")
    return Grade(False, f"no expected variant matched ({len(answer.rows)} candidate rows)")


def _variant_matches(got_rows: list[list[object]], exp_rows: list[list[str]]) -> bool:
    """Every expected row must appear in the candidate (order ignored): for each
    expected row there is a candidate row containing all its cell values."""
    remaining = list(got_rows)
    for exp in exp_rows:
        hit = None
        for i, cand in enumerate(remaining):
            if all(any(_cell_match(e, c) for c in cand) for e in exp if e.strip() != ""):
                hit = i
                break
        if hit is None:
            return False
        remaining.pop(hit)
    return True


def run_spider(
    lane_factory: Callable[[Path, str | None, str], list[Lane]],
    tasks: list[SpiderTask],
    repo: Path,
    *,
    workers: int = 4,
) -> list[QuestionResult]:
    """Tasks group by database; lanes are rebuilt per (db, knowledge) so the
    dictionary/knowledge tiers are generated for the right database."""
    from concurrent.futures import ThreadPoolExecutor

    def _one(task: SpiderTask) -> list[QuestionResult]:
        db_path = materialize_db(repo, task.db)
        results = []
        for lane in lane_factory(db_path, task.knowledge, task.db):
            try:
                answer = lane.answer(task.question)
            except Exception as exc:  # noqa: BLE001 — a dead call is a result
                answer = LaneAnswer(columns=[], rows=[], sql=None, error=f"lane crashed: {exc}")
            g = grade_exec(answer, task.expected)
            outcome = "correct" if g.correct else ("declined" if not answer.rows else "wrong")
            stages = stage_statuses(
                rows_correct=g.correct,
                delivered=bool(answer.rows),
                grounding=answer.grounding,
                served_lens=answer.lens,
            )
            if outcome == "correct" and stages["grounding"] == "failed":
                # Rows matched an expected variant but the DELIVERED prose does
                # not state them (e.g. cap-hit rows narrated as fiction).
                # Production serves the sentence — this is wrong.
                outcome = "wrong"
                g = Grade(False, "rows matched expected; delivered prose failed grounding")
            wrong_at = first_failed_stage(stages, outcome)
            evidence = {
                "rows": g.reason,
                "grounding": answer.grounding_reason or "prose failed numeric_grounding",
                "unattributed": f"wrong, but no graded stage failed: {g.reason}",
            }
            results.append(
                QuestionResult(
                    question_id=task.instance_id,
                    category=task.question.category,
                    lane=lane.name,
                    correct=g.correct,
                    reason=g.reason,
                    sql=answer.sql,
                    error=answer.error,
                    tier="discriminating",
                    outcome=outcome,
                    ai_cost_usd=answer.ai_cost_usd,
                    latency_ms=answer.latency_ms,
                    calls=answer.calls,
                    transcript=answer.transcript,
                    confidence=answer.confidence,
                    stages=stages,
                    wrong_at=wrong_at,
                    stage_evidence=evidence.get(wrong_at or "", ""),
                    answer=answer.answer,
                    prose_signature=prose_signature(answer.answer),
                )
            )
            mark = "✓" if g.correct else f"✗ {g.reason[:50]}"
            print(f"[{lane.name}] {task.instance_id}: {mark}", file=sys.stderr, flush=True)
        return results

    out: list[QuestionResult] = []
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for chunk in pool.map(_one, tasks):
                out.extend(chunk)
    else:
        for task in tasks:
            out.extend(_one(task))
    # regroup lane-major so paired stats line up
    out.sort(key=lambda r: (r.lane, r.question_id))
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    from services.contracts.protocols import ContextChunk, LLMProvider
    from services.llm.retry import RetryingLLM

    from .lanes import BaselineLane, DstLane
    from .runner import render_markdown, write_report
    from .world import profile_context, schema_summary

    ap = argparse.ArgumentParser(prog="services.benchmark.spider")
    ap.add_argument("--repo", default="/tmp/spider2", help="Spider2 checkout")
    ap.add_argument(
        "--sample", type=int, default=5, help="plain tasks to sample (doc tasks always kept)"
    )
    ap.add_argument("--limit", type=int, help="hard cap on total tasks (subset-first discipline)")
    ap.add_argument("--ids", help="comma-separated instance ids: run exactly these tasks")
    ap.add_argument(
        "--artifacts",
        help="dir of per-db authored artifacts (<db>.yaml: instructions, "
        "definitions[{term,body}]) — adds a dst-authored lane where present",
    )
    ap.add_argument("--model", default="deepseek-v4-flash")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--out", default="spider-out")
    ap.add_argument(
        "--agentic",
        action="store_true",
        help="add caller-simulation lanes: agent-baseline (raw SQL loop) vs "
        "agent-dst (same loop, governed lens calls)",
    )
    ap.add_argument("--max-steps", type=int, default=6, help="agentic tool-call budget")
    ap.add_argument(
        "--max-seconds", type=float, default=600.0, help="agentic wall-clock budget per question"
    )
    ap.add_argument(
        "--verify",
        action="store_true",
        help="add a dst-verified lane (inline judge + adversary on the authored "
        "lens) — prices the wrong->decline trade before any gate is built",
    )
    args = ap.parse_args(argv)

    repo = Path(args.repo)
    tasks = load_spider_tasks(repo, sample=args.sample, seed=args.seed)
    if args.ids:
        want = {i.strip() for i in args.ids.split(",") if i.strip()}
        # ids may fall outside the default sample — reload the full set once.
        tasks = [
            t
            for t in load_spider_tasks(repo, sample=10**6, seed=args.seed)
            if t.instance_id in want
        ]
        missing = want - {t.instance_id for t in tasks}
        if missing:
            print(f"warning: ids not found/gradable: {sorted(missing)}", file=sys.stderr)
    if args.limit and args.limit < len(tasks):
        tasks = tasks[: args.limit]
    print(
        f"spider tasks: {len(tasks)} ({sum(1 for t in tasks if t.knowledge)} with knowledge docs)"
    )

    llm: LLMProvider
    if args.model.startswith("deepseek"):
        from services.llm.deepseek_provider import DeepSeekProvider

        llm = DeepSeekProvider(api_key=os.environ["DEEPSEEK_API_KEY"])
    else:
        from services.llm.anthropic_provider import AnthropicProvider

        llm = AnthropicProvider(api_key=os.environ["ANTHROPIC_API_KEY"])
    llm = RetryingLLM(llm, attempts=5, base_delay=15.0)  # ladder policy, not serving's

    import yaml as _yaml

    from services.contracts.semantic_model import Definition

    authored: dict[str, dict[str, object]] = {}
    if args.artifacts:
        for f in sorted(Path(args.artifacts).glob("*.yaml")):
            authored[f.stem] = _yaml.safe_load(f.read_text(encoding="utf-8"))
        print(f"authored artifacts: {sorted(authored)}")

    schema_cache: dict[Path, str] = {}
    profile_cache: dict[Path, ContextChunk] = {}

    def lane_factory(db_path: Path, knowledge: str | None, db_name: str = "") -> list[Lane]:
        if db_path not in schema_cache:
            schema_cache[db_path] = schema_summary(db_path)
            profile_cache[db_path] = ContextChunk(
                text=profile_context(db_path), source="profiled-data-dictionary"
            )
        prof = profile_cache[db_path]
        lanes: list[Lane] = [
            BaselineLane(llm, db_path, schema_cache[db_path], model=args.model),
            DstLane(llm, db_path, model=args.model, name="dst-structural"),
            DstLane(llm, db_path, model=args.model, context_chunks=[prof], name="dst-dictionary"),
        ]
        if knowledge:
            lanes.append(
                DstLane(
                    llm,
                    db_path,
                    model=args.model,
                    context_chunks=[
                        prof,
                        ContextChunk(text=knowledge, source="external-knowledge"),
                    ],
                    name="dst-knowledge",
                )
            )
        if db_name in authored:
            # dst applied properly: the same information as the knowledge
            # doc, authored as product artifacts (decisive definitions +
            # lens instructions) instead of dumped prose. No certified
            # answers, no gold — the A/B is form, not information.
            a = authored[db_name]
            instr = a.get("instructions")
            raw_defs = a.get("definitions") or []
            assert isinstance(raw_defs, list)
            lanes.append(
                DstLane(
                    llm,
                    db_path,
                    model=args.model,
                    context_chunks=[prof],
                    instructions=instr if isinstance(instr, str) else None,
                    definitions=[Definition(**d) for d in raw_defs],
                    name="dst-authored",
                )
            )
        if args.verify and db_name in authored:
            a = authored[db_name]
            instr = a.get("instructions")
            raw_defs = a.get("definitions") or []
            assert isinstance(raw_defs, list)
            from .lanes import DstFeatures

            lanes.append(
                DstLane(
                    llm,
                    db_path,
                    model=args.model,
                    features=DstFeatures(judge=True, adversary=True),
                    context_chunks=[prof],
                    instructions=instr if isinstance(instr, str) else None,
                    definitions=[Definition(**d) for d in raw_defs],
                    name="dst-verified",
                )
            )
        if args.agentic:
            from .agentic import AgenticBaselineLane, AgenticDstLane, AgenticMemoLane, Budget
            from .agentic import Memo as _Memo
            from .conventions import memo_file

            chunks = [prof] + (
                [ContextChunk(text=knowledge, source="external-knowledge")] if knowledge else []
            )
            # The governed agent calls the BEST lens available: authored
            # artifacts when present (definitions close rules; the agent's
            # execute-and-inspect loop closes mechanics), else prose fallback.
            if db_name in authored:
                a = authored[db_name]
                instr = a.get("instructions")
                raw_defs = a.get("definitions") or []
                assert isinstance(raw_defs, list)
                inner = DstLane(
                    llm,
                    db_path,
                    model=args.model,
                    context_chunks=[prof],
                    instructions=instr if isinstance(instr, str) else None,
                    definitions=[Definition(**d) for d in raw_defs],
                    name="lens",
                )
            else:
                inner = DstLane(llm, db_path, model=args.model, context_chunks=chunks, name="lens")
            budget = Budget(tool_calls=args.max_steps, seconds=args.max_seconds)
            lanes.append(
                AgenticBaselineLane(
                    llm, db_path, schema_cache[db_path], model=args.model, budget=budget
                )
            )
            lanes.append(
                AgenticMemoLane(
                    llm,
                    db_path,
                    schema_cache[db_path],
                    [_Memo(memo_file(inner))],
                    model=args.model,
                    budget=budget,
                )
            )
            lanes.append(AgenticDstLane(inner, llm, model=args.model, budget=budget))
        return lanes

    results = run_spider(lane_factory, tasks, repo, workers=args.workers)
    json_path, md_path = write_report(results, Path(args.out))
    print()
    print(render_markdown(results))
    print(f"wrote {json_path} and {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
