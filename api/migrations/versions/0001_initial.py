"""Initial PostgreSQL schema for corner /life v2.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-19 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. channels
    op.create_table(
        "channels",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tg_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("tg_discussion_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("id_offset", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "last_crawled_msg_id", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tg_chat_id"),
        sa.UniqueConstraint("tg_discussion_chat_id"),
    )

    # 2. posts
    op.create_table(
        "posts",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("tg_msg_id", sa.BigInteger(), nullable=True),
        sa.Column("tg_media_group_id", sa.Text(), nullable=True),
        sa.Column("tg_all_msg_ids", postgresql.ARRAY(sa.BigInteger()), nullable=True),
        sa.Column("tg_discussion_root_id", sa.BigInteger(), nullable=True),
        sa.Column("date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("type", sa.Text(), nullable=True),
        sa.Column("text_html", sa.Text(), nullable=True),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("views", sa.Integer(), nullable=True),
        sa.Column("forwards", sa.Integer(), nullable=True),
        sa.Column("reply_to_post_id", sa.Integer(), nullable=True),
        sa.Column("forwarded_from", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reply_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("quote", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("files", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("extra_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "show_caption_above_media",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "has_media_spoiler", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.ForeignKeyConstraint(["reply_to_post_id"], ["posts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_id", "tg_msg_id", name="uq_posts_channel_tg_msg"),
        sa.UniqueConstraint(
            "channel_id", "tg_discussion_root_id", name="uq_posts_channel_discussion_root"
        ),
    )
    op.create_index("ix_posts_active_id", "posts", ["is_deleted", "id"], unique=False)
    op.create_index("ix_posts_channel_id", "posts", ["channel_id"], unique=False)
    op.create_index("ix_posts_date", "posts", ["date"], unique=False)
    op.create_index(
        "ix_posts_tg_all_msg_ids",
        "posts",
        ["tg_all_msg_ids"],
        unique=False,
        postgresql_using="gin",
    )

    # 3. post_images
    op.create_table(
        "post_images",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("tg_msg_id", sa.BigInteger(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("thumb_url", sa.Text(), nullable=True),
        sa.Column("original_url", sa.Text(), nullable=True),
        sa.Column("original_size", sa.BigInteger(), nullable=True),
        sa.Column("original_name", sa.Text(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("spoiler", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("phash", sa.Text(), nullable=True),
        sa.Column("rgb_sig", sa.LargeBinary(), nullable=True),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_post_images_post_sort", "post_images", ["post_id", "sort_order"], unique=False
    )

    # 4. tg_reactions
    op.create_table(
        "tg_reactions",
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("reaction_key", sa.Text(), nullable=False),
        sa.Column("emoji", sa.Text(), nullable=True),
        sa.Column("custom_emoji_id", sa.Text(), nullable=True),
        sa.Column("count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("post_id", "reaction_key"),
    )

    # 5. web_reactions
    op.create_table(
        "web_reactions",
        sa.Column("post_id", sa.Integer(), nullable=False),
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
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("post_id", "reaction_key", "actor_key"),
    )
    op.create_index("ix_web_reactions_post_id", "web_reactions", ["post_id"], unique=False)

    # 6. polls
    op.create_table(
        "polls",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("is_closed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_anonymous", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("type", sa.Text(), server_default=sa.text("'REGULAR'"), nullable=False),
        sa.Column(
            "allow_multiple_answers",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("correct_option_idx", sa.Integer(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("post_id"),
    )

    # 7. poll_options
    op.create_table(
        "poll_options",
        sa.Column("poll_id", sa.Text(), nullable=False),
        sa.Column("option_idx", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.Column("tg_voter_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(["poll_id"], ["polls.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("poll_id", "option_idx"),
    )

    # 8. web_poll_votes
    op.create_table(
        "web_poll_votes",
        sa.Column("poll_id", sa.Text(), nullable=False),
        sa.Column("option_idx", sa.Integer(), nullable=False),
        sa.Column("actor_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["poll_id"], ["polls.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("poll_id", "option_idx", "actor_key"),
    )
    op.create_index(
        "ix_web_poll_votes_poll_actor", "web_poll_votes", ["poll_id", "actor_key"], unique=False
    )

    # 9. users
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("role", sa.Text(), server_default=sa.text("'user'"), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("email_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("notify_replies", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )

    # 10. user_identities
    op.create_table(
        "user_identities",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_user_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("provider", "provider_user_id"),
    )
    op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"], unique=False)

    # 11. sessions
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"], unique=False)

    # 12. magic_links
    op.create_table(
        "magic_links",
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_magic_links_email", "magic_links", ["email"], unique=False)

    # 13. comments
    op.create_table(
        "comments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=True),
        sa.Column("tg_msg_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("anon_tag", sa.Text(), nullable=True),
        sa.Column("tg_author_id", sa.BigInteger(), nullable=True),
        sa.Column("tg_author_name", sa.Text(), nullable=True),
        sa.Column("tg_author_username", sa.Text(), nullable=True),
        sa.Column("tg_author_avatar", sa.Text(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("media", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.ForeignKeyConstraint(["parent_id"], ["comments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_id", "tg_msg_id", name="uq_comments_channel_tg_msg"),
    )
    op.create_index(
        "ix_comments_post_created", "comments", ["post_id", "created_at"], unique=False
    )
    op.create_index("ix_comments_parent_id", "comments", ["parent_id"], unique=False)

    # 14. notification_queue
    op.create_table(
        "notification_queue",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("recipient_user_id", sa.Integer(), nullable=False),
        sa.Column("comment_id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("send_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["comment_id"], ["comments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_queue_pending",
        "notification_queue",
        ["sent_at", "send_after"],
        unique=False,
    )
    op.create_index(
        "ix_notification_queue_recipient",
        "notification_queue",
        ["recipient_user_id"],
        unique=False,
    )

    # 15. original_upload_tokens
    op.create_table(
        "original_upload_tokens",
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("dm_msg_id", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index(
        "ix_original_upload_tokens_post_id",
        "original_upload_tokens",
        ["post_id"],
        unique=False,
    )

    # 16. used_captcha_salts
    op.create_table(
        "used_captcha_salts",
        sa.Column("salt", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("salt"),
    )
    op.create_index(
        "ix_used_captcha_salts_expires_at", "used_captcha_salts", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_table("used_captcha_salts")
    op.drop_table("original_upload_tokens")
    op.drop_table("notification_queue")
    op.drop_table("comments")
    op.drop_table("magic_links")
    op.drop_table("sessions")
    op.drop_table("user_identities")
    op.drop_table("users")
    op.drop_table("web_poll_votes")
    op.drop_table("poll_options")
    op.drop_table("polls")
    op.drop_table("web_reactions")
    op.drop_table("tg_reactions")
    op.drop_table("post_images")
    op.drop_table("posts")
    op.drop_table("channels")
