from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.captcha import verify_altcha_payload
from app.auth.security import (
    VISITOR_COOKIE_NAME,
    SlidingWindowRateLimiter,
    ensure_visitor_id,
    format_anon_display_name,
    get_client_ip,
    get_current_user_optional,
    make_anon_tag,
    make_comment_owner_hash,
    reject_control_chars,
    render_and_sanitize_comment_html,
    validate_reaction_emoji,
    verify_visitor_cookie,
)
from app.config import Settings, get_settings
from app.db.models import (
    Comment,
    CommentTgReaction,
    CommentWebReaction,
    ModerationWhitelist,
    NotificationQueue,
    Poll,
    Post,
    PostView,
    TgReaction,
    User,
    WebPollVote,
    WebReaction,
)
from app.db.repo import (
    compute_merged_comment_reactions,
    compute_merged_poll,
    compute_merged_reactions,
    compute_merged_views,
    find_allowed_reaction_entry,
    format_iso_utc,
    get_allowed_reactions,
    make_reaction_key,
    resolve_media_url,
    serialize_comment_media,
    strip_variation_selectors,
)
from app.db.session import get_db_session
from app.media.optimize import (
    MAX_COMMENT_MEDIA_BYTES,
    InvalidImageError,
    process_comment_image_upload,
)
from app.moderation import (
    evaluate_comment_moderation,
    is_sender_whitelisted,
    resolve_sender_whitelist_identity,
    upsert_moderation_whitelist,
)
from app.routes.auth import serialize_user_dto
from app.routes.media import resolve_safe_media_path
from app.schemas import (
    AllowedReactionDTO,
    AllowedReactionsResponse,
    CommentAuthorDTO,
    CommentDTO,
    CommentMediaDTO,
    CommentReactionToggleResponse,
    CommentsListResponse,
    CreateCommentRequest,
    DeleteCommentResponse,
    InteractionsResponse,
    MergedPollDTO,
    MergedPollOptionDTO,
    MergedReactionDTO,
    ModerationCommentDTO,
    ModerationDecisionRequest,
    ModerationDecisionResponse,
    ModerationQueueResponse,
    ModerationWhitelistEntryDTO,
    PollVoteRequest,
    PollVoteResponse,
    PostInteractionDTO,
    PostViewRequest,
    PostViewResponse,
    ReactionToggleRequest,
    ReactionToggleResponse,
)
from app.sse import SSEBus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["interactions"])

DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

MAX_BATCH_IDS = 200
MAX_VIEW_BATCH_IDS = 50
MAX_COMMENT_LENGTH = 4000
MAX_WEB_REACTIONS_PER_POST_ACTOR = 5
MAX_WEB_REACTIONS_PER_ACTOR = MAX_WEB_REACTIONS_PER_POST_ACTOR


def _get_sse_bus(request: Request) -> SSEBus:
    bus: SSEBus | None = getattr(request.app.state, "sse_bus", None)
    if bus is None:
        bus = SSEBus()
        request.app.state.sse_bus = bus
    return bus


def _get_rate_limiter(request: Request) -> SlidingWindowRateLimiter:
    limiter: SlidingWindowRateLimiter | None = getattr(
        request.app.state, "rate_limiter", None
    )
    if limiter is None:
        limiter = SlidingWindowRateLimiter()
        request.app.state.rate_limiter = limiter
    return limiter


def _parse_int_csv(raw: str | None, *, max_items: int = MAX_BATCH_IDS) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    seen: set[int] = set()
    for part in raw.split(","):
        s = part.strip()
        if not s:
            continue
        try:
            val = int(s)
        except ValueError:
            continue
        if val > 0 and val not in seen:
            seen.add(val)
            out.append(val)
            if len(out) >= max_items:
                break
    return out


async def _acquire_actor_lock(db: AsyncSession, scope_key: str, actor_key: str) -> None:
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k1), hashtext(:k2))"),
        {"k1": scope_key, "k2": actor_key},
    )


async def _resolve_actor(
    request: Request,
    response: Response,
    db: AsyncSession,
    settings: Settings,
) -> tuple[User | None, str]:
    me = await get_current_user_optional(request, db)
    if me is not None:
        return me, f"u:{me.id}"
    vid = ensure_visitor_id(request, response, settings)
    return None, f"v:{vid}"


def _to_merged_poll_dto(poll_dict: dict[str, Any]) -> MergedPollDTO:
    return MergedPollDTO(
        id=poll_dict["id"],
        question=poll_dict["question"],
        is_closed=poll_dict["is_closed"],
        is_anonymous=poll_dict["is_anonymous"],
        type=poll_dict["type"],
        allow_multiple_answers=poll_dict["allow_multiple_answers"],
        total_voter_count=poll_dict["total_voter_count"],
        my_votes=poll_dict["my_votes"],
        correct_option_idx=poll_dict["correct_option_idx"],
        explanation=poll_dict["explanation"],
        options=[
            MergedPollOptionDTO(
                idx=opt["idx"],
                text=opt["text"],
                tg_voter_count=opt["tg_voter_count"],
                web_voter_count=opt["web_voter_count"],
                voter_count=opt["voter_count"],
            )
            for opt in poll_dict["options"]
        ],
    )


def _to_public_poll_sse_payload(poll: Poll) -> dict[str, Any]:
    public = compute_merged_poll(poll, actor_key=None)
    dto = _to_merged_poll_dto(public)
    dumped = dto.model_dump()
    dumped.pop("my_votes", None)
    return dumped


def _to_public_reactions_sse_payload(merged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in item.items() if k != "reacted"} for item in merged]


def _enforce_reaction_rate_limit(request: Request, settings: Settings) -> None:
    limiter = _get_rate_limiter(request)
    ip = get_client_ip(request, settings)
    allowed, _ = limiter.hit("reaction", ip, limit=60, window_seconds=60.0)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many reaction toggles; please slow down",
        )


def _parse_reaction_toggle_request(
    payload: ReactionToggleRequest,
) -> tuple[str | None, str | None]:
    raw_emoji = payload.emoji.strip() if payload.emoji is not None else ""
    raw_custom = (
        payload.custom_emoji_id.strip() if payload.custom_emoji_id is not None else ""
    )
    if bool(raw_emoji) == bool(raw_custom):
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of emoji or custom_emoji_id",
        )
    if raw_emoji:
        return validate_reaction_emoji(raw_emoji), None
    if not raw_custom.isascii() or not raw_custom.isdigit() or len(raw_custom) > 32:
        raise HTTPException(status_code=400, detail="Invalid custom_emoji_id")
    return None, raw_custom


