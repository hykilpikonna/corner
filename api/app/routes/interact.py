from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.captcha import verify_altcha_payload
from app.auth.security import (
    VISITOR_COOKIE_NAME,
    SlidingWindowRateLimiter,
    ensure_visitor_id,
    get_client_ip,
    get_current_user_optional,
    make_anon_tag,
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
    NotificationQueue,
    Poll,
    Post,
    TgReaction,
    User,
    WebPollVote,
    WebReaction,
)
from app.db.repo import (
    compute_merged_comment_reactions,
    compute_merged_poll,
    compute_merged_reactions,
    format_iso_utc,
    make_reaction_key,
    resolve_media_url,
    strip_variation_selectors,
)
from app.db.session import get_db_session
from app.routes.auth import serialize_user_dto
from app.schemas import (
    CommentAuthorDTO,
    CommentDTO,
    CommentReactionToggleResponse,
    CommentsListResponse,
    CreateCommentRequest,
    DeleteCommentResponse,
    InteractionsResponse,
    MergedPollDTO,
    MergedPollOptionDTO,
    MergedReactionDTO,
    PollVoteRequest,
    PollVoteResponse,
    PostInteractionDTO,
    ReactionToggleRequest,
    ReactionToggleResponse,
)
from app.sse import SSEBus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["interactions"])

DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

MAX_BATCH_IDS = 200
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
    stripped_target = strip_variation_selectors(emoji)
    tg_fk = getattr(tg_model, fk_col_name)
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
            return existing_emoji

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


def _serialize_comment(
    comment: Comment,
    *,
    users_by_id: dict[int, User],
    me: User | None,
    settings: Settings,
    tombstone: bool = False,
    viewer_anon_tags: set[str] | None = None,
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
            display_name=comment.anon_tag or "Anonymous",
            avatar_url=None,
            role="anonymous",
            provider=None,
            username=None,
            verified=False,
        )

    is_mine_user = bool(
        me is not None and comment.user_id is not None and comment.user_id == me.id
    )
    is_mine_anon = bool(
        comment.user_id is None
        and comment.source == "web"
        and viewer_anon_tags
        and comment.anon_tag is not None
        and comment.anon_tag in viewer_anon_tags
    )
    is_mine = is_mine_user or is_mine_anon
    can_delete = bool(me is not None and (me.role == "owner" or is_mine_user))

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
        media=comment.media,
        created_at=format_iso_utc(comment.created_at),
        edited_at=format_iso_utc(comment.edited_at) if comment.edited_at else None,
        deleted=False,
        is_mine=is_mine,
        can_delete=can_delete,
        reactions=reactions,
    )


def _schedule_tg_bridge(request: Request, comment_id: int) -> None:
    tg_bridge = getattr(request.app.state, "tg_bridge", None)
    if tg_bridge is None:
        return

    async def _run() -> None:
        try:
            if callable(tg_bridge):
                res = tg_bridge(comment_id)
                if inspect.isawaitable(res):
                    await res
            elif hasattr(tg_bridge, "bridge_web_comment"):
                res = tg_bridge.bridge_web_comment(comment_id)
                if inspect.isawaitable(res):
                    await res
        except Exception:
            logger.exception("tg_bridge callback failed for comment_id=%s", comment_id)

    task = asyncio.create_task(_run(), name=f"tg-bridge-comment-{comment_id}")
    tasks: set[asyncio.Task[Any]] | None = getattr(
        request.app.state, "background_tasks", None
    )
    if tasks is not None:
        tasks.add(task)
        task.add_done_callback(tasks.discard)


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
            .where(Comment.post_id.in_(post_ids), Comment.deleted_at.is_(None))
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
            )

    return InteractionsResponse(
        allow_anon_comments=settings.ALLOW_ANON_COMMENTS,
        captcha_provider="altcha",
        me=serialize_user_dto(me) if me is not None else None,
        posts=posts_payload,
    )


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
    viewer_anon_tags: set[str] = set()
    if me is not None:
        viewer_anon_tags.add(make_anon_tag(post_id, f"u:{me.id}", settings.SECRET_KEY))
    vid = verify_visitor_cookie(
        request.cookies.get(VISITOR_COOKIE_NAME), settings.SECRET_KEY
    )
    if vid is not None:
        viewer_anon_tags.add(make_anon_tag(post_id, f"v:{vid}", settings.SECRET_KEY))

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

    # Iterative O(N) upward walk from live comments to mark ancestors that need tombstones
    by_id = {c.id: c for c in all_comments}
    has_live_descendant: set[int] = set()
    for c in all_comments:
        if c.deleted_at is not None:
            continue
        curr_parent = c.parent_id
        while curr_parent is not None and curr_parent not in has_live_descendant:
            has_live_descendant.add(curr_parent)
            parent_obj = by_id.get(curr_parent)
            curr_parent = parent_obj.parent_id if parent_obj is not None else None

    visible_comments: list[tuple[Comment, bool]] = []
    for c in all_comments:
        if c.deleted_at is None:
            visible_comments.append((c, False))
        elif c.id in has_live_descendant:
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
            actor_key=actor_key,
        )
        for c, is_tomb in visible_comments
    ]
    return CommentsListResponse(post_id=post_id, comments=serialized)


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
    if not stripped_body or len(stripped_body) > MAX_COMMENT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Comment body must be between 1 and {MAX_COMMENT_LENGTH} characters",
        )

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

    body_html = render_and_sanitize_comment_html(stripped_body)
    now = datetime.now(UTC)

    comment = Comment(
        post_id=post_id,
        parent_id=payload.parent_id,
        source="web",
        channel_id=post.channel_id,
        user_id=comment_user_id,
        anon_tag=anon_tag,
        body_text=stripped_body,
        body_html=body_html,
        created_at=now,
    )
    db.add(comment)
    await db.flush()

    if (
        parent_comment is not None
        and parent_comment.user_id is not None
        and parent_comment.user_id != comment_user_id
    ):
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
                    post_id=post_id,
                    send_after=now + timedelta(seconds=settings.EMAIL_DEBOUNCE_SECONDS),
                )
            )

    await db.commit()

    users_by_id = {me.id: me} if (me is not None and comment_user_id == me.id) else {}
    # Pass `me=me` so owner retains `can_delete=True` even when posting anonymously,
    # and pass `viewer_anon_tags={anon_tag}` so anonymous comments return `is_mine=True`.
    caller_dto = _serialize_comment(
        comment,
        users_by_id=users_by_id,
        me=me,
        settings=settings,
        viewer_anon_tags={anon_tag} if anon_tag else None,
        include_reactions=False,
    )
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

    return caller_dto


@router.delete("/comments/{comment_id}", response_model=DeleteCommentResponse)
async def delete_comment(
    comment_id: int,
    request: Request,
    db: DbSessionDep,
) -> DeleteCommentResponse:
    me = await get_current_user_optional(request, db)
    if me is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    comment = await db.get(Comment, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="Comment not found")

    if me.role != "owner" and comment.user_id != me.id:
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

    return DeleteCommentResponse(
        ok=True,
        post_id=comment.post_id,
        comment_id=comment.id,
    )
