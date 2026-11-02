"""The `dst observe` command."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from services.cli import style
from services.cli._common import (
    _bytes,
    _client,
    _detail,
)


def _observe(args: argparse.Namespace) -> int:
    """Who has been using this layer, and what for — from the terminal.

    The same view the API and the dashboard expose, on the CLI: "who has been
    using this and what for" is a question data teams answer from a terminal, and
    governance reachable only from a browser is not reachable by the callers most
    likely to need it.

    Read-only, admin-authed (`/mgmt/observe/*`), four shapes:

      dst observe                 headline + per-caller usage — the CFO answer
      dst observe callers         who, how many, how much, declines and errors
      dst observe requests        what they actually asked
      dst observe show <req_id>   one request: question, SQL, confidence
      dst observe export          the request ledger (--since/--until, --format csv|json)

    `--json` on any of them for the parseable form; agents should prefer it.
    Test traffic (agent `dst-test` or `benchmark`) is out of the headline,
    the basis line and the caller rollup unless `--include-test`.
    """
    import httpx

    url, headers = _client(args)

    def get(path: str, **params: str | int | None) -> object:
        r = httpx.get(
            f"{url}/mgmt/observe{path}",
            headers=headers,
            params={k: v for k, v in params.items() if v is not None},
            timeout=args.timeout,
        )
        if r.status_code >= 400:
            print(f"error: {_detail(r)}", file=sys.stderr)
            raise SystemExit(1)
        return r.json()

    action = getattr(args, "action", None) or "summary"
    include_test = True if getattr(args, "include_test", False) else None

    if action == "export":
        # The ledger at /mgmt/requests: every row in the window, written as the
        # server renders it (CSV bytes or a JSON list) so a pipe gets a file.
        fmt = "json" if args.json else (getattr(args, "format", None) or "csv")
        r = httpx.get(
            f"{url}/mgmt/requests",
            headers=headers,
            params={
                k: v
                for k, v in {
                    "since": getattr(args, "since", None),
                    "until": getattr(args, "until", None),
                    "lens": args.lens,
                    "caller": getattr(args, "caller", None),
                    "format": fmt,
                }.items()
                if v is not None
            },
            timeout=args.timeout,
        )
        if r.status_code >= 400:
            print(f"error: {_detail(r)}", file=sys.stderr)
            return 1
        sys.stdout.write(r.text if fmt == "csv" else json.dumps(r.json(), indent=2) + "\n")
        return 0

    if action == "show":
        trace = get(f"/requests/{args.request_id}")
        if args.json:
            print(json.dumps(trace, indent=2))
            return 0
        assert isinstance(trace, dict)
        for key in (
            "request_id",
            "created_at",
            "caller",
            "lens",
            "status",
            "confidence",
            "resolution_tag",
            "purpose",
        ):
            if trace.get(key) is not None:
                print(f"{key + ':':<14}{trace[key]}")
        # A probe row's `question` IS its SQL (the caller sent a statement, not a
        # question): print it once, labelled, instead of twice.
        probe = trace.get("kind") in ("sql_probe", "admin_sql")
        if not probe and (q := trace.get("question")):
            print(f"\n{q}")
        if sql := trace.get("sql"):
            print(f"\nsql:\n{sql}" if probe else f"\n{sql}")
        res = trace.get("resolution")
        if isinstance(res, dict) and res.get("slots"):
            # The ledger, one slot per line: what the answer resolved to and
            # whether each choice was the model's (declared) or the generator's.
            print(f"\n{style.accent('resolution:')} {res.get('tag')} ({res.get('method')})")
            for s in res["slots"]:
                if not isinstance(s, dict):
                    continue
                value = f" = {s['value']}" if s.get("value") else ""
                print(f"  {str(s.get('kind')):<11}{s.get('name')}{value}  [{s.get('source')}]")
        return 0

    if action == "requests":
        rows = get("/requests", lens=args.lens, status=args.status, limit=args.limit)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        assert isinstance(rows, list)
        if not rows:
            print("no requests recorded")
            return 0
        for r_ in rows:
            when = str(r_.get("created_at") or "")[:19].replace("T", " ")
            # Two-space gutters BETWEEN columns, not width-only padding: a
            # 14-char lens name or the 13-char 'clarification' status fills its
            # field exactly, and pad-only columns then run together.
            print(
                f"{when}  {str(r_.get('caller') or '-'):<16}  "
                f"{str(r_.get('lens') or '-'):<14}  "
                f"{str(r_.get('status') or '-'):<13}  "
                f"{str(r_.get('question') or '')[:60]}"
            )
        return 0

    callers = get("/callers", lens=args.lens, include_test=include_test)
    assert isinstance(callers, list)
    if action == "callers" and args.json:
        print(json.dumps(callers, indent=2))
        return 0

    kpis = get("/kpis", include_test=include_test) if action == "summary" else None
    if args.json:
        print(json.dumps({"kpis": kpis, "callers": callers}, indent=2))
        return 0

    if isinstance(kpis, dict):
        # The two bills apart, each labelled: the AI providers' and the
        # warehouse's. A total with unpriced items behind it is a floor and
        # says so; a sum of both would be neither bill.
        ai = f"AI ${float(kpis.get('ai_cost_usd') or 0):.4f}"
        if unpriced := int(kpis.get("unpriced") or 0):
            ai += f" (+{unpriced} unpriced)"
        head = f"{kpis.get('queries', 0)} queries · {ai}"
        if "warehouse_cost_usd" in kpis:
            wh = f"warehouse ${float(kpis.get('warehouse_cost_usd') or 0):.4f}"
            if wh_unpriced := int(kpis.get("wh_unpriced") or 0):
                wh += f" (+{wh_unpriced} unpriced)"
            head += f" · {wh}"
        if (scanned := kpis.get("wh_bytes")) is not None:
            # The warehouse half of the bill, in the unit the warehouse bills:
            # a Snowflake credit has no price here, but bytes are bytes.
            head += f" · {_bytes(int(scanned))} scanned"
        outcomes = kpis.get("outcomes")
        if isinstance(outcomes, dict):
            # Declines are governed outcomes (authoring/scope work), errors are
            # faults — printing them as one number turns a handful of real faults
            # plus a pile of governed declines into one alarming "error rate", and
            # the response to that number is always wrong. Only nonzero buckets,
            # ok first, error last.
            order = ("ok", "refused", "clarification", "rejected", "error")
            split = " · ".join(f"{outcomes.get(k, 0)} {k}" for k in order if outcomes.get(k))
            print(f"{head} · {split}" if split else head)
        else:  # pre-split server: 'errors' there means every non-ok outcome
            print(f"{head} · {kpis.get('errors', 0)} non-ok")
        if isinstance(modes := kpis.get("modes"), dict) and modes:
            # The declines and the capped answers by failure mode, most
            # frequent first: each name says which file fixes it.
            split = " · ".join(f"{n} {m}" for m, n in modes.items())
            print(f"{style.accent('modes:')} {split}")
        if isinstance(types := kpis.get("question_types"), dict) and types:
            # The questions that asked for no number, by type: answered
            # before any binding, never a refusal.
            split = " · ".join(f"{n} {m}" for m, n in types.items())
            print(f"{style.accent('question types:')} {split}")
        basis = kpis.get("basis")
        if isinstance(basis, dict) and int(basis.get("n") or 0):
            # Basis over EVERY served caller read, `raw` (the caller's own SQL)
            # inside the denominator: the governed share is only honest when
            # the ungoverned reads count against it. Nonzero buckets only;
            # `unknown` = recorded before the ledger, said as itself.
            n = int(basis["n"])
            buckets = ("certified", "declared", "mixed", "inferred", "raw", "unknown")
            split = " · ".join(
                f"{k} {round(100 * int(basis.get(k) or 0) / n)}%" for k in buckets if basis.get(k)
            )
            share = basis.get("governed_share")
            governed = f" · governed {round(100 * float(share))}%" if share is not None else ""
            print(f"{style.accent('basis:')} {split}  (n = {n:,}){governed}")
        elif isinstance(hist := kpis.get("resolution_histogram"), dict) and hist:
            # A server from before the basis block: the governed-only histogram
            # as counts. A pre-ledger server prints no line at all.
            order = ("certified", "declared", "mixed", "inferred", "unknown")
            split = " · ".join(f"{hist.get(k, 0)} {k}" for k in order if hist.get(k))
            print(f"{style.accent('basis:')} {split}")
        explore = kpis.get("explore")
        if isinstance(explore, dict) and int(explore.get("queries") or 0):
            # Outside-lens reads, cost and all, apart from the governed
            # figures: the `sql` tool inside a lens and answers from exposed
            # tables are not governed answers and are not counted above.
            print(
                f"{style.accent('explore:')} {int(explore['queries']):,} outside-lens read(s), "
                f"{int(explore.get('answered') or 0):,} answered · "
                f"AI ${float(explore.get('ai_cost_usd') or 0):.4f} · "
                f"warehouse ${float(explore.get('warehouse_cost_usd') or 0):.4f} · "
                f"{_bytes(int(explore.get('wh_bytes') or 0))} scanned — not counted above"
            )
        if test_n := int(kpis.get("test_requests") or 0):
            # Left out, and said: a filter that hides rows names how many.
            print(
                style.dim(
                    f"{test_n:,} test request(s) (agent dst-test / benchmark) not counted "
                    "above — --include-test counts them"
                )
            )
        routing = kpis.get("routing")
        if isinstance(routing, dict) and (routed_n := int(routing.get("decisions") or 0)):
            # Spend that no request row carries, said as itself: without this
            # line a header of "0 queries · $0.0040 AI" reads as a bug.
            print(
                style.dim(
                    f"AI spend includes ${float(routing.get('ai_cost_usd') or 0):.4f} "
                    f"routing {routed_n:,} question(s) no lens served"
                )
            )
        by_purpose = kpis.get("ai_by_purpose")
        if isinstance(by_purpose, dict) and by_purpose:
            # Where the AI spend went, one figure per purpose (route, decide,
            # judge, equivalence, bind, generate, compose).
            parts = []
            for name, b in by_purpose.items():
                if not isinstance(b, dict):
                    continue
                part = f"{name} ${float(b.get('ai_cost_usd') or 0):.4f}"
                if int(b.get("unpriced") or 0):
                    part += f" (+{b['unpriced']} unpriced)"
                parts.append(part)
            print(f"{style.accent('AI by purpose:')} {' · '.join(parts)}")
        evals = kpis.get("eval_spend")
        if isinstance(evals, dict) and int(evals.get("runs") or 0):
            unpriced_runs = int(evals.get("unpriced_runs") or 0)
            print(
                style.dim(
                    f"test traffic: {int(evals['runs']):,} eval run(s) spent "
                    f"${float(evals.get('ai_cost_usd') or 0):.4f} AI"
                    + (f" (+{unpriced_runs} with unpriced calls)" if unpriced_runs else "")
                    + ", not counted above"
                )
            )
        if isinstance(profiles := kpis.get("profiles"), dict):
            # How old the facts behind every answer are: a probe job that
            # stopped shows here before it shows as a wrong answer.
            from services.lenses.profile_age import headline as profile_headline

            line = profile_headline(profiles)
            if line is not None:
                print(style.warn(line) if profiles.get("stale_connections") else line)
        print()
    if not callers:
        # Distinguish "nobody has used it" from "the surface is broken" — the same
        # confusion that made an empty router page read as a bug.
        print("no caller activity recorded yet")
        return 0
    print(f"{'caller':<20}{'queries':>9}{'cost':>11}{'declined':>10}{'errors':>8}")
    for c in callers:
        declined = c.get("declined")
        print(
            f"{str(c.get('caller') or '-'):<20}{c.get('queries', 0):>9}"
            f"{float(c.get('cost_usd') or 0):>11.4f}"
            f"{('-' if declined is None else declined):>10}{c.get('errors', 0):>8}"
        )
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "observe",
        help="who has been using this layer and what for — usage by caller, recent "
        "requests, one request in full",
    )
    p.add_argument(
        "action",
        nargs="?",
        choices=["callers", "requests", "show", "export"],
        help="omit for the headline + per-caller usage; `export` writes the request ledger",
    )
    p.add_argument("request_id", nargs="?", help="with `show`: the request to print")
    p.add_argument("--lens", help="restrict to one lens")
    p.add_argument("--status", help="with `requests`: ok | refused | error")
    p.add_argument("--limit", type=int, default=50, help="with `requests` (max 200)")
    p.add_argument("--since", help="with `export`: ISO date or instant, inclusive")
    p.add_argument("--until", help="with `export`: ISO date or instant, exclusive")
    p.add_argument("--caller", help="with `export`: one caller")
    p.add_argument("--format", choices=["csv", "json"], help="with `export`: csv (default) or json")
    p.add_argument(
        "--include-test",
        action="store_true",
        help="count test traffic (agent dst-test or benchmark) in the headline and callers",
    )
    p.add_argument("--json", action="store_true")
    p.add_argument("--timeout", type=float, default=60)
    p.add_argument("--dir", default=".", help="project root whose .env holds URL and credentials")
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.set_defaults(fn=_observe)