async def _find_tg_reaction_match(
    db: AsyncSession,
    *,
    tg_model: type[TgReaction] | type[CommentTgReaction],
    fk_col_name: str,
    target_id: int,
    emoji: str | None = None,
    custom_emoji_id: str | None = None,
) -> tuple[bool, str | None]:
    """Return (exists, matched_emoji) when tg_model already has the reaction on target_id."""
    tg_fk = getattr(tg_model, fk_col_name)
    if custom_emoji_id is not None:
        rkey = make_reaction_key(custom_emoji_id=custom_emoji_id)
        hit = (
            await db.execute(
                select(tg_model.reaction_key)
                .where(tg_fk == target_id, tg_model.reaction_key == rkey)
                .limit(1)
            )
        ).scalar_one_or_none()
        return (hit is not None, None)

    assert emoji is not None
    stripped_target = strip_variation_selectors(emoji)
    tg_emojis = (
        await db.execute(
            select(tg_model.emoji).where(
                tg_fk == target_id,
                tg_model.emoji.is_not(None),
            )
        )
    ).scalars().all()
    for existing_emoji in tg_emojis:
        if (
            existing_emoji
            and strip_variation_selectors(existing_emoji) == stripped_target
        ):
            return (True, existing_emoji)
    return (False, None)


async def _normalize_reaction_emoji(
    db: AsyncSession,
    emoji: str,
    *,
    tg_model: type[TgReaction] | type[CommentTgReaction],
    web_model: type[WebReaction] | type[CommentWebReaction],
    fk_col_name: str,
    target_id: int,
) -> str:
    """Match variation selectors against existing TG/web reactions on the same target."""
    matched, tg_emoji = await _find_tg_reaction_match(
        db,
        tg_model=tg_model,
        fk_col_name=fk_col_name,
        target_id=target_id,
        emoji=emoji,
    )
    if matched and tg_emoji is not None:
        return tg_emoji

    stripped_target = strip_variation_selectors(emoji)
    web_fk = getattr(web_model, fk_col_name)
    web_emojis = (
        await db.execute(
            select(web_model.emoji)
            .where(
                web_fk == target_id,
                web_model.emoji.is_not(None),
            )
            .distinct()
        )
    ).scalars().all()
    for existing_emoji in web_emojis:
        if (
            existing_emoji
            and strip_variation_selectors(existing_emoji) == stripped_target
        ):
            return existing_emoji
    return emoji


async def _toggle_web_reaction(
    db: AsyncSession,
    *,
    tg_model: type[TgReaction] | type[CommentTgReaction],
    web_model: type[WebReaction] | type[CommentWebReaction],
    fk_col_name: str,
    target_id: int,
    target_label: str,
    lock_scope: str,
    actor_key: str,
    emoji: str | None,
    custom_emoji_id: str | None,
) -> None:
    """Serialize, normalize, enforce per-actor cap, and toggle a web reaction row."""
    await _acquire_actor_lock(db, lock_scope, actor_key)

    web_fk = getattr(web_model, fk_col_name)
    actor_rows = list(
        (
            await db.execute(
                select(web_model).where(
                    web_fk == target_id,
                    web_model.actor_key == actor_key,
                )
            )
        )
        .scalars()
        .all()
    )

    if emoji is not None:
        stripped_target = strip_variation_selectors(emoji)
        exact_key = make_reaction_key(emoji=emoji)
        matching_rows = [
            row
            for row in actor_rows
            if (
                row.emoji is not None
                and strip_variation_selectors(row.emoji) == stripped_target
            )
            or row.reaction_key == exact_key
        ]
    else:
        target_key = make_reaction_key(custom_emoji_id=custom_emoji_id)
        matching_rows = [row for row in actor_rows if row.reaction_key == target_key]

    if matching_rows:
        for row in matching_rows:
            await db.delete(row)
        await db.commit()
        return

    allowed_reactions = await get_allowed_reactions(db)
    allowed_entry = find_allowed_reaction_entry(
        allowed_reactions, emoji=emoji, custom_emoji_id=custom_emoji_id
    )
    if allowed_entry is None:
        has_existing_tg, _ = await _find_tg_reaction_match(
            db,
            tg_model=tg_model,
            fk_col_name=fk_col_name,
            target_id=target_id,
            emoji=emoji,
            custom_emoji_id=custom_emoji_id,
        )
        if not has_existing_tg:
            raise HTTPException(
                status_code=400,
                detail="Reaction emoji is not allowed",
            )

    if len(actor_rows) >= MAX_WEB_REACTIONS_PER_ACTOR:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {MAX_WEB_REACTIONS_PER_ACTOR} distinct reactions per {target_label} reached",
        )

    if emoji is not None:
        emoji = await _normalize_reaction_emoji(
            db,
            emoji,
            tg_model=tg_model,
            web_model=web_model,
            fk_col_name=fk_col_name,
            target_id=target_id,
        )
        if (
            strip_variation_selectors(emoji) == emoji
            and allowed_entry
            and allowed_entry.get("emoji")
        ):
            emoji = str(allowed_entry["emoji"])

    rkey = make_reaction_key(emoji=emoji, custom_emoji_id=custom_emoji_id)
    db.add(
        web_model(
            **{fk_col_name: target_id},
            reaction_key=rkey,
            emoji=emoji,
            custom_emoji_id=custom_emoji_id,
            actor_key=actor_key,
        )
    )
    await db.commit()


def _add_anon_ownership_tags(
    anon_tags: set[str],
    post_id: int,
    actor_key: str,
    secret: str,
) -> None:
    current = make_anon_tag(post_id, actor_key, secret)
    anon_tags.add(current)
    # Keep matching pre-rename DB rows whose anon_tag was stored as "Anonymous #<hex>".
    if current.startswith("Anon #"):
        anon_tags.add(f"Anonymous #{current[len('Anon #'):]}")


def _collect_viewer_ownership(
    request: Request,
    me: User | None,
    settings: Settings,
    *,
    post_id: int | None = None,
) -> tuple[set[str], set[str]]:
    actor_hashes: set[str] = set()
    anon_tags: set[str] = set()
    if me is not None:
        u_key = f"u:{me.id}"
        actor_hashes.add(make_comment_owner_hash(u_key, settings.SECRET_KEY))
        if post_id is not None:
            _add_anon_ownership_tags(anon_tags, post_id, u_key, settings.SECRET_KEY)
    vid = verify_visitor_cookie(
        request.cookies.get(VISITOR_COOKIE_NAME), settings.SECRET_KEY
    )
    if vid is not None:
        v_key = f"v:{vid}"
        actor_hashes.add(make_comment_owner_hash(v_key, settings.SECRET_KEY))
        if post_id is not None:
            _add_anon_ownership_tags(anon_tags, post_id, v_key, settings.SECRET_KEY)
    return actor_hashes, anon_tags


_PHOTO_MEDIA_EXTS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".apng", ".avif"})
_STICKER_MEDIA_EXTS: frozenset[str] = frozenset({".webp", ".tgs", ".webm", ".png"})
_THUMB_MEDIA_EXTS: frozenset[str] = frozenset({".webp", ".jpg", ".jpeg", ".png", ".gif"})
_MEDIA_MIME_BY_EXT: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".apng": "image/apng",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
    ".tgs": "application/x-tgsticker",
    ".webm": "video/webm",
}


