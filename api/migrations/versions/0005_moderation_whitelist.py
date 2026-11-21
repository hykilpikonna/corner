"""Add comment moderation status/metadata and moderation_whitelist table.

Revision ID: 0005_moderation_whitelist
Revises: 0004_views_media_ownership
Create Date: 2026-11-21 03:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_moderation_whitelist"
down_revision: str | Sequence[str] | None = "0004_views_media_ownership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "comments",
        sa.Column(
            "moderation_status",
            sa.Text(),
            server_default=sa.text("'approved'"),
            nullable=False,
        ),
    )
    op.add_column(
        "comments",
        sa.Column(
            "moderation_meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_comments_moderation_status",
        "comments",
        ["moderation_status", "created_at"],
        unique=False,
    )

    op.create_table(
        "moderation_whitelist",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "key", name="uq_moderation_whitelist_kind_key"),
    )


def downgrade() -> None:
    op.drop_table("moderation_whitelist")
    op.drop_index("ix_comments_moderation_status", table_name="comments")
    op.drop_column("comments", "moderation_meta")
    op.drop_column("comments", "moderation_status")
