"""The `dst evals` command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from services.cli import style
from services.cli._common import (
    _client,
    _detail,
    _read_project,
)


def _comment_head(text: str) -> list[str]:
    """The file's leading comment block (shape-teaching headers) — kept verbatim
    across a rewrite; a migration must not eat the docs that live in the file."""
    head: list[str] = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            head.append(line)
        else:
            break
    while head and not head[-1].strip():
        head.pop()
    return head


def _migrate_context(root: Path) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """(leaf→source table, connection name→dialect, ambiguous leaves) for a project.

    Eval-plane SQL names LEAF tables by design; certified SQL executes against
    the LIVE warehouse and needs the fully-qualified names. A
    leaf claimed by two entities with different sources is unmappable — named,
    never guessed."""
    from services.project.compile import CompileError, dialect_for
    from services.project.loader import split_semantic
    from services.project.schema import parse_project_yaml
    from services.project.warehouse_drift import parse_layer

    entities_by_path, _defs, _rels = parse_layer(split_semantic(_read_project(root)))
    leaf_to_table: dict[str, str] = {}
    ambiguous: set[str] = set()
    for entity in entities_by_path.values():
        leaf = entity.source.table.split(".")[-1].lower()
        if leaf in leaf_to_table and leaf_to_table[leaf] != entity.source.table:
            ambiguous.add(leaf)
        leaf_to_table[leaf] = entity.source.table
    for leaf in ambiguous:
        del leaf_to_table[leaf]
    dialects: dict[str, str] = {}
    project_path = root / "dst.yaml"
    if project_path.exists():
        try:
            project = parse_project_yaml(project_path.read_text(encoding="utf-8"))
        except ValueError:
            project = None
        if project is not None:
            for name, decl in project.connections.items():
                try:
                    dialects[name] = dialect_for(decl.type)
                except CompileError:
                    continue  # a context source — no dialect
    return leaf_to_table, dialects, sorted(ambiguous)


def _lens_dialect_local(lens_dir: Path, dialects: dict[str, str]) -> str | None:
    """The lens's SQL dialect from its lens.yaml connections — file-first."""
    import yaml

    lens_yaml = lens_dir / "lens.yaml"
    if lens_yaml.exists():
        try:
            data = yaml.safe_load(lens_yaml.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            data = {}
        for conn in data.get("connections") or []:
            if str(conn) in dialects:
                return dialects[str(conn)]
    if len(dialects) == 1:  # one warehouse in the project — no ambiguity
        return next(iter(dialects.values()))
    return None


def _evals_gate(args: argparse.Namespace) -> int:
    """`dst evals gate <lens>` — run ONE lens's publish gate (the exact code
    path apply gates on) and print the decision, publishing nothing. Without it,
    the only way to learn one lens's verdict is a full apply across every armed
    gate in the project, and the score it computes is invisible outside the
    database.
    Server-side the staged eval run rolls back, so a red dry run never becomes
    the baseline the next real apply is judged against."""
    import httpx

    if not args.lens:
        print("error: `dst evals gate` needs a lens name", file=sys.stderr)
        return 2
    url, headers = _client(args)
    # The CANDIDATE tree rides along: gating the stored bundle
    # false-greened every unpublished shared-asset change — the exact pushes
    # a dry run exists for. Outside a project dir the server gates the stored
    # bundle and says so.
    files = _read_project(Path(getattr(args, "dir", ".") or "."))
    payload = {"files": files} if files else None
    if not files:
        print(
            "note: no project files under --dir — gating the SERVER's stored bundle; "
            "unpublished file changes are not reflected",
            file=sys.stderr,
        )
    r = httpx.post(
        f"{url}/mgmt/lenses/{args.lens}/evals/gate",
        headers=headers,
        json=payload,
        timeout=900.0,
    )
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except Exception:  # noqa: BLE001 — non-JSON error body
            detail = r.text[:300]
        print(f"error: gate check failed (HTTP {r.status_code}): {detail}", file=sys.stderr)
        return 1
    out = r.json()
    if getattr(args, "as_json", False):
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        label = str(out.get("gate"))
        d: dict[str, Any] = out.get("gate_detail") or {}
        if label == "passed":
            painted = style.good(label)
        elif label == "off" or label.startswith("skipped"):
            painted = style.warn(label)
        else:
            painted = style.bad(label)
        print(f"{style.accent('lens ' + args.lens)}: gate {painted}")
        if d.get("score") is not None:
            prev = d.get("prev_score")
            bar = (
                f" (prev {prev})"
                if prev is not None
                else f" ({d.get('baseline') or 'no previous run'})"
            )
            print(f"  score {d['score']}{bar}")
        for q in d.get("failing") or []:
            print(f"  {style.bad('failing:')} {q}")
        for m in d.get("certified_failures") or []:
            print(f"  {style.bad('certified:')} {m}")
        for cid in d.get("flaky") or []:
            print(
                f"  {style.warn('flaky:')} {cid} " + style.dim("(passed on retry — certify to pin)")
            )
        if d and not d.get("gated"):
            print(f"  {style.warn('not scored:')} {d.get('detail') or d.get('skip_reason')}")
        for err in out.get("errors") or []:
            print(f"  {style.bad('error:')} {err}")
        if note := out.get("scope_note"):
            print(f"  {style.warn('scope:')} {note}")
        print(style.dim("  dry run — nothing published, no baseline recorded"))
    red = label not in ("passed", "off") and not label.startswith("skipped")
    return 1 if red else 0


def _evals_dispatch(args: argparse.Namespace) -> int:
    if args.action == "gate":
        return _evals_gate(args)
    if args.action == "from-traffic":
        return _evals_from_traffic(args)
    return _evals_migrate(args)


def _evals_from_traffic(args: argparse.Namespace) -> int:
    """`dst evals from-traffic <lens>` — production questions become the suite.

    Recent request-log rows are drafted into
    lenses/<lens>/evals/cases.yaml with the OBSERVED outcome shape as the
    expectation (`ok`→answer, `refused`/`rejected`→refuse,
    `clarification`→clarify; errors are faults, not expectations — skipped).

    Drafts land as ``status: candidate`` / ``source: harvested`` — visible to
    `dst test` (counted, never scored) until a person promotes them to
    approved. The file is the interface: entries are APPENDED, existing bytes
    (and their comments) untouched, and nothing lands server-side until the
    user reviews and runs `dst apply` — same contract as `dst evals migrate`."""
    import httpx
    import yaml

    if not args.lens:
        print("error: `dst evals from-traffic` needs a lens name", file=sys.stderr)
        return 1
    url, headers = _client(args)
    r = httpx.get(
        f"{url}/mgmt/observe/requests",
        headers=headers,
        params={"lens": args.lens, "limit": args.limit},
        timeout=30,
    )
    if r.status_code >= 400:
        print(f"error: {_detail(r)}", file=sys.stderr)
        return 1
    rows = r.json()

    # The stored triage field maps deterministically; sniffing prose was tried
    # elsewhere and misattributed — the same rule holds here.
    expect_of = {
        "ok": "answer",
        "refused": "refuse",
        "rejected": "refuse",
        "clarification": "clarify",
    }

    path = Path(args.dir) / "lenses" / args.lens / "evals" / "cases.yaml"
    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
    existing = yaml.safe_load(existing_text) if existing_text.strip() else []
    if existing is not None and not isinstance(existing, list):
        print(f"error: {path} is not a top-level YAML list", file=sys.stderr)
        return 1

    def norm(q: str) -> str:
        return " ".join(q.split()).rstrip("?").casefold()

    seen = {
        norm(str(c["question"]))
        for c in existing or []
        if isinstance(c, dict) and c.get("question")
    }
    drafted: list[dict[str, str]] = []
    skipped_outcome = skipped_dupe = skipped_ungoverned = 0
    for row in rows:
        expect = expect_of.get(str(row.get("status")))
        q = str(row.get("question") or "").strip()
        if expect is None or not q:
            skipped_outcome += 1
            continue
        if expect == "answer" and row.get("resolution_tag") in ("mixed", "inferred"):
            # An answer whose figure the ledger attributes to an invented
            # aggregate is not a known-good expectation — drafting it would
            # pin the ungoverned path as the suite's idea of correct. Rows
            # written before the ledger (no tag) keep the old contract.
            skipped_ungoverned += 1
            continue
        if norm(q) in seen:
            skipped_dupe += 1
            continue
        seen.add(norm(q))
        drafted.append(
            {"question": q, "expect": expect, "status": "candidate", "source": "harvested"}
        )
    if not drafted:
        print(
            f"nothing new to draft: {len(rows)} traffic row(s), "
            f"{skipped_dupe} already in the suite, {skipped_outcome} without a usable outcome, "
            f"{skipped_ungoverned} answered by an ungoverned path"
        )
        return 0
    stamp = (
        "# drafted from traffic by `dst evals from-traffic` — review, promote to\n"
        "# status: approved, then `dst apply`\n"
    )
    block = yaml.safe_dump(drafted, sort_keys=False, allow_unicode=True, width=88)
    path.parent.mkdir(parents=True, exist_ok=True)
    joiner = "" if not existing_text or existing_text.endswith("\n") else "\n"
    path.write_text(existing_text + joiner + stamp + block, encoding="utf-8")
    by_expect: dict[str, int] = {}
    for d in drafted:
        by_expect[d["expect"]] = by_expect.get(d["expect"], 0) + 1
    shape = " · ".join(f"{k}: {v}" for k, v in sorted(by_expect.items()))
    print(
        f"{len(drafted)} candidate case(s) drafted into {path} ({shape}); "
        f"{skipped_dupe} duplicate(s), {skipped_outcome} non-outcome row(s) and "
        f"{skipped_ungoverned} ungoverned-path answer(s) skipped"
    )
    print("candidates are visible to `dst test` but not scored until promoted to approved")
    return 0


def _evals_migrate(args: argparse.Namespace) -> int:
    """`dst evals migrate` — LOCAL, file-to-file, no server: every APPROVED
    value-shaped eval case (has expected_sql) becomes a
    certified_answers.yaml entry (the certified corpus IS the regression
    suite); cases.yaml keeps behavioral entries and anything not yet
    approved — migration is a promotion, and only a reviewed case may enter
    the serving corpus. Leaf table names are repointed at their live sources
    (the eval plane's own mapping rule) so migrated SQL actually executes.
    Nothing lands until the user reviews and runs `dst apply` — this edits
    files, exactly like they would by hand."""
    import yaml

    from services.project.apply import rewrite_to_sources

    root = Path(args.dir)
    case_files = sorted((root / "lenses").glob("*/evals/cases.yaml"))
    if not case_files:
        print(f"no lenses with evals/cases.yaml under {root}/lenses/ — nothing to migrate")
        return 0
    leaf_to_table, dialects, ambiguous = _migrate_context(root)
    for leaf in ambiguous:
        print(
            f"warning: leaf table '{leaf}' maps to several entity sources — its "
            "references stay unqualified; qualify those by hand",
            file=sys.stderr,
        )
    total_migrated = 0
    for cases_path in case_files:
        lens_dir = cases_path.parent.parent
        lens = lens_dir.name
        dialect = _lens_dialect_local(lens_dir, dialects)
        cases_text = cases_path.read_text(encoding="utf-8")
        cases = yaml.safe_load(cases_text) or []
        if not isinstance(cases, list):
            print(f"error: {cases_path} is not a top-level YAML list — fix it first")
            return 1
        cert_path = lens_dir / "certified_answers.yaml"
        cert_text = cert_path.read_text(encoding="utf-8") if cert_path.exists() else ""
        certified = yaml.safe_load(cert_text) or []
        if not isinstance(certified, list):
            print(f"error: {cert_path} is not a top-level YAML list — fix it first")
            return 1
        have = {str(a.get("question", "")).strip().lower() for a in certified}
        fresh: list[dict[str, object]] = []
        kept: list[dict[str, object]] = []
        skipped: list[str] = []
        for case in cases:
            question = str(case.get("question", "")).strip() if isinstance(case, dict) else ""
            if not isinstance(case, dict) or not case.get("expected_sql"):
                kept.append(case)  # behavioral / question-only — stays an eval case
                continue
            if question.lower() in have:
                skipped.append(question)  # already certified — the case just retires
                continue
            status = str(case.get("status") or "candidate")
            if status != "approved":
                # Certified answers SERVE on match — promoting unreviewed SQL
                # into the corpus is exactly what review exists to prevent.
                kept.append(case)
                print(
                    f"{lens}: kept '{question}' as an eval case — status is "
                    f"'{status}', and only approved cases migrate (approve it, "
                    "or certify by hand)"
                )
                continue
            sql = str(case["expected_sql"])
            if dialect is None:
                print(
                    f"warning: {lens}: no warehouse dialect resolvable from lens.yaml/"
                    f"dst.yaml — '{question}' migrated with its SQL unqualified; "
                    "verify table names before apply",
                    file=sys.stderr,
                )
            else:
                try:
                    # Eval-plane SQL names leaf tables; certified SQL runs live
                    # and needs the physical names (the same rewrite apply's
                    # oracle gate uses).
                    sql = rewrite_to_sources(sql, dialect, leaf_to_table)
                except Exception as exc:  # noqa: BLE001 — keep the original, say why
                    first = str(exc).splitlines()[0][:160]
                    print(
                        f"warning: {lens}: could not qualify '{question}' ({first}) — "
                        "migrated verbatim; qualify table names by hand",
                        file=sys.stderr,
                    )
            if case.get("expected_answer"):
                print(
                    f"note: {lens}: '{question}' had expected_answer — not carried; "
                    "certified answers verify by executing their SQL",
                    file=sys.stderr,
                )
            entry: dict[str, object] = {
                "question": question,
                "sql": sql,
                "source": "evals:migrated",
            }
            if case.get("created_by"):
                entry["verified_by"] = case["created_by"]
            fresh.append(entry)
            have.add(question.lower())
        if not fresh and not skipped:
            print(f"{lens}: no value cases — nothing to migrate")
            continue
        if fresh:
            dumped = yaml.safe_dump(fresh, sort_keys=False, allow_unicode=True)
            if certified:
                cert_out = cert_text.rstrip("\n") + "\n" + dumped
            else:  # keep the comment header, drop the empty `[]`
                head = _comment_head(cert_text)
                cert_out = "\n".join([*head, dumped]) if head else dumped
            cert_path.write_text(cert_out, encoding="utf-8")
        head = _comment_head(cases_text)
        # No live `[]` when nothing is kept — appending after it is malformed
        # YAML (the scaffold dropped its placeholder for the same reason).
        kept_dump = yaml.safe_dump(kept, sort_keys=False, allow_unicode=True) if kept else ""
        cases_out = "\n".join([*head, kept_dump]) if head else kept_dump
        cases_path.write_text(cases_out, encoding="utf-8")
        for question in skipped:
            print(f"{lens}: skipped '{question}' — already in certified_answers.yaml")
        print(
            f"{lens}: migrated {len(fresh)} case(s) into certified_answers.yaml; "
            f"{len(kept)} behavioral kept"
        )
        total_migrated += len(fresh)
    if total_migrated:
        print("review the entries, then `dst apply` to land them")
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "evals",
        help="eval utilities; `gate <lens>` runs ONE lens's publish gate as a dry run "
        "(the apply code path, nothing published); `migrate` moves value cases "
        "(expected_sql) into certified_answers.yaml — local file rewrite; "
        "`from-traffic <lens>` drafts candidate cases from recent production "
        "questions (observed outcome becomes the expectation) — appends to "
        "evals/cases.yaml for review",
    )
    p.add_argument("action", choices=["migrate", "gate", "from-traffic"])
    p.add_argument("lens", nargs="?", help="lens name (gate and from-traffic)")
    p.add_argument(
        "--limit",
        type=int,
        default=200,
        help="from-traffic: how many recent requests to read (default 200)",
    )
    p.add_argument("--dir", default=".", help="project root (default: .)")
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.add_argument(
        "--env",
        help="target a named environment minted by `dst env new` — resolves "
        "its recorded admin token from .dst/envs.json",
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="the decision as JSON")
    p.set_defaults(fn=_evals_dispatch)
