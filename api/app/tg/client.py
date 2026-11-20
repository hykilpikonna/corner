from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import shutil
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import nh3
from PIL import Image, ImageOps
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.db.models import (
    Channel,
    Comment,
    CommentTgReaction,
    NotificationQueue,
    OriginalUploadToken,
    Poll,
    PollOption,
    Post,
    PostImage,
    TgReaction,
    User,
    UserIdentity,
)
from app.db.repo import (
    compute_merged_comment_reactions,
    compute_merged_poll,
    compute_merged_reactions,
    ensure_channel,
    find_post_by_channel_tg_msg,
    format_iso_utc,
    get_active_channel,
    get_post_by_id,
    make_reaction_key,
    resolve_media_url,
    serialize_post,
    upsert_post,
)
from app.exports import generate_exports
from app.media.fingerprint import compute_image_fingerprint
from app.media.matcher import build_candidate_photo, match_originals_to_post_images
from app.media.optimize import (
    MAX_UPLOAD_BYTES,
    InvalidImageError,
    sanitize_original_filename,
    validate_image_file,
)
from app.routes.upload import (
    SSEPublishFn,
    apply_original_matches,
    build_target_post_images,
    invoke_sse_publish,
)
from app.tg.bridge import TelegramBridge

logger = logging.getLogger(__name__)

ALLOWED_COMMENT_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "i",
    "p",
    "pre",
    "s",
    "span",
    "strong",
    "u",
}
ALLOWED_COMMENT_ATTRS = {
    "a": {"href", "title"},
    "pre": {"language"},
    "span": {"class"},
    "blockquote": {"class"},
}
_SAFE_URL_SCHEMES = frozenset({"http", "https", "mailto", "tg"})
_IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".tif",
    ".tiff",
    ".bmp",
    ".heic",
    ".heif",
    ".avif",
}


def sanitize_tg_comment_html(raw_html: str) -> str:
    return nh3.clean(
        raw_html,
        tags=ALLOWED_COMMENT_TAGS,
        attributes=ALLOWED_COMMENT_ATTRS,
        url_schemes={"http", "https", "mailto"},
    )


def _sanitize_entity_url(raw_url: str, *, allow_schemeless_https: bool = False) -> str | None:
    cleaned = raw_url.strip()
    if not cleaned:
        return None
    parsed = urlparse(cleaned)
    if not parsed.scheme and allow_schemeless_https and not cleaned.startswith("//"):
        cleaned = f"https://{cleaned}"
        parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in _SAFE_URL_SCHEMES:
        return None
    return cleaned


def _parse_bot_id_from_token(token: str | None) -> int | None:
    if not token or ":" not in token:
        return None
    prefix = token.split(":", 1)[0].strip()
    if prefix.isdigit():
        return int(prefix)
    return None


def _ensure_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _extract_formatted_text(val: Any) -> str:
    if val is None:
        return ""
    if hasattr(val, "text"):
        return str(val.text or "")
    return str(val)


def _render_entities_to_html(text: str, entities: Sequence[Any] | None = None) -> str:
    """Convert Telegram text + UTF-16 offset entities into HTML with strict URL scheme checks."""
    if not text:
        return ""
    if not entities:
        return html.escape(text).replace("\n", "<br>")

    utf16 = text.encode("utf-16-le")
    code_units = len(utf16) // 2

    def _u16_slice(start: int, end: int) -> str:
        s = max(0, min(start, code_units))
        e = max(s, min(end, code_units))
        return utf16[s * 2 : e * 2].decode("utf-16-le", errors="replace")

    valid = [
        en
        for en in entities
        if getattr(en, "offset", None) is not None and getattr(en, "length", None) is not None
    ]
    valid.sort(key=lambda en: (int(en.offset), -int(en.length)))

    parts: list[str] = []
    cursor = 0
    for en in valid:
        offset = int(en.offset)
        length = int(en.length)
        end = offset + length
        if offset < cursor:
            continue
        if offset > cursor:
            parts.append(html.escape(_u16_slice(cursor, offset)).replace("\n", "<br>"))

        seg = _u16_slice(offset, end)
        esc = html.escape(seg).replace("\n", "<br>")
        etype = str(getattr(en, "type", "")).upper().split(".")[-1]

        if etype == "BOLD":
            parts.append(f"<b>{esc}</b>")
        elif etype == "ITALIC":
            parts.append(f"<em>{esc}</em>")
        elif etype == "UNDERLINE":
            parts.append(f"<u>{esc}</u>")
        elif etype == "STRIKETHROUGH":
            parts.append(f"<del>{esc}</del>")
        elif etype == "CODE":
            parts.append(f"<code>{html.escape(seg)}</code>")
        elif etype == "PRE":
            lang = getattr(en, "language", None)
            if lang:
                parts.append(
                    f'<pre language="{html.escape(str(lang), quote=True)}"><code>{html.escape(seg)}</code></pre>'
                )
            else:
                parts.append(f"<pre><code>{html.escape(seg)}</code></pre>")
        elif etype == "TEXT_LINK" and getattr(en, "url", None):
            safe_url = _sanitize_entity_url(str(en.url), allow_schemeless_https=False)
            if safe_url is not None:
                parts.append(f'<a href="{html.escape(safe_url, quote=True)}">{esc}</a>')
            else:
                parts.append(esc)
        elif etype == "URL":
            safe_url = _sanitize_entity_url(seg, allow_schemeless_https=True)
            if safe_url is not None:
                parts.append(f'<a href="{html.escape(safe_url, quote=True)}">{esc}</a>')
            else:
                parts.append(esc)
        elif etype == "MENTION":
            handle = html.escape(seg.lstrip("@"), quote=True)
            parts.append(f'<a href="https://t.me/{handle}">{esc}</a>')
        elif etype in ("BLOCKQUOTE", "EXPANDABLE_BLOCKQUOTE"):
            parts.append(f"<blockquote>{esc}</blockquote>")
        elif etype == "SPOILER":
            parts.append(f'<span class="spoiler">{esc}</span>')
        else:
            parts.append(esc)
        cursor = end

    if cursor < code_units:
        parts.append(html.escape(_u16_slice(cursor, code_units)).replace("\n", "<br>"))

    return "".join(parts)


def _read_image_size_sync(image_path: Path) -> tuple[int, int]:
    with Image.open(image_path) as im:
        w, h = im.size
        try:
            orient = im.getexif().get(274)
        except Exception:
            orient = None
        if orient in (5, 6, 7, 8):
            return h, w
        return w, h


def _create_webp_thumb_sync(
    image_path: Path, max_size: tuple[int, int] = (1000, 1000)
) -> Path | None:
    if not image_path.is_file():
        return None
    thumb_path = image_path.parent / f"{image_path.stem}_thumb.webp"
    if thumb_path.exists():
        return thumb_path
    tmp_path = image_path.parent / f".{image_path.stem}.{uuid.uuid4().hex[:8]}.tmp.webp"
    try:
        with Image.open(image_path) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail(max_size, Image.Resampling.LANCZOS)
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if ("A" in im.mode or im.mode == "P") else "RGB")
            im.save(tmp_path, "WEBP", quality=80, method=6)
        os.replace(tmp_path, thumb_path)
        return thumb_path
    except Exception:
        tmp_path.unlink(missing_ok=True)
        return None


def _extract_poll_dict(msg: Any) -> dict[str, Any] | None:
    poll = getattr(msg, "poll", None)
    if poll is None:
        return None

    poll_type_raw = getattr(poll, "type", "REGULAR")
    poll_type = str(poll_type_raw).split(".")[-1].upper()
    if poll_type not in ("REGULAR", "QUIZ"):
        poll_type = "REGULAR"

    allow_multi = bool(
        getattr(poll, "allows_multiple_answers", None)
        if getattr(poll, "allows_multiple_answers", None) is not None
        else getattr(poll, "allow_multiple_answers", False)
    )

    correct_idx = getattr(poll, "correct_option_id", None)
    if correct_idx is None:
        cids = getattr(poll, "correct_option_ids", None)
        if isinstance(cids, (list, tuple)) and cids:
            correct_idx = int(cids[0])

    options_out: list[dict[str, Any]] = []
    for idx, opt in enumerate(getattr(poll, "options", None) or []):
        opt_text = _extract_formatted_text(getattr(opt, "text", ""))
        opt_data = getattr(opt, "data", None)
        if opt_data is None:
            opt_data = getattr(opt, "persistent_id", str(idx))
        if isinstance(opt_data, bytes):
            opt_data = opt_data.decode("utf-8", errors="replace")
        options_out.append(
            {
                "option_idx": idx,
                "text": opt_text,
                "data": str(opt_data),
                "tg_voter_count": int(getattr(opt, "voter_count", 0) or 0),
            }
        )

    explanation_raw = getattr(poll, "explanation", None)
    explanation = _extract_formatted_text(explanation_raw) if explanation_raw is not None else None

    return {
        "id": str(getattr(poll, "id", f"poll_{msg.id}")),
        "question": _extract_formatted_text(getattr(poll, "question", "")),
        "is_closed": bool(getattr(poll, "is_closed", False)),
        "is_anonymous": bool(getattr(poll, "is_anonymous", True)),
        "type": poll_type,
        "allow_multiple_answers": allow_multi,
        "correct_option_idx": int(correct_idx) if correct_idx is not None else None,
        "explanation": explanation,
        "options": options_out,
    }


