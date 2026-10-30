"""The `dst lens` command."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from services.cli._common import (
    _client,
    _table,
)


def _prompt_preview(args: argparse.Namespace) -> int:
    """`dst lens prompt <lens> "<question>"` — what the model actually sees.

    The serialized semantic model had exactly one caller, inside the generation
    call: authors could only learn that a `dimensions:` block never reached the
    prompt by running a 15-25 minute benchmark lap (four agents did, and one
    spent three laps tuning a lens that was below its bare-schema control). This
    prints the assembled prompt and, per authored asset, whether it got there.
    No LLM call, no warehouse call."""
    import httpx

    if not args.question:
        print(
            'error: `dst lens prompt` needs a question — dst lens prompt <lens> "..."',
            file=sys.stderr,
        )
        return 1
    url, headers = _client(args)
    r = httpx.get(
        f"{url}/mgmt/lenses/{args.name}/prompt",
        headers=headers,
        params={"q": args.question},
        timeout=120,
    )
    if r.status_code == 404:
        print(f"no lens '{args.name}'", file=sys.stderr)
        return 1
    r.raise_for_status()
    d = r.json()
    if args.json:
        print(json.dumps(d, indent=2))
        return 0
    print(
        f"lens {d['lens']} · tier {d['generator_tier']} · prompt-set {d['prompt_hash']} "
        "· assembled, no LLM call"
    )
    if d.get("intent_system"):
        # The metric-layer tier answers FIRST; the raw-SQL prompt below is only
        # what an escalation would see. Printing them in the other order would
        # show the author a prompt the model may never be sent.
        print("\n=== first pass: metric-layer prompt ===")
        print(d["intent_system"])
        print("\n=== escalation: raw-SQL prompt ===")
    else:
        print("\n=== system prompt ===")
    print(d["system"])
    print(
        "\n=== context (user turn) === " + " · ".join(f"{k}: {v}" for k, v in d["counts"].items())
    )
    for chunk in d["prose"]:
        head = " ".join(chunk["text"].split())[:160]
        score = f" {chunk['score']:.2f}" if chunk["score"] else ""
        print(f"[{chunk['source']}]{score} ({len(chunk['text'])} chars) {head}")
    assets = d["assets"]
    kinds = dict.fromkeys(a["kind"] for a in assets)
    print("\n=== reaching the model ===")
    for kind in kinds:
        same = [a for a in assets if a["kind"] == kind]
        print(f"{kind:<16} {sum(1 for a in same if a['in_prompt'])}/{len(same)}")
    absent = [a for a in assets if not a["in_prompt"]]
    if absent:
        print("\n=== NOT reaching the model ===")
        for a in absent:
            print(f"{a['kind']:<16} {a['name']} — {a['note']}")
    late = [a for a in assets if a["in_prompt"] and a["note"]]
    if late:
        # Grouped: on a metric-layer lens this is most of the model, and the one
        # line that matters (the lens's own instructions) must not be buried.
        print("\n=== reaching it only if generation escalates to the raw-SQL tier ===")
        for kind in kinds:
            names = [a["name"] for a in late if a["kind"] == kind]
            shown = " · ".join(names) if len(names) <= 3 else ""
            if names:
                print(f"{kind:<16} {len(names):<4} {shown}")
    # The SECOND model call. Everything above decides the SQL; this decides the
    # English. Printing only the first half is how a certified answer came to
    # call a `'-'` "a null or placeholder value" with nothing able to show why.
    if d.get("compose_system"):
        print("\n=== second call: compose prompt (writes the English) ===")
        print(d["compose_system"])
        print(
            "\n=== compose (user turn) === <…> are slots — no generator and no "
            "warehouse ran; every column-bound definition is shown, a real serve "
            "carries the ones its answer's columns bind"
        )
        print(d["compose_user"])
        capable = [a for a in d["compose_assets"] if a["in_prompt"]]
        print("\n=== definitions that can reach the composer ===")
        for a in capable:
            print(f"{a['name']:<24} {a['note']}")
        blind = [a for a in d["compose_assets"] if not a["in_prompt"]]
        if blind:
            print("\n=== NOT reaching the composer ===")
            for a in blind:
                print(f"{a['name']:<24} {a['note']}")
    return 0


def _lens_log(args: argparse.Namespace) -> int:
    """`dst lens log <name>` — the published history as a change log.

    The versions and the diff API existed; reading history still meant
    dashboard archaeology. One table, newest first: version, date, who
    published (0051 actors), and the summary the publish recorded. The
    reviewer question this answers is "what changed on this lens and who did
    it" — in the terminal, where the apply that caused it just ran."""
    import httpx

    url, headers = _client(args)
    r = httpx.get(f"{url}/mgmt/lenses/{args.name}/versions", headers=headers, timeout=60)
    if r.status_code == 404:
        print(f"no lens '{args.name}'", file=sys.stderr)
        return 1
    rows = r.raise_for_status().json()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print(f"lens '{args.name}' has no published versions yet")
        return 0
    print(
        _table(
            ["version", "published", "by", "summary"],
            [
                [
                    f"v{v['version']}",
                    str(v["created_at"])[:10],
                    _actor_word(str(v.get("created_by") or "")),
                    v["summary"] or "—",
                ]
                for v in rows
            ],
        )
    )
    return 0