def _normalize_comment_media_input(
    media: CommentMediaDTO,
    settings: Settings,
) -> dict[str, Any]:
    media_type = media.media_type.strip().lower()
    if media_type not in {"photo", "sticker"}:
        raise HTTPException(
            status_code=400,
            detail="Comment media_type must be 'photo' or 'sticker'",
        )

    media_root_resolved = settings.media_dir.resolve()
    media_prefix = f"{settings.API_PREFIX.rstrip('/')}/media/"
    abs_media_prefix = f"{settings.api_base_url.rstrip('/')}/media/"

    def _extract_safe_rel(
        raw_url: str,
        *,
        allowed_prefixes: tuple[str, ...],
        allowed_exts: frozenset[str],
    ) -> tuple[str, Any]:
        cleaned = raw_url.strip()
        if not cleaned:
            raise HTTPException(status_code=400, detail="Invalid comment media path")
        if cleaned.startswith(abs_media_prefix):
            cleaned = cleaned[len(abs_media_prefix) :]
        elif cleaned.startswith(media_prefix):
            cleaned = cleaned[len(media_prefix) :]
        else:
            lowered = cleaned.lower()
            if lowered.startswith(("http://", "https://", "//", "data:", "javascript:", "vbscript:")):
                raise HTTPException(status_code=400, detail="Invalid comment media path")

        resolved = resolve_safe_media_path(settings.media_dir, cleaned)
        rel = resolved.relative_to(media_root_resolved).as_posix()
        if not rel.startswith(allowed_prefixes):
            raise HTTPException(status_code=400, detail="Invalid comment media directory")
        ext = resolved.suffix.lower()
        if ext not in allowed_exts:
            raise HTTPException(status_code=400, detail="Unsupported comment media extension")
        return rel, resolved

    if media_type == "photo":
        rel_url, resolved_main = _extract_safe_rel(
            media.url,
            allowed_prefixes=("comments/",),
            allowed_exts=_PHOTO_MEDIA_EXTS,
        )
    else:
        rel_url, resolved_main = _extract_safe_rel(
            media.url,
            allowed_prefixes=("stickers/", "comments/"),
            allowed_exts=_STICKER_MEDIA_EXTS,
        )

    rel_thumb: str | None = None
    if media.thumb:
        rel_thumb, _ = _extract_safe_rel(
            media.thumb,
            allowed_prefixes=("comments/", "stickers/"),
            allowed_exts=_THUMB_MEDIA_EXTS,
        )

    main_ext = resolved_main.suffix.lower()
    disk_size = resolved_main.stat().st_size
    disk_mime = _MEDIA_MIME_BY_EXT.get(main_ext)

    out: dict[str, Any] = {
        "media_type": media_type,
        "url": rel_url,
        "size": disk_size,
    }
    if disk_mime:
        out["mime_type"] = disk_mime
    if rel_thumb:
        out["thumb"] = rel_thumb
    if media.width is not None and media.width > 0:
        out["width"] = int(media.width)
    if media.height is not None and media.height > 0:
        out["height"] = int(media.height)
    if media.emoji:
        out["emoji"] = str(media.emoji)[:32]
    if media_type == "sticker":
        out["is_animated"] = main_ext == ".tgs"
        out["is_video"] = main_ext == ".webm"
    if media.sticker_id:
        out["sticker_id"] = str(media.sticker_id)[:128]
    return out


def _is_comment_owned_by_viewer(
    comment: Comment,
    *,
    me: User | None,
    viewer_actor_hashes: set[str] | None,
    viewer_anon_tags: set[str] | None,
) -> bool:
    is_mine_user = bool(
        me is not None and comment.user_id is not None and comment.user_id == me.id
    )
    is_mine_hash = bool(
        viewer_actor_hashes
        and comment.actor_key_hash is not None
        and comment.actor_key_hash in viewer_actor_hashes
    )
    is_mine_legacy_anon_tag = bool(
        comment.actor_key_hash is None
        and comment.user_id is None
        and comment.source == "web"
        and viewer_anon_tags
        and comment.anon_tag is not None
        and comment.anon_tag in viewer_anon_tags
    )
    return is_mine_user or is_mine_hash or is_mine_legacy_anon_tag


def _serialize_comment(
    comment: Comment,
    *,
    users_by_id: dict[int, User],
    me: User | None,
    settings: Settings,
    tombstone: bool = False,
    viewer_anon_tags: set[str] | None = None,
    viewer_actor_hashes: set[str] | None = None,
    actor_key: str | None = None,
    include_reactions: bool = True,
) -> CommentDTO:
    if tombstone or comment.deleted_at is not None:
        return CommentDTO(
            id=comment.id,
            post_id=comment.post_id,
            parent_id=comment.parent_id,
            source=comment.source,
            author=CommentAuthorDTO(
                display_name="[deleted]",
                avatar_url=None,
                role="anonymous",
                provider=None,
                username=None,
                verified=False,
            ),
            body_text="[deleted]",
            body_html="<p>[deleted]</p>",
            media=None,
            created_at=format_iso_utc(comment.created_at),
            edited_at=format_iso_utc(comment.edited_at) if comment.edited_at else None,
            deleted=True,
            is_mine=False,
            can_delete=False,
            moderation_status="approved",
            reactions=[],
        )

    user = users_by_id.get(comment.user_id) if comment.user_id is not None else None
    if user is not None:
        primary_ident = user.identities[0] if user.identities else None
        author = CommentAuthorDTO(
            display_name=user.display_name,
            avatar_url=user.avatar_url,
            role=user.role,
            provider=primary_ident.provider if primary_ident else "email",
            username=primary_ident.username if primary_ident else None,
            verified=True,
        )
    elif comment.source == "telegram":
        is_owner = (
            settings.OWNER_TG_ID is not None
            and comment.tg_author_id == settings.OWNER_TG_ID
        )
        author = CommentAuthorDTO(
            display_name=comment.tg_author_name or "Telegram User",
            avatar_url=resolve_media_url(comment.tg_author_avatar, settings.API_PREFIX),
            role="owner" if is_owner else "user",
            provider="telegram",
            username=comment.tg_author_username,
            verified=True,
        )
    else:
        author = CommentAuthorDTO(
            display_name=format_anon_display_name(comment.anon_tag),
            avatar_url=None,
            role="anonymous",
            provider=None,
            username=None,
            verified=False,
        )

    is_mine = _is_comment_owned_by_viewer(
        comment,
        me=me,
        viewer_actor_hashes=viewer_actor_hashes,
        viewer_anon_tags=viewer_anon_tags,
    )
    can_delete = bool(is_mine or (me is not None and me.role == "owner"))

    reactions = (
        [
            MergedReactionDTO(**r)
            for r in compute_merged_comment_reactions(comment, actor_key=actor_key)
        ]
        if include_reactions
        else []
    )

    return CommentDTO(
        id=comment.id,
        post_id=comment.post_id,
        parent_id=comment.parent_id,
        source=comment.source,
        author=author,
        body_text=comment.body_text,
        body_html=comment.body_html,
        media=serialize_comment_media(comment.media, settings.API_PREFIX),
        created_at=format_iso_utc(comment.created_at),
        edited_at=format_iso_utc(comment.edited_at) if comment.edited_at else None,
        deleted=False,
        is_mine=is_mine,
        can_delete=can_delete,
        moderation_status=comment.moderation_status,
        reactions=reactions,
    )


