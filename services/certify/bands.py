"""Serve bands fitted for one embedding model, from labelled paraphrase pairs.

Certified matching compares a question's embedding with each approved
question's. Three similarity bands decide what a hit does: at or above
``exact`` the approved answer serves outright; from ``equiv`` up a decider
checks that the question is a paraphrase first; from ``assist`` up the answer
is shown to generation as an example. The bands are a property of the
embedding model, since models spread similarities differently.

``dst calibrate-bands`` fits them from the project's certified questions and
a file of labelled pairs the user writes (dst does not generate paraphrases):
each pair is a question someone might ask, the certified question it names,
and whether that answer should serve it. The fit is stored per model and read
by every serve on an install whose embedder is that model.

Fitted so that no pair labelled ``serves: false`` reaches ``exact`` (a false
serve is the one outcome the band exists to prevent), ``equiv`` lets nearly
every true paraphrase reach the decider, and ``assist`` keeps every true
paraphrase as an example. Pairs a deterministic veto already decides at serve
time (a different period, a breakdown the approved SQL does not group by, a
different unit) are left out of the fit: the veto rejects them before any band
is read.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from services.benchmark.contamination import normalize

#: Fewest usable pairs of each label a fit needs: a band fitted on fewer
#: moves with every pair added, and the exact band rests on the highest
#: negative alone.
MIN_SERVES = 20
MIN_NOT = 20
_MARGIN = 0.005  # exact sits this far above the highest pair that must not serve
_CAP = 0.995


@dataclass(frozen=True)
class Bands:
    exact: float
    equiv: float
    assist: float


@dataclass(frozen=True)
class LabelledPair:
    asked: str
    certified: str
    serves: bool


@dataclass
class Fit:
    """What a fit measured, whether or not it produced bands."""

    model: str
    serves: list[float]
    not_serves: list[float]
    vetoed: int
    bands: Bands | None = None
    refused: str | None = None

    def lines(self) -> list[str]:
        out = [
            f"embedder {self.model}: {len(self.serves)} pair(s) that should serve, "
            f"{len(self.not_serves)} that should not"
            + (f", {self.vetoed} decided by a serve-time veto and left out" if self.vetoed else "")
        ]
        if self.bands is None:
            return out
        b = self.bands
        outright = sum(s >= b.exact for s in self.serves)
        checked = sum(b.equiv <= s < b.exact for s in self.serves)
        example = sum(b.assist <= s < b.equiv for s in self.serves)
        missed = len(self.serves) - outright - checked - example
        wrong = sum(s >= b.equiv for s in self.not_serves)
        out += [
            f"exact {b.exact}: serves {outright} of {len(self.serves)} paraphrases outright, "
            f"none of the {len(self.not_serves)} that should not serve",
            f"equivalent {b.equiv}: {checked} more go to the paraphrase check, along with "
            f"{wrong} that should not serve (the check decides those)",
            f"assist {b.assist}: {example} more are shown to generation as examples"
            + (f"; {missed} fall below every band" if missed else ""),
        ]
        return out


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _floor(x: float) -> float:
    return math.floor(x * 1000) / 1000


def _ceil(x: float) -> float:
    return math.ceil(x * 1000) / 1000


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.floor(q * (len(ordered) - 1))))]


def parse_pairs(doc: Any) -> list[LabelledPair]:
    """The pairs file: ``pairs:`` a list of ``{asked, certified, serves}``.
    ValueError naming the entry that is malformed."""
    items = doc.get("pairs") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise ValueError("the pairs file needs a top-level `pairs:` list")
    out: list[LabelledPair] = []
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"pair {i}: expected a mapping with asked, certified, serves")
        asked, certified, serves = item.get("asked"), item.get("certified"), item.get("serves")
        if not isinstance(asked, str) or not asked.strip():
            raise ValueError(f"pair {i}: `asked` must be a question")
        if not isinstance(certified, str) or not certified.strip():
            raise ValueError(f"pair {i}: `certified` must be a certified question")
        if not isinstance(serves, bool):
            raise ValueError(f"pair {i}: `serves` must be true or false")
        out.append(LabelledPair(asked.strip(), certified.strip(), serves))
    return out


def fit(
    model: str,
    corpus: dict[str, str],
    pairs: list[LabelledPair],
    embed: Any,
) -> Fit:
    """Fit bands for *model*. ``corpus``: certified question -> its SQL
    (plain answers only; a template never serves on similarity alone).
    ``embed``: texts -> vectors, the configured embedder's. A pair naming a
    question outside the corpus is a ValueError: a typo would otherwise drop
    it silently."""
    from services.certify.lexical import breakdown_mismatch, unit_mismatch
    from services.runtime.timewindow import temporal_mismatch

    by_norm = {normalize(q): q for q in corpus}
    unknown = sorted({p.certified for p in pairs if normalize(p.certified) not in by_norm})
    if unknown:
        shown = "; ".join(unknown[:5]) + ("; …" if len(unknown) > 5 else "")
        raise ValueError(
            f"{len(unknown)} pair(s) name a certified question this project does not "
            f"hold (or holds as a template): {shown}"
        )
    usable: list[tuple[LabelledPair, str]] = []
    vetoed = 0
    for p in pairs:
        approved = by_norm[normalize(p.certified)]
        sql = corpus[approved]
        if (
            temporal_mismatch(p.asked, approved)
            or breakdown_mismatch(p.asked, approved, sql) is not None
            or unit_mismatch(p.asked, approved, sql) is not None
        ):
            vetoed += 1
            continue
        usable.append((p, approved))
    texts = sorted({t for p, a in usable for t in (p.asked, a)})
    vectors = dict(zip(texts, embed(texts), strict=True)) if texts else {}
    serves = [_cosine(vectors[p.asked], vectors[a]) for p, a in usable if p.serves]
    not_serves = [_cosine(vectors[p.asked], vectors[a]) for p, a in usable if not p.serves]
    result = Fit(model, serves, not_serves, vetoed)
    short = []
    if len(serves) < MIN_SERVES:
        short.append(f"{MIN_SERVES - len(serves)} more that should serve (have {len(serves)})")
    if len(not_serves) < MIN_NOT:
        short.append(f"{MIN_NOT - len(not_serves)} more that should not (have {len(not_serves)})")
    if short:
        result.refused = (
            f"too few labelled pairs to fit bands: at least {MIN_SERVES} that should serve "
            f"and {MIN_NOT} that should not are needed, after the ones a serve-time veto "
            f"decides; add {' and '.join(short)}"
        )
        return result
    exact = min(_CAP, _ceil(max(not_serves) + _MARGIN))
    equiv = min(exact, _floor(_quantile(serves, 0.05)))
    assist = min(equiv, _floor(min(serves)))
    result.bands = Bands(exact=exact, equiv=equiv, assist=assist)
    return result


# ── storage: one row per embedding model, install-wide ──────────────────────

_TTL_S = 60.0
_CACHE: dict[str, tuple[float, Bands | None]] = {}


def store(model: str, result: Fit) -> None:
    """Write *result*'s bands for *model*, replacing any earlier fit. Runs on
    the admin engine: bands are install-level, like the embedder itself."""
    from services.db.session import admin_engine

    assert result.bands is not None
    b = result.bands
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO serve_band (model, exact, equiv, assist, serves, not_serves) "
                "VALUES (:m, :e, :q, :a, :s, :n) "
                "ON CONFLICT (model) DO UPDATE SET exact = EXCLUDED.exact, "
                "equiv = EXCLUDED.equiv, assist = EXCLUDED.assist, serves = EXCLUDED.serves, "
                "not_serves = EXCLUDED.not_serves, fitted_at = now()"
            ),
            {
                "m": model,
                "e": b.exact,
                "q": b.equiv,
                "a": b.assist,
                "s": len(result.serves),
                "n": len(result.not_serves),
            },
        )
    _CACHE.pop(model, None)


def stored(model: str) -> Bands | None:
    """The fitted bands for *model*, or None. Cached for a minute per
    process, so a fit reaches a running server within that; an unreachable
    database reads as no fit (the presets and defaults still serve)."""
    now = time.monotonic()
    hit = _CACHE.get(model)
    if hit is not None and now - hit[0] < _TTL_S:
        return hit[1]
    bands: Bands | None = None
    try:
        from services.db.session import engine

        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT exact, equiv, assist FROM serve_band WHERE model = :m"),
                {"m": model},
            ).first()
        if row is not None:
            bands = Bands(exact=float(row[0]), equiv=float(row[1]), assist=float(row[2]))
    except Exception:  # noqa: BLE001 — no table, no database: no fit
        bands = None
    _CACHE[model] = (now, bands)
    return bands


def reset_cache() -> None:
    _CACHE.clear()