def _lens_list(args: argparse.Namespace) -> int:
    """`dst lens list` — the deployed lenses, from the SERVER.

    Enumerating lenses used to mean reading plan output or listing lenses/*/ on
    disk — but files and server state can diverge by design (file absence never
    deletes; API/cloud-born lenses are server-only until adopted), and the
    documented drift case had no CLI read path. The `not in files` marker is
    the adoption cue: `dst export --lens <name>` brings it under the files."""
    import httpx

    url, headers = _client(args)
    r = httpx.get(f"{url}/mgmt/lenses", headers=headers, timeout=60)
    lenses = r.raise_for_status().json()
    if args.json:
        print(json.dumps(lenses, indent=2, ensure_ascii=False))
        return 0
    if not lenses:
        print("no lenses on this server — `dst apply` deploys the project's")
        return 0
    print(
        _table(
            ["lens", "status", "shape", "queries", "last queried", "origin"],
            [
                [
                    lens["name"],
                    lens["status"],
                    f"{lens['entity_count']}e/{lens['definition_count']}d",
                    lens["query_count"],
                    str(lens.get("last_queried_at") or "—")[:10],
                    "files"
                    if lens.get("from_files", True)
                    else f"not in files — adopt with `dst export --lens {lens['name']}`",
                ]
                for lens in lenses
            ],
        )
    )
    return 0


def _actor_word(actor: str) -> str:
    """`human:ana@corp` → ana@corp; `token:ci` → token 'ci'; '' → —."""
    if not actor:
        return "—"
    if actor.startswith("human:"):
        return actor.removeprefix("human:")
    if actor.startswith("token:"):
        return f"token '{actor.removeprefix('token:')}'"
    return actor


def _lens(args: argparse.Namespace) -> int:
    """`dst lens rm <name>` — the explicit deletion verb the doctrine
    requires (file absence never deletes; plan only flags server-only lenses).
    Prints the cascade BEFORE acting and asks y/N (`--yes` headless) — the
    semantic-rm guard style, with the blast radius spelled out.
    `dst lens prompt <name> "<question>"` prints what the model would see.
    `dst lens log <name>` prints the published-version history."""
    import httpx

    if args.action == "list":
        return _lens_list(args)
    if not args.name:
        print(f"error: `dst lens {args.action}` needs a lens name", file=sys.stderr)
        return 2
    if args.action == "prompt":
        return _prompt_preview(args)
    if args.action == "log":
        return _lens_log(args)
    url, headers = _client(args)
    counts: dict[str, int] = {}
    for label, path in (
        ("lens versions", f"/mgmt/lenses/{args.name}/versions"),
        ("certified answers", f"/mgmt/lenses/{args.name}/certified"),
        ("eval cases", f"/mgmt/lenses/{args.name}/evals/cases"),
    ):
        r = httpx.get(f"{url}{path}", headers=headers, timeout=60)
        if r.status_code == 404:  # the versions endpoint checks the lens exists
            print(f"no lens '{args.name}'", file=sys.stderr)
            return 1
        counts[label] = len(r.raise_for_status().json())
    cascade = ", ".join(f"{label}: {n}" for label, n in counts.items())
    print(
        f"deleting lens '{args.name}' also deletes — {cascade} "
        "(plus its eval runs and context chunks; "
        "request log and review rulings are kept)"
    )
    if not args.yes:
        if not sys.stdin.isatty():
            print("error: refusing without confirmation — pass --yes", file=sys.stderr)
            return 1
        if input("delete? y/N: ").strip().lower() != "y":
            print("aborted — nothing deleted")
            return 1
    r = httpx.delete(f"{url}/mgmt/lenses/{args.name}", headers=headers, timeout=60)
    if r.status_code == 404:
        print(f"no lens '{args.name}'", file=sys.stderr)
        return 1
    r.raise_for_status()
    print(f"removed lens '{args.name}'")
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "lens",
        help="manage lenses on the server: `list` (deployed lenses, incl. the "
        'server-only ones to adopt), `rm <name>`, `prompt <name> "<question>"` '
        "to see the prompt the model would actually get, or `log <name>` for the "
        "published-version history (who, when, why)",
    )
    p.add_argument("action", choices=["list", "rm", "prompt", "log"])
    p.add_argument("name", nargs="?", help="lens name (not used by `list`)")
    p.add_argument(
        "question",
        nargs="?",
        help="`prompt` only: the question to assemble the generation prompt for",
    )
    p.add_argument("--yes", action="store_true", help="skip the confirmation (headless)")
    p.add_argument(
        "--json",
        action="store_true",
        help="`prompt`: the full preview JSON; `log`: the raw version rows",
    )
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument(
        "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
    )
    p.set_defaults(fn=_lens)
