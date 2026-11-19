from __future__ import annotations

import html
import inspect
import logging
import re
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import Channel, Comment, Post, User
from app.db.repo import get_post_by_id
from app.routes.upload import create_upload_token

logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MAX_TG_TEXT_LEN = 4096


def _get_disabled_parse_mode() -> Any:
    try:
        from pyrogram.enums import ParseMode

        return ParseMode.DISABLED
    except Exception:
        return None


def _strip_html(raw: str | None, max_len: int = 180) -> str:
    if not raw:
        return ""
    plain = html.unescape(_HTML_TAG_RE.sub(" ", raw))
    normalized = " ".join(plain.split())
    if len(normalized) > max_len:
        return normalized[: max_len - 3].rstrip() + "..."
    return normalized


def _extract_site_host(site_url: str) -> str:
    parsed = urlparse(site_url)
    return parsed.netloc or site_url.strip("/") or "web"


def _post_permalink(site_url: str, post_id: int) -> str:
    base = site_url.rstrip("/")
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}shared={post_id}"


def _truncate_tg_text(text: str, limit: int = _MAX_TG_TEXT_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


class TelegramBridge:
    """Outbound web → Telegram bridge for discussion comments and Workflow B prompts."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        client: Any | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.client = client

    def set_client(self, client: Any | None) -> None:
        self.client = client

    async def _send_plain_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> Any:
        """Send plain text with parse_mode=DISABLED and link previews disabled to prevent injection."""
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if reply_to_message_id is not None:
            kwargs["reply_to_message_id"] = reply_to_message_id

        optional: dict[str, Any] = {"disable_web_page_preview": True}
        disabled_pm = _get_disabled_parse_mode()
        if disabled_pm is not None:
            optional["parse_mode"] = disabled_pm

        send_fn = self.client.send_message
        try:
            sig = inspect.signature(send_fn)
            has_var_kw = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
            for k, v in optional.items():
                if has_var_kw or k in sig.parameters:
                    kwargs[k] = v
        except (TypeError, ValueError):
            kwargs.update(optional)

        return await send_fn(**kwargs)

    async def bridge_web_comment(self, comment_id: int) -> int | None:
        """Forward a web comment to the active TG discussion group or DM owner for archived posts.

        Returns the Telegram message ID if bridged to the discussion group, or None otherwise.
        """
        if self.client is None:
            logger.debug("Telegram client not configured; skipping bridge for comment %s", comment_id)
            return None

        async with self.session_factory() as session:
            comment = await session.get(Comment, comment_id)
            if comment is None or comment.deleted_at is not None:
                return None
            if comment.source != "web":
                return comment.tg_msg_id
            if comment.tg_msg_id is not None:
                return comment.tg_msg_id

            post = await session.get(Post, comment.post_id)
            if post is None or post.is_deleted:
                return None

            channel = await session.get(Channel, post.channel_id)
            if channel is None:
                return None

            author_name = comment.anon_tag or "Anonymous"
            if comment.user_id is not None:
                user = await session.get(User, comment.user_id)
                if user is not None and user.display_name:
                    author_name = user.display_name

            parent_tg_msg_id: int | None = None
            if comment.parent_id is not None:
                parent_comment = await session.get(Comment, comment.parent_id)
                if parent_comment is not None and parent_comment.tg_msg_id is not None:
                    parent_tg_msg_id = parent_comment.tg_msg_id

            site_host = _extract_site_host(self.settings.SITE_URL)

            # Active epoch with discussion group + linked discussion root -> 2-way bridge
            if (
                channel.status == "active"
                and channel.tg_discussion_chat_id is not None
                and post.tg_discussion_root_id is not None
            ):
                reply_to_id = parent_tg_msg_id or post.tg_discussion_root_id
                formatted = _truncate_tg_text(
                    f"💬 {author_name} (via {site_host}):\n{comment.body_text}"
                )
                try:
                    sent = await self._send_plain_message(
                        chat_id=channel.tg_discussion_chat_id,
                        text=formatted,
                        reply_to_message_id=reply_to_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to bridge web comment %s to TG discussion chat %s",
                        comment_id,
                        channel.tg_discussion_chat_id,
                    )
                    return None

                sent_id = int(getattr(sent, "id", 0) or 0)
                if sent_id > 0:
                    comment.channel_id = channel.id
                    comment.tg_msg_id = sent_id
                    await session.commit()
                    return sent_id
                return None

            # Archived epoch (e.g. Epoch 1) or unlinked post -> DM owner alert instead
            if self.settings.OWNER_TG_ID is not None:
                permalink = _post_permalink(self.settings.SITE_URL, post.id)
                dm_text = _truncate_tg_text(
                    f"Archived post comment alert\n"
                    f"Post #{post.id}: {permalink}\n"
                    f"From: {author_name}\n\n"
                    f"{comment.body_text}"
                )
                try:
                    await self._send_plain_message(
                        chat_id=self.settings.OWNER_TG_ID,
                        text=dm_text,
                    )
                except Exception:
                    logger.exception(
                        "Failed to DM owner %s for archived comment %s",
                        self.settings.OWNER_TG_ID,
                        comment_id,
                    )
            return None

    async def prompt_original_photos(
        self,
        post_id: int,
        *,
        ttl_hours: int = 72,
    ) -> int | None:
        """Create a signed upload link and DM the owner to upload original photos for a post."""
        if self.client is None or self.settings.OWNER_TG_ID is None:
            return None

        async with self.session_factory() as session:
            post = await get_post_by_id(session, post_id, include_deleted=False)
            if post is None or not post.images:
                return None

            raw_token, token_row = await create_upload_token(
                session,
                post.id,
                dm_msg_id=None,
                ttl_hours=ttl_hours,
            )
            upload_url = f"{self.settings.api_base_url}/upload/{raw_token}"
            excerpt = _strip_html(post.text_html, max_len=140)
            photo_count = len(post.images)
            noun = "photo" if photo_count == 1 else "photos"

            lines = [
                f"📸 Original {noun} requested for Post #{post.id} ({photo_count} {noun})",
            ]
            if excerpt:
                lines.append(f"Summary: {excerpt}")
            lines.extend(
                [
                    "",
                    "Option 1: Reply directly to this message with the uncompressed image file(s).",
                    f"Option 2: Upload via browser (expires in {ttl_hours}h):",
                    upload_url,
                ]
            )
            dm_text = _truncate_tg_text("\n".join(lines))

            try:
                sent = await self._send_plain_message(
                    chat_id=self.settings.OWNER_TG_ID,
                    text=dm_text,
                )
            except Exception:
                logger.exception(
                    "Failed to send Workflow B prompt DM to owner %s for post %s",
                    self.settings.OWNER_TG_ID,
                    post.id,
                )
                await session.rollback()
                return None

            sent_id = int(getattr(sent, "id", 0) or 0)
            if sent_id > 0:
                token_row.dm_msg_id = sent_id
            await session.commit()
            return sent_id if sent_id > 0 else None
