"""ai_call: the long-lived part of a call's prompt-cache writes.

A cache write is billed at one rate for the short-lived cache (Anthropic's
5-minute, OpenAI's 30-minute) and another for the long-lived one (1-hour,
12-hour). ``cache_write_tokens`` stays every token written; this column is
the long-lived part of them, so a row's price can be read back from its
tokens.

Revision ID: 0072
Revises: 0071
Create Date: 2026-10-22
"""

from __future__ import annotations

from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE ai_call ADD COLUMN IF NOT EXISTS "
        "cache_write_long_tokens int NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE ai_call DROP COLUMN IF EXISTS cache_write_long_tokens")
