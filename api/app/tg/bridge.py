from __future__ import annotations

import html
import inspect
import logging
import re
from collections.abc import Awaitable, Callable
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.security import format_anon_display_name
from app.config import Settings
from app.db.models import Channel, Comment, Post, User
from app.db.repo import get_post_by_id
from app.media.stickers import lookup_sticker_tg_file_id
from app.routes.upload import create_upload_token
from app.tg.avatar_emoji import ensure_sender_avatar_custom_emoji

logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MAX_TG_TEXT_LEN = 4096
_MAX_TG_CAPTION_LEN = 1024


def _get_disabled_parse_mode() -> Any:
    try:
        from pyrogram.enums import ParseMode

        return ParseMode.DISABLED
    except Exception:
        return None


def _get_html_parse_mode() -> Any:
    try:
        from pyrogram.enums import ParseMode

        return ParseMode.HTML
    except Exception:
        return None


class _TgHtmlConverter(HTMLParser):
    """Convert sanitized comment HTML into Telegram Bot API HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[str | None] = []
        self.in_pre = False
        self.pre_lang: str | None = None

    def _emit_newline_sep(self, count: int = 1) -> None:
        current = "".join(self.parts)
        if not current:
            return
        trailing = 0
        for ch in reversed(current):
            if ch == "\n":
                trailing += 1
            else:
                break
        if trailing < count:
            self.parts.append("\n" * (count - trailing))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}

        if t == "br":
            self.parts.append("\n")
            return
        if t in ("p", "blockquote", "pre"):
            self._emit_newline_sep(2 if t == "p" else 1)

        if t in ("b", "strong"):
            self.parts.append("<b>")
            self.stack.append("</b>")
        elif t in ("i", "em"):
            cls_tokens = attr_map.get("class", "").split()
            cid = attr_map.get("data-custom-emoji-id", "").strip()
            if (
                "custom-emoji" in cls_tokens
                and cid.isascii()
                and cid.isdigit()
                and 1 <= len(cid) <= 32
            ):
                self.parts.append(f'<tg-emoji emoji-id="{cid}">')
                self.stack.append("</tg-emoji>")
            else:
                self.parts.append("<i>")
                self.stack.append("</i>")
        elif t == "u":
            self.parts.append("<u>")
            self.stack.append("</u>")
        elif t in ("s", "del"):
            self.parts.append("<s>")
            self.stack.append("</s>")
        elif t == "span":
            if "spoiler" in attr_map.get("class", "").split():
                self.parts.append("<tg-spoiler>")
                self.stack.append("</tg-spoiler>")
            else:
                self.stack.append(None)
        elif t == "blockquote":
            self.parts.append("<blockquote>")
            self.stack.append("</blockquote>")
        elif t == "pre":
            lang = attr_map.get("language", "").strip()
            self.in_pre = True
            self.pre_lang = lang or None
            if lang:
                self.parts.append(
                    f'<pre><code class="language-{html.escape(lang, quote=True)}">'
                )
                self.stack.append("</code></pre>")
            else:
                self.parts.append("<pre>")
                self.stack.append("</pre>")
        elif t == "code":
            if self.in_pre:
                # Already emitted <code class="language-..."> on <pre> if language was set
                if self.pre_lang:
                    self.stack.append(None)
                else:
                    self.parts.append("<code>")
                    self.stack.append("</code>")
            else:
                self.parts.append("<code>")
                self.stack.append("</code>")
        elif t == "a":
            href = attr_map.get("href", "").strip()
            scheme = urlparse(href).scheme.lower()
            if scheme in ("http", "https"):
                self.parts.append(f'<a href="{html.escape(href, quote=True)}">')
                self.stack.append("</a>")
            else:
                self.stack.append(None)
        elif t == "li":
            self._emit_newline_sep(1)
            self.parts.append("• ")
            self.stack.append(None)
        else:
            self.stack.append(None)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "br":
            return
        if self.stack:
            closing = self.stack.pop()
            if closing:
                self.parts.append(closing)
        if t == "pre":
            self.in_pre = False
            self.pre_lang = None
        if t in ("p", "blockquote", "pre", "li", "ul", "ol"):
            self._emit_newline_sep(1 if t != "p" else 2)

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(html.escape(data, quote=False))


def _comment_html_to_tg_html(body_html: str) -> str:
    if not body_html or not body_html.strip():
        return ""
    parser = _TgHtmlConverter()
    parser.feed(body_html)
    parser.close()
    while parser.stack:
        closing = parser.stack.pop()
        if closing:
            parser.parts.append(closing)
    return "".join(parser.parts).strip()


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
        lookup_discussion_root: Callable[[int, int], Awaitable[int | None]] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.client = client
        self.lookup_discussion_root = lookup_discussion_root

    def set_client(self, client: Any | None) -> None:
        self.client = client

    def set_discussion_lookup(
        self, fn: Callable[[int, int], Awaitable[int | None]] | None
    ) -> None:
        self.lookup_discussion_root = fn

    async def _send_with_optional_kwargs(
        self,
        fn: Any,
        base_kwargs: dict[str, Any],
        optional: dict[str, Any],
    ) -> Any:
        kwargs = dict(base_kwargs)
        try:
            sig = inspect.signature(fn)
            has_var_kw = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
            for k, v in optional.items():
                if has_var_kw or k in sig.parameters:
                    kwargs[k] = v
        except (TypeError, ValueError):
            kwargs.update(optional)
        return await fn(**kwargs)

    async def _send_plain_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
    ) -> Any:
        """Send plain text with parse_mode=DISABLED and link previews disabled to prevent injection."""
        base_kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if reply_to_message_id is not None:
            base_kwargs["reply_to_message_id"] = reply_to_message_id

        optional: dict[str, Any] = {"disable_web_page_preview": True}
        disabled_pm = _get_disabled_parse_mode()
        if disabled_pm is not None:
            optional["parse_mode"] = disabled_pm

        return await self._send_with_optional_kwargs(
            self.client.send_message, base_kwargs, optional
        )

    async def _send_html_or_plain_message(
        self,
        *,
        chat_id: int,
        html_text: str,
        plain_text: str,
        reply_to_message_id: int | None = None,
    ) -> Any:
        html_pm = _get_html_parse_mode()
        if html_pm is not None and len(html_text) <= _MAX_TG_TEXT_LEN:
            base_kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "text": html_text,
            }
            if reply_to_message_id is not None:
                base_kwargs["reply_to_message_id"] = reply_to_message_id
            try:
                return await self._send_with_optional_kwargs(
                    self.client.send_message,
                    base_kwargs,
                    {"parse_mode": html_pm, "disable_web_page_preview": True},
                )
            except Exception:
                logger.warning(
                    "Telegram HTML send failed for chat %s; falling back to plain text",
                    chat_id,
                    exc_info=True,
                )

        return await self._send_plain_message(
            chat_id=chat_id,
            text=_truncate_tg_text(plain_text, limit=_MAX_TG_TEXT_LEN),
            reply_to_message_id=reply_to_message_id,
        )

    def _resolve_local_media_file(self, rel_url: str | None) -> Any | None:
        if not rel_url:
            return None
        media_root = self.settings.media_dir.resolve()
        candidate = (media_root / str(rel_url).lstrip("/")).resolve()
        if not candidate.is_relative_to(media_root) or not candidate.is_file():
            return None
        return candidate

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
            if comment.moderation_status != "approved":
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

            user_obj: User | None = None
            author_name = format_anon_display_name(comment.anon_tag)
            if comment.user_id is not None:
                user_obj = await session.get(User, comment.user_id)
                if user_obj is not None and user_obj.display_name:
                    author_name = user_obj.display_name

            parent_tg_msg_id: int | None = None
            if comment.parent_id is not None:
                parent_comment = await session.get(Comment, comment.parent_id)
                if parent_comment is not None and parent_comment.tg_msg_id is not None:
                    parent_tg_msg_id = parent_comment.tg_msg_id

            # Resolve missing discussion root on active channels on demand before falling back to DM
            if (
                channel.status == "active"
                and channel.tg_discussion_chat_id is not None
                and post.tg_discussion_root_id is None
                and channel.tg_chat_id is not None
                and post.tg_msg_id is not None
                and self.lookup_discussion_root is not None
            ):
                try:
                    resolved_root_id = await self.lookup_discussion_root(
                        int(channel.tg_chat_id), int(post.tg_msg_id)
                    )
                    if resolved_root_id is not None and resolved_root_id > 0:
                        post.tg_discussion_root_id = int(resolved_root_id)
                        await session.commit()
                except Exception:
                    logger.debug(
                        "On-demand discussion root lookup failed for post %s",
                        post.id,
                        exc_info=True,
                    )

            # Active epoch with discussion group + linked discussion root -> mautrix-telegram style relay
            if (
                channel.status == "active"
                and channel.tg_discussion_chat_id is not None
                and post.tg_discussion_root_id is not None
            ):
                reply_to_id = parent_tg_msg_id or post.tg_discussion_root_id
                avatar_emoji = await ensure_sender_avatar_custom_emoji(
                    session_factory=self.session_factory,
                    settings=self.settings,
                    client=self.client,
                    user=user_obj,
                    anon_display_name=author_name if user_obj is None else None,
                )
                if avatar_emoji is not None:
                    pfp_cid, pfp_fallback = avatar_emoji
                    sender_html = (
                        f'<tg-emoji emoji-id="{pfp_cid}">{html.escape(pfp_fallback)}</tg-emoji> '
                        f"<b>{html.escape(author_name)}</b>"
                    )
                else:
                    sender_html = f"<b>{html.escape(author_name)}</b>"
                body_tg_html = _comment_html_to_tg_html(comment.body_html)
                if not body_tg_html and comment.body_text:
                    body_tg_html = html.escape(comment.body_text)

                full_html = (
                    f"{sender_html}:\n{body_tg_html}" if body_tg_html else sender_html
                )
                plain_fallback = (
                    f"{author_name}:\n{comment.body_text}"
                    if comment.body_text
                    else author_name
                )

                media = comment.media if isinstance(comment.media, dict) else None
                media_type = str(media.get("media_type") or "").lower() if media else ""
                local_media = (
                    self._resolve_local_media_file(media.get("url")) if media else None
                )
                sticker_file_id = (
                    lookup_sticker_tg_file_id(
                        self.settings.media_dir,
                        media.get("url"),
                        sticker_id=media.get("sticker_id"),
                    )
                    if media and media_type == "sticker"
                    else None
                )

                try:
                    sent: Any = None
                    if (
                        media_type == "sticker"
                        and (sticker_file_id is not None or local_media is not None)
                        and hasattr(self.client, "send_sticker")
                    ):
                        sticker_target = sticker_file_id or str(local_media)
                        try:
                            sent = await self._send_with_optional_kwargs(
                                self.client.send_sticker,
                                {
                                    "chat_id": channel.tg_discussion_chat_id,
                                    "sticker": sticker_target,
                                },
                                {"reply_to_message_id": reply_to_id},
                            )
                        except Exception:
                            if sticker_file_id is None or local_media is None:
                                raise
                            logger.warning(
                                "Telegram send_sticker by file_id failed for comment %s; retrying local file",
                                comment_id,
                                exc_info=True,
                            )
                            sent = await self._send_with_optional_kwargs(
                                self.client.send_sticker,
                                {
                                    "chat_id": channel.tg_discussion_chat_id,
                                    "sticker": str(local_media),
                                },
                                {"reply_to_message_id": reply_to_id},
                            )
                        follow_reply_id = int(getattr(sent, "id", 0) or 0) or reply_to_id
                        notice_html = (
                            full_html if body_tg_html else f"{sender_html} sent a sticker"
                        )
                        notice_plain = (
                            plain_fallback if comment.body_text else f"{author_name} sent a sticker"
                        )
                        await self._send_html_or_plain_message(
                            chat_id=channel.tg_discussion_chat_id,
                            html_text=notice_html,
                            plain_text=notice_plain,
                            reply_to_message_id=follow_reply_id,
                        )
                    elif (
                        media_type == "photo"
                        and local_media is not None
                        and hasattr(self.client, "send_photo")
                    ):
                        caption_html = (
                            full_html if body_tg_html else f"{sender_html} sent an image"
                        )
                        caption_plain = (
                            plain_fallback if comment.body_text else f"{author_name} sent an image"
                        )
                        html_pm = _get_html_parse_mode()
                        if html_pm is not None and len(caption_html) <= _MAX_TG_CAPTION_LEN:
                            try:
                                sent = await self._send_with_optional_kwargs(
                                    self.client.send_photo,
                                    {
                                        "chat_id": channel.tg_discussion_chat_id,
                                        "photo": str(local_media),
                                    },
                                    {
                                        "caption": caption_html,
                                        "parse_mode": html_pm,
                                        "reply_to_message_id": reply_to_id,
                                    },
                                )
                            except Exception:
                                logger.warning(
                                    "Telegram HTML photo caption failed for comment %s; retrying plain",
                                    comment_id,
                                    exc_info=True,
                                )
                                sent = None
                        if sent is None:
                            disabled_pm = _get_disabled_parse_mode()
                            opt: dict[str, Any] = {
                                "caption": _truncate_tg_text(
                                    caption_plain, limit=_MAX_TG_CAPTION_LEN
                                ),
                                "reply_to_message_id": reply_to_id,
                            }
                            if disabled_pm is not None:
                                opt["parse_mode"] = disabled_pm
                            sent = await self._send_with_optional_kwargs(
                                self.client.send_photo,
                                {
                                    "chat_id": channel.tg_discussion_chat_id,
                                    "photo": str(local_media),
                                },
                                opt,
                            )
                    else:
                        sent = await self._send_html_or_plain_message(
                            chat_id=channel.tg_discussion_chat_id,
                            html_text=full_html,
                            plain_text=plain_fallback,
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
                alert_title = (
                    "Archived post comment alert"
                    if channel.status == "archived"
                    else "Unlinked post comment alert"
                )
                dm_text = _truncate_tg_text(
                    f"{alert_title}\n"
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

    async def delete_bridged_comment(self, comment_id: int) -> bool:
        """Best-effort delete of a bridged or TG-sourced comment message in Telegram."""
        if self.client is None or not hasattr(self.client, "delete_messages"):
            return False

        async with self.session_factory() as session:
            comment = await session.get(Comment, comment_id)
            if comment is None or comment.tg_msg_id is None or comment.channel_id is None:
                return False
            channel = await session.get(Channel, comment.channel_id)
            if channel is None or channel.tg_discussion_chat_id is None:
                return False
            tg_chat_id = int(channel.tg_discussion_chat_id)
            tg_msg_id = int(comment.tg_msg_id)

        try:
            await self.client.delete_messages(tg_chat_id, [tg_msg_id])
            return True
        except Exception:
            logger.warning(
                "Best-effort TG delete failed for comment_id=%s (chat=%s, msg=%s)",
                comment_id,
                tg_chat_id,
                tg_msg_id,
                exc_info=True,
            )
            return False

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

    async def notify_owner_moderation_pending(self, comment_id: int) -> int | None:
        """Send a safe, zero-payload alert DM to OWNER_TG_ID with a link to the web review modal."""
        if self.client is None or self.settings.OWNER_TG_ID is None:
            return None

        async with self.session_factory() as session:
            comment = await session.get(Comment, comment_id)
            if comment is None or comment.deleted_at is not None:
                return None
            if comment.moderation_status != "pending":
                return None

            sender_label = format_anon_display_name(comment.anon_tag)
            if comment.user_id is not None:
                user = await session.get(User, comment.user_id)
                if user is not None and user.display_name:
                    sender_label = user.display_name

            meta = comment.moderation_meta if isinstance(comment.moderation_meta, dict) else {}
            reason = str(meta.get("reason") or "needs_review")
            top_cat = meta.get("top_category")
            top_score = meta.get("top_score")
            reason_line = f"{reason}"
            if top_cat and isinstance(top_score, (int, float)) and float(top_score) > 0:
                reason_line = f"{reason} ({top_cat}: {float(top_score):.0%})"

            site_base = self.settings.SITE_URL.rstrip("/")
            review_url = f"{site_base}?review={comment.id}"
            lines = [
                f"🛡️ Comment #{comment.id} pending review on Post #{comment.post_id}",
                f"Sender: {sender_label}",
                f"Reason: {reason_line}",
                f"Review: {review_url}",
            ]
            try:
                sent = await self._send_plain_message(
                    chat_id=self.settings.OWNER_TG_ID,
                    text="\n".join(lines),
                )
            except Exception:
                logger.warning(
                    "Failed sending moderation alert DM for comment %s",
                    comment_id,
                    exc_info=True,
                )
                return None
            sent_id = int(getattr(sent, "id", 0) or 0)
            return sent_id if sent_id > 0 else None
