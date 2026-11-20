"""Add post web_views, post_views deduplication table, and comment actor_key_hash.

Revision ID: 0004_views_media_ownership
Revises: 0003_site_settings
Create Date: 2026-11-20 04:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_views_media_ownership"
down_revision: str | Sequence[str] | None = "0003_site_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "posts",
        sa.Column(
            "web_views",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )

    op.create_table(
        "post_views",
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("actor_key", sa.Text(), nullable=False),
        sa.Column(
            "viewed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("post_id", "actor_key"),
    )
    op.create_index(
        "ix_post_views_post_viewed_at",
        "post_views",
        ["post_id", "viewed_at"],
        unique=False,
    )

    op.add_column(
        "comments",
        sa.Column("actor_key_hash", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_comments_post_actor_hash",
        "comments",
        ["post_id", "actor_key_hash"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_comments_post_actor_hash", table_name="comments")
    op.drop_column("comments", "actor_key_hash")

    op.drop_index("ix_post_views_post_viewed_at", table_name="post_views")
    op.drop_table("post_views")

    op.drop_column("posts", "web_views")
