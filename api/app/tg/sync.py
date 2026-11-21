from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.db.models import Comment, CommentTgReaction, Poll, PollOption, Post, TgReaction
from app.db.repo import (
    compute_merged_comment_reactions,
    compute_merged_poll,
    compute_merged_reactions,
    find_post_by_channel_tg_msg,
    get_active_channel,
    get_post_by_id,
    make_reaction_key,
    serialize_post,
)
from app.exports import generate_exports
from app.routes.upload import SSEPublishFn, invoke_sse_publish
from app.tg.client import (
    TelegramService,
    _extract_poll_dict,
    _extract_reactions_list,
    _render_entities_to_html,
)

logger = logging.getLogger(__name__)

try:
    from pyrogram.errors import FloodWait as _PyroFloodWait
except Exception:
    _PyroFloodWait = None  # type: ignore[assignment]

try:
    from tgc.pyro.crawl import (  # type: ignore[import-not-found]
        ChannelBannedError,
        check_chat_banned,
    )
except Exception:

    class ChannelBannedError(Exception):
        """Raised when a Telegram channel is banned, restricted, deactivated, or inaccessible."""

    def check_chat_banned(chat: Any) -> str | None:
        is_restricted = bool(getattr(chat, "is_restricted", False))
        is_banned = bool(getattr(chat, "is_banned", False))
        is_deactivated = bool(getattr(chat, "is_deactivated", False))
        restrictions = getattr(chat, "restrictions", None) or []

        if is_restricted or is_banned or is_deactivated or restrictions:
            terms: list[str] = []
            if is_banned:
                terms.append("banned")
            if is_restricted:
                terms.append("restricted")
            if is_deactivated:
                terms.append("deactivated")
            status = "/".join(terms) if terms else "banned or restricted"
            title = getattr(chat, "title", None) or getattr(chat, "id", "unknown")
            return f"Chat '{title}' is {status} by Telegram!"
        return None


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _is_flood_wait(exc: Exception) -> float | None:
    if _PyroFloodWait is not None and isinstance(exc, _PyroFloodWait):
        val = getattr(exc, "value", None)
        if isinstance(val, (int, float)):
            return float(val)
    if type(exc).__name__ == "FloodWait":
        val = getattr(exc, "value", None)
        if isinstance(val, (int, float)):
            return float(val)
    return None


