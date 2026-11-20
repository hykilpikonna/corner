"""Add comment_tg_reactions and comment_web_reactions tables.

Revision ID: 0002_comment_reactions
Revises: 0001_initial
Create Date: 2026-11-19 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_comment_reactions"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "comment_tg_reactions",
        sa.Column("comment_id", sa.Integer(), nullable=False),
        sa.Column("reaction_key", sa.Text(), nullable=False),
        sa.Column("emoji", sa.Text(), nullable=True),
        sa.Column("custom_emoji_id", sa.Text(), nullable=True),
        sa.Column("count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(["comment_id"], ["comments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("comment_id", "reaction_key"),
    )

    op.create_table(
        "comment_web_reactions",
        sa.Column("comment_id", sa.Integer(), nullable=False),
        sa.Column("reaction_key", sa.Text(), nullable=False),
        sa.Column("emoji", sa.Text(), nullable=True),
        sa.Column("custom_emoji_id", sa.Text(), nullable=True),
        sa.Column("actor_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["comment_id"], ["comments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("comment_id", "reaction_key", "actor_key"),
    )
    op.create_index(
        "ix_comment_web_reactions_comment_id",
        "comment_web_reactions",
        ["comment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_comment_web_reactions_comment_id", table_name="comment_web_reactions")
    op.drop_table("comment_web_reactions")
    op.drop_table("comment_tg_reactions")
