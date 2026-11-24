from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from PIL import Image, ImageDraw
from pyrogram.enums import ParseMode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import Channel, Comment, NotificationQueue, OriginalUploadToken, Post, User
from app.db.repo import ensure_channel, get_active_channel, get_post_by_id, upsert_post
from app.db.session import create_engine_and_factory
from app.media.optimize import MAX_UPLOAD_BYTES
from app.sse import SSEBus, SSEEvent
from app.tg.bridge import TelegramBridge
from app.tg.client import AlbumBuffer, TelegramService
from app.tg.sync import ChannelBannedError, PeriodicReconciler


def _make_jpeg_bytes(
    color: tuple[int, int, int], *, pattern: int = 0, size: tuple[int, int] = (640, 480)
) -> bytes:
    im = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(im)
    w, h = size
    if pattern == 0:
        draw.rectangle([0, 0, w // 2, h], fill=(240, 220, 60))
        draw.ellipse([w // 4, h // 4, 3 * w // 4, 3 * h // 4], fill=(40, 40, 180))
    else:
        draw.polygon([(0, h), (w // 2, 0), (w, h)], fill=(20, 190, 120))
        draw.rectangle([w // 3, h // 2, w, h], fill=(180, 30, 80))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _drain_bus(queue: asyncio.Queue[SSEEvent]) -> list[SSEEvent]:
    items: list[SSEEvent] = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


@dataclass
class FakeTgClient:
    """In-memory mock Kurigram client for deterministic end-to-end tests."""

    bot_id: int = 999000
    bot_username: str = "corner_test_bot"
    next_msg_id: int = 5000
    sent_messages: list[dict[str, Any]] = field(default_factory=list)
    downloads: dict[str, bytes] = field(default_factory=dict)
    download_calls: list[Any] = field(default_factory=list)
    chat_map: dict[int, Any] = field(default_factory=dict)
    messages_by_chat: dict[int, dict[int, Any]] = field(default_factory=dict)
    raise_on_get_messages: Exception | None = None
    handlers: list[Any] = field(default_factory=list)
    started: bool = False

    def add_handler(self, handler: Any) -> None:
        self.handlers.append(handler)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def get_me(self) -> Any:
        return SimpleNamespace(id=self.bot_id, username=self.bot_username, is_bot=True)

    async def get_chat(self, chat_id: int) -> Any:
        if chat_id not in self.chat_map:
            raise RuntimeError(f"Chat {chat_id} inaccessible")
        return self.chat_map[chat_id]

    async def get_messages(self, chat_id: int, message_ids: list[int] | int) -> list[Any]:
        if self.raise_on_get_messages is not None:
            raise self.raise_on_get_messages
        ids = [message_ids] if isinstance(message_ids, int) else list(message_ids)
        store = self.messages_by_chat.get(chat_id, {})
        out = []
        for mid in ids:
            if mid in store:
                out.append(store[mid])
            else:
                out.append(SimpleNamespace(id=mid, empty=True))
        return out

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: Any | None = None,
        disable_web_page_preview: bool | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: Any | None = None,
    ) -> Any:
        self.next_msg_id += 1
        rec = {
            "id": self.next_msg_id,
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
            "reply_to_message_id": reply_to_message_id,
            "reply_markup": reply_markup,
        }
        self.sent_messages.append(rec)
        return SimpleNamespace(id=self.next_msg_id)

    async def download_media(self, message_or_file_id: Any, *, file_name: str) -> str | None:
        self.download_calls.append(message_or_file_id)
        key: str | None = None
        if isinstance(message_or_file_id, str):
            key = message_or_file_id
        else:
            doc = getattr(message_or_file_id, "document", None)
            photo = getattr(message_or_file_id, "photo", None)
            if doc is not None and getattr(doc, "file_id", None):
                key = str(doc.file_id)
            elif photo is not None and getattr(photo, "file_id", None):
                key = str(photo.file_id)
            elif getattr(message_or_file_id, "id", None) is not None:
                key = f"msg:{message_or_file_id.id}"

        payload = self.downloads.get(key or "")
        if payload is None:
            return None
        dest = Path(file_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        return str(dest)


@pytest_asyncio.fixture
async def db_factory(
    test_settings: Settings,
    db_session: AsyncSession,  # ensures clean truncated DB
) -> async_sessionmaker[AsyncSession]:
    engine, factory = create_engine_and_factory(test_settings)
    yield factory
    await engine.dispose()


@pytest.fixture
def tg_settings(test_settings: Settings) -> Settings:
    return test_settings.model_copy(
        update={
            "OWNER_TG_ID": 777001,
            "TG_CHANNEL_ID": -1002000002,
            "TG_DISCUSSION_CHAT_ID": -1003000003,
            "EMAIL_DEBOUNCE_SECONDS": 120,
        }
    )


# ---------------------------------------------------------------------------
# 1. AlbumBuffer Debounce & Max-Wait Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_album_buffer_debounce_and_max_cap() -> None:
    flushed: list[list[int]] = []

    async def _on_flush(msgs: list[Any]) -> None:
        flushed.append([m.id for m in msgs])

    buf = AlbumBuffer(on_flush=_on_flush, debounce_seconds=0.06, max_wait_seconds=0.18)

    buf.add("g1", SimpleNamespace(id=12))
    await asyncio.sleep(0.02)
    buf.add("g1", SimpleNamespace(id=10))
    await asyncio.sleep(0.02)
    buf.add("g1", SimpleNamespace(id=11))

    await buf.wait_idle()
    assert flushed == [[10, 11, 12]]

    flushed.clear()
    for i in range(6):
        buf.add("g2", SimpleNamespace(id=100 + i))
        await asyncio.sleep(0.04)

    await buf.wait_idle()
    assert len(flushed) >= 1
    assert sum(len(batch) for batch in flushed) == 6
    await buf.close()


# ---------------------------------------------------------------------------
# 2. TelegramService: Real SSEBus (B1), Album Caption Edit (B2),
#    Reaction Chat Isolation (B3), Split Album Merge (M9), Polls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_service_real_sse_bus_album_edit_and_reaction_isolation(
    db_factory: async_sessionmaker[AsyncSession],
    tg_settings: Settings,
) -> None:
    # Wire to REAL synchronous SSEBus.publish (B1 regression!)
    sse_bus = SSEBus()
    sub_id, sse_q, _ = sse_bus.subscribe()

    fake_client = FakeTgClient()
    fake_client.downloads["photo_1"] = _make_jpeg_bytes((180, 40, 40), pattern=0)
    fake_client.downloads["photo_2"] = _make_jpeg_bytes((40, 80, 190), pattern=1)
    fake_client.downloads["photo_3"] = _make_jpeg_bytes((60, 180, 80), pattern=0)

    # M1: pre-populate bot_id from TG_BOT_TOKEN prefix even before start()
    token_settings = tg_settings.model_copy(update={"TG_BOT_TOKEN": "999000:dummy_not_real_token"})
    service = TelegramService(
        session_factory=db_factory,
        settings=token_settings,
        sse_publish=sse_bus.publish,
        client=fake_client,
        album_debounce_seconds=0.05,
        album_max_wait_seconds=0.20,
        dm_debounce_seconds=0.05,
        dm_max_wait_seconds=0.20,
    )
    assert service.bot_id == 999000
    await service.start()
    assert service.bot_id == 999000

    # 1. Incoming 2-photo album via AlbumBuffer
    chat = SimpleNamespace(id=tg_settings.TG_CHANNEL_ID)
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    m1 = SimpleNamespace(
        id=20,
        chat=chat,
        date=now,
        caption="Beach album",
        text=None,
        media_group_id="mg_beach",
        photo=SimpleNamespace(file_id="photo_1", width=640, height=480, file_size=12345),
        views=10,
        forwards=1,
    )
    m2 = SimpleNamespace(
        id=21,
        chat=chat,
        date=now,
        caption=None,
        text=None,
        media_group_id="mg_beach",
        photo=SimpleNamespace(file_id="photo_2", width=640, height=480, file_size=23456),
        views=10,
        forwards=1,
    )
    await service.handle_channel_message(m1)
    await service.handle_channel_message(m2)
    await service.album_buffer.wait_idle()

    async with db_factory() as session:
        posts = list((await session.execute(select(Post))).scalars().all())
        assert len(posts) == 1
        post = await get_post_by_id(session, posts[0].id)
        assert post is not None
        assert post.tg_msg_id == 20
        assert post.tg_all_msg_ids == [20, 21]
        assert len(post.images) == 2
        assert all(img.phash is not None and img.rgb_sig is not None for img in post.images)

        # Simulate Workflow B upgraded original on photo #1 before caption edit
        post.images[0].original_url = "epochs/1/originals/20_0_orig.jpg"
        post.images[0].original_size = 987654
        post.images[0].original_name = "BEACH_RAW.JPG"
        await session.commit()

        tokens = list((await session.execute(select(OriginalUploadToken))).scalars().all())
        assert len(tokens) == 1
        assert tokens[0].post_id == post.id
        assert tokens[0].dm_msg_id is not None

    events = _drain_bus(sse_q)
    assert any(ev.event == "post.created" and ev.data["id"] == post.id for ev in events)

    # 2. B2 regression: Telegram EditedMessageHandler sends ONLY the single edited photo msg
    #    (with `msg.photo` STILL populated!) — sibling photo #2 AND original_url MUST be preserved!
    edited_m1_with_photo = SimpleNamespace(
        id=20,
        chat=chat,
        date=now,
        edit_date=datetime(2026, 11, 19, 12, 5, tzinfo=UTC),
        caption="Beach album (edited!)",
        text=None,
        media_group_id="mg_beach",
        photo=SimpleNamespace(file_id="photo_1", width=640, height=480, file_size=12345),
        views=42,
    )
    await service.handle_channel_edit(edited_m1_with_photo)

    async with db_factory() as session:
        refreshed = await get_post_by_id(session, post.id)
        assert refreshed is not None
        assert refreshed.text_html == "Beach album (edited!)"
        assert len(refreshed.images) == 2
        assert refreshed.images[0].original_url == "epochs/1/originals/20_0_orig.jpg"
        assert refreshed.images[0].original_name == "BEACH_RAW.JPG"
        assert refreshed.images[1].tg_msg_id == 21
    events = _drain_bus(sse_q)
    assert any(ev.event == "post.updated" and ev.data["id"] == post.id for ev in events)

    # 3. M9 regression: late-arriving split album sibling merges into SAME Post (no duplicate Post!)
    m3_late = SimpleNamespace(
        id=22,
        chat=chat,
        date=now,
        caption=None,
        text=None,
        media_group_id="mg_beach",
        photo=SimpleNamespace(file_id="photo_3", width=640, height=480, file_size=11111),
        views=None,
    )
    await service.handle_channel_message(m3_late)
    await service.album_buffer.wait_idle()

    async with db_factory() as session:
        posts = list((await session.execute(select(Post))).scalars().all())
        assert len(posts) == 1
        refreshed = await get_post_by_id(session, post.id)
        assert refreshed is not None
        assert len(refreshed.images) == 3
        assert refreshed.tg_all_msg_ids == [20, 21, 22]
        assert refreshed.text_html == "Beach album (edited!)"
        assert refreshed.views == 42
    _drain_bus(sse_q)

    # 4. Live reaction count update from ACTIVE CHANNEL -> updates tg_reactions
    reaction_update = SimpleNamespace(
        chat=chat,
        message_id=20,
        reactions=[SimpleNamespace(emoji="❤️", custom_emoji_id=None, count=7)],
    )
    await service.handle_reaction_count_update(reaction_update)
    events = _drain_bus(sse_q)
    assert len(events) == 1
    assert events[0].event == "reaction.updated"
    assert events[0].data["reactions"][0]["tg_count"] == 7
    assert "reacted" not in events[0].data["reactions"][0]

    # 5. B3 regression: reaction count update from DISCUSSION GROUP with colliding message_id=20
    #    MUST be ignored and MUST NOT corrupt channel post reactions!
    disc_reaction_update = SimpleNamespace(
        chat=SimpleNamespace(id=tg_settings.TG_DISCUSSION_CHAT_ID),
        message_id=20,
        reactions=[SimpleNamespace(emoji="👎", custom_emoji_id=None, count=99)],
    )
    await service.handle_reaction_count_update(disc_reaction_update)
    assert _drain_bus(sse_q) == []
    async with db_factory() as session:
        refreshed = await get_post_by_id(session, post.id)
        assert refreshed is not None
        assert len(refreshed.tg_reactions) == 1
        assert refreshed.tg_reactions[0].emoji == "❤️"
        assert refreshed.tg_reactions[0].count == 7

    # 6. Poll post + live poll update -> updates voter count & emits poll.updated via real SSEBus
    poll_msg = SimpleNamespace(
        id=30,
        chat=chat,
        date=now,
        text=None,
        caption=None,
        media_group_id=None,
        poll=SimpleNamespace(
            id="tg_poll_99",
            question=SimpleNamespace(text="Coffee or Tea?"),
            is_closed=False,
            is_anonymous=True,
            type="REGULAR",
            allows_multiple_answers=False,
            options=[
                SimpleNamespace(text=SimpleNamespace(text="Coffee"), persistent_id="0", voter_count=2),
                SimpleNamespace(text=SimpleNamespace(text="Tea"), persistent_id="1", voter_count=1),
            ],
        ),
    )
    await service.handle_channel_message(poll_msg)
    _drain_bus(sse_q)
    await service.handle_poll_update(
        SimpleNamespace(
            id="tg_poll_99",
            is_closed=True,
            options=[
                SimpleNamespace(voter_count=9),
                SimpleNamespace(voter_count=4),
            ],
        )
    )
    events = _drain_bus(sse_q)
    assert len(events) == 1
    assert events[0].event == "poll.updated"
    assert events[0].data["poll"]["is_closed"] is True
    assert events[0].data["poll"]["options"][0]["tg_voter_count"] == 9
    assert "my_votes" not in events[0].data["poll"]

    sse_bus.unsubscribe(sub_id)
    await service.stop()


# ---------------------------------------------------------------------------
# 3. Discussion 2-Way Bridge, ParseMode.DISABLED (M2), Manual Forward (B4),
#    Loop Prevention (M1), Entity Offsets & URL Safety (M10), Archived Epoch 1 DM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discussion_bridge_manual_fwd_loop_guard_entities_and_archived_dm(
    db_factory: async_sessionmaker[AsyncSession],
    tg_settings: Settings,
) -> None:
    sse_bus = SSEBus()
    sub_id, sse_q, _ = sse_bus.subscribe()

    fake_client = FakeTgClient(bot_id=999000)
    fake_client.downloads["avatar_bob_small"] = _make_jpeg_bytes((50, 120, 200), size=(64, 64))

    bridge = TelegramBridge(
        session_factory=db_factory,
        settings=tg_settings,
        client=fake_client,
    )
    service = TelegramService(
        session_factory=db_factory,
        settings=tg_settings,
        sse_publish=sse_bus.publish,
        bridge=bridge,
        client=fake_client,
        bot_id=999000,
    )

    async with db_factory() as session:
        ch1 = await ensure_channel(
            session,
            channel_id=1,
            tg_chat_id=-1001000001,
            title="Epoch 1",
            status="archived",
            id_offset=0,
        )
        post_epoch1 = await upsert_post(
            session,
            channel=ch1,
            canonical_id=5,
            tg_msg_id=5,
            date=datetime(2025, 1, 1, 0, 0, tzinfo=UTC),
            text_html="Old archived post",
        )
        ch2 = await ensure_channel(
            session,
            channel_id=2,
            tg_chat_id=tg_settings.TG_CHANNEL_ID,
            tg_discussion_chat_id=tg_settings.TG_DISCUSSION_CHAT_ID,
            title="Epoch 2",
            status="active",
            id_offset=100,
        )
        post_epoch2 = await upsert_post(
            session,
            channel=ch2,
            tg_msg_id=50,
            date=datetime(2026, 11, 19, 12, 0, tzinfo=UTC),
            text_html="Live Epoch 2 post",
        )
        alice = User(
            display_name="Alice",
            email="alice@example.com",
            email_verified=True,
            notify_replies=True,
            role="user",
        )
        session.add(alice)
        await session.flush()

        web_comment_e2 = Comment(
            post_id=post_epoch2.id,
            source="web",
            user_id=alice.id,
            body_text="Check <a href='https://evil.example'>click</a> & [phish](https://evil.example)",
            body_html="<p>Check</p>",
            created_at=datetime(2026, 11, 19, 12, 1, tzinfo=UTC),
        )
        web_comment_e1 = Comment(
            post_id=post_epoch1.id,
            source="web",
            anon_tag="Anonymous #ab12",
            body_text="Comment on archived Epoch 1",
            body_html="<p>Comment on archived Epoch 1</p>",
            created_at=datetime(2026, 11, 19, 12, 2, tzinfo=UTC),
        )
        session.add(web_comment_e2)
        session.add(web_comment_e1)
        await session.commit()
        alice_id = alice.id
        e2_post_id = post_epoch2.id
        e2_comment_id = web_comment_e2.id
        e1_comment_id = web_comment_e1.id

    disc_chat = SimpleNamespace(id=tg_settings.TG_DISCUSSION_CHAT_ID)

    # 1. B4 regression: MANUAL (non-auto) forward of channel post MUST NOT hijack tg_discussion_root_id!
    manual_fwd = SimpleNamespace(
        id=7999,
        chat=disc_chat,
        is_automatic_forward=False,
        forward_from_chat=SimpleNamespace(id=tg_settings.TG_CHANNEL_ID),
        forward_from_message_id=50,
        from_user=SimpleNamespace(id=12345, first_name="Mallory"),
        text="Live Epoch 2 post",
        date=datetime(2026, 11, 19, 12, 0, 1, tzinfo=UTC),
    )
    await service.handle_discussion_message(manual_fwd)
    async with db_factory() as session:
        p2 = await session.get(Post, e2_post_id)
        assert p2 is not None and p2.tg_discussion_root_id is None

    # 2. Genuine automatic forward links tg_discussion_root_id = 8001!
    autofwd = SimpleNamespace(
        id=8001,
        chat=disc_chat,
        is_automatic_forward=True,
        forward_from_chat=SimpleNamespace(id=tg_settings.TG_CHANNEL_ID),
        forward_from_message_id=50,
        text="Live Epoch 2 post",
        date=datetime(2026, 11, 19, 12, 0, 2, tzinfo=UTC),
    )
    await service.handle_discussion_message(autofwd)
    async with db_factory() as session:
        p2 = await session.get(Post, e2_post_id)
        assert p2 is not None and p2.tg_discussion_root_id == 8001

    # 3. Web -> TG bridge sends sanitized HTML with ParseMode.HTML and disable_web_page_preview=True!
    bridged_tg_id = await bridge.bridge_web_comment(e2_comment_id)
    assert bridged_tg_id is not None
    sent = fake_client.sent_messages[-1]
    assert sent["chat_id"] == tg_settings.TG_DISCUSSION_CHAT_ID
    assert sent["reply_to_message_id"] == 8001
    assert sent["parse_mode"] == ParseMode.HTML
    assert sent["disable_web_page_preview"] is True
    assert "<b>Alice</b>:" in sent["text"]
    assert "evil.example" not in sent["text"]

    # 4. Strict bot echo loop prevention (bot_id match AND is_self=True!)
    bot_echo = SimpleNamespace(
        id=bridged_tg_id,
        chat=disc_chat,
        from_user=SimpleNamespace(id=999000, first_name="Bot", is_self=True),
        reply_to_top_message_id=8001,
        reply_to_message_id=8001,
        text=sent["text"],
        date=datetime(2026, 11, 19, 12, 1, 5, tzinfo=UTC),
    )
    assert await service.handle_discussion_message(bot_echo) is None

    # 5. Incoming TG reply with leading spaces + BOLD + javascript: TEXT_LINK (M10 entity offset & scheme check!)
    bob_reply = SimpleNamespace(
        id=8010,
        chat=disc_chat,
        from_user=SimpleNamespace(
            id=424242,
            first_name="Bob",
            last_name="Lee",
            username="boblee",
            photo=SimpleNamespace(small_file_id="avatar_bob_small"),
        ),
        reply_to_top_message_id=8001,
        reply_to_message_id=bridged_tg_id,
        text="  Hi Alice! click example.com",
        entities=[
            SimpleNamespace(type="BOLD", offset=2, length=2),  # "Hi" (offset includes 2 leading spaces!)
            SimpleNamespace(type="TEXT_LINK", offset=12, length=5, url="javascript:alert(1)"),
            SimpleNamespace(type="URL", offset=18, length=11),  # "example.com" -> https://example.com
        ],
        date=datetime(2026, 11, 19, 12, 3, tzinfo=UTC),
    )
    saved_bob = await service.handle_discussion_message(bob_reply)
    assert saved_bob is not None
    assert saved_bob.post_id == e2_post_id
    assert saved_bob.parent_id == e2_comment_id
    assert "<b>Hi</b>" in saved_bob.body_html
    assert "javascript:" not in saved_bob.body_html
    assert 'href="https://example.com"' in saved_bob.body_html
    assert saved_bob.tg_author_avatar == "avatars/424242.jpg"
    assert (tg_settings.media_dir / "avatars" / "424242.jpg").is_file()

    # Duplicate delivery ignored
    assert await service.handle_discussion_message(bob_reply) is None

    # Verify SSE comment.created on real SSEBus & debounced SMTP notification queued for Alice!
    events = _drain_bus(sse_q)
    assert any(
        ev.event == "comment.created"
        and ev.data["id"] == saved_bob.id
        and ev.data["post_id"] == e2_post_id
        and ev.data["is_mine"] is False
        and ev.data["can_delete"] is False
        for ev in events
    )
    async with db_factory() as session:
        notifs = list((await session.execute(select(NotificationQueue))).scalars().all())
        assert len(notifs) == 1
        assert notifs[0].recipient_user_id == alice_id

    # 6. Archived Epoch 1 web comment -> DMs owner with ParseMode.DISABLED!
    before_count = len(fake_client.sent_messages)
    assert await bridge.bridge_web_comment(e1_comment_id) is None
    assert len(fake_client.sent_messages) == before_count + 1
    dm_alert = fake_client.sent_messages[-1]
    assert dm_alert["chat_id"] == tg_settings.OWNER_TG_ID
    assert dm_alert["parse_mode"] == ParseMode.DISABLED
    assert dm_alert["disable_web_page_preview"] is True
    assert "Post #5" in dm_alert["text"]

    sse_bus.unsubscribe(sub_id)


# ---------------------------------------------------------------------------
# 4. PeriodicReconciler: Multi-Photo Album Reactions (B5), Circuit Breaker (B5),
#    post.deleted SSE (M3), Edit-Date Guard (M4), ChannelBannedError Protection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_reconciler_album_reactions_circuit_breaker_and_post_deleted(
    db_factory: async_sessionmaker[AsyncSession],
    tg_settings: Settings,
) -> None:
    sse_bus = SSEBus()
    sub_id, sse_q, _ = sse_bus.subscribe()

    fake_client = FakeTgClient()
    chat_id = tg_settings.TG_CHANNEL_ID
    assert chat_id is not None

    # Seed Epoch 2 with 4 posts:
    # - p10: 2-photo album (tg_all_msg_ids=[10, 11]) with initial ❤️=5 reaction and rich HTML
    # - p12, p13, p14: single posts
    async with db_factory() as session:
        ch2 = await ensure_channel(
            session,
            channel_id=2,
            tg_chat_id=chat_id,
            title="Epoch 2",
            status="active",
            id_offset=0,
        )
        p10 = await upsert_post(
            session,
            channel=ch2,
            tg_msg_id=10,
            tg_media_group_id="mg_album",
            tg_all_msg_ids=[10, 11],
            date=datetime(2026, 11, 19, 10, 0, tzinfo=UTC),
            text_html='Rich <i class="custom-emoji">✨</i> album',
            views=10,
            forwards=0,
            tg_reactions=[{"emoji": "❤️", "custom_emoji_id": None, "count": 5}],
        )
        p12 = await upsert_post(
            session,
            channel=ch2,
            tg_msg_id=12,
            date=datetime(2026, 11, 19, 10, 5, tzinfo=UTC),
            text_html="Post 12",
            views=5,
        )
        p13 = await upsert_post(
            session,
            channel=ch2,
            tg_msg_id=13,
            date=datetime(2026, 11, 19, 10, 10, tzinfo=UTC),
            text_html="Post 13",
            views=5,
        )
        p14 = await upsert_post(
            session,
            channel=ch2,
            tg_msg_id=14,
            date=datetime(2026, 11, 19, 10, 15, tzinfo=UTC),
            text_html="Post 14 (will be deleted)",
            views=5,
        )
        await session.commit()
        p10_id, p12_id, p13_id, p14_id = p10.id, p12.id, p13.id, p14.id

    reconciler = PeriodicReconciler(
        session_factory=db_factory,
        settings=tg_settings,
        client=fake_client,
        sse_publish=sse_bus.publish,
        overlap_posts=20,
        batch_size=50,
    )

    # Case A: Channel is banned -> raises ChannelBannedError and preserves ALL posts!
    fake_client.chat_map[chat_id] = SimpleNamespace(
        id=chat_id, title="Banned Channel", is_banned=True, is_restricted=False, is_deactivated=False
    )
    with pytest.raises(ChannelBannedError):
        await reconciler.reconcile_once()

    # Case B: Channel accessible but returns 0 messages -> raises ChannelBannedError!
    fake_client.chat_map[chat_id] = SimpleNamespace(
        id=chat_id, title="Healthy Channel", is_banned=False, is_restricted=False, is_deactivated=False
    )
    fake_client.messages_by_chat[chat_id] = {}
    with pytest.raises(ChannelBannedError, match="0 messages"):
        await reconciler.reconcile_once()

    # Case C (B5 regression): Partial response missing 3 of 4 posts (>50%) -> trips circuit breaker!
    ch_obj = SimpleNamespace(id=chat_id)
    fake_client.messages_by_chat[chat_id] = {
        10: SimpleNamespace(
            id=10,
            empty=False,
            chat=ch_obj,
            date=datetime(2026, 11, 19, 10, 0, tzinfo=UTC),
            caption="Plain text",
            views=20,
            forwards=1,
        ),
    }
    with pytest.raises(ChannelBannedError, match="circuit breaker"):
        await reconciler.reconcile_once()

    async with db_factory() as session:
        for pid in (p10_id, p12_id, p13_id, p14_id):
            assert (await session.get(Post, pid)).is_deleted is False

    # Case D (B5 + M3 + M4 regression):
    # - Album p10 (msgs 10 & 11): msg 10 has ❤️=8 reactions & NO edit_date -> rich HTML NOT clobbered!
    #   msg 11 (sibling) has reactions=[] (empty) -> MUST NOT wipe p10's ❤️=8 reactions!
    # - p12 has edit_date changed -> text_html updated!
    # - p13 unchanged
    # - p14 confirmed missing (1 of 4 = 25% <= 50%) -> marked is_deleted=True & emits `post.deleted`!
    # - msg 15 missed during downtime -> ingested & emits `post.created`!
    fake_client.messages_by_chat[chat_id] = {
        10: SimpleNamespace(
            id=10,
            empty=False,
            chat=ch_obj,
            date=datetime(2026, 11, 19, 10, 0, tzinfo=UTC),
            caption="Plain text without custom emoji",
            edit_date=None,
            views=250,
            forwards=14,
            reactions=SimpleNamespace(
                reactions=[SimpleNamespace(emoji="❤️", custom_emoji_id=None, count=8)]
            ),
        ),
        11: SimpleNamespace(
            id=11,
            empty=False,
            chat=ch_obj,
            date=datetime(2026, 11, 19, 10, 0, tzinfo=UTC),
            caption=None,
            views=250,
            forwards=14,
            reactions=SimpleNamespace(reactions=[]),  # reaction-less album sibling!
        ),
        12: SimpleNamespace(
            id=12,
            empty=False,
            chat=ch_obj,
            date=datetime(2026, 11, 19, 10, 5, tzinfo=UTC),
            text="Post 12 (edited!)",
            edit_date=datetime(2026, 11, 19, 10, 30, tzinfo=UTC),
            views=19,
        ),
        13: SimpleNamespace(
            id=13,
            empty=False,
            chat=ch_obj,
            date=datetime(2026, 11, 19, 10, 10, tzinfo=UTC),
            text="Post 13",
            views=5,
        ),
        # 14 absent -> confirmed deleted (1/4 = 25%)
        15: SimpleNamespace(
            id=15,
            empty=False,
            chat=ch_obj,
            date=datetime(2026, 11, 19, 11, 0, tzinfo=UTC),
            text="Missed during downtime",
            views=88,
            forwards=2,
        ),
    }

    stats = await reconciler.reconcile_once()
    assert stats["status"] == "ok"
    assert stats["updated_posts"] == 2  # p10 (views/reactions) and p12 (edit/views)
    assert stats["deleted_posts"] == 1  # p14
    assert stats["new_posts"] == 1  # msg 15

    async with db_factory() as session:
        refreshed_10 = await get_post_by_id(session, p10_id, include_deleted=True)
        refreshed_12 = await get_post_by_id(session, p12_id, include_deleted=True)
        refreshed_14 = await get_post_by_id(session, p14_id, include_deleted=True)

        assert refreshed_10 is not None
        assert refreshed_10.views == 250
        assert refreshed_10.text_html == 'Rich <i class="custom-emoji">✨</i> album'  # M4: not clobbered!
        assert len(refreshed_10.tg_reactions) == 1
        assert refreshed_10.tg_reactions[0].count == 8  # B5: not wiped by sibling msg 11!

        assert refreshed_12 is not None
        assert refreshed_12.text_html == "Post 12 (edited!)"

        assert refreshed_14 is not None
        assert refreshed_14.is_deleted is True

    events = _drain_bus(sse_q)
    assert any(ev.event == "post.deleted" and ev.data == {"post_id": p14_id} for ev in events)
    assert not any(ev.event == "post.updated" and ev.data.get("id") == p14_id for ev in events)
    assert any(ev.event == "post.created" and ev.data.get("text") == "Missed during downtime" for ev in events)
    assert any(ev.event == "reaction.updated" and ev.data["post_id"] == p10_id for ev in events)

    sse_bus.unsubscribe(sub_id)


# ---------------------------------------------------------------------------
# 5. Workflow B Owner DM Drop (Size Cap M7, Unauthorized Callback M8, Swap)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_dm_workflow_b_size_cap_auto_apply_and_callback_auth(
    db_factory: async_sessionmaker[AsyncSession],
    tg_settings: Settings,
) -> None:
    fake_client = FakeTgClient(bot_id=999000)

    orig_a = _make_jpeg_bytes((200, 40, 40), pattern=0, size=(1200, 900))
    orig_b = _make_jpeg_bytes((30, 80, 210), pattern=1, size=(900, 1200))
    fake_client.downloads["tg_p1"] = orig_a
    fake_client.downloads["tg_p2"] = orig_b
    fake_client.downloads["dm_doc_a"] = orig_a
    fake_client.downloads["dm_doc_b"] = orig_b

    service = TelegramService(
        session_factory=db_factory,
        settings=tg_settings,
        client=fake_client,
        album_debounce_seconds=0.04,
        dm_debounce_seconds=0.05,
        dm_max_wait_seconds=0.20,
    )
    await service.start()

    ch = SimpleNamespace(id=tg_settings.TG_CHANNEL_ID)
    now = datetime(2026, 11, 19, 13, 0, tzinfo=UTC)
    await service.handle_channel_message(
        SimpleNamespace(
            id=70,
            chat=ch,
            date=now,
            caption="DM test",
            text=None,
            media_group_id="mg_dm",
            photo=SimpleNamespace(file_id="tg_p1", width=1200, height=900, file_size=5000),
        )
    )
    await service.handle_channel_message(
        SimpleNamespace(
            id=71,
            chat=ch,
            date=now,
            caption=None,
            text=None,
            media_group_id="mg_dm",
            photo=SimpleNamespace(file_id="tg_p2", width=900, height=1200, file_size=5000),
        )
    )
    await service.album_buffer.wait_idle()

    async with db_factory() as session:
        tok = (await session.execute(select(OriginalUploadToken))).scalar_one()
        prompt_dm_id = tok.dm_msg_id
        post_id = tok.post_id
        assert prompt_dm_id is not None

    owner = SimpleNamespace(id=tg_settings.OWNER_TG_ID)

    # M7 regression: oversized document rejected BEFORE calling download_media!
    dl_before = len(fake_client.download_calls)
    await service.handle_owner_dm(
        SimpleNamespace(
            id=9000,
            from_user=owner,
            reply_to_message_id=prompt_dm_id,
            document=SimpleNamespace(
                file_id="oversized_doc",
                file_name="HUGE.JPG",
                mime_type="image/jpeg",
                file_size=MAX_UPLOAD_BYTES + 1,
            ),
        )
    )
    await service.dm_buffer.wait_idle()
    assert len(fake_client.download_calls) == dl_before
    assert "exceeds maximum allowed size" in fake_client.sent_messages[-1]["text"]

    # Owner replies with scrambled original documents -> auto-applies!
    await service.handle_owner_dm(
        SimpleNamespace(
            id=9001,
            from_user=owner,
            reply_to_message_id=prompt_dm_id,
            document=SimpleNamespace(
                file_id="dm_doc_b", file_name="B_FULL.JPG", mime_type="image/jpeg", file_size=len(orig_b)
            ),
        )
    )
    await service.handle_owner_dm(
        SimpleNamespace(
            id=9002,
            from_user=owner,
            reply_to_message_id=prompt_dm_id,
            document=SimpleNamespace(
                file_id="dm_doc_a", file_name="A_FULL.JPG", mime_type="image/jpeg", file_size=len(orig_a)
            ),
        )
    )
    await service.dm_buffer.wait_idle()

    async with db_factory() as session:
        refreshed = await get_post_by_id(session, post_id)
        assert refreshed is not None
        assert refreshed.images[0].original_name == "A_FULL.JPG"
        assert refreshed.images[1].original_name == "B_FULL.JPG"

    # Burst-shot near-identical pair -> inline buttons -> unauthorized user ignored -> owner Swap applies!
    base_im = Image.new("RGB", (800, 600), (20, 20, 20))
    draw_base = ImageDraw.Draw(base_im)
    for y in range(0, 600, 8):
        draw_base.rectangle([0, y, 800, y + 8], fill=(min(255, 180 + y // 10), 90 + y // 6, 40))
    draw_base.ellipse([120, 100, 460, 440], fill=(250, 235, 120))
    draw_base.polygon([(0, 600), (320, 320), (800, 600)], fill=(45, 25, 65))
    burst_im_1 = base_im.copy()
    burst_im_2 = base_im.copy()
    ImageDraw.Draw(burst_im_2).rectangle([20, 20, 65, 65], fill=(220, 190, 110))

    b1 = io.BytesIO()
    burst_im_1.save(b1, "JPEG", quality=92)
    b2 = io.BytesIO()
    burst_im_2.save(b2, "JPEG", quality=92)
    fake_client.downloads["burst_tg_1"] = b1.getvalue()
    fake_client.downloads["burst_tg_2"] = b2.getvalue()
    fake_client.downloads["burst_dm_1"] = b1.getvalue()
    fake_client.downloads["burst_dm_2"] = b2.getvalue()

    await service.handle_channel_message(
        SimpleNamespace(
            id=80,
            chat=ch,
            date=now,
            caption="Burst post",
            text=None,
            media_group_id="mg_burst",
            photo=SimpleNamespace(file_id="burst_tg_1", width=800, height=600, file_size=5000),
        )
    )
    await service.handle_channel_message(
        SimpleNamespace(
            id=81,
            chat=ch,
            date=now,
            caption=None,
            text=None,
            media_group_id="mg_burst",
            photo=SimpleNamespace(file_id="burst_tg_2", width=800, height=600, file_size=5000),
        )
    )
    await service.album_buffer.wait_idle()

    async with db_factory() as session:
        tok2 = (
            await session.execute(
                select(OriginalUploadToken).order_by(OriginalUploadToken.post_id.desc()).limit(1)
            )
        ).scalar_one()
        burst_dm_id = tok2.dm_msg_id
        burst_post_id = tok2.post_id

    await service.handle_owner_dm(
        SimpleNamespace(
            id=9101,
            from_user=owner,
            reply_to_message_id=burst_dm_id,
            document=SimpleNamespace(file_id="burst_dm_1", file_name="BURST_1.JPG", mime_type="image/jpeg"),
        )
    )
    await service.handle_owner_dm(
        SimpleNamespace(
            id=9102,
            from_user=owner,
            reply_to_message_id=burst_dm_id,
            document=SimpleNamespace(file_id="burst_dm_2", file_name="BURST_2.JPG", mime_type="image/jpeg"),
        )
    )
    await service.dm_buffer.wait_idle()

    ambig_msg = fake_client.sent_messages[-1]
    assert "Ambiguous" in ambig_msg["text"]
    markup = ambig_msg["reply_markup"]
    assert markup is not None
    swap_btn = [
        btn
        for row in markup.inline_keyboard
        for btn in row
        if btn.callback_data.startswith("wf_swap:")
    ][0]

    # Unauthorized non-owner callback query MUST be ignored (manifest untouched!)
    unauth_answered: list[str] = []

    class _UnauthCallbackQuery:
        from_user = SimpleNamespace(id=111222)  # NOT OWNER_TG_ID!
        data = swap_btn.callback_data

        @staticmethod
        async def answer(text: str = "", show_alert: bool = False) -> None:
            unauth_answered.append(text)

    await service.handle_callback_query(_UnauthCallbackQuery())
    assert unauth_answered == []

    # Authorized owner clicks Swap -> applies cleanly!
    answered: list[str] = []
    edited_texts: list[str] = []

    class _OwnerCallbackQuery:
        from_user = owner
        data = swap_btn.callback_data

        class message:
            @staticmethod
            async def edit_text(text: str) -> None:
                edited_texts.append(text)

        @staticmethod
        async def answer(text: str = "", show_alert: bool = False) -> None:
            answered.append(text)

    await service.handle_callback_query(_OwnerCallbackQuery())
    assert answered == ["Applied!"]
    assert any("Applied 2 confirmed" in t for t in edited_texts)

    # Second click on same Swap button -> safely rejected as already processed!
    answered.clear()
    await service.handle_callback_query(_OwnerCallbackQuery())
    assert answered == ["Batch expired or already processed"]

    async with db_factory() as session:
        burst_post = await get_post_by_id(session, burst_post_id)
        assert burst_post is not None
        assert {img.original_name for img in burst_post.images} == {"BURST_1.JPG", "BURST_2.JPG"}

    await service.stop()


@pytest.mark.asyncio
async def test_owner_dm_reaction_allowlist_commands_and_custom_emoji_entities(
    test_settings: Settings,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    from app.db.repo import get_allowed_reactions
    from app.sse import SSEBus

    engine, db_factory = create_engine_and_factory(test_settings)
    sse_bus = SSEBus()
    _, sse_queue, _ = sse_bus.subscribe()

    sent_dm_replies: list[tuple[int, str]] = []

    class _CmdClient:
        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def get_me(self) -> Any:
            return SimpleNamespace(id=999999, username="corner_test_bot")

        async def send_message(self, chat_id: int, text: str, **_kwargs: Any) -> Any:
            sent_dm_replies.append((int(chat_id), text))
            return SimpleNamespace(id=len(sent_dm_replies) + 100)

        async def get_custom_emoji_stickers(self, custom_emoji_ids: list[int]) -> list[Any]:
            return [
                SimpleNamespace(custom_emoji_id=cid, is_animated=False, is_video=False)
                for cid in custom_emoji_ids
            ]

        async def download_media(self, sticker: Any, file_name: str) -> str:
            Path(file_name).parent.mkdir(parents=True, exist_ok=True)
            Path(file_name).write_bytes(b"fake-webp")
            return file_name

    tg_settings = test_settings.model_copy(
        update={
            "OWNER_TG_ID": 777001,
            "TG_CHANNEL_ID": -100112233,
            "DATA_DIR": tmp_path / "data_cmd",
        }
    )
    tg_settings.exports_dir.mkdir(parents=True, exist_ok=True)

    service = TelegramService(
        session_factory=db_factory,
        settings=tg_settings,
        client=_CmdClient(),
        sse_publish=sse_bus.publish,
    )
    await service.start()
    owner = SimpleNamespace(id=777001, first_name="Owner")
    stranger = SimpleNamespace(id=888999, first_name="Stranger")

    try:
        # 1. Stranger DM command is completely ignored
        await service.handle_owner_dm(SimpleNamespace(from_user=stranger, text="/reactions set ❤️"))
        assert sent_dm_replies == []

        # 2. Owner /reactions lists default ordered list (without mutating / SSE)
        await service.handle_owner_dm(SimpleNamespace(from_user=owner, text="/reactions"))
        assert len(sent_dm_replies) == 1
        assert "Global allowed reactions" in sent_dm_replies[-1][1]
        assert "1. ❤️" in sent_dm_replies[-1][1]

        # 3. Owner /reactions set ❤️👍🔥 + TG CUSTOM_EMOJI entity updates exact order, caches custom asset, emits SSE
        cmd_text = "/reactions set ❤️👍🔥 ⭐"
        star_idx = cmd_text.index("⭐")
        utf16_off = len(cmd_text[:star_idx].encode("utf-16-le")) // 2
        custom_entity = SimpleNamespace(
            type="CUSTOM_EMOJI",
            offset=utf16_off,
            length=1,
            custom_emoji_id=5368324170671202286,
        )
        await service.handle_owner_dm(
            SimpleNamespace(from_user=owner, text=cmd_text, entities=[custom_entity])
        )
        ev = await asyncio.wait_for(sse_queue.get(), timeout=1.0)
        assert ev.event == "reactions.allowed_updated"
        assert [x["reaction_key"] for x in ev.data["allowed_reactions"]] == [
            "e:❤",
            "e:👍",
            "e:🔥",
            "c:5368324170671202286",
        ]
        assert (tg_settings.exports_dir / "emoji" / "5368324170671202286.webp").is_file()

        # 4. Add emoji at position #1, move #4 to #2, remove 🔥
        await service.handle_owner_dm(
            SimpleNamespace(from_user=owner, text="/reactions add 🎉 at 1")
        )
        await asyncio.wait_for(sse_queue.get(), timeout=1.0)

        await service.handle_owner_dm(
            SimpleNamespace(from_user=owner, text="/reactions move #5 2")
        )
        await asyncio.wait_for(sse_queue.get(), timeout=1.0)

        await service.handle_owner_dm(
            SimpleNamespace(from_user=owner, text="/reactions rm 🔥")
        )
        ev_rm = await asyncio.wait_for(sse_queue.get(), timeout=1.0)
        assert [x["reaction_key"] for x in ev_rm.data["allowed_reactions"]] == [
            "e:🎉",
            "c:5368324170671202286",
            "e:❤",
            "e:👍",
        ]

        async with db_factory() as session:
            saved = await get_allowed_reactions(session)
            assert [x["reaction_key"] for x in saved] == [
                "e:🎉",
                "c:5368324170671202286",
                "e:❤",
                "e:👍",
            ]

        # 5. Reset restores defaults
        await service.handle_owner_dm(SimpleNamespace(from_user=owner, text="/reactions reset"))
        ev_reset = await asyncio.wait_for(sse_queue.get(), timeout=1.0)
        assert len(ev_reset.data["allowed_reactions"]) > 10
        assert ev_reset.data["allowed_reactions"][0]["reaction_key"] == "e:❤"
    finally:
        await service.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_dual_bot_and_readonly_user_client_architecture(
    tmp_path: Path,
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    """Bot handles sending/bridging; read-only user_client handles polls, raw reactions, and deletions."""
    from pyrogram.handlers import DeletedMessagesHandler, PollHandler, RawUpdateHandler

    tg_settings = test_settings.model_copy(
        update={
            "DATA_DIR": tmp_path,
            "TG_BOT_TOKEN": "999000:TEST_BOT_TOKEN",
            "TG_CHANNEL_ID": -1001234567890,
            "TG_DISCUSSION_CHAT_ID": -1009876543210,
            "OWNER_TG_ID": 777000,
        }
    )
    engine, db_factory = create_engine_and_factory(tg_settings)
    sse_bus = SSEBus()
    _, sse_q, _ = sse_bus.subscribe()

    bot_client = FakeTgClient()
    user_client = FakeTgClient(bot_id=888000, bot_username="reader_user")
    user_client.get_me = lambda: asyncio.sleep(  # type: ignore[method-assign]
        0, result=SimpleNamespace(id=888000, username="reader_user", is_bot=False)
    )

    service = TelegramService(
        session_factory=db_factory,
        settings=tg_settings,
        sse_publish=sse_bus.publish,
        client=bot_client,
        user_client=user_client,
    )
    reconciler = PeriodicReconciler(
        session_factory=db_factory,
        settings=tg_settings,
        telegram_service=service,
        sse_publish=sse_bus.publish,
    )

    try:
        await service.start()
        assert bot_client.started is True
        assert user_client.started is True
        # Bridge uses ONLY the bot client, never user_client
        assert service.bridge.client is bot_client
        # Reconciler prefers user_client for read access when available
        assert reconciler._get_client() is user_client

        # user_client registers only read-only listeners: PollHandler, RawUpdateHandler, DeletedMessagesHandler
        user_handler_types = [type(h) for h in user_client.handlers]
        assert PollHandler in user_handler_types
        assert RawUpdateHandler in user_handler_types
        assert DeletedMessagesHandler in user_handler_types
        assert len(user_handler_types) == 3

        # Seed a channel post + poll + discussion comment in DB
        async with db_factory() as session:
            ch = await ensure_channel(
                session,
                tg_chat_id=tg_settings.TG_CHANNEL_ID,
                tg_discussion_chat_id=tg_settings.TG_DISCUSSION_CHAT_ID,
                status="active",
            )
            post = await upsert_post(
                session,
                channel=ch,
                tg_msg_id=501,
                tg_all_msg_ids=[501],
                date=datetime(2026, 11, 20, 12, 0, tzinfo=UTC),
                text_html="Poll post",
                poll={
                    "id": "poll_owner_501",
                    "question": "Tea or coffee?",
                    "options": [{"text": "Tea"}, {"text": "Coffee"}],
                    "is_closed": False,
                    "is_anonymous": True,
                },
            )
            comment = Comment(
                post_id=post.id,
                source="telegram",
                channel_id=ch.id,
                tg_msg_id=9001,
                tg_author_id=4242,
                tg_author_name="Reader",
                body_text="Nice poll",
                body_html="<p>Nice poll</p>",
                created_at=datetime(2026, 11, 20, 12, 1, tzinfo=UTC),
            )
            session.add(comment)
            await session.commit()
            post_id = post.id
            comment_id = comment.id

        poll_handler = next(h for h in user_client.handlers if isinstance(h, PollHandler))
        raw_handler = next(h for h in user_client.handlers if isinstance(h, RawUpdateHandler))
        del_handler = next(h for h in user_client.handlers if isinstance(h, DeletedMessagesHandler))

        # 1. Poll update via user_client updates poll counts and emits poll.updated
        await poll_handler.callback(
            user_client,
            SimpleNamespace(
                id="poll_owner_501",
                is_closed=True,
                options=[
                    SimpleNamespace(voter_count=3),
                    SimpleNamespace(voter_count=7),
                ],
            ),
        )
        events = _drain_bus(sse_q)
        assert any(
            ev.event == "poll.updated"
            and ev.data["post_id"] == post_id
            and ev.data["poll"]["is_closed"] is True
            and [o["voter_count"] for o in ev.data["poll"]["options"]] == [3, 7]
            for ev in events
        )

        # 2. Raw UpdateMessageReactions via user_client updates channel post reactions (read-only)
        chan_raw_update = SimpleNamespace(
            peer=SimpleNamespace(channel_id=1234567890),
            msg_id=501,
            reactions=SimpleNamespace(
                results=[
                    SimpleNamespace(reaction=SimpleNamespace(emoticon="🔥"), count=4),
                    SimpleNamespace(reaction=SimpleNamespace(document_id=5368324170671202286), count=2),
                ]
            ),
        )
        await raw_handler.callback(user_client, chan_raw_update, {}, {})
        events = _drain_bus(sse_q)
        assert len(events) == 1
        assert events[0].event == "reaction.updated"
        assert {r["reaction_key"]: r["tg_count"] for r in events[0].data["reactions"]} == {
            "e:🔥": 4,
            "c:5368324170671202286": 2,
        }

        # Duplicate identical reaction event is deduplicated (no extra SSE emitted)
        await raw_handler.callback(user_client, chan_raw_update, {}, {})
        assert _drain_bus(sse_q) == []

        # Raw reaction update from an unrelated chat is strictly ignored
        await raw_handler.callback(
            user_client,
            SimpleNamespace(
                peer=SimpleNamespace(channel_id=9999999999),
                msg_id=501,
                reactions=SimpleNamespace(
                    results=[SimpleNamespace(reaction=SimpleNamespace(emoticon="👎"), count=99)]
                ),
            ),
            {},
            {},
        )
        assert _drain_bus(sse_q) == []

        # 3. Raw UpdateMessageReactions on discussion chat updates CommentTgReaction
        disc_raw_update = SimpleNamespace(
            peer=SimpleNamespace(channel_id=9876543210),
            msg_id=9001,
            reactions=SimpleNamespace(
                results=[SimpleNamespace(reaction=SimpleNamespace(emoticon="👍"), count=5)]
            ),
        )
        await raw_handler.callback(user_client, disc_raw_update, {}, {})
        events = _drain_bus(sse_q)
        assert len(events) == 1
        assert events[0].event == "comment.reaction.updated"
        assert events[0].data["comment_id"] == comment_id
        assert events[0].data["reactions"][0]["tg_count"] == 5

        # 4. Deletion events via user_client soft-delete discussion comments and channel posts
        await del_handler.callback(
            user_client,
            [
                SimpleNamespace(id=9001, chat=SimpleNamespace(id=tg_settings.TG_DISCUSSION_CHAT_ID)),
                SimpleNamespace(id=501, chat=SimpleNamespace(id=tg_settings.TG_CHANNEL_ID)),
            ],
        )
        events = _drain_bus(sse_q)
        assert any(ev.event == "post.deleted" and ev.data == {"post_id": post_id} for ev in events)
        assert any(
            ev.event == "comment.deleted"
            and ev.data == {"post_id": post_id, "comment_id": comment_id}
            for ev in events
        )
        async with db_factory() as session:
            p_after = await session.get(Post, post_id)
            c_after = await session.get(Comment, comment_id)
            assert p_after is not None and p_after.is_deleted is True
            assert c_after is not None and c_after.deleted_at is not None

        # Verify user_client never sent a single message or reaction
        assert user_client.sent_messages == []
    finally:
        await service.stop()
        assert user_client.started is False
        assert bot_client.started is False
        await engine.dispose()


async def test_crawl_channel_history_uses_bot_get_messages_only(
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """Startup/CLI channel crawl uses bot get_messages ID ranges and never user_client."""
    tg_settings = test_settings.model_copy(
        update={
            "DATA_DIR": tmp_path / "crawl_data",
            "TG_BOT_TOKEN": "999000:TEST_BOT_TOKEN",
            "TG_CHANNEL_ID": -1001998877665,
            "TG_DISCUSSION_CHAT_ID": -1001998877666,
            "OWNER_TG_ID": 777000,
        }
    )
    engine, factory = create_engine_and_factory(tg_settings)

    def _msg(msg_id: int, text: str, *, media_group_id: str | None = None) -> Any:
        return SimpleNamespace(
            id=msg_id,
            empty=False,
            chat=SimpleNamespace(id=tg_settings.TG_CHANNEL_ID),
            date=datetime(2026, 11, 20, 12, 0, tzinfo=UTC),
            edit_date=None,
            text=text or None,
            caption=None,
            entities=None,
            caption_entities=None,
            media_group_id=media_group_id,
            photo=None,
            poll=None,
            reactions=None,
            views=None,
            forwards=None,
            forward_from_chat=None,
            forward_sender_name=None,
            reply_to_message_id=None,
            quote=None,
            author_signature=None,
            service=None,
            show_caption_above_media=False,
            has_media_spoiler=False,
        )

    class _StrictNoCrawlUserClient(FakeTgClient):
        async def get_messages(self, *args: Any, **kwargs: Any) -> list[Any]:
            raise AssertionError("user_client.get_messages must not be called for channel crawl")

        async def get_chat_history(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("get_chat_history must never be called")

    bot_client = FakeTgClient(
        bot_id=999000,
        chat_map={
            tg_settings.TG_CHANNEL_ID: SimpleNamespace(
                id=tg_settings.TG_CHANNEL_ID,
                title="Active Channel",
                username="active_chan",
            )
        },
        messages_by_chat={
            tg_settings.TG_CHANNEL_ID: {
                1: _msg(1, "First historic post"),
                2: _msg(2, "Album pic 1", media_group_id="g1"),
                3: _msg(3, "", media_group_id="g1"),
                205: _msg(205, "Later historic post"),
            }
        },
    )
    user_client = _StrictNoCrawlUserClient(bot_id=777888)
    sse_bus = SSEBus()
    service = TelegramService(
        session_factory=factory,
        settings=tg_settings,
        sse_publish=sse_bus.publish,
        client=bot_client,
        user_client=user_client,
    )

    try:
        # Full crawl traverses past empty gap (205 in second batch when batch_size=200)
        summary = await service.crawl_channel_history(full=True, batch_size=200)
        assert summary["status"] == "ok"
        assert summary["crawled_messages"] == 4
        assert summary["posts_ingested"] == 3
        assert summary["max_msg_id"] == 205

        async with factory() as session:
            ch = await get_active_channel(session)
            assert ch is not None
            assert isinstance(ch, Channel)
            assert ch.title == "Active Channel"
            assert ch.username == "active_chan"
            assert ch.last_crawled_msg_id == 205
            posts = list(
                (
                    await session.execute(
                        select(Post).where(Post.channel_id == ch.id).order_by(Post.tg_msg_id)
                    )
                )
                .scalars()
                .all()
            )
            assert [p.tg_msg_id for p in posts] == [1, 2, 205]

        # Ensure owner DM was NOT spammed during historical backfill
        assert bot_client.sent_messages == []
    finally:
        await service.stop()
        await engine.dispose()


async def test_fallback_process_sticker_and_media_attachments(
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """Built-in converter preserves video/animated stickers, video, and voice files."""
    tg_settings = test_settings.model_copy(
        update={
            "DATA_DIR": tmp_path / "media_data",
            "TG_BOT_TOKEN": "999000:TEST_BOT_TOKEN",
            "TG_CHANNEL_ID": -1001998877667,
            "TG_DISCUSSION_CHAT_ID": -1001998877668,
            "OWNER_TG_ID": 777000,
        }
    )
    engine, factory = create_engine_and_factory(tg_settings)
    bot_client = FakeTgClient(
        bot_id=999000,
        chat_map={
            tg_settings.TG_CHANNEL_ID: SimpleNamespace(
                id=tg_settings.TG_CHANNEL_ID,
                title="Media Channel",
                username="media_chan",
            )
        },
        downloads={
            "sticker.webm": b"WEBM_BYTES",
            "sticker.tgs": b"TGS_BYTES",
            "voice.ogg": b"OGG_BYTES",
        },
    )

    async def _fake_dl(target: Any, *, file_name: str) -> str:
        dest = Path(file_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"MEDIA_BYTES")
        return str(dest)

    bot_client.download_media = _fake_dl  # type: ignore[method-assign]

    service = TelegramService(
        session_factory=factory,
        settings=tg_settings,
        client=bot_client,
    )
    try:
        async with factory() as session:
            await ensure_channel(
                session,
                tg_chat_id=tg_settings.TG_CHANNEL_ID,
                tg_discussion_chat_id=tg_settings.TG_DISCUSSION_CHAT_ID,
                status="active",
            )
            await session.commit()

        webm_sticker_msg = SimpleNamespace(
            id=11,
            empty=False,
            chat=SimpleNamespace(id=tg_settings.TG_CHANNEL_ID),
            date=datetime(2026, 11, 21, 0, 50, tzinfo=UTC),
            edit_date=None,
            text=None,
            caption=None,
            entities=None,
            caption_entities=None,
            media_group_id=None,
            photo=None,
            sticker=SimpleNamespace(
                file_id="st_webm_id",
                file_name="sticker.webm",
                mime_type="video/webm",
                file_size=1234,
                width=512,
                height=512,
                is_animated=False,
                is_video=True,
                emoji="🖱",
                thumbs=None,
            ),
            poll=None,
            reactions=None,
            views=2,
            forwards=None,
            forward_from_chat=None,
            forward_sender_name=None,
            reply_to_message_id=None,
            quote=None,
            author_signature=None,
            service=None,
            show_caption_above_media=False,
            has_media_spoiler=False,
        )
        tgs_sticker_msg = SimpleNamespace(
            **{
                **webm_sticker_msg.__dict__,
                "id": 12,
                "sticker": SimpleNamespace(
                    file_id="st_tgs_id",
                    file_name="AnimatedSticker.tgs",
                    mime_type="application/x-tgsticker",
                    file_size=2222,
                    width=512,
                    height=512,
                    is_animated=True,
                    is_video=False,
                    emoji="✨",
                    thumbs=None,
                ),
            }
        )
        voice_msg = SimpleNamespace(
            **{
                **webm_sticker_msg.__dict__,
                "id": 13,
                "sticker": None,
                "voice": SimpleNamespace(
                    file_id="voice_id",
                    mime_type="audio/ogg",
                    file_size=3333,
                    duration=4,
                    thumbs=None,
                ),
            }
        )

        ingested = await service._ingest_channel_messages(
            [webm_sticker_msg, tgs_sticker_msg, voice_msg],
            is_edit=False,
            trigger_prompt=False,
        )
        assert len(ingested) == 3

        by_tg = {p.tg_msg_id: p for p in ingested}
        assert by_tg[11].files is not None
        assert by_tg[11].files[0]["media_type"] == "sticker"
        assert by_tg[11].files[0]["url"].endswith("/11.webm")
        assert by_tg[11].files[0]["sticker_emoji"] == "🖱"

        assert by_tg[12].files is not None
        assert by_tg[12].files[0]["media_type"] == "sticker"
        assert by_tg[12].files[0]["url"].endswith("/12.tgs")
        assert by_tg[12].files[0]["sticker_emoji"] == "✨"

        assert by_tg[13].files is not None
        assert by_tg[13].files[0]["media_type"] == "voice_message"
        assert by_tg[13].files[0]["url"].endswith("/13.ogg")
    finally:
        await service.stop()
        await engine.dispose()


async def test_autoforward_kurigram_and_ondemand_discussion_root_lookup(
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """Kurigram `automatic_forward=True` and on-demand `get_discussion_message` resolve active roots."""
    tg_settings = test_settings.model_copy(
        update={
            "DATA_DIR": tmp_path / "disc_lookup_data",
            "SITE_URL": "https://aza.moe/life",
            "TG_BOT_TOKEN": "999000:TEST_BOT_TOKEN",
            "TG_CHANNEL_ID": -1001998877771,
            "TG_DISCUSSION_CHAT_ID": -1001998877772,
            "OWNER_TG_ID": 777000,
        }
    )
    engine, factory = create_engine_and_factory(tg_settings)
    bot_client = FakeTgClient(bot_id=999000)
    user_client = FakeTgClient(bot_id=888000, bot_username="reader_user")

    async def _fake_get_disc(chat_id: int, msg_id: int) -> Any:
        if chat_id == tg_settings.TG_CHANNEL_ID and msg_id == 77:
            return SimpleNamespace(
                id=99077, chat=SimpleNamespace(id=tg_settings.TG_DISCUSSION_CHAT_ID)
            )
        return None

    user_client.get_discussion_message = _fake_get_disc  # type: ignore[attr-defined]
    service = TelegramService(
        session_factory=factory,
        settings=tg_settings,
        client=bot_client,
        user_client=user_client,
    )

    try:
        async with factory() as session:
            ch = await ensure_channel(
                session,
                tg_chat_id=tg_settings.TG_CHANNEL_ID,
                tg_discussion_chat_id=tg_settings.TG_DISCUSSION_CHAT_ID,
                status="active",
            )
            p_kuri = await upsert_post(
                session,
                channel=ch,
                tg_msg_id=76,
                tg_all_msg_ids=[76],
                date=datetime(2026, 11, 21, 1, 0, tzinfo=UTC),
                text_html="Kurigram autoforward target",
            )
            p_ondemand = await upsert_post(
                session,
                channel=ch,
                tg_msg_id=77,
                tg_all_msg_ids=[77],
                date=datetime(2026, 11, 21, 1, 1, tzinfo=UTC),
                text_html="Needs on-demand discussion lookup",
            )
            c = Comment(
                post_id=p_ondemand.id,
                source="web",
                anon_tag="Anon #9f8f",
                body_text="测试喵",
                body_html="<p>测试喵</p>",
                created_at=datetime(2026, 11, 21, 1, 2, tzinfo=UTC),
            )
            session.add(c)
            await session.commit()
            kuri_post_id = p_kuri.id
            ondemand_post_id = p_ondemand.id
            comment_id = c.id

        # 1. Kurigram `automatic_forward=True` + `forward_origin.sender_chat` links tg_discussion_root_id
        kuri_autofwd = SimpleNamespace(
            id=99076,
            chat=SimpleNamespace(id=tg_settings.TG_DISCUSSION_CHAT_ID),
            automatic_forward=True,
            is_automatic_forward=None,
            forward_origin=SimpleNamespace(
                sender_chat=SimpleNamespace(id=tg_settings.TG_CHANNEL_ID),
                message_id=76,
            ),
            text="Kurigram autoforward target",
            date=datetime(2026, 11, 21, 1, 0, 2, tzinfo=UTC),
        )
        await service.handle_discussion_message(kuri_autofwd)
        async with factory() as session:
            p1 = await session.get(Post, kuri_post_id)
            assert p1 is not None and p1.tg_discussion_root_id == 99076

        # 2. On-demand lookup resolves missing root 99077 and relays in mautrix/telegram format
        bridged_id = await service.bridge.bridge_web_comment(comment_id)
        assert bridged_id is not None
        assert len(bot_client.sent_messages) == 1
        relayed = bot_client.sent_messages[0]
        assert relayed["chat_id"] == tg_settings.TG_DISCUSSION_CHAT_ID
        assert relayed["reply_to_message_id"] == 99077
        assert relayed["text"] == "<b>Anon #9f8f</b>:\n测试喵"
        assert relayed["parse_mode"] == ParseMode.HTML
        async with factory() as session:
            p2 = await session.get(Post, ondemand_post_id)
            assert p2 is not None and p2.tg_discussion_root_id == 99077
    finally:
        await service.stop()
        await engine.dispose()


async def test_owner_dm_fullsync_preserves_existing_images_and_updates_edits(
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    """/fullsync crawls from ID #1, updates edits/deletions, and never overwrites existing images."""
    tg_settings = test_settings.model_copy(
        update={
            "DATA_DIR": tmp_path / "fullsync_data",
            "TG_BOT_TOKEN": "999000:TEST_BOT_TOKEN",
            "TG_CHANNEL_ID": -1001998877773,
            "TG_DISCUSSION_CHAT_ID": -1001998877774,
            "OWNER_TG_ID": 777000,
        }
    )
    engine, factory = create_engine_and_factory(tg_settings)

    photo_msg_1 = SimpleNamespace(
        id=1,
        empty=False,
        chat=SimpleNamespace(id=tg_settings.TG_CHANNEL_ID),
        date=datetime(2026, 11, 21, 1, 0, tzinfo=UTC),
        edit_date=datetime(2026, 11, 21, 1, 5, tzinfo=UTC),
        text=None,
        caption="Edited photo caption",
        entities=None,
        caption_entities=None,
        media_group_id=None,
        photo=SimpleNamespace(width=800, height=600, file_size=5000),
        poll=None,
        reactions=None,
        views=42,
        forwards=1,
        forward_from_chat=None,
        forward_sender_name=None,
        reply_to_message_id=None,
        quote=None,
        author_signature=None,
        service=None,
        show_caption_above_media=False,
        has_media_spoiler=False,
    )
    bot_client = FakeTgClient(
        bot_id=999000,
        chat_map={
            tg_settings.TG_CHANNEL_ID: SimpleNamespace(
                id=tg_settings.TG_CHANNEL_ID,
                title="Fullsync Channel",
                username="fullsync_chan",
            )
        },
        messages_by_chat={tg_settings.TG_CHANNEL_ID: {1: photo_msg_1}},
    )
    dl_calls: list[int] = []

    async def _fail_if_existing_photo_dl(msg: Any, *, file_name: str) -> str:
        dl_calls.append(int(getattr(msg, "id", 0)))
        raise AssertionError("Must not re-download photo for existing post during /fullsync")

    bot_client.download_media = _fail_if_existing_photo_dl  # type: ignore[method-assign]
    service = TelegramService(
        session_factory=factory,
        settings=tg_settings,
        client=bot_client,
    )

    try:
        async with factory() as session:
            ch = await ensure_channel(
                session,
                tg_chat_id=tg_settings.TG_CHANNEL_ID,
                tg_discussion_chat_id=tg_settings.TG_DISCUSSION_CHAT_ID,
                status="active",
            )
            p1 = await upsert_post(
                session,
                channel=ch,
                tg_msg_id=1,
                tg_all_msg_ids=[1],
                date=datetime(2026, 11, 21, 1, 0, tzinfo=UTC),
                text_html="Original caption",
                images=[
                    {
                        "sort_order": 0,
                        "tg_msg_id": 1,
                        "url": f"epochs/{ch.id}/media/1.jpg",
                        "thumb_url": f"epochs/{ch.id}/media/1_thumb.webp",
                        "original_url": f"epochs/{ch.id}/originals/1.png",
                        "original_size": 999999,
                        "width": 4000,
                        "height": 3000,
                        "phash": "abcd1234",
                    }
                ],
            )
            p_deleted = await upsert_post(
                session,
                channel=ch,
                tg_msg_id=2,
                tg_all_msg_ids=[2],
                date=datetime(2026, 11, 21, 1, 2, tzinfo=UTC),
                text_html="Deleted post",
            )
            ch.last_crawled_msg_id = 2
            await session.commit()
            p1_id = p1.id
            p_del_id = p_deleted.id

        dm_msg = SimpleNamespace(
            id=88001,
            from_user=SimpleNamespace(id=tg_settings.OWNER_TG_ID),
            text="/fullsync",
            entities=None,
        )
        await service.handle_owner_dm(dm_msg)

        assert dl_calls == []
        assert any("Full sync (ok):" in m["text"] for m in bot_client.sent_messages)

        async with factory() as session:
            after_p1 = await get_post_by_id(session, p1_id, include_deleted=False)
            assert after_p1 is not None
            assert after_p1.text_html == "Edited photo caption"
            assert after_p1.views == 42
            assert len(after_p1.images) == 1
            assert after_p1.images[0].url.endswith("/media/1.jpg")
            assert after_p1.images[0].original_url.endswith("/originals/1.png")
            assert after_p1.images[0].width == 4000
            assert after_p1.images[0].phash == "abcd1234"

            after_del = await session.get(Post, p_del_id)
            assert after_del is not None and after_del.is_deleted is True
    finally:
        await service.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_comment_reaction_sync_message_reaction_and_reconciler(
    db_factory: async_sessionmaker[AsyncSession],
    tg_settings: Settings,
) -> None:
    cfg = tg_settings.model_copy(
        update={
            "TG_CHANNEL_ID": -100777100,
            "TG_DISCUSSION_CHAT_ID": -100777200,
        }
    )
    sse_bus = SSEBus()
    sub_id, sse_q, _ = sse_bus.subscribe()

    bot_client = FakeTgClient(bot_id=999000)
    service = TelegramService(
        session_factory=db_factory,
        settings=cfg,
        sse_publish=sse_bus.publish,
        client=bot_client,
    )
    try:
        async with db_factory() as session:
            ch = await ensure_channel(
                session,
                tg_chat_id=cfg.TG_CHANNEL_ID,
                tg_discussion_chat_id=cfg.TG_DISCUSSION_CHAT_ID,
                status="active",
            )
            post = await upsert_post(
                session,
                channel=ch,
                tg_msg_id=10,
                tg_all_msg_ids=[10],
                date=datetime(2026, 11, 21, 1, 0, tzinfo=UTC),
                text_html="Post 10",
            )
            c = Comment(
                post_id=post.id,
                channel_id=ch.id,
                source="web",
                tg_msg_id=501,
                body_text="hi",
                body_html="<p>hi</p>",
                created_at=datetime(2026, 11, 21, 1, 1, tzinfo=UTC),
            )
            session.add(c)
            await session.commit()
            c_id = c.id

        # 1. Per-user delta fallback (no user_client) from UpdateBotMessageReaction
        disc_chat = SimpleNamespace(id=cfg.TG_DISCUSSION_CHAT_ID)
        await service.handle_message_reaction_update(
            SimpleNamespace(
                chat=disc_chat,
                message_id=501,
                old_reaction=[],
                new_reaction=[SimpleNamespace(emoji="❤", custom_emoji_id=None)],
            )
        )
        events = _drain_bus(sse_q)
        assert any(
            ev.event == "comment.reaction.updated"
            and ev.data["comment_id"] == c_id
            and ev.data["reactions"][0]["emoji"] == "❤"
            and ev.data["reactions"][0]["count"] == 1
            for ev in events
        )

        # 2. PeriodicReconciler picks up authoritative discussion reactions
        user_client = FakeTgClient(bot_id=888000)
        ch_obj = SimpleNamespace(id=cfg.TG_CHANNEL_ID)
        user_client.chat_map[cfg.TG_CHANNEL_ID] = SimpleNamespace(
            id=cfg.TG_CHANNEL_ID, title="Test", is_banned=False, is_restricted=False, is_deactivated=False
        )
        user_client.messages_by_chat[cfg.TG_CHANNEL_ID] = {
            10: SimpleNamespace(
                id=10,
                empty=False,
                chat=ch_obj,
                date=datetime(2026, 11, 21, 1, 0, tzinfo=UTC),
                text="Post 10",
                views=10,
            )
        }
        user_client.messages_by_chat[cfg.TG_DISCUSSION_CHAT_ID] = {
            501: SimpleNamespace(
                id=501,
                empty=False,
                chat=disc_chat,
                reactions=SimpleNamespace(
                    reactions=[
                        SimpleNamespace(emoji="❤", custom_emoji_id=None, count=2),
                        SimpleNamespace(emoji="🔥", custom_emoji_id=None, count=1),
                    ]
                ),
            )
        }
        service.user_client = user_client
        reconciler = PeriodicReconciler(
            session_factory=db_factory,
            settings=cfg,
            client=user_client,
            telegram_service=service,
            sse_publish=sse_bus.publish,
        )
        stats = await reconciler.reconcile_once()
        assert stats["updated_comment_reactions"] == 1
        events2 = _drain_bus(sse_q)
        assert any(
            ev.event == "comment.reaction.updated"
            and ev.data["comment_id"] == c_id
            and len(ev.data["reactions"]) == 2
            for ev in events2
        )
    finally:
        sse_bus.unsubscribe(sub_id)
        await service.stop()


@pytest.mark.asyncio
async def test_sticker_manifest_file_id_and_bridge_native_sticker(
    db_factory: async_sessionmaker[AsyncSession],
    tg_settings: Settings,
    tmp_path: Path,
) -> None:
    from pyrogram import enums

    from app.media.stickers import list_sticker_packs, lookup_sticker_tg_file_id
    from app.tg.bridge import TelegramBridge

    cfg = tg_settings.model_copy(
        update={
            "DATA_DIR": tmp_path / "data",
            "TG_CHANNEL_ID": -100777100,
            "TG_DISCUSSION_CHAT_ID": -100777200,
        }
    )

    sent_stickers: list[dict[str, Any]] = []

    class _StickerClient(FakeTgClient):
        async def get_sticker_set(self, name: str) -> Any:
            return SimpleNamespace(
                name="CatPack",
                title="Cat Pack",
                sticker_type=enums.StickerType.REGULAR,
                stickers=[
                    SimpleNamespace(
                        file_id="CAACAgIAAxkBAAIBZ2fStickerFileId123",
                        file_unique_id="u_cat_1",
                        emoji="😺",
                        width=512,
                        height=512,
                    )
                ],
            )

        async def send_sticker(self, chat_id: int, sticker: str, **kwargs: Any) -> Any:
            sent_stickers.append({"chat_id": chat_id, "sticker": sticker, **kwargs})
            self.next_msg_id += 1
            return SimpleNamespace(id=self.next_msg_id)

    client = _StickerClient(bot_id=999000)
    client.downloads["CAACAgIAAxkBAAIBZ2fStickerFileId123"] = b"RIFFxxxxWEBP"
    service = TelegramService(session_factory=db_factory, settings=cfg, client=client)

    try:
        ok, _ = await service.import_sticker_set("CatPack")
        assert ok

        assert (
            lookup_sticker_tg_file_id(cfg.media_dir, "/media/stickers/CatPack/u_cat_1.webp")
            == "CAACAgIAAxkBAAIBZ2fStickerFileId123"
        )
        packs = list_sticker_packs(cfg.media_dir)
        assert "file_id" not in packs[0]["stickers"][0]

        async with db_factory() as session:
            ch = await ensure_channel(
                session,
                tg_chat_id=cfg.TG_CHANNEL_ID,
                tg_discussion_chat_id=cfg.TG_DISCUSSION_CHAT_ID,
                status="active",
            )
            post = await upsert_post(
                session,
                channel=ch,
                tg_msg_id=10,
                tg_all_msg_ids=[10],
                date=datetime(2026, 11, 21, 1, 0, tzinfo=UTC),
                text_html="Post 10",
            )
            post.tg_discussion_root_id = 200
            c = Comment(
                post_id=post.id,
                channel_id=ch.id,
                source="web",
                anon_tag="#abcd",
                body_text="",
                body_html="",
                created_at=datetime(2026, 11, 21, 1, 1, tzinfo=UTC),
                media={
                    "media_type": "sticker",
                    "url": "stickers/CatPack/u_cat_1.webp",
                    "sticker_id": "CatPack/u_cat_1.webp",
                    "emoji": "😺",
                },
            )
            session.add(c)
            await session.commit()
            c_id = c.id

        bridge = TelegramBridge(session_factory=db_factory, settings=cfg, client=client)
        bridged_id = await bridge.bridge_web_comment(c_id)
        assert bridged_id is not None
        assert len(sent_stickers) == 1
        assert sent_stickers[0]["sticker"] == "CAACAgIAAxkBAAIBZ2fStickerFileId123"
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_custom_emoji_meta_unicode_alt(tmp_path: Path, tg_settings: Settings) -> None:
    from app.media.stickers import list_custom_emojis
    from app.tg.reactions_cmd import cache_custom_emoji_stickers

    cfg = tg_settings.model_copy(update={"DATA_DIR": tmp_path / "data"})
    emoji_dir = cfg.exports_dir / "emoji"
    emoji_dir.mkdir(parents=True, exist_ok=True)
    # Pre-existing cached emoji file missing .emoji_meta.json
    (emoji_dir / "5555555555555555.webp").write_bytes(b"RIFF")

    client = FakeTgClient(bot_id=999000)

    async def _get_custom(ids: list[int]) -> list[Any]:
        out = []
        for cid in ids:
            if cid == 5555555555555555:
                out.append(SimpleNamespace(custom_emoji_id=cid, emoji="🔥", is_animated=False, is_video=False))
            elif cid == 6666666666666666:
                out.append(SimpleNamespace(custom_emoji_id=cid, emoji="❤️", is_animated=False, is_video=False))
        return out

    async def _dl(sticker: Any, *, file_name: str) -> str:
        Path(file_name).write_bytes(b"WEBP")
        return file_name

    client.get_custom_emoji_stickers = _get_custom  # type: ignore[method-assign]
    client.download_media = _dl  # type: ignore[method-assign]

    await cache_custom_emoji_stickers(
        client, cfg.exports_dir, ["5555555555555555", "6666666666666666"]
    )
    items = list_custom_emojis(cfg)
    by_id = {it["id"]: it for it in items}
    assert by_id["5555555555555555"]["alt"] == "🔥"
    assert by_id["6666666666666666"]["alt"] == "❤️"


@pytest.mark.asyncio
async def test_web_page_link_preview_extraction_and_disabled_guard(
    tg_settings: Settings,
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only active Telegram WebPage previews are extracted; disabled/empty/unsafe ones are omitted."""
    from io import BytesIO

    from PIL import Image
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.models import Poll, Post
    from app.db.repo import serialize_post

    client = FakeTgClient(bot_id=999000)

    async def _dl(target: Any, *, file_name: str) -> str:
        buf = BytesIO()
        Image.new("RGB", (64, 36), (220, 80, 60)).save(buf, format="JPEG")
        Path(file_name).write_bytes(buf.getvalue())
        return file_name

    client.download_media = _dl  # type: ignore[method-assign]
    service = TelegramService(settings=tg_settings, session_factory=db_factory, client=client)

    try:
        async with db_factory() as session:
            channel = await ensure_channel(
                session,
                tg_chat_id=tg_settings.TG_CHANNEL_ID,
                tg_discussion_chat_id=tg_settings.TG_DISCUSSION_CHAT_ID,
                status="active",
            )
            await session.commit()
            channel_id = channel.id

        active_large_msg = SimpleNamespace(
            id=29,
            date=datetime(2026, 11, 24, 10, 0, tzinfo=UTC),
            edit_date=None,
            text="Check this video https://www.youtube.com/watch?v=M8-vje-bq9c",
            entities=[],
            caption=None,
            caption_entities=None,
            photo=None,
            media_group_id=None,
            show_caption_above_media=False,
            link_preview_options=SimpleNamespace(is_disabled=False, prefer_large_media=True),
            web_page=SimpleNamespace(
                id=123456,
                url="https://www.youtube.com/watch?v=M8-vje-bq9c",
                display_url="youtube.com/watch?v=M8-vje-bq9c",
                type="video",
                site_name="YouTube",
                title="Cute Demo Video",
                description="A longer description for testing link preview rendering.",
                author=None,
                has_large_media=True,
                prefer_large_media=True,
                prefer_small_media=False,
                photo=SimpleNamespace(
                    file_id="wp_photo_29",
                    file_unique_id="wp_uid_29",
                    width=1280,
                    height=720,
                    thumbs=[],
                ),
                raw=None,
            ),
        )
        disabled_msg = SimpleNamespace(
            id=28,
            date=datetime(2026, 11, 24, 9, 0, tzinfo=UTC),
            edit_date=None,
            text="Disabled preview https://hydev.org/lol/nenrei.html",
            entities=[],
            caption=None,
            caption_entities=None,
            photo=None,
            media_group_id=None,
            show_caption_above_media=False,
            link_preview_options=SimpleNamespace(is_disabled=True),
            web_page=SimpleNamespace(
                id=999,
                url="https://hydev.org/lol/nenrei.html",
                site_name="hydev",
                title="Should Be Ignored",
                photo=None,
                raw=None,
            ),
        )
        empty_raw_cls = type("WebPageEmpty", (), {})
        empty_msg = SimpleNamespace(
            id=30,
            date=datetime(2026, 11, 24, 11, 0, tzinfo=UTC),
            edit_date=None,
            text="No preview yet https://example.com",
            entities=[],
            caption=None,
            caption_entities=None,
            photo=None,
            media_group_id=None,
            show_caption_above_media=False,
            link_preview_options=SimpleNamespace(is_disabled=False),
            web_page=SimpleNamespace(
                id=0,
                url="https://example.com",
                site_name="Example",
                title="Empty",
                photo=None,
                raw=SimpleNamespace(webpage=empty_raw_cls()),
            ),
        )
        unsafe_url_msg = SimpleNamespace(
            id=31,
            date=datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
            edit_date=None,
            text="Bad link",
            entities=[],
            caption=None,
            caption_entities=None,
            photo=None,
            media_group_id=None,
            show_caption_above_media=False,
            link_preview_options=None,
            web_page=SimpleNamespace(
                id=1,
                url="javascript:alert(1)",
                site_name="XSS",
                title="Bad",
                photo=None,
                raw=None,
            ),
        )

        ingested = await service._ingest_channel_messages(
            [disabled_msg, active_large_msg, empty_msg, unsafe_url_msg],
            is_edit=False,
            trigger_prompt=False,
        )
        by_tg = {p.tg_msg_id: p for p in ingested}
        assert by_tg[28].files is None
        assert by_tg[30].files is None
        assert by_tg[31].files is None

        wp_files = by_tg[29].files
        assert wp_files is not None and len(wp_files) == 1
        wp = wp_files[0]
        assert wp["media_type"] == "web_page"
        assert wp["url"] == "https://www.youtube.com/watch?v=M8-vje-bq9c"
        assert wp["site_name"] == "YouTube"
        assert wp["title"] == "Cute Demo Video"
        assert wp["prefer_large_media"] is True
        assert wp["has_large_media"] is True
        assert wp["thumb"].endswith("_thumb.webp")
        thumb_rel = wp["thumb"].removeprefix(f"epochs/{channel_id}/media/")
        assert (tg_settings.media_dir / "epochs" / str(channel_id) / "media" / thumb_rel).is_file()

        async with db_factory() as session:
            db_post = (
                await session.execute(
                    select(Post)
                    .options(
                        selectinload(Post.images),
                        selectinload(Post.tg_reactions),
                        selectinload(Post.web_reactions),
                        selectinload(Post.poll).selectinload(Poll.options),
                        selectinload(Post.poll).selectinload(Poll.web_votes),
                    )
                    .where(Post.tg_msg_id == 29)
                )
            ).scalar_one()
            dto = serialize_post(db_post, api_prefix="/api/life")
            assert dto["files"][0]["url"] == "https://www.youtube.com/watch?v=M8-vje-bq9c"
            assert dto["files"][0]["thumb"].startswith(f"/api/life/media/epochs/{channel_id}/media/")

        # Editing msg 29 to disable link preview clears web_page cleanly
        edited_disabled = SimpleNamespace(
            **{
                **active_large_msg.__dict__,
                "edit_date": datetime(2026, 11, 24, 12, 30, tzinfo=UTC),
                "link_preview_options": SimpleNamespace(is_disabled=True),
                "web_page": None,
            }
        )
        await service._ingest_channel_messages([edited_disabled], is_edit=True, trigger_prompt=False)
        async with db_factory() as session:
            refreshed = (
                await session.execute(select(Post).where(Post.tg_msg_id == 29))
            ).scalar_one()
            assert refreshed.files is None

        # PeriodicReconciler backfills web_page onto existing preview-less post #29 cleanly
        # using reconciler's own client (so file_reference matches the fetching client)
        reconcile_client = FakeTgClient(bot_id=888000, bot_username="reader_user")
        reconcile_client.download_media = _dl  # type: ignore[method-assign]
        reconcile_client.chat_map[tg_settings.TG_CHANNEL_ID] = SimpleNamespace(
            id=tg_settings.TG_CHANNEL_ID,
            title="Test Channel",
            is_banned=False,
            is_restricted=False,
            is_deactivated=False,
        )
        reconcile_client.messages_by_chat[tg_settings.TG_CHANNEL_ID] = {
            28: disabled_msg,
            29: active_large_msg,
            30: empty_msg,
            31: unsafe_url_msg,
        }

        async def _fail_bot_dl(*args: Any, **kwargs: Any) -> str:
            raise AssertionError("Reconciler must use fetching client to download wp thumbnail")

        client.download_media = _fail_bot_dl  # type: ignore[method-assign]
        reconciler = PeriodicReconciler(
            session_factory=db_factory,
            settings=tg_settings,
            client=reconcile_client,
            telegram_service=service,
        )
        await reconciler.reconcile_once()
        async with db_factory() as session:
            backfilled_29 = (
                await session.execute(select(Post).where(Post.tg_msg_id == 29))
            ).scalar_one()
            still_disabled_28 = (
                await session.execute(select(Post).where(Post.tg_msg_id == 28))
            ).scalar_one()
            assert backfilled_29.files is not None and len(backfilled_29.files) == 1
            assert backfilled_29.files[0]["media_type"] == "web_page"
            assert backfilled_29.files[0]["thumb"].endswith("_thumb.webp")
            assert still_disabled_28.files is None
    finally:
        await service.stop()


