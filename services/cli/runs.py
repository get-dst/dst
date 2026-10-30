"""The `dst runs` command."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from services.cli import style
from services.cli._common import (
    _client,
    _detail,
)


def _runs(args: argparse.Namespace) -> int:
    """The run history `dst test` always kept, finally visible — and comparable.

    dst runs <lens>              every recorded run, newest first
    dst runs <lens> --diff A B   two runs side by side: score delta + case flips

    A and B are run ids (a unique prefix is enough) or `latest` / `prev`. B may
    live on another server via --other-url/--other-token — that is the
    cross-environment compare: same suite, sandbox vs production."""
    import httpx

    from services.evals.service import REGRESSION_EPS

    url, headers = _client(args)

    def get(base: str, hdrs: dict[str, str], path: str) -> Any:
        r = httpx.get(f"{base}{path}", headers=hdrs, timeout=args.timeout)
        if r.status_code >= 400:
            print(f"error: {_detail(r)}", file=sys.stderr)
            raise SystemExit(1)
        return r.json()

    if args.all or not args.lens:
        # Every lens in one call — the cross-lens report that otherwise reads
        # eval_run by hand. Per lens, so a lens with no history shows as such.
        if args.diff:
            print("error: --diff compares two runs of ONE lens — name it", file=sys.stderr)
            return 1
        if not args.all:
            print("error: name a lens, or --all for every lens", file=sys.stderr)
            return 1
        # One call on a server that has the cross-lens endpoint; a lens at a
        # time on one that predates it.
        r = httpx.get(
            f"{url}/mgmt/observe/runs",
            params={"last": max(args.last, 0)},
            headers=headers,
            timeout=args.timeout,
        )
        if r.status_code == 404:
            names = [lens["name"] for lens in get(url, headers, "/mgmt/lenses")]
            by_lens = {
                n: get(url, headers, f"/mgmt/lenses/{n}/evals/runs")[: max(args.last, 0)]
                for n in names
            }
        elif r.status_code >= 400:
            print(f"error: {_detail(r)}", file=sys.stderr)
            return 1
        else:
            by_lens = {e["lens"]: e["runs"] for e in r.json()["runs"]}
            names = list(by_lens)
        if args.as_json:
            print(json.dumps({"runs": [{"lens": n, "runs": rs} for n, rs in by_lens.items()]}))
            return 0
        if not names:
            print(f"no lenses on {url}")
            return 0
        print(
            f"{'LENS':<28} {'ID':<10} {'MODE':<12} {'SCORE':>6} {'PASS':>5} {'FAIL':>5} "
            f"{'ERR':>4}  STARTED"
        )
        for n, rs in by_lens.items():
            if not rs:
                print(f"{n:<28} {style.dim('no recorded runs')}")
                continue
            for r in rs:
                score = "—" if r["score"] is None else f"{float(r['score']):.0%}"
                print(
                    f"{n:<28} {str(r['id'])[:8]:<10} {r['mode']:<12} {score:>6}"
                    f" {r['passed']:>5} {r['failed']:>5} {r['errored']:>4}"
                    f"  {str(r['started_at'])[:19]}"
                )
        return 0

    runs = get(url, headers, f"/mgmt/lenses/{args.lens}/evals/runs")
    if not args.diff:
        if args.as_json:
            print(json.dumps(runs, indent=2))
            return 0
        if not runs:
            # Two paths, one table: `dst test` writes to DATABASE_URL's database,
            # this command reads through the server. An empty list is as often a
            # server on another database as a suite never run, so say both.
            print(
                f"no recorded runs for lens {args.lens!r} on {url} — `dst test {args.lens}` "
                "records one in the database named by DATABASE_URL and prints which; this "
                "list shows it only if that is the database this server serves"
            )
            return 0
        print(f"{'ID':<10} {'MODE':<12} {'SCORE':>6} {'PASS':>5} {'FAIL':>5} {'ERR':>4}  STARTED")
        for r in runs:
            score = "—" if r["score"] is None else f"{float(r['score']):.0%}"
            print(
                f"{str(r['id'])[:8]:<10} {r['mode']:<12} {score:>6}"
                f" {r['passed']:>5} {r['failed']:>5} {r['errored']:>4}"
                f"  {str(r['started_at'])[:19]}"
            )
        return 0

    if args.other_url and not args.other_token:
        print("error: --other-url needs --other-token (its own admin token)", file=sys.stderr)
        return 1
    # An environment is a server+token, and the cheap shape is org-per-env on
    # ONE server — so --other-token alone (same URL, another org) is a valid
    # side B. Keying the "other side" off the URL alone silently reused side
    # A's runs for it; found dogfooding the first probe.
    other_url = args.other_url.rstrip("/") if args.other_url else url
    other_headers = {"Authorization": f"Bearer {args.other_token}"} if args.other_token else headers
    runs_b = (
        runs
        if (other_url, other_headers) == (url, headers)
        else get(other_url, other_headers, f"/mgmt/lenses/{args.lens}/evals/runs")
    )

    def resolve(spec: str, pool: list[dict[str, Any]], side: str) -> dict[str, Any]:
        # The endpoint returns newest first. `latest`/`prev` stay within one
        # MODE: the pool mixes `dst test` runs with the apply gate's
        # `regression` runs, and a `prev` that silently crossed modes shifted
        # the baseline mid-probe — the compare must be like against like.
        if spec == "latest":
            picked = pool[:1]
        elif spec == "prev":
            picked = [r for r in pool[1:] if not pool or r["mode"] == pool[0]["mode"]][:1]
        else:
            picked = [r for r in pool if str(r["id"]).startswith(spec)]
        if len(picked) != 1:
            what = "no run matches" if not picked else f"{len(picked)} runs match"
            print(f"error: {side}: {what} {spec!r}", file=sys.stderr)
            raise SystemExit(1)
        return picked[0]

    a = resolve(args.diff[0], runs, "A")
    b = resolve(args.diff[1], runs_b, "B")
    res_a = get(url, headers, f"/mgmt/lenses/{args.lens}/evals/runs/{a['id']}/results")
    res_b = get(other_url, other_headers, f"/mgmt/lenses/{args.lens}/evals/runs/{b['id']}/results")
    # Question is the join key: case ids differ across environments, the
    # question asked is what both sides share.
    pa = {r["question"]: r for r in res_a}
    pb = {r["question"]: r for r in res_b}
    newly_failing = sorted(q for q in pb if not pb[q]["passed"] and pa.get(q, {}).get("passed"))
    newly_passing = sorted(q for q in pb if pb[q]["passed"] and q in pa and not pa[q]["passed"])
    still_failing = sorted(q for q in pb if not pb[q]["passed"] and q in pa and not pa[q]["passed"])
    only_a = sorted(set(pa) - set(pb))
    only_b = sorted(set(pb) - set(pa))
    regressed = (
        a["score"] is not None
        and b["score"] is not None
        and float(b["score"]) < float(a["score"]) - REGRESSION_EPS
    )
    if args.as_json:
        print(
            json.dumps(
                {
                    "a": a,
                    "b": b,
                    "regressed": regressed,
                    "newly_failing": newly_failing,
                    "newly_passing": newly_passing,
                    "still_failing": still_failing,
                    "only_in_a": only_a,
                    "only_in_b": only_b,
                },
                indent=2,
            )
        )
        return 1 if regressed or newly_failing else 0

    def score_of(r: dict[str, Any]) -> str:
        return "—" if r["score"] is None else f"{float(r['score']):.0%}"

    print(f"A {str(a['id'])[:8]} {a['mode']} {score_of(a)} ({a['passed']}p {a['failed']}f)")
    print(f"B {str(b['id'])[:8]} {b['mode']} {score_of(b)} ({b['passed']}p {b['failed']}f)")
    for q in newly_failing:
        why = pb[q].get("reason") or ""
        print(style.bad(f"  newly failing: {q}") + (f" — {why}" if why else ""))
    for q in newly_passing:
        print(style.good(f"  newly passing: {q}"))
    for q in still_failing:
        print(style.dim(f"  still failing: {q}"))
    for q in only_a:
        print(style.dim(f"  only in A: {q}"))
    for q in only_b:
        print(style.dim(f"  only in B: {q}"))
    if regressed or newly_failing:
        print(style.bad("regressed") + " — score dropped or a case flipped to failing")
        return 1
    print(style.good("no regression"))
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "runs",
        help="eval-run history for a lens (what dst test records): list newest "
        "first, or `--diff A B` to compare two runs — score delta + per-case "
        "flips; exit 1 when B regresses A (score drop beyond the publish "
        "gate's epsilon, or any case newly failing)",
    )
    p.add_argument("lens", nargs="?", help="lens whose runs to read (or --all)")
    p.add_argument(
        "--all",
        action="store_true",
        help="every lens on the server: each one's recorded runs, newest first",
    )
    p.add_argument(
        "--last",
        type=int,
        default=5,
        metavar="N",
        help="with --all: runs per lens (default 5)",
    )
    p.add_argument(
        "--diff",
        nargs=2,
        metavar=("A", "B"),
        help="two runs to compare: a run id (unique prefix is enough), or latest | prev",
    )
    p.add_argument(
        "--env",
        help="target a named environment minted by `dst env new` — resolves "
        "its recorded admin token from .dst/envs.json (mutually exclusive "
        "with --token)",
    )
    p.add_argument(
        "--other-url",
        help="resolve run B on another server — the cross-environment compare "
        "(same suite, sandbox vs production)",
    )
    p.add_argument(
        "--other-token",
        help="admin token for side B; alone (no --other-url) it means another "
        "org on the SAME server — the org-per-env shape `dst env new` mints",
    )
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.add_argument("--dir", default=".", help="project root (default: .)")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--json", dest="as_json", action="store_true", help="parseable form")
    p.set_defaults(fn=_runs)
