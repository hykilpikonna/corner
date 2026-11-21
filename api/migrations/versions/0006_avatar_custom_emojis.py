"""Add avatar_custom_emojis cache table for bridged Telegram web avatar icons.

Revision ID: 0006_avatar_custom_emojis
Revises: 0005_moderation_whitelist
Create Date: 2026-11-21 04:30:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_avatar_custom_emojis"
down_revision: str | Sequence[str] | None = "0005_moderation_whitelist"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "avatar_custom_emojis",
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("custom_emoji_id", sa.Text(), nullable=False),
        sa.Column("pack_name", sa.Text(), nullable=False),
        sa.Column(
            "fallback_emoji",
            sa.Text(),
            server_default=sa.text("'👤'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("source_key"),
    )


def downgrade() -> None:
    op.drop_table("avatar_custom_emojis")
