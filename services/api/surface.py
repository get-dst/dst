"""Surface area = the router-lens's accept-vs-decline rate.

Surface area is the unmeasured half of the north star (answer yield = surface ×
accuracy). Accuracy needs an oracle; surface does not — it is just "could we route
this question to a covering lens, yes or no." Rolled up from the persisted routing
decisions (``routing_decision``, migration 0019) per org over a window:

    surface = routed / asked   (the router-lens's accept-vs-decline rate)

with a trend (this window vs the previous one of equal length). The **declines**
are the actionable signal: they cluster into uncovered-metric groups (a normalized
token-overlap clustering, the no-SQL analogue of the distiller's shape clustering)
so "we missed 40 questions" becomes "all about churn / support tickets" — the gap
candidates a customer resolves by extending or building a lens.

``GET /mgmt/surface`` (admin-authed) returns the rate, the trend, and the clusters.
"""

from __future__ import annotations

import re
from collections import Counter

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from services.api import route_store
from services.api.route_store import RoutingDecision
from services.auth.deps import get_app_session

router = APIRouter(prefix="/mgmt/surface", tags=["surface"])

# Words too generic to name a gap — stripped before clustering so a cluster's label
# is its actual subject ("churn", "tickets"), not "how many what is the".
_STOPWORDS = frozenset(
    """a an the of for to in on at by is are was were be been how what which who whom
    whose when where why do does did our we us my me you your their them they it this
    that these those many much show give list tell about over per and or vs versus
    last this week month quarter year ytd mtd qtd current total number count get""".split()
)
_TOKEN = re.compile(r"[a-z0-9]+")


def _stem(tok: str) -> str:
    """Lightweight stemmer: collapse common plural/tense endings so churn/churned and
    ticket/tickets cluster together. Not linguistically complete — just enough to make
    surface clusters about a *subject*, not a surface form."""
    for suffix in ("ing", "ed", "es", "s"):
        if len(tok) > len(suffix) + 2 and tok.endswith(suffix):
            return tok[: -len(suffix)]
    return tok


class UncoveredCluster(BaseModel):
    """A group of declined questions sharing a subject — one coverage-gap candidate."""

    label: str  # the most distinctive shared term(s) — the gap's name
    count: int
    examples: list[str] = Field(default_factory=list)  # up to 3 verbatim questions


class SurfaceReport(BaseModel):
    """The router-lens's surface area over a window, plus the gap candidates."""

    window_days: int
    asked: int
    routed: int
    declined: int
    # Outage declines (DECIDER_DOWN / ROUTER_DOWN) — counted apart and
    # excluded from asked/declined/surface_area/clusters: a decider outage must
    # read as an outage, never as a coverage collapse.
    degraded_declines: int = 0
    # routed / asked over the window; None when nothing was asked (no rate to report).
    surface_area: float | None = None
    # surface_area minus the prior equal-length window's rate; None when no prior data.
    trend: float | None = None
    uncovered_clusters: list[UncoveredCluster] = Field(default_factory=list)
    recent: list[route_store.RoutingDecision] = Field(default_factory=list)


def _content_tokens(question: str) -> list[str]:
    return [
        _stem(t) for t in _TOKEN.findall(question.lower()) if t not in _STOPWORDS and len(t) > 2
    ]


def _rate(decisions: list[RoutingDecision]) -> float | None:
    if not decisions:
        return None
    routed = sum(1 for d in decisions if d.covered)
    return routed / len(decisions)


def cluster_declines(
    declines: list[RoutingDecision], *, min_count: int = 1
) -> list[UncoveredCluster]:
    """Group declined questions into uncovered-metric candidates by their most-shared
    content term (stopwords stripped, lightly stemmed) — the no-SQL analogue of the
    distiller's shape clustering. Each question joins the cluster of its highest
    document-frequency salient token, so questions about the same subject (churn,
    tickets) land together; clusters are returned largest-first (ties: by label)."""
    # Document frequency over (stemmed) content tokens → the token shared by the MOST
    # declines names the common subject; bucketing on it groups the gap.
    df: Counter[str] = Counter()
    per_q: list[tuple[RoutingDecision, list[str]]] = []
    for d in declines:
        toks = _content_tokens(d.question)
        per_q.append((d, toks))
        df.update(set(toks))

    buckets: dict[str, list[RoutingDecision]] = {}
    for d, toks in per_q:
        if not toks:
            key = "(unclassified)"
        else:
            # the most-shared content token names the common subject; ties → the most
            # SPECIFIC term (longest, then alphabetical) so a singleton gap still gets a
            # meaningful label ("support") over an incidental one ("open").
            top_df = max(df[t] for t in set(toks))
            key = max((t for t in set(toks) if df[t] == top_df), key=lambda t: (len(t), t))
        buckets.setdefault(key, []).append(d)

    clusters = [
        UncoveredCluster(
            label=key,
            count=len(rows),
            examples=[r.question for r in rows[:3]],
        )
        for key, rows in buckets.items()
        if len(rows) >= min_count
    ]
    clusters.sort(key=lambda c: (-c.count, c.label))
    return clusters


def compute_surface(session: Session, *, days: int = 30) -> SurfaceReport:
    """Roll the org's routing decisions over the trailing ``days`` into surface area
    + trend + the uncovered-question clusters."""
    raw_window = route_store.list_window(session, days=days)
    prior = route_store.list_window(session, days=days * 2)
    # Outage declines are availability noise, not surface signal — split them out
    # of every coverage number so a decider outage never reads as the
    # org losing coverage.
    degraded = [d for d in raw_window if d.degraded is not None]
    window = [d for d in raw_window if d.degraded is None]
    # the prior equal-length window = (2×days .. days) ago = the older half of `prior`
    window_ids = {d.id for d in raw_window}
    prior_only = [d for d in prior if d.id not in window_ids and d.degraded is None]

    asked = len(window)
    routed = sum(1 for d in window if d.covered)
    declined = asked - routed
    rate = _rate(window)
    prior_rate = _rate(prior_only)
    trend = (rate - prior_rate) if (rate is not None and prior_rate is not None) else None

    return SurfaceReport(
        window_days=days,
        asked=asked,
        routed=routed,
        declined=declined,
        degraded_declines=len(degraded),
        surface_area=rate,
        trend=trend,
        uncovered_clusters=cluster_declines([d for d in window if not d.covered]),
        recent=route_store.latest(session, 50),
    )


@router.get("")
def surface(
    days: int = Query(default=30, ge=1, le=365),
    session: Session = Depends(get_app_session),
) -> SurfaceReport:
    """The router-lens's surface area for the org: routed / asked over the window, the
    trend vs the prior window, and the uncovered-metric clusters (the gap candidates)."""
    return compute_surface(session, days=days)