def _schedule_tg_bridge_method(
    request: Request,
    *,
    method_name: str,
    comment_id: int,
    task_prefix: str,
    allow_callable: bool = False,
) -> None:
    tg_bridge = getattr(request.app.state, "tg_bridge", None)
    if tg_bridge is None:
        return

    async def _run() -> None:
        try:
            if allow_callable and callable(tg_bridge):
                res = tg_bridge(comment_id)
                if inspect.isawaitable(res):
                    await res
            elif hasattr(tg_bridge, method_name):
                res = getattr(tg_bridge, method_name)(comment_id)
                if inspect.isawaitable(res):
                    await res
        except Exception:
            logger.exception("%s failed for comment_id=%s", task_prefix, comment_id)

    task = asyncio.create_task(_run(), name=f"{task_prefix}-{comment_id}")
    tasks: set[asyncio.Task[Any]] | None = getattr(
        request.app.state, "background_tasks", None
    )
    if tasks is not None:
        tasks.add(task)
        task.add_done_callback(tasks.discard)


def _schedule_tg_bridge(request: Request, comment_id: int) -> None:
    _schedule_tg_bridge_method(
        request,
        method_name="bridge_web_comment",
        comment_id=comment_id,
        task_prefix="tg-bridge-comment",
        allow_callable=True,
    )


def _schedule_tg_delete(request: Request, comment_id: int) -> None:
    _schedule_tg_bridge_method(
        request,
        method_name="delete_bridged_comment",
        comment_id=comment_id,
        task_prefix="tg-delete-comment",
    )


def _schedule_moderation_ping(request: Request, comment_id: int) -> None:
    _schedule_tg_bridge_method(
        request,
        method_name="notify_owner_moderation_pending",
        comment_id=comment_id,
        task_prefix="tg-mod-alert",
    )


# ---------------------------------------------------------------------------
# 1. Batch Interactions Hydration
# ---------------------------------------------------------------------------


@router.get("/interactions", response_model=InteractionsResponse)
async def get_interactions(
    request: Request,
    response: Response,
    db: DbSessionDep,
    settings: SettingsDep,
    ids: Annotated[str | None, Query()] = None,
) -> InteractionsResponse:
    me, actor_key = await _resolve_actor(request, response, db, settings)
    post_ids = _parse_int_csv(ids)

    posts_payload: dict[str, PostInteractionDTO] = {}
    if post_ids:
        posts_stmt = (
            select(Post)
            .options(
                selectinload(Post.tg_reactions),
                selectinload(Post.web_reactions),
                selectinload(Post.poll).selectinload(Poll.options),
                selectinload(Post.poll).selectinload(Poll.web_votes),
            )
            .where(Post.id.in_(post_ids), Post.is_deleted.is_(False))
        )
        posts = list((await db.execute(posts_stmt)).scalars().all())

        counts_stmt = (
            select(Comment.post_id, func.count(Comment.id))
            .where(
                Comment.post_id.in_(post_ids),
                Comment.deleted_at.is_(None),
                Comment.moderation_status == "approved",
            )
            .group_by(Comment.post_id)
        )
        comment_counts = {
            int(pid): int(cnt) for pid, cnt in (await db.execute(counts_stmt)).all()
        }

        for post in posts:
            merged_reactions = [
                MergedReactionDTO(**r)
                for r in compute_merged_reactions(post, actor_key=actor_key)
            ]
            merged_poll = (
                _to_merged_poll_dto(compute_merged_poll(post.poll, actor_key=actor_key))
                if post.poll is not None
                else None
            )
            posts_payload[str(post.id)] = PostInteractionDTO(
                reactions=merged_reactions,
                poll=merged_poll,
                comment_count=comment_counts.get(post.id, 0),
                views=compute_merged_views(post),
            )

    allowed_reactions = [
        AllowedReactionDTO(**item) for item in await get_allowed_reactions(db)
    ]

    return InteractionsResponse(
        allow_anon_comments=settings.ALLOW_ANON_COMMENTS,
        captcha_provider="altcha",
        me=serialize_user_dto(me) if me is not None else None,
        allowed_reactions=allowed_reactions,
        posts=posts_payload,
    )


@router.get("/reactions/allowed", response_model=AllowedReactionsResponse)
async def get_allowed_reactions_route(
    db: DbSessionDep,
) -> AllowedReactionsResponse:
    return AllowedReactionsResponse(
        allowed_reactions=[
            AllowedReactionDTO(**item) for item in await get_allowed_reactions(db)
        ]
    )


