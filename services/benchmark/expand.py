"""Expand the question set mechanically from the oracle's group-by maps.

Every entry in a per-entity oracle map (overdue by customer, won value by rep,
invoiced by year/service, AR by bucket) is a parameterized question instance
with a guaranteed-resolvable gold answer. This is the cheap path from 14
hand-written questions to ~100 instances — the statistical floor moves from
±19pp to ~±7pp on a lane accuracy without any new oracle work.

Templates here must stay *mechanical* (entity name substitution only); the
hand-authored set owns nuance, traps, and Finnish phrasing.
"""

from __future__ import annotations

from .questions import Question

# (oracle map key, question template, category, tier)
_MAP_TEMPLATES: list[tuple[str, str, str, str]] = [
    (
        "overdue_by_customer",
        "How much overdue receivable does the customer named '{name}' have, in euros?",
        "entity-overdue",
        "discriminating",  # 'overdue' is definitional; raw context invites bucket subsetting
    ),
    (
        "outstanding_by_customer",
        "What is the total outstanding (unpaid) amount on invoices for the customer "
        "named '{name}', in euros?",
        "entity-outstanding",
        "discriminating",  # v2: requires latest-snapshot selection
    ),
    (
        "won_value_by_rep",
        "What is the total contract value of deals won by the sales rep named '{name}', in euros?",
        "entity-rep-won",
        "discriminating",  # gold leaderboard diverges (excludes Renewals); 'Won' filter trap
    ),
    (
        "payout_by_rep",
        "How much total sales compensation has been paid to the sales rep "
        "named '{name}', in euros?",
        "entity-rep-payout",
        "calibration",
    ),
    (
        "invoiced_net_by_year",
        "What was our total net invoiced amount in euros for {name}, as one number "
        "(use the invoice issue date)?",
        "time-slicing",
        "discriminating",  # era fork in bronze; deprecated gold mart for 2026
    ),
    (
        "invoiced_net_by_service",
        "How much have we invoiced in total (net, euros) for {name} services?",
        "service-slicing",
        "discriminating",  # v2: silver merge gap — totals require the bronze union
    ),
    (
        "ar_by_bucket",
        "How much outstanding receivable sits in the '{name}' aging bucket, in euros?",
        "bucket-lookup",
        "calibration",
    ),
    (
        "overdue_as_of",
        "What was our total overdue receivable as of {name}, in euros?",
        "point-in-time",
        "discriminating",  # snapshot selection + the overdue definition
    ),
    (
        "invoiced_net_by_month",
        "What was our revenue in {name}, in euros?",
        "business-language",
        "discriminating",  # 'revenue' unqualified = finance's — the mart war
    ),
]


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in value.lower()).strip("-")


def expand_from_oracle(oracle: dict[str, object], *, per_map_limit: int = 40) -> list[Question]:
    """One scalar question per map entry, deterministic order, ids stable."""
    questions: list[Question] = []
    for map_key, template, category, tier in _MAP_TEMPLATES:
        entries = oracle.get(map_key)
        if not isinstance(entries, dict):
            continue
        for name in sorted(entries)[:per_map_limit]:
            questions.append(
                Question(
                    id=f"x-{_slug(map_key)}-{_slug(name)}",
                    category=category,
                    question=template.format(name=name),
                    oracle_path=[map_key, name],
                    kind="scalar",
                    tier=tier,  # type: ignore[arg-type]
                )
            )
    return questions
