"""The `dst reviews`, `dst rule`, `dst retire`, `dst correct` commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from services.cli._common import (
    _client,
    _detail,
)


def _reviews(args: argparse.Namespace) -> int:
    """The review queue — the data team's whole queue under an admin token, or
    just YOUR OWN tickets under a caller key (`--key dst_…`, or a project whose
    .env holds only DST_API_KEY).

    The caller half exists because filing a correction is only half a loop. A
    business user who reports a wrong answer learns nothing more unless someone
    tells him out of band; `/mgmt/reviews` is admin-only and correctly so. The
    data plane's `/v1/reviews` answers the one question he has — what happened
    to the thing I reported — and answers it about his requests only."""
    import httpx

    url, headers = _client(args, caller_key_ok=True, admin_first=True)
    if args.watch:
        if args.as_caller:
            print(
                "error: --watch polls the data team's queue and needs an admin token; "
                "`dst reviews --key …` lists your own tickets",
                file=sys.stderr,
            )
            return 1
        return _reviews_watch(args, url, headers)
    # Unfiltered by default — --json included. Defaulting --json to needs_human
    # hid auto-approved origin:ai tickets and made auto_review look like a no-op.
    # Agents filter themselves with --state/--origin.
    params = {"state": args.state} if args.state else {}
    path = "/v1/reviews" if args.as_caller else "/mgmt/reviews"
    r = httpx.get(f"{url}{path}", headers=headers, params=params, timeout=60)
    tickets = r.raise_for_status().json()
    if args.origin:
        # Client-side: the list endpoint filters by state only.
        tickets = [t for t in tickets if t.get("origin") == args.origin]
    if args.json:
        print(json.dumps(tickets))
        return 0
    if not tickets:
        scope = "you have filed no corrections" if args.as_caller else "review queue is empty"
        print(scope + (f" (state={args.state})" if args.state else ""))
        return 0
    for t in tickets:
        verdict = t.get("human_verdict") or t.get("ai_verdict") or "-"
        print(
            f"{t['ticket_id']}  {t['state']:<10} {t['lens']:<20} "
            f"caller={t['caller']:<12} origin={t.get('origin') or 'human':<6} verdict={verdict}"
        )
    return 0


def _reviews_watch(args: argparse.Namespace, url: str, headers: dict[str, str]) -> int:
    """Poll the needs_human queue; print each ticket once as it first appears
    (id, lens, question). In-memory seen-set only — Ctrl-C stops it; no
    daemon, no state file (agent- and cron-friendly).

    Takes the resolved client from `_reviews`: the door has to be chosen before
    the branch, so a caller key gets the refusal that names its own read path
    instead of a bare "no admin token"."""
    import time

    import httpx

    seen: set[str] = set()
    try:
        while True:
            try:
                r = httpx.get(
                    f"{url}/mgmt/reviews",
                    headers=headers,
                    params={"state": "needs_human"},
                    timeout=60,
                )
                tickets = r.raise_for_status().json()
            except httpx.HTTPError as exc:
                # A watcher must ride out transient 5xx/timeouts, not die on them.
                print(f"watch: {exc} — retrying", file=sys.stderr, flush=True)
                time.sleep(args.interval)
                continue
            for t in tickets:
                if t["ticket_id"] in seen:
                    continue
                seen.add(t["ticket_id"])
                # The ticket doesn't carry the question — join the request log,
                # exactly as the UI's queue does.
                rq = httpx.get(
                    f"{url}/mgmt/observe/requests/{t['request_id']}", headers=headers, timeout=60
                )
                q = rq.json().get("question") if rq.status_code == 200 else None
                line = " ".join(q.split()) if q else t["request_id"]
                print(f"{t['ticket_id']}  {t['lens']}  {line}", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def _retire(args: argparse.Namespace) -> int:
    """Retire a certified answer — served no more, history kept.

    A wrong certified answer used to be unremovable in practice: files own only
    file-originated rows, so a review-promoted pair could only be destroyed
    (losing its provenance) or left serving. This is the missing verb.
    """
    import httpx

    url, headers = _client(args)
    r = httpx.post(
        f"{url}/mgmt/lenses/{args.lens}/certified/{args.answer_id}/retire",
        headers=headers,
        timeout=30,
    )
    if r.status_code == 404:
        print(f"no certified answer '{args.answer_id}' on lens '{args.lens}'", file=sys.stderr)
        return 1
    body = r.raise_for_status().json()
    print(f"retired {args.answer_id}: {body.get('question', '')}")
    print("  it will not be served, matched, or tested again; its record stays.")
    return 0


def _rule(args: argparse.Namespace) -> int:
    import httpx

    url, headers = _client(args)
    body = {"verdict": args.verdict, "reasoning": args.reasoning}
    r = httpx.post(
        f"{url}/mgmt/reviews/{args.ticket_id}/rule", headers=headers, json=body, timeout=60
    )
    if r.status_code == 404:
        print(f"no review ticket '{args.ticket_id}'")
        return 1
    t = r.raise_for_status().json()
    print(f"{t['ticket_id']}: ruled {args.verdict} (state={t['state']})")
    if not args.certify:
        return 0
    # --certify: promote the ruled question→SQL in the same act (the UI's
    # "Promote this question→SQL"). The rule response carries lens + request_id.
    # Embedder-less installs still succeed (0027): stored unembedded + a warning.
    r = httpx.post(
        f"{url}/mgmt/lenses/{t['lens']}/certified/from-request/{t['request_id']}",
        headers=headers,
        json={"allow_untyped": bool(getattr(args, "allow_untyped", False))},
        timeout=60,
    )
    if r.status_code >= 400:
        # The ruling already landed — say so instead of tracebacking past it.
        try:
            detail = r.json().get("detail")
        except Exception:  # noqa: BLE001 — non-JSON error bodies happen on 5xx
            detail = r.text[:200]
        print(f"error: ruled, but not certified — {detail}", file=sys.stderr)
        return 1
    cert = r.json()
    print(f"certified {cert['id']} (lens={cert['lens']})")
    if cert.get("warning"):
        print(cert["warning"], file=sys.stderr)
    return 0


def _note_text(args: argparse.Namespace) -> str | None:
    """The correction note: ``--note``, ``--note-file`` (``-`` reads stdin), or
    piped stdin. Corrections are paragraphs — the cold-start agents wrote
    multi-sentence rulings, which is not a shell flag's shape. Prints why and
    returns None when no note can be read."""
    if args.note_file:
        if args.note_file == "-":
            note = sys.stdin.read()
        else:
            path = Path(args.note_file)
            if not path.is_file():
                print(f"error: no note file '{args.note_file}'", file=sys.stderr)
                return None
            note = path.read_text(encoding="utf-8")
    elif args.note:
        note = args.note
    elif not sys.stdin.isatty():
        note = sys.stdin.read()
    else:
        print(
            "error: no correction note — pass --note TEXT, --note-file PATH "
            "(or `-`), or pipe the note in",
            file=sys.stderr,
        )
        return None
    note = note.strip()
    if not note:
        print("error: the correction note is empty", file=sys.stderr)
        return None
    return note


def _correct(args: argparse.Namespace) -> int:
    """`dst correct <request_id> --kind K --target T --note …` — step 3 of
    the documented improvement loop as a verb.

    Filing a correction was REST-only, so every agent running the loop
    hand-rolled an HTTP script to get from a wrong answer to a filed correction.
    ``--target`` is REQUIRED here even though the API allows it to be absent:
    the term the correction is about is what the reviewer's fix lands on, and
    a fix grafted onto the wrong page can regress a question that was passing.
    Opens the review ticket the ruling is made on.

    ``--key dst_…`` files AS a caller, and that door is the reason the verb
    matters: the flywheel begins with the person who SAW the wrong answer, and
    that person holds a caller key and nothing else. Resolving credentials as an
    admin verb shut the verb on exactly them, so every wrong answer a business
    user found reached the data team as a message rather than through the
    product. `POST /v1/reviews` had accepted caller keys all along; no CLI path
    reached it with one."""
    import httpx

    url, headers = _client(args, caller_key_ok=True, admin_first=True)
    note = _note_text(args)
    if note is None:
        return 1
    correction: dict[str, object] = {"kind": args.kind, "note": note, "target": args.target}
    if args.corrected_sql:
        correction["corrected_sql"] = args.corrected_sql
    if args.corrected_answer:
        correction["corrected_answer"] = args.corrected_answer
    r = httpx.post(
        f"{url}/v1/reviews",
        headers=headers,
        json={"request_id": args.request_id, "correction": correction},
        timeout=args.timeout,
    )
    if r.status_code >= 400:
        print(f"error: {_detail(r)}", file=sys.stderr)
        return 1
    ticket = r.json()
    if args.json:
        print(json.dumps(ticket, indent=2, ensure_ascii=False))
        return 0
    print(
        f"{ticket['ticket_id']}: correction filed ({args.kind} → {args.target}) "
        f"on {args.request_id} — state={ticket.get('state')}"
    )
    # The next step has to be one the filer can actually take. `rule` is an
    # admin verb: printing it at a business user who just filed with a caller
    # key sends them straight back into the door that was shut.
    if args.as_caller:
        print(
            f"next: your data team triages it — check back with "
            f"`dst reviews --key …` (this ticket: {ticket['ticket_id']})"
        )
    else:
        print(
            f"next: dst rule {ticket['ticket_id']} --verdict approve|changes|reject "
            "[--certify] — then author the fix in the project's files"
        )
    return 0


def register_reviews(sub: Any) -> None:
    p = sub.add_parser(
        "reviews",
        help="list the review queue (all states and origins) — or, with a caller "
        "key, just the tickets on your own requests",
    )
    p.add_argument("--state", help="filter: open|ai_review|needs_human|approved|changes|rejected")
    p.add_argument(
        "--origin",
        choices=["ai", "human"],
        help="filter: 'ai' = auto-flagged by the lens's auto_review policy, "
        "'human' = caller-raised",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--json",
        action="store_true",
        help="machine-readable ticket list on stdout (unfiltered — combine with --state/--origin)",
    )
    g.add_argument(
        "--watch",
        action="store_true",
        help="poll needs_human tickets, print each new one once (id, lens, question); Ctrl-C stops",
    )
    p.add_argument("--interval", type=int, default=30, help="watch poll interval, seconds")
    p.add_argument(
        "--dir",
        default=".",
        help="project root whose .env holds the URL and credentials",
    )
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.add_argument(
        "--key",
        help="read AS a caller: a dst_ caller key (default: DST_API_KEY from env or "
        "./.env when no admin token is in scope). Lists only the tickets on your own "
        "requests — what happened to the corrections you filed",
    )
    p.set_defaults(fn=_reviews)


def register_rule(sub: Any) -> None:
    p = sub.add_parser("rule", help="rule on a review ticket")
    p.add_argument("ticket_id")
    p.add_argument("--verdict", required=True, choices=["approve", "changes", "reject"])
    p.add_argument("--reasoning", default="")
    p.add_argument(
        "--certify",
        action="store_true",
        help="after an approve ruling, promote the request's question→SQL to certified",
    )
    p.add_argument(
        "--allow-untyped",
        dest="allow_untyped",
        action="store_true",
        help="with --certify: certify an answer that was served UNTYPED (the raw-SQL "
        "escalation) — a different act from certifying a typed resolution; you have "
        "read the SQL",
    )
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.set_defaults(fn=_rule)


def register_retire(sub: Any) -> None:
    p = sub.add_parser(
        "retire",
        help="stop serving a certified answer, keeping its history (the way out "
        "when an approved answer turns out to be wrong)",
    )
    p.add_argument("lens")
    p.add_argument("answer_id", help="certified answer id (from `dst lens show` / the dashboard)")
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.set_defaults(fn=_retire)


def register_correct(sub: Any) -> None:
    p = sub.add_parser(
        "correct",
        help="file a correction against a served answer (loop step 3) — opens the "
        "review ticket the ruling is made on",
    )
    p.add_argument("request_id", help="the request_id of the wrong answer (printed by `query`)")
    p.add_argument(
        "--kind",
        required=True,
        choices=["definition", "scope", "number", "freshness", "other"],
        help="what kind of wrong it is — the drafter routes on this",
    )
    p.add_argument(
        "--target",
        required=True,
        help="the definition term (or artifact) this correction is about — used "
        "VERBATIM by the drafter; a new term drafts a new definition. Required: "
        "without it placement is vocabulary matching, which mistargets",
    )
    p.add_argument("--note", help="what is wrong, stated decisively (paragraphs: use --note-file)")
    p.add_argument("--note-file", help="read the note from a file, or `-` for stdin")
    p.add_argument("--corrected-sql", help="the SQL that WOULD have been right (strongest signal)")
    p.add_argument("--corrected-answer", help="the answer that would have been right")
    p.add_argument("--json", action="store_true", help="the full ticket JSON on stdout")
    p.add_argument("--timeout", type=int, default=180, help="seconds to wait (the judge runs here)")
    p.add_argument("--dir", default=".", help="project root whose .env supplies url + token")
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.add_argument(
        "--key",
        help="file AS a caller: a dst_ caller key (default: DST_API_KEY from env or "
        "./.env when no admin token is in scope) — the posture of the business user who "
        "found the wrong answer. You may correct your own requests; an admin, anyone's",
    )
    p.set_defaults(fn=_correct)
