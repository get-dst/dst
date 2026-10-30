"""The `dst query` command."""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING, Any

from services.cli import style
from services.cli._common import (
    CLI_AGENT,
    _client,
)

if TYPE_CHECKING:
    from typing import TextIO

# A query that produced no SQL exits NON-ZERO. Exit 0 used to mean "the request
# reached the server", so a parser error, a guard rejection and a provider timeout
# all came back as exit 0 with the reason buried in English in `answer` — the
# silent-empty shape, and unbranchable for the agents this command exists for. The
# scaffold already teaches "check the exit code, not just the diffs"
# (services/cli/init.py:509); now the exit code is worth checking.
#
# Three classes, because collapsing them would cost the distinction the product is
# built on. A DECLINE is not a failure: refusing a question the lens cannot answer,
# and asking which meaning of an ambiguous term you want, are the governed
# behaviors dst exists to provide — an agent that sees 3 should rephrase or ask
# its user, while one that sees 1 should retry or escalate. 2 is argparse's own
# usage-error code and is deliberately skipped.
EXIT_DECLINED = 3
_QUERY_EXIT: dict[str, int] = {
    "ok": 0,
    "refused": EXIT_DECLINED,
    "clarification": EXIT_DECLINED,
    "rejected": 1,
    "error": 1,
}


def _freshness_part(d: dict[str, object]) -> str | None:
    """The measured freshness floor, and loudly when it breaks the lens's declared
    contract. `data_as_of` rode the API response from day one and the terminal
    never printed it — the one field a human can't sanity-check from the answer."""
    as_of = d.get("data_as_of")
    if not as_of:
        return None
    verification = d.get("verification")
    raw_checks = verification.get("checks") if isinstance(verification, dict) else None
    checks = raw_checks if isinstance(raw_checks, list) else []
    stale = any(
        isinstance(c, dict) and c.get("name") == "freshness" and c.get("status") == "fail"
        for c in checks
    )
    if stale:
        return style.warn(f"as of {as_of} — stale")
    return style.accent("as of:") + f" {as_of}"


def _query(args: argparse.Namespace) -> int:
    """Ask a governed question from the terminal — the verify step of the
    authoring loop as one command instead of a source-.env-and-curl incantation
    (in-project agents were literally doing that).

    ``--key dst_…`` asks AS a caller instead of as the admin, which is the only
    way to prove an access grant: an admin token bypasses every allow-list, so
    "granted B, does it work?" and "is C still denied?" both answer 200 under
    the admin door. That gap sent agents back to curl."""
    import httpx

    url, headers = _client(args, caller_key_ok=True)
    r = httpx.post(
        f"{url}/v1/lenses/{args.lens}/query",
        # named, so the request ledger tells a terminal ask from a caller's
        headers={**headers, "X-Dst-Agent": CLI_AGENT},
        json={"q": args.question},
        timeout=args.timeout,
    )
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail") or r.json().get("error", {}).get("message")
        except Exception:  # noqa: BLE001 — non-JSON error bodies
            detail = r.text[:200]
        print(f"error: {detail}", file=sys.stderr)
        return 1
    d = r.json()
    # Unknown status → treat as a failure, never as an answer: a caller reading a
    # newer server must not be told "answered" by an old client's ignorance.
    code = _QUERY_EXIT.get(str(d.get("status") or "ok"), 1)
    if args.json:
        print(json.dumps(d, indent=2))
        return code
    clarification = d.get("clarification")
    if clarification:
        print(f"clarify: {clarification['question']}")
        for option in clarification.get("options", []):
            print(f"  - {option}")
        _print_request_id(d)
        return code
    if code != 0:
        # stdout stays the answer channel and nothing else, so `$(dst query …)`
        # is either an answer or empty — never a sentence about why there isn't one.
        # The request_id rides on stderr with it: a refusal is exactly the kind of
        # response `dst correct` gets filed against.
        print(f"{d.get('status')}: {d.get('answer') or ''}", file=sys.stderr)
        _print_request_id(d, stream=sys.stderr)
        return code
    print(d.get("answer") or "")
    if d.get("sql"):
        print(f"\n{style.accent('sql:')} {style.dim(d['sql'])}")
    if d.get("trust_summary"):
        # The basis, in the author's words — not just an identifier. Two people
        # holding different "revenue" numbers reconcile on this line or not at all:
        # `definition: net_revenue` below names the winner, but only "money
        # actually collected, minus anything refunded" tells a commercial director
        # why his figure differs from his colleague's.
        print(f"{style.accent('basis:')} {d['trust_summary']}")
    # What the answer discloses about itself (an UNTYPED escalation, a degraded
    # lens, a grade resting on partial evidence) is printed verbatim: dropped
    # here, a raw-SQL answer read exactly like a typed one.
    for note in d.get("degraded") or []:
        print(style.warn(str(note)))
    meta = [
        part
        for part in (
            style.accent("confidence:") + f" {_confidence_word(str(d['confidence']))}"
            if d.get("confidence")
            else None,
            style.good(f"certified ({d['certification']})")
            if d.get("certification") not in (None, "none")
            else None,
            style.accent("definition:") + f" {d['definition_used']}"
            if d.get("definition_used")
            else None,
            _resolution_part(d),
            _freshness_part(d),
        )
        if part
    ]
    if meta:
        print(" · ".join(meta))
    _print_request_id(d)
    return code


