"""The `dst test` command."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from services.cli import style
from services.cli._common import (
    _adopt_project_env,
    _env_dirs,
    _project_org,
    _read_project,
)
from services.cli.project import (
    _add_table_selection_args,
    _lineage_graph,
    _selection_line,
    _tables_from,
)

if TYPE_CHECKING:
    from services.certify.store import CertifiedAnswer
    from services.evals.slot_lane import SlotLaneOutcome


def _database_label() -> str:
    """DATABASE_URL as the in-process verbs resolved it, password hidden — what
    `dst test` prints beside the run it recorded, so a run that landed in a
    different database from the one the server serves is found by reading the
    output instead of by guessing."""
    from sqlalchemy.engine import make_url

    from services.config import settings

    return make_url(settings.database_url).render_as_string(hide_password=True)


def _lens_profile_age(session: Any, bundle: Any) -> Any:
    """The age of the lens's scope's newest profile against the org's
    ``probe.max_age`` (services/lenses/profile_age.py)."""
    from services.lenses import profile_store
    from services.lenses.profile_age import profile_age
    from services.project import probe_settings

    connections = list(bundle.config.connections)
    profiles = (
        [s.profile for s in profile_store.list_profiles(session, connections[0])]
        if connections
        else []
    )
    tables = {e.source.table for e in bundle.semantic_model.entities}
    return profile_age(profiles, probe_settings.load(session).max_age_delta, tables=tables)


def _test_selection(args: argparse.Namespace) -> set[str] | None:
    """The lenses `dst test --select/--tables-from` reaches, resolved against
    the project files under --dir with the plan/apply grammar; None when no
    selector was given. A selection reaching no lens prints its notes and
    returns an empty set: nothing is tested, and that is said."""
    from services.project.plan import resolve_selection

    names = _tables_from(args)
    selectors = [
        *(s.strip() for s in getattr(args, "select", None) or [] if s.strip()),
        *(f"table/{n}+" for n in names),
    ]
    emit_json = bool(getattr(args, "as_json", False))
    if "*" in selectors:  # everything, as with no selector
        return None
    if not selectors:
        if getattr(args, "tables_from", None) is None:
            return None
        print(f"--tables-from {args.tables_from} names no table or model — nothing to test")
        return set()
    if "@changed" in selectors:
        print(
            "error: dst test resolves --select against the project files alone; @changed "
            "needs the server's view, so use it on plan or apply",
            file=sys.stderr,
        )
        raise SystemExit(2)
    root = Path(getattr(args, "dir", ".") or ".")
    files = _read_project(root)
    if not files:
        print(
            f"error: --select needs the project files, and {root.resolve()} has none",
            file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        selection = resolve_selection(
            files,
            selectors,
            "all",
            db_semantic={},
            db_trees={},
            lineage=_lineage_graph(args, files, selectors),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    row = selection.row()
    args._selection_row = row
    print(_selection_line(row), file=sys.stderr if emit_json else sys.stdout)
    if not selection.lenses:
        if emit_json:
            print(json.dumps({"selection": row, "results": [], "passed": 0, "total": 0}, indent=2))
        else:
            print("nothing to test: the selection reaches no lens")
        return set()
    return set(selection.lenses)


def _test_scope(args: argparse.Namespace) -> list[tuple[uuid.UUID, str]] | None:
    """Which orgs `dst test` sweeps — THIS project's, not every tenant in the
    database. None (after printing why) when the scope resolves to nothing.

    It used to iterate every org and filter on lens NAME alone, so one
    `dst test tox` swept four different orgs' identically-named `tox`
    lenses — four copies of the same corpus, 37 minutes, and nothing in the
    output naming whose `PASS` was whose. A green suite that silently tested
    someone else's lens is worse than a red one: it is the same lie
    `--dir`/`_env_dirs` exist to prevent, entering through the org loop instead
    of through .env.

    Scope resolution is ``_project_org``; what is left here is this verb's own
    answer to an unscopable run — every org, said out loud, which a read-only
    sweep can afford. A named org that does not exist is an error, never a
    silent full sweep."""
    from sqlalchemy import text

    from services.config import resolve_env_ref
    from services.db.session import admin_engine

    scoped = _project_org(args)
    if scoped is not None:
        return [scoped]
    if args.org:
        print(f"error: no org named '{args.org}'", file=sys.stderr)
        return None
    if resolve_env_ref("DST_ADMIN_TOKEN", dirs=_env_dirs(args)):
        print(
            f"note: the admin token in {_env_dirs(args)[0]}/.env matches no org in this "
            "database — sweeping every org (use --org to scope)",
            file=sys.stderr,
        )
    else:
        print(
            "note: no DST_ADMIN_TOKEN for this project — sweeping EVERY org in the "
            "database; lens names are not unique across orgs, so use --org to scope",
            file=sys.stderr,
        )
    with admin_engine.connect() as c:
        every = c.execute(text("SELECT id, name FROM org ORDER BY created_at"))
        return [(r[0], r[1]) for r in every]


def _slot_lane_for_lens(
    bundle: object,
    oid: object,
    resolved: object,
    answers: list[CertifiedAnswer],
    repeat: int = 1,
    explicit: bool = False,
) -> SlotLaneOutcome | None:
    """The slot lane for one lens: the typed resolver over every certified
    question that carries gold, graded before any SQL (services/evals/
    slot_lane.py). By default it runs on the decider typed serving runs on
    (the switch: a typed-decision provider under `auto`, any decider under
    `on`, nothing under `off`); `--slots` (``explicit``) runs it on any decider
    that resolves, the vote included. None when none does — the caller says so
    instead of printing a clean zero."""
    from services.config import settings
    from services.evals.slot_lane import run_slot_lane
    from services.llm import registry
    from services.runtime import assembly
    from services.runtime.typed_resolver import TypedResolver

    assemble_for, generators_for = assembly.eval_harness(bundle, oid, resolved)  # type: ignore[arg-type]
    if explicit and settings.typed_serving == "off":
        # The emission arm: --slots with typed serving off grades the one-shot
        # JSON intent the way it grades a typed resolution — same table.
        from services.evals.slot_lane import EmissionResolver

        def emission_for(answer: object) -> EmissionResolver:
            assembled = assemble_for(answer)  # type: ignore[arg-type]
            generator, _escalate = generators_for(assembled)
            return EmissionResolver(generator, assembled.prose, domains=assembled.value_domains)

        return run_slot_lane(
            answers,
            bundle.semantic_model,  # type: ignore[attr-defined]
            emission_for,
            concurrency=settings.gate_concurrency,
            repeat=repeat,
        )
    decider = registry.resolve_decider() if explicit else assembly.typed_decider()
    if decider is None:
        return None
    today = assembly.business_today_date(bundle.semantic_model.timezone)  # type: ignore[attr-defined]

    def resolver_for(answer: object) -> TypedResolver:
        assembled = assemble_for(answer)  # type: ignore[arg-type]
        return TypedResolver(
            decider,
            domains=assembled.value_domains,
            entity_domains=assembled.entity_domains,
            period_fields=assembled.period_fields,
            today=today,
        )

    # The same ceiling the rows lane runs under: the lane is decisions only,
    # so its wall time is the provider's round trips divided by the workers.
    return run_slot_lane(
        answers,
        bundle.semantic_model,  # type: ignore[attr-defined]
        resolver_for,
        concurrency=settings.gate_concurrency,
        repeat=repeat,
    )


def _ledger_line(plain: str, *, ok: bool, elapsed: float | None) -> str:
    """One test-ledger row: the existing `VERDICT  label: question` bytes with
    the verdict painted and a dim latency column appended. The
    plain text is the parse contract — padding is computed on it, not on the
    escape-laden string."""
    verdict, _, rest = plain.partition("  ")
    painted = (style.good if ok else style.bad)(verdict) + "  " + rest
    if elapsed is None:
        return painted
    pad = " " * max(2, 78 - len(plain))
    return painted + pad + style.dim(f"{elapsed:.1f}s")


def _test(args: argparse.Namespace) -> int:
    """`dst test [lens]` — the certified corpus as the regression suite:
    for each ACTIVE certified answer, execute its stored SQL (the
    oracle) AND run its question through the generation pipeline with certified
    matching disabled, then compare executed results. Approved behavioral
    expectations (expect: clarify|refuse) run alongside, scored on response
    shape. Always the FULL active corpus (apply's gate scopes to
    binding-affected answers; this verb is the cron/CI full sweep). In-process
    against the configured database, like migrate/bootstrap — no server needed;
    ``--dir`` names the project whose .env supplies DATABASE_URL and the
    provider keys, so the one gate-verification command is invocable from
    outside its own directory. ORG-SCOPED (see ``_test_scope``), and every
    result line names its org. A green answer is a re-verification: its
    bindings re-stamp to current hashes (the plan's re-verify flag clears
    through evidence). Exit 1 on any divergence or failed expectation."""
    _adopt_project_env(args)
    if getattr(args, "url", None) or getattr(args, "token", None):
        # Accepted for trio-uniformity (the `bootstrap --url` precedent), but
        # never silently: a token naming prod while DATABASE_URL names localhost
        # is exactly the confusion this verb must not create.
        print(
            "note: --url/--token are ignored — `dst test` runs in-process against "
            "DATABASE_URL, not through a server; --dir picks the project whose .env "
            "supplies it",
            file=sys.stderr,
        )
    from services.certify import store as certify_store
    from services.certify.bindings import restamp_bindings
    from services.contracts.eval import EvalResult
    from services.db.session import org_session
    from services.evals import compare as run_compare
    from services.evals import slot_lane as slot_lane_mod
    from services.evals import store as eval_store
    from services.evals.certified_suite import format_result, run_certified_suite, value_json
    from services.evals.runner import run_behavioral
    from services.evals.service import _row_to_case, lens_serve
    from services.lenses import store as lens_store
    from services.lenses.connections import resolve_connector
    from services.llm import registry
    from services.observability import usage as ai_usage
    from services.runtime import assembly
    from services.runtime.answer import AnswerComposer

    if args.all:
        args.lens = None
    selected = _test_selection(args)
    if selected is not None and not selected:
        return 0  # the selection reaches no lens; said, and nothing ran
    if registry.resolve(registry.tier("smart")) is None:
        print(
            "error: no smart-tier model resolves — set smart_model (and its API key) on a provider",
            file=sys.stderr,
        )
        return 1
    orgs = _test_scope(args)
    if orgs is None:
        return 1
    total = failed = 0
    cert_n = beh_n = candidates_n = ungoverned_n = untyped_n = 0
    proven_n = 0
    slot_lanes: list[tuple[str, SlotLaneOutcome]] = []  # (lens label, its lane)
    compare_last: list[dict[str, object]] = []  # one entry per lens under --compare-last
    found_lens = False
    # The slot lane's graded decisions, reported per decider and slot.
    cal_records: list[object] = []
    # (org id, org name, lens, eval_run id) — stamped with calibration, then said
    # out loud: the run lands in DATABASE_URL's database, and nothing else in the
    # output names it.
    run_ids: list[tuple[Any, str, str, str]] = []
    emit_json = bool(getattr(args, "as_json", False))
    rows: list[dict[str, object]] = []

    def say(text: str) -> None:  # prose is stdout; --json swallows it for the object below
        if not emit_json:
            print(text)

    stale_lenses: list[str] = []  # labels whose profiles are past probe.max_age
    for oid, org_name in orgs:
        with org_session(oid) as session:
            for name, _dn, _desc, bundle in lens_store.list_published(session):
                if args.lens and name != args.lens:
                    continue
                if selected is not None and name not in selected:
                    continue
                found_lens = True
                # Every result line names the ORG — see _test_scope. A bare
                # "PASS  tox: …" from a sweep that hit four identically-named
                # lenses says nothing about whose corpus just went green.
                label = f"{org_name}/{name}"
                # A suite run against stale profiles verifies against facts
                # the warehouse may have moved past: the value guard and the
                # freshness checks abstain there, so the run could not have
                # failed the way it is meant to. Not green — said, counted,
                # exit 4 below.
                if (age := _lens_profile_age(session, bundle)).stale:
                    say(
                        style.warn("SKIP")
                        + f"  {label}: profiles last probed {age.age_days:g} days ago "
                        f"(max {age.max_age_days:g}) — run `dst probe`, commit, apply, "
                        "then test"
                    )
                    stale_lenses.append(label)
                    continue
                answers = [
                    a
                    for a in certify_store.list_for_lens(session, name)
                    if certify_store.is_active(a.status)
                ]
                # The sweep TESTS unembedded answers (their oracle is fine)
                # but must never imply they serve — matching is pgvector cosine,
                # and a NULL vector can never be returned. Said before the
                # results so it survives a long run's scrollback.
                if unembedded := certify_store.count_unembedded(session, name):
                    say(f"{label}: {certify_store.UNEMBEDDED_WARNING.format(n=unembedded)}")
                behavioral = [
                    case
                    for case in (
                        _row_to_case(row)
                        for row in eval_store.list_cases(session, name, status="approved")
                    )
                    if case.expect is not None
                ]
                # Cases that exist but will NOT run this sweep. Without this line an
                # unarmed suite and a fully-passing one print the same shape:
                # `passed 11/11` while candidates silently sit out.
                lens_candidates = len(
                    [c for c in eval_store.list_cases(session, name) if c.status == "candidate"]
                )
                candidates_n += lens_candidates
                if tag_filter := getattr(args, "tag", None) or []:
                    # --tag runs a case SLICE (persona:cfo, intent:discriminator).
                    # Certified answers carry no tags, so a tagged run is
                    # behavioral-only by construction.
                    behavioral = [c for c in behavioral if any(t in c.tags for t in tag_filter)]
                    answers = []
                if not answers and not behavioral:
                    slice_note = f" matching --tag {', '.join(tag_filter)}" if tag_filter else ""
                    parked = (
                        f" ({lens_candidates} candidate case(s) not run — promote with "
                        "status: approved)"
                        if lens_candidates
                        else ""
                    )
                    say(
                        f"{label}: no active certified answers, no behavioral "
                        f"cases{slice_note}{parked}"
                    )
                    continue
                # The lens's OWN model, with NO tier fallback. Falling back
                # dialled a different vendor for a lens pinned to one this
                # install does not have — the sweep would then report PASS for
                # a model the lens never serves on. Untestable is said out loud
                # and counted, never quietly tested against someone else.
                lens_ref = bundle.config.model.model_ref()
                resolved = registry.resolve(lens_ref)
                if resolved is None:
                    total += 1
                    failed += 1
                    reason = registry.unservable_detail(lens_ref)
                    say(
                        style.warn("SKIP")
                        + f"  {label}: NOT verified — this lens's model cannot be served "
                        f"here: {reason}"
                    )
                    rows.append(
                        {"org": org_name, "lens": name, "verdict": "skip", "reason": reason}
                    )
                    continue
                model_name = resolved.name
                connector = resolve_connector(bundle.config.connections[0], oid)
                composer = AnswerComposer(resolved.llm, model=model_name)
                # Liveness before the first slow call: in silence, working, hung and
                # rate-limited all look identical to the reader.
                progress_bits = []
                if answers:
                    progress_bits.append(f"{len(answers)} certified")
                if behavioral:
                    progress_bits.append(f"{len(behavioral)} behavioral")
                if lens_candidates:
                    progress_bits.append(f"{lens_candidates} candidate(s) not run")
                say(style.dim(f"{label}: running {' + '.join(progress_bits)} …"))
                # The slot lane runs whenever a decider resolves (typed serving):
                # every certified question graded in seconds, and a case whose
                # typed SQL is identical or equivalent to the certified text is
                # PROVEN there.
                # The rows lane below executes only what the slot lane could not
                # prove — the warehouse pays for the disagreements (--rows
                # executes everything, --slots executes nothing).
                slots_only = bool(getattr(args, "slots", False))
                rows_all = bool(getattr(args, "rows", False))
                proven: set[str] = set()
                slot_results: list[EvalResult] = []
                slot_rows: dict[str, dict[str, object]] = {}
                lane = _slot_lane_for_lens(
                    bundle,
                    oid,
                    resolved,
                    answers,
                    repeat=getattr(args, "repeat", 1),
                    explicit=slots_only,
                )
                if lane is not None or slots_only:
                    if lane is None:
                        total += 1
                        failed += 1
                        say(
                            style.warn("SKIP")
                            + f"  {label}: NOT verified — no decider resolves for the slot "
                            "lane (configure a typed-decision provider or a fast-tier model)"
                        )
                        rows.append(
                            {
                                "org": org_name,
                                "lens": name,
                                "verdict": "skip",
                                "reason": "no decider for the slot lane",
                            }
                        )
                        continue
                    for slot_case in lane.cases:
                        total += 1
                        cert_n += 1
                        ok = slot_case.verdict == "passed"
                        if not ok:
                            failed += 1
                        slot_row: dict[str, object] = {
                            "org": org_name,
                            "lens": name,
                            "lane": "slots",
                            # eval_result.case_id of this row, and the
                            # certified answer it grades: the join keys.
                            "case_id": f"slots:{slot_case.answer_id}",
                            "certified_answer_id": slot_case.answer_id,
                            "answer_id": slot_case.answer_id,
                            "question": slot_case.question,
                            "verdict": "pass" if ok else "fail",
                            "typed": slot_case.typed,
                            "reason": slot_case.evidence or None,
                            "decisions": [d.model_dump() for d in slot_case.decisions],
                            "gold": slot_case.gold,
                            "runs": slot_case.runs,
                            "agreement": slot_case.agreement,
                            "sql_match": slot_case.sql_match,
                            "elapsed_s": slot_case.elapsed_s,
                            "calls": slot_case.calls,
                            "input_tokens": slot_case.input_tokens,
                            "output_tokens": slot_case.output_tokens,
                            "cost_usd": slot_case.cost_usd,
                        }
                        rows.append(slot_row)
                        slot_rows[slot_case.answer_id] = slot_row
                        why = f" ({slot_case.evidence})" if slot_case.evidence and not ok else ""
                        sql_mark = {
                            "identical": " [sql identical]",
                            "equivalent": " [sql equivalent]",
                            "differs": " [sql differs from the certified text]",
                        }.get(slot_case.sql_match or "", "")
                        say(
                            _ledger_line(
                                f"{'PASS' if ok else 'FAIL'}  {label}: {slot_case.question}"
                                f"{why}{sql_mark}",
                                ok=ok,
                                elapsed=slot_case.elapsed_s,
                            )
                        )
                        cal_records += slot_case.graded
                    slot_lanes.append((label, lane))
                    if lane.cases or lane.skipped:
                        say(
                            f"{label}: slot lane — {len(lane.cases)} case(s) × {lane.repeat} "
                            f"run(s) in {lane.elapsed_s:.1f}s on {lane.workers} worker(s)"
                            + (f"; {len(lane.skipped)} skipped" if lane.skipped else "")
                            + (
                                f"; {lane.attributed_gold} graded against gold attributed "
                                "from the certified SQL (certified before the ledger)"
                                if lane.attributed_gold
                                else ""
                            )
                        )
                    # Not graded is said per answer, with the reason: graded plus
                    # skipped is the corpus, never a count that quietly shrank.
                    for sk in lane.skipped:
                        say(style.warn(f"SKIP  {label}: {sk.question} — {sk.reason}"))
                    proven = {
                        c.answer_id
                        for c in lane.cases
                        if c.verdict == "passed" and c.sql_match in ("identical", "equivalent")
                    }
                    proven_n += len(proven)
                # --slots executes nothing: the rows lane and the behavioral
                # pins sit out, but the slot verdicts are still RECORDED below.
                to_execute = (
                    []
                    if slots_only
                    else (answers if rows_all else [a for a in answers if a.id not in proven])
                )
                outcome = None
                if proven and not rows_all and not slots_only:
                    say(
                        f"{label}: {len(proven)} certified answer(s) proven by the slot lane "
                        "(typed SQL identical or equivalent to the certified text) — not "
                        "executed; --rows executes every case"
                    )
                if to_execute:
                    assemble_for, generators_for = assembly.eval_harness(bundle, oid, resolved)
                    outcome = run_certified_suite(
                        connector=connector,
                        lens=name,
                        answers=to_execute,
                        assemble_for=assemble_for,
                        generators_for=generators_for,
                        composer=composer,
                        model_name=model_name,
                    )
                    by_id = {r.answer_id: r for r in outcome.results}
                    gold_stored = 0
                    for a in to_execute:
                        r = by_id[a.id]
                        total += 1
                        cert_n += 1
                        if r.passed and r.resolution and not a.resolution:
                            # A green typed run IS the typed gold: the slot lane
                            # grades this answer in seconds from now on.
                            certify_store.set_resolution(session, a.id, r.resolution)
                            gold_stored += 1
                        rows.append(
                            {
                                "org": org_name,
                                "lens": name,
                                # The certified answer's id: stable across a
                                # rewording, and what eval_result.case_id carries
                                # (`certified:<id>`), so a report joins on it.
                                "case_id": f"certified:{r.answer_id}",
                                "certified_answer_id": r.answer_id,
                                "answer_id": r.answer_id,
                                "question": r.question,
                                "verdict": "pass" if r.passed else "fail",
                                "certified": format_result(r.oracle_result),
                                "generated": format_result(r.generated_result),
                                "reason": r.reason,
                                "elapsed_s": r.elapsed_s,
                                # Stage attribution: which stage broke (None when
                                # rows matched) and what the resolution lane saw.
                                "stages": r.stages,
                                "wrong_at": r.wrong_at,
                                "resolution": {
                                    "expected": r.resolution_expected,
                                    "got": r.resolution_got,
                                    "evidence": r.stage_evidence or None,
                                },
                                "typed": r.typed,
                            }
                        )
                        if r.typed is False:
                            untyped_n += 1
                        if not r.passed:
                            failed += 1
                            why = f" ({r.reason})" if r.reason else ""
                            at = f" [wrong at: {r.wrong_at}]" if r.wrong_at else ""
                            say(
                                _ledger_line(
                                    f"FAIL  {label}: {r.question} — certified SQL → "
                                    f"{format_result(r.oracle_result)}, generated → "
                                    f"{format_result(r.generated_result)}{why}{at}",
                                    ok=False,
                                    elapsed=r.elapsed_s,
                                )
                            )
                            continue
                        if r.ungoverned_pass:
                            # Right number, ungoverned path: the rows matched but
                            # generation did not resolve to the oracle's declared
                            # names. Reported, never a failure — rows are the gate.
                            ungoverned_n += 1
                        say(
                            _ledger_line(
                                f"PASS  {label}: {r.question}"
                                # A lenient pass says how it passed, here as in
                                # --json: a reader of either sees the same bridge.
                                + (f" ({r.reason})" if r.reason else "")
                                + (
                                    f" [resolution: {r.stage_evidence}]"
                                    if r.ungoverned_pass
                                    else ""
                                )
                                + (" [untyped]" if r.typed is False else ""),
                                ok=True,
                                elapsed=r.elapsed_s,
                            )
                        )
                        # Evidence-based re-verify: a passing
                        # test re-stamps the answer's bindings to current hashes.
                        # Templates re-stamp from the RENDERED sample-bound SQL —
                        # raw {placeholder} SQL never parses.
                        from services.evals.certified_suite import _render_template_case

                        rendered = (
                            _render_template_case(a, bundle.semantic_model.dialect)
                            if a.slots
                            else None
                        )
                        stamp_sql = rendered[0] if rendered else a.sql
                        fresh = restamp_bindings(stamp_sql, a.bindings, bundle.semantic_model)
                        if fresh and fresh != (a.bindings or {}):
                            certify_store.update(
                                session,
                                a.id,
                                sql=a.sql,
                                verified_value=a.verified_value,
                                bindings=fresh,
                            )
                    if gold_stored:
                        say(
                            f"{label}: stored typed gold for {gold_stored} certified answer(s) "
                            "from this green run — the slot lane grades them from now on"
                        )
                if lane is not None:
                    # The data lane is the oracle: a compiled SQL written another
                    # way than the certified text that returned the certified
                    # rows is "differs (data equal)", never a failure of either.
                    executed = {r.answer_id: r.passed for r in outcome.results} if outcome else {}
                    equal_n = 0
                    for c in lane.cases:
                        if (
                            c.sql_match == "differs"
                            and c.answer_id in executed
                            and slot_lane_mod.metric_slot_passed(c)
                        ):
                            c.data_equal = executed[c.answer_id]
                            slot_rows[c.answer_id]["sql_data_equal"] = c.data_equal
                            equal_n += int(c.data_equal)
                    if equal_n:
                        say(
                            f"{label}: {equal_n} slot case(s) compiled SQL that differs from "
                            "the certified text and returned the certified rows — differs "
                            "(data equal)"
                        )
                    slot_results = slot_lane_mod.eval_results(lane.cases)
                if behavioral and not slots_only:
                    # The slim rest of evals/cases.yaml: expect: clarify|refuse
                    # pins run alongside the corpus, as the scaffold promises.
                    # Through the query door's own core, so a pin grades the
                    # response a caller gets (typed serving, untyped_fallback).
                    b_outcome = run_behavioral(
                        lens=name,
                        cases=behavioral,
                        serve=lens_serve(
                            name, bundle, org_id=oid, connector=connector, resolved=resolved
                        ),
                    )
                    by_case = {res.case_id: res for res in b_outcome.results}
                    for case in behavioral:
                        res = by_case.get(case.id)
                        if res is None:
                            continue
                        total += 1
                        beh_n += 1
                        rows.append(
                            {
                                "org": org_name,
                                "lens": name,
                                "case_id": case.id,
                                "certified_answer_id": None,
                                "question": case.question,
                                "expect": case.expect,
                                "verdict": "pass" if res.passed else "fail",
                                "reason": res.reason,
                                "mode": res.mode,
                                **({"tags": case.tags} if case.tags else {}),
                            }
                        )
                        if res.passed:
                            say(
                                _ledger_line(
                                    f"PASS  {label}: {case.question} [expect: {case.expect}]",
                                    ok=True,
                                    elapsed=None,
                                )
                            )
                        else:
                            failed += 1
                            why = f" ({res.reason})" if res.reason else ""
                            say(
                                _ledger_line(
                                    f"FAIL  {label}: {case.question} [expect: {case.expect}]{why}",
                                    ok=False,
                                    elapsed=None,
                                )
                            )
                # Every result is recorded: the run lands in eval_run /
                # eval_result (mode "test"), the same tables the publish gate
                # writes, so accuracy is a queryable trend whoever runs the
                # sweep — CI, cron, or a laptop. Output bytes stay unchanged.
                recorded: list[EvalResult] = list(slot_results)
                if outcome is not None:
                    for cr in outcome.results:
                        recorded.append(
                            EvalResult(
                                run_id="",
                                case_id=f"certified:{cr.answer_id}",
                                question=cr.question,
                                passed=cr.passed,
                                grade="pass"
                                if cr.passed
                                else ("errored" if "error" in cr.oracle_result else "fail"),
                                # The stage tag rides the eval history (SA.1):
                                # a decomposition of the TREND is where it earns
                                # its keep. Resolution rides even on a pass.
                                checks={
                                    **(
                                        {"wrong_at": cr.wrong_at, "stages": cr.stages}
                                        if cr.stages
                                        else {}
                                    ),
                                    # Both executed values ride the row: a copy of
                                    # eval_result elsewhere holds the numbers, not
                                    # only the verdict and the SQL.
                                    "certified": format_result(cr.oracle_result),
                                    "generated": format_result(cr.generated_result),
                                },
                                reason=cr.reason,
                                actual_sql=cr.generated_sql,
                                # JSON-native, cells canonical: the shape a
                                # verified_value has, so the two compare.
                                actual_value=value_json(cr.generated_result),
                                **ai_usage.spend_fields(cr.ai_calls),
                            )
                        )
                if behavioral and not slots_only:
                    recorded.extend(b_outcome.results)
                if recorded:
                    r_passed = sum(1 for r in recorded if r.passed)
                    r_errored = sum(1 for r in recorded if r.grade == "errored")
                    score = r_passed / len(recorded)
                    # The run is recorded under the published bundle's
                    # fingerprint: two runs with the same one measured the
                    # same lens, so their verdicts compare case by case.
                    fingerprint = lens_store.bundle_fingerprint(bundle)
                    # Captured BEFORE create_run — afterwards "last" is this run.
                    previous = (
                        eval_store.last_run(session, name, "test")
                        if getattr(args, "compare_last", False)
                        else None
                    )
                    previous_results = (
                        eval_store.list_results(session, previous.id) if previous else []
                    )
                    run_id = eval_store.create_run(
                        session,
                        name,
                        "test",
                        lens_version=fingerprint,
                        score=score,
                        passed=r_passed,
                        failed=len(recorded) - r_passed - r_errored,
                        errored=r_errored,
                    )
                    for rec in recorded:
                        rec.run_id = run_id
                    eval_store.record_results(session, run_id, recorded)
                    run_ids.append((oid, org_name, name, run_id))
                    if getattr(args, "compare_last", False):
                        cmp = run_compare.compare_runs(
                            previous, previous_results, fingerprint, recorded, score
                        )
                        compare_last.append({"org": org_name, "lens": name, **cmp})
                        for line in run_compare.describe(cmp, label):
                            say(line)
    scope = ", ".join(n for _, n in orgs)
    if args.lens and not found_lens:
        print(f"error: no published lens '{args.lens}' in org {scope}", file=sys.stderr)
        return 1
    from services.evals.calibration import by_slot as calibration_by_slot
    from services.evals.calibration import format_report
    from services.evals.calibration import report as calibration_report

    calibration = calibration_report(cal_records)  # type: ignore[arg-type]
    if calibration and run_ids:
        # The table rides the run so the accuracy tab shows it — the same rows
        # the CLI prints, never a number derived from production traffic.
        for run_oid, _org, _lens, run_id in run_ids:
            with org_session(run_oid) as session:
                eval_store.set_calibration(session, run_id, calibration)
    # Where the run went. `dst test` writes in-process to DATABASE_URL's database;
    # `dst runs` and the dashboard read through the server at DST_URL. When those
    # two name different databases the run exists and nobody can find it, so the
    # destination is said with the run id — the one line that resolves "I ran the
    # suite and the history is empty".
    database = _database_label()
    recorded_runs = [
        {"org": org_name, "lens": lens_name, "run_id": run_id}
        for _oid, org_name, lens_name, run_id in run_ids
    ]
    # The failure modes the suite's behavioural cases came back with, counted
    # from the rows (a clarification's or refusal's name, whatever the case
    # expected); certified rows that matched carry none.
    by_mode: dict[str, int] = {}
    for row in rows:
        if isinstance(m := row.get("mode"), str):
            by_mode[m] = by_mode.get(m, 0) + 1
    if emit_json:
        # Computed FROM the rows, never from a parallel counter: a tally that can
        # disagree with its own results is the class the comparator ticket named.
        wrong_by_stage: dict[str, int] = {}
        for row in rows:
            if row.get("verdict") == "fail" and row.get("wrong_at"):
                key = str(row["wrong_at"])
                wrong_by_stage[key] = wrong_by_stage.get(key, 0) + 1
        print(
            json.dumps(
                {
                    "selection": getattr(args, "_selection_row", None),
                    "results": rows,
                    "passed": total - failed,
                    "total": total,
                    "certified": cert_n,
                    "behavioral": beh_n,
                    "candidates_not_run": candidates_n,
                    "wrong_by_stage": wrong_by_stage,
                    "by_mode": by_mode,
                    # The deciders' calibration over this corpus (advisory —
                    # never a gate until the defaults are stamped from it).
                    "calibration": calibration,
                    # The same figures per decision slot: n, accuracy, the
                    # unanimous bin's accuracy and ECE (None below the n floor).
                    "calibration_by_slot": calibration_by_slot(calibration),
                    "untyped_answers": untyped_n,
                    "proven_by_slots": proven_n,
                    "slot_lane": slot_lane_mod.summary(
                        slot_lanes, repeat=getattr(args, "repeat", 1)
                    ),
                    # Per lens under --compare-last: the previous run, the lens
                    # fingerprint, and the verdict flips when the lens is the same.
                    "compare_last": compare_last if getattr(args, "compare_last", False) else None,
                    "ungoverned_passes": sum(
                        1
                        for row in rows
                        if row.get("verdict") == "pass"
                        and isinstance(stages := row.get("stages"), dict)
                        and stages.get("resolution") == "failed"
                    ),
                    "orgs": scope,
                    # The runs this sweep recorded and the database they went to
                    # (password hidden) — `dst runs` lists them only through a
                    # server that serves this same database.
                    "recorded": recorded_runs,
                    "database": database,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        if rows:
            print(style.accent("─" * 64))
        # The denominator says what it is made of: a bare `passed 11/11`
        # reads as a green suite when it may be a handful of certified plus
        # behavioral cases with candidates not run — an unarmed suite and a passing one
        # must not print the same shape).
        composition = f"{cert_n} certified + {beh_n} behavioral"
        print(style.bold(f"{total - failed}/{total} passed") + f" ({composition}) in org {scope}")
        if by_mode:
            # The non-answers by failure mode, most frequent first: the
            # scorecard a blind authoring lap works from.
            print(
                style.accent("modes:")
                + " "
                + " · ".join(f"{n} {m}" for m, n in sorted(by_mode.items(), key=lambda kv: -kv[1]))
            )
        for _oid, org_name, lens_name, run_id in run_ids:
            print(f"recorded: run {run_id[:8]} ({org_name}/{lens_name}) in {database}")
        if run_ids:
            print(
                style.dim(
                    "`dst runs <lens>` and the dashboard read run history through the server "
                    "at DST_URL — they list this run only if that server serves this database"
                )
            )
        elif total:
            print(style.dim(f"no run recorded in {database} — nothing was scored"))
        if ungoverned_n:
            print(
                style.warn(
                    f"{ungoverned_n} passed with an ungoverned path — the rows matched, but "
                    "generation did not resolve to the certified SQL's declared metric or "
                    "definition (see the [resolution: …] notes)"
                )
            )
        if untyped_n:
            print(
                style.warn(
                    f"{untyped_n} certified case(s) answered UNTYPED — the question did not "
                    "type and raw-SQL generation answered it (allowed in the suite so the "
                    "rows still grade); the slot lane says which slot clarified"
                )
            )
        # Advisory: the exit code never keys off calibration — the policy
        # defaults are tuned on this very report, and gating would be circular.
        print(style.dim(format_report(calibration)))
        if candidates_n:
            print(
                style.warn(
                    f"note: {candidates_n} candidate case(s) were NOT run — promote with "
                    "status: approved in evals/cases.yaml (or the approve endpoint) to "
                    "include them"
                )
            )
    if failed:
        return 1
    if stale_lenses:
        # Same outcome as an empty suite: nothing those lenses hold was
        # verified, and a green exit would launder a probe job that stopped.
        print(
            f"warning: nothing was verified for {', '.join(stale_lenses)} — their profiles "
            "are older than probe.max_age, so the value and freshness guards could not "
            "stand behind a pass. Run `dst probe`, commit profiles/, apply, then test.",
            file=sys.stderr,
        )
        return 4
    if total == 0:
        # `0/0 passed` with exit 0 is indistinguishable from `10/10 passed` to a
        # script, to CI, or to anyone skimming output — a suite that verifies
        # nothing would report as assurance, and a definition change could break
        # a metric across an apply with the gate reporting green.
        #
        # Exit 4 is the honest third outcome, in the same spirit as `query`'s
        # 0 answered / 3 declined / 1 broke: nothing passed and nothing failed
        # because there was nothing to verify. Scripts branch on it; a CI gate
        # treats it as not-green without having to parse prose.
        print(
            "warning: nothing was verified — this lens has no certified answers and no "
            "behavioral cases, so this run could not have failed. Add certified answers "
            "(`dst reviews rule <id> --verdict approve --certify`) or eval cases.",
            file=sys.stderr,
        )
        return 4
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "test",
        help="run the certified test suite — generation vs certified SQL, full "
        "active corpus; exit 1 on any divergence",
    )
    p.add_argument("lens", nargs="?", help="lens to test (default: every published lens)")
    p.add_argument(
        "--select",
        nargs="+",
        action="extend",
        metavar="SELECTOR",
        help="test only the lenses these selectors reach, resolved against the project "
        "files under --dir: the plan/apply grammar (lens/NAME, entity/NAME+, "
        "table/NAME+, globs, * for everything) except @changed",
    )
    _add_table_selection_args(p)
    p.add_argument(
        "--all",
        action="store_true",
        help="test every published lens (the default when no lens is named)",
    )
    p.add_argument(
        "--tag",
        action="append",
        help="run only behavioral cases carrying this tag (repeatable, any-match) — "
        "e.g. --tag intent:discriminator; certified answers carry no tags, so a "
        "tagged run is a case slice",
    )
    p.add_argument(
        "--dir",
        default=".",
        help="project root whose .env supplies DATABASE_URL and the provider keys "
        "(default: .) — point the sweep at a project from outside it",
    )
    p.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="slot lane only: resolve every certified question N times; a case passes "
        "only when every run passes and every run produced the same typed intent "
        "(determinism, measured) — the rows lane ignores it",
    )
    p.add_argument(
        "--slots",
        action="store_true",
        help="the slot lane only: grade each certified question's TYPED resolution "
        "against its typed gold (stored, or attributed from the certified SQL) and "
        "compare the compiled SQL to the certified text — no warehouse, no composer, "
        "seconds per corpus. Without the flag the slot lane still runs whenever a "
        "decider resolves, and the rows lane executes only the cases it could not prove",
    )
    p.add_argument(
        "--rows",
        action="store_true",
        help="execute EVERY certified case against the warehouse, including the ones "
        "the slot lane proved (typed SQL identical to the certified text) — the "
        "data-drift check; by default those are not executed",
    )
    p.add_argument(
        "--compare-last",
        dest="compare_last",
        action="store_true",
        help="after recording, print each lens's score against its previous test "
        "run and, when the published lens is unchanged between the two (same "
        "fingerprint), every case whose verdict flipped (informational — the exit "
        "code stays the suite's own; `dst runs <lens> --diff prev latest` is the "
        "comparator with exit semantics)",
    )
    p.add_argument(
        "--org",
        help="org to sweep, by name (default: the org this project's "
        "DST_ADMIN_TOKEN authenticates as) — lens names are NOT unique across "
        "orgs, so an unscoped sweep tests every org's lens of that name",
    )
    p.add_argument(
        "--env",
        help="scope the sweep to a named environment minted by `dst env new` — "
        "the env name IS its org name, so no token changes hands",
    )
    p.add_argument(
        "--url",
        help="ignored — `test` runs in-process against DATABASE_URL (no server needed); use --dir",
    )
    p.add_argument(
        "--token",
        help="ignored — `test` runs in-process against DATABASE_URL (no server needed); use --dir",
    )
    p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="the same rows as a parseable object — per case: org, lens, question, "
        "verdict, certified/generated result, reason, elapsed_s — plus the summary",
    )
    p.set_defaults(fn=_test)
