from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import nh3
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    Channel,
    Comment,
    Poll,
    PollOption,
    Post,
    PostImage,
    SiteSetting,
    TgReaction,
)

# PostgreSQL transaction-scoped advisory lock keys
_POST_ALLOC_LOCK_KEY = 728391001
_CHANNEL_SEQ_LOCK_KEY = 728391002

_DISALLOWED_URL_SCHEMES = ("data:", "javascript:", "vbscript:")

ALLOWED_POST_TAGS: set[str] = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "del",
    "details",
    "div",
    "em",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "span",
    "strong",
    "summary",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
ALLOWED_POST_ATTRS: dict[str, set[str]] = {
    "a": {"href", "title"},
    "blockquote": {"class", "expandable"},
    "code": {"class"},
    "div": {"class"},
    "figure": {"class"},
    "i": {"class", "emoji-src", "data-custom-emoji-id"},
    "img": {"src", "alt", "width", "height", "class", "data-src", "data-full"},
    "pre": {"language", "class"},
    "span": {"class"},
}


def sanitize_post_html(raw_html: str) -> str:
    """Sanitize Telegram / tg-blog rich post HTML with an explicit allowlist."""
    return nh3.clean(
        raw_html,
        tags=ALLOWED_POST_TAGS,
        attributes=ALLOWED_POST_ATTRS,
        url_schemes={"http", "https", "mailto"},
    )


def format_iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_reaction_key(emoji: str | None = None, custom_emoji_id: str | None = None) -> str:
    if custom_emoji_id:
        return f"c:{custom_emoji_id}"
    if emoji:
        return f"e:{emoji}"
    raise ValueError("Either emoji or custom_emoji_id must be provided")


ALLOWED_REACTIONS_SETTING_KEY = "allowed_reactions"

DEFAULT_ALLOWED_REACTION_EMOJIS: tuple[str, ...] = (
    "❤️", "👍", "🔥", "🥰", "👏", "😂", "😮", "😢", "🎉", "✨",
    "👎", "😁", "🤔", "🤯", "😱", "🤬", "🤩", "🤮", "💩", "🙏",
    "👌", "🕊", "🤡", "🥱", "🥴", "😍", "🐳", "❤️‍🔥", "🌚", "🌭",
    "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾",
    "💋", "😈", "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈",
    "😇", "😨", "🤝", "✍️", "🤗", "🫡", "🎅", "🎄", "☃️", "💅",
    "🤪", "🗿", "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎",
    "👾", "🤷",
)


def canonical_reaction_key(
    *, emoji: str | None = None, custom_emoji_id: str | None = None
) -> str:
    if custom_emoji_id:
        return f"c:{custom_emoji_id.strip()}"
    if emoji:
        return f"e:{strip_variation_selectors(emoji.strip())}"
    raise ValueError("Either emoji or custom_emoji_id must be provided")