def _resolution_part(d: dict[str, object]) -> str | None:
    """`resolution: declared` — whether the figure's MEANING was governed.
    Beside `definition:` (the human sentence names the winner) this is the
    typed verdict: declared/certified = the semantic model's metric computed
    it; mixed/inferred = the model summed columns it chose, and the invented
    slots are named so the reader sees what was guessed."""
    res = d.get("resolution")
    if not isinstance(res, dict) or not res.get("tag"):
        return None
    tag = str(res["tag"])
    part = (
        style.accent("resolution:")
        + f" {style.good(tag) if tag in ('certified', 'declared') else tag}"
    )
    if tag in ("certified", "declared"):
        return part
    invented = [
        str(s.get("name"))
        for s in res.get("slots") or []
        if isinstance(s, dict)
        and s.get("source") == "inferred"
        and s.get("kind") in ("metric", "definition")
    ]
    return part + (style.dim(f" (inferred: {', '.join(invented)})") if invented else "")


def _confidence_word(word: str) -> str:
    """The confidence grade, painted semantically — the one line that used to
    look identical at every grade."""
    if word == "verified":
        return style.good(word)
    if word == "partial":
        return style.warn(word)
    if word == "unverified":
        return style.bad(word)
    return word


def _print_request_id(response: dict[str, object], *, stream: TextIO | None = None) -> None:
    """The correction loop's only input, on the human path too. `dst correct`
    takes a request_id and nothing else, and it used to be reachable only through
    `--json` — so the loop's step 3 asked you to re-run step 2 differently.

    Follows the answer onto whichever stream carried it, so the non-zero paths do
    not put a line on stdout that `$(dst query …)` would capture."""
    if response.get("request_id"):
        print(f"request_id: {response['request_id']}", file=stream or sys.stdout)


def register(sub: Any) -> None:
    p = sub.add_parser(
        "query", help="ask a governed question through a lens (the verify one-liner)"
    )
    p.add_argument("lens")
    p.add_argument("question")
    p.add_argument("--json", action="store_true", help="full QueryResponse JSON")
    p.add_argument("--timeout", type=int, default=180)
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
        "--env",
        help="ask through a named environment minted by `dst env new` — resolves "
        "its recorded admin token; an explicit --env outranks an ambient caller "
        "key, so the question lands in THAT env",
    )
    p.add_argument(
        "--key",
        help="ask AS a caller: a dst_ caller key from `dst keys create` (default: "
        "DST_API_KEY from env or ./.env). The admin token bypasses every lens "
        "allow-list, so this is the only way to prove an access grant — or a denial",
    )
    p.set_defaults(fn=_query)
