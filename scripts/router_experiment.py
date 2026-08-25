"""Router-signal experiment: does a real decider beat cosine floor+margin?

The router has ONE signal — max cosine of the question against a lens's anchor
strings — and the floor+margin is a band-aid for embedding compression that cannot tell
"two lenses genuinely cover this" from "uncovered noise scores similar against several".
This harness measures whether a stronger decider fixes that, on a held-out,
contamination-guarded labeled set, with REAL Voyage embeddings and REAL DeepSeek, reusing
the RoutingMetrics/evaluate machinery so the numbers are comparable to the eval.

Methods compared (each exposes .route(q) -> RouteDecision so evaluate() scores them all):
  cosine  — services.router.Router with VoyageEmbedder + SHIPPED thresholds (the baseline).
  llm     — DeepSeek decides covered/which/none over the candidate lenses' coverage.
  hybrid  — trust cosine on a CONFIDENT single match (fast, no LLM); else ask the LLM
            (the uncertain region — exactly where cosine is weak). Counts LLM calls.

Run: uv run python scripts/router_experiment.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Allow `uv run python scripts/router_experiment.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.config import settings  # noqa: E402
from services.context.embedder import VoyageEmbedder  # noqa: E402
from services.contracts.protocols import CacheableBlock, Message  # noqa: E402
from services.llm import resolve_provider  # noqa: E402
from services.router import DEFAULT_CONFIDENT, CoverageProfile, RouteDecision, Router  # noqa: E402
from services.router.eval import assert_eval_held_out, evaluate  # noqa: E402


class PrecomputedEmbedder:
    """Serve REAL Voyage vectors from a one-shot batch + disk cache (the free tier is
    3 RPM, but the Router embeds one question per route() call). Same embeddings as
    production — fetched in a single request, persisted so reruns never re-hit Voyage."""

    # voyage-3.5's native dim (VoyageEmbedder.dim went instance-level in the
    # embedder ladder; the cache is recorded at the default 1024 regardless).
    dim = 1024
    _CACHE = Path("/tmp/router_experiment_voyage.json")

    def __init__(self, texts: list[str]) -> None:
        import json

        uniq = sorted(set(texts))
        cache: dict[str, list[float]] = {}
        if self._CACHE.exists():
            cache = json.loads(self._CACHE.read_text())
        missing = [t for t in uniq if t not in cache]
        if missing:
            voyage = VoyageEmbedder(settings.voyage_api_key)
            for t, v in zip(missing, voyage.embed(missing), strict=True):
                cache[t] = v
            self._CACHE.write_text(json.dumps(cache))
        self._vecs = {t: cache[t] for t in uniq}

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vecs[t] for t in texts]


# --------------------------------------------------------------------------- #
# Lens catalogue (the routing-eval fixture lenses — realistic governed-metric anchors)
# --------------------------------------------------------------------------- #
PROFILES = [
    CoverageProfile(
        lens="finance",
        anchors=[
            "total net invoiced revenue",
            "outstanding receivables",
            "overdue invoices",
            "accounts receivable aging",
            "payments collected",
        ],
        description="Finance: governed answers over invoices, receivables, payments.",
    ),
    CoverageProfile(
        lens="customer_value",
        anchors=[
            "how many repeat customers",
            "customer lifetime value",
            "average order value",
            "number of orders per customer",
            "returning vs new customers",
        ],
        description="Customer Value: lifetime value and order activity.",
    ),
    CoverageProfile(
        lens="sales",
        anchors=[
            "quote win rate",
            "which rep won the most deals",
            "pipeline coverage",
            "deals closed this quarter",
            "average deal size",
            "sales cycle length",
        ],
        description="Sales: deals, quotes, pipeline.",
    ),
    CoverageProfile(
        lens="marketing",
        anchors=[
            "ad spend by channel",
            "customer acquisition cost",
            "campaign click-through rate",
            "leads generated",
            "return on ad spend",
        ],
        description="Marketing: campaigns, channels, spend, lead generation.",
    ),
    CoverageProfile(
        lens="web_product",
        anchors=[
            "weekly active users",
            "30-day retention rate",
            "new signups",
            "onboarding funnel drop-off",
            "sessions per user",
            "DAU to MAU ratio",
        ],
        description="Web Product: product usage, activation, retention.",
    ),
    CoverageProfile(
        lens="inventory_ops",
        anchors=[
            "products low on stock",
            "average fulfillment time",
            "backordered orders",
            "average shipping time",
            "inventory turnover",
        ],
        description="Inventory & Ops: stock levels, fulfillment, shipping.",
    ),
]

# --------------------------------------------------------------------------- #
# Held-out labeled set: PARAPHRASES (never verbatim anchors — that would measure
# lookup, not routing) + uncovered noise (None ⇒ the right outcome is a decline).
# This is the hard test the 10-question fixture skips: cosine aces verbatim anchors
# but the real failure is paraphrase recall + noise rejection.
# --------------------------------------------------------------------------- #
LABELED: list[tuple[str, str | None]] = [
    # finance — paraphrased, not verbatim
    ("how much have we billed customers in total", "finance"),
    ("how much money do customers still owe us", "finance"),
    ("which bills are past their due date", "finance"),
    ("show the aging breakdown of unpaid invoices", "finance"),
    ("what is our current AR balance", "finance"),
    ("how much did we collect in payments last month", "finance"),
    # customer_value — paraphrased
    ("how many customers buy from us more than once", "customer_value"),
    ("what is a typical customer worth over their lifetime", "customer_value"),
    ("what's the average spend per order", "customer_value"),
    ("how many times does each customer order on average", "customer_value"),
    ("who are our most valuable long-term customers", "customer_value"),
    ("what share of buyers are returning versus brand new", "customer_value"),
    # sales — paraphrased
    ("what fraction of our quotes do we win", "sales"),
    ("which salesperson closed the most business", "sales"),
    ("do we have enough pipeline to hit the target", "sales"),
    ("how many deals did we close last quarter", "sales"),
    ("what's our typical deal size this year", "sales"),
    ("how long does a deal take to close on average", "sales"),
    # marketing — paraphrased
    ("what did we spend on ads last month", "marketing"),
    ("which channel brings us the cheapest leads", "marketing"),
    ("what's our cost to acquire a customer", "marketing"),
    ("which campaign had the best click-through rate", "marketing"),
    ("how many leads did the webinar bring in", "marketing"),
    ("what's our return on advertising spend", "marketing"),
    # web_product — paraphrased
    ("how many active users did we have last week", "web_product"),
    ("what's our 30-day retention rate", "web_product"),
    ("how many people signed up yesterday", "web_product"),
    ("where do users drop off during onboarding", "web_product"),
    ("how many sessions does each user have on average", "web_product"),
    ("what's our daily-to-monthly active ratio", "web_product"),
    # inventory_ops — paraphrased
    ("which products are running low on stock", "inventory_ops"),
    ("how long does it take us to fulfill an order", "inventory_ops"),
    ("how many orders are on backorder", "inventory_ops"),
    ("what's our average time to ship", "inventory_ops"),
    ("how fast is our inventory turning over", "inventory_ops"),
    ("which warehouse is holding the most stock", "inventory_ops"),
    # uncovered — no lens governs these; correct outcome is a DECLINE
    ("how many support tickets are open this week", None),
    ("what is the weather tomorrow", None),
    ("what is the headcount in HR", None),
    ("what is our office wifi password", None),
    ("how many employees took sick leave last month", None),
    ("how many days of PTO do I have left", None),
    ("what is the server uptime this month", None),
    ("how many candidates applied for the engineering role", None),
    ("what's on the cafeteria menu today", None),
    ("when does our office lease expire", None),
    ("what's our Glassdoor rating", None),
    ("how many security incidents were reported", None),
    ("who is on call this weekend", None),
    ("how many website visitors did we get yesterday", None),
    # adversarial: keyword overlaps a lens but no lens actually governs it (must DECLINE)
    ("how many laptops are in inventory for new hires", None),  # 'inventory' ≠ product stock
    ("what's the marketing team's headcount", None),  # 'marketing' but it's an HR question
    ("which vendor invoices is legal still reviewing", None),  # 'invoices' but a legal/ops Q
]

LENS_NAMES = {p.lens for p in PROFILES}
MODEL = "deepseek-v4-flash"

# --------------------------------------------------------------------------- #
# LLM decider
# --------------------------------------------------------------------------- #
_SYSTEM = (
    "You route a data question to the ONE governed lens that actually covers it, or "
    "decline if none does. A lens covers a question only when its governed metrics include "
    "what is asked. Be strict: if no lens governs the subject, answer none — never stretch a "
    "lens to fit. Reply with ONLY a lens name or the word none."
)


def _catalogue() -> str:
    lines = []
    for p in PROFILES:
        lines.append(f"- {p.lens}: governed metrics: {'; '.join(p.anchors)}. ({p.description})")
    return "\n".join(lines)


class LLMRouter:
    """DeepSeek decides covered/which/none over the lens catalogue. score is cosmetic
    (1.0 route / 0.0 decline) — only .covered/.lens feed evaluate()."""

    def __init__(self) -> None:
        self._provider = resolve_provider(MODEL)
        if self._provider is None:
            raise SystemExit("DEEPSEEK key missing — cannot run the LLM method.")
        self._cat = _catalogue()
        self.calls = 0

    def decide(self, question: str) -> str | None:
        self.calls += 1
        user = f"Lenses:\n{self._cat}\n\nQuestion: {question}\nAnswer (lens name or none):"
        out = self._provider.complete(
            system=[CacheableBlock(text=_SYSTEM)],
            messages=[Message(role="user", content=user)],
            model=MODEL,
            temperature=0.0,
            # deepseek-v4-flash REASONS in output tokens; a tight cap truncates before
            # the visible answer ever lands. Give reasoning room, then the answer.
            max_tokens=512,
        )
        low = out.text.strip().lower()
        # Robust parse: a lens name appears as a substring in any form (customer_value /
        # "customer value" / **customer_value** / "the sales lens"). Longest name first so
        # customer_value wins over a stray "customer".
        for name in sorted(LENS_NAMES, key=len, reverse=True):
            if name in low or name.replace("_", " ") in low:
                return name
        return None

    def route(self, question: str) -> RouteDecision:
        lens = self.decide(question)
        if lens is None:
            return RouteDecision(False, None, 0.0, [], "llm: no lens covers this")
        return RouteDecision(True, lens, 1.0, [], "llm: routed")


class HybridRouter:
    """Trust cosine on a confident single match (no LLM); hand the uncertain region to
    the LLM — the band where cosine's floor+margin is a guess. Counts LLM calls."""

    def __init__(self, cosine: Router, llm: LLMRouter) -> None:
        self._cosine = cosine
        self._llm = llm
        self.calls = 0

    def route(self, question: str) -> RouteDecision:
        d = self._cosine.route(question)
        if d.covered and d.score >= DEFAULT_CONFIDENT:
            return d  # fast path: an unambiguous near-exact match — cosine is right here
        self.calls += 1
        return self._llm.route(question)


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #
def main() -> None:
    assert_eval_held_out(LABELED, PROFILES)  # refuse to score if a label leaks into anchors
    print(
        f"labeled set: {len(LABELED)} questions "
        f"({sum(g is not None for _, g in LABELED)} covered, "
        f"{sum(g is None for _, g in LABELED)} uncovered)\n"
    )

    if not settings.voyage_api_key:
        raise SystemExit("VOYAGE key missing — cannot run the cosine baseline.")
    # One Voyage batch over every anchor + question, then route offline from the cache.
    all_texts = [a for p in PROFILES for a in p.anchors] + [q for q, _ in LABELED]
    cosine = Router(PROFILES, PrecomputedEmbedder(all_texts))

    # The PREFILTER safety check: production hands the LLM only cosine's top-k. If the gold
    # lens isn't in the top-k, the LLM can't recover it — so recall@k bounds the whole system.
    covered = [(q, g) for q, g in LABELED if g is not None]
    print("PREFILTER — cosine recall@k (is the gold lens in cosine's top-k shortlist?)")
    for k in (1, 3, 5):
        hits = sum(1 for q, g in covered if g in [lens for lens, _ in cosine.scored(q)[:k]])
        print(f"  recall@{k}: {hits}/{len(covered)} = {hits / len(covered):.0%}")
    print()
    if "--prefilter-only" in sys.argv:
        return
    llm = LLMRouter()
    hybrid = HybridRouter(cosine, llm)

    def report(name: str, router: object, llm_calls: int | None = None) -> None:
        t0 = time.time()
        m = evaluate(router, LABELED)  # type: ignore[arg-type]
        dt = time.time() - t0
        extra = f"  [LLM calls: {llm_calls}/{len(LABELED)}]" if llm_calls is not None else ""
        print(f"### {name}  ({dt:.1f}s){extra}")
        print(m.render())
        if m.mis_routed or m.routed_uncovered or m.over_declined:
            print("  misses:")
            for q in m.routed_uncovered:
                print(f"    [routed-uncovered] {q!r}  (should have declined)")
            for q in m.mis_routed:
                print(f"    [mis-routed]       {q!r}")
            for q in m.over_declined:
                print(f"    [over-declined]    {q!r}  (a lens covers it)")
        print()

    report("cosine", cosine)
    llm.calls = 0
    report("llm", llm, llm_calls=len(LABELED))  # the LLM method calls once per question
    hybrid.calls = 0
    report("hybrid", hybrid)  # hybrid.calls is known only after evaluate() runs — printed below
    print(
        f"hybrid used the LLM on {hybrid.calls}/{len(LABELED)} questions "
        f"(cosine fast-pathed the rest)"
    )


if __name__ == "__main__":
    main()
