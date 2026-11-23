from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    tg_discussion_chat_id: Mapped[int | None] = mapped_column(
        BigInteger, unique=True, nullable=True
    )
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="active", server_default=sa_text("'active'")
    )  # 'archived' | 'active'
    id_offset: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa_text("0")
    )
    last_crawled_msg_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=sa_text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    posts: Mapped[list[Post]] = relationship(back_populates="channel")


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("channel_id", "tg_msg_id", name="uq_posts_channel_tg_msg"),
        UniqueConstraint(
            "channel_id", "tg_discussion_root_id", name="uq_posts_channel_discussion_root"
        ),
        Index("ix_posts_active_id", "is_deleted", "id"),
        Index("ix_posts_channel_id", "channel_id"),
        Index("ix_posts_date", "date"),
        Index("ix_posts_tg_all_msg_ids", "tg_all_msg_ids", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), nullable=False)
    tg_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tg_media_group_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    tg_all_msg_ids: Mapped[list[int] | None] = mapped_column(ARRAY(BigInteger), nullable=True)
    tg_discussion_root_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )

    type: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    web_views: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa_text("0")
    )
    forwards: Mapped[int | None] = mapped_column(Integer, nullable=True)

    reply_to_post_id: Mapped[int | None] = mapped_column(
        ForeignKey("posts.id", ondelete="SET NULL"), nullable=True
    )
    forwarded_from: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    reply_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    quote: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    files: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    extra_meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    show_caption_above_media: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    has_media_spoiler: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )

    channel: Mapped[Channel] = relationship(back_populates="posts")
    images: Mapped[list[PostImage]] = relationship(
        back_populates="post",
        cascade="all, delete-orphan",
        order_by="PostImage.sort_order",
    )
    tg_reactions: Mapped[list[TgReaction]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )
    web_reactions: Mapped[list[WebReaction]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )
    poll: Mapped[Poll | None] = relationship(
        back_populates="post", cascade="all, delete-orphan", uselist=False
    )
    comments: Mapped[list[Comment]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )


class PostImage(Base):
    __tablename__ = "post_images"
    __table_args__ = (Index("ix_post_images_post_sort", "post_id", "sort_order"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa_text("0")
    )
    tg_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    thumb_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    jxl_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumb_jxl_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    original_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    spoiler: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    phash: Mapped[str | None] = mapped_column(Text, nullable=True)
    rgb_sig: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    post: Mapped[Post] = relationship(back_populates="images")


class PostView(Base):
    __tablename__ = "post_views"
    __table_args__ = (Index("ix_post_views_post_viewed_at", "post_id", "viewed_at"),)

    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True
    )
    actor_key: Mapped[str] = mapped_column(Text, primary_key=True)
    viewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TgReaction(Base):
    __tablename__ = "tg_reactions"

    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True
    )
    reaction_key: Mapped[str] = mapped_column(Text, primary_key=True)
    emoji: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_emoji_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa_text("0"))

    post: Mapped[Post] = relationship(back_populates="tg_reactions")


class WebReaction(Base):
    __tablename__ = "web_reactions"
    __table_args__ = (Index("ix_web_reactions_post_id", "post_id"),)

    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True
    )
    reaction_key: Mapped[str] = mapped_column(Text, primary_key=True)
    emoji: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_emoji_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_key: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    post: Mapped[Post] = relationship(back_populates="web_reactions")


class Poll(Base):
    __tablename__ = "polls"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    is_closed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    is_anonymous: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    type: Mapped[str] = mapped_column(
        Text, nullable=False, default="REGULAR", server_default=sa_text("'REGULAR'")
    )
    allow_multiple_answers: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    correct_option_idx: Mapped[int | None] = mapped_column(Integer, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)

    post: Mapped[Post] = relationship(back_populates="poll")
    options: Mapped[list[PollOption]] = relationship(
        back_populates="poll",
        cascade="all, delete-orphan",
        order_by="PollOption.option_idx",
    )
    web_votes: Mapped[list[WebPollVote]] = relationship(
        back_populates="poll", cascade="all, delete-orphan"
    )


class PollOption(Base):
    __tablename__ = "poll_options"

    poll_id: Mapped[str] = mapped_column(
        ForeignKey("polls.id", ondelete="CASCADE"), primary_key=True
    )
    option_idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[str] = mapped_column(Text, nullable=False)
    tg_voter_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa_text("0")
    )

    poll: Mapped[Poll] = relationship(back_populates="options")