class PeriodicReconciler:
    """15-minute overlap safety-net reconciler for Epoch 2 channel views, edits, reactions, polls, and deletions."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        client: Any | None = None,
        telegram_service: TelegramService | None = None,
        sse_publish: SSEPublishFn | None = None,
        overlap_posts: int = 25,
        batch_size: int = 200,
        max_batches: int = 25,
        max_flood_wait_retry_seconds: float = 10.0,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.client = client
        self.telegram_service = telegram_service
        self.sse_publish = sse_publish
        self.overlap_posts = max(1, int(overlap_posts))
        self.batch_size = max(1, int(batch_size))
        self.max_batches = max(1, int(max_batches))
        self.max_flood_wait_retry_seconds = max(0.0, float(max_flood_wait_retry_seconds))
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._lock = asyncio.Lock()

    def _get_client(self) -> Any | None:
        if self.client is not None:
            return self.client
        if self.telegram_service is not None:
            return self.telegram_service.user_client or self.telegram_service.client
        return None

    async def _emit_sse(self, event: str, data: dict[str, Any]) -> None:
        publish = self.sse_publish or (
            self.telegram_service.sse_publish if self.telegram_service is not None else None
        )
        await invoke_sse_publish(publish, event, data)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop(), name="tg-periodic-reconciler")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        interval = max(1, int(self.settings.RECONCILE_INTERVAL_SECONDS))
        # Run a quick catch-up reconciliation shortly after startup before entering the periodic sleep
        first_timeout = min(5, interval)
        first_pass = True
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=first_timeout if first_pass else interval,
                )
                break
            except TimeoutError:
                pass
            first_pass = False

            try:
                stats = await self.reconcile_once()
                logger.info("Periodic reconciliation completed: %s", stats)
            except ChannelBannedError as exc:
                logger.error("Periodic reconciliation aborted (channel ban guard): %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Periodic reconciliation iteration failed")

    async def _fetch_batch_safe(
        self,
        tg_client: Any,
        chat_id: int,
        id_range: list[int],
    ) -> list[Any]:
        """Fetch one batch of message IDs, retrying short FloodWaits and wrapping errors as ChannelBannedError."""
        for attempt in range(2):
            try:
                batch = await tg_client.get_messages(chat_id, id_range)
                if batch is None:
                    return []
                return batch if isinstance(batch, list) else [batch]
            except Exception as exc:
                wait_sec = _is_flood_wait(exc)
                if (
                    wait_sec is not None
                    and attempt == 0
                    and wait_sec <= self.max_flood_wait_retry_seconds
                ):
                    logger.warning(
                        "FloodWait (%ss) fetching batch %s..%s; retrying once",
                        wait_sec,
                        id_range[0],
                        id_range[-1],
                    )
                    await asyncio.sleep(wait_sec)
                    continue
                raise ChannelBannedError(
                    f"Failed fetching message batch {id_range[0]}..{id_range[-1]} "
                    f"for channel {chat_id}: {exc}"
                ) from exc
        return []

    async def reconcile_once(self) -> dict[str, Any]:
        """Run a single overlap reconciliation pass against the active Epoch 2 channel.

        Serialized via `self._lock`. Raises `ChannelBannedError` if the channel is
        banned/restricted/inaccessible, returns 0 messages when existing posts are expected,
        or trips the >50% mass-deletion circuit breaker — never wiping existing data.
        """
        async with self._lock:
            return await self._reconcile_once_locked()

    async def _reconcile_once_locked(self) -> dict[str, Any]:
        tg_client = self._get_client()
        if tg_client is None:
            return {"status": "skipped", "reason": "no_client"}

        async with self.session_factory() as session:
            channel = await get_active_channel(session)
            if channel is None or channel.tg_chat_id is None:
                return {"status": "skipped", "reason": "no_active_channel"}

            chat_id = int(channel.tg_chat_id)
            channel_id = channel.id
            disc_chat_id = (
                int(channel.tg_discussion_chat_id)
                if channel.tg_discussion_chat_id is not None
                else None
            )

            # 1. Channel banned / restricted guard
            try:
                chat = await tg_client.get_chat(chat_id)
            except Exception as exc:
                raise ChannelBannedError(
                    f"Failed to access active channel {chat_id}: {exc}"
                ) from exc

            if chat is None:
                raise ChannelBannedError(f"Active channel {chat_id} returned None")

            banned_reason = check_chat_banned(chat)
            if banned_reason:
                raise ChannelBannedError(banned_reason)

            # 2. Compute overlap window from recent non-deleted posts in active channel
            overlap_stmt = (
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
                    Post.is_deleted.is_(False),
                    Post.tg_msg_id.is_not(None),
                )
                .order_by(Post.tg_msg_id.desc())
                .limit(self.overlap_posts)
            )
            overlap_posts = list((await session.execute(overlap_stmt)).scalars().all())

            if overlap_posts:
                candidate_ids: list[int] = []
                for p in overlap_posts:
                    if p.tg_msg_id is not None:
                        candidate_ids.append(int(p.tg_msg_id))
                    for mid in p.tg_all_msg_ids or []:
                        candidate_ids.append(int(mid))
                start_id = max(1, min(candidate_ids))
            else:
                start_id = max(1, int(channel.last_crawled_msg_id) + 1)

            # 3. Fetch messages in bounded batches of `batch_size` (capped by `max_batches`)
            fetched_msgs: list[Any] = []
            curr_id = start_id
            highest_requested_id = start_id - 1
            known_max_id = max(
                int(channel.last_crawled_msg_id),
                max(
                    (int(p.tg_msg_id) for p in overlap_posts if p.tg_msg_id is not None),
                    default=0,
                ),
            )

            batches_fetched = 0
            while batches_fetched < self.max_batches:
                end_id = curr_id + self.batch_size
                id_range = list(range(curr_id, end_id))
                highest_requested_id = end_id - 1
                batch = await self._fetch_batch_safe(tg_client, chat_id, id_range)
                batches_fetched += 1

                non_empty = [m for m in batch if m is not None and not getattr(m, "empty", False)]
                fetched_msgs.extend(non_empty)

                if not non_empty:
                    if end_id - 1 >= known_max_id:
                        break
                    curr_id = end_id
                    continue

                max_batch_id = max(int(m.id) for m in non_empty)
                if max_batch_id >= known_max_id and (end_id - 1) - max_batch_id > 20:
                    break

                curr_id = end_id

            # 4. Empty-result safety guard: never wipe data if overlap window returned 0 messages!
            if overlap_posts and not fetched_msgs:
                raise ChannelBannedError(
                    f"Overlap reconcile returned 0 messages from ID #{start_id} for channel {chat_id}, "
                    f"but {len(overlap_posts)} existing posts were expected! Aborting to prevent data loss."
                )

            present_msg_ids: set[int] = {int(m.id) for m in fetched_msgs}
            max_seen_id = max(present_msg_ids, default=int(channel.last_crawled_msg_id))

            # 5. Deletion-ratio circuit breaker BEFORE applying any mutations!
            candidate_deleted_posts: list[Post] = []
            for p in overlap_posts:
                post_ids = {int(x) for x in (p.tg_all_msg_ids or [])}
                if p.tg_msg_id is not None:
                    post_ids.add(int(p.tg_msg_id))
                if not post_ids:
                    continue
                if min(post_ids) >= start_id and max(post_ids) <= highest_requested_id:
                    if not (post_ids & present_msg_ids):
                        candidate_deleted_posts.append(p)

            if len(overlap_posts) >= 4 and (len(candidate_deleted_posts) / len(overlap_posts)) > 0.50:
                raise ChannelBannedError(
                    f"Mass-deletion circuit breaker tripped for channel {chat_id}: "
                    f"{len(candidate_deleted_posts)}/{len(overlap_posts)} overlap posts missing! "
                    f"Aborting without deleting any posts."
                )

            # 6. Group fetched messages by existing Post (so multi-photo albums reconcile as a unit!)
            existing_by_tg_id: dict[int, Post] = {}
            for p in overlap_posts:
                if p.tg_msg_id is not None:
                    existing_by_tg_id[int(p.tg_msg_id)] = p
                for mid in p.tg_all_msg_ids or []:
                    existing_by_tg_id[int(mid)] = p

            grouped_existing_msgs: dict[int, tuple[Post, list[Any]]] = {}
            new_msgs: list[Any] = []

            for msg in fetched_msgs:
                mid = int(msg.id)
                existing_post = existing_by_tg_id.get(mid)
                if existing_post is None:
                    existing_post = await find_post_by_channel_tg_msg(session, channel_id, mid)

                if existing_post is None:
                    new_msgs.append(msg)
                    continue

                entry = grouped_existing_msgs.get(existing_post.id)
                if entry is None:
                    grouped_existing_msgs[existing_post.id] = (existing_post, [msg])
                else:
                    entry[1].append(msg)

            updated_post_ids: set[int] = set()
            reaction_changed_ids: set[int] = set()
            poll_changed_ids: set[int] = set()
            deleted_post_ids: set[int] = set()
            exports_dirty = False

            # 7. Reconcile each existing post from its grouped album/single messages
            for existing_post, post_msgs in grouped_existing_msgs.values():
                post_msgs.sort(key=lambda m: int(m.id))
                primary_msg = next(
                    (
                        m
                        for m in post_msgs
                        if existing_post.tg_msg_id is not None
                        and int(m.id) == int(existing_post.tg_msg_id)
                    ),
                    post_msgs[0],
                )
                changed = False

                # Views & forwards: max non-None across album siblings
                view_vals = [int(m.views) for m in post_msgs if getattr(m, "views", None) is not None]
                if view_vals:
                    best_views = max(view_vals)
                    if existing_post.views != best_views:
                        existing_post.views = best_views
                        changed = True

                fwd_vals = [
                    int(m.forwards) for m in post_msgs if getattr(m, "forwards", None) is not None
                ]
                if fwd_vals:
                    best_forwards = max(fwd_vals)
                    if existing_post.forwards != best_forwards:
                        existing_post.forwards = best_forwards
                        changed = True

                # Only overwrite text_html if edit_date actually changed (prevents clobbering rich/tgc HTML)
                edit_dates = [
                    ed
                    for m in post_msgs
                    if (ed := _ensure_utc(getattr(m, "edit_date", None))) is not None
                ]
                latest_edit = max(edit_dates) if edit_dates else None
                edit_date_changed = latest_edit is not None and latest_edit != existing_post.edited_at

                if edit_date_changed:
                    existing_post.edited_at = latest_edit
                    changed = True

                caption_msg = next(
                    (
                        m
                        for m in post_msgs
                        if getattr(m, "text", None) is not None
                        or getattr(m, "caption", None) is not None
                    ),
                    None,
                )
                if caption_msg is not None and (edit_date_changed or existing_post.text_html is None):
                    rendered: str | None = None
                    if self.telegram_service is not None:
                        try:
                            conv = await self.telegram_service._convert_channel_messages(
                                [caption_msg], channel
                            )
                            if conv and conv[0].get("text") is not None:
                                rendered = str(conv[0]["text"])
                        except Exception:
                            logger.debug(
                                "Fallback HTML render for reconciled msg %s",
                                caption_msg.id,
                                exc_info=True,
                            )
                    if rendered is None:
                        raw_t = getattr(caption_msg, "text", None)
                        raw_c = getattr(caption_msg, "caption", None)
                        if raw_t is not None:
                            rendered = _render_entities_to_html(
                                str(raw_t), getattr(caption_msg, "entities", None)
                            )
                        elif raw_c is not None:
                            rendered = _render_entities_to_html(
                                str(raw_c), getattr(caption_msg, "caption_entities", None)
                            )
                    if rendered is not None and rendered != existing_post.text_html:
                        existing_post.text_html = rendered
                        changed = True

                # Reconcile TG reactions safely across album siblings:
                # Prefer primary message reactions; if primary has None/empty while a sibling has non-empty,
                # preserve/use the sibling's reactions. Never let a reaction-less sibling wipe primary reactions!
                primary_reactions = _extract_reactions_list(primary_msg)
                effective_reactions: list[dict[str, Any]] | None = None
                if primary_reactions:
                    effective_reactions = primary_reactions
                else:
                    sibling_non_empty = [
                        r_list
                        for m in post_msgs
                        if m is not primary_msg and (r_list := _extract_reactions_list(m))
                    ]
                    if sibling_non_empty:
                        effective_reactions = sibling_non_empty[0]
                    elif primary_reactions is not None:
                        # Primary explicitly reported empty reaction list and no sibling has reactions
                        effective_reactions = []

                if effective_reactions is not None:
                    existing_map = {r.reaction_key: int(r.count) for r in existing_post.tg_reactions}
                    incoming_map: dict[str, tuple[str | None, str | None, int]] = {}
                    for r in effective_reactions:
                        rkey = make_reaction_key(
                            emoji=r.get("emoji"), custom_emoji_id=r.get("custom_emoji_id")
                        )
                        incoming_map[rkey] = (
                            r.get("emoji"),
                            r.get("custom_emoji_id"),
                            int(r.get("count") or 0),
                        )
                    if {k: v[2] for k, v in incoming_map.items()} != existing_map:
                        await session.execute(
                            delete(TgReaction).where(TgReaction.post_id == existing_post.id)
                        )
                        for rkey, (emoji, cid, cnt) in incoming_map.items():
                            session.add(
                                TgReaction(
                                    post_id=existing_post.id,
                                    reaction_key=rkey,
                                    emoji=emoji,
                                    custom_emoji_id=cid,
                                    count=cnt,
                                )
                            )
                        reaction_changed_ids.add(existing_post.id)
                        changed = True

                # Reconcile poll state from whichever message in the group carries a poll
                poll_dict = next(
                    (pd for m in post_msgs if (pd := _extract_poll_dict(m)) is not None),
                    None,
                )
                if poll_dict is not None and existing_post.poll is not None:
                    poll_changed = False
                    if existing_post.poll.is_closed != bool(poll_dict["is_closed"]):
                        existing_post.poll.is_closed = bool(poll_dict["is_closed"])
                        poll_changed = True
                    for opt in poll_dict.get("options") or []:
                        opt_idx = int(opt["option_idx"])
                        opt_row = await session.get(PollOption, (existing_post.poll.id, opt_idx))
                        if opt_row is not None and opt_row.tg_voter_count != int(
                            opt["tg_voter_count"]
                        ):
                            opt_row.tg_voter_count = int(opt["tg_voter_count"])
                            poll_changed = True
                    if poll_changed:
                        poll_changed_ids.add(existing_post.id)
                        changed = True

                if changed:
                    updated_post_ids.add(existing_post.id)
                    exports_dirty = True

            # 8. Apply confirmed deletions (passed circuit breaker)
            for p in candidate_deleted_posts:
                p.is_deleted = True
                deleted_post_ids.add(p.id)
                exports_dirty = True

            await session.commit()
            session.expire_all()

            # 9. Emit SSE updates: full Post on `post.updated`, dedicated `post.deleted` for deletions
            for pid in sorted(updated_post_ids):
                refreshed = await get_post_by_id(session, pid, include_deleted=False)
                if refreshed is None:
                    continue
                await self._emit_sse(
                    "post.updated",
                    serialize_post(refreshed, api_prefix=self.settings.API_PREFIX),
                )
                if pid in reaction_changed_ids:
                    merged_r = compute_merged_reactions(refreshed)
                    await self._emit_sse(
                        "reaction.updated",
                        {
                            "post_id": refreshed.id,
                            "reactions": [
                                {k: v for k, v in item.items() if k != "reacted"}
                                for item in merged_r
                            ],
                        },
                    )
                if pid in poll_changed_ids and refreshed.poll is not None:
                    merged_p = compute_merged_poll(refreshed.poll)
                    await self._emit_sse(
                        "poll.updated",
                        {
                            "post_id": refreshed.id,
                            "poll": {k: v for k, v in merged_p.items() if k != "my_votes"},
                        },
                    )

            for pid in sorted(deleted_post_ids):
                await self._emit_sse("post.deleted", {"post_id": pid})

            # 10. Ingest newly discovered messages missed during downtime BEFORE advancing last_crawled_msg_id
            new_post_count = 0
            if new_msgs:
                svc = self.telegram_service or TelegramService(
                    session_factory=self.session_factory,
                    settings=self.settings,
                    sse_publish=self.sse_publish,
                    client=tg_client,
                )
                ingested = await svc._ingest_channel_messages(
                    new_msgs,
                    is_edit=False,
                    trigger_prompt=True,
                )
                new_post_count = len(ingested)
                exports_dirty = False

            # Advance last_crawled_msg_id ONLY after new_msgs ingestion has succeeded!
            ch_reload = await get_active_channel(session)
            if ch_reload is not None and max_seen_id > ch_reload.last_crawled_msg_id:
                ch_reload.last_crawled_msg_id = max_seen_id
                await session.commit()

            if exports_dirty:
                await generate_exports(session, self.settings)

            updated_comment_reactions = await self._reconcile_comment_reactions(
                session,
                tg_client=tg_client,
                channel_id=channel_id,
                disc_chat_id=disc_chat_id,
            )

            return {
                "status": "ok",
                "checked_messages": len(fetched_msgs),
                "updated_posts": len(updated_post_ids),
                "updated_comment_reactions": updated_comment_reactions,
                "new_posts": new_post_count,
                "deleted_posts": len(deleted_post_ids),
            }

    async def _reconcile_comment_reactions(
        self,
        session: AsyncSession,
        *,
        tg_client: Any,
        channel_id: int,
        disc_chat_id: int | None,
    ) -> int:
        if disc_chat_id is None or not hasattr(tg_client, "get_messages"):
            return 0

        recent_comments = list(
            (
                await session.execute(
                    select(Comment)
                    .options(selectinload(Comment.tg_reactions))
                    .join(Post, Post.id == Comment.post_id)
                    .where(
                        Comment.channel_id == channel_id,
                        Comment.tg_msg_id.is_not(None),
                        Comment.deleted_at.is_(None),
                        Post.is_deleted.is_(False),
                    )
                    .order_by(Comment.tg_msg_id.desc())
                    .limit(self.overlap_posts)
                )
            )
            .scalars()
            .all()
        )
        if not recent_comments:
            return 0

        by_tg_id = {int(c.tg_msg_id): c for c in recent_comments if c.tg_msg_id is not None}
        tg_ids = sorted(by_tg_id)
        is_user_client = (
            self.telegram_service is not None
            and tg_client is self.telegram_service.user_client
        )

        fetched_msgs: list[Any] = []
        for idx in range(0, len(tg_ids), self.batch_size):
            chunk = tg_ids[idx : idx + self.batch_size]
            try:
                batch = await tg_client.get_messages(disc_chat_id, chunk)
            except Exception:
                logger.debug(
                    "Discussion reaction reconcile skipped for chat %s batch %s",
                    disc_chat_id,
                    chunk,
                    exc_info=True,
                )
                return 0
            if isinstance(batch, list):
                fetched_msgs.extend(batch)
            elif batch is not None:
                fetched_msgs.append(batch)

        changed_comment_ids: list[int] = []
        for msg in fetched_msgs:
            if msg is None or getattr(msg, "empty", False):
                continue
            mid = getattr(msg, "id", None)
            if mid is None:
                continue
            comment = by_tg_id.get(int(mid))
            if comment is None:
                continue

            extracted = _extract_reactions_list(msg)
            if extracted is None:
                if not is_user_client:
                    continue
                extracted = []

            existing_map = {r.reaction_key: int(r.count) for r in comment.tg_reactions}
            incoming_map: dict[str, tuple[str | None, str | None, int]] = {}
            for r in extracted:
                rkey = make_reaction_key(
                    emoji=r.get("emoji"), custom_emoji_id=r.get("custom_emoji_id")
                )
                incoming_map[rkey] = (
                    r.get("emoji"),
                    r.get("custom_emoji_id"),
                    int(r.get("count") or 0),
                )
            if {k: v[2] for k, v in incoming_map.items()} == existing_map:
                continue

            await session.execute(
                delete(CommentTgReaction).where(CommentTgReaction.comment_id == comment.id)
            )
            for rkey, (emoji, cid, cnt) in incoming_map.items():
                session.add(
                    CommentTgReaction(
                        comment_id=comment.id,
                        reaction_key=rkey,
                        emoji=emoji,
                        custom_emoji_id=cid,
                        count=cnt,
                    )
                )
            changed_comment_ids.append(comment.id)

        if not changed_comment_ids:
            return 0

        await session.commit()
        session.expire_all()

        for cid in changed_comment_ids:
            refreshed = (
                await session.execute(
                    select(Comment)
                    .options(
                        selectinload(Comment.tg_reactions),
                        selectinload(Comment.web_reactions),
                    )
                    .where(Comment.id == cid, Comment.deleted_at.is_(None))
                )
            ).scalar_one_or_none()
            if refreshed is None:
                continue
            merged = compute_merged_comment_reactions(refreshed)
            public_reactions = [
                {k: v for k, v in item.items() if k != "reacted"} for item in merged
            ]
            await self._emit_sse(
                "comment.reaction.updated",
                {
                    "post_id": refreshed.post_id,
                    "comment_id": refreshed.id,
                    "reactions": public_reactions,
                },
            )

        return len(changed_comment_ids)