def _extract_reactions_list(msg: Any) -> list[dict[str, Any]] | None:
    reactions_obj = getattr(msg, "reactions", None)
    if reactions_obj is None:
        return None
    raw_list = getattr(reactions_obj, "reactions", reactions_obj)
    if not isinstance(raw_list, (list, tuple)):
        return None
    out: list[dict[str, Any]] = []
    for r in raw_list:
        emoji = getattr(r, "emoji", None)
        custom_emoji_id = getattr(r, "custom_emoji_id", None)
        count = int(getattr(r, "count", 0) or 0)
        if not emoji and custom_emoji_id is None:
            continue
        out.append(
            {
                "emoji": emoji,
                "custom_emoji_id": str(custom_emoji_id) if custom_emoji_id is not None else None,
                "count": count,
            }
        )
    return out


def _extract_forward_info(msg: Any) -> dict[str, Any] | None:
    origin = getattr(msg, "forward_origin", None)
    if origin is not None:
        sender_user = getattr(origin, "sender_user", None)
        if sender_user is not None:
            name = " ".join(
                p
                for p in (
                    getattr(sender_user, "first_name", None),
                    getattr(sender_user, "last_name", None),
                )
                if p
            ).strip()
            username = getattr(sender_user, "username", None)
            return {"name": name, "url": f"https://t.me/{username}" if username else None}
        chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if chat is not None:
            return {"name": getattr(chat, "title", str(getattr(chat, "id", "")))}
        sender_name = getattr(origin, "sender_user_name", None)
        if sender_name:
            return {"name": str(sender_name)}
        return None

    fwd_user = getattr(msg, "forward_from", None)
    if fwd_user is not None:
        name = " ".join(
            p
            for p in (getattr(fwd_user, "first_name", None), getattr(fwd_user, "last_name", None))
            if p
        ).strip()
        username = getattr(fwd_user, "username", None)
        return {"name": name, "url": f"https://t.me/{username}" if username else None}

    fwd_chat = getattr(msg, "forward_from_chat", None)
    if fwd_chat is not None:
        return {"name": getattr(fwd_chat, "title", str(getattr(fwd_chat, "id", "")))}

    fwd_sender = getattr(msg, "forward_sender_name", None)
    if fwd_sender:
        return {"name": str(fwd_sender)}
    return None


def _try_import_tgc_process_messages():
    try:
        from tgc.pyro.crawl import (  # type: ignore[import-not-found]
            process_messages as tgc_process_messages,
        )

        return tgc_process_messages
    except Exception:
        return None


def _post_image_to_dict(img: PostImage, *, sort_order: int | None = None) -> dict[str, Any]:
    return {
        "sort_order": img.sort_order if sort_order is None else sort_order,
        "tg_msg_id": img.tg_msg_id,
        "url": img.url,
        "thumb_url": img.thumb_url,
        "original_url": img.original_url,
        "original_size": img.original_size,
        "original_name": img.original_name,
        "size": img.size,
        "width": img.width,
        "height": img.height,
        "spoiler": img.spoiler,
        "phash": img.phash,
        "rgb_sig": img.rgb_sig,
    }


class AlbumBuffer:
    """Sliding-window debouncer for Telegram album messages sharing a `media_group_id`.

    Waits `debounce_seconds` after the latest message in a group, capped at `max_wait_seconds`
    from the first message in that group, then flushes all buffered messages sorted by `sort_key`.
    Flushes for the same `group_key` are serialized via per-key locks.
    """

    def __init__(
        self,
        *,
        on_flush: Callable[[list[Any]], Awaitable[None]],
        debounce_seconds: float = 1.5,
        max_wait_seconds: float = 5.0,
        sort_key: Callable[[Any], int] | None = None,
    ) -> None:
        self._on_flush = on_flush
        self.debounce_seconds = debounce_seconds
        self.max_wait_seconds = max_wait_seconds
        self._sort_key = sort_key or (lambda m: int(getattr(m, "id", 0) or 0))
        self._groups: dict[str, list[Any]] = {}
        self._started_at: dict[str, float] = {}
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._group_locks: dict[str, asyncio.Lock] = {}
        self._in_flight: set[asyncio.Task[None]] = set()
        self._closed = False

    def add(self, group_key: str, msg: Any) -> None:
        if self._closed:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()

        if group_key not in self._groups:
            self._groups[group_key] = []
            self._started_at[group_key] = now
        self._groups[group_key].append(msg)

        existing_timer = self._timers.pop(group_key, None)
        if existing_timer is not None and not existing_timer.done():
            existing_timer.cancel()

        elapsed = now - self._started_at[group_key]
        remaining_cap = max(0.0, self.max_wait_seconds - elapsed)
        delay = min(self.debounce_seconds, remaining_cap)

        task = loop.create_task(self._wait_and_flush(group_key, delay))
        self._timers[group_key] = task

    async def _wait_and_flush(self, group_key: str, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self.flush_group(group_key)

    async def flush_group(self, group_key: str) -> None:
        timer = self._timers.pop(group_key, None)
        current = asyncio.current_task()
        if timer is not None and timer is not current and not timer.done():
            timer.cancel()

        msgs = self._groups.pop(group_key, None)
        self._started_at.pop(group_key, None)
        if not msgs:
            return

        msgs.sort(key=self._sort_key)
        task = asyncio.create_task(self._run_flush(group_key, msgs))
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)
        await task

    async def _run_flush(self, group_key: str, msgs: list[Any]) -> None:
        lock = self._group_locks.setdefault(group_key, asyncio.Lock())
        async with lock:
            try:
                await self._on_flush(msgs)
            except Exception:
                logger.exception("AlbumBuffer flush callback failed for group %s", group_key)
        if group_key not in self._groups and group_key not in self._timers and not lock.locked():
            self._group_locks.pop(group_key, None)

    async def wait_idle(self) -> None:
        """Wait until all scheduled debounce timers and in-flight flush callbacks finish."""
        while self._timers or self._in_flight:
            pending = list(self._timers.values()) + list(self._in_flight)
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)

    async def flush_all(self) -> None:
        keys = list(self._groups.keys())
        for key in keys:
            await self.flush_group(key)

    async def close(self) -> None:
        self._closed = True
        cancelled: list[asyncio.Task[None]] = []
        for timer in list(self._timers.values()):
            if not timer.done():
                timer.cancel()
                cancelled.append(timer)
        self._timers.clear()
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        await self.flush_all()
        if self._in_flight:
            await asyncio.gather(*list(self._in_flight), return_exceptions=True)


