from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import SESSION_COOKIE_NAME, VISITOR_COOKIE_NAME, create_session
from app.config import Settings, get_settings
from app.db.models import (
    Comment,
    CommentTgReaction,
    Post,
    User,
    UserIdentity,
)
from app.db.repo import (
    default_allowed_reactions,
    ensure_channel,
    set_allowed_reactions,
    upsert_post,
)
from app.db.session import create_engine_and_factory, get_db_session
from app.main import create_app
from app.routes.interact import MAX_WEB_REACTIONS_PER_POST_ACTOR
from app.sse import SSEBus
from app.tg.client import TelegramService


async def _setup_app_and_client(
    settings: Settings, db_session: AsyncSession
) -> tuple[Any, AsyncClient]:
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    settings.exports_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(settings)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = _override_session

    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://aza.moe",
    )
    return app, client


async def _seed_user_session(
    db_session: AsyncSession,
    *,
    display_name: str,
    email: str,
    role: str = "user",
) -> tuple[User, str]:
    user = User(
        role=role,
        display_name=display_name,
        email=email,
        email_verified=True,
        notify_replies=True,
    )
    db_session.add(user)
    await db_session.flush()
    db_session.add(
        UserIdentity(
            provider="github",
            provider_user_id=f"github-{user.id}",
            user_id=user.id,
            username=display_name.lower(),
        )
    )
    token = await create_session(db_session, user.id)
    await db_session.commit()
    return user, token


