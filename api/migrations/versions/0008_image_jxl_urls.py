"""Add jxl_url and thumb_jxl_url columns on post_images for progressive/HDR JPEG XL delivery.

Revision ID: 0008_image_jxl_urls
Revises: 0007_comment_media_groups
Create Date: 2026-11-23 16:40:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_image_jxl_urls"
down_revision: str | Sequence[str] | None = "0007_comment_media_groups"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("post_images", sa.Column("jxl_url", sa.Text(), nullable=True))
    op.add_column("post_images", sa.Column("thumb_jxl_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("post_images", "thumb_jxl_url")
    op.drop_column("post_images", "jxl_url")