def normalize_allowed_reactions(raw_items: Sequence[dict[str, Any]]) -> list[dict[str, str | None]]:
    """Deduplicate and normalize an ordered list of emoji/custom_emoji_id choices."""
    seen: set[str] = set()
    out: list[dict[str, str | None]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        cid_raw = raw.get("custom_emoji_id")
        cid = str(cid_raw).strip() if cid_raw is not None else ""
        emoji_raw = raw.get("emoji")
        emoji = str(emoji_raw).strip() if emoji_raw is not None else ""
        if cid:
            if not cid.isascii() or not cid.isdigit() or len(cid) > 32:
                continue
            key = canonical_reaction_key(custom_emoji_id=cid)
            if key in seen:
                continue
            seen.add(key)
            out.append({"reaction_key": key, "emoji": None, "custom_emoji_id": cid})
        elif emoji:
            key = canonical_reaction_key(emoji=emoji)
            if key in seen:
                continue
            seen.add(key)
            out.append({"reaction_key": key, "emoji": emoji, "custom_emoji_id": None})
    return out


def default_allowed_reactions() -> list[dict[str, str | None]]:
    return normalize_allowed_reactions([{"emoji": e} for e in DEFAULT_ALLOWED_REACTION_EMOJIS])


async def get_allowed_reactions(session: AsyncSession) -> list[dict[str, str | None]]:
    row = await session.get(SiteSetting, ALLOWED_REACTIONS_SETTING_KEY)
    if row is None or not isinstance(row.value, list):
        return default_allowed_reactions()
    return normalize_allowed_reactions(row.value)


async def set_allowed_reactions(
    session: AsyncSession, items: Sequence[dict[str, Any]]
) -> list[dict[str, str | None]]:
    normalized = normalize_allowed_reactions(items)
    now = datetime.now(UTC)
    existing = await session.get(SiteSetting, ALLOWED_REACTIONS_SETTING_KEY)
    if existing is None:
        session.add(
            SiteSetting(
                key=ALLOWED_REACTIONS_SETTING_KEY,
                value=normalized,
                updated_at=now,
            )
        )
    else:
        existing.value = normalized
        existing.updated_at = now
    await session.flush()
    return normalized


def find_allowed_reaction_entry(
    allowed_reactions: Sequence[dict[str, str | None]],
    *,
    emoji: str | None = None,
    custom_emoji_id: str | None = None,
) -> dict[str, str | None] | None:
    target_key = canonical_reaction_key(emoji=emoji, custom_emoji_id=custom_emoji_id)
    for item in allowed_reactions:
        item_key = item.get("reaction_key")
        if item_key == target_key:
            return item
        item_emoji = item.get("emoji")
        item_cid = item.get("custom_emoji_id")
        if canonical_reaction_key(emoji=item_emoji, custom_emoji_id=item_cid) == target_key:
            return item
    return None


def resolve_media_url(raw_url: str | None, api_prefix: str = "/api/life") -> str | None:
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
    prefix = api_prefix.rstrip("/")
    return f"{prefix}/media/{cleaned}"


def resolve_absolute_media_url(
    raw_url: str | None, base_url: str, api_prefix: str = "/api/life"
) -> str | None:
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
    base = base_url.rstrip("/")
    if stripped.startswith("/"):
        return f"{base}{stripped}"
    cleaned = stripped.removeprefix("./").lstrip("/")
    if not cleaned:
        return None
    prefix = api_prefix.rstrip("/")
    return f"{base}{prefix}/media/{cleaned}"


def _post_load_options():
    return (
        selectinload(Post.images),
        selectinload(Post.tg_reactions),
        selectinload(Post.web_reactions),
        selectinload(Post.poll).selectinload(Poll.options),
        selectinload(Post.poll).selectinload(Poll.web_votes),
    )


async def sync_channel_id_sequence(session: AsyncSession) -> None:
    """Keep channels.id PostgreSQL sequence aligned without ever rewinding backwards."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CHANNEL_SEQ_LOCK_KEY}
    )
    await session.execute(
        text(
            "SELECT setval("
            "pg_get_serial_sequence('channels', 'id'), "
            "GREATEST("
            "COALESCE((SELECT MAX(id) FROM channels), 1), "
            "COALESCE(("
            "SELECT last_value FROM pg_sequences "
            "WHERE schemaname = current_schema() AND sequencename = 'channels_id_seq'"
            "), 1)"
            "), "
            "(SELECT COUNT(*) > 0 FROM channels)"
            ")"
        )
    )


async def get_max_post_id(session: AsyncSession) -> int:
    res = await session.execute(select(func.coalesce(func.max(Post.id), 0)))
    return int(res.scalar_one())


async def get_channel(session: AsyncSession, channel_id: int) -> Channel | None:
    return await session.get(Channel, channel_id)


async def get_active_channel(session: AsyncSession) -> Channel | None:
    stmt = select(Channel).where(Channel.status == "active").order_by(Channel.id.desc()).limit(1)
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def ensure_channel(
    session: AsyncSession,
    *,
    channel_id: int | None = None,
    tg_chat_id: int | None = None,
    tg_discussion_chat_id: int | None = None,
    title: str | None = None,
    username: str | None = None,
    status: str | None = None,
    id_offset: int | None = None,
) -> Channel:
    """Create or update a channel epoch.

    When updating an existing channel, `status` and `id_offset` are preserved unless
    explicitly supplied (and `id_offset` is only updated if no posts exist for the channel).
    When creating a new epoch, `status` defaults to `'active'` and `id_offset` defaults
    to `MAX(posts.id)` so new epochs never collide with prior ones.
    """
    existing: Channel | None = None
    if channel_id is not None:
        existing = await session.get(Channel, channel_id)
    elif tg_chat_id is not None:
        res = await session.execute(select(Channel).where(Channel.tg_chat_id == tg_chat_id))
        existing = res.scalar_one_or_none()

    if existing is not None:
        if tg_chat_id is not None:
            existing.tg_chat_id = tg_chat_id
        if tg_discussion_chat_id is not None:
            existing.tg_discussion_chat_id = tg_discussion_chat_id
        if title is not None:
            existing.title = title
        if username is not None:
            existing.username = username
        if status is not None:
            existing.status = status
        if id_offset is not None and existing.id_offset != id_offset:
            post_count_res = await session.execute(
                select(func.count(Post.id)).where(Post.channel_id == existing.id)
            )
            if int(post_count_res.scalar_one()) == 0:
                existing.id_offset = id_offset
        await session.flush()
        return existing

    computed_offset = id_offset if id_offset is not None else await get_max_post_id(session)
    channel_kwargs: dict[str, Any] = {
        "tg_chat_id": tg_chat_id,
        "tg_discussion_chat_id": tg_discussion_chat_id,
        "title": title,
        "username": username,
        "status": status if status is not None else "active",
        "id_offset": computed_offset,
        "last_crawled_msg_id": 0,
    }
    if channel_id is not None:
        channel_kwargs["id"] = channel_id

    channel = Channel(**channel_kwargs)
    session.add(channel)
    await session.flush()
    if channel_id is not None:
        await sync_channel_id_sequence(session)
    return channel


async def find_post_by_channel_tg_msg(
    session: AsyncSession, channel_id: int, tg_msg_id: int
) -> Post | None:
    stmt = (
        select(Post)
        .options(*_post_load_options())
        .where(
            Post.channel_id == channel_id,
            or_(
                Post.tg_msg_id == tg_msg_id,
                Post.tg_all_msg_ids.any(tg_msg_id),
            ),
        )
        .limit(1)
    )
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def allocate_canonical_post_id(
    session: AsyncSession, channel: Channel, tg_msg_id: int | None
) -> int:
    """Allocate a canonical monotonic post ID under a transaction-scoped advisory lock."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _POST_ALLOC_LOCK_KEY}
    )
    max_id = await get_max_post_id(session)
    if tg_msg_id is not None:
        candidate = channel.id_offset + int(tg_msg_id)
        if candidate > 0:
            existing = await session.get(Post, candidate)
            if existing is None:
                return candidate
    return max_id + 1