class WebPollVote(Base):
    __tablename__ = "web_poll_votes"
    __table_args__ = (Index("ix_web_poll_votes_poll_actor", "poll_id", "actor_key"),)

    poll_id: Mapped[str] = mapped_column(
        ForeignKey("polls.id", ondelete="CASCADE"), primary_key=True
    )
    option_idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_key: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    poll: Mapped[Poll] = relationship(back_populates="web_votes")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role: Mapped[str] = mapped_column(
        Text, nullable=False, default="user", server_default=sa_text("'user'")
    )  # 'owner' | 'user'
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa_text("false")
    )
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    notify_replies: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa_text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    identities: Mapped[list[UserIdentity]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        order_by="UserIdentity.provider",
    )
    sessions: Mapped[list[Session]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserIdentity(Base):
    __tablename__ = "user_identities"
    __table_args__ = (Index("ix_user_identities_user_id", "user_id"),)

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    provider_user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    username: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(back_populates="identities")


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_user_id", "user_id"),
        Index("ix_sessions_expires_at", "expires_at"),
    )

    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped[User] = relationship(back_populates="sessions")


class MagicLink(Base):
    __tablename__ = "magic_links"
    __table_args__ = (Index("ix_magic_links_email", "email"),)

    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = (
        UniqueConstraint("channel_id", "tg_msg_id", name="uq_comments_channel_tg_msg"),
        Index("ix_comments_post_created", "post_id", "created_at"),
        Index("ix_comments_parent_id", "parent_id"),
        Index("ix_comments_post_actor_hash", "post_id", "actor_key_hash"),
        Index("ix_comments_moderation_status", "moderation_status", "created_at"),
        Index("ix_comments_channel_media_group", "channel_id", "tg_media_group_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("comments.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)  # 'web' | 'telegram'
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), nullable=True)
    tg_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tg_media_group_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    anon_tag: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_key_hash: Mapped[str | None] = mapped_column(Text, nullable=True)

    tg_author_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tg_author_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    tg_author_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    tg_author_avatar: Mapped[str | None] = mapped_column(Text, nullable=True)

    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str] = mapped_column(Text, nullable=False)
    media: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    moderation_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="approved", server_default=sa_text("'approved'")
    )  # 'approved' | 'pending' | 'rejected'
    moderation_meta: Mapped[Any | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    post: Mapped[Post] = relationship(back_populates="comments")
    tg_reactions: Mapped[list[CommentTgReaction]] = relationship(
        back_populates="comment", cascade="all, delete-orphan"
    )
    web_reactions: Mapped[list[CommentWebReaction]] = relationship(
        back_populates="comment", cascade="all, delete-orphan"
    )


class CommentTgReaction(Base):
    __tablename__ = "comment_tg_reactions"

    comment_id: Mapped[int] = mapped_column(
        ForeignKey("comments.id", ondelete="CASCADE"), primary_key=True
    )
    reaction_key: Mapped[str] = mapped_column(Text, primary_key=True)
    emoji: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_emoji_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa_text("0")
    )

    comment: Mapped[Comment] = relationship(back_populates="tg_reactions")


class CommentWebReaction(Base):
    __tablename__ = "comment_web_reactions"
    __table_args__ = (Index("ix_comment_web_reactions_comment_id", "comment_id"),)

    comment_id: Mapped[int] = mapped_column(
        ForeignKey("comments.id", ondelete="CASCADE"), primary_key=True
    )
    reaction_key: Mapped[str] = mapped_column(Text, primary_key=True)
    emoji: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_emoji_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_key: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    comment: Mapped[Comment] = relationship(back_populates="web_reactions")


class NotificationQueue(Base):
    __tablename__ = "notification_queue"
    __table_args__ = (
        Index("ix_notification_queue_pending", "sent_at", "send_after"),
        Index("ix_notification_queue_recipient", "recipient_user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recipient_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    comment_id: Mapped[int] = mapped_column(
        ForeignKey("comments.id", ondelete="CASCADE"), nullable=False
    )
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False
    )
    send_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa_text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class OriginalUploadToken(Base):
    __tablename__ = "original_upload_tokens"
    __table_args__ = (Index("ix_original_upload_tokens_post_id", "post_id"),)

    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False
    )
    dm_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UsedCaptchaSalt(Base):
    __tablename__ = "used_captcha_salts"
    __table_args__ = (Index("ix_used_captcha_salts_expires_at", "expires_at"),)

    salt: Mapped[str] = mapped_column(Text, primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SiteSetting(Base):
    __tablename__ = "site_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ModerationWhitelist(Base):
    __tablename__ = "moderation_whitelist"
    __table_args__ = (
        UniqueConstraint("kind", "key", name="uq_moderation_whitelist_kind_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # 'user' | 'actor'
    key: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class AvatarCustomEmoji(Base):
    __tablename__ = "avatar_custom_emojis"

    source_key: Mapped[str] = mapped_column(Text, primary_key=True)
    custom_emoji_id: Mapped[str] = mapped_column(Text, nullable=False)
    pack_name: Mapped[str] = mapped_column(Text, nullable=False)
    fallback_emoji: Mapped[str] = mapped_column(
        Text, nullable=False, default="👤", server_default=sa_text("'👤'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
