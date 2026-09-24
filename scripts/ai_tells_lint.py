"""AI-tells lint: does a piece of public text read like a model wrote it?

Counts the tells that readers, Wikipedia's AI-signs page and the excess-vocabulary studies
agree on, measures sentence-length burstiness, and reports a score per 1,000 words. Used
on fan-out output before anything is pasted anywhere.

    uv run python scripts/ai_tells_lint.py FILE [FILE ...] [--max 6]
"""

from __future__ import annotations

import argparse
import re
import statistics
from pathlib import Path

# Words and phrases whose frequency jumped in model-written text. Each hit is one tell.
VOCAB = [
    r"\bdelve\w*",
    r"\btapestry\b",
    r"\blandscape\b",
    r"\bcrucial\b",
    r"\bpivotal\b",
    r"\bleverag\w+",
    r"\bseamless\w*",
    r"\brobust\b",
    r"\bstreamlin\w+",
    r"\bunlock\w*",
    r"\bgame[- ]chang\w+",
    r"\brevolutioni[sz]\w+",
    r"\bempower\w*",
    r"\bharness\w*",
    r"\bnavigat\w+ the\b",
    r"\bin today'?s\b",
    r"\bfast-paced\b",
    r"\bever-evolving\b",
    r"\bmultifaceted\b",
    r"\bunderscore\w*",
    r"\bshowcas\w+",
    r"\bfoster\w*",
    r"\bvibrant\b",
    r"\btestament to\b",
    r"\ba journey\b",
    r"\bat its core\b",
    r"\bin essence\b",
    r"\bit'?s worth noting\b",
    r"\bit is worth noting\b",
    r"\bin conclusion\b",
    r"\bto summari[sz]e\b",
    r"\blet'?s dive\b",
    r"\bdive into\b",
    r"\bdeep dive\b",
    r"\bin the realm of\b",
    r"\bplays a (crucial|vital|key) role\b",
    r"\bpaves? the way\b",
    r"\bstands? as a\b",
    r"\bserves? as a\b",
    r"\bwhether you'?re\b",
    r"\bnot only .{0,40} but also\b",
    r"\bthe bottom line\b",
    r"\bkey takeaways?\b",
    r"\bfinal thoughts\b",
    r"\bhope this helps\b",
    r"\bcomprehensive\b",
    r"\bholistic\b",
    r"\bsynerg\w+",
    r"\bparadigm\b",
    r"\becosystem\b",
    r"\bcutting[- ]edge\b",
    r"\bstate[- ]of[- ]the[- ]art\b",
    r"\bmeticulous\w*\b",
    r"\bintricate\b",
]
# Structural tells: the contrast reframe, the tricolon of adjectives, the rhetorical opener.
STRUCT = [
    (
        r"\b(it'?s|this is|that'?s) not (about )?[^.,;]{2,40}[,;]? "
        r"(it'?s|this is|that'?s) (about )?",
        "not-X-it's-Y reframe",
    ),
    (r"\b\w+, \w+, and \w+\b", "tricolon"),
    (r"(^|\n)\s*(Ever wondered|Have you ever|What if|Imagine)\b", "rhetorical opener"),
    (r"\bthe (\w+ )?(is|was) (clear|simple|obvious): ", "the-answer-is-clear colon"),
    (r"—", "em-dash"),
]
CLOSER = re.compile(r"(In conclusion|To sum up|In summary|Ultimately|At the end of the day)", re.I)


def sentences(text: str) -> list[str]:
    body = re.sub(r"```.*?```", " ", text, flags=re.S)
    body = re.sub(r"^#.*$", " ", body, flags=re.M)
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    parts = re.split(r"(?<=[.!?])\s+", body)
    return [p.strip() for p in parts if len(p.split()) >= 3]


def lint(path: Path) -> tuple[float, list[str]]:
    text = path.read_text()
    words = max(1, len(text.split()))
    hits: list[str] = []
    for pat in VOCAB:
        for m in re.finditer(pat, text, re.I):
            hits.append(f"vocab: {m.group(0)}")
    for pat, name in STRUCT:
        n = len(re.findall(pat, text, re.I | re.M))
        if name == "em-dash" and n <= words / 500:
            continue  # one per 500 words is a human rate
        if name == "tricolon" and n <= words / 400:
            continue
        hits.extend([f"struct: {name}"] * n)
    if CLOSER.search("\n".join(text.splitlines()[-6:])):
        hits.append("struct: summarising closer")
    sents = sentences(text)
    if len(sents) >= 8:
        lengths = [len(s.split()) for s in sents]
        cv = statistics.pstdev(lengths) / max(1, statistics.mean(lengths))
        if cv < 0.45:
            hits.append(f"rhythm: sentence lengths too even (cv {cv:.2f}, humans run 0.5-0.9)")
    if not re.search(r"\b(I|we|my|our)\b", text):
        hits.append("voice: no first person anywhere")
    if not re.search(r"\d", re.sub(r"```.*?```", "", text, flags=re.S)):
        hits.append("voice: not one number in the prose")
    per_k = 1000 * len(hits) / words
    return per_k, hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--max", type=float, default=6.0, help="tells per 1,000 words allowed")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    worst = 0.0
    for f in args.files:
        per_k, hits = lint(Path(f))
        worst = max(worst, per_k)
        flag = "FAIL" if per_k > args.max else "ok  "
        print(f"{flag} {per_k:5.1f} tells/1k  {f}")
        if not args.quiet:
            for h in sorted(set(hits)):
                print(f"       {h} x{hits.count(h)}")
    return 1 if worst > args.max else 0


if __name__ == "__main__":
    raise SystemExit(main())