async def upsert_post(
    session: AsyncSession,
    *,
    channel: Channel,
    tg_msg_id: int | None,
    date: datetime,
    canonical_id: int | None = None,
    tg_media_group_id: str | None = None,
    tg_all_msg_ids: list[int] | None = None,
    tg_discussion_root_id: int | None = None,
    edited_at: datetime | None = None,
    is_deleted: bool = False,
    post_type: str | None = None,
    text_html: str | None = None,
    author: str | None = None,
    views: int | None = None,
    forwards: int | None = None,
    reply_to_post_id: int | None = None,
    forwarded_from: Any | None = None,
    reply_snapshot: dict[str, Any] | None = None,
    quote: dict[str, Any] | None = None,
    files: list[dict[str, Any]] | None = None,
    extra_meta: dict[str, Any] | None = None,
    show_caption_above_media: bool = False,
    has_media_spoiler: bool = False,
    images: list[dict[str, Any]] | None = None,
    tg_reactions: list[dict[str, Any]] | None = None,
    poll: dict[str, Any] | None = None,
) -> Post:
    """Idempotently create or update a post along with its images, TG reactions, and poll.

    Serializes canonical ID allocation via a transaction-scoped PostgreSQL advisory lock
    and updates `PostImage` rows in place so Workflow B original photo upgrades and
    fingerprints are never wiped out by subsequent syncs or edits.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _POST_ALLOC_LOCK_KEY}
    )

    post: Post | None = None
    if canonical_id is not None:
        post = await session.get(Post, canonical_id)
    if post is None and tg_msg_id is not None:
        post = await find_post_by_channel_tg_msg(session, channel.id, tg_msg_id)

    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    if edited_at is not None and edited_at.tzinfo is None:
        edited_at = edited_at.replace(tzinfo=UTC)

    cleaned_html = sanitize_post_html(text_html) if text_html else text_html

    all_ids: list[int] | None = None
    if tg_all_msg_ids:
        all_ids = sorted({int(x) for x in tg_all_msg_ids})
    elif tg_msg_id is not None:
        all_ids = [int(tg_msg_id)]

    if post is None:
        post_id = (
            canonical_id
            if canonical_id is not None
            else await allocate_canonical_post_id(session, channel, tg_msg_id)
        )
        post = Post(
            id=post_id,
            channel_id=channel.id,
            tg_msg_id=tg_msg_id,
            tg_media_group_id=tg_media_group_id,
            tg_all_msg_ids=all_ids,
            tg_discussion_root_id=tg_discussion_root_id,
            date=date,
            edited_at=edited_at,
            is_deleted=is_deleted,
            type=post_type,
            text_html=cleaned_html,
            author=author,
            views=views,
            forwards=forwards,
            reply_to_post_id=reply_to_post_id,
            forwarded_from=forwarded_from,
            reply_snapshot=reply_snapshot,
            quote=quote,
            files=files,
            extra_meta=extra_meta,
            show_caption_above_media=show_caption_above_media,
            has_media_spoiler=has_media_spoiler,
        )
        session.add(post)
        await session.flush()
    else:
        post.channel_id = channel.id
        if tg_msg_id is not None:
            post.tg_msg_id = tg_msg_id
        post.tg_media_group_id = tg_media_group_id
        if all_ids is not None:
            post.tg_all_msg_ids = all_ids
        if tg_discussion_root_id is not None:
            post.tg_discussion_root_id = tg_discussion_root_id
        post.date = date
        post.edited_at = edited_at
        post.is_deleted = is_deleted
        post.type = post_type
        post.text_html = cleaned_html
        post.author = author
        post.views = views
        post.forwards = forwards
        post.reply_to_post_id = reply_to_post_id
        post.forwarded_from = forwarded_from
        post.reply_snapshot = reply_snapshot
        post.quote = quote
        post.files = files
        post.extra_meta = extra_meta
        post.show_caption_above_media = show_caption_above_media
        post.has_media_spoiler = has_media_spoiler
        await session.flush()

    if images is not None:
        existing_rows = list(
            (
                await session.execute(
                    select(PostImage)
                    .where(PostImage.post_id == post.id)
                    .order_by(PostImage.sort_order.asc(), PostImage.id.asc())
                )
            )
            .scalars()
            .all()
        )
        by_tg_msg_id: dict[int, PostImage] = {}
        by_sort_order: dict[int, PostImage] = {}
        for row in existing_rows:
            if row.tg_msg_id is not None and row.tg_msg_id not in by_tg_msg_id:
                by_tg_msg_id[int(row.tg_msg_id)] = row
            if row.sort_order not in by_sort_order:
                by_sort_order[row.sort_order] = row

        matched_ids: set[int] = set()
        for idx, img in enumerate(images):
            sort_order = int(img.get("sort_order", idx))
            inc_tg_msg_id = int(img["tg_msg_id"]) if img.get("tg_msg_id") is not None else None
            inc_orig_url = img.get("original_url")
            inc_orig_size = img.get("original_size")
            inc_orig_name = img.get("original_name")
            inc_thumb = img.get("thumb_url") or img.get("thumb")
            inc_width = int(img.get("width") or 0)
            inc_height = int(img.get("height") or 0)

            existing_img: PostImage | None = None
            if inc_tg_msg_id is not None:
                cand = by_tg_msg_id.get(inc_tg_msg_id)
                if cand is not None and cand.id not in matched_ids:
                    existing_img = cand
            if existing_img is None:
                cand = by_sort_order.get(sort_order)
                if cand is not None and cand.id not in matched_ids:
                    existing_img = cand

            if existing_img is not None:
                matched_ids.add(existing_img.id)
                existing_img.sort_order = sort_order
                if inc_tg_msg_id is not None:
                    existing_img.tg_msg_id = inc_tg_msg_id

                # Preserve Workflow B upgraded display URL/dimensions & original metadata
                # when incoming sync payload does not explicitly supply a new original_url.
                has_existing_original = bool(existing_img.original_url)
                if inc_orig_url is not None:
                    existing_img.original_url = inc_orig_url
                    existing_img.url = str(img["url"])
                    if inc_thumb is not None:
                        existing_img.thumb_url = inc_thumb
                    if inc_width > 0:
                        existing_img.width = inc_width
                    if inc_height > 0:
                        existing_img.height = inc_height
                elif not has_existing_original:
                    existing_img.url = str(img["url"])
                    if inc_thumb is not None:
                        existing_img.thumb_url = inc_thumb
                    if inc_width > 0:
                        existing_img.width = inc_width
                    if inc_height > 0:
                        existing_img.height = inc_height
                else:
                    if not existing_img.thumb_url and inc_thumb is not None:
                        existing_img.thumb_url = inc_thumb

                if inc_orig_size is not None:
                    existing_img.original_size = inc_orig_size
                if inc_orig_name is not None:
                    existing_img.original_name = inc_orig_name
                if img.get("size") is not None:
                    existing_img.size = img["size"]
                if "spoiler" in img:
                    existing_img.spoiler = bool(img["spoiler"])
                if img.get("phash") is not None:
                    existing_img.phash = img["phash"]
                if img.get("rgb_sig") is not None:
                    existing_img.rgb_sig = img["rgb_sig"]
            else:
                session.add(
                    PostImage(
                        post_id=post.id,
                        sort_order=sort_order,
                        tg_msg_id=inc_tg_msg_id,
                        url=str(img["url"]),
                        thumb_url=inc_thumb,
                        original_url=inc_orig_url,
                        original_size=inc_orig_size,
                        original_name=inc_orig_name,
                        size=img.get("size"),
                        width=inc_width,
                        height=inc_height,
                        spoiler=bool(img.get("spoiler", False)),
                        phash=img.get("phash"),
                        rgb_sig=img.get("rgb_sig"),
                    )
                )

        stale_ids = [row.id for row in existing_rows if row.id not in matched_ids]
        if stale_ids:
            await session.execute(delete(PostImage).where(PostImage.id.in_(stale_ids)))
        await session.flush()

    if tg_reactions is not None:
        await session.execute(delete(TgReaction).where(TgReaction.post_id == post.id))
        seen_keys: set[str] = set()
        for r in tg_reactions:
            emoji = r.get("emoji")
            custom_emoji_id = (
                str(r["custom_emoji_id"]) if r.get("custom_emoji_id") is not None else None
            )
            if not emoji and not custom_emoji_id:
                continue
            rkey = make_reaction_key(emoji=emoji, custom_emoji_id=custom_emoji_id)
            if rkey in seen_keys:
                continue
            seen_keys.add(rkey)
            session.add(
                TgReaction(
                    post_id=post.id,
                    reaction_key=rkey,
                    emoji=emoji,
                    custom_emoji_id=custom_emoji_id,
                    count=int(r.get("count") or 0),
                )
            )
        await session.flush()

    if poll is not None:
        await upsert_poll(session, post_id=post.id, poll_data=poll)

    if tg_msg_id is not None and tg_msg_id > channel.last_crawled_msg_id:
        channel.last_crawled_msg_id = int(tg_msg_id)
        await session.flush()

    return post


async def upsert_poll(
    session: AsyncSession, *, post_id: int, poll_data: dict[str, Any]
) -> Poll:
    poll_id = str(poll_data.get("id") or f"poll_{post_id}")
    existing_by_post = (
        await session.execute(select(Poll).where(Poll.post_id == post_id))
    ).scalar_one_or_none()
    if existing_by_post is not None and existing_by_post.id != poll_id:
        await session.delete(existing_by_post)
        await session.flush()

    poll_obj = await session.get(Poll, poll_id)
    if poll_obj is None:
        poll_obj = Poll(
            id=poll_id,
            post_id=post_id,
            question=str(poll_data.get("question") or ""),
            is_closed=bool(poll_data.get("is_closed", False)),
            is_anonymous=bool(poll_data.get("is_anonymous", True)),
            type=str(poll_data.get("type") or "REGULAR"),
            allow_multiple_answers=bool(poll_data.get("allow_multiple_answers", False)),
            correct_option_idx=poll_data.get("correct_option_idx"),
            explanation=poll_data.get("explanation"),
        )
        session.add(poll_obj)
    else:
        poll_obj.post_id = post_id
        poll_obj.question = str(poll_data.get("question") or "")
        poll_obj.is_closed = bool(poll_data.get("is_closed", False))
        poll_obj.is_anonymous = bool(poll_data.get("is_anonymous", True))
        poll_obj.type = str(poll_data.get("type") or "REGULAR")
        poll_obj.allow_multiple_answers = bool(poll_data.get("allow_multiple_answers", False))
        poll_obj.correct_option_idx = poll_data.get("correct_option_idx")
        poll_obj.explanation = poll_data.get("explanation")
    await session.flush()

    await session.execute(delete(PollOption).where(PollOption.poll_id == poll_obj.id))
    for idx, opt in enumerate(poll_data.get("options") or []):
        session.add(
            PollOption(
                poll_id=poll_obj.id,
                option_idx=int(opt.get("option_idx", idx)),
                text=str(opt.get("text") or ""),
                data=str(opt.get("data") if opt.get("data") is not None else idx),
                tg_voter_count=int(opt.get("tg_voter_count", opt.get("voter_count", 0)) or 0),
            )
        )
    await session.flush()
    return poll_obj


async def get_post_by_id(
    session: AsyncSession, post_id: int, *, include_deleted: bool = False
) -> Post | None:
    stmt = select(Post).options(*_post_load_options()).where(Post.id == post_id)
    if not include_deleted:
        stmt = stmt.where(Post.is_deleted.is_(False))
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def list_posts(
    session: AsyncSession,
    *,
    after_id: int | None = None,
    limit: int | None = None,
    descending: bool = False,
    include_deleted: bool = False,
) -> list[Post]:
    stmt = select(Post).options(*_post_load_options())
    if not include_deleted:
        stmt = stmt.where(Post.is_deleted.is_(False))
    if after_id is not None:
        stmt = stmt.where(Post.id > after_id)
    stmt = stmt.order_by(Post.id.desc() if descending else Post.id.asc())
    if limit is not None:
        stmt = stmt.limit(limit)
    res = await session.execute(stmt)
    return list(res.scalars().all())


def strip_variation_selectors(emoji: str) -> str:
    return emoji.replace("\ufe0f", "").replace("\ufe0e", "")


def _canonical_reaction_group_key(
    *,
    reaction_key: str,
    emoji: str | None,
    custom_emoji_id: str | None,
) -> str:
    if emoji:
        return make_reaction_key(emoji=strip_variation_selectors(emoji))
    if custom_emoji_id:
        return make_reaction_key(custom_emoji_id=custom_emoji_id)
    return reaction_key


def _prefer_display_emoji(current: str | None, candidate: str | None) -> str | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    if "\ufe0f" in candidate and "\ufe0f" not in current:
        return candidate
    return current


def _compute_merged_reactions(
    tg_reactions: Sequence[Any],
    web_reactions: Sequence[Any],
    *,
    actor_key: str | None = None,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    web_actors_by_group: dict[str, set[str]] = defaultdict(set)

    for tr in tg_reactions:
        gkey = _canonical_reaction_group_key(
            reaction_key=tr.reaction_key,
            emoji=tr.emoji,
            custom_emoji_id=tr.custom_emoji_id,
        )
        entry = merged.get(gkey)
        if entry is None:
            merged[gkey] = {
                "reaction_key": tr.reaction_key,
                "emoji": tr.emoji,
                "custom_emoji_id": tr.custom_emoji_id,
                "tg_count": int(tr.count),
                "web_count": 0,
                "count": int(tr.count),
                "reacted": False,
            }
        else:
            entry["tg_count"] += int(tr.count)
            entry["emoji"] = _prefer_display_emoji(entry["emoji"], tr.emoji)
            entry["reaction_key"] = make_reaction_key(
                emoji=entry["emoji"], custom_emoji_id=entry["custom_emoji_id"]
            )
            entry["count"] = entry["tg_count"] + entry["web_count"]

    for wr in web_reactions:
        gkey = _canonical_reaction_group_key(
            reaction_key=wr.reaction_key,
            emoji=wr.emoji,
            custom_emoji_id=wr.custom_emoji_id,
        )
        entry = merged.get(gkey)
        if entry is None:
            entry = {
                "reaction_key": wr.reaction_key,
                "emoji": wr.emoji,
                "custom_emoji_id": wr.custom_emoji_id,
                "tg_count": 0,
                "web_count": 0,
                "count": 0,
                "reacted": False,
            }
            merged[gkey] = entry
        else:
            preferred = _prefer_display_emoji(entry["emoji"], wr.emoji)
            if preferred != entry["emoji"]:
                entry["emoji"] = preferred
                entry["reaction_key"] = make_reaction_key(
                    emoji=entry["emoji"], custom_emoji_id=entry["custom_emoji_id"]
                )
        web_actors_by_group[gkey].add(wr.actor_key)
        entry["web_count"] = len(web_actors_by_group[gkey])
        entry["count"] = entry["tg_count"] + entry["web_count"]
        if actor_key is not None and wr.actor_key == actor_key:
            entry["reacted"] = True

    result = [item for item in merged.values() if item["count"] > 0]
    result.sort(key=lambda item: (-item["count"], item["reaction_key"]))
    return result


def compute_merged_reactions(
    post: Post, *, actor_key: str | None = None
) -> list[dict[str, Any]]:
    return _compute_merged_reactions(
        post.tg_reactions,
        post.web_reactions,
        actor_key=actor_key,
    )


def compute_merged_comment_reactions(
    comment: Comment, *, actor_key: str | None = None
) -> list[dict[str, Any]]:
    return _compute_merged_reactions(
        comment.tg_reactions,
        comment.web_reactions,
        actor_key=actor_key,
    )


def compute_merged_poll(
    poll: Poll, *, actor_key: str | None = None
) -> dict[str, Any]:
    web_counts: dict[int, int] = defaultdict(int)
    my_votes: list[int] = []
    web_voters: set[str] = set()

    for vote in poll.web_votes:
        web_counts[vote.option_idx] += 1
        web_voters.add(vote.actor_key)
        if actor_key is not None and vote.actor_key == actor_key:
            my_votes.append(vote.option_idx)
    my_votes.sort()

    options_out: list[dict[str, Any]] = []
    tg_sum = 0
    for opt in sorted(poll.options, key=lambda o: o.option_idx):
        wc = web_counts.get(opt.option_idx, 0)
        tc = int(opt.tg_voter_count)
        tg_sum += tc
        options_out.append(
            {
                "idx": opt.option_idx,
                "text": opt.text,
                "data": opt.data,
                "tg_voter_count": tc,
                "web_voter_count": wc,
                "voter_count": tc + wc,
            }
        )

    total_voter_count = tg_sum + len(web_voters)
    reveal_answer = poll.is_closed or bool(my_votes) or poll.type != "QUIZ"

    return {
        "id": poll.id,
        "question": poll.question,
        "is_closed": poll.is_closed,
        "is_anonymous": poll.is_anonymous,
        "type": poll.type,
        "allow_multiple_answers": poll.allow_multiple_answers,
        "total_voter_count": total_voter_count,
        "my_votes": my_votes,
        "correct_option_idx": poll.correct_option_idx if reveal_answer else None,
        "explanation": poll.explanation if reveal_answer else None,
        "options": options_out,
    }


def compute_merged_views(post: Post) -> str | None:
    web_views = int(getattr(post, "web_views", 0) or 0)
    if post.views is None and web_views <= 0:
        return None
    return str((post.views or 0) + web_views)


def serialize_comment_media(
    raw_media: Any, api_prefix: str = "/api/life"
) -> dict[str, Any] | None:
    if not raw_media or not isinstance(raw_media, dict):
        return None
    out = dict(raw_media)
    if out.get("url"):
        resolved_url = resolve_media_url(str(out["url"]), api_prefix)
        if resolved_url:
            out["url"] = resolved_url
        else:
            out.pop("url", None)
    if out.get("thumb"):
        resolved_thumb = resolve_media_url(str(out["thumb"]), api_prefix)
        if resolved_thumb:
            out["thumb"] = resolved_thumb
        else:
            out.pop("thumb", None)
    return out


def serialize_post(post: Post, *, api_prefix: str = "/api/life") -> dict[str, Any]:
    """Serialize a Post ORM instance into canonical `tg-blog` models.ts JSON dict."""
    out: dict[str, Any] = {
        "id": post.id,
        "date": format_iso_utc(post.date),
    }

    if post.text_html:
        out["text"] = post.text_html
    if post.forwarded_from is not None:
        out["forwarded_from"] = post.forwarded_from
    if post.type:
        out["type"] = post.type
    merged_views = compute_merged_views(post)
    if merged_views is not None:
        out["views"] = merged_views
    if post.author:
        out["author"] = post.author

    if post.reply_snapshot:
        reply_obj = dict(post.reply_snapshot)
        if "id" not in reply_obj and post.reply_to_post_id is not None:
            reply_obj["id"] = post.reply_to_post_id
        if reply_obj.get("thumb"):
            resolved_thumb = resolve_media_url(str(reply_obj["thumb"]), api_prefix)
            if resolved_thumb:
                reply_obj["thumb"] = resolved_thumb
            else:
                reply_obj.pop("thumb", None)
        out["reply"] = reply_obj
    elif post.reply_to_post_id is not None:
        out["reply"] = {"id": post.reply_to_post_id}

    if post.images:
        serialized_images: list[dict[str, Any]] = []
        for img in sorted(post.images, key=lambda i: i.sort_order):
            resolved_url = resolve_media_url(img.url, api_prefix)
            if not resolved_url:
                continue
            item: dict[str, Any] = {
                "url": resolved_url,
                "width": img.width,
                "height": img.height,
            }
            if img.thumb_url:
                resolved_thumb = resolve_media_url(img.thumb_url, api_prefix)
                if resolved_thumb:
                    item["thumb"] = resolved_thumb
            if img.spoiler:
                item["spoiler"] = True
            if img.size is not None:
                item["size"] = img.size
            if img.original_url:
                resolved_orig = resolve_media_url(img.original_url, api_prefix)
                if resolved_orig:
                    item["original_url"] = resolved_orig
            if img.original_size is not None:
                item["original_size"] = img.original_size
            if img.original_name:
                item["original_name"] = img.original_name
            serialized_images.append(item)
        if serialized_images:
            out["images"] = serialized_images

    serialized_files: list[dict[str, Any]] = []
    if post.files:
        for f in post.files:
            if not isinstance(f, dict):
                continue
            f_copy = dict(f)
            if f_copy.get("url"):
                resolved_url = resolve_media_url(str(f_copy["url"]), api_prefix)
                if resolved_url:
                    f_copy["url"] = resolved_url
                else:
                    f_copy.pop("url", None)
            if f_copy.get("thumb"):
                resolved_thumb = resolve_media_url(str(f_copy["thumb"]), api_prefix)
                if resolved_thumb:
                    f_copy["thumb"] = resolved_thumb
                else:
                    f_copy.pop("thumb", None)
            serialized_files.append(f_copy)

    if post.poll is not None:
        merged_poll = compute_merged_poll(post.poll)
        poll_file: dict[str, Any] = {
            "media_type": "poll",
            "id": merged_poll["id"],
            "question": merged_poll["question"],
            "options": [
                {
                    "text": opt["text"],
                    "voter_count": opt["voter_count"],
                    "data": opt["data"],
                }
                for opt in merged_poll["options"]
            ],
            "total_voter_count": merged_poll["total_voter_count"],
            "is_closed": 1 if merged_poll["is_closed"] else 0,
            "is_anonymous": 1 if merged_poll["is_anonymous"] else 0,
            "type": merged_poll["type"],
            "allow_multiple_answers": merged_poll["allow_multiple_answers"],
        }
        serialized_files.append(poll_file)

    if serialized_files:
        out["files"] = serialized_files

    if post.quote:
        out["quote"] = post.quote

    merged_reactions = compute_merged_reactions(post)
    if merged_reactions:
        out["reactions"] = [
            {
                k: v
                for k, v in {
                    "emoji": r["emoji"],
                    "custom_emoji_id": r["custom_emoji_id"],
                    "count": r["count"],
                }.items()
                if v is not None
            }
            for r in merged_reactions
        ]

    if post.show_caption_above_media:
        out["show_caption_above_media"] = True
    if post.has_media_spoiler:
        out["has_media_spoiler"] = True

    return out
