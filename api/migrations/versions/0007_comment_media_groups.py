"""Add tg_media_group_id column and index on comments for album grouping.

Revision ID: 0007_comment_media_groups
Revises: 0006_avatar_custom_emojis
Create Date: 2026-11-21 23:20:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_comment_media_groups"
down_revision: str | Sequence[str] | None = "0006_avatar_custom_emojis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("comments", sa.Column("tg_media_group_id", sa.Text(), nullable=True))
    op.create_index(
        "ix_comments_channel_media_group",
        "comments",
        ["channel_id", "tg_media_group_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_comments_channel_media_group", table_name="comments")
    op.drop_column("comments", "tg_media_group_id")