class TelegramService:
    """Real-time Kurigram MTProto event engine for channel posts, discussion comments, reactions, polls, and Workflow B DMs."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        sse_publish: SSEPublishFn | None = None,
        bridge: TelegramBridge | None = None,
        client: Any | None = None,
        album_debounce_seconds: float = 1.5,
        album_max_wait_seconds: float = 5.0,
        dm_debounce_seconds: float = 2.0,
        dm_max_wait_seconds: float = 8.0,
        bot_id: int | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.sse_publish = sse_publish
        self.bridge = bridge or TelegramBridge(
            session_factory=session_factory,
            settings=settings,
            client=client,
        )
        self.client = client
        # Pre-populate bot_id from TG_BOT_TOKEN prefix so echo loop guard is active even during startup
        self.bot_id: int | None = (
            bot_id if bot_id is not None else _parse_bot_id_from_token(settings.TG_BOT_TOKEN)
        )
        self._started = False
        self._handlers_registered = False

        self.album_buffer = AlbumBuffer(
            on_flush=self._on_album_flushed,
            debounce_seconds=album_debounce_seconds,
            max_wait_seconds=album_max_wait_seconds,
        )
        self.dm_buffer = AlbumBuffer(
            on_flush=self._on_dm_batch_flushed,
            debounce_seconds=dm_debounce_seconds,
            max_wait_seconds=dm_max_wait_seconds,
            sort_key=lambda item: int(getattr(item[1], "id", 0) or 0),
        )

    @property
    def is_running(self) -> bool:
        return self._started

    async def _emit_sse(self, event: str, data: dict[str, Any]) -> None:
        await invoke_sse_publish(self.sse_publish, event, data)

    async def start(self) -> None:
        """Start Kurigram client and register handlers (clean no-op if TG_BOT_TOKEN is unset)."""
        if self._started:
            return

        if self.client is None:
            if not self.settings.TG_BOT_TOKEN:
                logger.info("TG_BOT_TOKEN is unset; TelegramService running in disabled no-op mode.")
                return
            if not self.settings.TG_API_ID or not self.settings.TG_API_HASH:
                raise ValueError(
                    "TG_API_ID and TG_API_HASH must be configured when TG_BOT_TOKEN is set"
                )

            from pyrogram import Client

            self.settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.client = Client(
                name=self.settings.TG_SESSION_NAME,
                api_id=int(self.settings.TG_API_ID),
                api_hash=str(self.settings.TG_API_HASH),
                bot_token=self.settings.TG_BOT_TOKEN,
                workdir=str(self.settings.DATA_DIR),
            )

        self.bridge.set_client(self.client)

        await self.client.start()
        me = await self.client.get_me()
        self.bot_id = int(me.id)
        # Register handlers only AFTER bot_id is confirmed to eliminate startup echo race
        self._register_handlers()
        self._started = True
        logger.info("TelegramService started as bot ID %s (@%s)", me.id, getattr(me, "username", None))

        if self.settings.TG_CHANNEL_ID is not None:
            async with self.session_factory() as session:
                await ensure_channel(
                    session,
                    tg_chat_id=self.settings.TG_CHANNEL_ID,
                    tg_discussion_chat_id=self.settings.TG_DISCUSSION_CHAT_ID,
                    status="active",
                )
                await session.commit()

    async def stop(self) -> None:
        await self.album_buffer.close()
        await self.dm_buffer.close()
        if self._started and self.client is not None:
            try:
                await self.client.stop()
            except Exception:
                logger.exception("Error stopping Kurigram client")
        self._started = False

    def _register_handlers(self) -> None:
        if self._handlers_registered or self.client is None:
            return
        if not hasattr(self.client, "add_handler"):
            return

        from pyrogram import filters
        from pyrogram.handlers import (
            CallbackQueryHandler,
            EditedMessageHandler,
            MessageHandler,
            MessageReactionCountHandler,
            PollHandler,
        )

        if self.settings.TG_CHANNEL_ID is not None:
            channel_filter = filters.chat(self.settings.TG_CHANNEL_ID)

            async def _on_channel_msg(_client: Any, msg: Any) -> None:
                await self.handle_channel_message(msg)

            async def _on_channel_edit(_client: Any, msg: Any) -> None:
                await self.handle_channel_edit(msg)

            self.client.add_handler(MessageHandler(_on_channel_msg, channel_filter))
            self.client.add_handler(EditedMessageHandler(_on_channel_edit, channel_filter))

        if self.settings.TG_DISCUSSION_CHAT_ID is not None:
            disc_filter = filters.chat(self.settings.TG_DISCUSSION_CHAT_ID)

            async def _on_discussion_msg(_client: Any, msg: Any) -> None:
                await self.handle_discussion_message(msg)

            self.client.add_handler(MessageHandler(_on_discussion_msg, disc_filter))

        if self.settings.OWNER_TG_ID is not None:
            owner_dm_filter = filters.private & filters.user(self.settings.OWNER_TG_ID)

            async def _on_owner_dm(_client: Any, msg: Any) -> None:
                await self.handle_owner_dm(msg)

            async def _on_cb(_client: Any, query: Any) -> None:
                await self.handle_callback_query(query)

            self.client.add_handler(MessageHandler(_on_owner_dm, owner_dm_filter))
            self.client.add_handler(CallbackQueryHandler(_on_cb))

        async def _on_reaction_count(_client: Any, update: Any) -> None:
            await self.handle_reaction_count_update(update)

        async def _on_poll(_client: Any, poll: Any) -> None:
            await self.handle_poll_update(poll)

        reaction_chats = [
            cid
            for cid in (self.settings.TG_CHANNEL_ID, self.settings.TG_DISCUSSION_CHAT_ID)
            if cid is not None
        ]
        reaction_filter = filters.chat(reaction_chats) if reaction_chats else None
        self.client.add_handler(MessageReactionCountHandler(_on_reaction_count, reaction_filter))
        self.client.add_handler(PollHandler(_on_poll))
        self._handlers_registered = True

    # ------------------------------------------------------------------
    # Channel Posts & Edits
    # ------------------------------------------------------------------

    async def handle_channel_message(self, msg: Any) -> None:
        media_group_id = getattr(msg, "media_group_id", None)
        if media_group_id is not None:
            chat_id = getattr(getattr(msg, "chat", None), "id", 0)
            self.album_buffer.add(f"{chat_id}:{media_group_id}", msg)
            return
        await self._ingest_channel_messages([msg], is_edit=False)

    async def handle_channel_edit(self, msg: Any) -> None:
        await self._ingest_channel_messages([msg], is_edit=True)

    async def _on_album_flushed(self, msgs: list[Any]) -> None:
        await self._ingest_channel_messages(msgs, is_edit=False)

    async def _convert_channel_messages(
        self,
        msgs: list[Any],
        channel: Channel,
    ) -> list[dict[str, Any]]:
        """Convert Kurigram messages using `tgc.pyro.crawl.process_messages` if installed, else built-in fallback."""
        epoch_dir = self.settings.media_dir / "epochs" / str(channel.id)
        epoch_dir.mkdir(parents=True, exist_ok=True)
        url_prefix = f"epochs/{channel.id}"

        tgc_proc = _try_import_tgc_process_messages()
        if tgc_proc is not None and self.client is not None:
            try:
                from pyrogram import Client as PyroClient

                if isinstance(self.client, PyroClient):
                    return await tgc_proc(
                        msgs,
                        epoch_dir,
                        client=self.client,
                        media_subdir="media",
                        emoji_subdir="emoji",
                        url_prefix=url_prefix,
                        optimize_images=True,
                    )
            except Exception:
                logger.exception(
                    "tgc.pyro.crawl.process_messages failed; falling back to built-in converter"
                )

        return await self._fallback_process_messages(msgs, epoch_dir, url_prefix=url_prefix)

    async def _fallback_process_messages(
        self,
        msgs: list[Any],
        epoch_dir: Path,
        *,
        url_prefix: str,
    ) -> list[dict[str, Any]]:
        """Built-in message converter & album grouper matching `tgc` output shape."""
        media_dir = epoch_dir / "media"
        media_dir.mkdir(parents=True, exist_ok=True)

        sorted_msgs = sorted(msgs, key=lambda m: int(getattr(m, "id", 0) or 0))
        grouped: dict[str, dict[str, Any]] = {}
        posts_out: list[dict[str, Any]] = []

        for msg in sorted_msgs:
            msg_id = int(getattr(msg, "id", 0))
            raw_text = getattr(msg, "text", None)
            raw_caption = getattr(msg, "caption", None)
            if raw_text:
                text_html = _render_entities_to_html(
                    str(raw_text), getattr(msg, "entities", None)
                )
            elif raw_caption:
                text_html = _render_entities_to_html(
                    str(raw_caption), getattr(msg, "caption_entities", None)
                )
            elif getattr(msg, "poll", None) is not None:
                text_html = html.escape(_extract_formatted_text(msg.poll.question))
            else:
                text_html = None

            date = _ensure_utc(getattr(msg, "date", None))
            edit_date = getattr(msg, "edit_date", None)
            edited_at = _ensure_utc(edit_date) if edit_date is not None else None
            media_group_id = getattr(msg, "media_group_id", None)

            images: list[dict[str, Any]] = []
            files: list[dict[str, Any]] = []

            photo = getattr(msg, "photo", None)
            if photo is not None:
                dest_path = media_dir / f"{msg_id}.jpg"
                downloaded_path: Path | None = None
                if self.client is not None and hasattr(self.client, "download_media"):
                    try:
                        dl = await self.client.download_media(msg, file_name=str(dest_path))
                        if dl:
                            downloaded_path = Path(str(dl))
                    except Exception:
                        logger.exception("Failed to download photo for msg %s", msg_id)

                width = int(getattr(photo, "width", 0) or 0)
                height = int(getattr(photo, "height", 0) or 0)
                file_size = getattr(photo, "file_size", None)
                rel_url = f"{url_prefix}/media/{dest_path.name}"
                rel_thumb: str | None = None

                if downloaded_path is not None and downloaded_path.is_file():
                    rel_url = f"{url_prefix}/media/{downloaded_path.name}"
                    file_size = downloaded_path.stat().st_size
                    thumb_p = await asyncio.to_thread(_create_webp_thumb_sync, downloaded_path)
                    if thumb_p is not None:
                        rel_thumb = f"{url_prefix}/media/{thumb_p.name}"
                    try:
                        width, height = await asyncio.to_thread(
                            _read_image_size_sync, downloaded_path
                        )
                    except Exception:
                        pass

                images.append(
                    {
                        "tg_msg_id": msg_id,
                        "url": rel_url,
                        "thumb": rel_thumb,
                        "width": width,
                        "height": height,
                        "size": file_size,
                        "spoiler": bool(getattr(msg, "has_media_spoiler", False)),
                    }
                )

            poll_dict = _extract_poll_dict(msg)
            reactions = _extract_reactions_list(msg)

            quote_obj = getattr(msg, "quote", None)
            quote = None
            if quote_obj is not None and getattr(quote_obj, "text", None):
                quote = {
                    "text": _render_entities_to_html(
                        str(quote_obj.text), getattr(quote_obj, "entities", None)
                    ),
                    "position": getattr(quote_obj, "position", None),
                }

            post_dict: dict[str, Any] = {
                "id": msg_id,
                "tg_all_msg_ids": [msg_id],
                "date": date,
                "edited_at": edited_at,
                "type": "service" if getattr(msg, "service", None) else None,
                "text": text_html,
                "author": getattr(msg, "author_signature", None),
                "views": getattr(msg, "views", None),
                "forwards": getattr(msg, "forwards", None),
                "forwarded_from": _extract_forward_info(msg),
                "media_group_id": str(media_group_id) if media_group_id is not None else None,
                "reply_id": getattr(msg, "reply_to_message_id", None),
                "quote": quote,
                "reactions": reactions,
                "show_caption_above_media": bool(getattr(msg, "show_caption_above_media", False)),
                "has_media_spoiler": bool(getattr(msg, "has_media_spoiler", False)),
                "images": images,
                "files": files,
                "poll": poll_dict,
            }

            if media_group_id is not None:
                mg_key = str(media_group_id)
                if mg_key in grouped:
                    parent = grouped[mg_key]
                    parent["tg_all_msg_ids"].append(msg_id)
                    if not parent.get("text") and text_html:
                        parent["text"] = text_html
                    parent["images"].extend(images)
                    parent["files"].extend(files)
                    if reactions and not parent.get("reactions"):
                        parent["reactions"] = reactions
                    if post_dict["views"] is not None:
                        parent["views"] = post_dict["views"]
                    if post_dict["forwards"] is not None:
                        parent["forwards"] = post_dict["forwards"]
                    continue
                grouped[mg_key] = post_dict

            posts_out.append(post_dict)

        return posts_out

    async def _resolve_channel_for_chat(
        self,
        session: AsyncSession,
        tg_chat_id: int | None,
    ) -> Channel | None:
        """Resolve Channel strictly by `tg_chat_id` when provided (never fall back on mismatch!)."""
        if tg_chat_id is not None:
            res = await session.execute(select(Channel).where(Channel.tg_chat_id == tg_chat_id))
            return res.scalar_one_or_none()
        return await get_active_channel(session)

    async def _find_post_by_media_group(
        self,
        session: AsyncSession,
        channel_id: int,
        tg_media_group_id: str,
    ) -> Post | None:
        stmt = (
            select(Post)
            .options(
                selectinload(Post.images),
                selectinload(Post.tg_reactions),
                selectinload(Post.web_reactions),
                selectinload(Post.poll).selectinload(Poll.options),
                selectinload(Post.poll).selectinload(Poll.web_votes),
            )
            .where(
                Post.channel_id == channel_id,
                Post.tg_media_group_id == tg_media_group_id,
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _prepare_incoming_image_dict(
        self,
        raw_img: dict[str, Any],
        *,
        sort_order: int,
        prev_img: PostImage | None,
    ) -> tuple[dict[str, Any], int | None]:
        img_url = str(raw_img["url"])
        thumb_url = raw_img.get("thumb_url") or raw_img.get("thumb")
        img_tg_id = raw_img.get("tg_msg_id")
        if img_tg_id is None:
            stem = Path(img_url).stem
            if stem.isdigit():
                img_tg_id = int(stem)
        img_tg_int = int(img_tg_id) if img_tg_id is not None else None

        # Preserve previously upgraded Workflow B original if already applied
        if prev_img is not None and prev_img.original_url:
            return (
                {
                    "sort_order": sort_order,
                    "tg_msg_id": img_tg_int or prev_img.tg_msg_id,
                    "url": prev_img.url,
                    "thumb_url": prev_img.thumb_url,
                    "original_url": prev_img.original_url,
                    "original_size": prev_img.original_size,
                    "original_name": prev_img.original_name,
                    "size": prev_img.size,
                    "width": prev_img.width,
                    "height": prev_img.height,
                    "spoiler": bool(raw_img.get("spoiler", prev_img.spoiler)),
                    "phash": prev_img.phash,
                    "rgb_sig": prev_img.rgb_sig,
                },
                img_tg_int,
            )

        width = int(raw_img.get("width") or 0)
        height = int(raw_img.get("height") or 0)
        phash: str | None = raw_img.get("phash")
        rgb_sig: bytes | None = raw_img.get("rgb_sig")

        media_root_resolved = self.settings.media_dir.resolve()
        rel_clean = img_url.lstrip("./").lstrip("/")
        if rel_clean and not rel_clean.startswith(("http://", "https://")):
            local_path = (media_root_resolved / rel_clean).resolve()
            if (
                local_path.is_relative_to(media_root_resolved)
                and local_path.is_file()
                and (phash is None or rgb_sig is None)
            ):
                try:
                    phash, rgb_sig, det_w, det_h = await asyncio.to_thread(
                        compute_image_fingerprint, local_path
                    )
                    if width <= 0:
                        width = det_w
                    if height <= 0:
                        height = det_h
                except Exception:
                    logger.exception("Failed fingerprinting %s", local_path)

        return (
            {
                "sort_order": sort_order,
                "tg_msg_id": img_tg_int,
                "url": img_url,
                "thumb_url": thumb_url,
                "size": raw_img.get("size"),
                "width": width,
                "height": height,
                "spoiler": bool(raw_img.get("spoiler", False)),
                "phash": phash,
                "rgb_sig": rgb_sig,
            },
            img_tg_int,
        )

    async def _ingest_channel_messages(
        self,
        msgs: list[Any],
        *,
        is_edit: bool = False,
        trigger_prompt: bool = True,
    ) -> list[Post]:
        if not msgs:
            return []

        first_chat_id = getattr(getattr(msgs[0], "chat", None), "id", None)
        async with self.session_factory() as session:
            channel = await self._resolve_channel_for_chat(session, first_chat_id)
            if channel is None:
                logger.warning("No matching channel found for incoming chat_id=%s", first_chat_id)
                return []

            converted = await self._convert_channel_messages(msgs, channel)
            if not converted:
                return []

            mg_ids_map: dict[str, list[int]] = {}
            for raw_m in msgs:
                mg = getattr(raw_m, "media_group_id", None)
                if mg is not None:
                    mg_ids_map.setdefault(str(mg), []).append(int(raw_m.id))

            ingested_posts: list[tuple[Post, bool]] = []

            for raw_post in converted:
                tg_msg_id = int(raw_post["id"])
                mg_id = (
                    str(raw_post["media_group_id"])
                    if raw_post.get("media_group_id") is not None
                    else None
                )

                existing = await find_post_by_channel_tg_msg(session, channel.id, tg_msg_id)
                merged_split_album = False
                if existing is None and mg_id is not None:
                    # Slow split album: merge late-arriving siblings into existing Post by media_group_id
                    existing = await self._find_post_by_media_group(session, channel.id, mg_id)
                    if existing is not None:
                        merged_split_album = True

                was_new = existing is None

                all_ids: set[int] = {tg_msg_id}
                for mid in raw_post.get("tg_all_msg_ids") or []:
                    all_ids.add(int(mid))
                if mg_id and mg_id in mg_ids_map:
                    all_ids.update(mg_ids_map[mg_id])
                if existing and existing.tg_all_msg_ids:
                    all_ids.update(int(x) for x in existing.tg_all_msg_ids)

                # Extract & merge images safely (preserving album siblings on single-message edits & split albums!)
                raw_images = raw_post.get("images")
                if raw_images is None and raw_post.get("image"):
                    raw_images = [raw_post["image"]]

                images_payload: list[dict[str, Any]] | None = None
                if raw_images:
                    existing_sorted = (
                        sorted(existing.images, key=lambda i: i.sort_order)
                        if existing is not None
                        else []
                    )
                    existing_by_order = {ex_img.sort_order: ex_img for ex_img in existing_sorted}
                    existing_by_tg_id = {
                        int(ex_img.tg_msg_id): ex_img
                        for ex_img in existing_sorted
                        if ex_img.tg_msg_id is not None
                    }

                    is_partial_album_merge = existing is not None and (
                        merged_split_album
                        or (
                            is_edit
                            and (
                                existing.tg_media_group_id is not None
                                or len(existing_sorted) > len(raw_images)
                            )
                        )
                    )

                    if is_partial_album_merge:
                        merged_list: list[dict[str, Any]] = [
                            _post_image_to_dict(ex) for ex in existing_sorted
                        ]
                        idx_by_tg_id = {
                            int(item["tg_msg_id"]): pos
                            for pos, item in enumerate(merged_list)
                            if item.get("tg_msg_id") is not None
                        }
                        for raw_img in raw_images:
                            if not isinstance(raw_img, dict) or not raw_img.get("url"):
                                continue
                            raw_tg_id = raw_img.get("tg_msg_id")
                            if raw_tg_id is None:
                                stem = Path(str(raw_img["url"])).stem
                                if stem.isdigit():
                                    raw_tg_id = int(stem)
                            raw_tg_int = int(raw_tg_id) if raw_tg_id is not None else None
                            prev_img = (
                                existing_by_tg_id.get(raw_tg_int)
                                if raw_tg_int is not None
                                else None
                            )

                            if raw_tg_int is not None and raw_tg_int in idx_by_tg_id:
                                pos = idx_by_tg_id[raw_tg_int]
                                updated_dict, img_tg_int = await self._prepare_incoming_image_dict(
                                    raw_img,
                                    sort_order=pos,
                                    prev_img=prev_img,
                                )
                                if img_tg_int is not None:
                                    all_ids.add(img_tg_int)
                                merged_list[pos] = updated_dict
                            else:
                                new_pos = len(merged_list)
                                new_dict, img_tg_int = await self._prepare_incoming_image_dict(
                                    raw_img,
                                    sort_order=new_pos,
                                    prev_img=None,
                                )
                                if img_tg_int is not None:
                                    all_ids.add(img_tg_int)
                                merged_list.append(new_dict)
                                if img_tg_int is not None:
                                    idx_by_tg_id[img_tg_int] = new_pos

                        for pos, item in enumerate(merged_list):
                            item["sort_order"] = pos
                        images_payload = merged_list
                    else:
                        images_payload = []
                        for idx, raw_img in enumerate(raw_images):
                            if not isinstance(raw_img, dict) or not raw_img.get("url"):
                                continue
                            raw_tg_id = raw_img.get("tg_msg_id")
                            if raw_tg_id is None:
                                stem = Path(str(raw_img["url"])).stem
                                if stem.isdigit():
                                    raw_tg_id = int(stem)
                            prev_img = (
                                existing_by_tg_id.get(int(raw_tg_id))
                                if raw_tg_id is not None
                                else None
                            ) or existing_by_order.get(idx)

                            img_dict, img_tg_int = await self._prepare_incoming_image_dict(
                                raw_img,
                                sort_order=idx,
                                prev_img=prev_img,
                            )
                            if img_tg_int is not None:
                                all_ids.add(img_tg_int)
                            images_payload.append(img_dict)
                elif (is_edit or merged_split_album) and existing is not None:
                    images_payload = None

                # Separate non-poll files and poll dict
                raw_files = raw_post.get("files") or []
                other_files: list[dict[str, Any]] = []
                poll_payload: dict[str, Any] | None = raw_post.get("poll")
                for f in raw_files:
                    if not isinstance(f, dict):
                        continue
                    if f.get("media_type") == "poll" and poll_payload is None:
                        poll_payload = f
                    else:
                        other_files.append(f)

                # Resolve reply_to_post_id within same channel
                reply_to_post_id = existing.reply_to_post_id if existing else None
                reply_snapshot = raw_post.get("reply") or (
                    existing.reply_snapshot if existing else None
                )
                reply_tg_id = raw_post.get("reply_id")
                if reply_tg_id is not None:
                    target_parent = await find_post_by_channel_tg_msg(
                        session, channel.id, int(reply_tg_id)
                    )
                    if target_parent is not None:
                        reply_to_post_id = target_parent.id

                post_date = raw_post.get("date")
                if isinstance(post_date, str):
                    post_date = datetime.fromisoformat(post_date.replace("Z", "+00:00"))
                post_date = _ensure_utc(post_date)

                edited_at = raw_post.get("edited_at")
                if is_edit and edited_at is None:
                    edited_at = datetime.now(UTC)

                # Preserve existing fields when an edited album sibling or late sibling omits them
                incoming_text = raw_post.get("text")
                if existing is not None and (is_edit or merged_split_album) and incoming_text is None:
                    effective_text_html = existing.text_html
                else:
                    effective_text_html = incoming_text

                effective_views = (
                    raw_post.get("views")
                    if raw_post.get("views") is not None
                    else (existing.views if existing is not None else None)
                )
                effective_forwards = (
                    raw_post.get("forwards")
                    if raw_post.get("forwards") is not None
                    else (existing.forwards if existing is not None else None)
                )
                effective_author = (
                    raw_post.get("author")
                    if raw_post.get("author") is not None
                    else (existing.author if existing is not None else None)
                )
                effective_forwarded_from = (
                    raw_post.get("forwarded_from")
                    if raw_post.get("forwarded_from") is not None
                    else (existing.forwarded_from if existing is not None else None)
                )
                effective_quote = (
                    raw_post.get("quote")
                    if raw_post.get("quote") is not None
                    else (existing.quote if existing is not None else None)
                )

                upserted = await upsert_post(
                    session,
                    channel=channel,
                    canonical_id=existing.id if existing is not None else None,
                    tg_msg_id=(
                        existing.tg_msg_id
                        if ((is_edit or merged_split_album) and existing is not None)
                        else tg_msg_id
                    ),
                    tg_media_group_id=mg_id or (existing.tg_media_group_id if existing else None),
                    tg_all_msg_ids=sorted(all_ids),
                    tg_discussion_root_id=existing.tg_discussion_root_id if existing else None,
                    date=existing.date if ((is_edit or merged_split_album) and existing is not None) else post_date,
                    edited_at=edited_at if edited_at is not None else (existing.edited_at if existing else None),
                    is_deleted=False,
                    post_type=raw_post.get("type") or (existing.type if existing else None),
                    text_html=effective_text_html,
                    author=effective_author,
                    views=effective_views,
                    forwards=effective_forwards,
                    reply_to_post_id=reply_to_post_id,
                    forwarded_from=effective_forwarded_from,
                    reply_snapshot=reply_snapshot,
                    quote=effective_quote,
                    files=(
                        other_files
                        if other_files
                        else (existing.files if (is_edit or merged_split_album) and existing else None)
                    ),
                    show_caption_above_media=bool(
                        raw_post.get("show_caption_above_media")
                        or (existing.show_caption_above_media if existing else False)
                    ),
                    has_media_spoiler=bool(
                        raw_post.get("has_media_spoiler")
                        or (existing.has_media_spoiler if existing else False)
                    ),
                    images=images_payload,
                    tg_reactions=raw_post.get("reactions"),
                    poll=poll_payload,
                )
                ingested_posts.append((upserted, was_new))

            ingested_ids = [(post_obj.id, was_new) for post_obj, was_new in ingested_posts]
            await session.commit()
            session.expire_all()
            await generate_exports(session, self.settings)

            refreshed_posts: list[Post] = []
            for post_id, was_new in ingested_ids:
                refreshed = await get_post_by_id(session, post_id, include_deleted=False)
                if refreshed is None:
                    continue
                refreshed_posts.append(refreshed)
                event_name = "post.created" if (was_new and not is_edit) else "post.updated"
                await self._emit_sse(
                    event_name,
                    serialize_post(refreshed, api_prefix=self.settings.API_PREFIX),
                )
                if was_new and not is_edit and trigger_prompt and refreshed.images:
                    await self.bridge.prompt_original_photos(refreshed.id)

            return refreshed_posts

    # ------------------------------------------------------------------
    # Discussion Group (Auto-Forward Linking + 2-Way Incoming Comments)
    # ------------------------------------------------------------------

    async def _cache_user_avatar(self, tg_user: Any) -> str | None:
        if tg_user is None:
            return None
        user_id = getattr(tg_user, "id", None)
        if user_id is None:
            return None
        rel_path = f"avatars/{int(user_id)}.jpg"
        abs_path = self.settings.media_dir / rel_path
        if abs_path.is_file():
            return rel_path

        photo = getattr(tg_user, "photo", None)
        if photo is None or self.client is None or not hasattr(self.client, "download_media"):
            return None

        file_id = getattr(photo, "small_file_id", None) or getattr(photo, "big_file_id", None)
        if not file_id:
            return None

        tmp_path = abs_path.parent / f".{user_id}.{uuid.uuid4().hex[:8]}.tmp.jpg"
        try:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            dl = await self.client.download_media(file_id, file_name=str(tmp_path))
            if dl:
                dl_path = Path(str(dl))
                if dl_path.is_file():
                    os.replace(dl_path, abs_path)
                    return rel_path
        except Exception:
            logger.debug("Could not cache avatar for TG user %s", user_id, exc_info=True)
            tmp_path.unlink(missing_ok=True)
        return None

    async def handle_discussion_message(self, msg: Any) -> Comment | None:
        """Process auto-forwarded channel roots and regular discussion comments with strict loop protection."""
        if getattr(msg, "service", None):
            return None

        disc_chat_id = getattr(getattr(msg, "chat", None), "id", None)
        async with self.session_factory() as session:
            channel: Channel | None = None
            if disc_chat_id is not None:
                res = await session.execute(
                    select(Channel).where(Channel.tg_discussion_chat_id == disc_chat_id)
                )
                channel = res.scalar_one_or_none()
            else:
                channel = await get_active_channel(session)
            if channel is None:
                return None

            # 1. Detect automatic channel forward -> link posts.tg_discussion_root_id
            fwd_channel_msg_id = self._extract_autoforward_channel_msg_id(msg, channel)
            if fwd_channel_msg_id is not None:
                target_post = await find_post_by_channel_tg_msg(
                    session, channel.id, fwd_channel_msg_id
                )
                if target_post is not None and target_post.tg_discussion_root_id is None:
                    target_post.tg_discussion_root_id = int(msg.id)
                    await session.commit()
                    logger.info(
                        "Linked discussion root msg %s -> post #%s",
                        msg.id,
                        target_post.id,
                    )
                return None

            # 2. Strict loop guard #1: ignore messages echoed by our own bot!
            from_user = getattr(msg, "from_user", None)
            if from_user is not None:
                if bool(getattr(from_user, "is_self", False)):
                    return None
                if self.bot_id is not None and int(getattr(from_user, "id", 0)) == int(self.bot_id):
                    return None

            msg_id = int(getattr(msg, "id", 0) or 0)
            if msg_id <= 0:
                return None

            # 3. Strict loop guard #2: deduplicate by UNIQUE(channel_id, tg_msg_id)
            existing_comment = (
                await session.execute(
                    select(Comment).where(
                        Comment.channel_id == channel.id,
                        Comment.tg_msg_id == msg_id,
                    )
                )
            ).scalar_one_or_none()
            if existing_comment is not None:
                return None

            # 4. Resolve post_id & parent_id via reply_to_top_message_id / reply_to_message_id walk
            post_id: int | None = None
            parent_comment: Comment | None = None

            reply_to_top_id = getattr(msg, "reply_to_top_message_id", None)
            reply_to_msg_id = getattr(msg, "reply_to_message_id", None)

            if reply_to_msg_id is not None:
                parent_comment = (
                    await session.execute(
                        select(Comment).where(
                            Comment.channel_id == channel.id,
                            Comment.tg_msg_id == int(reply_to_msg_id),
                        )
                    )
                ).scalar_one_or_none()
                if parent_comment is not None:
                    post_id = parent_comment.post_id
                else:
                    direct_post = (
                        await session.execute(
                            select(Post).where(
                                Post.channel_id == channel.id,
                                Post.tg_discussion_root_id == int(reply_to_msg_id),
                            )
                        )
                    ).scalar_one_or_none()
                    if direct_post is not None:
                        post_id = direct_post.id

            if post_id is None and reply_to_top_id is not None:
                root_post = (
                    await session.execute(
                        select(Post).where(
                            Post.channel_id == channel.id,
                            Post.tg_discussion_root_id == int(reply_to_top_id),
                        )
                    )
                ).scalar_one_or_none()
                if root_post is not None:
                    post_id = root_post.id

            if post_id is None:
                return None

            # 5. Extract & sanitize comment body (render entities BEFORE stripping so UTF-16 offsets stay exact!)
            raw_text = getattr(msg, "text", None) or getattr(msg, "caption", None) or ""
            raw_text_str = str(raw_text)
            entities = (
                getattr(msg, "entities", None)
                if getattr(msg, "text", None)
                else getattr(msg, "caption_entities", None)
            )
            body_text = raw_text_str.strip()
            media_payload: dict[str, Any] | None = None

            if not body_text:
                if getattr(msg, "sticker", None) is not None:
                    emoji = getattr(msg.sticker, "emoji", "") or ""
                    body_text = f"[Sticker {emoji}]".strip()
                    media_payload = {"media_type": "sticker", "sticker_emoji": emoji}
                elif getattr(msg, "photo", None) is not None:
                    body_text = "[Photo]"
                    media_payload = {"media_type": "photo"}
                elif getattr(msg, "document", None) is not None:
                    body_text = "[File]"
                    media_payload = {"media_type": "document"}
                else:
                    return None
                body_html = sanitize_tg_comment_html(f"<em>{html.escape(body_text)}</em>")
            else:
                raw_html = _render_entities_to_html(raw_text_str, entities)
                body_html = sanitize_tg_comment_html(raw_html).strip()

            # 6. Author metadata + optional linked web user + lazy avatar cache
            tg_author_id: int | None = None
            tg_author_name: str | None = None
            tg_author_username: str | None = None
            tg_author_avatar: str | None = None
            linked_user: User | None = None

            if from_user is not None:
                tg_author_id = int(from_user.id)
                first = getattr(from_user, "first_name", None) or ""
                last = getattr(from_user, "last_name", None) or ""
                tg_author_name = (
                    " ".join(p for p in (first, last) if p).strip()
                    or getattr(from_user, "username", None)
                    or f"User {tg_author_id}"
                )
                tg_author_username = getattr(from_user, "username", None)
                tg_author_avatar = await self._cache_user_avatar(from_user)

                identity = await session.get(UserIdentity, ("telegram", str(tg_author_id)))
                if identity is not None:
                    linked_user = await session.get(User, identity.user_id)
            else:
                sender_chat = getattr(msg, "sender_chat", None)
                if sender_chat is not None:
                    tg_author_id = int(sender_chat.id)
                    tg_author_name = getattr(sender_chat, "title", "Channel")
                    tg_author_username = getattr(sender_chat, "username", None)

            created_at = _ensure_utc(getattr(msg, "date", None))
            comment = Comment(
                post_id=post_id,
                parent_id=parent_comment.id if parent_comment is not None else None,
                source="telegram",
                channel_id=channel.id,
                tg_msg_id=msg_id,
                user_id=linked_user.id if linked_user is not None else None,
                tg_author_id=tg_author_id,
                tg_author_name=tg_author_name,
                tg_author_username=tg_author_username,
                tg_author_avatar=tg_author_avatar,
                body_text=body_text,
                body_html=body_html,
                media=media_payload,
                created_at=created_at,
            )
            session.add(comment)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                return None

            # 7. Enqueue debounced SMTP reply notification if parent belongs to verified web user
            if parent_comment is not None and parent_comment.user_id is not None:
                if linked_user is None or parent_comment.user_id != linked_user.id:
                    recipient = await session.get(User, parent_comment.user_id)
                    if (
                        recipient is not None
                        and recipient.email
                        and recipient.email_verified
                        and recipient.notify_replies
                    ):
                        send_after = datetime.now(UTC) + timedelta(
                            seconds=self.settings.EMAIL_DEBOUNCE_SECONDS
                        )
                        session.add(
                            NotificationQueue(
                                recipient_user_id=recipient.id,
                                comment_id=comment.id,
                                post_id=post_id,
                                send_after=send_after,
                            )
                        )

            await session.commit()

            # 8. Emit SSE comment.created (raw public CommentDTO directly)
            comment_dto = self._serialize_tg_comment_dto(comment, linked_user)
            await self._emit_sse("comment.created", comment_dto)
            return comment

    @staticmethod
    def _extract_autoforward_channel_msg_id(msg: Any, channel: Channel) -> int | None:
        """Return original channel message ID ONLY if `msg` is an automatic forward from `channel`."""
        if channel.tg_chat_id is None:
            return None

        is_auto = bool(getattr(msg, "is_automatic_forward", False))
        if not is_auto:
            return None

        fwd_chat = getattr(msg, "forward_from_chat", None)
        fwd_msg_id = getattr(msg, "forward_from_message_id", None)

        origin = getattr(msg, "forward_origin", None)
        origin_chat = getattr(origin, "chat", None) if origin is not None else None
        origin_msg_id = getattr(origin, "message_id", None) if origin is not None else None

        src_chat_id = (
            getattr(fwd_chat, "id", None)
            if fwd_chat is not None
            else getattr(origin_chat, "id", None)
        )
        src_msg_id = fwd_msg_id if fwd_msg_id is not None else origin_msg_id

        if src_msg_id is None or src_chat_id is None:
            return None

        if int(src_chat_id) == int(channel.tg_chat_id):
            return int(src_msg_id)
        return None

    def _serialize_tg_comment_dto(
        self,
        comment: Comment,
        linked_user: User | None,
    ) -> dict[str, Any]:
        is_owner = (
            (linked_user is not None and linked_user.role == "owner")
            or (
                self.settings.OWNER_TG_ID is not None
                and comment.tg_author_id is not None
                and int(comment.tg_author_id) == int(self.settings.OWNER_TG_ID)
            )
        )
        avatar_url = (
            resolve_media_url(comment.tg_author_avatar, self.settings.API_PREFIX)
            if comment.tg_author_avatar
            else (linked_user.avatar_url if linked_user is not None else None)
        )
        display_name = (
            linked_user.display_name
            if linked_user is not None
            else (comment.tg_author_name or "Telegram User")
        )
        return {
            "id": comment.id,
            "post_id": comment.post_id,
            "parent_id": comment.parent_id,
            "source": comment.source,
            "author": {
                "display_name": display_name,
                "avatar_url": avatar_url,
                "role": "owner" if is_owner else "user",
                "provider": "telegram",
                "username": comment.tg_author_username,
                "verified": True,
            },
            "body_text": comment.body_text,
            "body_html": comment.body_html,
            "media": comment.media,
            "created_at": format_iso_utc(comment.created_at),
            "edited_at": format_iso_utc(comment.edited_at) if comment.edited_at else None,
            "deleted": comment.deleted_at is not None,
            "is_mine": False,
            "can_delete": False,
            "reactions": [],
        }

    # ------------------------------------------------------------------
    # Live Reaction Count & Poll Updates
    # ------------------------------------------------------------------

    @staticmethod
    async def _replace_tg_reactions(
        session: AsyncSession,
        *,
        model: type[TgReaction] | type[CommentTgReaction],
        fk_col_name: str,
        target_id: int,
        raw_reactions: Sequence[Any],
    ) -> None:
        fk_col = getattr(model, fk_col_name)
        await session.execute(delete(model).where(fk_col == target_id))
        seen: set[str] = set()
        for r in raw_reactions:
            emoji = getattr(r, "emoji", None)
            cid = getattr(r, "custom_emoji_id", None)
            cid_str = str(cid) if cid is not None else None
            if not emoji and not cid_str:
                continue
            rkey = make_reaction_key(emoji=emoji, custom_emoji_id=cid_str)
            if rkey in seen:
                continue
            seen.add(rkey)
            session.add(
                model(
                    **{fk_col_name: target_id},
                    reaction_key=rkey,
                    emoji=emoji,
                    custom_emoji_id=cid_str,
                    count=int(getattr(r, "count", 0) or 0),
                )
            )

    async def handle_reaction_count_update(self, update: Any) -> None:
        chat_id = getattr(getattr(update, "chat", None), "id", None)
        msg_id = getattr(update, "message_id", None)
        if msg_id is None or chat_id is None:
            return

        chat_id_int = int(chat_id)
        raw_reactions = getattr(update, "reactions", None) or []

        async with self.session_factory() as session:
            channel = await self._resolve_channel_for_chat(session, chat_id_int)
            if channel is not None:
                post = await find_post_by_channel_tg_msg(session, channel.id, int(msg_id))
                if post is None:
                    # Channel-chat reaction for an untracked/service message must never
                    # collide with a discussion Comment sharing the same tg_msg_id.
                    return
                post_id = post.id
                await self._replace_tg_reactions(
                    session,
                    model=TgReaction,
                    fk_col_name="post_id",
                    target_id=post_id,
                    raw_reactions=raw_reactions,
                )
                await session.commit()
                session.expire_all()

                refreshed = await get_post_by_id(session, post_id, include_deleted=False)
                if refreshed is None:
                    return
                merged = compute_merged_reactions(refreshed)
                public_reactions = [
                    {k: v for k, v in item.items() if k != "reacted"} for item in merged
                ]
                await self._emit_sse(
                    "reaction.updated",
                    {"post_id": refreshed.id, "reactions": public_reactions},
                )
                return

            disc_channel = (
                await session.execute(
                    select(Channel).where(Channel.tg_discussion_chat_id == chat_id_int)
                )
            ).scalar_one_or_none()
            if disc_channel is None:
                return

            comment = (
                await session.execute(
                    select(Comment)
                    .join(Post, Post.id == Comment.post_id)
                    .where(
                        Comment.channel_id == disc_channel.id,
                        Comment.tg_msg_id == int(msg_id),
                        Comment.deleted_at.is_(None),
                        Post.is_deleted.is_(False),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if comment is None:
                return

            comment_id = comment.id
            await self._replace_tg_reactions(
                session,
                model=CommentTgReaction,
                fk_col_name="comment_id",
                target_id=comment_id,
                raw_reactions=raw_reactions,
            )
            await session.commit()
            session.expire_all()

            refreshed_comment = (
                await session.execute(
                    select(Comment)
                    .options(
                        selectinload(Comment.tg_reactions),
                        selectinload(Comment.web_reactions),
                    )
                    .where(Comment.id == comment_id, Comment.deleted_at.is_(None))
                )
            ).scalar_one_or_none()
            if refreshed_comment is None:
                return
            merged = compute_merged_comment_reactions(refreshed_comment)
            public_reactions = [
                {k: v for k, v in item.items() if k != "reacted"} for item in merged
            ]
            await self._emit_sse(
                "comment.reaction.updated",
                {
                    "post_id": refreshed_comment.post_id,
                    "comment_id": refreshed_comment.id,
                    "reactions": public_reactions,
                },
            )

    async def handle_poll_update(self, tg_poll: Any) -> None:
        poll_id = str(getattr(tg_poll, "id", "") or "")
        if not poll_id:
            return

        async with self.session_factory() as session:
            poll = await session.get(Poll, poll_id)
            if poll is None:
                return

            post_id = poll.post_id
            poll.is_closed = bool(getattr(tg_poll, "is_closed", poll.is_closed))
            tg_options = getattr(tg_poll, "options", None) or []
            for idx, opt in enumerate(tg_options):
                opt_row = await session.get(PollOption, (poll.id, idx))
                if opt_row is not None:
                    opt_row.tg_voter_count = int(getattr(opt, "voter_count", 0) or 0)
            await session.commit()
            session.expire_all()

            refreshed_post = await get_post_by_id(session, post_id, include_deleted=False)
            if refreshed_post is None or refreshed_post.poll is None:
                return
            merged = compute_merged_poll(refreshed_post.poll)
            public_poll = {k: v for k, v in merged.items() if k != "my_votes"}
            await self._emit_sse(
                "poll.updated",
                {"post_id": refreshed_post.id, "poll": public_poll},
            )

    # ------------------------------------------------------------------
    # Workflow B: Owner Private DM Drop & Inline Confirm/Swap Buttons
    # ------------------------------------------------------------------

    @staticmethod
    def _is_image_dm_message(msg: Any) -> bool:
        doc = getattr(msg, "document", None)
        if doc is not None:
            mime = str(getattr(doc, "mime_type", "") or "").lower()
            fname = str(getattr(doc, "file_name", "") or "").lower()
            if mime.startswith("image/") and mime != "image/svg+xml":
                return True
            if Path(fname).suffix in _IMAGE_EXTENSIONS:
                return True
        if getattr(msg, "photo", None) is not None:
            return True
        return False

    async def handle_owner_dm(self, msg: Any) -> None:
        """Buffer original image files dropped by OWNER_TG_ID in private DM."""
        from_user = getattr(msg, "from_user", None)
        if (
            self.settings.OWNER_TG_ID is None
            or from_user is None
            or int(from_user.id) != int(self.settings.OWNER_TG_ID)
        ):
            return
        if not self._is_image_dm_message(msg):
            return

        doc = getattr(msg, "document", None)
        doc_size = getattr(doc, "file_size", None) if doc is not None else None
        if doc_size is not None and int(doc_size) > MAX_UPLOAD_BYTES:
            if self.client is not None:
                await self.client.send_message(
                    chat_id=self.settings.OWNER_TG_ID,
                    text=f"❌ File exceeds maximum allowed size ({MAX_UPLOAD_BYTES} bytes).",
                )
            return

        target_post_id = await self._resolve_dm_target_post_id(msg)
        if target_post_id is None:
            if self.client is not None:
                await self.client.send_message(
                    chat_id=self.settings.OWNER_TG_ID,
                    text="Could not find a recent photo post to attach these originals to. Please reply directly to a photo prompt message.",
                )
            return

        self.dm_buffer.add(f"dm:{from_user.id}:{target_post_id}", (target_post_id, msg))

    async def _resolve_dm_target_post_id(self, msg: Any) -> int | None:
        reply_id = getattr(msg, "reply_to_message_id", None)
        async with self.session_factory() as session:
            if reply_id is not None:
                res = await session.execute(
                    select(OriginalUploadToken)
                    .where(OriginalUploadToken.dm_msg_id == int(reply_id))
                    .limit(1)
                )
                tok = res.scalar_one_or_none()
                if tok is not None:
                    return tok.post_id

            stmt = (
                select(Post.id)
                .join(PostImage, PostImage.post_id == Post.id)
                .where(Post.is_deleted.is_(False))
                .order_by(Post.id.desc())
                .limit(1)
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    async def _on_dm_batch_flushed(self, items: list[tuple[int, Any]]) -> None:
        if not items or self.client is None or self.settings.OWNER_TG_ID is None:
            return

        target_post_id = items[0][0]
        msgs = [m for _, m in items]

        staging_id = str(uuid.uuid4())
        staging_dir = self.settings.DATA_DIR / "staging" / staging_id
        staging_dir.mkdir(parents=True, exist_ok=True)
        keep_staging = False

        async with self.session_factory() as session:
            post = await get_post_by_id(session, target_post_id, include_deleted=False)
            if post is None or not post.images:
                await asyncio.to_thread(shutil.rmtree, staging_dir, True)
                return

            candidates = []
            manifest_files: dict[str, dict[str, Any]] = {}
            try:
                for idx, msg in enumerate(msgs):
                    doc = getattr(msg, "document", None)
                    doc_size = getattr(doc, "file_size", None) if doc is not None else None
                    if doc_size is not None and int(doc_size) > MAX_UPLOAD_BYTES:
                        raise InvalidImageError(
                            f"File exceeds maximum allowed size ({MAX_UPLOAD_BYTES} bytes)"
                        )
                    raw_name = (
                        getattr(doc, "file_name", None)
                        if doc is not None
                        else f"photo_{getattr(msg, 'id', idx)}.jpg"
                    )
                    file_key = f"dm_{idx}_{uuid.uuid4().hex[:8]}"
                    dl_target = staging_dir / f"{file_key}_{sanitize_original_filename(raw_name)}"
                    dl_result = await self.client.download_media(msg, file_name=str(dl_target))
                    if not dl_result:
                        continue
                    dl_path = Path(str(dl_result))
                    info = await asyncio.to_thread(validate_image_file, dl_path)
                    safe_name = sanitize_original_filename(raw_name, fallback_ext=info.extension)
                    cand = await asyncio.to_thread(
                        build_candidate_photo, file_key, safe_name, dl_path
                    )
                    candidates.append(cand)
                    manifest_files[file_key] = {
                        "filename": safe_name,
                        "stored_name": dl_path.name,
                        "size": cand.size,
                    }

                if not candidates:
                    return

                targets = build_target_post_images(post)
                result = await asyncio.to_thread(
                    match_originals_to_post_images,
                    candidates,
                    targets,
                    media_root=self.settings.media_dir,
                )

                if result.status == "confident":
                    post_id = post.id
                    image_by_id = {img.id: img for img in post.images}
                    cand_by_key = {c.file_key: c for c in candidates}
                    mapping = [
                        (
                            cand_by_key[pm.file_key].path,
                            cand_by_key[pm.file_key].filename,
                            image_by_id[pm.image_id],
                        )
                        for pm in result.matches
                    ]
                    applied = await apply_original_matches(
                        session,
                        self.settings,
                        post,
                        mapping,
                        sse_publish=self.sse_publish,
                    )
                    summary = "\n".join(
                        f"• Photo #{item['sort_order'] + 1} ← {item['original_name']}"
                        for item in applied
                    )
                    await self.client.send_message(
                        chat_id=self.settings.OWNER_TG_ID,
                        text=f"✅ Applied {len(applied)} original(s) to Post #{post_id}:\n{summary}",
                    )
                    return

                # Ambiguous -> save manifest and send Confirm / Swap / Cancel inline buttons
                manifest = {
                    "staging_id": staging_id,
                    "post_id": post.id,
                    "owner_tg_id": self.settings.OWNER_TG_ID,
                    "created_at": datetime.now(UTC).isoformat(),
                    "files": manifest_files,
                    "proposed": [pm.to_dict() for pm in result.matches],
                }
                manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
                await asyncio.to_thread((staging_dir / "manifest.json").write_bytes, manifest_bytes)
                keep_staging = True

                lines = [
                    f"⚠️ Ambiguous photo match for Post #{post.id} (similar/burst shots detected):",
                    "",
                ]
                for pm in result.matches:
                    lines.append(
                        f"• {pm.filename} → Photo #{pm.sort_order + 1} "
                        f"(dist={pm.distance:.3f}"
                        + (f", margin={pm.margin:.3f}" if pm.margin is not None else "")
                        + ")"
                    )

                reply_markup = self._build_dm_confirm_markup(staging_id, len(result.matches))
                await self.client.send_message(
                    chat_id=self.settings.OWNER_TG_ID,
                    text="\n".join(lines),
                    reply_markup=reply_markup,
                )
            except InvalidImageError as exc:
                await self.client.send_message(
                    chat_id=self.settings.OWNER_TG_ID,
                    text=f"❌ Rejected invalid image file: {exc}",
                )
            except Exception:
                logger.exception("Failed processing owner DM batch for post %s", target_post_id)
            finally:
                if not keep_staging:
                    await asyncio.to_thread(shutil.rmtree, staging_dir, True)

    @staticmethod
    def _build_dm_confirm_markup(staging_id: str, match_count: int) -> Any:
        try:
            from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            buttons = [InlineKeyboardButton("✅ Confirm", callback_data=f"wf_confirm:{staging_id}")]
            if match_count == 2:
                buttons.append(InlineKeyboardButton("🔄 Swap", callback_data=f"wf_swap:{staging_id}"))
            buttons.append(InlineKeyboardButton("❌ Cancel", callback_data=f"wf_cancel:{staging_id}"))
            return InlineKeyboardMarkup([buttons])
        except Exception:
            return None

    async def handle_callback_query(self, query: Any) -> None:
        """Handle inline [Confirm] / [Swap] / [Cancel] Workflow B callback buttons from OWNER_TG_ID."""
        from_user = getattr(query, "from_user", None)
        if (
            self.settings.OWNER_TG_ID is None
            or from_user is None
            or int(from_user.id) != int(self.settings.OWNER_TG_ID)
        ):
            return

        data = str(getattr(query, "data", "") or "")
        if ":" not in data:
            return
        action, staging_id = data.split(":", 1)
        if action not in ("wf_confirm", "wf_swap", "wf_cancel"):
            return

        try:
            canonical_id = str(uuid.UUID(staging_id))
        except ValueError:
            await query.answer("Invalid batch ID", show_alert=True)
            return

        staging_root = (self.settings.DATA_DIR / "staging").resolve()
        staging_dir = (staging_root / canonical_id).resolve()
        if not staging_dir.is_relative_to(staging_root):
            await query.answer("Invalid batch ID", show_alert=True)
            return

        manifest_path = staging_dir / "manifest.json"
        processing_path = staging_dir / "manifest.processing.json"

        # Atomically claim manifest to prevent TOCTOU double-click races
        try:
            await asyncio.to_thread(os.replace, manifest_path, processing_path)
        except FileNotFoundError:
            if processing_path.is_file():
                await query.answer("Already processing...", show_alert=False)
            else:
                await query.answer("Batch expired or already processed", show_alert=True)
            return

        def _restore_manifest_sync() -> None:
            if processing_path.is_file() and not manifest_path.exists():
                try:
                    os.replace(processing_path, manifest_path)
                except OSError:
                    pass

        try:
            manifest = json.loads(await asyncio.to_thread(processing_path.read_text, "utf-8"))
        except (OSError, json.JSONDecodeError):
            await asyncio.to_thread(_restore_manifest_sync)
            await query.answer("Corrupted batch manifest", show_alert=True)
            return

        if manifest.get("owner_tg_id") != self.settings.OWNER_TG_ID:
            await asyncio.to_thread(_restore_manifest_sync)
            await query.answer("Unauthorized batch", show_alert=True)
            return

        if action == "wf_cancel":
            await asyncio.to_thread(shutil.rmtree, staging_dir, True)
            await query.answer("Cancelled")
            if getattr(query, "message", None) is not None and hasattr(query.message, "edit_text"):
                await query.message.edit_text("❌ Original photo upload cancelled.")
            return

        proposed: list[dict[str, Any]] = list(manifest.get("proposed") or [])
        if action == "wf_swap" and len(proposed) == 2:
            proposed[0]["image_id"], proposed[1]["image_id"] = (
                proposed[1]["image_id"],
                proposed[0]["image_id"],
            )

        post_id = int(manifest["post_id"])
        manifest_files: dict[str, dict[str, Any]] = manifest.get("files") or {}

        try:
            async with self.session_factory() as session:
                post = await get_post_by_id(session, post_id, include_deleted=False)
                if post is None:
                    await asyncio.to_thread(shutil.rmtree, staging_dir, True)
                    await query.answer("Post no longer exists", show_alert=True)
                    return

                image_by_id = {img.id: img for img in post.images}
                mapping: list[tuple[Path, str, PostImage]] = []
                for item in proposed:
                    finfo = manifest_files.get(str(item["file_key"]))
                    target_img = image_by_id.get(int(item["image_id"]))
                    if finfo is None or target_img is None:
                        continue
                    staged_file = (staging_dir / Path(str(finfo["stored_name"])).name).resolve()
                    if staged_file.is_relative_to(staging_dir) and staged_file.is_file():
                        mapping.append(
                            (staged_file, str(finfo.get("filename") or staged_file.name), target_img)
                        )

                if not mapping:
                    await asyncio.to_thread(shutil.rmtree, staging_dir, True)
                    await query.answer("No valid files left to apply", show_alert=True)
                    return

                applied = await apply_original_matches(
                    session,
                    self.settings,
                    post,
                    mapping,
                    sse_publish=self.sse_publish,
                )
        except InvalidImageError as exc:
            await asyncio.to_thread(shutil.rmtree, staging_dir, True)
            await query.answer(f"Invalid image: {exc}", show_alert=True)
            return
        except Exception:
            await asyncio.to_thread(_restore_manifest_sync)
            logger.exception("Transient error applying Workflow B callback for post %s", post_id)
            await query.answer("Temporary error; please retry", show_alert=True)
            return

        await asyncio.to_thread(shutil.rmtree, staging_dir, True)
        await query.answer("Applied!")
        if getattr(query, "message", None) is not None and hasattr(query.message, "edit_text"):
            await query.message.edit_text(
                f"✅ Applied {len(applied)} confirmed original(s) to Post #{post_id}."
            )
