"""routing_decision.degraded: an outage decline is not a coverage decline

Revision ID: 0056
Revises: 0055
Create Date: 2026-08-19

When the LLM coverage decider errors or starves, the router
now DECLINES with a degraded marker (DECIDER_DOWN / ROUTER_DOWN) instead of
silently falling back to the mid-band cosine route (the weakest routing arm).
The marker persists here so the surface report can
count outage declines separately — a decider outage must read as an outage,
never as a coverage collapse. NULL = an honest routing decision, the steady
state.
"""

from __future__ import annotations

from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE routing_decision ADD COLUMN degraded text")


def downgrade() -> None:
    op.execute("ALTER TABLE routing_decision DROP COLUMN IF EXISTS degraded")
