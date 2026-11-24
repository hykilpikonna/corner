from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import nh3
from dateutil import parser as date_parser
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import Channel, Comment, CommentTgReaction, Post
from app.db.repo import ensure_channel, make_reaction_key, sanitize_post_html, upsert_post
from app.db.session import close_db, init_db
from app.exports import generate_exports
from app.media.fingerprint import compute_image_fingerprint

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

_KNOWN_POST_KEYS = {
    "id",
    "date",
    "edited",
    "edited_at",
    "text",
    "caption",
    "forwarded_from",
    "type",
    "views",
    "forwards",
    "author",
    "reply",
    "images",
    "files",
    "quote",
    "reactions",
    "show_caption_above_media",
    "has_media_spoiler",
    "media_group_id",
}

_INLINE_MEDIA_ATTR_RE = re.compile(
    r"""(?P<attr>\b(?:src|data-src|data-full|emoji-src))=(?P<q>["'])(?P<val>(?:\./)?(?:media|emoji)/[^"']+)(?P=q)""",
    re.IGNORECASE,
)

_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_DISALLOWED_URL_SCHEMES = ("data:", "javascript:", "vbscript:")
_INTERNAL_TG_MEDIA_KEYS = frozenset({"access_hash", "file_reference", "bytes", "stripped_thumb"})


def sanitize_comment_html(raw_html: str) -> str:
    return nh3.clean(
        raw_html,
        tags=ALLOWED_COMMENT_TAGS,
        attributes=ALLOWED_COMMENT_ATTRS,
        url_schemes={"http", "https", "mailto"},
    )


def _normalize_link_href(raw_url: str) -> str:
    url = raw_url.strip()
    if not url:
        return ""
    if url.startswith("//") or url.lower().startswith(_DISALLOWED_URL_SCHEMES):
        return ""
    if url.startswith(("/", "#")) or _URL_SCHEME_RE.match(url):
        return url
    return f"https://{url}"


def _sanitize_media_metadata(obj: Any) -> Any:
    """Recursively strip internal Telethon binary/auth fields (access_hash, file_reference)."""
    if isinstance(obj, dict):
        cleaned: dict[str, Any] = {}
        for k, v in obj.items():
            if str(k) in _INTERNAL_TG_MEDIA_KEYS:
                continue
            cleaned[k] = _sanitize_media_metadata(v)
        return cleaned
    if isinstance(obj, list):
        return [_sanitize_media_metadata(item) for item in obj]
    return obj


