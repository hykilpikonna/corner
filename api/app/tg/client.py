from __future__ import annotations

import asyncio
import hashlib
import html
import inspect
import json
import logging
import mimetypes
import os
import shutil
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from PIL import Image, ImageOps
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.auth.security import sanitize_comment_html
from app.config import Settings
from app.db.models import (
    Channel,
    Comment,
    CommentTgReaction,
    CommentWebReaction,
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
    serialize_comment_media,
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
from app.media.stickers import (
    EMOJI_EXTENSIONS,
    STICKER_EXTENSIONS,
    list_sticker_packs,
    load_custom_emoji_meta,
    load_dismissed_default_packs,
    parse_sticker_set_ref,
    parse_sticker_set_url,
    remove_sticker_by_id,
    remove_sticker_pack,
    sanitize_sticker_filename,
    sanitize_sticker_pack_name,
    save_sticker_pack_manifest,
    upsert_sticker_manifest_entry,
)
from app.routes.upload import (
    SSEPublishFn,
    apply_original_matches,
    build_target_post_images,
    invoke_sse_publish,
)
from app.tg.bridge import TelegramBridge
from app.tg.reactions_cmd import (
    cache_custom_emoji_stickers,
    execute_owner_reaction_command,
)

logger = logging.getLogger(__name__)

_SAFE_URL_SCHEMES = frozenset({"http", "https", "mailto", "tg"})
_MAX_COMMENT_TG_MEDIA_BYTES = 20 * 1024 * 1024
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
    return sanitize_comment_html(
        raw_html,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
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
            parts.append(f'<span class="spoiler"><span>{esc}</span></span>')
        elif etype == "CUSTOM_EMOJI":
            cid = getattr(en, "custom_emoji_id", None)
            cid_str = str(cid).strip() if cid is not None else ""
            if cid_str.isascii() and cid_str.isdigit() and 1 <= len(cid_str) <= 32:
                parts.append(
                    f'<i class="custom-emoji" data-custom-emoji-id="{cid_str}">{esc}</i>'
                )
            else:
                parts.append(esc)
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


def _is_safe_http_url(raw_url: Any) -> str | None:
    """Return stripped HTTP(S) URL when valid and safe, else None."""
    if not isinstance(raw_url, str):
        return None
    stripped = raw_url.strip()
    if not stripped or stripped.startswith("//"):
        return None
    parsed = urlparse(stripped)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return None
    return stripped


def _is_inactive_web_page(msg: Any, wp: Any) -> bool:
    """Return True when Telegram link preview is absent, disabled, empty, or pending."""
    if wp is None:
        return True
    lpo = getattr(msg, "link_preview_options", None)
    if lpo is not None and bool(getattr(lpo, "is_disabled", False)):
        return True
    raw_outer = getattr(wp, "raw", None)
    raw_wp = getattr(raw_outer, "webpage", raw_outer)
    if raw_wp is not None:
        if type(raw_wp).__name__ in ("WebPageEmpty", "WebPagePending"):
            return True
        try:
            from pyrogram import raw as pyro_raw

            if isinstance(raw_wp, (pyro_raw.types.WebPageEmpty, pyro_raw.types.WebPagePending)):
                return True
        except Exception:
            pass
    return False


def _pick_photo_file_id(media_obj: Any) -> str | None:
    """Extract best downloadable file_id from a Pyrogram media or Photo object."""
    fid = getattr(media_obj, "file_id", None)
    if fid:
        return str(fid)
    thumbs = getattr(media_obj, "thumbs", None) or getattr(media_obj, "thumb", None)
    if not thumbs:
        return None
    thumb_obj = (
        max(thumbs, key=lambda t: int(getattr(t, "file_size", 0) or 0))
        if isinstance(thumbs, (list, tuple))
        else thumbs
    )
    thumb_fid = getattr(thumb_obj, "file_id", None)
    return str(thumb_fid) if thumb_fid else None


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
        "jxl_url": img.jxl_url,
        "thumb_jxl_url": img.thumb_jxl_url,
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
        user_client: Any | None = None,
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
            lookup_discussion_root=self.lookup_discussion_root_id,
        )
        if bridge is not None and getattr(bridge, "lookup_discussion_root", None) is None:
            bridge.set_discussion_lookup(self.lookup_discussion_root_id)
        self.client = client
        # Read-only MTProto user client: strictly for listening to polls, reactions, and deletions.
        # Never attach self.bridge or outbound write calls to user_client.
        self.user_client = user_client
        # Pre-populate bot_id from TG_BOT_TOKEN prefix so echo loop guard is active even during startup
        self.bot_id: int | None = (
            bot_id if bot_id is not None else _parse_bot_id_from_token(settings.TG_BOT_TOKEN)
        )
        self.bot_username: str | None = None
        self._started = False
        self._user_started = False
        self._handlers_registered = False
        self._user_handlers_registered = False
        self._default_stickers_task: asyncio.Task[Any] | None = None
        self._emoji_meta_task: asyncio.Task[Any] | None = None
        self._crawl_task: asyncio.Task[Any] | None = None
        self._unlinked_autoforwards: dict[tuple[int, int], int] = {}
        self._channel_ingest_locks: dict[int | str, asyncio.Lock] = {}

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
        self.discussion_album_buffer = AlbumBuffer(
            on_flush=self._on_discussion_album_flushed,
            debounce_seconds=album_debounce_seconds,
            max_wait_seconds=album_max_wait_seconds,
        )

    @property
    def is_running(self) -> bool:
        return self._started

    async def _emit_sse(self, event: str, data: dict[str, Any]) -> None:
        await invoke_sse_publish(self.sse_publish, event, data)

    def _has_user_session_config(self) -> bool:
        return bool(self.settings.TG_USER_SESSION_STRING or self.settings.TG_USER_SESSION_NAME)

    async def _start_user_client(self) -> None:
        if self._user_started:
            return
        if self.user_client is None:
            if not self._has_user_session_config():
                return
            if not self.settings.TG_API_ID or not self.settings.TG_API_HASH:
                raise ValueError(
                    "TG_API_ID and TG_API_HASH must be configured when TG_USER_SESSION_* is set"
                )
            from pyrogram import Client

            self.settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
            if self.settings.TG_USER_SESSION_STRING:
                self.user_client = Client(
                    name=self.settings.TG_USER_SESSION_NAME or "user_reader",
                    api_id=int(self.settings.TG_API_ID),
                    api_hash=str(self.settings.TG_API_HASH),
                    session_string=self.settings.TG_USER_SESSION_STRING,
                    in_memory=True,
                )
            else:
                self.user_client = Client(
                    name=str(self.settings.TG_USER_SESSION_NAME),
                    api_id=int(self.settings.TG_API_ID),
                    api_hash=str(self.settings.TG_API_HASH),
                    workdir=str(self.settings.DATA_DIR),
                )

        await self.user_client.start()
        user_me = await self.user_client.get_me()
        if getattr(user_me, "is_bot", False):
            logger.warning(
                "Configured user_client (%s) is a bot account; expected a user session for full poll/reaction/deletion events.",
                getattr(user_me, "id", None),
            )
        if hasattr(self.user_client, "get_chat"):
            for cid in (self.settings.TG_CHANNEL_ID, self.settings.TG_DISCUSSION_CHAT_ID):
                if cid is not None:
                    try:
                        await self.user_client.get_chat(cid)
                    except Exception:
                        logger.debug("user_client get_chat(%s) warmup failed", cid, exc_info=True)
        self._register_user_handlers()
        self._user_started = True
        logger.info(
            "TelegramService read-only user_client started as ID %s (@%s)",
            getattr(user_me, "id", None),
            getattr(user_me, "username", None),
        )

    async def start(self) -> None:
        """Start Kurigram bot client and optional read-only user client."""
        has_bot = bool(self.client is not None or self.settings.TG_BOT_TOKEN)
        has_user = bool(self.user_client is not None or self._has_user_session_config())
        if not has_bot and not has_user:
            logger.info("TG_BOT_TOKEN and TG_USER_SESSION_* are unset; TelegramService running in disabled no-op mode.")
            return

        if has_bot and not self._started:
            if self.client is None:
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
            self.bot_username = (
                str(getattr(me, "username", "") or "").strip().lstrip("@") or None
            )
            # Register handlers only AFTER bot_id is confirmed to eliminate startup echo race
            self._register_handlers()
            self._started = True
            logger.info("TelegramService started as bot ID %s (@%s)", me.id, getattr(me, "username", None))

            if hasattr(self.client, "set_my_commands"):
                try:
                    from pyrogram.types import BotCommand, BotCommandScopeChat

                    scope = (
                        BotCommandScopeChat(chat_id=self.settings.OWNER_TG_ID)
                        if self.settings.OWNER_TG_ID is not None
                        else None
                    )
                    commands = [
                        BotCommand("reactions", "View or edit allowed web reaction emojis"),
                        BotCommand("stickers", "List, import, or remove custom web stickers"),
                        BotCommand("fullsync", "Crawl and sync all active channel posts from start"),
                        BotCommand("help", "Show bot command help"),
                    ]
                    if scope is not None:
                        await self.client.set_my_commands(commands, scope=scope)
                except Exception:
                    logger.debug("Could not register bot commands menu", exc_info=True)

        await self._start_user_client()

        if self.settings.TG_CHANNEL_ID is not None:
            async with self.session_factory() as session:
                await ensure_channel(
                    session,
                    tg_chat_id=self.settings.TG_CHANNEL_ID,
                    tg_discussion_chat_id=self.settings.TG_DISCUSSION_CHAT_ID,
                    status="active",
                )
                await session.commit()

        if (
            self.client is not None
            and hasattr(self.client, "get_sticker_set")
            and self.settings.DEFAULT_STICKER_PACKS
        ):
            self._default_stickers_task = asyncio.create_task(
                self.ensure_default_sticker_packs(),
                name="ensure_default_sticker_packs",
            )

        if self.client is not None and hasattr(self.client, "get_custom_emoji_stickers"):
            self._emoji_meta_task = asyncio.create_task(
                self.ensure_custom_emoji_meta(),
                name="ensure_custom_emoji_meta",
            )

        if self.client is not None and self.settings.TG_CHANNEL_ID is not None:
            try:
                from pyrogram import Client as PyroClient

                is_real_client = isinstance(self.client, PyroClient)
            except Exception:
                is_real_client = False
            if is_real_client:
                self._crawl_task = asyncio.create_task(
                    self._run_startup_crawl(),
                    name="crawl_channel_history",
                )

    async def _run_startup_crawl(self) -> None:
        try:
            summary = await self.crawl_channel_history(trigger_prompt=False)
            logger.info("Startup bot channel crawl completed: %s", summary)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Startup bot channel crawl failed")

    async def lookup_discussion_root_id(self, tg_chat_id: int, tg_msg_id: int) -> int | None:
        """Read-only single-post discussion root lookup via `user_client.get_discussion_message`."""
        if self.user_client is None or not hasattr(self.user_client, "get_discussion_message"):
            return None
        try:
            disc_msg = await self.user_client.get_discussion_message(
                int(tg_chat_id), int(tg_msg_id)
            )
        except Exception:
            logger.debug(
                "get_discussion_message(%s, %s) failed", tg_chat_id, tg_msg_id, exc_info=True
            )
            return None
        disc_id = int(getattr(disc_msg, "id", 0) or 0) if disc_msg is not None else 0
        return disc_id if disc_id > 0 else None

    async def lookup_discussion_root_id_for_post(
        self,
        tg_chat_id: int,
        *,
        tg_msg_id: int | None,
        all_msg_ids: Sequence[int] | None = None,
    ) -> int | None:
        """Try ordered candidate channel message IDs (lowest first for albums) to resolve discussion root."""
        candidates: list[int] = []
        seen: set[int] = set()
        for raw_id in [*(sorted(int(x) for x in (all_msg_ids or []))), *( [int(tg_msg_id)] if tg_msg_id is not None else [] )]:
            if raw_id > 0 and raw_id not in seen:
                seen.add(raw_id)
                candidates.append(raw_id)
        for cand_id in candidates:
            root_id = await self.lookup_discussion_root_id(tg_chat_id, cand_id)
            if root_id is not None:
                return root_id
        return None

    async def crawl_channel_history(
        self,
        chat_id: int | None = None,
        *,
        start_id: int | None = None,
        full: bool = False,
        batch_size: int = 200,
        trigger_prompt: bool = False,
        preserve_existing_images: bool | None = None,
    ) -> dict[str, Any]:
        """Catch up / backfill channel posts using ONLY the bot client's ID-range get_messages (TelegramBackup style)."""
        target_chat_id = chat_id if chat_id is not None else self.settings.TG_CHANNEL_ID
        if self.client is None or target_chat_id is None:
            return {"status": "skipped", "reason": "no_bot_client_or_channel"}
        if not hasattr(self.client, "get_chat") or not hasattr(self.client, "get_messages"):
            return {"status": "skipped", "reason": "client_missing_get_messages"}

        preserve_imgs = full if preserve_existing_images is None else preserve_existing_images

        chat = await self.client.get_chat(target_chat_id)
        if chat is None:
            return {"status": "skipped", "reason": "chat_not_found"}

        existing_ids_before: set[int] = set()
        async with self.session_factory() as session:
            channel = await ensure_channel(
                session,
                tg_chat_id=int(target_chat_id),
                tg_discussion_chat_id=self.settings.TG_DISCUSSION_CHAT_ID,
                title=getattr(chat, "title", None),
                username=getattr(chat, "username", None),
                status="active",
            )
            last_crawled = int(channel.last_crawled_msg_id or 0)
            existing_ids_before = set(
                (
                    await session.execute(
                        select(Post.id).where(
                            Post.channel_id == channel.id,
                            Post.is_deleted.is_(False),
                        )
                    )
                )
                .scalars()
                .all()
            )
            await session.commit()

        curr_idx = (
            max(1, int(start_id))
            if start_id is not None
            else (1 if full else max(1, last_crawled + 1))
        )
        effective_batch = max(1, min(200, int(batch_size)))
        get_messages_fn = self.client.get_messages
        try:
            supports_replies = "replies" in inspect.signature(get_messages_fn).parameters
        except (TypeError, ValueError):
            supports_replies = False

        msgs: list[Any] = []
        max_seen_id = last_crawled
        first_batch = True

        while True:
            if not first_batch:
                await asyncio.sleep(0.2)
            first_batch = False
            end_idx = curr_idx + effective_batch
            id_range = range(curr_idx, end_idx)

            additional_msgs: Any = None
            for attempt in range(2):
                try:
                    if supports_replies:
                        additional_msgs = await get_messages_fn(
                            chat.id, id_range, replies=0
                        )
                    else:
                        additional_msgs = await get_messages_fn(chat.id, list(id_range))
                    break
                except Exception as exc:
                    wait_sec = getattr(exc, "value", None)
                    if (
                        type(exc).__name__ == "FloodWait"
                        and isinstance(wait_sec, (int, float))
                        and attempt == 0
                        and float(wait_sec) <= 30.0
                    ):
                        await asyncio.sleep(float(wait_sec))
                        continue
                    raise

            batch_items = (
                list(additional_msgs)
                if isinstance(additional_msgs, (list, tuple))
                else ([additional_msgs] if additional_msgs is not None else [])
            )
            non_empty = [
                m
                for m in batch_items
                if m is not None and not getattr(m, "empty", False)
            ]
            if not non_empty:
                break

            msgs.extend(non_empty)
            max_batch_id = max(int(getattr(m, "id", 0) or 0) for m in non_empty)
            max_seen_id = max(max_seen_id, max_batch_id)

            if not full and (end_idx - 1) - max_batch_id > 20:
                break
            curr_idx = end_idx

        posts_ingested = 0
        posts_created = 0
        posts_updated = 0
        deleted_posts = 0
        if msgs:
            ingested = await self._ingest_channel_messages(
                msgs,
                is_edit=False,
                trigger_prompt=trigger_prompt,
                preserve_existing_images=preserve_imgs,
            )
            posts_ingested = len(ingested)
            posts_created = sum(1 for p in ingested if p.id not in existing_ids_before)
            posts_updated = posts_ingested - posts_created

            present_tg_ids = {int(getattr(m, "id", 0) or 0) for m in msgs}
            async with self.session_factory() as session:
                ch = await self._resolve_channel_for_chat(session, int(target_chat_id))
                if ch is not None:
                    if max_seen_id > int(ch.last_crawled_msg_id or 0):
                        ch.last_crawled_msg_id = max_seen_id
                    if full and start_id is None:
                        active_posts = list(
                            (
                                await session.execute(
                                    select(Post).where(
                                        Post.channel_id == ch.id,
                                        Post.is_deleted.is_(False),
                                        Post.tg_msg_id.is_not(None),
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                        candidates: list[Post] = []
                        for p in active_posts:
                            post_ids = {int(x) for x in (p.tg_all_msg_ids or [])}
                            if p.tg_msg_id is not None:
                                post_ids.add(int(p.tg_msg_id))
                            if post_ids and not (post_ids & present_tg_ids):
                                candidates.append(p)
                        # Mass-deletion safety circuit breaker (>50% missing -> abort deletions)
                        if not (
                            len(active_posts) >= 4
                            and len(candidates) / len(active_posts) > 0.50
                        ):
                            for p in candidates:
                                p.is_deleted = True
                            deleted_posts = len(candidates)
                        else:
                            logger.warning(
                                "Fullsync skipped %d/%d candidate deletions due to >50%% circuit breaker",
                                len(candidates),
                                len(active_posts),
                            )
                    await session.commit()
                    if deleted_posts:
                        await generate_exports(session, self.settings)
                        for p in candidates:
                            await self._emit_sse("post.deleted", {"post_id": p.id})

        return {
            "status": "ok",
            "crawled_messages": len(msgs),
            "posts_ingested": posts_ingested,
            "posts_created": posts_created,
            "posts_updated": posts_updated,
            "deleted_posts": deleted_posts,
            "max_msg_id": max_seen_id,
        }

    async def stop(self) -> None:
        if self._crawl_task is not None:
            if not self._crawl_task.done():
                self._crawl_task.cancel()
            try:
                await self._crawl_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("Channel crawl task exited with error", exc_info=True)
            self._crawl_task = None
        if self._default_stickers_task is not None:
            if not self._default_stickers_task.done():
                self._default_stickers_task.cancel()
            try:
                await self._default_stickers_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("Default sticker sync task exited with error", exc_info=True)
            self._default_stickers_task = None
        if self._emoji_meta_task is not None:
            if not self._emoji_meta_task.done():
                self._emoji_meta_task.cancel()
            try:
                await self._emoji_meta_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("Custom emoji meta sync task exited with error", exc_info=True)
            self._emoji_meta_task = None
        await self.album_buffer.close()
        await self.dm_buffer.close()
        await self.discussion_album_buffer.close()
        if self._user_started and self.user_client is not None:
            try:
                await self.user_client.stop()
            except Exception:
                logger.exception("Error stopping read-only user_client")
        self._user_started = False
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
            DeletedMessagesHandler,
            EditedMessageHandler,
            MessageHandler,
            MessageReactionCountHandler,
            MessageReactionHandler,
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

            async def _on_discussion_deleted(_client: Any, messages: Any) -> None:
                await self.handle_discussion_deleted(messages)

            self.client.add_handler(MessageHandler(_on_discussion_msg, disc_filter))
            self.client.add_handler(
                DeletedMessagesHandler(_on_discussion_deleted, disc_filter)
            )

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

        async def _on_message_reaction(_client: Any, update: Any) -> None:
            await self.handle_message_reaction_update(update)

        async def _on_poll(_client: Any, poll: Any) -> None:
            await self.handle_poll_update(poll)

        reaction_chats = [
            cid
            for cid in (self.settings.TG_CHANNEL_ID, self.settings.TG_DISCUSSION_CHAT_ID)
            if cid is not None
        ]
        reaction_filter = filters.chat(reaction_chats) if reaction_chats else None
        self.client.add_handler(MessageReactionCountHandler(_on_reaction_count, reaction_filter))
        self.client.add_handler(MessageReactionHandler(_on_message_reaction, reaction_filter))
        self.client.add_handler(PollHandler(_on_poll))
        self._handlers_registered = True

    def _register_user_handlers(self) -> None:
        """Attach strictly read-only listeners (polls, reactions, deletions) to user_client."""
        if self._user_handlers_registered or self.user_client is None:
            return
        if not hasattr(self.user_client, "add_handler"):
            return

        from pyrogram.handlers import (
            DeletedMessagesHandler,
            PollHandler,
            RawUpdateHandler,
        )

        async def _on_user_poll(_client: Any, poll: Any) -> None:
            await self.handle_poll_update(poll)

        async def _on_user_raw(_client: Any, update: Any, _users: Any = None, _chats: Any = None) -> None:
            if hasattr(update, "peer") and hasattr(update, "msg_id") and hasattr(update, "reactions"):
                await self.handle_raw_reaction_update(update)

        async def _on_user_deleted(_client: Any, messages: Any) -> None:
            await self.handle_user_deleted(messages)

        self.user_client.add_handler(PollHandler(_on_user_poll))
        self.user_client.add_handler(RawUpdateHandler(_on_user_raw))
        self.user_client.add_handler(DeletedMessagesHandler(_on_user_deleted))
        self._user_handlers_registered = True

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
        *,
        skip_photo_msg_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Convert Kurigram messages using `tgc.pyro.crawl.process_messages` if installed, else built-in fallback."""
        epoch_dir = self.settings.media_dir / "epochs" / str(channel.id)
        epoch_dir.mkdir(parents=True, exist_ok=True)
        url_prefix = f"epochs/{channel.id}"

        tgc_proc = _try_import_tgc_process_messages()
        if tgc_proc is not None and self.client is not None and not skip_photo_msg_ids:
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

        return await self._fallback_process_messages(
            msgs,
            epoch_dir,
            url_prefix=url_prefix,
            skip_photo_msg_ids=skip_photo_msg_ids,
        )

    @staticmethod
    def _infer_fallback_media_ext(
        attr_name: str,
        media_obj: Any,
        default_ext: str,
    ) -> str:
        if attr_name == "sticker":
            if getattr(media_obj, "is_animated", False):
                return ".tgs"
            if getattr(media_obj, "is_video", False):
                return ".webm"
        raw_name = getattr(media_obj, "file_name", None)
        if raw_name:
            suffix = Path(str(raw_name)).suffix.lower()
            if suffix and 2 <= len(suffix) <= 8 and suffix[1:].isalnum():
                return suffix
        mime_type = str(getattr(media_obj, "mime_type", "") or "").lower().strip()
        if mime_type:
            mime_overrides = {
                "application/x-tgsticker": ".tgs",
                "video/webm": ".webm",
                "image/webp": ".webp",
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "audio/ogg": ".ogg",
                "audio/mpeg": ".mp3",
                "video/mp4": ".mp4",
            }
            if mime_type in mime_overrides:
                return mime_overrides[mime_type]
            guessed = mimetypes.guess_extension(mime_type)
            if guessed:
                return guessed
        return default_ext

    async def _download_target_to_webp(
        self,
        file_id: str | None,
        *,
        raw_dest_name: str,
        msg_id: int,
        media_dir: Path,
        url_prefix: str,
        download_client: Any | None = None,
        warn_on_error: bool = False,
    ) -> tuple[str | None, int | None, int | None]:
        """Download a Telegram file_id to WebP thumbnail and return `(rel_url, width, height)`."""
        if not file_id:
            return None, None, None
        raw_thumb_dest = media_dir / raw_dest_name
        webp_existing = media_dir / f"{raw_thumb_dest.stem}_thumb.webp"
        if webp_existing.is_file() and webp_existing.stat().st_size > 0:
            w, h = await asyncio.to_thread(_read_image_size_sync, webp_existing)
            return f"{url_prefix}/media/{webp_existing.name}", w or None, h or None
        dl_client = download_client if download_client is not None else self.client
        if dl_client is None or not hasattr(dl_client, "download_media"):
            return None, None, None
        tmp_thumb = media_dir / f".dl_{msg_id}_thumb_{uuid.uuid4().hex[:8]}.jpg"
        try:
            dl = await dl_client.download_media(file_id, file_name=str(tmp_thumb))
            if not dl:
                return None, None, None
            dl_path = Path(str(dl))
            if not dl_path.is_file():
                return None, None, None
            os.replace(dl_path, raw_thumb_dest)
            webp_p = await asyncio.to_thread(_create_webp_thumb_sync, raw_thumb_dest)
            target_p = webp_p if (webp_p is not None and webp_p.is_file()) else raw_thumb_dest
            w, h = await asyncio.to_thread(_read_image_size_sync, target_p)
            return f"{url_prefix}/media/{target_p.name}", w or None, h or None
        except Exception:
            log_fn = logger.warning if warn_on_error else logger.debug
            log_fn("Could not download thumbnail for msg %s", msg_id, exc_info=True)
            return None, None, None
        finally:
            tmp_thumb.unlink(missing_ok=True)

    async def _download_fallback_thumb(
        self,
        media_obj: Any,
        *,
        msg_id: int,
        media_dir: Path,
        url_prefix: str,
    ) -> str | None:
        thumbs = getattr(media_obj, "thumbs", None) or getattr(media_obj, "thumb", None)
        if not thumbs:
            return None
        thumb_obj = (
            max(thumbs, key=lambda t: int(getattr(t, "file_size", 0) or 0))
            if isinstance(thumbs, (list, tuple))
            else thumbs
        )
        file_id = getattr(thumb_obj, "file_id", None)
        rel_url, _, _ = await self._download_target_to_webp(
            str(file_id) if file_id else None,
            raw_dest_name=f"{msg_id}_thumb_raw.jpg",
            msg_id=msg_id,
            media_dir=media_dir,
            url_prefix=url_prefix,
        )
        return rel_url

    async def _extract_fallback_web_page(
        self,
        msg: Any,
        *,
        msg_id: int,
        media_dir: Path,
        url_prefix: str,
        download_client: Any | None = None,
    ) -> dict[str, Any] | None:
        """Extract an active Telegram WebPage link preview into `files` shape."""
        wp = getattr(msg, "web_page", None)
        if _is_inactive_web_page(msg, wp):
            return None
        safe_url = _is_safe_http_url(getattr(wp, "url", None))
        if not safe_url:
            return None

        lpo = getattr(msg, "link_preview_options", None)
        wp_photo = getattr(wp, "photo", None)
        rel_thumb: str | None = None
        thumb_w: int | None = None
        thumb_h: int | None = None
        if wp_photo is not None:
            photo_fid = _pick_photo_file_id(wp_photo)
            key_src = str(
                getattr(wp_photo, "file_unique_id", None)
                or getattr(wp, "id", None)
                or safe_url
            )
            photo_hash = hashlib.sha1(key_src.encode("utf-8")).hexdigest()[:10]
            media_dir.mkdir(parents=True, exist_ok=True)
            rel_thumb, dl_w, dl_h = await self._download_target_to_webp(
                photo_fid,
                raw_dest_name=f"{msg_id}_wp_{photo_hash}.jpg",
                msg_id=msg_id,
                media_dir=media_dir,
                url_prefix=url_prefix,
                download_client=download_client,
                warn_on_error=True,
            )
            thumb_w = dl_w or (int(getattr(wp_photo, "width", 0) or 0) or None)
            thumb_h = dl_h or (int(getattr(wp_photo, "height", 0) or 0) or None)

        site_name = (getattr(wp, "site_name", None) or "").strip() or None
        title = (getattr(wp, "title", None) or "").strip() or None
        description = (getattr(wp, "description", None) or "").strip() or None
        display_url = (getattr(wp, "display_url", None) or "").strip() or None
        author = (getattr(wp, "author", None) or "").strip() or None
        wp_type = (getattr(wp, "type", None) or "").strip() or None

        if not any((site_name, title, description, rel_thumb)):
            return None

        out: dict[str, Any] = {
            "tg_msg_id": msg_id,
            "media_type": "web_page",
            "url": safe_url,
        }
        if display_url:
            out["display_url"] = display_url
        if site_name:
            out["site_name"] = site_name
        if title:
            out["title"] = title
        if description:
            out["description"] = description
        if author:
            out["author"] = author
        if wp_type:
            out["web_page_type"] = wp_type
        if rel_thumb:
            out["thumb"] = rel_thumb
        if thumb_w:
            out["width"] = thumb_w
        if thumb_h:
            out["height"] = thumb_h

        has_large_media = bool(getattr(wp, "has_large_media", False))
        prefer_large_media = bool(
            getattr(wp, "prefer_large_media", False)
            or getattr(lpo, "prefer_large_media", False)
        )
        prefer_small_media = bool(
            getattr(wp, "prefer_small_media", False)
            or getattr(lpo, "prefer_small_media", False)
        )
        if has_large_media:
            out["has_large_media"] = True
        if prefer_large_media:
            out["prefer_large_media"] = True
        if prefer_small_media:
            out["prefer_small_media"] = True
        return out

    async def _extract_fallback_file_attachment(
        self,
        msg: Any,
        *,
        msg_id: int,
        media_dir: Path,
        url_prefix: str,
    ) -> dict[str, Any] | None:
        media_specs: tuple[tuple[str, str | None, str, str], ...] = (
            ("sticker", "sticker", ".webp", "image/webp"),
            ("animation", "animation", ".mp4", "video/mp4"),
            ("video", "video_file", ".mp4", "video/mp4"),
            ("video_note", "video_file", ".mp4", "video/mp4"),
            ("audio", "audio_file", ".mp3", "audio/mpeg"),
            ("voice", "voice_message", ".ogg", "audio/ogg"),
            ("document", None, ".bin", "application/octet-stream"),
        )
        for attr_name, tg_media_type, default_ext, default_mime in media_specs:
            media_obj = getattr(msg, attr_name, None)
            if media_obj is None:
                continue

            ext = self._infer_fallback_media_ext(attr_name, media_obj, default_ext)
            dest_path = media_dir / f"{msg_id}{ext}"
            expected_size = getattr(media_obj, "file_size", None)
            downloaded_path: Path | None = None
            if dest_path.is_file() and (
                expected_size is None or dest_path.stat().st_size == int(expected_size)
            ):
                downloaded_path = dest_path
            elif self.client is not None and hasattr(self.client, "download_media"):
                tmp_dest = media_dir / f".dl_{msg_id}_{uuid.uuid4().hex[:8]}{ext}"
                try:
                    dl = await self.client.download_media(msg, file_name=str(tmp_dest))
                    if dl:
                        dl_path = Path(str(dl))
                        if dl_path.is_file():
                            os.replace(dl_path, dest_path)
                            downloaded_path = dest_path
                except Exception:
                    logger.exception("Failed to download %s for msg %s", attr_name, msg_id)
                finally:
                    tmp_dest.unlink(missing_ok=True)

            effective_name = (
                downloaded_path.name
                if downloaded_path is not None and downloaded_path.is_file()
                else dest_path.name
            )
            file_size = (
                downloaded_path.stat().st_size
                if downloaded_path is not None and downloaded_path.is_file()
                else getattr(media_obj, "file_size", None)
            )
            rel_thumb = await self._download_fallback_thumb(
                media_obj,
                msg_id=msg_id,
                media_dir=media_dir,
                url_prefix=url_prefix,
            )

            file_dict: dict[str, Any] = {
                "tg_msg_id": msg_id,
                "url": f"{url_prefix}/media/{effective_name}",
                "mime_type": getattr(media_obj, "mime_type", None) or default_mime,
                "original_name": getattr(media_obj, "file_name", None) or effective_name,
                "size": file_size,
            }
            if tg_media_type is not None:
                file_dict["media_type"] = tg_media_type
            if rel_thumb is not None:
                file_dict["thumb"] = rel_thumb
            if getattr(msg, "has_media_spoiler", False):
                file_dict["spoiler"] = True
            for opt_key in ("width", "height", "duration", "title", "performer"):
                val = getattr(media_obj, opt_key, None)
                if val is not None:
                    file_dict[opt_key] = val
            if attr_name == "sticker":
                emoji_val = getattr(media_obj, "emoji", None)
                if emoji_val:
                    file_dict["sticker_emoji"] = str(emoji_val)
            return file_dict

        venue = getattr(msg, "venue", None)
        if venue is not None and getattr(venue, "location", None) is not None:
            return {
                "media_type": "location",
                "latitude": venue.location.latitude,
                "longitude": venue.location.longitude,
                "title": getattr(venue, "title", "") or "",
                "address": getattr(venue, "address", "") or "",
            }
        loc = getattr(msg, "location", None)
        if loc is not None:
            return {
                "media_type": "location",
                "latitude": loc.latitude,
                "longitude": loc.longitude,
            }
        contact = getattr(msg, "contact", None)
        if contact is not None:
            return {
                "media_type": "contact",
                "phone_number": getattr(contact, "phone_number", "") or "",
                "first_name": getattr(contact, "first_name", "") or "",
                "last_name": getattr(contact, "last_name", None),
            }
        return await self._extract_fallback_web_page(
            msg,
            msg_id=msg_id,
            media_dir=media_dir,
            url_prefix=url_prefix,
        )

    async def _fallback_process_messages(
        self,
        msgs: list[Any],
        epoch_dir: Path,
        *,
        url_prefix: str,
        skip_photo_msg_ids: set[int] | None = None,
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
            if photo is not None and not (skip_photo_msg_ids and msg_id in skip_photo_msg_ids):
                dest_path = media_dir / f"{msg_id}.jpg"
                tmp_dest = media_dir / f".dl_{msg_id}_{uuid.uuid4().hex[:8]}.jpg"
                downloaded_path: Path | None = None
                if self.client is not None and hasattr(self.client, "download_media"):
                    try:
                        dl = await self.client.download_media(msg, file_name=str(tmp_dest))
                        if dl:
                            dl_path = Path(str(dl))
                            if dl_path.is_file():
                                os.replace(dl_path, dest_path)
                                downloaded_path = dest_path
                    except Exception:
                        logger.exception("Failed to download photo for msg %s", msg_id)
                    finally:
                        tmp_dest.unlink(missing_ok=True)

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

            # Extract non-photo media attachments uniformly for tg-blog FileView
            extra_file = await self._extract_fallback_file_attachment(
                msg,
                msg_id=msg_id,
                media_dir=media_dir,
                url_prefix=url_prefix,
            )
            if extra_file is not None:
                files.append(extra_file)

            # Cache custom emoji stickers referenced in text/caption/quote entities
            custom_emoji_ids: list[str] = []
            for ent_src in (
                getattr(msg, "entities", None),
                getattr(msg, "caption_entities", None),
                getattr(getattr(msg, "quote", None), "entities", None),
            ):
                for ent in ent_src or []:
                    cid = getattr(ent, "custom_emoji_id", None)
                    if cid is not None and str(cid).strip().isdigit():
                        custom_emoji_ids.append(str(cid).strip())
            if custom_emoji_ids and self.client is not None and hasattr(self.client, "get_custom_emoji_stickers"):
                try:
                    await cache_custom_emoji_stickers(
                        self.client, self.settings.exports_dir, custom_emoji_ids
                    )
                except Exception:
                    logger.debug("Could not cache custom emojis for msg %s", msg_id, exc_info=True)

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
                    "jxl_url": prev_img.jxl_url,
                    "thumb_jxl_url": prev_img.thumb_jxl_url,
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
        preserve_existing_images: bool = False,
    ) -> list[Post]:
        if not msgs:
            return []

        first_chat_id = getattr(getattr(msgs[0], "chat", None), "id", None)
        lock_key: int | str = int(first_chat_id) if first_chat_id is not None else "active"
        ingest_lock = self._channel_ingest_locks.setdefault(lock_key, asyncio.Lock())
        async with ingest_lock:
            return await self._ingest_channel_messages_locked(
                msgs,
                first_chat_id=first_chat_id,
                is_edit=is_edit,
                trigger_prompt=trigger_prompt,
                preserve_existing_images=preserve_existing_images,
            )

    async def _ingest_channel_messages_locked(
        self,
        msgs: list[Any],
        *,
        first_chat_id: int | None,
        is_edit: bool = False,
        trigger_prompt: bool = True,
        preserve_existing_images: bool = False,
    ) -> list[Post]:
        async with self.session_factory() as session:
            channel = await self._resolve_channel_for_chat(session, first_chat_id)
            if channel is None:
                logger.warning("No matching channel found for incoming chat_id=%s", first_chat_id)
                return []

            skip_photo_msg_ids: set[int] = set()
            if preserve_existing_images:
                posts_with_imgs = list(
                    (
                        await session.execute(
                            select(Post)
                            .options(selectinload(Post.images))
                            .where(
                                Post.channel_id == channel.id,
                                Post.is_deleted.is_(False),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for p in posts_with_imgs:
                    if not p.images:
                        continue
                    if p.tg_msg_id is not None:
                        skip_photo_msg_ids.add(int(p.tg_msg_id))
                    for mid in p.tg_all_msg_ids or []:
                        skip_photo_msg_ids.add(int(mid))
                    for im in p.images:
                        if im.tg_msg_id is not None:
                            skip_photo_msg_ids.add(int(im.tg_msg_id))

            converted = await self._convert_channel_messages(
                msgs,
                channel,
                skip_photo_msg_ids=skip_photo_msg_ids or None,
            )
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
                if preserve_existing_images and existing is not None and existing.images:
                    images_payload = None
                elif raw_images:
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

                linked_disc_root_id = existing.tg_discussion_root_id if existing else None
                if linked_disc_root_id is None and channel.status == "active":
                    for mid in sorted(all_ids):
                        cached_root = self._unlinked_autoforwards.pop((channel.id, int(mid)), None)
                        if cached_root is not None:
                            linked_disc_root_id = int(cached_root)
                            break

                existing_only_web_page = (
                    existing is not None
                    and isinstance(existing.files, list)
                    and bool(existing.files)
                    and all(
                        isinstance(f, dict) and f.get("media_type") == "web_page"
                        for f in existing.files
                    )
                )
                files_payload: list[dict[str, Any]] | None
                if (
                    is_edit
                    and not merged_split_album
                    and existing is not None
                    and existing.tg_media_group_id is None
                    and not other_files
                    and existing_only_web_page
                ):
                    files_payload = None
                elif existing is not None and isinstance(existing.files, list) and (
                    merged_split_album
                    or (
                        is_edit
                        and (
                            existing.tg_media_group_id is not None
                            or len(existing.files) > len(other_files)
                        )
                    )
                ):
                    if other_files:
                        merged_files: list[dict[str, Any]] = [
                            dict(f) for f in existing.files if isinstance(f, dict)
                        ]
                        idx_by_key: dict[tuple[str, Any], int] = {}
                        for pos, item in enumerate(merged_files):
                            if item.get("tg_msg_id") is not None:
                                idx_by_key[("tg", int(item["tg_msg_id"]))] = pos
                            if item.get("url"):
                                idx_by_key[("url", str(item["url"]))] = pos
                        for new_f in other_files:
                            match_pos: int | None = None
                            if new_f.get("tg_msg_id") is not None:
                                match_pos = idx_by_key.get(("tg", int(new_f["tg_msg_id"])))
                            if match_pos is None and new_f.get("url"):
                                match_pos = idx_by_key.get(("url", str(new_f["url"])))
                            if match_pos is not None:
                                merged_files[match_pos] = dict(new_f)
                            else:
                                new_pos = len(merged_files)
                                merged_files.append(dict(new_f))
                                if new_f.get("tg_msg_id") is not None:
                                    idx_by_key[("tg", int(new_f["tg_msg_id"]))] = new_pos
                                if new_f.get("url"):
                                    idx_by_key[("url", str(new_f["url"]))] = new_pos
                        merged_files.sort(
                            key=lambda item: (
                                0 if item.get("tg_msg_id") is not None else 1,
                                int(item.get("tg_msg_id") or 0),
                            )
                        )
                        files_payload = merged_files
                    else:
                        files_payload = existing.files
                elif other_files:
                    files_payload = other_files
                elif (is_edit or merged_split_album or preserve_existing_images) and existing is not None:
                    files_payload = existing.files
                else:
                    files_payload = None

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
                    tg_discussion_root_id=linked_disc_root_id,
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
                    files=files_payload,
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

            # Fill in missing discussion root for newly ingested active-channel posts (e.g. album debounce race)
            if (
                channel.status == "active"
                and channel.tg_chat_id is not None
                and channel.tg_discussion_chat_id is not None
                and self.user_client is not None
            ):
                root_linked = False
                for post_obj, was_new in ingested_posts:
                    if not was_new or post_obj.tg_discussion_root_id is not None:
                        continue
                    resolved_root = await self.lookup_discussion_root_id_for_post(
                        int(channel.tg_chat_id),
                        tg_msg_id=post_obj.tg_msg_id,
                        all_msg_ids=post_obj.tg_all_msg_ids,
                    )
                    if resolved_root is None:
                        continue
                    conflict = (
                        await session.execute(
                            select(Post.id).where(
                                Post.channel_id == channel.id,
                                Post.tg_discussion_root_id == resolved_root,
                            )
                        )
                    ).scalar_one_or_none()
                    if conflict is None:
                        post_obj.tg_discussion_root_id = resolved_root
                        root_linked = True
                if root_linked:
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
                if trigger_prompt and refreshed.images and (
                    was_new
                    or (
                        not is_edit
                        and (refreshed.date is None or refreshed.date >= datetime.now(UTC) - timedelta(hours=1))
                    )
                ):
                    has_token = (
                        await session.execute(
                            select(OriginalUploadToken.token_hash)
                            .where(OriginalUploadToken.post_id == refreshed.id)
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if has_token is None and any(not img.original_url for img in refreshed.images):
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

    async def _download_tg_media_to_rel(
        self,
        media_obj: Any,
        msg: Any,
        rel_path: str,
    ) -> Path | None:
        if self.client is None or not hasattr(self.client, "download_media"):
            return None
        abs_path = self.settings.media_dir / rel_path
        if abs_path.is_file():
            return abs_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = abs_path.parent / f".{abs_path.stem}.{uuid.uuid4().hex[:8]}.tmp{abs_path.suffix}"
        target_arg = getattr(media_obj, "file_id", None) or msg
        try:
            dl = await self.client.download_media(target_arg, file_name=str(tmp_path))
            if dl:
                dl_path = Path(str(dl))
                if dl_path.is_file():
                    os.replace(dl_path, abs_path)
                    return abs_path
        except Exception:
            logger.debug("Failed to download TG media to %s", rel_path, exc_info=True)
            tmp_path.unlink(missing_ok=True)
        return None

    async def _download_discussion_media(
        self,
        msg: Any,
        channel_id: int,
        msg_id: int,
    ) -> dict[str, Any] | None:
        photo = getattr(msg, "photo", None)
        if photo is not None:
            rel_url = f"comments/tg_{channel_id}_{msg_id}.jpg"
            abs_path = await self._download_tg_media_to_rel(photo, msg, rel_url)
            if abs_path is None:
                return None
            width = getattr(photo, "width", None)
            height = getattr(photo, "height", None)
            rel_thumb: str | None = None
            size = abs_path.stat().st_size
            try:
                width, height = await asyncio.to_thread(_read_image_size_sync, abs_path)
                thumb_path = await asyncio.to_thread(
                    _create_webp_thumb_sync, abs_path, (480, 480)
                )
                if thumb_path is not None:
                    rel_thumb = f"comments/{thumb_path.name}"
            except Exception:
                logger.debug("Failed reading photo size/thumb for %s", abs_path, exc_info=True)
            payload: dict[str, Any] = {
                "media_type": "photo",
                "url": rel_url,
                "size": int(size),
            }
            if rel_thumb:
                payload["thumb"] = rel_thumb
            if width is not None:
                payload["width"] = int(width)
            if height is not None:
                payload["height"] = int(height)
            if bool(getattr(msg, "has_media_spoiler", False)):
                payload["spoiler"] = True
            return payload

        sticker = getattr(msg, "sticker", None)
        if sticker is not None:
            is_animated = bool(getattr(sticker, "is_animated", False))
            is_video = bool(getattr(sticker, "is_video", False))
            ext = ".tgs" if is_animated else (".webm" if is_video else ".webp")
            rel_url = f"comments/tg_{channel_id}_{msg_id}{ext}"
            abs_path = await self._download_tg_media_to_rel(sticker, msg, rel_url)
            if abs_path is None:
                return None
            width = getattr(sticker, "width", None)
            height = getattr(sticker, "height", None)
            size = abs_path.stat().st_size
            if not is_animated and not is_video and (width is None or height is None):
                try:
                    width, height = await asyncio.to_thread(_read_image_size_sync, abs_path)
                except Exception:
                    pass
            emoji = getattr(sticker, "emoji", None) or None
            sticker_uid = (
                getattr(sticker, "file_unique_id", None)
                or getattr(sticker, "file_id", None)
            )
            payload = {
                "media_type": "sticker",
                "url": rel_url,
                "size": int(size),
                "is_animated": is_animated,
                "is_video": is_video,
            }
            if emoji:
                payload["emoji"] = str(emoji)
            if width is not None:
                payload["width"] = int(width)
            if height is not None:
                payload["height"] = int(height)
            if sticker_uid:
                payload["sticker_id"] = str(sticker_uid)
            return payload

        animation = getattr(msg, "animation", None)
        if animation is not None:
            mime = str(getattr(animation, "mime_type", "") or "").lower()
            ext = ".gif" if "gif" in mime else ".mp4"
            rel_url = f"comments/tg_{channel_id}_{msg_id}{ext}"
            file_size = getattr(animation, "file_size", None)
            if file_size is not None and int(file_size) > _MAX_COMMENT_TG_MEDIA_BYTES:
                return None
            abs_path = await self._download_tg_media_to_rel(animation, msg, rel_url)
            if abs_path is None:
                return None
            payload = {
                "media_type": "animation",
                "url": rel_url,
                "size": abs_path.stat().st_size,
            }
            if getattr(animation, "width", None) is not None:
                payload["width"] = int(animation.width)
            if getattr(animation, "height", None) is not None:
                payload["height"] = int(animation.height)
            if getattr(animation, "file_name", None):
                payload["file_name"] = sanitize_original_filename(
                    str(animation.file_name), fallback_ext=ext
                )
            if mime:
                payload["mime_type"] = mime
            rel_thumb = await self._download_fallback_thumb(
                animation,
                msg_id=msg_id,
                media_dir=self.settings.media_dir / "comments",
                url_prefix="comments",
            )
            if rel_thumb:
                payload["thumb"] = rel_thumb
            return payload

        video = getattr(msg, "video", None)
        if video is not None:
            mime = str(getattr(video, "mime_type", "") or "").lower()
            ext = ".webm" if "webm" in mime else ".mp4"
            rel_url = f"comments/tg_{channel_id}_{msg_id}{ext}"
            file_size = getattr(video, "file_size", None)
            if file_size is not None and int(file_size) > _MAX_COMMENT_TG_MEDIA_BYTES:
                return None
            abs_path = await self._download_tg_media_to_rel(video, msg, rel_url)
            if abs_path is None:
                return None
            payload = {
                "media_type": "video",
                "url": rel_url,
                "size": abs_path.stat().st_size,
            }
            if getattr(video, "width", None) is not None:
                payload["width"] = int(video.width)
            if getattr(video, "height", None) is not None:
                payload["height"] = int(video.height)
            if getattr(video, "duration", None) is not None:
                payload["duration"] = int(video.duration)
            if getattr(video, "file_name", None):
                payload["file_name"] = sanitize_original_filename(
                    str(video.file_name), fallback_ext=ext
                )
            if mime:
                payload["mime_type"] = mime
            rel_thumb = await self._download_fallback_thumb(
                video,
                msg_id=msg_id,
                media_dir=self.settings.media_dir / "comments",
                url_prefix="comments",
            )
            if rel_thumb:
                payload["thumb"] = rel_thumb
            return payload

        video_note = getattr(msg, "video_note", None)
        if video_note is not None:
            rel_url = f"comments/tg_{channel_id}_{msg_id}.mp4"
            file_size = getattr(video_note, "file_size", None)
            if file_size is not None and int(file_size) > _MAX_COMMENT_TG_MEDIA_BYTES:
                return None
            abs_path = await self._download_tg_media_to_rel(video_note, msg, rel_url)
            if abs_path is None:
                return None
            length = getattr(video_note, "length", None)
            payload = {
                "media_type": "video",
                "url": rel_url,
                "size": abs_path.stat().st_size,
                "mime_type": "video/mp4",
            }
            if length is not None:
                payload["width"] = int(length)
                payload["height"] = int(length)
            if getattr(video_note, "duration", None) is not None:
                payload["duration"] = int(video_note.duration)
            rel_thumb = await self._download_fallback_thumb(
                video_note,
                msg_id=msg_id,
                media_dir=self.settings.media_dir / "comments",
                url_prefix="comments",
            )
            if rel_thumb:
                payload["thumb"] = rel_thumb
            return payload

        voice = getattr(msg, "voice", None)
        if voice is not None:
            mime = str(getattr(voice, "mime_type", "") or "audio/ogg").lower()
            ext = ".ogg" if "ogg" in mime else ".mp3"
            rel_url = f"comments/tg_{channel_id}_{msg_id}{ext}"
            file_size = getattr(voice, "file_size", None)
            if file_size is not None and int(file_size) > _MAX_COMMENT_TG_MEDIA_BYTES:
                return None
            abs_path = await self._download_tg_media_to_rel(voice, msg, rel_url)
            if abs_path is None:
                return None
            payload = {
                "media_type": "voice",
                "url": rel_url,
                "size": abs_path.stat().st_size,
                "mime_type": mime,
            }
            if getattr(voice, "duration", None) is not None:
                payload["duration"] = int(voice.duration)
            return payload

        audio = getattr(msg, "audio", None)
        if audio is not None:
            mime = str(getattr(audio, "mime_type", "") or "audio/mpeg").lower()
            raw_fname = getattr(audio, "file_name", None)
            safe_fname = (
                sanitize_original_filename(str(raw_fname), fallback_ext="")
                if raw_fname
                else None
            )
            ext = Path(safe_fname).suffix.lower() if safe_fname else ""
            if not ext:
                ext = ".ogg" if "ogg" in mime else (".m4a" if "mp4" in mime else ".mp3")
            rel_url = f"comments/tg_{channel_id}_{msg_id}{ext}"
            file_size = getattr(audio, "file_size", None)
            if file_size is not None and int(file_size) > _MAX_COMMENT_TG_MEDIA_BYTES:
                return None
            abs_path = await self._download_tg_media_to_rel(audio, msg, rel_url)
            if abs_path is None:
                return None
            payload = {
                "media_type": "audio",
                "url": rel_url,
                "size": abs_path.stat().st_size,
                "mime_type": mime,
            }
            if safe_fname:
                payload["file_name"] = safe_fname
            if getattr(audio, "duration", None) is not None:
                payload["duration"] = int(audio.duration)
            if getattr(audio, "title", None):
                payload["title"] = str(audio.title)
            if getattr(audio, "performer", None):
                payload["performer"] = str(audio.performer)
            return payload

        doc = getattr(msg, "document", None)
        if doc is not None:
            mime = str(getattr(doc, "mime_type", "") or "").lower()
            raw_fname = getattr(doc, "file_name", None)
            safe_fname = (
                sanitize_original_filename(str(raw_fname), fallback_ext="")
                if raw_fname
                else None
            )
            ext = Path(safe_fname).suffix.lower() if safe_fname else ""
            if not ext:
                ext = ".bin"
            rel_url = f"comments/tg_{channel_id}_{msg_id}{ext}"
            file_size = getattr(doc, "file_size", None)
            if file_size is not None and int(file_size) > _MAX_COMMENT_TG_MEDIA_BYTES:
                return None
            abs_path = await self._download_tg_media_to_rel(doc, msg, rel_url)
            if abs_path is None:
                return None
            payload = {
                "media_type": "document",
                "url": rel_url,
                "size": abs_path.stat().st_size,
            }
            if safe_fname:
                payload["file_name"] = safe_fname
            if mime:
                payload["mime_type"] = mime
            rel_thumb = await self._download_fallback_thumb(
                doc,
                msg_id=msg_id,
                media_dir=self.settings.media_dir / "comments",
                url_prefix="comments",
            )
            if rel_thumb:
                payload["thumb"] = rel_thumb
            return payload

        venue = getattr(msg, "venue", None)
        if venue is not None and getattr(venue, "location", None) is not None:
            return {
                "media_type": "location",
                "latitude": float(venue.location.latitude),
                "longitude": float(venue.location.longitude),
                "title": str(getattr(venue, "title", "") or ""),
                "address": str(getattr(venue, "address", "") or ""),
            }

        loc = getattr(msg, "location", None)
        if loc is not None:
            return {
                "media_type": "location",
                "latitude": float(loc.latitude),
                "longitude": float(loc.longitude),
            }

        contact = getattr(msg, "contact", None)
        if contact is not None:
            return {
                "media_type": "contact",
                "phone_number": str(getattr(contact, "phone_number", "") or ""),
                "first_name": str(getattr(contact, "first_name", "") or ""),
                "last_name": getattr(contact, "last_name", None),
            }

        dice = getattr(msg, "dice", None)
        if dice is not None:
            dice_payload: dict[str, Any] = {
                "media_type": "dice",
                "emoji": str(getattr(dice, "emoji", "") or "🎲"),
            }
            if getattr(dice, "value", None) is not None:
                dice_payload["value"] = int(dice.value)
            return dice_payload

        poll_dict = _extract_poll_dict(msg)
        if poll_dict is not None:
            return {"media_type": "poll", **poll_dict}

        return None

    @staticmethod
    def _merge_comment_media_items(
        existing_media: dict[str, Any] | None,
        new_items: list[tuple[int, dict[str, Any]]],
    ) -> dict[str, Any] | None:
        images: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        seen_ids: list[int] = []

        def _append_item(mid: int | None, item: dict[str, Any]) -> None:
            if mid is not None and mid in seen_ids:
                return
            if mid is not None:
                seen_ids.append(mid)
            entry = dict(item)
            if mid is not None and "tg_msg_id" not in entry:
                entry["tg_msg_id"] = mid
            if str(entry.get("media_type") or "").lower() == "photo":
                images.append(entry)
            else:
                files.append(entry)

        if isinstance(existing_media, dict):
            for raw_id in existing_media.get("tg_msg_ids") or []:
                if isinstance(raw_id, int) and raw_id not in seen_ids:
                    seen_ids.append(raw_id)
            if existing_media.get("media_type") == "album" or (
                isinstance(existing_media.get("images"), list)
                or isinstance(existing_media.get("files"), list)
            ):
                for img in existing_media.get("images") or []:
                    if isinstance(img, dict):
                        images.append(dict(img))
                for f in existing_media.get("files") or []:
                    if isinstance(f, dict):
                        files.append(dict(f))
            else:
                single = {k: v for k, v in existing_media.items() if k != "tg_msg_ids"}
                if str(single.get("media_type") or "").lower() == "photo":
                    images.append(single)
                elif single:
                    files.append(single)

        for mid, item in new_items:
            _append_item(mid, item)

        total = len(images) + len(files)
        if total == 0:
            return None
        if total == 1:
            single_out = dict(images[0] if images else files[0])
            if len(seen_ids) > 1:
                single_out["tg_msg_ids"] = seen_ids
            return single_out
        album_out: dict[str, Any] = {
            "media_type": "album",
            "tg_msg_ids": seen_ids,
        }
        if images:
            album_out["images"] = images
        if files:
            album_out["files"] = files
        return album_out

    async def _on_discussion_album_flushed(self, msgs: list[Any]) -> None:
        await self._ingest_discussion_messages(msgs)

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
                elif target_post is None:
                    # Channel post may still be in `album_buffer` debounce; cache briefly so flush picks it up
                    if len(self._unlinked_autoforwards) >= 500:
                        oldest_key = next(iter(self._unlinked_autoforwards))
                        self._unlinked_autoforwards.pop(oldest_key, None)
                    self._unlinked_autoforwards[(channel.id, int(fwd_channel_msg_id))] = int(msg.id)
                return None

        # 2. Strict loop guard #1: ignore messages echoed by our own bot!
        from_user = getattr(msg, "from_user", None)
        if from_user is not None:
            if bool(getattr(from_user, "is_self", False)):
                return None
            if self.bot_id is not None and int(getattr(from_user, "id", 0)) == int(self.bot_id):
                return None

        # 3. Buffer discussion media groups (`media_group_id`) so multi-photo/file albums become ONE comment
        media_group_id = getattr(msg, "media_group_id", None)
        if media_group_id is not None:
            chat_key = disc_chat_id if disc_chat_id is not None else channel.id
            self.discussion_album_buffer.add(f"{chat_key}:{media_group_id}", msg)
            return None

        return await self._ingest_discussion_messages([msg])

    async def _resolve_post_id_from_telegram_thread(
        self,
        session: AsyncSession,
        channel: Channel,
        disc_chat_id: int | None,
        first_msg: Any,
    ) -> int | None:
        """Walk parent discussion messages in Telegram to link missing `tg_discussion_root_id` on demand."""
        tg_reader = self.user_client or self.client
        if disc_chat_id is None or tg_reader is None or not hasattr(tg_reader, "get_messages"):
            return None

        queue: list[int] = []
        for raw_cid in (
            getattr(first_msg, "reply_to_top_message_id", None),
            getattr(first_msg, "reply_to_message_id", None),
        ):
            if raw_cid is not None:
                cid = int(raw_cid)
                if cid > 0 and cid not in queue:
                    queue.append(cid)

        visited: set[int] = set()
        hops = 0
        while queue and hops < 8:
            cur_id = queue.pop(0)
            if cur_id in visited:
                continue
            visited.add(cur_id)
            hops += 1

            # Check local DB first at each hop
            existing_c = (
                await session.execute(
                    select(Comment).where(
                        Comment.channel_id == channel.id,
                        Comment.tg_msg_id == cur_id,
                    )
                )
            ).scalar_one_or_none()
            if existing_c is not None:
                return existing_c.post_id

            existing_p = (
                await session.execute(
                    select(Post).where(
                        Post.channel_id == channel.id,
                        Post.tg_discussion_root_id == cur_id,
                    )
                )
            ).scalar_one_or_none()
            if existing_p is not None:
                return existing_p.id

            try:
                fetched = await tg_reader.get_messages(int(disc_chat_id), cur_id)
            except Exception:
                logger.debug(
                    "Failed walking discussion thread msg %s in %s", cur_id, disc_chat_id, exc_info=True
                )
                continue
            if isinstance(fetched, list):
                fetched = fetched[0] if fetched else None
            if fetched is None or getattr(fetched, "empty", False):
                continue

            ch_msg_id = self._extract_autoforward_channel_msg_id(fetched, channel)
            if ch_msg_id is not None:
                target_post = await find_post_by_channel_tg_msg(session, channel.id, ch_msg_id)
                if target_post is not None:
                    if target_post.tg_discussion_root_id is None:
                        conflict = (
                            await session.execute(
                                select(Post.id).where(
                                    Post.channel_id == channel.id,
                                    Post.tg_discussion_root_id == int(fetched.id),
                                )
                            )
                        ).scalar_one_or_none()
                        if conflict is None:
                            target_post.tg_discussion_root_id = int(fetched.id)
                            await session.flush()
                            logger.info(
                                "Linked discussion root msg %s -> post #%s (on-demand)",
                                fetched.id,
                                target_post.id,
                            )
                    return target_post.id

            for nxt_raw in (
                getattr(fetched, "reply_to_top_message_id", None),
                getattr(fetched, "reply_to_message_id", None),
            ):
                if nxt_raw is not None:
                    nxt = int(nxt_raw)
                    if nxt > 0 and nxt not in visited and nxt not in queue:
                        queue.append(nxt)

        return None

    async def merge_comment_group_rows(
        self,
        session: AsyncSession,
        channel: Channel,
        group_comments: list[Comment],
        *,
        tg_media_group_id: str,
    ) -> Comment | None:
        """Merge already-persisted duplicate album Comment rows into one keeper Comment cleanly."""
        if not group_comments:
            return None
        ordered_by_id = sorted(group_comments, key=lambda c: c.id)
        keeper = ordered_by_id[0]
        extras = ordered_by_id[1:]

        if not extras:
            if keeper.tg_media_group_id != tg_media_group_id:
                keeper.tg_media_group_id = tg_media_group_id
                await session.commit()
            return keeper

        ordered_by_tg = sorted(group_comments, key=lambda c: (int(c.tg_msg_id or 0), c.id))
        media_items: list[tuple[int, dict[str, Any]]] = []
        best_text = keeper.body_text or ""
        best_html = keeper.body_html or ""

        for c in ordered_by_tg:
            mid = int(c.tg_msg_id or c.id)
            if not best_text and (c.body_text or "").strip():
                best_text = c.body_text
                best_html = c.body_html
            if isinstance(c.media, dict):
                if c.media.get("media_type") == "album":
                    for sub in c.media.get("images") or []:
                        if isinstance(sub, dict):
                            media_items.append((int(sub.get("tg_msg_id") or mid), sub))
                    for sub in c.media.get("files") or []:
                        if isinstance(sub, dict):
                            media_items.append((int(sub.get("tg_msg_id") or mid), sub))
                else:
                    media_items.append((mid, c.media))

        merged_media = self._merge_comment_media_items(None, media_items)
        if merged_media is not None:
            all_tg_ids = sorted(
                {int(c.tg_msg_id) for c in group_comments if c.tg_msg_id is not None}
                | {int(x) for x in (merged_media.get("tg_msg_ids") or []) if isinstance(x, int)}
            )
            if all_tg_ids:
                merged_media["tg_msg_ids"] = all_tg_ids

        extra_ids = [c.id for c in extras]
        post_id = keeper.post_id
        min_tg_msg_id = min(
            (int(c.tg_msg_id) for c in group_comments if c.tg_msg_id is not None),
            default=keeper.tg_msg_id,
        )

        # Re-point replies & pending notification queue references before deleting duplicate rows
        await session.execute(
            update(Comment)
            .where(Comment.parent_id.in_(extra_ids))
            .values(parent_id=keeper.id)
        )
        await session.execute(
            update(NotificationQueue)
            .where(NotificationQueue.comment_id.in_(extra_ids))
            .values(comment_id=keeper.id)
        )

        # Preserve TG & web reactions attached to duplicate rows
        existing_tg_r = (
            await session.execute(
                select(CommentTgReaction).where(CommentTgReaction.comment_id == keeper.id)
            )
        ).scalars().all()
        keeper_tg_keys = {r.reaction_key: r for r in existing_tg_r}
        for r in (
            await session.execute(
                select(CommentTgReaction).where(CommentTgReaction.comment_id.in_(extra_ids))
            )
        ).scalars().all():
            if r.reaction_key in keeper_tg_keys:
                keeper_tg_keys[r.reaction_key].count = max(
                    keeper_tg_keys[r.reaction_key].count, r.count
                )
            else:
                new_r = CommentTgReaction(
                    comment_id=keeper.id,
                    reaction_key=r.reaction_key,
                    emoji=r.emoji,
                    custom_emoji_id=r.custom_emoji_id,
                    count=r.count,
                )
                session.add(new_r)
                keeper_tg_keys[r.reaction_key] = new_r

        existing_web_r = (
            await session.execute(
                select(CommentWebReaction).where(CommentWebReaction.comment_id == keeper.id)
            )
        ).scalars().all()
        keeper_web_keys = {(r.reaction_key, r.actor_key) for r in existing_web_r}
        for r in (
            await session.execute(
                select(CommentWebReaction).where(CommentWebReaction.comment_id.in_(extra_ids))
            )
        ).scalars().all():
            wkey = (r.reaction_key, r.actor_key)
            if wkey not in keeper_web_keys:
                session.add(
                    CommentWebReaction(
                        comment_id=keeper.id,
                        reaction_key=r.reaction_key,
                        emoji=r.emoji,
                        custom_emoji_id=r.custom_emoji_id,
                        actor_key=r.actor_key,
                    )
                )
                keeper_web_keys.add(wkey)

        await session.flush()
        await session.execute(delete(Comment).where(Comment.id.in_(extra_ids)))
        await session.flush()

        keeper.tg_msg_id = min_tg_msg_id
        keeper.tg_media_group_id = tg_media_group_id
        if merged_media is not None:
            keeper.media = merged_media
        if best_text:
            keeper.body_text = best_text
            keeper.body_html = best_html

        linked_user = await session.get(User, keeper.user_id) if keeper.user_id is not None else None
        await session.commit()

        for extra_id in extra_ids:
            await self._emit_sse(
                "comment.deleted",
                {"post_id": post_id, "comment_id": extra_id},
            )
        comment_dto = self._serialize_tg_comment_dto(keeper, linked_user)
        await self._emit_sse("comment.updated", comment_dto)
        return keeper

    async def _ingest_discussion_messages(
        self,
        msgs: list[Any],
        *,
        fallback_post_id: int | None = None,
    ) -> Comment | None:
        sorted_msgs = sorted(
            (m for m in msgs if getattr(m, "service", None) is None and int(getattr(m, "id", 0) or 0) > 0),
            key=lambda m: int(getattr(m, "id", 0) or 0),
        )
        if not sorted_msgs:
            return None

        first_msg = sorted_msgs[0]
        first_msg_id = int(getattr(first_msg, "id", 0))
        disc_chat_id = getattr(getattr(first_msg, "chat", None), "id", None)
        raw_mg_id = getattr(first_msg, "media_group_id", None)
        mg_id_str = str(raw_mg_id) if raw_mg_id is not None else None

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

            # Strict loop guard #2: deduplicate by UNIQUE(channel_id, tg_msg_id)
            batch_ids = [int(getattr(m, "id", 0) or 0) for m in sorted_msgs]
            existing_by_id = (
                await session.execute(
                    select(Comment).where(
                        Comment.channel_id == channel.id,
                        Comment.tg_msg_id.in_(batch_ids),
                    )
                )
            ).scalars().all()
            existing_ids = {int(c.tg_msg_id) for c in existing_by_id if c.tg_msg_id is not None}

            # Also check if an existing Comment already owns this media_group_id (slow split album)
            existing_group_comment: Comment | None = None
            if mg_id_str is not None:
                existing_group_comment = (
                    await session.execute(
                        select(Comment).where(
                            Comment.channel_id == channel.id,
                            Comment.tg_media_group_id == mg_id_str,
                        )
                    )
                ).scalar_one_or_none()
                if existing_group_comment is not None and isinstance(existing_group_comment.media, dict):
                    for mid in existing_group_comment.media.get("tg_msg_ids") or []:
                        if isinstance(mid, int):
                            existing_ids.add(mid)

            pending_msgs = [m for m in sorted_msgs if int(getattr(m, "id", 0)) not in existing_ids]
            if not pending_msgs:
                return None

            # Resolve post_id & parent_id via reply_to_top_message_id / reply_to_message_id walk
            post_id: int | None = existing_group_comment.post_id if existing_group_comment is not None else None
            parent_comment: Comment | None = None

            if post_id is None:
                reply_to_top_id = getattr(first_msg, "reply_to_top_message_id", None)
                reply_to_msg_id = getattr(first_msg, "reply_to_message_id", None)

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
                    post_id = await self._resolve_post_id_from_telegram_thread(
                        session, channel, disc_chat_id, first_msg
                    )

                if post_id is None and fallback_post_id is not None:
                    post_id = int(fallback_post_id)

            if post_id is None:
                return None

            # Extract caption/body + download media across all messages in the batch
            body_text = ""
            body_html = ""
            media_items: list[tuple[int, dict[str, Any]]] = []
            custom_ids: list[str] = []

            for m in pending_msgs:
                mid = int(getattr(m, "id", 0))
                raw_text = getattr(m, "text", None) or getattr(m, "caption", None) or ""
                raw_text_str = str(raw_text)
                entities = (
                    getattr(m, "entities", None)
                    if getattr(m, "text", None)
                    else getattr(m, "caption_entities", None)
                )
                if raw_text_str.strip() and not body_text:
                    body_text = raw_text_str.strip()
                    raw_html = _render_entities_to_html(raw_text_str, entities)
                    body_html = sanitize_tg_comment_html(raw_html).strip()
                if entities:
                    for en in entities:
                        etype = str(getattr(en, "type", "")).upper().split(".")[-1]
                        cid = getattr(en, "custom_emoji_id", None)
                        if etype == "CUSTOM_EMOJI" and cid is not None:
                            custom_ids.append(str(cid).strip())

                downloaded = await self._download_discussion_media(m, channel.id, mid)
                if downloaded is not None:
                    media_items.append((mid, downloaded))

            if self.client is not None and custom_ids:
                await cache_custom_emoji_stickers(
                    self.client, self.settings.exports_dir, custom_ids
                )

            if existing_group_comment is not None:
                merged_media = self._merge_comment_media_items(
                    existing_group_comment.media if isinstance(existing_group_comment.media, dict) else None,
                    media_items,
                )
                if merged_media is None and not body_text:
                    return None
                if merged_media is not None:
                    existing_group_comment.media = merged_media
                if body_text and not existing_group_comment.body_text:
                    existing_group_comment.body_text = body_text
                    existing_group_comment.body_html = body_html
                linked_user: User | None = None
                if existing_group_comment.user_id is not None:
                    linked_user = await session.get(User, existing_group_comment.user_id)
                await session.commit()
                comment_dto = self._serialize_tg_comment_dto(existing_group_comment, linked_user)
                await self._emit_sse("comment.updated", comment_dto)
                return existing_group_comment

            media_payload = self._merge_comment_media_items(None, media_items)
            if not body_text and media_payload is None:
                return None

            # Author metadata + optional linked web user + lazy avatar cache
            from_user = getattr(first_msg, "from_user", None)
            tg_author_id: int | None = None
            tg_author_name: str | None = None
            tg_author_username: str | None = None
            tg_author_avatar: str | None = None
            linked_user = None

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
                sender_chat = getattr(first_msg, "sender_chat", None)
                if sender_chat is not None:
                    tg_author_id = int(sender_chat.id)
                    tg_author_name = getattr(sender_chat, "title", "Channel")
                    tg_author_username = getattr(sender_chat, "username", None)

            created_at = _ensure_utc(getattr(first_msg, "date", None))
            comment = Comment(
                post_id=post_id,
                parent_id=parent_comment.id if parent_comment is not None else None,
                source="telegram",
                channel_id=channel.id,
                tg_msg_id=first_msg_id,
                tg_media_group_id=mg_id_str,
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

        is_auto = bool(
            getattr(msg, "automatic_forward", False)
            or getattr(msg, "is_automatic_forward", False)
        )
        if not is_auto:
            return None

        fwd_chat = getattr(msg, "forward_from_chat", None)
        fwd_msg_id = getattr(msg, "forward_from_message_id", None)

        origin = getattr(msg, "forward_origin", None)
        origin_chat = (
            getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
            if origin is not None
            else None
        )
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
            "media": serialize_comment_media(comment.media, self.settings.API_PREFIX),
            "created_at": format_iso_utc(comment.created_at),
            "edited_at": format_iso_utc(comment.edited_at) if comment.edited_at else None,
            "deleted": comment.deleted_at is not None,
            "is_mine": False,
            "can_delete": False,
            "reactions": [],
        }

    async def handle_discussion_deleted(self, messages: Any) -> list[int]:
        """Soft-delete comments whose TG discussion messages were deleted and emit SSE."""
        msg_list = list(messages) if isinstance(messages, (list, tuple, set)) else [messages]
        ids_by_chat: dict[int | None, set[int]] = {}
        for m in msg_list:
            mid = getattr(m, "id", None)
            if mid is None:
                continue
            cid = getattr(getattr(m, "chat", None), "id", None)
            chat_key = int(cid) if cid is not None else self.settings.TG_DISCUSSION_CHAT_ID
            ids_by_chat.setdefault(chat_key, set()).add(int(mid))

        if not ids_by_chat:
            return []

        deleted_events: list[tuple[int, int]] = []
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            for disc_chat_id, msg_ids in ids_by_chat.items():
                if not msg_ids:
                    continue
                if disc_chat_id is not None:
                    channel = (
                        await session.execute(
                            select(Channel).where(Channel.tg_discussion_chat_id == disc_chat_id)
                        )
                    ).scalar_one_or_none()
                else:
                    channel = await get_active_channel(session)
                if channel is None:
                    continue

                rows = list(
                    (
                        await session.execute(
                            select(Comment).where(
                                Comment.channel_id == channel.id,
                                Comment.tg_msg_id.in_(msg_ids),
                                Comment.deleted_at.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for c in rows:
                    c.deleted_at = now
                    deleted_events.append((c.post_id, c.id))

            if deleted_events:
                await session.commit()

        for post_id, comment_id in deleted_events:
            await self._emit_sse(
                "comment.deleted",
                {"post_id": post_id, "comment_id": comment_id},
            )
        return [cid for _, cid in deleted_events]

    async def handle_channel_deleted(self, messages: Any) -> list[int]:
        """Soft-delete posts whose primary TG channel messages were deleted and emit SSE."""
        msg_list = list(messages) if isinstance(messages, (list, tuple, set)) else [messages]
        ids_by_chat: dict[int | None, set[int]] = {}
        for m in msg_list:
            mid = getattr(m, "id", None)
            if mid is None:
                continue
            cid = getattr(getattr(m, "chat", None), "id", None)
            chat_key = int(cid) if cid is not None else self.settings.TG_CHANNEL_ID
            ids_by_chat.setdefault(chat_key, set()).add(int(mid))

        if not ids_by_chat:
            return []

        deleted_post_ids: list[int] = []
        async with self.session_factory() as session:
            for chan_chat_id, msg_ids in ids_by_chat.items():
                if not msg_ids:
                    continue
                if chan_chat_id is not None:
                    channel = (
                        await session.execute(
                            select(Channel).where(Channel.tg_chat_id == chan_chat_id)
                        )
                    ).scalar_one_or_none()
                else:
                    channel = await get_active_channel(session)
                if channel is None:
                    continue

                rows = list(
                    (
                        await session.execute(
                            select(Post).where(
                                Post.channel_id == channel.id,
                                Post.tg_msg_id.in_(msg_ids),
                                Post.is_deleted.is_(False),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for post in rows:
                    post.is_deleted = True
                    deleted_post_ids.append(post.id)

            if deleted_post_ids:
                await session.commit()
                await generate_exports(session, self.settings)

        for post_id in deleted_post_ids:
            await self._emit_sse("post.deleted", {"post_id": post_id})
        return deleted_post_ids

    async def handle_user_deleted(self, messages: Any) -> tuple[list[int], list[int]]:
        """Route user_client deletion updates to channel post or discussion comment deletion."""
        msg_list = list(messages) if isinstance(messages, (list, tuple, set)) else [messages]
        channel_msgs: list[Any] = []
        disc_msgs: list[Any] = []
        for m in msg_list:
            cid = getattr(getattr(m, "chat", None), "id", None)
            if cid is None:
                continue
            cid_int = int(cid)
            if self.settings.TG_CHANNEL_ID is not None and cid_int == int(self.settings.TG_CHANNEL_ID):
                channel_msgs.append(m)
            elif (
                self.settings.TG_DISCUSSION_CHAT_ID is not None
                and cid_int == int(self.settings.TG_DISCUSSION_CHAT_ID)
            ):
                disc_msgs.append(m)
            elif self.settings.TG_CHANNEL_ID is None and self.settings.TG_DISCUSSION_CHAT_ID is None:
                channel_msgs.append(m)
                disc_msgs.append(m)

        deleted_posts = await self.handle_channel_deleted(channel_msgs) if channel_msgs else []
        deleted_comments = await self.handle_discussion_deleted(disc_msgs) if disc_msgs else []
        if deleted_posts or deleted_comments:
            logger.info(
                "user_client processed deletions: posts=%s comments=%s",
                deleted_posts,
                deleted_comments,
            )
        return deleted_posts, deleted_comments

    # ------------------------------------------------------------------
    # Live Reaction Count & Poll Updates
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_raw_peer_chat_id(peer: Any) -> int | None:
        if peer is None:
            return None
        channel_id = getattr(peer, "channel_id", None)
        if channel_id is not None:
            return int(f"-100{int(channel_id)}")
        chat_id = getattr(peer, "chat_id", None)
        if chat_id is not None:
            return -int(chat_id)
        user_id = getattr(peer, "user_id", None)
        if user_id is not None:
            return int(user_id)
        return None

    @staticmethod
    def _extract_raw_reaction_items(raw_reactions: Any) -> list[Any] | None:
        if raw_reactions is None:
            return None
        raw_list = getattr(raw_reactions, "results", raw_reactions)
        if raw_list is None:
            return None
        items: list[Any] = []
        for entry in raw_list:
            count = int(getattr(entry, "count", 0) or 0)
            reaction_obj = getattr(entry, "reaction", None)
            if reaction_obj is not None:
                emoji = getattr(reaction_obj, "emoticon", None)
                cid = getattr(reaction_obj, "document_id", None)
            else:
                emoji = getattr(entry, "emoji", None)
                cid = getattr(entry, "custom_emoji_id", None)
            if not emoji and cid is None:
                continue
            items.append(
                SimpleNamespace(
                    emoji=emoji,
                    custom_emoji_id=str(cid) if cid is not None else None,
                    count=count,
                )
            )
        return items

    @staticmethod
    def _normalize_reaction_rows(
        raw_reactions: Sequence[Any],
    ) -> list[tuple[str, str | None, str | None, int]]:
        normalized: list[tuple[str, str | None, str | None, int]] = []
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
            normalized.append((rkey, emoji, cid_str, int(getattr(r, "count", 0) or 0)))
        return normalized

    @classmethod
    async def _replace_tg_reactions(
        cls,
        session: AsyncSession,
        *,
        model: type[TgReaction] | type[CommentTgReaction],
        fk_col_name: str,
        target_id: int,
        raw_reactions: Sequence[Any],
    ) -> None:
        fk_col = getattr(model, fk_col_name)
        await session.execute(delete(model).where(fk_col == target_id))
        for rkey, emoji, cid_str, count in cls._normalize_reaction_rows(raw_reactions):
            session.add(
                model(
                    **{fk_col_name: target_id},
                    reaction_key=rkey,
                    emoji=emoji,
                    custom_emoji_id=cid_str,
                    count=count,
                )
            )

    @classmethod
    def _build_delta_reaction_items(
        cls,
        existing_rows: Sequence[TgReaction | CommentTgReaction],
        *,
        old_reactions: Sequence[Any],
        new_reactions: Sequence[Any],
    ) -> list[Any]:
        counts: dict[str, tuple[str | None, str | None, int]] = {
            r.reaction_key: (r.emoji, r.custom_emoji_id, int(r.count))
            for r in existing_rows
            if int(r.count) > 0
        }
        for rkey, _emoji, _cid, _cnt in cls._normalize_reaction_rows(old_reactions):
            cur = counts.get(rkey)
            if cur is None:
                continue
            next_cnt = cur[2] - 1
            if next_cnt <= 0:
                counts.pop(rkey, None)
            else:
                counts[rkey] = (cur[0], cur[1], next_cnt)
        for rkey, emoji, cid, _cnt in cls._normalize_reaction_rows(new_reactions):
            cur = counts.get(rkey)
            if cur is None:
                counts[rkey] = (emoji, cid, 1)
            else:
                counts[rkey] = (cur[0] or emoji, cur[1] or cid, cur[2] + 1)
        return [
            SimpleNamespace(emoji=emoji, custom_emoji_id=cid, count=cnt)
            for emoji, cid, cnt in counts.values()
            if cnt > 0
        ]

    async def _persist_post_reactions(
        self,
        session: AsyncSession,
        *,
        post: Post,
        raw_reactions: Sequence[Any],
    ) -> None:
        post_id = post.id
        incoming_rows = self._normalize_reaction_rows(raw_reactions)
        incoming_map = {rkey: count for rkey, _, _, count in incoming_rows}
        existing_map = {r.reaction_key: int(r.count) for r in post.tg_reactions}
        if existing_map == incoming_map:
            return
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

    async def _persist_comment_reactions(
        self,
        session: AsyncSession,
        *,
        comment: Comment,
        raw_reactions: Sequence[Any],
    ) -> None:
        comment_id = comment.id
        incoming_rows = self._normalize_reaction_rows(raw_reactions)
        incoming_map = {rkey: count for rkey, _, _, count in incoming_rows}
        existing_map = {r.reaction_key: int(r.count) for r in comment.tg_reactions}
        if existing_map == incoming_map:
            return
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

    async def _find_reaction_target(
        self,
        session: AsyncSession,
        *,
        chat_id_int: int,
        msg_id: int,
    ) -> tuple[Post | None, Comment | None]:
        channel = await self._resolve_channel_for_chat(session, chat_id_int)
        if channel is not None:
            post = await find_post_by_channel_tg_msg(session, channel.id, msg_id)
            return post, None

        disc_channel = (
            await session.execute(
                select(Channel).where(Channel.tg_discussion_chat_id == chat_id_int)
            )
        ).scalar_one_or_none()
        if disc_channel is None:
            return None, None

        comment = (
            await session.execute(
                select(Comment)
                .options(selectinload(Comment.tg_reactions))
                .join(Post, Post.id == Comment.post_id)
                .where(
                    Comment.channel_id == disc_channel.id,
                    Comment.tg_msg_id == msg_id,
                    Comment.deleted_at.is_(None),
                    Post.is_deleted.is_(False),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return None, comment

    async def _apply_reaction_count_update(
        self,
        *,
        chat_id_int: int,
        msg_id: int,
        raw_reactions: Sequence[Any],
    ) -> None:
        async with self.session_factory() as session:
            post, comment = await self._find_reaction_target(
                session, chat_id_int=chat_id_int, msg_id=msg_id
            )
            if post is not None:
                await self._persist_post_reactions(session, post=post, raw_reactions=raw_reactions)
            elif comment is not None:
                await self._persist_comment_reactions(
                    session, comment=comment, raw_reactions=raw_reactions
                )

    async def _apply_reaction_user_delta_update(
        self,
        *,
        chat_id_int: int,
        msg_id: int,
        old_reactions: Sequence[Any],
        new_reactions: Sequence[Any],
    ) -> None:
        async with self.session_factory() as session:
            post, comment = await self._find_reaction_target(
                session, chat_id_int=chat_id_int, msg_id=msg_id
            )
            if post is not None:
                updated_rows = self._build_delta_reaction_items(
                    post.tg_reactions,
                    old_reactions=old_reactions,
                    new_reactions=new_reactions,
                )
                await self._persist_post_reactions(session, post=post, raw_reactions=updated_rows)
            elif comment is not None:
                updated_rows = self._build_delta_reaction_items(
                    comment.tg_reactions,
                    old_reactions=old_reactions,
                    new_reactions=new_reactions,
                )
                await self._persist_comment_reactions(
                    session, comment=comment, raw_reactions=updated_rows
                )

    async def handle_message_reaction_update(self, update: Any) -> None:
        """Handle bot `UpdateBotMessageReaction` (e.g. reactions on discussion group comments)."""
        chat_id = getattr(getattr(update, "chat", None), "id", None)
        msg_id = getattr(update, "message_id", None)
        if chat_id is None or msg_id is None:
            return
        chat_id_int = int(chat_id)
        msg_id_int = int(msg_id)

        if self.user_client is not None and hasattr(self.user_client, "get_messages"):
            try:
                res = await self.user_client.get_messages(chat_id_int, [msg_id_int])
                msg = res[0] if isinstance(res, list) and res else (res if not isinstance(res, list) else None)
                if msg is not None and not getattr(msg, "empty", False):
                    raw_reactions = _extract_reactions_list(msg) or []
                    await self._apply_reaction_count_update(
                        chat_id_int=chat_id_int,
                        msg_id=msg_id_int,
                        raw_reactions=raw_reactions,
                    )
                    return
            except Exception:
                logger.debug(
                    "Authoritative reaction fetch failed for %s/%s; using delta fallback",
                    chat_id_int,
                    msg_id_int,
                    exc_info=True,
                )

        await self._apply_reaction_user_delta_update(
            chat_id_int=chat_id_int,
            msg_id=msg_id_int,
            old_reactions=getattr(update, "old_reaction", None) or [],
            new_reactions=getattr(update, "new_reaction", None) or [],
        )

    async def handle_reaction_count_update(self, update: Any) -> None:
        chat_id = getattr(getattr(update, "chat", None), "id", None)
        msg_id = getattr(update, "message_id", None)
        if msg_id is None or chat_id is None:
            return
        raw_reactions = getattr(update, "reactions", None) or []
        await self._apply_reaction_count_update(
            chat_id_int=int(chat_id),
            msg_id=int(msg_id),
            raw_reactions=raw_reactions,
        )

    async def handle_raw_reaction_update(self, update: Any) -> None:
        """Process raw MTProto UpdateMessageReactions from read-only user_client."""
        chat_id_int = self._extract_raw_peer_chat_id(getattr(update, "peer", None))
        msg_id = getattr(update, "msg_id", None)
        if chat_id_int is None or msg_id is None:
            return
        watched_chats = {
            int(cid)
            for cid in (self.settings.TG_CHANNEL_ID, self.settings.TG_DISCUSSION_CHAT_ID)
            if cid is not None
        }
        if watched_chats and chat_id_int not in watched_chats:
            return
        raw_reactions = self._extract_raw_reaction_items(getattr(update, "reactions", None))
        if raw_reactions is None:
            return
        await self._apply_reaction_count_update(
            chat_id_int=chat_id_int,
            msg_id=int(msg_id),
            raw_reactions=raw_reactions,
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
        """Handle owner DM slash commands and buffer original image files."""
        from_user = getattr(msg, "from_user", None)
        if (
            self.settings.OWNER_TG_ID is None
            or from_user is None
            or int(from_user.id) != int(self.settings.OWNER_TG_ID)
        ):
            return

        raw_text = str(getattr(msg, "text", None) or "").strip()
        if raw_text.startswith("/"):
            cmd_head = raw_text.split(None, 1)[0].split("@", 1)[0].lower()
            if cmd_head in ("/fullsync", "/sync"):
                if self.client is not None:
                    await self.client.send_message(
                        chat_id=self.settings.OWNER_TG_ID,
                        text="🔄 Running full channel sync from ID #1 (existing images preserved)...",
                    )
                try:
                    summary = await self.crawl_channel_history(
                        full=True,
                        trigger_prompt=False,
                        preserve_existing_images=True,
                    )
                    reply_text = (
                        f"✅ Full sync ({summary.get('status')}): "
                        f"crawled {summary.get('crawled_messages', 0)} messages, "
                        f"created {summary.get('posts_created', 0)}, "
                        f"updated {summary.get('posts_updated', 0)}, "
                        f"deleted {summary.get('deleted_posts', 0)}, "
                        f"max TG ID #{summary.get('max_msg_id', 0)}."
                    )
                except Exception as exc:
                    logger.exception("/fullsync command failed")
                    reply_text = f"❌ Full sync failed: {exc}"
                if self.client is not None:
                    await self.client.send_message(
                        chat_id=self.settings.OWNER_TG_ID,
                        text=reply_text,
                    )
                return

            if cmd_head in ("/stickers", "/sticker"):
                reply_text = await self._execute_owner_sticker_command(raw_text)
                if self.client is not None:
                    await self.client.send_message(
                        chat_id=self.settings.OWNER_TG_ID,
                        text=reply_text,
                    )
                return

            reply_text, updated_allowed = await execute_owner_reaction_command(
                raw_text,
                getattr(msg, "entities", None),
                session_factory=self.session_factory,
            )
            if updated_allowed is not None:
                cids = [
                    str(item["custom_emoji_id"])
                    for item in updated_allowed
                    if item.get("custom_emoji_id")
                ]
                if cids:
                    await cache_custom_emoji_stickers(
                        self.client, self.settings.exports_dir, cids
                    )
                await self._emit_sse(
                    "reactions.allowed_updated",
                    {"allowed_reactions": updated_allowed},
                )
            if self.client is not None:
                await self.client.send_message(
                    chat_id=self.settings.OWNER_TG_ID,
                    text=reply_text,
                )
            return

        if getattr(msg, "sticker", None) is not None:
            await self._handle_owner_dm_sticker(msg)
            return

        if raw_text and parse_sticker_set_url(raw_text) is not None:
            _, reply_text = await self.import_sticker_set(raw_text)
            if self.client is not None:
                await self.client.send_message(
                    chat_id=self.settings.OWNER_TG_ID,
                    text=reply_text,
                )
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

    @staticmethod
    def _resolve_sticker_spec(sticker: Any, fallback_stem: str) -> tuple[str, str, bool, bool]:
        """Derive sanitized `(file_name, ext, is_animated, is_video)` for a Telegram Sticker."""
        is_animated = bool(getattr(sticker, "is_animated", False))
        is_video = bool(getattr(sticker, "is_video", False))
        ext = ".tgs" if is_animated else (".webm" if is_video else ".webp")
        raw_uid = (
            getattr(sticker, "file_unique_id", None)
            or getattr(sticker, "file_id", None)
            or fallback_stem
        )
        stem = sanitize_sticker_filename(str(raw_uid))
        max_stem = 80 - len(ext)
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)] or fallback_stem[:max_stem]
        file_name = f"{stem[:max_stem]}{ext}"
        return file_name, ext, is_animated, is_video

    async def _download_sticker_file(
        self,
        target_arg: Any,
        *,
        dest: Path,
        ext: str,
        log_context: str,
    ) -> bool:
        """Download a sticker via `client.download_media` into `dest` atomically."""
        if self.client is None or not hasattr(self.client, "download_media"):
            return False
        tmp_path = dest.parent / f".{dest.name}.{uuid.uuid4().hex[:8]}.tmp{ext}"
        try:
            dl = await self.client.download_media(target_arg, file_name=str(tmp_path))
            dl_path = Path(str(dl)) if dl else None
            if dl_path is not None and dl_path.is_file() and dl_path.stat().st_size > 0:
                os.replace(dl_path, dest)
                return True
            if dl_path is not None:
                dl_path.unlink(missing_ok=True)
            tmp_path.unlink(missing_ok=True)
            return False
        except Exception:
            logger.warning("Failed downloading sticker (%s)", log_context, exc_info=True)
            tmp_path.unlink(missing_ok=True)
            return False

    async def _handle_owner_dm_sticker(self, msg: Any) -> None:
        sticker = getattr(msg, "sticker", None)
        if sticker is None or self.settings.OWNER_TG_ID is None:
            return
        pack_name = sanitize_sticker_pack_name(getattr(sticker, "set_name", None))
        fallback_stem = f"msg_{getattr(msg, 'id', uuid.uuid4().hex[:8])}"
        file_name, ext, is_animated, is_video = self._resolve_sticker_spec(sticker, fallback_stem)

        pack_dir = self.settings.media_dir / "stickers" / pack_name
        pack_dir.mkdir(parents=True, exist_ok=True)
        dest = pack_dir / file_name

        target_arg = getattr(sticker, "file_id", None) or msg
        downloaded = await self._download_sticker_file(
            target_arg, dest=dest, ext=ext, log_context=f"owner dm {pack_name}/{file_name}"
        )

        if not downloaded:
            if self.client is not None:
                await self.client.send_message(
                    chat_id=self.settings.OWNER_TG_ID,
                    text="❌ Failed to download sticker file.",
                )
            return

        emoji = getattr(sticker, "emoji", None) or None
        width = getattr(sticker, "width", None)
        height = getattr(sticker, "height", None)
        await asyncio.to_thread(
            upsert_sticker_manifest_entry,
            pack_dir,
            pack_name=pack_name,
            file_name=file_name,
            emoji=emoji,
            width=int(width) if width is not None else 512,
            height=int(height) if height is not None else 512,
            is_animated=is_animated,
            is_video=is_video,
            file_id=getattr(sticker, "file_id", None) or None,
        )
        (self.settings.media_dir / "stickers" / ".no_auto_seed").unlink(missing_ok=True)

        if self.client is not None:
            emoji_label = f"{emoji} " if emoji else ""
            await self.client.send_message(
                chat_id=self.settings.OWNER_TG_ID,
                text=f"✅ Saved sticker {emoji_label}to `{pack_name}/{file_name}`",
            )

    async def import_sticker_set(self, ref_or_name: str) -> tuple[bool, str]:
        """Fetch and download a Telegram sticker set into `media_dir/stickers/<pack>/`."""
        if self.client is None or not hasattr(self.client, "get_sticker_set"):
            return False, "❌ Telegram client is not connected."

        short_name = parse_sticker_set_ref(ref_or_name)
        if short_name is None:
            return (
                False,
                "❌ Invalid sticker pack reference. Send a `t.me/addstickers/<name>` link or pack short name.",
            )

        try:
            sticker_set = await self.client.get_sticker_set(short_name)
        except Exception as exc:
            logger.warning("Failed to fetch sticker set %s: %s", short_name, exc)
            return False, f"❌ Could not fetch sticker pack `{short_name}`."

        if sticker_set is None:
            return False, f"❌ Could not fetch sticker pack `{short_name}`."

        raw_stickers = list(getattr(sticker_set, "stickers", None) or [])
        if not raw_stickers:
            return False, f"❌ Sticker pack `{short_name}` has no stickers."

        resolved_name = getattr(sticker_set, "name", None) or short_name
        pack_name = sanitize_sticker_pack_name(resolved_name)
        raw_title = getattr(sticker_set, "title", None)
        pack_title = str(raw_title).strip() if raw_title is not None and str(raw_title).strip() else pack_name
        pack_dir = self.settings.media_dir / "stickers" / pack_name
        pack_dir.mkdir(parents=True, exist_ok=True)

        sem = asyncio.Semaphore(4)
        seen_names: set[str] = set()
        tasks: list[asyncio.Task[tuple[str, dict[str, Any] | None]]] = []

        async def _process_one(
            sticker: Any,
            file_name: str,
            ext: str,
            is_animated: bool,
            is_video: bool,
        ) -> tuple[str, dict[str, Any] | None]:
            dest = pack_dir / file_name
            width = getattr(sticker, "width", None)
            height = getattr(sticker, "height", None)
            entry = {
                "file": file_name,
                "emoji": getattr(sticker, "emoji", None) or None,
                "width": int(width) if width is not None else 512,
                "height": int(height) if height is not None else 512,
                "is_animated": is_animated,
                "is_video": is_video,
                "file_id": getattr(sticker, "file_id", None) or None,
            }
            if dest.is_file() and dest.stat().st_size > 0:
                return "skipped", entry
            if dest.is_file() and dest.stat().st_size == 0:
                dest.unlink(missing_ok=True)

            target_arg = getattr(sticker, "file_id", None) or sticker
            async with sem:
                ok = await self._download_sticker_file(
                    target_arg, dest=dest, ext=ext, log_context=f"{pack_name}/{file_name}"
                )
            if ok:
                return "saved", entry
            return "failed", None

        for idx, sticker in enumerate(raw_stickers):
            file_name, ext, is_animated, is_video = self._resolve_sticker_spec(
                sticker, f"{pack_name}_{idx}"
            )
            if file_name in seen_names:
                suffix = f"_{idx}"
                stem = Path(file_name).stem[: 80 - len(ext) - len(suffix)]
                file_name = f"{stem}{suffix}{ext}"
            seen_names.add(file_name)
            tasks.append(
                asyncio.create_task(_process_one(sticker, file_name, ext, is_animated, is_video))
            )

        results = await asyncio.gather(*tasks)
        saved = 0
        skipped = 0
        failed = 0
        manifest_entries: list[dict[str, Any]] = []
        for status, entry in results:
            if status == "saved":
                saved += 1
                if entry is not None:
                    manifest_entries.append(entry)
            elif status == "skipped":
                skipped += 1
                if entry is not None:
                    manifest_entries.append(entry)
            else:
                failed += 1

        if manifest_entries:
            await asyncio.to_thread(
                save_sticker_pack_manifest,
                pack_dir,
                pack_name=pack_name,
                pack_title=pack_title,
                entries=manifest_entries,
            )
            (self.settings.media_dir / "stickers" / ".no_auto_seed").unlink(missing_ok=True)
        else:
            try:
                pack_dir.rmdir()
            except OSError:
                pass
            return (
                False,
                f"❌ Failed to download any stickers from **{pack_title}** (`{pack_name}`).",
            )

        summary_parts = [f"{saved} saved"]
        if skipped:
            summary_parts.append(f"{skipped} skipped")
        if failed:
            summary_parts.append(f"{failed} failed")
        return (
            True,
            f"✅ Imported sticker pack **{pack_title}** (`{pack_name}`): {', '.join(summary_parts)}.",
        )

    @staticmethod
    def _has_installed_pack_stickers(stickers_root: Path, pack_name: str) -> bool:
        if not stickers_root.is_dir():
            return False
        target_lower = pack_name.lower()
        for entry in stickers_root.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if entry.name.lower() != target_lower:
                continue
            if any(
                f.is_file() and f.suffix.lower() in STICKER_EXTENSIONS and f.stat().st_size > 0
                for f in entry.iterdir()
            ):
                return True
        return False

    @staticmethod
    def _pack_missing_file_ids(stickers_root: Path, pack_name: str) -> bool:
        if not stickers_root.is_dir():
            return False
        target_lower = pack_name.lower()
        for entry in stickers_root.iterdir():
            if not entry.is_dir() or entry.name.startswith(".") or entry.name.lower() != target_lower:
                continue
            manifest_path = entry / "manifest.json"
            if not manifest_path.is_file():
                return False
            try:
                manifest = json.loads(manifest_path.read_text("utf-8"))
            except Exception:
                return False
            stickers = manifest.get("stickers") if isinstance(manifest, dict) else None
            if not isinstance(stickers, list) or not stickers:
                return False
            return any(
                isinstance(item, dict) and not str(item.get("file_id") or "").strip()
                for item in stickers
            )
        return False

    async def ensure_default_sticker_packs(self) -> dict[str, int]:
        """Sync missing `DEFAULT_STICKER_PACKS` unless dismissed by the owner."""
        counts = {"imported": 0, "skipped": 0, "failed": 0}
        if self.client is None or not hasattr(self.client, "get_sticker_set"):
            return counts

        stickers_root = self.settings.media_dir / "stickers"
        dismissed = await asyncio.to_thread(load_dismissed_default_packs, stickers_root)

        for raw_ref in self.settings.DEFAULT_STICKER_PACKS:
            short_name = parse_sticker_set_ref(raw_ref)
            if short_name is None:
                logger.warning("Skipping invalid default sticker pack ref: %r", raw_ref)
                counts["failed"] += 1
                continue

            pack_name = sanitize_sticker_pack_name(short_name)
            if short_name.lower() in dismissed or pack_name.lower() in dismissed:
                counts["skipped"] += 1
                continue

            already_installed = await asyncio.to_thread(
                self._has_installed_pack_stickers, stickers_root, pack_name
            )
            if already_installed:
                if await asyncio.to_thread(self._pack_missing_file_ids, stickers_root, pack_name):
                    try:
                        await self.import_sticker_set(short_name)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.debug("Failed backfilling sticker file_ids for %s", short_name, exc_info=True)
                counts["skipped"] += 1
                continue

            try:
                ok, summary = await self.import_sticker_set(short_name)
                if ok:
                    counts["imported"] += 1
                else:
                    counts["failed"] += 1
                    logger.warning("Default sticker pack %s not imported: %s", short_name, summary)
            except asyncio.CancelledError:
                raise
            except Exception:
                counts["failed"] += 1
                logger.warning("Failed syncing default sticker pack %s", short_name, exc_info=True)

        return counts

    async def ensure_custom_emoji_meta(self) -> int:
        """Backfill `.emoji_meta.json` Unicode fallbacks from DB posts and Telegram API."""
        import re

        from app.media.stickers import upsert_custom_emoji_meta

        candidate_roots: list[Path] = [self.settings.exports_dir / "emoji"]
        epochs_dir = self.settings.media_dir / "epochs"
        if epochs_dir.is_dir():
            candidate_roots.extend(
                p / "emoji" for p in sorted(epochs_dir.iterdir()) if p.is_dir()
            )

        existing_roots = [r for r in candidate_roots if r.is_dir()]
        if not existing_roots:
            return 0

        # 1. Seed from existing post HTML (<i class="custom-emoji" emoji-src=".../emoji/{id}.ext">🔥</i>)
        db_alts: dict[str, str] = {}
        if self.session_factory is not None:
            pattern = re.compile(
                r'<i\b[^>]*\bclass=["\'][^"\']*\bcustom-emoji\b[^"\']*["\'][^>]*'
                r'(?:data-custom-emoji-id=["\'](\d{1,32})["\']|emoji-src=["\'][^"\']*/emoji/(\d{1,32})\.[a-z0-9]+["\'])'
                r'[^>]*>([^<]{1,16})</i>',
                re.IGNORECASE,
            )
            try:
                async with self.session_factory() as session:
                    rows = (
                        await session.execute(
                            select(Post.text_html).where(Post.text_html.like("%custom-emoji%"))
                        )
                    ).scalars().all()
                for raw_html in rows:
                    if not raw_html:
                        continue
                    for m in pattern.finditer(raw_html):
                        cid = (m.group(1) or m.group(2) or "").strip()
                        alt_text = html.unescape(m.group(3)).strip()
                        if cid and alt_text and alt_text != "✦":
                            db_alts.setdefault(cid, alt_text)
            except Exception:
                logger.debug("Failed seeding custom emoji alts from DB", exc_info=True)

        refreshed = 0
        for root in existing_roots:
            existing_meta = await asyncio.to_thread(load_custom_emoji_meta, root)
            all_ids = [
                p.stem
                for p in sorted(root.iterdir(), key=lambda x: x.name)
                if p.is_file()
                and p.suffix.lower() in EMOJI_EXTENSIONS
                and p.stem.isascii()
                and p.stem.isdigit()
            ]
            if db_alts:
                seed_updates = {
                    cid: db_alts[cid]
                    for cid in all_ids
                    if cid in db_alts and not existing_meta.get(cid)
                }
                if seed_updates:
                    await asyncio.to_thread(upsert_custom_emoji_meta, root, seed_updates)
                    existing_meta.update(seed_updates)
                    refreshed += len(seed_updates)

            missing_ids = [cid for cid in all_ids if not existing_meta.get(cid)]
            if (
                missing_ids
                and self.client is not None
                and hasattr(self.client, "get_custom_emoji_stickers")
            ):
                try:
                    await cache_custom_emoji_stickers(self.client, root.parent, missing_ids)
                    refreshed += len(missing_ids)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Failed backfilling custom emoji meta in %s", root, exc_info=True)
        return refreshed

    async def _execute_owner_sticker_command(self, raw_text: str) -> str:
        parts = raw_text.strip().split(None, 2)
        subcmd = parts[1].lower() if len(parts) > 1 else "list"
        arg = parts[2].strip() if len(parts) > 2 else ""

        if subcmd in ("list", "ls"):
            packs = await asyncio.to_thread(
                list_sticker_packs,
                self.settings.media_dir,
                self.settings.API_PREFIX,
                auto_seed=False,
            )
            if not packs:
                return (
                    "No custom stickers installed yet. "
                    "Send a sticker, paste a `t.me/addstickers/...` link, or run `/stickers add <link|name>`!"
                )
            lines = ["Installed sticker packs:"]
            for pack in packs:
                title = pack.get("title") or pack["name"]
                title_part = f"{title} " if title and title != pack["name"] else ""
                lines.append(f"\n{title_part}[{pack['name']}] ({len(pack['stickers'])})")
                for st in pack["stickers"]:
                    em = f"{st['emoji']} " if st.get("emoji") else ""
                    lines.append(f"- {em}{st['id']}")
            lines.append(
                "\nAdd pack: /stickers add <link|name>"
                "\nSync defaults: /stickers defaults"
                "\nRemove sticker: /stickers rm <pack/file>"
                "\nRemove pack: /stickers rmpack <pack>"
            )
            return "\n".join(lines)[:4000]

        if subcmd in ("defaults", "sync"):
            if self.client is None or not hasattr(self.client, "get_sticker_set"):
                return "❌ Telegram client is not connected."
            counts = await self.ensure_default_sticker_packs()
            return (
                f"🔄 Synced default sticker packs: "
                f"{counts['imported']} imported, {counts['skipped']} skipped, {counts['failed']} failed."
            )

        if subcmd in ("add", "pull", "import"):
            if not arg:
                return "Usage: /stickers add <t.me/addstickers/... | short_name>"
            _, msg_text = await self.import_sticker_set(arg)
            return msg_text

        if subcmd in ("rm", "remove", "del", "delete"):
            if not arg:
                return "Usage: /stickers rm <pack/file>"
            _, msg_text = await asyncio.to_thread(
                remove_sticker_by_id, self.settings.media_dir, arg
            )
            return msg_text

        if subcmd in ("rmpack", "rmdir", "delpack"):
            if not arg:
                return "Usage: /stickers rmpack <pack>"
            _, msg_text = await asyncio.to_thread(
                remove_sticker_pack, self.settings.media_dir, arg
            )
            return msg_text

        return (
            "Sticker commands:\n"
            "/stickers list\n"
            "/stickers add <t.me/addstickers/... | name>\n"
            "/stickers defaults\n"
            "/stickers rm <pack/file>\n"
            "/stickers rmpack <pack>\n\n"
            "Or send/forward any Telegram sticker or paste a t.me/addstickers/... link to install it."
        )

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
