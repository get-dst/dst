"""The router gate's lens catalogue and held-out labelled questions.

Read by tests/test_router_gate.py against the recorded bge-small vectors in
router_experiment_bge.json.gz: every text here must have a recorded vector, so
changing a question or an anchor means re-recording the fixture.
"""

from __future__ import annotations

from services.router import CoverageProfile

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