@pytest.mark.asyncio
async def test_comment_reactions_toggle_merge_list_and_sse_broadcast(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    ch = await ensure_channel(db_session, channel_id=1, status="active", id_offset=0)
    await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=10,
        canonical_id=10,
        date=datetime(2026, 11, 19, 8, 0, tzinfo=UTC),
        text_html="Post 10",
    )
    alice, alice_token = await _seed_user_session(
        db_session,
        display_name="Alice",
        email="alice@example.com",
    )

    now = datetime(2026, 11, 19, 8, 5, tzinfo=UTC)
    deleted_parent = Comment(
        post_id=10,
        source="web",
        user_id=alice.id,
        body_text="Parent to tombstone",
        body_html="<p>Parent to tombstone</p>",
        created_at=now,
        deleted_at=now,
    )
    db_session.add(deleted_parent)
    await db_session.flush()

    live_comment = Comment(
        post_id=10,
        parent_id=deleted_parent.id,
        source="telegram",
        channel_id=ch.id,
        tg_msg_id=501,
        tg_author_id=4242,
        tg_author_name="TG Bob",
        body_text="Nice post",
        body_html="<p>Nice post</p>",
        created_at=datetime(2026, 11, 19, 8, 6, tzinfo=UTC),
    )
    db_session.add(live_comment)
    await db_session.flush()

    # Bare TG heart (\u2764) without FE0F + reaction on deleted parent (must stay hidden on tombstone)
    db_session.add(
        CommentTgReaction(
            comment_id=live_comment.id,
            reaction_key="e:\u2764",
            emoji="\u2764",
            count=3,
        )
    )
    db_session.add(
        CommentTgReaction(
            comment_id=deleted_parent.id,
            reaction_key="e:🔥",
            emoji="🔥",
            count=2,
        )
    )
    await db_session.commit()

    app, client = await _setup_app_and_client(test_settings, db_session)
    sub_id, queue, _ = app.state.sse_bus.subscribe(post_ids={10})

    try:
        # 1. Anonymous visitor toggles reaction ON with "❤️" (\u2764\ufe0f) -> merges with TG "\u2764"
        async with client:
            r_list_initial = await client.get("/api/life/posts/10/comments")
            assert r_list_initial.status_code == 200
            comments = r_list_initial.json()["comments"]
            assert len(comments) == 2
            assert comments[0]["deleted"] is True
            assert comments[0]["reactions"] == []
            assert comments[1]["reactions"] == [
                {
                    "reaction_key": "e:\u2764",
                    "emoji": "\u2764",
                    "custom_emoji_id": None,
                    "tg_count": 3,
                    "web_count": 0,
                    "count": 3,
                    "reacted": False,
                }
            ]

            # Anonymous cookie toggle ON
            r_anon_on = await client.post(
                f"/api/life/comments/{live_comment.id}/reactions/toggle",
                json={"emoji": "❤️"},
            )
            assert r_anon_on.status_code == 200
            assert VISITOR_COOKIE_NAME in client.cookies
            anon_body = r_anon_on.json()
            assert anon_body["post_id"] == 10
            assert anon_body["comment_id"] == live_comment.id
            assert len(anon_body["reactions"]) == 1
            assert anon_body["reactions"][0]["tg_count"] == 3
            assert anon_body["reactions"][0]["web_count"] == 1
            assert anon_body["reactions"][0]["count"] == 4
            assert anon_body["reactions"][0]["reacted"] is True

            sse_ev = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert sse_ev.event == "comment.reaction.updated"
            assert sse_ev.post_id == 10
            assert sse_ev.data["post_id"] == 10
            assert sse_ev.data["comment_id"] == live_comment.id
            assert sse_ev.data["reactions"][0]["count"] == 4
            assert "reacted" not in sse_ev.data["reactions"][0]

            # GET /posts/{post_id}/comments reflects anon visitor's reacted=True
            r_list_after_anon = await client.get("/api/life/posts/10/comments")
            assert r_list_after_anon.status_code == 200
            assert r_list_after_anon.json()["comments"][1]["reactions"][0]["reacted"] is True

            # Anonymous visitor toggles OFF
            r_anon_off = await client.post(
                f"/api/life/comments/{live_comment.id}/reactions/toggle",
                json={"emoji": "❤️"},
            )
            assert r_anon_off.status_code == 200
            assert r_anon_off.json()["reactions"][0]["web_count"] == 0
            assert r_anon_off.json()["reactions"][0]["count"] == 3
            assert r_anon_off.json()["reactions"][0]["reacted"] is False
            await asyncio.wait_for(queue.get(), timeout=1.0)

        # 2. Verified user: unlisted custom emoji is rejected (400); once added to allowlist it succeeds
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://aza.moe",
        ) as auth_client:
            auth_client.cookies.set(SESSION_COOKIE_NAME, alice_token)

            r_disallowed = await auth_client.post(
                f"/api/life/comments/{live_comment.id}/reactions/toggle",
                json={"custom_emoji_id": "55512345"},
            )
            assert r_disallowed.status_code == 400

            # Existing CommentTgReaction on a comment can be toggled even if unlisted
            db_session.add(
                CommentTgReaction(
                    comment_id=live_comment.id,
                    reaction_key="c:777888999",
                    emoji=None,
                    custom_emoji_id="777888999",
                    count=2,
                )
            )
            await db_session.commit()

            r_tg_comment_custom = await auth_client.post(
                f"/api/life/comments/{live_comment.id}/reactions/toggle",
                json={"custom_emoji_id": "777888999"},
            )
            assert r_tg_comment_custom.status_code == 200
            tg_c_keys = {r["reaction_key"]: r for r in r_tg_comment_custom.json()["reactions"]}
            assert tg_c_keys["c:777888999"]["count"] == 3
            assert tg_c_keys["c:777888999"]["reacted"] is True
            await asyncio.wait_for(queue.get(), timeout=1.0)

            await set_allowed_reactions(
                db_session,
                [*default_allowed_reactions(), {"emoji": None, "custom_emoji_id": "55512345"}],
            )
            await db_session.commit()

            r_user_on = await auth_client.post(
                f"/api/life/comments/{live_comment.id}/reactions/toggle",
                json={"custom_emoji_id": "55512345"},
            )
            assert r_user_on.status_code == 200
            keys = {r["reaction_key"]: r for r in r_user_on.json()["reactions"]}
            assert keys["c:55512345"]["count"] == 1
            assert keys["c:55512345"]["web_count"] == 1
            assert keys["c:55512345"]["reacted"] is True
            assert keys["e:\u2764"]["reacted"] is False

            sse_ev2 = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert sse_ev2.event == "comment.reaction.updated"
            assert all("reacted" not in r for r in sse_ev2.data["reactions"])

            r_list_user = await auth_client.get("/api/life/posts/10/comments")
            assert r_list_user.status_code == 200
            list_keys = {
                r["reaction_key"]: r for r in r_list_user.json()["comments"][1]["reactions"]
            }
            assert list_keys["c:55512345"]["reacted"] is True

            # Creating a new comment emits comment.created with reactions: []
            r_create = await auth_client.post(
                "/api/life/posts/10/comments",
                json={"body": "Brand new comment"},
            )
            assert r_create.status_code == 201
            assert r_create.json()["reactions"] == []

            sse_created = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert sse_created.event == "comment.created"
            assert sse_created.data["reactions"] == []
    finally:
        app.state.sse_bus.unsubscribe(sub_id)