def _normalize_channel_id(chat_id: int | None) -> int | None:
    """Normalize Telegram Bot/Kurigram -100<id> channel IDs and bare positive channel IDs."""
    if chat_id is None:
        return None
    s = str(int(chat_id))
    if s.startswith("-100") and len(s) > 4:
        return int(s[4:])
    return abs(int(chat_id))


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        raw = value.strip()
        if raw.isdigit():
            return datetime.fromtimestamp(int(raw), tz=UTC)
        dt = date_parser.isoparse(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _parse_views(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    raw = str(value).strip().upper().replace(",", "")
    if not raw:
        return None
    try:
        if raw.endswith("K"):
            return int(float(raw[:-1]) * 1_000)
        if raw.endswith("M"):
            return int(float(raw[:-1]) * 1_000_000)
        return int(float(raw))
    except ValueError:
        return None


def _parse_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    raw = str(value).strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return None


def _rewrite_epoch_media_path(raw_url: str | None, channel_id: int) -> str | None:
    if not raw_url:
        return None
    stripped = raw_url.strip()
    if not stripped:
        return None
    lowered = stripped.lower()
    if stripped.startswith("//") or lowered.startswith(_DISALLOWED_URL_SCHEMES):
        return None
    if lowered.startswith(("http://", "https://")):
        return stripped
    if stripped.startswith("/"):
        return stripped
    cleaned = stripped.removeprefix("./").lstrip("/")
    if not cleaned:
        return None
    prefix = f"epochs/{channel_id}/"
    if cleaned.startswith(prefix):
        return cleaned
    return f"{prefix}{cleaned}"


def _rewrite_inline_html_urls(
    text_html: str | None, channel_id: int, api_prefix: str
) -> str | None:
    if not text_html:
        return text_html
    prefix = api_prefix.rstrip("/")

    def _repl(match: re.Match[str]) -> str:
        attr = match.group("attr")
        quote = match.group("q")
        val = match.group("val").removeprefix("./").lstrip("/")
        return f"{attr}={quote}{prefix}/media/epochs/{channel_id}/{val}{quote}"

    return _INLINE_MEDIA_ATTR_RE.sub(_repl, text_html)


def _extract_numeric_stem(url: str | None) -> int | None:
    if not url:
        return None
    stem = Path(url).stem
    if stem.isdigit():
        return int(stem)
    return None


def _hardlink_tree(src: Path, dst: Path) -> None:
    """Recursively hardlink regular files from src into dst, falling back to copy on EXDEV."""
    for root, dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        target_dir = dst / rel_root
        target_dir.mkdir(parents=True, exist_ok=True)
        for d in dirs:
            (target_dir / d).mkdir(parents=True, exist_ok=True)
        for filename in files:
            src_file = Path(root) / filename
            dst_file = target_dir / filename
            if not src_file.is_file() or src_file.is_symlink():
                continue
            if dst_file.exists() or dst_file.is_symlink():
                try:
                    if os.path.samefile(src_file, dst_file):
                        continue
                except OSError:
                    pass
                dst_file.unlink()
            try:
                os.link(src_file, dst_file)
            except OSError as exc:
                logger.warning(
                    "Hardlink failed for %s -> %s (%s); falling back to copy",
                    src_file,
                    dst_file,
                    exc,
                )
                shutil.copy2(src_file, dst_file)


def _safe_copy2(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> str:
    """Copy file safely even when dst is already a hardlink to src."""
    dst_path = Path(dst)
    if dst_path.exists():
        try:
            if os.path.samefile(src, dst_path):
                return str(dst_path)
        except OSError:
            pass
    return str(shutil.copy2(src, dst))


def _sync_epoch_media(source_dir: Path, dest_epoch_dir: Path, media_mode: str) -> None:
    if media_mode == "skip":
        return
    dest_epoch_dir.mkdir(parents=True, exist_ok=True)
    for subdir_name in ("media", "emoji", "originals"):
        src = source_dir / subdir_name
        if not src.exists():
            continue
        dst = dest_epoch_dir / subdir_name
        if media_mode == "hardlink":
            _hardlink_tree(src, dst)
        elif media_mode == "copy":
            shutil.copytree(src, dst, copy_function=_safe_copy2, dirs_exist_ok=True)
        else:
            raise ValueError(f"Unsupported media_mode: {media_mode}")


async def import_epoch1_posts(
    session: AsyncSession,
    settings: Settings,
    source_dir: Path,
    *,
    channel_id: int = 1,
    tg_chat_id: int | None = None,
    title: str = "Epoch 1 (Archived)",
    username: str | None = None,
    media_mode: str = "copy",
    regenerate_exports: bool = True,
) -> dict[str, Any]:
    """Import a TelegramBackup posts.json directory into Epoch 1."""
    posts_json_path = source_dir / "posts.json"
    if not posts_json_path.is_file():
        raise FileNotFoundError(f"posts.json not found in {source_dir}")

    channel = await ensure_channel(
        session,
        channel_id=channel_id,
        tg_chat_id=tg_chat_id,
        title=title,
        username=username,
        status="archived",
        id_offset=0,
    )

    dest_epoch_dir = settings.media_dir / "epochs" / str(channel.id)
    _sync_epoch_media(source_dir, dest_epoch_dir, media_mode)

    source_dir_resolved = source_dir.resolve()
    media_dir_resolved = settings.media_dir.resolve()

    raw_posts: list[dict[str, Any]] = json.loads(posts_json_path.read_text(encoding="utf-8"))
    raw_posts.sort(key=lambda item: int(item["id"]))

    stats = {
        "channel_id": channel.id,
        "posts_imported": 0,
        "images_imported": 0,
        "images_fingerprinted": 0,
        "polls_imported": 0,
        "reactions_imported": 0,
        "max_id": 0,
    }

    pending_replies: list[tuple[int, int]] = []
    imported_ids: set[int] = set()

    for raw in raw_posts:
        post_id = int(raw["id"])
        imported_ids.add(post_id)
        stats["max_id"] = max(stats["max_id"], post_id)

        date = _parse_datetime(raw["date"])
        edited_raw = raw.get("edited_at") or raw.get("edited")
        edited_at = _parse_datetime(edited_raw) if edited_raw else None

        raw_text = raw.get("text")
        if raw_text is None:
            raw_text = raw.get("caption")
        rewritten_html = _rewrite_inline_html_urls(raw_text, channel.id, settings.API_PREFIX)
        text_html = sanitize_post_html(rewritten_html) if rewritten_html else rewritten_html

        all_msg_ids: set[int] = {post_id}

        # Process images
        images_payload: list[dict[str, Any]] = []
        for idx, raw_img in enumerate(raw.get("images") or []):
            if not isinstance(raw_img, dict) or not raw_img.get("url"):
                continue
            raw_img_url = str(raw_img["url"])
            rel_url = _rewrite_epoch_media_path(raw_img_url, channel.id)
            if not rel_url:
                continue
            rel_thumb = _rewrite_epoch_media_path(raw_img.get("thumb"), channel.id)
            rel_orig = _rewrite_epoch_media_path(raw_img.get("original_url"), channel.id)

            img_tg_id = _extract_numeric_stem(raw_img_url)
            if img_tg_id is not None:
                all_msg_ids.add(img_tg_id)

            width = int(raw_img.get("width") or 0)
            height = int(raw_img.get("height") or 0)
            phash: str | None = None
            rgb_sig: bytes | None = None

            # Locate image file strictly inside media_dir or source_dir before fingerprinting
            candidate_pairs: list[tuple[Path, Path]] = []
            if not rel_url.startswith(("http://", "https://", "/")):
                candidate_pairs.append((settings.media_dir / rel_url, media_dir_resolved))
            raw_rel = raw_img_url.removeprefix("./").lstrip("/")
            if raw_rel and not raw_img_url.startswith(("http://", "https://", "/")):
                candidate_pairs.append((source_dir / raw_rel, source_dir_resolved))

            for cand, allowed_root in candidate_pairs:
                try:
                    resolved = cand.resolve()
                    if not resolved.is_relative_to(allowed_root):
                        continue
                except OSError:
                    continue
                if resolved.is_file():
                    try:
                        phash, rgb_sig, det_w, det_h = compute_image_fingerprint(resolved)
                        if width <= 0:
                            width = det_w
                        if height <= 0:
                            height = det_h
                        stats["images_fingerprinted"] += 1
                    except Exception as exc:
                        logger.warning("Failed to fingerprint %s: %s", resolved, exc)
                    break

            images_payload.append(
                {
                    "sort_order": idx,
                    "tg_msg_id": img_tg_id,
                    "url": rel_url,
                    "thumb_url": rel_thumb,
                    "original_url": rel_orig,
                    "original_size": _parse_optional_int(raw_img.get("original_size")),
                    "original_name": raw_img.get("original_name"),
                    "size": _parse_optional_int(raw_img.get("size")),
                    "width": width,
                    "height": height,
                    "spoiler": bool(raw_img.get("spoiler", False)),
                    "phash": phash,
                    "rgb_sig": rgb_sig,
                }
            )
            stats["images_imported"] += 1

        # Process files & extract poll if present
        other_files: list[dict[str, Any]] = []
        poll_payload: dict[str, Any] | None = None
        for raw_file in raw.get("files") or []:
            if not isinstance(raw_file, dict):
                continue
            if raw_file.get("media_type") == "poll":
                poll_payload = {
                    "id": str(raw_file.get("id") or f"epoch{channel.id}_poll_{post_id}"),
                    "question": str(raw_file.get("question") or ""),
                    "is_closed": bool(raw_file.get("is_closed", False)),
                    "is_anonymous": bool(raw_file.get("is_anonymous", True)),
                    "type": str(raw_file.get("type") or "REGULAR"),
                    "allow_multiple_answers": bool(raw_file.get("allow_multiple_answers", False)),
                    "correct_option_idx": raw_file.get("correct_option_idx"),
                    "explanation": raw_file.get("explanation"),
                    "options": raw_file.get("options") or [],
                }
                stats["polls_imported"] += 1
                continue

            f_copy = dict(raw_file)
            if f_copy.get("url"):
                file_tg_id = _extract_numeric_stem(str(f_copy["url"]))
                if file_tg_id is not None:
                    all_msg_ids.add(file_tg_id)
                rewritten_file_url = _rewrite_epoch_media_path(str(f_copy["url"]), channel.id)
                if rewritten_file_url:
                    f_copy["url"] = rewritten_file_url
                else:
                    f_copy.pop("url", None)
            if f_copy.get("thumb"):
                rewritten_thumb = _rewrite_epoch_media_path(str(f_copy["thumb"]), channel.id)
                if rewritten_thumb:
                    f_copy["thumb"] = rewritten_thumb
                else:
                    f_copy.pop("thumb", None)
            other_files.append(f_copy)

        # Process reply snapshot
        reply_snapshot: dict[str, Any] | None = None
        if isinstance(raw.get("reply"), dict):
            reply_snapshot = dict(raw["reply"])
            if reply_snapshot.get("thumb"):
                rewritten_rthumb = _rewrite_epoch_media_path(
                    str(reply_snapshot["thumb"]), channel.id
                )
                if rewritten_rthumb:
                    reply_snapshot["thumb"] = rewritten_rthumb
                else:
                    reply_snapshot.pop("thumb", None)
            target_reply_id = _parse_optional_int(reply_snapshot.get("id"))
            if target_reply_id is not None:
                pending_replies.append((post_id, target_reply_id))

        reactions_raw = raw.get("reactions") or []
        stats["reactions_imported"] += len(reactions_raw)

        extra_meta = {k: v for k, v in raw.items() if k not in _KNOWN_POST_KEYS} or None

        await upsert_post(
            session,
            channel=channel,
            canonical_id=post_id,
            tg_msg_id=post_id,
            tg_media_group_id=(
                str(raw["media_group_id"]) if raw.get("media_group_id") is not None else None
            ),
            tg_all_msg_ids=sorted(all_msg_ids),
            date=date,
            edited_at=edited_at,
            post_type=raw.get("type"),
            text_html=text_html,
            author=raw.get("author"),
            views=_parse_views(raw.get("views")),
            forwards=_parse_optional_int(raw.get("forwards")),
            forwarded_from=raw.get("forwarded_from"),
            reply_snapshot=reply_snapshot,
            quote=raw.get("quote"),
            files=other_files or None,
            extra_meta=extra_meta,
            show_caption_above_media=bool(raw.get("show_caption_above_media", False)),
            has_media_spoiler=bool(raw.get("has_media_spoiler", False)),
            images=images_payload,
            tg_reactions=reactions_raw,
            poll=poll_payload,
        )
        stats["posts_imported"] += 1

    # Second pass: link reply_to_post_id when target post exists
    if pending_replies:
        existing_ids_res = await session.execute(select(Post.id))
        existing_ids = set(existing_ids_res.scalars().all())
        for child_id, parent_post_id in pending_replies:
            if parent_post_id in existing_ids and child_id != parent_post_id:
                child_post = await session.get(Post, child_id)
                if child_post is not None:
                    child_post.reply_to_post_id = parent_post_id
        await session.flush()

    if stats["max_id"] > channel.last_crawled_msg_id:
        channel.last_crawled_msg_id = stats["max_id"]
        await session.flush()

    await session.commit()

    if regenerate_exports:
        await generate_exports(session, settings)

    return stats


# ---------------------------------------------------------------------------
# Epoch 1 Discussion Group Comment Importer (Telethon JSON/JSONL + tdesktop)
# ---------------------------------------------------------------------------


@dataclass
class _ParsedDiscussionMsg:
    tg_msg_id: int
    created_at: datetime
    edited_at: datetime | None
    reply_to_msg_id: int | None
    reply_to_top_id: int | None
    fwd_channel_id: int | None
    fwd_channel_post_id: int | None
    fwd_from_name: str | None
    is_forwarded_from_channel: bool
    tg_author_id: int | None
    tg_author_name: str | None
    tg_author_username: str | None
    body_text: str
    body_html: str
    media: dict[str, Any] | None
    tg_reactions: list[tuple[str, str | None, str | None, int]] = field(default_factory=list)
    media_rel_paths: list[str] = field(default_factory=list)


def _load_discussion_dump(
    source_path: Path,
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """Load either JSON (tdesktop result.json / Telethon array/object) or JSONL."""
    raw_text = source_path.read_text(encoding="utf-8").strip()
    if not raw_text:
        return [], {}

    users_map: dict[int, dict[str, Any]] = {}
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        messages = []
        for line in raw_text.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, dict):
                messages.append(obj)
        return messages, users_map

    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)], users_map

    if isinstance(data, dict):
        raw_users = data.get("users")
        if isinstance(raw_users, dict):
            for k, v in raw_users.items():
                uid = _parse_optional_int(k)
                if uid is not None and isinstance(v, dict):
                    users_map[uid] = v
        elif isinstance(raw_users, list):
            for u in raw_users:
                if isinstance(u, dict) and u.get("id") is not None:
                    uid = _parse_optional_int(u["id"])
                    if uid is not None:
                        users_map[uid] = u
        msgs = data.get("messages")
        if isinstance(msgs, list):
            return [item for item in msgs if isinstance(item, dict)], users_map
        return [data], users_map

    return [], users_map


def _parse_peer_id(peer: Any) -> tuple[str | None, int | None]:
    """Extract (kind, id) from Telethon Peer dict or tdesktop 'user123'/'channel456' string."""
    if peer is None:
        return None, None
    if isinstance(peer, int):
        return "user", peer
    if isinstance(peer, str):
        s = peer.strip()
        if s.startswith("user") and s[4:].isdigit():
            return "user", int(s[4:])
        if s.startswith("channel") and s[7:].isdigit():
            return "channel", int(s[7:])
        if s.lstrip("-").isdigit():
            return "user", int(s)
        return None, None
    if isinstance(peer, dict):
        kind = str(peer.get("_") or "")
        if "user_id" in peer and peer["user_id"] is not None:
            return "user", int(peer["user_id"])
        if "channel_id" in peer and peer["channel_id"] is not None:
            return "channel", int(peer["channel_id"])
        if "chat_id" in peer and peer["chat_id"] is not None:
            return "chat", int(peer["chat_id"])
        if kind == "PeerUser" and peer.get("id") is not None:
            return "user", int(peer["id"])
        if kind == "PeerChannel" and peer.get("id") is not None:
            return "channel", int(peer["id"])
    return None, None


def _render_tdesktop_text(
    raw_text: Any, text_entities: list[dict[str, Any]] | None
) -> tuple[str, str]:
    """Convert Telegram Desktop `text` (str or mixed list) / `text_entities` to (plain, safe_html)."""
    segments: list[Any] = []
    if isinstance(text_entities, list) and text_entities:
        segments = text_entities
    elif isinstance(raw_text, list):
        segments = raw_text
    elif isinstance(raw_text, str):
        segments = [raw_text]

    plain_parts: list[str] = []
    html_parts: list[str] = []

    for seg in segments:
        if isinstance(seg, str):
            plain_parts.append(seg)
            html_parts.append(html.escape(seg).replace("\n", "<br>"))
            continue
        if not isinstance(seg, dict):
            continue
        t = str(seg.get("text") or "")
        etype = str(seg.get("type") or "plain")
        plain_parts.append(t)
        esc = html.escape(t).replace("\n", "<br>")
        if etype == "bold":
            html_parts.append(f"<b>{esc}</b>")
        elif etype == "italic":
            html_parts.append(f"<em>{esc}</em>")
        elif etype == "underline":
            html_parts.append(f"<u>{esc}</u>")
        elif etype == "strikethrough":
            html_parts.append(f"<del>{esc}</del>")
        elif etype == "code":
            html_parts.append(f"<code>{html.escape(t)}</code>")
        elif etype == "pre":
            lang = seg.get("language")
            if lang:
                html_parts.append(
                    f'<pre language="{html.escape(str(lang), quote=True)}"><code>{html.escape(t)}</code></pre>'
                )
            else:
                html_parts.append(f"<pre><code>{html.escape(t)}</code></pre>")
        elif etype == "text_link" and seg.get("href"):
            norm_href = _normalize_link_href(str(seg["href"]))
            if norm_href:
                html_parts.append(f'<a href="{html.escape(norm_href, quote=True)}">{esc}</a>')
            else:
                html_parts.append(esc)
        elif etype == "link":
            norm_href = _normalize_link_href(t)
            if norm_href:
                html_parts.append(f'<a href="{html.escape(norm_href, quote=True)}">{esc}</a>')
            else:
                html_parts.append(esc)
        elif etype == "mention":
            handle = html.escape(t.lstrip("@"), quote=True)
            html_parts.append(f'<a href="https://t.me/{handle}">{esc}</a>')
        elif etype == "blockquote":
            html_parts.append(f"<blockquote>{esc}</blockquote>")
        elif etype == "spoiler":
            html_parts.append(f'<span class="spoiler"><span>{esc}</span></span>')
        else:
            html_parts.append(esc)

    plain = "".join(plain_parts).strip()
    rendered = sanitize_comment_html("".join(html_parts))
    return plain, rendered


def _render_telethon_text(text: str, entities: list[dict[str, Any]] | None) -> tuple[str, str]:
    """Convert Telethon plain message text + UTF-16 offset entities to (plain, safe_html)."""
    if not text:
        return "", ""
    if not entities:
        safe = html.escape(text).replace("\n", "<br>")
        return text.strip(), sanitize_comment_html(safe)

    # Encode to UTF-16LE so Telegram UTF-16 code-unit offsets slice accurately
    utf16 = text.encode("utf-16-le")
    code_units = len(utf16) // 2

    def _u16_slice(start: int, end: int) -> str:
        s = max(0, min(start, code_units))
        e = max(s, min(end, code_units))
        return utf16[s * 2 : e * 2].decode("utf-16-le", errors="replace")

    # Sort non-overlapping / outer-first entities
    valid_entities = [
        en
        for en in entities
        if isinstance(en, dict) and en.get("offset") is not None and en.get("length") is not None
    ]
    valid_entities.sort(key=lambda en: (int(en["offset"]), -int(en["length"])))

    parts: list[str] = []
    cursor = 0
    for en in valid_entities:
        offset = int(en["offset"])
        length = int(en["length"])
        if length <= 0 or offset < cursor:
            continue
        end = offset + length
        if offset > cursor:
            parts.append(html.escape(_u16_slice(cursor, offset)).replace("\n", "<br>"))
        segment = _u16_slice(offset, end)
        esc = html.escape(segment).replace("\n", "<br>")
        etype = str(en.get("_") or en.get("type") or "")

        if etype in ("MessageEntityBold", "bold"):
            parts.append(f"<b>{esc}</b>")
        elif etype in ("MessageEntityItalic", "italic"):
            parts.append(f"<em>{esc}</em>")
        elif etype in ("MessageEntityUnderline", "underline"):
            parts.append(f"<u>{esc}</u>")
        elif etype in ("MessageEntityStrike", "strikethrough"):
            parts.append(f"<del>{esc}</del>")
        elif etype in ("MessageEntityCode", "code"):
            parts.append(f"<code>{html.escape(segment)}</code>")
        elif etype in ("MessageEntityPre", "pre"):
            lang = en.get("language")
            if lang:
                parts.append(
                    f'<pre language="{html.escape(str(lang), quote=True)}"><code>{html.escape(segment)}</code></pre>'
                )
            else:
                parts.append(f"<pre><code>{html.escape(segment)}</code></pre>")
        elif etype in ("MessageEntityTextUrl", "text_link") and en.get("url"):
            norm_href = _normalize_link_href(str(en["url"]))
            if norm_href:
                parts.append(f'<a href="{html.escape(norm_href, quote=True)}">{esc}</a>')
            else:
                parts.append(esc)
        elif etype in ("MessageEntityUrl", "url"):
            norm_href = _normalize_link_href(segment)
            if norm_href:
                parts.append(f'<a href="{html.escape(norm_href, quote=True)}">{esc}</a>')
            else:
                parts.append(esc)
        elif etype in ("MessageEntityMention", "mention"):
            handle = html.escape(segment.lstrip("@"), quote=True)
            parts.append(f'<a href="https://t.me/{handle}">{esc}</a>')
        elif etype in ("MessageEntityBlockquote", "blockquote"):
            parts.append(f"<blockquote>{esc}</blockquote>")
        elif etype in ("MessageEntitySpoiler", "spoiler"):
            parts.append(f'<span class="spoiler"><span>{esc}</span></span>')
        else:
            parts.append(esc)
        cursor = end

    if cursor < code_units:
        parts.append(html.escape(_u16_slice(cursor, code_units)).replace("\n", "<br>"))

    return text.strip(), sanitize_comment_html("".join(parts))


def _safe_tdesktop_rel_path(raw_val: Any) -> str | None:
    if not isinstance(raw_val, str):
        return None
    cleaned = raw_val.strip().replace("\\", "/")
    if not cleaned or cleaned.startswith("(") or cleaned.startswith("/"):
        return None
    parts = PurePosixPath(cleaned).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    return "/".join(parts)


def _normalize_tdesktop_comment_media(
    raw: dict[str, Any], *, media_prefix: str
) -> tuple[dict[str, Any] | None, list[str]]:
    rel_paths: list[str] = []
    prefix = media_prefix.strip("/")

    rel_photo = _safe_tdesktop_rel_path(raw.get("photo"))
    rel_thumb = _safe_tdesktop_rel_path(raw.get("thumbnail"))
    if rel_photo:
        rel_paths.append(rel_photo)
        out: dict[str, Any] = {
            "media_type": "photo",
            "url": f"{prefix}/{rel_photo}",
        }
        if rel_thumb:
            rel_paths.append(rel_thumb)
            out["thumb"] = f"{prefix}/{rel_thumb}"
        for k_src, k_dst in (("width", "width"), ("height", "height"), ("photo_file_size", "size")):
            if raw.get(k_src) is not None:
                out[k_dst] = raw[k_src]
        return out, rel_paths

    rel_file = _safe_tdesktop_rel_path(raw.get("file"))
    if not rel_file:
        return None, []
    rel_paths.append(rel_file)
    raw_kind = str(raw.get("media_type") or "").strip().lower()
    lower_file = rel_file.lower()

    if raw_kind == "sticker":
        mtype = "sticker"
    elif raw_kind == "animation":
        mtype = "animation"
    elif raw_kind in ("video_file", "video_message"):
        mtype = "video"
    elif raw_kind == "voice_message":
        mtype = "voice"
    elif raw_kind == "audio_file":
        mtype = "audio"
    else:
        mtype = "document"

    out = {
        "media_type": mtype,
        "url": f"{prefix}/{rel_file}",
    }
    if rel_thumb:
        rel_paths.append(rel_thumb)
        out["thumb"] = f"{prefix}/{rel_thumb}"
    if mtype == "sticker":
        if raw.get("sticker_emoji"):
            out["emoji"] = str(raw["sticker_emoji"])
        out["is_animated"] = lower_file.endswith(".tgs")
        out["is_video"] = lower_file.endswith(".webm")
    if raw.get("file_name"):
        out["file_name"] = str(raw["file_name"])
    elif mtype in ("document", "video", "animation"):
        out["file_name"] = PurePosixPath(rel_file).name
    if raw.get("mime_type"):
        out["mime_type"] = str(raw["mime_type"])
    if raw.get("file_size") is not None:
        out["size"] = raw["file_size"]
    for dim in ("width", "height"):
        if raw.get(dim) is not None:
            out[dim] = raw[dim]
    return out, rel_paths


def _parse_discussion_message(
    raw: dict[str, Any],
    users_map: dict[int, dict[str, Any]],
    *,
    media_prefix: str = "comments/epoch1",
) -> _ParsedDiscussionMsg | None:
    # Skip service messages
    if raw.get("type") == "service" or raw.get("_") == "MessageService":
        return None

    tg_msg_id = _parse_optional_int(raw.get("id"))
    if tg_msg_id is None:
        return None

    is_tdesktop = (
        raw.get("type") == "message"
        or "reply_to_message_id" in raw
        or "date_unixtime" in raw
        or ("from" in raw and "message" not in raw)
    )

    fwd_channel_id: int | None = None
    fwd_from_name: str | None = None
    tg_reactions: list[tuple[str, str | None, str | None, int]] = []
    media_rel_paths: list[str] = []

    if is_tdesktop:
        created_at = _parse_datetime(raw.get("date_unixtime") or raw.get("date"))
        edited_raw = raw.get("edited_unixtime") or raw.get("edited")
        edited_at = _parse_datetime(edited_raw) if edited_raw else None

        reply_to_msg_id = _parse_optional_int(raw.get("reply_to_message_id"))
        reply_to_top_id = _parse_optional_int(raw.get("reply_to_top_id"))

        fwd_channel_post_id = (
            _parse_optional_int(raw.get("forwarded_from_message_id"))
            or _parse_optional_int(raw.get("saved_from_msg_id"))
            or _parse_optional_int(raw.get("channel_post"))
        )
        if isinstance(raw.get("fwd_from"), dict):
            fwd = raw["fwd_from"]
            if fwd_channel_post_id is None:
                fwd_channel_post_id = _parse_optional_int(
                    fwd.get("channel_post") or fwd.get("saved_from_msg_id")
                )
            fk, fid = _parse_peer_id(fwd.get("from_id") or fwd.get("saved_from_peer"))
            if fk == "channel" and fid is not None:
                fwd_channel_id = _normalize_channel_id(fid)

        peer_kind, peer_id = _parse_peer_id(raw.get("from_id"))
        if fwd_channel_id is None and peer_kind == "channel" and peer_id is not None:
            fwd_channel_id = _normalize_channel_id(peer_id)

        raw_fwd_name = raw.get("forwarded_from") or raw.get("saved_from") or raw.get("from")
        if raw_fwd_name is not None:
            fwd_from_name = str(raw_fwd_name)

        is_fwd = bool(
            fwd_channel_post_id is not None
            or raw.get("forwarded_from")
            or raw.get("saved_from")
            or peer_kind == "channel"
        )

        author_name = raw.get("from")
        if author_name is not None:
            author_name = str(author_name)
        author_username = raw.get("from_username")

        body_text, body_html = _render_tdesktop_text(raw.get("text"), raw.get("text_entities"))
        media, media_rel_paths = _normalize_tdesktop_comment_media(raw, media_prefix=media_prefix)

        seen_rkeys: dict[str, tuple[str, str | None, str | None, int]] = {}
        for rx in raw.get("reactions") or []:
            if not isinstance(rx, dict):
                continue
            cnt = _parse_optional_int(rx.get("count")) or 0
            if cnt <= 0:
                continue
            if rx.get("type") == "emoji" and rx.get("emoji"):
                em = str(rx["emoji"])
                rkey = make_reaction_key(emoji=em)
                prev = seen_rkeys.get(rkey)
                seen_rkeys[rkey] = (
                    rkey,
                    em,
                    None,
                    (prev[3] + cnt) if prev is not None else cnt,
                )
        tg_reactions = list(seen_rkeys.values())
    else:
        # Telethon raw to_dict() format
        created_at = _parse_datetime(raw.get("date"))
        edited_raw = raw.get("edit_date") or raw.get("edited_at")
        edited_at = _parse_datetime(edited_raw) if edited_raw else None

        reply_to = raw.get("reply_to") if isinstance(raw.get("reply_to"), dict) else {}
        reply_to_msg_id = _parse_optional_int(
            reply_to.get("reply_to_msg_id") or raw.get("reply_to_msg_id")
        )
        reply_to_top_id = _parse_optional_int(
            reply_to.get("reply_to_top_id") or raw.get("reply_to_top_id")
        )

        fwd = raw.get("fwd_from") if isinstance(raw.get("fwd_from"), dict) else {}
        fwd_channel_post_id = _parse_optional_int(
            fwd.get("channel_post")
            or fwd.get("saved_from_msg_id")
            or raw.get("saved_from_msg_id")
        )
        peer_kind, peer_id = _parse_peer_id(raw.get("from_id"))
        fwd_from_kind, fwd_from_id = _parse_peer_id(
            fwd.get("from_id") or fwd.get("saved_from_peer")
        )
        if fwd_from_kind == "channel" and fwd_from_id is not None:
            fwd_channel_id = _normalize_channel_id(fwd_from_id)
        elif peer_kind == "channel" and peer_id is not None and bool(fwd):
            fwd_channel_id = _normalize_channel_id(peer_id)

        if fwd.get("from_name"):
            fwd_from_name = str(fwd["from_name"])

        is_fwd = bool(
            fwd_channel_post_id is not None
            or fwd_from_kind == "channel"
            or (peer_kind == "channel" and bool(fwd))
        )

        sender = raw.get("sender") or raw.get("from_user")
        if not isinstance(sender, dict) and peer_id is not None:
            sender = users_map.get(peer_id)
        author_name: str | None = None
        author_username: str | None = None
        if isinstance(sender, dict):
            first = str(sender.get("first_name") or "").strip()
            last = str(sender.get("last_name") or "").strip()
            author_name = (
                " ".join(part for part in (first, last) if part)
                or sender.get("title")
                or sender.get("name")
            )
            author_username = sender.get("username")
        elif raw.get("post_author"):
            author_name = str(raw["post_author"])

        raw_msg_text = str(raw.get("message") or raw.get("text") or "")
        entities = raw.get("entities") if isinstance(raw.get("entities"), list) else None
        body_text, body_html = _render_telethon_text(raw_msg_text, entities)

        raw_media = raw.get("media")
        media = _sanitize_media_metadata(raw_media) if isinstance(raw_media, dict) else None
        if not body_text and media is not None:
            media_kind = (
                media.get("media_type")
                or media.get("_")
                or ("sticker" if media.get("sticker_emoji") else "media")
            )
            body_text = f"[{media_kind}]"
            body_html = sanitize_comment_html(f"<em>{html.escape(body_text)}</em>")

    return _ParsedDiscussionMsg(
        tg_msg_id=tg_msg_id,
        created_at=created_at,
        edited_at=edited_at,
        reply_to_msg_id=reply_to_msg_id,
        reply_to_top_id=reply_to_top_id,
        fwd_channel_id=fwd_channel_id,
        fwd_channel_post_id=fwd_channel_post_id,
        fwd_from_name=fwd_from_name,
        is_forwarded_from_channel=is_fwd,
        tg_author_id=peer_id if peer_kind == "user" else None,
        tg_author_name=author_name,
        tg_author_username=author_username,
        body_text=body_text,
        body_html=body_html,
        media=media,
        tg_reactions=tg_reactions,
        media_rel_paths=media_rel_paths,
    )


def _normalize_match_text(value: str | None) -> str:
    if not value:
        return ""
    stripped = re.sub(r"<[^>]+>", "", value)
    stripped = html.unescape(stripped)
    return re.sub(r"\s+", " ", stripped).strip().lower()[:120]


def _match_tdesktop_root_to_post(
    msg: _ParsedDiscussionMsg,
    epoch_posts: list[Post],
    normalized_post_texts: dict[int, str],
    *,
    expected_channel_id: int | None,
    expected_channel_title: str | None,
) -> Post | None:
    """Fallback matcher for tdesktop auto-forwarded channel posts lacking channel_post ID."""
    if expected_channel_id is not None:
        if msg.fwd_channel_id is None or msg.fwd_channel_id != expected_channel_id:
            return None
    elif expected_channel_title and msg.fwd_from_name:
        if msg.fwd_from_name.strip().lower() != expected_channel_title.strip().lower():
            return None

    msg_norm = _normalize_match_text(msg.body_text)
    msg_is_media_only = not msg_norm or (msg.media is not None and msg.body_text.startswith("["))

    best_post: Post | None = None
    best_score: tuple[int, float] | None = None
    near_12s: list[tuple[float, Post]] = []

    for post in epoch_posts:
        post_dt = post.date if post.date.tzinfo else post.date.replace(tzinfo=UTC)
        delta = abs((msg.created_at - post_dt).total_seconds())
        if delta <= 12:
            near_12s.append((delta, post))
        if delta > 180:
            continue
        post_norm = normalized_post_texts[post.id]
        text_match = 0
        if msg_norm and post_norm:
            if msg_norm == post_norm or msg_norm in post_norm or post_norm in msg_norm:
                text_match = 2
            elif len(msg_norm) >= 16 and msg_norm[:32] == post_norm[:32]:
                text_match = 1
        elif msg_is_media_only and not post_norm and delta <= 15:
            text_match = 1

        if text_match == 0:
            continue

        score = (text_match, -delta)
        if best_score is None or score > best_score:
            best_score = score
            best_post = post

    if best_post is None and len(near_12s) == 1:
        return near_12s[0][1]
    return best_post


def _sync_comment_media_files(
    rel_paths: set[str],
    *,
    src_root: Path,
    dest_root: Path,
    media_mode: str,
) -> int:
    if media_mode == "skip" or not rel_paths or not src_root.is_dir():
        return 0
    src_resolved = src_root.resolve()
    dest_resolved = dest_root.resolve()
    dest_resolved.mkdir(parents=True, exist_ok=True)
    copied = 0
    for rel in sorted(rel_paths):
        parts = PurePosixPath(rel).parts
        src_file = (src_resolved / Path(*parts)).resolve()
        if not src_file.is_relative_to(src_resolved) or not src_file.is_file():
            continue
        dst_file = (dest_resolved / Path(*parts)).resolve()
        if not dst_file.is_relative_to(dest_resolved):
            continue
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        if dst_file.exists():
            continue
        if media_mode == "hardlink":
            try:
                os.link(src_file, dst_file)
            except OSError:
                shutil.copy2(src_file, dst_file)
        else:
            shutil.copy2(src_file, dst_file)
        copied += 1
    return copied


async def import_epoch1_comments(
    session: AsyncSession,
    source_path: Path,
    *,
    channel_id: int = 1,
    discussion_chat_id: int | None = None,
    settings: Settings | None = None,
    media_mode: str = "copy",
) -> dict[str, int]:
    """Import Epoch 1 discussion group backup (Telethon JSON/JSONL or tdesktop result.json).

    Idempotent on re-run via UNIQUE(channel_id, tg_msg_id).
    """
    if not source_path.is_file():
        raise FileNotFoundError(f"Discussion backup not found: {source_path}")

    existing_channel = await session.get(Channel, channel_id)
    if existing_channel is not None:
        channel = await ensure_channel(
            session,
            channel_id=channel_id,
            tg_discussion_chat_id=discussion_chat_id,
        )
    else:
        channel = await ensure_channel(
            session,
            channel_id=channel_id,
            tg_discussion_chat_id=discussion_chat_id,
            status="archived",
            id_offset=0,
        )

    expected_channel_id = _normalize_channel_id(channel.tg_chat_id)
    expected_channel_title = channel.title
    media_prefix = f"comments/epoch{channel.id}"

    raw_msgs, users_map = _load_discussion_dump(source_path)
    parsed_msgs: list[_ParsedDiscussionMsg] = []
    for raw in raw_msgs:
        parsed = _parse_discussion_message(raw, users_map, media_prefix=media_prefix)
        if parsed is not None:
            parsed_msgs.append(parsed)
    parsed_msgs.sort(key=lambda m: m.tg_msg_id)
    msgs_by_id = {m.tg_msg_id: m for m in parsed_msgs}

    # Load Epoch 1 posts and build lookup indices
    posts_res = await session.execute(select(Post).where(Post.channel_id == channel.id))
    epoch_posts = list(posts_res.scalars().all())
    tg_msg_to_post: dict[int, Post] = {}
    disc_root_to_post: dict[int, Post] = {}
    normalized_post_texts: dict[int, str] = {}

    for post in epoch_posts:
        normalized_post_texts[post.id] = _normalize_match_text(post.text_html)
        if post.tg_msg_id is not None:
            tg_msg_to_post[int(post.tg_msg_id)] = post
        for sibling_id in post.tg_all_msg_ids or []:
            tg_msg_to_post[int(sibling_id)] = post
        if post.tg_discussion_root_id is not None:
            disc_root_to_post[int(post.tg_discussion_root_id)] = post

    # Step 1: Identify auto-forwarded channel post root messages in the discussion group
    root_msg_to_post: dict[int, Post] = dict(disc_root_to_post)
    roots_linked = 0
    candidate_roots: list[_ParsedDiscussionMsg] = []

    for msg in parsed_msgs:
        if msg.reply_to_msg_id is not None:
            continue
        if msg.is_forwarded_from_channel or msg.fwd_channel_id is not None:
            candidate_roots.append(msg)
        if msg.tg_msg_id in root_msg_to_post:
            continue

        matched_post: Post | None = None
        if msg.fwd_channel_post_id is not None:
            if expected_channel_id is not None:
                if msg.fwd_channel_id is None or msg.fwd_channel_id != expected_channel_id:
                    continue
            matched_post = tg_msg_to_post.get(msg.fwd_channel_post_id)
        elif msg.is_forwarded_from_channel:
            matched_post = _match_tdesktop_root_to_post(
                msg,
                epoch_posts,
                normalized_post_texts,
                expected_channel_id=expected_channel_id,
                expected_channel_title=expected_channel_title,
            )

        if matched_post is not None:
            root_msg_to_post[msg.tg_msg_id] = matched_post
            if matched_post.tg_discussion_root_id is None:
                matched_post.tg_discussion_root_id = msg.tg_msg_id
                roots_linked += 1

    # Propagate matched roots to captionless album sibling forwards from the same channel
    for idx, msg in enumerate(candidate_roots):
        if msg.tg_msg_id in root_msg_to_post or msg.body_text:
            continue
        if expected_channel_id is not None and msg.fwd_channel_id != expected_channel_id:
            continue
        for j in range(max(0, idx - 10), min(len(candidate_roots), idx + 11)):
            sibling = candidate_roots[j]
            if sibling.tg_msg_id not in root_msg_to_post:
                continue
            if expected_channel_id is not None and sibling.fwd_channel_id != expected_channel_id:
                continue
            if (
                abs(sibling.tg_msg_id - msg.tg_msg_id) <= 10
                and abs((sibling.created_at - msg.created_at).total_seconds()) <= 5
            ):
                root_msg_to_post[msg.tg_msg_id] = root_msg_to_post[sibling.tg_msg_id]
                break

    await session.flush()

    # Step 2: Resolve comment -> (Post, parent_tg_msg_id) via reply_to_top_id / reply chain
    def _resolve_post_for_msg(msg: _ParsedDiscussionMsg) -> Post | None:
        if msg.reply_to_top_id is not None and msg.reply_to_top_id in root_msg_to_post:
            return root_msg_to_post[msg.reply_to_top_id]
        visited: set[int] = {msg.tg_msg_id}
        cur_id = msg.reply_to_msg_id
        while cur_id is not None and cur_id not in visited:
            visited.add(cur_id)
            if cur_id in root_msg_to_post:
                return root_msg_to_post[cur_id]
            parent_msg = msgs_by_id.get(cur_id)
            if parent_msg is None:
                break
            if (
                parent_msg.reply_to_top_id is not None
                and parent_msg.reply_to_top_id in root_msg_to_post
            ):
                return root_msg_to_post[parent_msg.reply_to_top_id]
            cur_id = parent_msg.reply_to_msg_id
        return None

    # Preload existing comments for idempotent upsert
    existing_res = await session.execute(
        select(Comment).where(Comment.channel_id == channel.id, Comment.tg_msg_id.is_not(None))
    )
    existing_by_tg_id: dict[int, Comment] = {
        int(c.tg_msg_id): c for c in existing_res.scalars().all() if c.tg_msg_id is not None
    }
    tg_msg_to_comment_id: dict[int, int] = {k: v.id for k, v in existing_by_tg_id.items()}

    existing_rx_res = await session.execute(
        select(CommentTgReaction)
        .join(Comment, Comment.id == CommentTgReaction.comment_id)
        .where(Comment.channel_id == channel.id)
    )
    existing_rx_by_key: dict[tuple[int, str], CommentTgReaction] = {
        (rx.comment_id, rx.reaction_key): rx for rx in existing_rx_res.scalars().all()
    }

    comments_imported = 0
    comments_updated = 0
    skipped_unlinked = 0
    pending_parent_links: list[tuple[Comment, int]] = []
    imported_comments: list[tuple[Comment, _ParsedDiscussionMsg]] = []
    media_rels_to_sync: set[str] = set()
    unflushed = 0

    for msg in parsed_msgs:
        if msg.tg_msg_id in root_msg_to_post:
            continue
        post = _resolve_post_for_msg(msg)
        if post is None:
            skipped_unlinked += 1
            continue
        if not msg.body_text and msg.media is None:
            skipped_unlinked += 1
            continue

        media_rels_to_sync.update(msg.media_rel_paths)

        parent_tg_msg_id: int | None = None
        if (
            msg.reply_to_msg_id is not None
            and msg.reply_to_msg_id not in root_msg_to_post
            and msg.reply_to_msg_id != msg.tg_msg_id
        ):
            parent_tg_msg_id = msg.reply_to_msg_id

        parent_id = (
            tg_msg_to_comment_id.get(parent_tg_msg_id) if parent_tg_msg_id is not None else None
        )

        existing = existing_by_tg_id.get(msg.tg_msg_id)
        if existing is None:
            comment = Comment(
                post_id=post.id,
                parent_id=parent_id,
                source="telegram",
                channel_id=channel.id,
                tg_msg_id=msg.tg_msg_id,
                tg_author_id=msg.tg_author_id,
                tg_author_name=msg.tg_author_name or "Telegram User",
                tg_author_username=msg.tg_author_username,
                body_text=msg.body_text,
                body_html=msg.body_html,
                media=msg.media,
                created_at=msg.created_at,
                edited_at=msg.edited_at,
            )
            session.add(comment)
            existing_by_tg_id[msg.tg_msg_id] = comment
            imported_comments.append((comment, msg))
            comments_imported += 1
            if parent_tg_msg_id is not None and parent_id is None:
                pending_parent_links.append((comment, parent_tg_msg_id))
            unflushed += 1
            if unflushed >= 500:
                await session.flush()
                for pending_c, pending_msg in imported_comments:
                    if pending_c.id is not None:
                        tg_msg_to_comment_id[pending_msg.tg_msg_id] = pending_c.id
                unflushed = 0
        else:
            existing.post_id = post.id
            if parent_id is not None:
                existing.parent_id = parent_id
            elif parent_tg_msg_id is not None:
                pending_parent_links.append((existing, parent_tg_msg_id))
            existing.tg_author_id = msg.tg_author_id
            existing.tg_author_name = msg.tg_author_name or existing.tg_author_name
            existing.tg_author_username = msg.tg_author_username
            existing.body_text = msg.body_text
            existing.body_html = msg.body_html
            existing.media = msg.media
            existing.edited_at = msg.edited_at
            imported_comments.append((existing, msg))
            comments_updated += 1

    await session.flush()
    for c_obj, msg in imported_comments:
        if c_obj.id is not None:
            tg_msg_to_comment_id[msg.tg_msg_id] = c_obj.id

    for comment, parent_tg_id in pending_parent_links:
        resolved_parent_id = tg_msg_to_comment_id.get(parent_tg_id)
        if resolved_parent_id is not None and resolved_parent_id != comment.id:
            comment.parent_id = resolved_parent_id

    for comment, msg in imported_comments:
        if comment.id is None or not msg.tg_reactions:
            continue
        for rkey, emoji_val, cid_val, cnt in msg.tg_reactions:
            rx_obj = existing_rx_by_key.get((comment.id, rkey))
            if rx_obj is None:
                rx_obj = CommentTgReaction(
                    comment_id=comment.id,
                    reaction_key=rkey,
                    emoji=emoji_val,
                    custom_emoji_id=cid_val,
                    count=cnt,
                )
                session.add(rx_obj)
                existing_rx_by_key[(comment.id, rkey)] = rx_obj
            else:
                rx_obj.emoji = emoji_val
                rx_obj.custom_emoji_id = cid_val
                rx_obj.count = cnt

    media_copied = 0
    if settings is not None and media_rels_to_sync:
        media_copied = await asyncio.to_thread(
            _sync_comment_media_files,
            media_rels_to_sync,
            src_root=source_path.parent,
            dest_root=settings.media_dir / "comments" / f"epoch{channel.id}",
            media_mode=media_mode,
        )

    await session.commit()
    return {
        "roots_linked": roots_linked,
        "comments_imported": comments_imported,
        "comments_updated": comments_updated,
        "skipped_unlinked": skipped_unlinked,
        "media_copied": media_copied,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="corner-api-cli", description="corner /life v2 CLI tools")
    sub = parser.add_subparsers(dest="command", required=True)

    p_posts = sub.add_parser(
        "import-epoch1-posts",
        help="Import a TelegramBackup posts.json directory into Epoch 1",
    )
    p_posts.add_argument(
        "--source-dir", type=Path, required=True, help="Directory containing posts.json"
    )
    p_posts.add_argument("--channel-id", type=int, default=1)
    p_posts.add_argument("--tg-chat-id", type=int, default=None)
    p_posts.add_argument("--title", type=str, default="Epoch 1 (Archived)")
    p_posts.add_argument("--username", type=str, default=None)
    p_posts.add_argument(
        "--media-mode",
        choices=["copy", "hardlink", "skip"],
        default="copy",
        help="How to populate DATA_DIR/media/epochs/<channel_id>/",
    )
    p_posts.add_argument("--no-exports", action="store_true", help="Skip regenerating static exports")

    p_comments = sub.add_parser(
        "import-epoch1-comments",
        help="Import Epoch 1 discussion group backup (Telethon JSON/JSONL or tdesktop result.json)",
    )
    p_comments.add_argument("--source", type=Path, required=True, help="Backup JSON or JSONL file")
    p_comments.add_argument("--channel-id", type=int, default=1)
    p_comments.add_argument("--discussion-chat-id", type=int, default=None)
    p_comments.add_argument(
        "--media-mode",
        choices=["copy", "hardlink", "skip"],
        default="copy",
        help="How to populate DATA_DIR/media/comments/epoch<channel_id>/",
    )

    sub.add_parser("rebuild-exports", help="Atomically regenerate posts.json, pages/, and feeds")
    p_crawl = sub.add_parser(
        "crawl-channel",
        help="Backfill active channel posts using the bot account (TelegramBackup ID-range method)",
    )
    p_crawl.add_argument("--start-id", type=int, default=None)
    p_crawl.add_argument("--full", action="store_true")
    p_crawl.add_argument("--batch-size", type=int, default=200)
    sub.add_parser(
        "login-tg-user",
        help="Interactively log in to a read-only Telegram user account and print TG_USER_SESSION_STRING",
    )
    p_backfill = sub.add_parser(
        "backfill-avatars",
        help="Fetch and cache profile photos + usernames for historical Telegram comment authors",
    )
    p_backfill.add_argument("--channel-id", type=int, default=1)
    p_enrich = sub.add_parser(
        "enrich-webpages",
        help="Backfill missing title/site/thumbnail metadata on existing web_page files",
    )
    p_enrich.add_argument("--channel-id", type=int, default=None)
    return parser


async def backfill_comment_avatars(
    session: AsyncSession,
    settings: Settings,
    *,
    client: Any,
    channel_id: int | None = 1,
) -> dict[str, int]:
    """Download Telegram avatars + backfill usernames for imported historical comments."""
    from pyrogram.errors import FloodWait

    avatars_dir = settings.media_dir / "avatars"
    avatars_dir.mkdir(parents=True, exist_ok=True)

    # 1. Determine target user IDs missing avatar or username
    stmt = select(Comment.tg_author_id).where(Comment.tg_author_id.is_not(None)).distinct()
    if channel_id is not None:
        stmt = stmt.where(Comment.channel_id == channel_id)
    all_uids = [int(uid) for uid in (await session.execute(stmt)).scalars().all() if uid]

    # Link any already-cached disk avatars first
    disk_linked = 0
    missing_uids: list[int] = []
    for uid in all_uids:
        rel_path = f"avatars/{uid}.jpg"
        if (settings.media_dir / rel_path).is_file():
            upd = (
                update(Comment)
                .where(Comment.tg_author_id == uid, Comment.tg_author_avatar.is_(None))
                .values(tg_author_avatar=rel_path)
            )
            res = await session.execute(upd)
            disk_linked += int(res.rowcount or 0)
        else:
            missing_uids.append(uid)

    # Also resolve username for anyone missing tg_author_username
    uname_stmt = (
        select(Comment.tg_author_id)
        .where(Comment.tg_author_id.is_not(None), Comment.tg_author_username.is_(None))
        .distinct()
    )
    if channel_id is not None:
        uname_stmt = uname_stmt.where(Comment.channel_id == channel_id)
    need_info_uids = set(missing_uids) | {
        int(uid) for uid in (await session.execute(uname_stmt)).scalars().all() if uid
    }

    users_by_uid: dict[int, Any] = {}
    disc_chat_id = settings.TG_DISCUSSION_CHAT_ID
    if disc_chat_id is not None and hasattr(client, "get_chat_members"):
        try:
            async for member in client.get_chat_members(disc_chat_id):
                u = getattr(member, "user", None)
                uid = getattr(u, "id", None) if u is not None else None
                if uid is not None:
                    users_by_uid[int(uid)] = u
        except Exception:
            logger.debug("Could not enumerate discussion chat members", exc_info=True)

    # Resolve remaining user IDs by fetching one of their actual messages in the discussion chat
    unresolved = [uid for uid in sorted(need_info_uids) if uid not in users_by_uid]
    if unresolved and disc_chat_id is not None and hasattr(client, "get_messages"):
        sample_stmt = (
            select(Comment.tg_author_id, func.max(Comment.tg_msg_id))
            .where(
                Comment.tg_author_id.in_(unresolved),
                Comment.tg_msg_id.is_not(None),
            )
            .group_by(Comment.tg_author_id)
        )
        if channel_id is not None:
            sample_stmt = sample_stmt.where(Comment.channel_id == channel_id)
        sample_rows = (await session.execute(sample_stmt)).all()
        sample_msg_ids = [int(mid) for _, mid in sample_rows if mid is not None]
        for i in range(0, len(sample_msg_ids), 150):
            batch_mids = sample_msg_ids[i : i + 150]
            try:
                msgs = await client.get_messages(disc_chat_id, batch_mids)
                for m in msgs if isinstance(msgs, list) else [msgs]:
                    u = getattr(m, "from_user", None)
                    uid = getattr(u, "id", None) if u is not None else None
                    if uid is not None and int(uid) not in users_by_uid:
                        users_by_uid[int(uid)] = u
            except FloodWait as fw:
                await asyncio.sleep(float(getattr(fw, "value", 5)) + 1.0)
            except Exception:
                logger.debug("Could not batch-fetch sample messages for avatars", exc_info=True)

    # Fetch any remaining user IDs directly
    unresolved = [uid for uid in sorted(need_info_uids) if uid not in users_by_uid]
    for i in range(0, len(unresolved), 50):
        batch = unresolved[i : i + 50]
        try:
            res_users = await client.get_users(batch)
            for u in res_users if isinstance(res_users, list) else [res_users]:
                uid = getattr(u, "id", None)
                if uid is not None:
                    users_by_uid[int(uid)] = u
        except FloodWait as fw:
            await asyncio.sleep(float(getattr(fw, "value", 5)) + 1.0)
        except Exception:
            # Fallback individually so one deleted/unreachable peer doesn't drop the whole batch
            for uid in batch:
                try:
                    u = await client.get_users(uid)
                    if getattr(u, "id", None) is not None:
                        users_by_uid[int(u.id)] = u
                except FloodWait as fw:
                    await asyncio.sleep(float(getattr(fw, "value", 5)) + 1.0)
                except Exception:
                    continue

    avatars_downloaded = 0
    rows_avatar_updated = 0
    rows_username_updated = 0
    for uid, tg_user in users_by_uid.items():
        if getattr(tg_user, "is_deleted", False):
            continue
        username = getattr(tg_user, "username", None)
        if username:
            res_u = await session.execute(
                update(Comment)
                .where(Comment.tg_author_id == uid, Comment.tg_author_username.is_(None))
                .values(tg_author_username=str(username))
            )
            rows_username_updated += int(res_u.rowcount or 0)

        rel_path = f"avatars/{uid}.jpg"
        abs_path = settings.media_dir / rel_path
        if not abs_path.is_file():
            photo = getattr(tg_user, "photo", None)
            file_id = (
                getattr(photo, "small_file_id", None) or getattr(photo, "big_file_id", None)
                if photo is not None
                else None
            )
            if file_id and hasattr(client, "download_media"):
                tmp_path = avatars_dir / f".{uid}.{uuid.uuid4().hex[:8]}.tmp.jpg"
                try:
                    dl = await client.download_media(file_id, file_name=str(tmp_path))
                    if dl and Path(str(dl)).is_file():
                        os.replace(str(dl), abs_path)
                        avatars_downloaded += 1
                except FloodWait as fw:
                    await asyncio.sleep(float(getattr(fw, "value", 5)) + 1.0)
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    tmp_path.unlink(missing_ok=True)
        if abs_path.is_file():
            res_a = await session.execute(
                update(Comment)
                .where(Comment.tg_author_id == uid, Comment.tg_author_avatar.is_(None))
                .values(tg_author_avatar=rel_path)
            )
            rows_avatar_updated += int(res_a.rowcount or 0)

    # Also cache the discussion group chat avatar for anonymous admin messages (`讨论讨论w`)
    if disc_chat_id is not None and hasattr(client, "get_chat"):
        try:
            chat = await client.get_chat(disc_chat_id)
            chat_title = str(getattr(chat, "title", "") or "").strip()
            photo = getattr(chat, "photo", None)
            file_id = (
                getattr(photo, "small_file_id", None) or getattr(photo, "big_file_id", None)
                if photo is not None
                else None
            )
            if chat_title and file_id and hasattr(client, "download_media"):
                norm_chat = _normalize_channel_id(disc_chat_id) or abs(disc_chat_id)
                rel_chat = f"avatars/chat_{norm_chat}.jpg"
                abs_chat = settings.media_dir / rel_chat
                if not abs_chat.is_file():
                    tmp_chat = avatars_dir / f".chat_{norm_chat}.{uuid.uuid4().hex[:8]}.tmp.jpg"
                    try:
                        dl = await client.download_media(file_id, file_name=str(tmp_chat))
                        if dl and Path(str(dl)).is_file():
                            os.replace(str(dl), abs_chat)
                            avatars_downloaded += 1
                    except Exception:
                        tmp_chat.unlink(missing_ok=True)
                if abs_chat.is_file():
                    res_c = await session.execute(
                        update(Comment)
                        .where(
                            Comment.source == "telegram",
                            Comment.tg_author_id.is_(None),
                            Comment.tg_author_name == chat_title,
                            Comment.tg_author_avatar.is_(None),
                        )
                        .values(tg_author_avatar=rel_chat)
                    )
                    rows_avatar_updated += int(res_c.rowcount or 0)
        except Exception:
            logger.debug("Could not fetch discussion chat avatar", exc_info=True)

    await session.commit()
    return {
        "distinct_users": len(all_uids),
        "users_resolved": len(users_by_uid),
        "avatars_downloaded": avatars_downloaded,
        "disk_linked_rows": disk_linked,
        "rows_avatar_updated": rows_avatar_updated,
        "rows_username_updated": rows_username_updated,
    }


async def enrich_incomplete_web_pages(
    session: AsyncSession,
    settings: Settings,
    *,
    channel_id: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, int]:
    """Enrich existing `media_type == 'web_page'` files that lack title/thumb metadata."""
    from app.tg.client import _create_webp_thumb_sync, _is_safe_http_url, _read_image_size_sync

    stmt = select(Post).where(Post.is_deleted.is_(False), Post.files.is_not(None))
    if channel_id is not None:
        stmt = stmt.where(Post.channel_id == channel_id)
    posts = list((await session.execute(stmt.order_by(Post.id.asc()))).scalars().all())

    updated_posts = 0
    enriched_items = 0
    og_re = re.compile(
        r'<meta\b[^>]+?(?:property|name)=["\'](?P<key>og:title|og:site_name|og:description|og:image|twitter:title|twitter:image)["\'][^>]+?content=["\'](?P<val>[^"\']+)["\']',
        re.IGNORECASE,
    )

    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": "corner-life/1.0"},
        transport=transport,
    ) as client:
        for post in posts:
            if not isinstance(post.files, list):
                continue
            new_files: list[dict[str, Any]] = []
            post_changed = False
            media_dir = settings.media_dir / "epochs" / str(post.channel_id) / "media"
            url_prefix = f"epochs/{post.channel_id}"

            for item in post.files:
                if not isinstance(item, dict) or item.get("media_type") != "web_page":
                    new_files.append(item)
                    continue
                if item.get("title") and item.get("thumb"):
                    new_files.append(item)
                    continue

                safe_url = _is_safe_http_url(item.get("url"))
                if not safe_url:
                    new_files.append(item)
                    continue

                updated_item = dict(item)
                parsed = urlparse(safe_url)
                host = parsed.netloc.lower()
                thumb_src: str | None = None

                try:
                    if host in ("youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com"):
                        oembed_url = (
                            f"https://www.youtube.com/oembed?url={quote(safe_url, safe='')}&format=json"
                        )
                        resp = await client.get(oembed_url)
                        if resp.status_code == 200:
                            data = resp.json()
                            if not updated_item.get("title") and data.get("title"):
                                updated_item["title"] = str(data["title"]).strip()
                            if not updated_item.get("site_name"):
                                updated_item["site_name"] = str(
                                    data.get("provider_name") or "YouTube"
                                ).strip()
                            if not updated_item.get("author") and data.get("author_name"):
                                updated_item["author"] = str(data["author_name"]).strip()
                            if not updated_item.get("display_url"):
                                updated_item["display_url"] = (
                                    f"{host.removeprefix('www.')}{parsed.path}"
                                    + (f"?{parsed.query}" if parsed.query else "")
                                )
                            thumb_src = _is_safe_http_url(data.get("thumbnail_url"))
                    else:
                        resp = await client.get(safe_url)
                        if resp.status_code == 200 and "html" in resp.headers.get("content-type", "").lower():
                            found = {m.group("key").lower(): html.unescape(m.group("val")).strip() for m in og_re.finditer(resp.text[:200_000])}
                            if not updated_item.get("title"):
                                updated_item["title"] = found.get("og:title") or found.get("twitter:title")
                            if not updated_item.get("site_name") and found.get("og:site_name"):
                                updated_item["site_name"] = found["og:site_name"]
                            if not updated_item.get("description") and found.get("og:description"):
                                updated_item["description"] = found["og:description"]
                            thumb_src = _is_safe_http_url(found.get("og:image") or found.get("twitter:image"))

                    if thumb_src and not updated_item.get("thumb"):
                        img_resp = await client.get(thumb_src)
                        if img_resp.status_code == 200 and 0 < len(img_resp.content) <= 10 * 1024 * 1024:
                            media_dir.mkdir(parents=True, exist_ok=True)
                            url_hash = hashlib.sha1(safe_url.encode("utf-8")).hexdigest()[:10]
                            raw_dest = media_dir / f"{post.id}_wp_{url_hash}.jpg"
                            raw_dest.write_bytes(img_resp.content)
                            webp_p = await asyncio.to_thread(_create_webp_thumb_sync, raw_dest)
                            target_p = webp_p if (webp_p is not None and webp_p.is_file()) else raw_dest
                            w, h = await asyncio.to_thread(_read_image_size_sync, target_p)
                            updated_item["thumb"] = f"{url_prefix}/media/{target_p.name}"
                            if w and h:
                                updated_item["width"] = w
                                updated_item["height"] = h
                except Exception:
                    logger.warning("Failed enriching web_page on post #%s (%s)", post.id, safe_url, exc_info=True)

                if updated_item != item:
                    post_changed = True
                    enriched_items += 1
                new_files.append(updated_item)

            if post_changed:
                post.files = new_files
                updated_posts += 1

    await session.commit()
    await generate_exports(session, settings)
    return {"updated_posts": updated_posts, "enriched_items": enriched_items}