@router.post("/posts/view", response_model=PostViewResponse)
async def record_post_views(
    payload: PostViewRequest,
    request: Request,
    response: Response,
    db: DbSessionDep,
    settings: SettingsDep,
) -> PostViewResponse:
    limiter = _get_rate_limiter(request)
    ip = get_client_ip(request, settings)
    allowed, _ = limiter.hit("view", ip, limit=60, window_seconds=60.0)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many view requests; please slow down",
        )

    unique_ids: list[int] = []
    seen_ids: set[int] = set()
    for raw_id in payload.post_ids:
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            continue
        if raw_id > 0 and raw_id not in seen_ids:
            seen_ids.add(raw_id)
            unique_ids.append(raw_id)
            if len(unique_ids) >= MAX_VIEW_BATCH_IDS:
                break

    me = await get_current_user_optional(request, db)
    existing_vid = verify_visitor_cookie(
        request.cookies.get(VISITOR_COOKIE_NAME), settings.SECRET_KEY
    )
    had_valid_actor = (me is not None) or (existing_vid is not None)
    if me is not None:
        actor_key = f"u:{me.id}"
    else:
        vid = (
            existing_vid
            if existing_vid is not None
            else ensure_visitor_id(request, response, settings)
        )
        actor_key = f"v:{vid}"

    if not unique_ids:
        return PostViewResponse(views={})

    existing_posts = list(
        (
            await db.execute(
                select(Post)
                .where(Post.id.in_(unique_ids), Post.is_deleted.is_(False))
                .order_by(Post.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if not existing_posts:
        return PostViewResponse(views={})

    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=24)
    updated_views_map: dict[str, str] = {}
    all_views_map: dict[str, str] = {}

    for post in existing_posts:
        if not had_valid_actor:
            allowed_nc, _ = limiter.hit(
                f"view-nc:{post.id}", ip, limit=1, window_seconds=3600.0
            )
            if not allowed_nc:
                merged_str = compute_merged_views(post)
                if merged_str is not None:
                    all_views_map[str(post.id)] = merged_str
                continue

        view_stmt = (
            pg_insert(PostView)
            .values(post_id=post.id, actor_key=actor_key, viewed_at=now)
            .on_conflict_do_update(
                index_elements=[PostView.post_id, PostView.actor_key],
                set_={"viewed_at": now},
                where=PostView.viewed_at < cutoff,
            )
            .returning(PostView.post_id)
        )
        inserted_or_updated = (await db.execute(view_stmt)).scalar_one_or_none()
        if inserted_or_updated is not None:
            upd_row = (
                await db.execute(
                    update(Post)
                    .where(Post.id == post.id)
                    .values(web_views=Post.web_views + 1)
                    .returning(Post.views, Post.web_views)
                )
            ).one()
            merged_str = str((upd_row[0] or 0) + int(upd_row[1]))
            updated_views_map[str(post.id)] = merged_str
            all_views_map[str(post.id)] = merged_str
        else:
            merged_str = compute_merged_views(post)
            if merged_str is not None:
                all_views_map[str(post.id)] = merged_str

    await db.commit()

    if updated_views_map:
        _get_sse_bus(request).publish(
            "post.views.updated",
            {"views": updated_views_map},
            post_id=None,
        )

    return PostViewResponse(views=all_views_map)


# ---------------------------------------------------------------------------
# 2. Reactions Toggle
# ---------------------------------------------------------------------------


@router.post("/posts/{post_id}/reactions/toggle", response_model=ReactionToggleResponse)
async def toggle_reaction(
    post_id: int,
    payload: ReactionToggleRequest,
    request: Request,
    response: Response,
    db: DbSessionDep,
    settings: SettingsDep,
) -> ReactionToggleResponse:
    _enforce_reaction_rate_limit(request, settings)
    emoji, custom_emoji_id = _parse_reaction_toggle_request(payload)

    post_exists = (
        await db.execute(
            select(Post.id).where(Post.id == post_id, Post.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if post_exists is None:
        raise HTTPException(status_code=404, detail="Post not found")

    _, actor_key = await _resolve_actor(request, response, db, settings)

    await _toggle_web_reaction(
        db,
        tg_model=TgReaction,
        web_model=WebReaction,
        fk_col_name="post_id",
        target_id=post_id,
        target_label="post",
        lock_scope=f"react:{post_id}",
        actor_key=actor_key,
        emoji=emoji,
        custom_emoji_id=custom_emoji_id,
    )

    refreshed_post = (
        await db.execute(
            select(Post)
            .options(
                selectinload(Post.tg_reactions),
                selectinload(Post.web_reactions),
            )
            .where(Post.id == post_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    merged = compute_merged_reactions(refreshed_post, actor_key=actor_key)
    _get_sse_bus(request).publish(
        "reaction.updated",
        {
            "post_id": post_id,
            "reactions": _to_public_reactions_sse_payload(merged),
        },
        post_id=post_id,
    )

    return ReactionToggleResponse(
        post_id=post_id,
        reactions=[MergedReactionDTO(**r) for r in merged],
    )


@router.post(
    "/comments/{comment_id}/reactions/toggle",
    response_model=CommentReactionToggleResponse,
)
async def toggle_comment_reaction(
    comment_id: int,
    payload: ReactionToggleRequest,
    request: Request,
    response: Response,
    db: DbSessionDep,
    settings: SettingsDep,
) -> CommentReactionToggleResponse:
    _enforce_reaction_rate_limit(request, settings)
    emoji, custom_emoji_id = _parse_reaction_toggle_request(payload)

    comment_post_id = (
        await db.execute(
            select(Comment.post_id)
            .join(Post, Post.id == Comment.post_id)
            .where(
                Comment.id == comment_id,
                Comment.deleted_at.is_(None),
                Comment.moderation_status == "approved",
                Post.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if comment_post_id is None:
        raise HTTPException(status_code=404, detail="Comment not found")

    _, actor_key = await _resolve_actor(request, response, db, settings)

    await _toggle_web_reaction(
        db,
        tg_model=CommentTgReaction,
        web_model=CommentWebReaction,
        fk_col_name="comment_id",
        target_id=comment_id,
        target_label="comment",
        lock_scope=f"react-comment:{comment_id}",
        actor_key=actor_key,
        emoji=emoji,
        custom_emoji_id=custom_emoji_id,
    )

    refreshed_comment = (
        await db.execute(
            select(Comment)
            .options(
                selectinload(Comment.tg_reactions),
                selectinload(Comment.web_reactions),
            )
            .where(Comment.id == comment_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    merged = compute_merged_comment_reactions(refreshed_comment, actor_key=actor_key)
    _get_sse_bus(request).publish(
        "comment.reaction.updated",
        {
            "post_id": refreshed_comment.post_id,
            "comment_id": refreshed_comment.id,
            "reactions": _to_public_reactions_sse_payload(merged),
        },
        post_id=refreshed_comment.post_id,
    )

    return CommentReactionToggleResponse(
        post_id=refreshed_comment.post_id,
        comment_id=refreshed_comment.id,
        reactions=[MergedReactionDTO(**r) for r in merged],
    )


# ---------------------------------------------------------------------------
# 3. Polls Voting
# ---------------------------------------------------------------------------


@router.post("/polls/{poll_id}/vote", response_model=PollVoteResponse)
async def vote_poll(
    poll_id: str,
    payload: PollVoteRequest,
    request: Request,
    response: Response,
    db: DbSessionDep,
    settings: SettingsDep,
) -> PollVoteResponse:
    limiter = _get_rate_limiter(request)
    ip = get_client_ip(request, settings)
    allowed, _ = limiter.hit("poll-vote", ip, limit=30, window_seconds=60.0)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many poll votes; please slow down",
        )

    poll_stmt = (
        select(Poll)
        .join(Post, Post.id == Poll.post_id)
        .options(selectinload(Poll.options))
        .where(Poll.id == poll_id, Post.is_deleted.is_(False))
        .execution_options(populate_existing=True)
    )
    poll = (await db.execute(poll_stmt)).scalar_one_or_none()
    if poll is None:
        raise HTTPException(status_code=404, detail="Poll not found")

    if poll.is_closed:
        raise HTTPException(status_code=409, detail="Poll is closed")

    valid_indices = {opt.option_idx for opt in poll.options}
    unique_indices = sorted(set(payload.option_indices))
    for idx in unique_indices:
        if idx not in valid_indices:
            raise HTTPException(status_code=400, detail=f"Invalid option index: {idx}")

    if not poll.allow_multiple_answers and len(unique_indices) > 1:
        raise HTTPException(
            status_code=400,
            detail="Multiple answers are not allowed for this poll",
        )

    _, actor_key = await _resolve_actor(request, response, db, settings)

    # Serialize concurrent votes for the same (poll_id, actor_key) BEFORE checking QUIZ lock
    await _acquire_actor_lock(db, f"poll:{poll_id}", actor_key)

    existing_actor_votes = list(
        (
            await db.execute(
                select(WebPollVote.option_idx).where(
                    WebPollVote.poll_id == poll_id,
                    WebPollVote.actor_key == actor_key,
                )
            )
        )
        .scalars()
        .all()
    )
    if poll.type == "QUIZ":
        if existing_actor_votes:
            raise HTTPException(
                status_code=409,
                detail="Quiz poll vote is locked after voting",
            )
        if not unique_indices:
            raise HTTPException(
                status_code=400,
                detail="Quiz polls require selecting an option",
            )

    await db.execute(
        delete(WebPollVote).where(
            WebPollVote.poll_id == poll_id,
            WebPollVote.actor_key == actor_key,
        )
    )
    for idx in unique_indices:
        db.add(
            WebPollVote(
                poll_id=poll_id,
                option_idx=idx,
                actor_key=actor_key,
            )
        )
    await db.commit()

    refreshed_poll = (
        await db.execute(
            select(Poll)
            .options(
                selectinload(Poll.options),
                selectinload(Poll.web_votes),
            )
            .where(Poll.id == poll_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    merged_for_actor = _to_merged_poll_dto(
        compute_merged_poll(refreshed_poll, actor_key=actor_key)
    )
    _get_sse_bus(request).publish(
        "poll.updated",
        {
            "post_id": refreshed_poll.post_id,
            "poll": _to_public_poll_sse_payload(refreshed_poll),
        },
        post_id=refreshed_poll.post_id,
    )

    return PollVoteResponse(
        post_id=refreshed_poll.post_id,
        poll=merged_for_actor,
    )


# ---------------------------------------------------------------------------
# 4. Comments (List, Create, Soft-Delete)
# ---------------------------------------------------------------------------


@router.get("/posts/{post_id}/comments", response_model=CommentsListResponse)
async def get_post_comments(
    post_id: int,
    request: Request,
    db: DbSessionDep,
    settings: SettingsDep,
) -> CommentsListResponse:
    post_exists = (
        await db.execute(
            select(Post.id).where(Post.id == post_id, Post.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if post_exists is None:
        raise HTTPException(status_code=404, detail="Post not found")

    me = await get_current_user_optional(request, db)
    viewer_hashes, viewer_anon_tags = _collect_viewer_ownership(
        request, me, settings, post_id=post_id
    )
    vid = verify_visitor_cookie(
        request.cookies.get(VISITOR_COOKIE_NAME), settings.SECRET_KEY
    )
    actor_key = f"u:{me.id}" if me is not None else (f"v:{vid}" if vid is not None else None)

    comments_stmt = (
        select(Comment)
        .options(
            selectinload(Comment.tg_reactions),
            selectinload(Comment.web_reactions),
        )
        .where(Comment.post_id == post_id)
        .order_by(Comment.created_at.asc(), Comment.id.asc())
    )
    all_comments = list((await db.execute(comments_stmt)).scalars().all())

    def _is_visible_to_viewer(c: Comment) -> bool:
        if c.deleted_at is not None:
            return False
        if c.moderation_status == "approved":
            return True
        if c.moderation_status == "pending":
            return _is_comment_owned_by_viewer(
                c,
                me=me,
                viewer_actor_hashes=viewer_hashes or None,
                viewer_anon_tags=viewer_anon_tags or None,
            )
        return False

    # Iterative O(N) upward walk from live visible comments to mark ancestors that need tombstones
    by_id = {c.id: c for c in all_comments}
    has_live_descendant: set[int] = set()
    for c in all_comments:
        if not _is_visible_to_viewer(c):
            continue
        curr_parent = c.parent_id
        while curr_parent is not None and curr_parent not in has_live_descendant:
            has_live_descendant.add(curr_parent)
            parent_obj = by_id.get(curr_parent)
            curr_parent = parent_obj.parent_id if parent_obj is not None else None

    visible_comments: list[tuple[Comment, bool]] = []
    for c in all_comments:
        if _is_visible_to_viewer(c):
            visible_comments.append((c, False))
        elif c.deleted_at is not None and c.id in has_live_descendant:
            visible_comments.append((c, True))

    user_ids = {
        c.user_id
        for c, is_tomb in visible_comments
        if not is_tomb and c.user_id is not None
    }
    users_by_id: dict[int, User] = {}
    if user_ids:
        users_rows = (
            await db.execute(
                select(User)
                .options(selectinload(User.identities))
                .where(User.id.in_(user_ids))
            )
        ).scalars().all()
        users_by_id = {u.id: u for u in users_rows}

    serialized = [
        _serialize_comment(
            c,
            users_by_id=users_by_id,
            me=me,
            settings=settings,
            tombstone=is_tomb,
            viewer_anon_tags=viewer_anon_tags or None,
            viewer_actor_hashes=viewer_hashes or None,
            actor_key=actor_key,
        )
        for c, is_tomb in visible_comments
    ]
    viewer_anon_tag = (
        make_anon_tag(post_id, actor_key, settings.SECRET_KEY)
        if actor_key is not None
        else None
    )
    return CommentsListResponse(
        post_id=post_id,
        comments=serialized,
        viewer_anon_tag=viewer_anon_tag,
    )


@router.post("/comments/media", response_model=CommentMediaDTO)
async def upload_comment_media(
    request: Request,
    db: DbSessionDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
) -> CommentMediaDTO:
    me = await get_current_user_optional(request, db)
    if me is None and not settings.ALLOW_ANON_COMMENTS:
        raise HTTPException(
            status_code=403,
            detail="Anonymous comments are disabled",
        )

    limiter = _get_rate_limiter(request)
    ip = get_client_ip(request, settings)
    allowed, _ = limiter.hit("comment-media", ip, limit=20, window_seconds=60.0)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many media uploads; please slow down",
        )

    try:
        data = await file.read(MAX_COMMENT_MEDIA_BYTES + 1)
    finally:
        await file.close()

    try:
        saved = await asyncio.to_thread(
            process_comment_image_upload,
            data,
            settings.media_dir,
            max_bytes=MAX_COMMENT_MEDIA_BYTES,
        )
    except InvalidImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return CommentMediaDTO(
        media_type=saved["media_type"],
        url=resolve_media_url(saved["url"], settings.API_PREFIX) or saved["url"],
        thumb=resolve_media_url(saved["thumb"], settings.API_PREFIX),
        width=saved["width"],
        height=saved["height"],
        size=saved["size"],
        mime_type=saved["mime_type"],
    )


@router.post(
    "/posts/{post_id}/comments",
    response_model=CommentDTO,
    status_code=201,
)
async def create_comment(
    post_id: int,
    payload: CreateCommentRequest,
    request: Request,
    response: Response,
    db: DbSessionDep,
    settings: SettingsDep,
) -> CommentDTO:
    limiter = _get_rate_limiter(request)
    ip = get_client_ip(request, settings)
    allowed, _ = limiter.hit("comment", ip, limit=15, window_seconds=60.0)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many comments; please wait a moment",
        )

    post = (
        await db.execute(
            select(Post).where(Post.id == post_id, Post.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")

    reject_control_chars(payload.body, allow_newlines=True)
    stripped_body = payload.body.strip()
    if len(stripped_body) > MAX_COMMENT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Comment body must be at most {MAX_COMMENT_LENGTH} characters",
        )
    if not stripped_body and payload.media is None:
        raise HTTPException(
            status_code=400,
            detail="Comment must include body text or media",
        )

    media_payload: dict[str, Any] | None = None
    if payload.media is not None:
        media_payload = _normalize_comment_media_input(payload.media, settings)

    parent_comment: Comment | None = None
    if payload.parent_id is not None:
        parent_comment = await db.get(Comment, payload.parent_id)
        if (
            parent_comment is None
            or parent_comment.post_id != post_id
            or parent_comment.deleted_at is not None
        ):
            raise HTTPException(status_code=404, detail="Parent comment not found")

    me, actor_key = await _resolve_actor(request, response, db, settings)
    is_anonymous = (me is None) or payload.as_anonymous

    comment_user_id: int | None = None
    anon_tag: str | None = None

    if is_anonymous:
        if not settings.ALLOW_ANON_COMMENTS:
            raise HTTPException(
                status_code=403,
                detail="Anonymous comments are disabled",
            )
        await verify_altcha_payload(db, payload.altcha_payload, settings)
        anon_tag = make_anon_tag(post_id, actor_key, settings.SECRET_KEY)
    else:
        assert me is not None
        comment_user_id = me.id

    actor_key_hash = make_comment_owner_hash(actor_key, settings.SECRET_KEY)
    body_html = render_and_sanitize_comment_html(stripped_body) if stripped_body else ""
    now = datetime.now(UTC)

    is_owner = me is not None and me.role == "owner"
    whitelisted = await is_sender_whitelisted(
        db,
        user_id=comment_user_id,
        actor_key_hash=actor_key_hash,
    )
    mod_decision = await evaluate_comment_moderation(
        settings=settings,
        body_text=stripped_body,
        media=media_payload,
        is_anonymous=is_anonymous,
        is_owner=is_owner,
        is_whitelisted=whitelisted,
    )

    comment = Comment(
        post_id=post_id,
        parent_id=payload.parent_id,
        source="web",
        channel_id=post.channel_id,
        user_id=comment_user_id,
        anon_tag=anon_tag,
        actor_key_hash=actor_key_hash,
        body_text=stripped_body,
        body_html=body_html,
        media=media_payload,
        moderation_status=mod_decision.status,
        moderation_meta=mod_decision.meta,
        created_at=now,
    )
    db.add(comment)
    await db.flush()

    if mod_decision.status == "approved":
        await _enqueue_parent_reply_notification(
            db,
            parent_comment=parent_comment,
            comment=comment,
            now=now,
            settings=settings,
        )

    await db.commit()

    users_by_id = {me.id: me} if (me is not None and comment_user_id == me.id) else {}
    caller_dto = _serialize_comment(
        comment,
        users_by_id=users_by_id,
        me=me,
        settings=settings,
        viewer_anon_tags={anon_tag} if anon_tag else None,
        viewer_actor_hashes={actor_key_hash},
        include_reactions=False,
    )

    if mod_decision.status == "approved":
        public_dto = _serialize_comment(
            comment,
            users_by_id=users_by_id,
            me=None,
            settings=settings,
            include_reactions=False,
        )
        # Publish raw CommentDTO directly on `comment.created` (matches frontend api.ts)
        _get_sse_bus(request).publish(
            "comment.created",
            public_dto.model_dump(),
            post_id=post_id,
        )
        _schedule_tg_bridge(request, comment.id)
    elif mod_decision.status == "pending":
        _schedule_moderation_ping(request, comment.id)

    return caller_dto


@router.delete("/comments/{comment_id}", response_model=DeleteCommentResponse)
async def delete_comment(
    comment_id: int,
    request: Request,
    db: DbSessionDep,
    settings: SettingsDep,
) -> DeleteCommentResponse:
    me = await get_current_user_optional(request, db)
    vid = verify_visitor_cookie(
        request.cookies.get(VISITOR_COOKIE_NAME), settings.SECRET_KEY
    )
    if me is None and vid is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    comment = await db.get(Comment, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")

    viewer_hashes, viewer_anon_tags = _collect_viewer_ownership(
        request, me, settings, post_id=comment.post_id
    )
    is_owner = me is not None and me.role == "owner"
    owns_by_user = me is not None and comment.user_id is not None and comment.user_id == me.id
    owns_by_hash = (
        comment.actor_key_hash is not None and comment.actor_key_hash in viewer_hashes
    )
    owns_by_legacy_anon = (
        comment.actor_key_hash is None
        and comment.user_id is None
        and comment.source == "web"
        and comment.anon_tag is not None
        and comment.anon_tag in viewer_anon_tags
    )
    if not (is_owner or owns_by_user or owns_by_hash or owns_by_legacy_anon):
        raise HTTPException(
            status_code=403,
            detail="You are not allowed to delete this comment",
        )

    if comment.deleted_at is None:
        comment.deleted_at = datetime.now(UTC)
        await db.commit()
        _get_sse_bus(request).publish(
            "comment.deleted",
            {
                "post_id": comment.post_id,
                "comment_id": comment.id,
            },
            post_id=comment.post_id,
        )
        if comment.tg_msg_id is not None and comment.channel_id is not None:
            _schedule_tg_delete(request, comment.id)

    return DeleteCommentResponse(
        ok=True,
        post_id=comment.post_id,
        comment_id=comment.id,
    )


# ---------------------------------------------------------------------------
# 5. Owner Moderation Queue & Sender Whitelist
# ---------------------------------------------------------------------------


async def _require_owner(request: Request, db: AsyncSession) -> User:
    me = await get_current_user_optional(request, db)
    if me is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if me.role != "owner":
        raise HTTPException(status_code=403, detail="Owner privileges required")
    return me


async def _enqueue_parent_reply_notification(
    db: AsyncSession,
    *,
    parent_comment: Comment | None,
    comment: Comment,
    now: datetime,
    settings: Settings,
) -> None:
    if (
        parent_comment is None
        or parent_comment.user_id is None
        or parent_comment.user_id == comment.user_id
    ):
        return
    parent_user = await db.get(User, parent_comment.user_id)
    if (
        parent_user is not None
        and parent_user.email
        and parent_user.email_verified
        and parent_user.notify_replies
    ):
        db.add(
            NotificationQueue(
                recipient_user_id=parent_user.id,
                comment_id=comment.id,
                post_id=comment.post_id,
                send_after=now + timedelta(seconds=settings.EMAIL_DEBOUNCE_SECONDS),
            )
        )


def _format_whitelist_label(comment: Comment, users_by_id: dict[int, User]) -> str:
    if comment.user_id is not None and comment.user_id in users_by_id:
        u = users_by_id[comment.user_id]
        return f"{u.display_name} (user #{u.id})"
    tag = format_anon_display_name(comment.anon_tag)
    short_hash = (comment.actor_key_hash or "")[:8]
    return f"{tag} [{short_hash}]" if short_hash else tag


@router.get("/moderation/comments", response_model=ModerationQueueResponse)
async def get_moderation_queue(
    request: Request,
    db: DbSessionDep,
    settings: SettingsDep,
) -> ModerationQueueResponse:
    await _require_owner(request, db)

    pending_rows = list(
        (
            await db.execute(
                select(Comment)
                .where(
                    Comment.moderation_status == "pending",
                    Comment.deleted_at.is_(None),
                )
                .order_by(Comment.created_at.desc(), Comment.id.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    whitelist_rows = list(
        (
            await db.execute(
                select(ModerationWhitelist).order_by(
                    ModerationWhitelist.created_at.desc(),
                    ModerationWhitelist.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    wl_keys = {(w.kind, w.key) for w in whitelist_rows}

    user_ids = {c.user_id for c in pending_rows if c.user_id is not None}
    users_by_id: dict[int, User] = {}
    if user_ids:
        u_rows = (
            await db.execute(
                select(User)
                .options(selectinload(User.identities))
                .where(User.id.in_(user_ids))
            )
        ).scalars().all()
        users_by_id = {u.id: u for u in u_rows}

    comments_out: list[ModerationCommentDTO] = []
    for c in pending_rows:
        base_dto = _serialize_comment(
            c,
            users_by_id=users_by_id,
            me=None,
            settings=settings,
            include_reactions=False,
        )
        ident = resolve_sender_whitelist_identity(c)
        comments_out.append(
            ModerationCommentDTO(
                id=c.id,
                post_id=c.post_id,
                parent_id=c.parent_id,
                source=c.source,
                author=base_dto.author,
                body_text=c.body_text,
                body_html=c.body_html,
                media=base_dto.media,
                created_at=format_iso_utc(c.created_at),
                moderation_status=c.moderation_status,
                moderation_meta=c.moderation_meta if isinstance(c.moderation_meta, dict) else None,
                sender_whitelisted=bool(ident is not None and ident in wl_keys),
                can_whitelist=ident is not None,
                whitelist_kind=ident[0] if ident else None,
                whitelist_label=_format_whitelist_label(c, users_by_id) if ident else None,
            )
        )

    whitelist_out = [
        ModerationWhitelistEntryDTO(
            id=w.id,
            kind=w.kind,
            key=w.key,
            label=w.label,
            created_at=format_iso_utc(w.created_at),
        )
        for w in whitelist_rows
    ]
    return ModerationQueueResponse(
        pending_count=len(comments_out),
        comments=comments_out,
        whitelist=whitelist_out,
    )


@router.post(
    "/moderation/comments/{comment_id}/decision",
    response_model=ModerationDecisionResponse,
)
async def decide_moderation_comment(
    comment_id: int,
    payload: ModerationDecisionRequest,
    request: Request,
    db: DbSessionDep,
    settings: SettingsDep,
) -> ModerationDecisionResponse:
    owner = await _require_owner(request, db)
    comment = await db.get(Comment, comment_id)
    if comment is None or comment.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Comment not found")

    now = datetime.now(UTC)
    meta = dict(comment.moderation_meta) if isinstance(comment.moderation_meta, dict) else {}
    meta["reviewed_by_user_id"] = owner.id
    meta["reviewed_at"] = format_iso_utc(now)
    whitelisted = False

    if payload.action == "approve":
        was_pending = comment.moderation_status != "approved"
        comment.moderation_status = "approved"
        comment.moderation_meta = meta

        if payload.whitelist_sender:
            ident = resolve_sender_whitelist_identity(comment)
            if ident is not None:
                users_by_id: dict[int, User] = {}
                if comment.user_id is not None:
                    u = await db.get(User, comment.user_id)
                    if u is not None:
                        users_by_id[u.id] = u
                await upsert_moderation_whitelist(
                    db,
                    kind=ident[0],
                    key=ident[1],
                    label=_format_whitelist_label(comment, users_by_id),
                    created_by_user_id=owner.id,
                )
                whitelisted = True

        parent_comment = (
            await db.get(Comment, comment.parent_id)
            if comment.parent_id is not None
            else None
        )
        if was_pending:
            await _enqueue_parent_reply_notification(
                db,
                parent_comment=parent_comment,
                comment=comment,
                now=now,
                settings=settings,
            )

        await db.commit()

        if was_pending:
            users_by_id_pub: dict[int, User] = {}
            if comment.user_id is not None:
                u = (
                    await db.execute(
                        select(User)
                        .options(selectinload(User.identities))
                        .where(User.id == comment.user_id)
                    )
                ).scalar_one_or_none()
                if u is not None:
                    users_by_id_pub[u.id] = u
            public_dto = _serialize_comment(
                comment,
                users_by_id=users_by_id_pub,
                me=None,
                settings=settings,
                include_reactions=False,
            )
            _get_sse_bus(request).publish(
                "comment.created",
                public_dto.model_dump(),
                post_id=comment.post_id,
            )
            _schedule_tg_bridge(request, comment.id)
    else:
        comment.moderation_status = "rejected"
        comment.moderation_meta = meta
        comment.deleted_at = now
        await db.commit()
        if comment.tg_msg_id is not None and comment.channel_id is not None:
            _schedule_tg_delete(request, comment.id)

    return ModerationDecisionResponse(
        ok=True,
        comment_id=comment.id,
        post_id=comment.post_id,
        moderation_status=comment.moderation_status,
        whitelisted=whitelisted,
    )


@router.delete("/moderation/whitelist/{entry_id}")
async def remove_moderation_whitelist_entry(
    entry_id: int,
    request: Request,
    db: DbSessionDep,
) -> dict[str, Any]:
    await _require_owner(request, db)
    row = await db.get(ModerationWhitelist, entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Whitelist entry not found")
    await db.delete(row)
    await db.commit()
    return {"ok": True, "id": entry_id}