@pytest.mark.asyncio
async def test_comment_reactions_404_validation_cap_and_concurrency(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    ch = await ensure_channel(db_session, channel_id=1, status="active", id_offset=0)
    await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=20,
        canonical_id=20,
        date=datetime(2026, 11, 19, 9, 0, tzinfo=UTC),
        text_html="Post 20",
    )
    await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=21,
        canonical_id=21,
        date=datetime(2026, 11, 19, 9, 1, tzinfo=UTC),
        text_html="Deleted post 21",
        is_deleted=True,
    )
    _, alice_token = await _seed_user_session(
        db_session,
        display_name="Racer",
        email="racer@example.com",
    )

    now = datetime(2026, 11, 19, 9, 2, tzinfo=UTC)
    live_comment = Comment(
        post_id=20,
        source="web",
        anon_tag="Anonymous #1234",
        body_text="Live",
        body_html="<p>Live</p>",
        created_at=now,
    )
    deleted_comment = Comment(
        post_id=20,
        source="web",
        anon_tag="Anonymous #1234",
        body_text="Gone",
        body_html="<p>Gone</p>",
        created_at=now,
        deleted_at=now,
    )
    deleted_post_comment = Comment(
        post_id=21,
        source="web",
        anon_tag="Anonymous #1234",
        body_text="Orphan",
        body_html="<p>Orphan</p>",
        created_at=now,
    )
    db_session.add_all([live_comment, deleted_comment, deleted_post_comment])
    await db_session.commit()

    engine, factory = create_engine_and_factory(test_settings)
    test_settings.media_dir.mkdir(parents=True, exist_ok=True)
    test_settings.exports_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(test_settings)

    async def _per_req_session() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s

    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_db_session] = _per_req_session

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://aza.moe",
        ) as client:
            client.cookies.set(SESSION_COOKIE_NAME, alice_token)

            # Missing / deleted comment / deleted parent post -> 404
            for bad_id in (999999, deleted_comment.id, deleted_post_comment.id):
                r_404 = await client.post(
                    f"/api/life/comments/{bad_id}/reactions/toggle",
                    json={"emoji": "❤️"},
                )
                assert r_404.status_code == 404

            # Validation errors -> 400
            assert (
                await client.post(
                    f"/api/life/comments/{live_comment.id}/reactions/toggle",
                    json={},
                )
            ).status_code == 400
            assert (
                await client.post(
                    f"/api/life/comments/{live_comment.id}/reactions/toggle",
                    json={"emoji": "❤️", "custom_emoji_id": "123"},
                )
            ).status_code == 400
            assert (
                await client.post(
                    f"/api/life/comments/{live_comment.id}/reactions/toggle",
                    json={"emoji": "not-an-emoji"},
                )
            ).status_code == 400
            assert (
                await client.post(
                    f"/api/life/comments/{live_comment.id}/reactions/toggle",
                    json={"custom_emoji_id": "abc"},
                )
            ).status_code == 400

            # Unlisted valid emoji is rejected against global allowlist
            assert (
                await client.post(
                    f"/api/life/comments/{live_comment.id}/reactions/toggle",
                    json={"emoji": "🍆"},
                )
            ).status_code == 400

            # Distinct reactions cap -> 400 on overflow, toggle off still allowed
            emojis = ["❤️", "🔥", "👍", "👏", "✨"]
            assert len(emojis) == MAX_WEB_REACTIONS_PER_POST_ACTOR
            for emoji in emojis:
                r_ok = await client.post(
                    f"/api/life/comments/{live_comment.id}/reactions/toggle",
                    json={"emoji": emoji},
                )
                assert r_ok.status_code == 200

            r_over = await client.post(
                f"/api/life/comments/{live_comment.id}/reactions/toggle",
                json={"emoji": "🎉"},
            )
            assert r_over.status_code == 400

            r_off = await client.post(
                f"/api/life/comments/{live_comment.id}/reactions/toggle",
                json={"emoji": "✨"},
            )
            assert r_off.status_code == 200

            # Concurrent rapid toggles serialize cleanly without 500
            concurrent = await asyncio.gather(
                *[
                    client.post(
                        f"/api/life/comments/{live_comment.id}/reactions/toggle",
                        json={"emoji": "🎉"},
                    )
                    for _ in range(4)
                ]
            )
            assert all(r.status_code == 200 for r in concurrent)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_tg_service_comment_created_and_comment_reaction_count_update(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    tg_settings = test_settings.model_copy(
        update={
            "TG_CHANNEL_ID": -1002000002,
            "TG_DISCUSSION_CHAT_ID": -1003000003,
        }
    )
    ch = await ensure_channel(
        db_session,
        channel_id=1,
        tg_chat_id=tg_settings.TG_CHANNEL_ID,
        tg_discussion_chat_id=tg_settings.TG_DISCUSSION_CHAT_ID,
        status="active",
        id_offset=0,
    )
    post = await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=20,
        canonical_id=20,
        tg_discussion_root_id=9001,
        date=datetime(2026, 11, 19, 10, 0, tzinfo=UTC),
        text_html="Post with discussion",
    )
    await db_session.commit()

    engine, factory = create_engine_and_factory(tg_settings)
    sse_bus = SSEBus()
    sub_id, queue, _ = sse_bus.subscribe(post_ids={post.id})
    service = TelegramService(
        session_factory=factory,
        settings=tg_settings,
        sse_publish=sse_bus.publish,
        bot_id=999000,
    )

    try:
        # 1. Incoming TG discussion comment emits comment.created with reactions=[]
        disc_chat = SimpleNamespace(id=tg_settings.TG_DISCUSSION_CHAT_ID)
        tg_msg = SimpleNamespace(
            id=8050,
            chat=disc_chat,
            reply_to_message_id=9001,
            reply_to_top_message_id=9001,
            from_user=SimpleNamespace(id=111222, first_name="Cara", username="cara"),
            text="Hello from TG",
            date=datetime(2026, 11, 19, 10, 1, tzinfo=UTC),
        )
        created_comment = await service.handle_discussion_message(tg_msg)
        assert created_comment is not None

        ev_created = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert ev_created.event == "comment.created"
        assert ev_created.data["id"] == created_comment.id
        assert ev_created.data["reactions"] == []

        # 2. Reaction count update in discussion group updates CommentTgReaction & emits comment.reaction.updated
        await service.handle_reaction_count_update(
            SimpleNamespace(
                chat=disc_chat,
                message_id=8050,
                reactions=[SimpleNamespace(emoji="👍", custom_emoji_id=None, count=4)],
            )
        )
        ev_react = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert ev_react.event == "comment.reaction.updated"
        assert ev_react.data["post_id"] == post.id
        assert ev_react.data["comment_id"] == created_comment.id
        assert ev_react.data["reactions"] == [
            {
                "reaction_key": "e:👍",
                "emoji": "👍",
                "custom_emoji_id": None,
                "tg_count": 4,
                "web_count": 0,
                "count": 4,
            }
        ]

        # 3. Unknown/service message on channel chat_id with colliding msg_id=8050 MUST NOT mutate comment!
        await service.handle_reaction_count_update(
            SimpleNamespace(
                chat=SimpleNamespace(id=tg_settings.TG_CHANNEL_ID),
                message_id=8050,
                reactions=[SimpleNamespace(emoji="🔥", custom_emoji_id=None, count=2)],
            )
        )
        assert queue.empty()

        # 4. Subsequent update on discussion chat_id replaces CommentTgReaction cleanly
        await service.handle_reaction_count_update(
            SimpleNamespace(
                chat=disc_chat,
                message_id=8050,
                reactions=[SimpleNamespace(emoji="🔥", custom_emoji_id=None, count=2)],
            )
        )
        ev_react_2 = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert ev_react_2.event == "comment.reaction.updated"
        assert ev_react_2.data["reactions"][0]["reaction_key"] == "e:🔥"
        assert ev_react_2.data["reactions"][0]["tg_count"] == 2

        async with factory() as s:
            rows = (
                await s.execute(
                    select(CommentTgReaction).where(
                        CommentTgReaction.comment_id == created_comment.id
                    )
                )
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].reaction_key == "e:🔥"
            assert rows[0].count == 2

            # Colliding discussion message_id=20 (matching Post.tg_msg_id=20) still does NOT touch Post
            await service.handle_reaction_count_update(
                SimpleNamespace(
                    chat=disc_chat,
                    message_id=20,
                    reactions=[SimpleNamespace(emoji="👎", custom_emoji_id=None, count=99)],
                )
            )
            assert queue.empty()
            p = await s.get(Post, post.id)
            assert p is not None
    finally:
        sse_bus.unsubscribe(sub_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_web_reaction_before_tg_reaction_variation_selector_merge_and_toggle_off(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    tg_settings = test_settings.model_copy(
        update={
            "TG_CHANNEL_ID": -1002000002,
            "TG_DISCUSSION_CHAT_ID": -1003000003,
        }
    )
    ch = await ensure_channel(
        db_session,
        channel_id=1,
        tg_chat_id=tg_settings.TG_CHANNEL_ID,
        tg_discussion_chat_id=tg_settings.TG_DISCUSSION_CHAT_ID,
        status="active",
        id_offset=0,
    )
    await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=30,
        canonical_id=30,
        date=datetime(2026, 11, 19, 11, 0, tzinfo=UTC),
        text_html="Post 30",
    )
    comment = Comment(
        post_id=30,
        source="telegram",
        channel_id=ch.id,
        tg_msg_id=8100,
        tg_author_id=4242,
        tg_author_name="TG Bob",
        body_text="VS race test",
        body_html="<p>VS race test</p>",
        created_at=datetime(2026, 11, 19, 11, 1, tzinfo=UTC),
    )
    db_session.add(comment)
    await db_session.commit()
    comment_id = comment.id

    engine, factory = create_engine_and_factory(tg_settings)
    app = create_app(tg_settings)

    async def _per_req_session() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s

    app.dependency_overrides[get_settings] = lambda: tg_settings
    app.dependency_overrides[get_db_session] = _per_req_session
    service = TelegramService(
        session_factory=factory,
        settings=tg_settings,
        sse_publish=app.state.sse_bus.publish,
        bot_id=999000,
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://aza.moe",
        ) as client:
            # 1. Web user clicks "❤️" (\u2764\ufe0f) BEFORE any TG reaction exists
            r_on = await client.post(
                f"/api/life/comments/{comment_id}/reactions/toggle",
                json={"emoji": "❤️"},
            )
            assert r_on.status_code == 200
            assert len(r_on.json()["reactions"]) == 1
            assert r_on.json()["reactions"][0]["count"] == 1
            assert r_on.json()["reactions"][0]["reacted"] is True

            # 2. Later, TG syncs bare "\u2764" (without FE0F) on discussion chat
            await service.handle_reaction_count_update(
                SimpleNamespace(
                    chat=SimpleNamespace(id=tg_settings.TG_DISCUSSION_CHAT_ID),
                    message_id=8100,
                    reactions=[SimpleNamespace(emoji="\u2764", custom_emoji_id=None, count=1)],
                )
            )

            # 3. GET /posts/30/comments merges them into a single pill with count=2, reacted=True
            r_list = await client.get("/api/life/posts/30/comments")
            assert r_list.status_code == 200
            reactions = r_list.json()["comments"][0]["reactions"]
            assert len(reactions) == 1
            assert reactions[0]["tg_count"] == 1
            assert reactions[0]["web_count"] == 1
            assert reactions[0]["count"] == 2
            assert reactions[0]["reacted"] is True

            # 4. Second click on "❤️" cleanly removes the earlier "\u2764\ufe0f" row -> count=1, reacted=False
            r_off = await client.post(
                f"/api/life/comments/{comment_id}/reactions/toggle",
                json={"emoji": "❤️"},
            )
            assert r_off.status_code == 200
            off_reactions = r_off.json()["reactions"]
            assert len(off_reactions) == 1
            assert off_reactions[0]["tg_count"] == 1
            assert off_reactions[0]["web_count"] == 0
            assert off_reactions[0]["count"] == 1
            assert off_reactions[0]["reacted"] is False
    finally:
        await engine.dispose()