async def _login_tg_user(settings: Settings) -> None:
    if not settings.TG_API_ID or not settings.TG_API_HASH:
        raise SystemExit("TG_API_ID and TG_API_HASH must be set before running login-tg-user.")
    from pyrogram import Client

    client = Client(
        name=settings.TG_USER_SESSION_NAME or "user_reader",
        api_id=int(settings.TG_API_ID),
        api_hash=str(settings.TG_API_HASH),
        in_memory=True,
    )
    async with client:
        me = await client.get_me()
        if getattr(me, "is_bot", False):
            raise SystemExit("Logged in as a bot account; please log in with a regular user account.")
        session_str = await client.export_session_string()
        print(f"Logged in as user ID {me.id} (@{getattr(me, 'username', None)})")
        print(f"TG_USER_SESSION_STRING={session_str}")


async def _run_cli(args: argparse.Namespace) -> None:
    settings = get_settings()
    if args.command == "login-tg-user":
        await _login_tg_user(settings)
        return
    _, factory = init_db(settings)
    try:
        async with factory() as session:
            if args.command == "import-epoch1-posts":
                summary = await import_epoch1_posts(
                    session,
                    settings,
                    args.source_dir,
                    channel_id=args.channel_id,
                    tg_chat_id=args.tg_chat_id,
                    title=args.title,
                    username=args.username,
                    media_mode=args.media_mode,
                    regenerate_exports=not args.no_exports,
                )
                print(json.dumps(summary, indent=2))
            elif args.command == "import-epoch1-comments":
                summary = await import_epoch1_comments(
                    session,
                    args.source,
                    channel_id=args.channel_id,
                    discussion_chat_id=args.discussion_chat_id,
                    settings=settings,
                    media_mode=args.media_mode,
                )
                print(json.dumps(summary, indent=2))
            elif args.command == "rebuild-exports":
                meta = await generate_exports(session, settings)
                print(json.dumps(meta, indent=2))
            elif args.command == "enrich-webpages":
                summary = await enrich_incomplete_web_pages(
                    session, settings, channel_id=args.channel_id
                )
                print(json.dumps(summary, indent=2))
            elif args.command == "backfill-avatars":
                from pyrogram import Client

                if not settings.TG_API_ID or not settings.TG_API_HASH:
                    raise SystemExit("TG_API_ID and TG_API_HASH must be configured.")
                if settings.TG_USER_SESSION_STRING:
                    tg_client = Client(
                        name="avatar_backfill_user",
                        api_id=int(settings.TG_API_ID),
                        api_hash=str(settings.TG_API_HASH),
                        session_string=settings.TG_USER_SESSION_STRING,
                        in_memory=True,
                        no_updates=True,
                    )
                elif settings.TG_BOT_TOKEN:
                    tg_client = Client(
                        name="avatar_backfill_bot",
                        api_id=int(settings.TG_API_ID),
                        api_hash=str(settings.TG_API_HASH),
                        bot_token=settings.TG_BOT_TOKEN,
                        in_memory=True,
                        no_updates=True,
                    )
                else:
                    raise SystemExit("TG_USER_SESSION_STRING or TG_BOT_TOKEN must be configured.")
                async with tg_client:
                    summary = await backfill_comment_avatars(
                        session,
                        settings,
                        client=tg_client,
                        channel_id=args.channel_id,
                    )
                print(json.dumps(summary, indent=2))
            elif args.command == "crawl-channel":
                from pyrogram import Client

                from app.tg.client import TelegramService

                if not settings.TG_API_ID or not settings.TG_API_HASH or not settings.TG_BOT_TOKEN:
                    raise SystemExit("TG_API_ID, TG_API_HASH, and TG_BOT_TOKEN must be configured.")
                if settings.TG_CHANNEL_ID is None:
                    raise SystemExit("TG_CHANNEL_ID must be configured.")
                settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
                bot_client = Client(
                    name=settings.TG_SESSION_NAME,
                    api_id=int(settings.TG_API_ID),
                    api_hash=str(settings.TG_API_HASH),
                    bot_token=settings.TG_BOT_TOKEN,
                    workdir=str(settings.DATA_DIR),
                )
                svc = TelegramService(
                    session_factory=factory,
                    settings=settings,
                    client=bot_client,
                )
                async with bot_client:
                    svc.bridge.set_client(bot_client)
                    summary = await svc.crawl_channel_history(
                        start_id=args.start_id,
                        full=args.full,
                        batch_size=args.batch_size,
                        trigger_prompt=False,
                    )
                print(json.dumps(summary, indent=2))
    finally:
        await close_db()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = _build_parser()
    args = parser.parse_args(argv)
    asyncio.run(_run_cli(args))


if __name__ == "__main__":
    main()
