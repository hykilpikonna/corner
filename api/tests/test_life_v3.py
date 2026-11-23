from __future__ import annotations

import asyncio
import hashlib
import io
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from pyrogram.enums import ParseMode
from pyrogram.handlers import DeletedMessagesHandler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import (
    SESSION_COOKIE_NAME,
    VISITOR_COOKIE_NAME,
    create_session,
    render_and_sanitize_comment_html,
    sanitize_comment_html,
)
from app.config import Settings
from app.db.models import Comment, Post, PostView, User
from app.db.repo import (
    compute_merged_views,
    ensure_channel,
    get_post_by_id,
    serialize_post,
    upsert_post,
)
from app.db.session import create_engine_and_factory, get_db_session
from app.main import create_app
from app.sse import SSEEvent
from app.tg.bridge import TelegramBridge
from app.tg.client import TelegramService, _render_entities_to_html
from app.tg.sync import PeriodicReconciler


def _solve_altcha(challenge_payload: dict[str, Any]) -> str:
    import base64

    salt = str(challenge_payload["salt"])
    target = str(challenge_payload["challenge"])
    max_num = int(challenge_payload["maxnumber"])
    for n in range(max_num + 1):
        if hashlib.sha256(f"{salt}{n}".encode()).hexdigest() == target:
            raw = json.dumps(
                {
                    "algorithm": challenge_payload["algorithm"],
                    "challenge": target,
                    "number": n,
                    "salt": salt,
                    "signature": challenge_payload["signature"],
                }
            )
            return base64.b64encode(raw.encode()).decode()
    raise AssertionError("Failed to solve ALTCHA challenge")


def _make_png_bytes(size: tuple[int, int] = (120, 80), color: tuple[int, int, int] = (40, 140, 220)) -> bytes:
    im = Image.new("RGB", size, color)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _make_jpeg_with_exif_orientation(orientation: int = 6) -> bytes:
    # 80x40 image with orientation=6 (rotate 90 CW -> oriented 40x80)
    im = Image.new("RGB", (80, 40), (200, 90, 40))
    exif = im.getexif()
    exif[274] = orientation
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif.tobytes(), quality=90)
    return buf.getvalue()


def _make_webp_bytes(size: tuple[int, int] = (256, 256)) -> bytes:
    im = Image.new("RGBA", size, (100, 200, 150, 255))
    buf = io.BytesIO()
    im.save(buf, "WEBP")
    return buf.getvalue()


async def _setup_app_and_client(
    cfg: Settings,
    db_session: AsyncSession,
) -> tuple[Any, AsyncClient]:
    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    cfg.exports_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(cfg)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _override_session
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="https://aza.moe")
    return app, client


async def _seed_user(
    db: AsyncSession,
    *,
    display_name: str,
    role: str = "user",
) -> tuple[User, str]:
    user = User(
        display_name=display_name,
        email=f"{display_name.lower()}@example.com",
        email_verified=True,
        role=role,
    )
    db.add(user)
    await db.flush()
    token = await create_session(db, user.id)
    await db.commit()
    return user, token


def _drain_sse(q: asyncio.Queue[SSEEvent]) -> list[SSEEvent]:
    items: list[SSEEvent] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


@dataclass
class _V3FakeTgClient:
    bot_id: int = 999000
    bot_username: str = "v3_test_bot"
    next_msg_id: int = 9000
    sent_messages: list[dict[str, Any]] = field(default_factory=list)
    sent_photos: list[dict[str, Any]] = field(default_factory=list)
    sent_stickers: list[dict[str, Any]] = field(default_factory=list)
    deleted_calls: list[tuple[int, list[int]]] = field(default_factory=list)
    downloads: dict[str, bytes] = field(default_factory=dict)
    handlers: list[Any] = field(default_factory=list)
    chat_map: dict[int, Any] = field(default_factory=dict)
    messages_by_chat: dict[int, dict[int, Any]] = field(default_factory=dict)
    fail_html_send: bool = False
    fail_delete: bool = False

    def add_handler(self, handler: Any) -> None:
        self.handlers.append(handler)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def get_me(self) -> Any:
        return SimpleNamespace(id=self.bot_id, username=self.bot_username, is_bot=True)

    async def get_chat(self, chat_id: int) -> Any:
        return self.chat_map[chat_id]

    async def get_messages(self, chat_id: int, message_ids: list[int] | int) -> list[Any]:
        ids = [message_ids] if isinstance(message_ids, int) else list(message_ids)
        store = self.messages_by_chat.get(chat_id, {})
        return [store.get(mid, SimpleNamespace(id=mid, empty=True)) for mid in ids]

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: Any | None = None,
        disable_web_page_preview: bool | None = None,
        reply_to_message_id: int | None = None,
    ) -> Any:
        if self.fail_html_send and parse_mode == ParseMode.HTML:
            raise RuntimeError("Simulated Telegram HTML parse error")
        self.next_msg_id += 1
        rec = {
            "id": self.next_msg_id,
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
            "reply_to_message_id": reply_to_message_id,
        }
        self.sent_messages.append(rec)
        return SimpleNamespace(id=self.next_msg_id)

    async def send_photo(
        self,
        chat_id: int,
        photo: str,
        *,
        caption: str | None = None,
        parse_mode: Any | None = None,
        reply_to_message_id: int | None = None,
    ) -> Any:
        if self.fail_html_send and parse_mode == ParseMode.HTML:
            raise RuntimeError("Simulated HTML caption error")
        self.next_msg_id += 1
        rec = {
            "id": self.next_msg_id,
            "chat_id": chat_id,
            "photo": photo,
            "caption": caption,
            "parse_mode": parse_mode,
            "reply_to_message_id": reply_to_message_id,
        }
        self.sent_photos.append(rec)
        return SimpleNamespace(id=self.next_msg_id)

    async def send_sticker(
        self,
        chat_id: int,
        sticker: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> Any:
        self.next_msg_id += 1
        rec = {
            "id": self.next_msg_id,
            "chat_id": chat_id,
            "sticker": sticker,
            "reply_to_message_id": reply_to_message_id,
        }
        self.sent_stickers.append(rec)
        return SimpleNamespace(id=self.next_msg_id)

    async def delete_messages(self, chat_id: int, message_ids: list[int]) -> None:
        if self.fail_delete:
            raise RuntimeError("Simulated delete error")
        self.deleted_calls.append((chat_id, list(message_ids)))

    async def download_media(self, message_or_file_id: Any, *, file_name: str) -> str | None:
        key = message_or_file_id if isinstance(message_or_file_id, str) else f"msg:{getattr(message_or_file_id, 'id', '')}"
        payload = self.downloads.get(key)
        if payload is None:
            return None
        dest = Path(file_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        return str(dest)


# ---------------------------------------------------------------------------
# 1. Website View Count Increment, 24h Dedup, Reconciler Preservation, SSE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_views_dedup_merged_views_sse_and_reconciler_preservation(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    ch = await ensure_channel(
        db_session,
        channel_id=1,
        tg_chat_id=-1002000002,
        status="active",
        id_offset=0,
    )
    p1 = await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=10,
        canonical_id=10,
        date=datetime(2026, 11, 19, 10, 0, tzinfo=UTC),
        text_html="Post 10",
        views=100,
    )
    p2 = await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=11,
        canonical_id=11,
        date=datetime(2026, 11, 19, 10, 5, tzinfo=UTC),
        text_html="Post 11 (no TG views)",
        views=None,
    )
    p_del = await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=12,
        canonical_id=12,
        date=datetime(2026, 11, 19, 10, 10, tzinfo=UTC),
        text_html="Deleted post",
        is_deleted=True,
        views=50,
    )
    await db_session.commit()

    p1_loaded = await get_post_by_id(db_session, p1.id)
    p2_loaded = await get_post_by_id(db_session, p2.id)
    assert p1_loaded is not None and p2_loaded is not None
    assert compute_merged_views(p1_loaded) == "100"
    assert compute_merged_views(p2_loaded) is None
    assert serialize_post(p1_loaded)["views"] == "100"
    assert "views" not in serialize_post(p2_loaded)

    app, client = await _setup_app_and_client(test_settings, db_session)
    # Subscriber listening to an unrelated post_id={999} must STILL receive post.views.updated
    sub_id, queue, _ = app.state.sse_bus.subscribe(post_ids={999})

    try:
        async with client:
            # First view from Visitor A on [10, 10, 11, 12(deleted), 9999(unknown), -1]
            r1 = await client.post(
                "/api/life/posts/view",
                json={"post_ids": [p1.id, p1.id, p2.id, p_del.id, 9999, -1]},
            )
            assert r1.status_code == 200
            assert r1.json() == {"views": {"10": "101", "11": "1"}}
            assert VISITOR_COOKIE_NAME in r1.cookies

            ev1 = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert ev1.event == "post.views.updated"
            assert ev1.post_id is None
            assert ev1.data == {"views": {"10": "101", "11": "1"}}

            # Immediate repeat by same Visitor A within 24h -> NO increment, NO SSE event
            r2 = await client.post("/api/life/posts/view", json={"post_ids": [10, 11]})
            assert r2.status_code == 200
            assert r2.json() == {"views": {"10": "101", "11": "1"}}
            assert queue.empty()

            # Cookieless bot guard: r1 was cookieless and consumed the 1/hour view-nc slot for this IP,
            # so subsequent cookieless POSTs from the same IP do NOT inflate web_views!
            client.cookies.clear()
            r_nc1 = await client.post("/api/life/posts/view", json={"post_ids": [10]})
            assert r_nc1.status_code == 200
            assert r_nc1.json() == {"views": {"10": "101"}}
            assert queue.empty()

            # Visitor B arriving with a valid cookie (from /interactions) increments cleanly
            client.cookies.clear()
            await client.get("/api/life/interactions?ids=10")
            r3 = await client.post("/api/life/posts/view", json={"post_ids": [10]})
            assert r3.status_code == 200
            assert r3.json() == {"views": {"10": "102"}}
            ev2 = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert ev2.event == "post.views.updated"
            assert ev2.data == {"views": {"10": "102"}}

            # Simulate >24h elapsed for Visitor B -> re-viewing increments again
            pv_rows = list((await db_session.execute(select(PostView))).scalars().all())
            for pv in pv_rows:
                pv.viewed_at = datetime.now(UTC) - timedelta(hours=25)
            await db_session.commit()

            r4 = await client.post("/api/life/posts/view", json={"post_ids": [10]})
            assert r4.status_code == 200
            assert r4.json() == {"views": {"10": "103"}}
            ev3 = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert ev3.data == {"views": {"10": "103"}}

            # GET /interactions reflects merged views
            r_int = await client.get("/api/life/interactions?ids=10,11")
            assert r_int.status_code == 200
            posts_int = r_int.json()["posts"]
            assert posts_int["10"]["views"] == "103"
            assert posts_int["11"]["views"] == "1"

            # Batch cap: > 50 post_ids truncates cleanly to 50
            big_ids = list(range(1, 80))
            r_big = await client.post("/api/life/posts/view", json={"post_ids": big_ids})
            assert r_big.status_code == 200

            # Rate limit: 60/min IP bucket "view"
            got_429 = False
            for _ in range(60):
                rl = await client.post("/api/life/posts/view", json={"post_ids": []})
                if rl.status_code == 429:
                    got_429 = True
                    break
            assert got_429
    finally:
        app.state.sse_bus.unsubscribe(sub_id)

    # PeriodicReconciler updating TG views must NOT overwrite web_views!
    engine, factory = create_engine_and_factory(test_settings)
    try:
        fake_tg = _V3FakeTgClient()
        fake_tg.chat_map[-1002000002] = SimpleNamespace(id=-1002000002, title="Epoch")
        fake_tg.messages_by_chat[-1002000002] = {
            10: SimpleNamespace(
                id=10,
                views=250,
                forwards=0,
                date=datetime(2026, 11, 19, 10, 0, tzinfo=UTC),
                text="Post 10",
            ),
            11: SimpleNamespace(
                id=11,
                views=5,
                forwards=0,
                date=datetime(2026, 11, 19, 10, 5, tzinfo=UTC),
                text="Post 11",
            ),
        }
        reconciler = PeriodicReconciler(
            session_factory=factory,
            settings=test_settings,
            client=fake_tg,
        )
        stats = await reconciler.reconcile_once()
        assert stats["status"] == "ok"

        async with factory() as verify_sess:
            p10_after = await verify_sess.get(Post, 10)
            p11_after = await verify_sess.get(Post, 11)
            assert p10_after is not None and p10_after.views == 250 and p10_after.web_views == 3
            assert p11_after is not None and p11_after.views == 5 and p11_after.web_views == 2
            assert compute_merged_views(p10_after) == "253"
            assert compute_merged_views(p11_after) == "7"
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 2. Delete Own Messages (Registered + Anonymous + 2-Way TG Sync)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anonymous_and_incognito_delete_own_and_two_way_tg_delete(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    cfg = test_settings.model_copy(
        update={
            "ALTCHA_MAX_NUMBER": 50,
            "TG_CHANNEL_ID": -1002000002,
            "TG_DISCUSSION_CHAT_ID": -1003000003,
        }
    )
    ch = await ensure_channel(
        db_session,
        channel_id=1,
        tg_chat_id=cfg.TG_CHANNEL_ID,
        tg_discussion_chat_id=cfg.TG_DISCUSSION_CHAT_ID,
        status="active",
        id_offset=0,
    )
    await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=100,
        canonical_id=100,
        tg_discussion_root_id=8000,
        date=datetime(2026, 11, 19, 10, 0, tzinfo=UTC),
        text_html="Post 100",
    )
    bob, bob_token = await _seed_user(db_session, display_name="Bob")

    app, client = await _setup_app_and_client(cfg, db_session)
    fake_tg = _V3FakeTgClient()
    engine, factory = create_engine_and_factory(cfg)
    bridge = TelegramBridge(session_factory=factory, settings=cfg, client=fake_tg)
    service = TelegramService(
        session_factory=factory,
        settings=cfg,
        sse_publish=app.state.sse_bus.publish,
        bridge=bridge,
        client=fake_tg,
        bot_id=999000,
    )
    app.state.tg_bridge = bridge

    sub_id, queue, _ = app.state.sse_bus.subscribe(post_ids={100})
    try:
        async with client:
            # 1. Anonymous visitor creates comment (gets actor_key_hash + life_vid cookie)
            ch1 = (await client.get("/api/life/captcha/challenge")).json()
            r_anon = await client.post(
                "/api/life/posts/100/comments",
                json={"body": "Anon own message", "altcha_payload": _solve_altcha(ch1)},
            )
            assert r_anon.status_code == 201
            anon_comment = r_anon.json()
            assert anon_comment["is_mine"] is True
            assert anon_comment["can_delete"] is True
            anon_cookie = r_anon.cookies.get(VISITOR_COOKIE_NAME)
            assert anon_cookie is not None

            # Wait for background bridge -> assigns tg_msg_id
            await asyncio.sleep(0.1)
            assert len(fake_tg.sent_messages) == 1
            bridged_tg_msg_id = fake_tg.sent_messages[0]["id"]

            # Drain comment.created
            ev_created = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert ev_created.event == "comment.created"

            # 2. Completely unauthenticated caller (no user, no visitor cookie) -> 401
            client.cookies.clear()
            r_unauth = await client.delete(f"/api/life/comments/{anon_comment['id']}")
            assert r_unauth.status_code == 401

            # 3. Different logged-in user (Bob) -> 403
            client.cookies.set(SESSION_COOKIE_NAME, bob_token)
            r_bob_del = await client.delete(f"/api/life/comments/{anon_comment['id']}")
            assert r_bob_del.status_code == 403

            # 4. Different visitor cookie -> 403
            client.cookies.clear()
            r_mint = await client.get("/api/life/interactions?ids=100")
            other_cookie = r_mint.cookies.get(VISITOR_COOKIE_NAME)
            assert other_cookie is not None
            client.cookies.set(VISITOR_COOKIE_NAME, other_cookie)
            r_other_vid = await client.delete(f"/api/life/comments/{anon_comment['id']}")
            assert r_other_vid.status_code == 403

            # 5. Original anonymous visitor deletes own comment -> 200 + SSE + TG delete_messages!
            client.cookies.clear()
            client.cookies.set(VISITOR_COOKIE_NAME, anon_cookie)
            r_self_del = await client.delete(f"/api/life/comments/{anon_comment['id']}")
            assert r_self_del.status_code == 200
            ev_del = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert ev_del.event == "comment.deleted"
            assert ev_del.data == {"post_id": 100, "comment_id": anon_comment["id"]}

            await asyncio.sleep(0.1)
            assert fake_tg.deleted_calls == [(cfg.TG_DISCUSSION_CHAT_ID, [bridged_tg_msg_id])]

            # 6. Registered user posting as_anonymous=True can also delete own incognito comment
            client.cookies.clear()
            client.cookies.set(SESSION_COOKIE_NAME, bob_token)
            ch2 = (await client.get("/api/life/captcha/challenge")).json()
            r_incog = await client.post(
                "/api/life/posts/100/comments",
                json={
                    "body": "Bob incognito delete test",
                    "as_anonymous": True,
                    "altcha_payload": _solve_altcha(ch2),
                },
            )
            assert r_incog.status_code == 201
            incog_id = r_incog.json()["id"]
            assert r_incog.json()["is_mine"] is True
            assert r_incog.json()["can_delete"] is True
            assert (await client.delete(f"/api/life/comments/{incog_id}")).status_code == 200
            _drain_sse(queue)

            # 7. TG -> Web 2-way deletion: DeletedMessagesHandler + handle_discussion_deleted
            service._register_handlers()
            assert any(isinstance(h, DeletedMessagesHandler) for h in fake_tg.handlers)

            tg_comment_msg = SimpleNamespace(
                id=8123,
                chat=SimpleNamespace(id=cfg.TG_DISCUSSION_CHAT_ID),
                from_user=SimpleNamespace(id=555111, first_name="TgUser"),
                reply_to_top_message_id=8000,
                reply_to_message_id=8000,
                text="Comment from TG to delete",
                date=datetime(2026, 11, 19, 10, 30, tzinfo=UTC),
            )
            saved_tg = await service.handle_discussion_message(tg_comment_msg)
            assert saved_tg is not None
            _drain_sse(queue)

            deleted_ids = await service.handle_discussion_deleted(
                [SimpleNamespace(id=8123, chat=SimpleNamespace(id=cfg.TG_DISCUSSION_CHAT_ID))]
            )
            assert deleted_ids == [saved_tg.id]
            ev_tg_del = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert ev_tg_del.event == "comment.deleted"
            assert ev_tg_del.data == {"post_id": 100, "comment_id": saved_tg.id}

            async with factory() as verify_sess:
                c_row = await verify_sess.get(Comment, saved_tg.id)
                assert c_row is not None and c_row.deleted_at is not None
    finally:
        app.state.sse_bus.unsubscribe(sub_id)
        await engine.dispose()


# ---------------------------------------------------------------------------
# 3. Markdown, Custom Emoji Parsing & Strict XSS Sanitization
# ---------------------------------------------------------------------------


def test_markdown_custom_emoji_parsing_and_xss_sanitization() -> None:
    md = (
        "**bold** __italic__ <u>under</u> **<u>nested</u>** ~~strike~~ ||secret||\n"
        "x < y and y > z\n"
        "> quoted line 1\n"
        "> quoted line 2\n\n"
        "[a<b](https://example.com/path__with__underscores?x=1&y=2) "
        '<tg-emoji emoji-id="5368324170671202286">🔥</tg-emoji> '
        "`<script>inline</script>`\n\n"
        "```python\nx = '<script>alert(1)</script>' **not_bold**\n```\n\n"
        "<script>bad()</script><img src=x onerror=alert(1)>"
        '<i class="custom-emoji evil" data-custom-emoji-id="abc123">X</i>'
        '<span class="evil">plain</span>'
    )
    html_out = render_and_sanitize_comment_html(md)

    assert "<b>bold</b>" in html_out
    assert "<i>italic</i>" in html_out
    assert "<u>under</u>" in html_out
    assert "<b><u>nested</u></b>" in html_out
    assert "<s>strike</s>" in html_out
    assert '<span class="spoiler"><span>secret</span></span>' in html_out
    assert "x &lt; y and y &gt; z" in html_out
    assert "<blockquote>quoted line 1<br>quoted line 2</blockquote>" in html_out
    assert 'href="https://example.com/path__with__underscores?x=1&amp;y=2"' in html_out
    assert ">a&lt;b</a>" in html_out
    assert '<i class="custom-emoji" data-custom-emoji-id="5368324170671202286">🔥</i>' in html_out
    assert "<code>&lt;script&gt;inline&lt;/script&gt;</code>" in html_out
    assert '<pre language="python"><code>x = \'&lt;script&gt;alert(1)&lt;/script&gt;\' **not_bold**</code></pre>' in html_out

    assert "<script" not in html_out.lower()
    assert "<img" not in html_out.lower()
    assert "<i class=\"custom-emoji evil\"" not in html_out
    assert "<span class=\"evil\"" not in html_out

    # Nested markdown + spoiler combinations must nest cleanly without corrupting HTML attributes
    assert (
        render_and_sanitize_comment_html("~~||secret||~~")
        == '<p><s><span class="spoiler"><span>secret</span></span></s></p>'
    )
    assert (
        render_and_sanitize_comment_html("||~~secret~~||")
        == '<p><span class="spoiler"><span><s>secret</s></span></span></p>'
    )
    assert (
        render_and_sanitize_comment_html("**||~~secret~~||**")
        == '<p><b><span class="spoiler"><span><s>secret</s></span></span></b></p>'
    )
    assert (
        render_and_sanitize_comment_html("||**__secret__**||")
        == '<p><span class="spoiler"><span><b><i>secret</i></b></span></span></p>'
    )

    # Also verify nh3 attribute_filter directly on raw HTML tags
    sanitized_direct = sanitize_comment_html(
        '<i class="custom-emoji evil" data-custom-emoji-id="abc123">X</i>'
        '<span class="evil">plain</span>'
    )
    assert 'data-custom-emoji-id="abc123"' not in sanitized_direct
    assert 'class="evil"' not in sanitized_direct

    # Telegram entity rendering for CUSTOM_EMOJI
    tg_html = _render_entities_to_html(
        "Hi 🔥!",
        [SimpleNamespace(type="CUSTOM_EMOJI", offset=3, length=2, custom_emoji_id=5368324170671202286)],
    )
    assert '<i class="custom-emoji" data-custom-emoji-id="5368324170671202286">🔥</i>' in tg_html


# ---------------------------------------------------------------------------
# 4. Comment Media Upload + Validation + TG Discussion Media + HTML Bridge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_comment_media_upload_validation_tg_ingestion_and_html_bridge(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    cfg = test_settings.model_copy(
        update={
            "TG_CHANNEL_ID": -1002000002,
            "TG_DISCUSSION_CHAT_ID": -1003000003,
        }
    )
    ch = await ensure_channel(
        db_session,
        channel_id=1,
        tg_chat_id=cfg.TG_CHANNEL_ID,
        tg_discussion_chat_id=cfg.TG_DISCUSSION_CHAT_ID,
        status="active",
        id_offset=0,
    )
    await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=100,
        canonical_id=100,
        tg_discussion_root_id=8000,
        date=datetime(2026, 11, 19, 10, 0, tzinfo=UTC),
        text_html="Post 100",
    )
    _, alice_token = await _seed_user(db_session, display_name="Alice")

    app, client = await _setup_app_and_client(cfg, db_session)
    fake_tg = _V3FakeTgClient()
    engine, factory = create_engine_and_factory(cfg)
    bridge = TelegramBridge(session_factory=factory, settings=cfg, client=fake_tg)
    service = TelegramService(
        session_factory=factory,
        settings=cfg,
        sse_publish=app.state.sse_bus.publish,
        bridge=bridge,
        client=fake_tg,
        bot_id=999000,
    )
    app.state.tg_bridge = bridge

    sub_id, queue, _ = app.state.sse_bus.subscribe(post_ids={100})
    try:
        async with client:
            client.cookies.set(SESSION_COOKIE_NAME, alice_token)

            # Reject SVG and non-image uploads
            r_svg = await client.post(
                "/api/life/comments/media",
                files={"file": ("evil.svg", b"<svg onload=alert(1)></svg>", "image/svg+xml")},
            )
            assert r_svg.status_code == 400

            r_txt = await client.post(
                "/api/life/comments/media",
                files={"file": ("notes.txt", b"not an image", "text/plain")},
            )
            assert r_txt.status_code == 400

            # Reject animated GIF frame-bomb (> 300 frames)
            frames = [Image.new("RGB", (8, 8), (i % 255, 0, 0)) for i in range(305)]
            gif_buf = io.BytesIO()
            frames[0].save(gif_buf, format="GIF", save_all=True, append_images=frames[1:], loop=0)
            r_bomb = await client.post(
                "/api/life/comments/media",
                files={"file": ("bomb.gif", gif_buf.getvalue(), "image/gif")},
            )
            assert r_bomb.status_code == 400

            # Upload EXIF-rotated JPEG -> orientation applied & stripped (80x40 -> 40x80) + _thumb.webp
            rotated_jpg = _make_jpeg_with_exif_orientation(6)
            r_up = await client.post(
                "/api/life/comments/media",
                files={"file": ("photo.jpg", rotated_jpg, "image/jpeg")},
            )
            assert r_up.status_code == 200
            media_dto = r_up.json()
            assert media_dto["media_type"] == "photo"
            assert media_dto["url"].startswith("/api/life/media/comments/web_")
            assert media_dto["thumb"].startswith("/api/life/media/comments/web_")
            assert media_dto["width"] == 40
            assert media_dto["height"] == 80

            # Reject empty body with no media
            r_empty = await client.post("/api/life/posts/100/comments", json={"body": "   "})
            assert r_empty.status_code == 400

            # Reject traversal or external URL in CreateCommentRequest.media
            r_ext = await client.post(
                "/api/life/posts/100/comments",
                json={
                    "body": "bad",
                    "media": {"media_type": "photo", "url": "https://evil.com/x.jpg"},
                },
            )
            assert r_ext.status_code == 400

            r_trav = await client.post(
                "/api/life/posts/100/comments",
                json={
                    "body": "bad",
                    "media": {"media_type": "photo", "url": "../config.toml"},
                },
            )
            assert r_trav.status_code == 400

            # Create media-only photo comment with caption empty -> succeeds & bridges via send_photo!
            r_photo_comment = await client.post(
                "/api/life/posts/100/comments",
                json={"body": "", "media": media_dto},
            )
            assert r_photo_comment.status_code == 201
            pc = r_photo_comment.json()
            assert pc["body_text"] == ""
            assert pc["body_html"] == ""
            assert pc["media"]["url"] == media_dto["url"]

            await asyncio.sleep(0.1)
            assert len(fake_tg.sent_photos) == 1
            assert fake_tg.sent_photos[0]["parse_mode"] == ParseMode.HTML

            # Create formatted Markdown + custom emoji comment -> bridges via ParseMode.HTML
            r_md_comment = await client.post(
                "/api/life/posts/100/comments",
                json={
                    "body": '**Hi** ||secret|| <tg-emoji emoji-id="5368324170671202286">🔥</tg-emoji>'
                },
            )
            assert r_md_comment.status_code == 201
            await asyncio.sleep(0.1)
            sent_html = fake_tg.sent_messages[-1]
            assert sent_html["parse_mode"] == ParseMode.HTML
            assert "<b>Hi</b>" in sent_html["text"]
            assert "<tg-spoiler>secret</tg-spoiler>" in sent_html["text"]
            assert '<tg-emoji emoji-id="5368324170671202286">🔥</tg-emoji>' in sent_html["text"]

            # Verify fallback to plain text when Telegram rejects HTML
            fake_tg.fail_html_send = True
            r_fb = await client.post(
                "/api/life/posts/100/comments",
                json={"body": "**Fallback**"},
            )
            assert r_fb.status_code == 201
            await asyncio.sleep(0.1)
            assert fake_tg.sent_messages[-1]["parse_mode"] == ParseMode.DISABLED
            fake_tg.fail_html_send = False

            # Seed an installed sticker pack on disk, fetch via GET /stickers, and post sticker comment -> bridges via send_sticker!
            demo_pack_dir = cfg.media_dir / "stickers" / "demo_pack"
            demo_pack_dir.mkdir(parents=True, exist_ok=True)
            (demo_pack_dir / "wave.webp").write_bytes(_make_webp_bytes((512, 512)))
            (demo_pack_dir / "manifest.json").write_text(
                '{"name": "demo_pack", "title": "Demo Pack", "stickers": [{"file": "wave.webp", "emoji": "👋", "width": 512, "height": 512}]}',
                encoding="utf-8",
            )
            r_stickers = await client.get("/api/life/stickers")
            assert r_stickers.status_code == 200
            demo_pack = r_stickers.json()["packs"][0]
            assert demo_pack["id"] == "demo_pack"
            assert demo_pack["name"] == "demo_pack"
            first_sticker = demo_pack["stickers"][0]
            assert first_sticker["format"] == "webp"

            # Reject non-sticker extensions (e.g. manifest.json) and photo pointing at stickers/
            r_bad_manifest = await client.post(
                "/api/life/posts/100/comments",
                json={
                    "body": "",
                    "media": {"media_type": "sticker", "url": "stickers/demo_pack/manifest.json"},
                },
            )
            assert r_bad_manifest.status_code == 400

            r_bad_dir = await client.post(
                "/api/life/posts/100/comments",
                json={"body": "", "media": {"media_type": "photo", "url": first_sticker["url"]}},
            )
            assert r_bad_dir.status_code == 400

            r_st_comment = await client.post(
                "/api/life/posts/100/comments",
                json={
                    "body": "Sticker with caption",
                    "media": {
                        "media_type": "sticker",
                        "url": first_sticker["url"],
                        "emoji": first_sticker["emoji"],
                        "width": first_sticker["width"],
                        "height": first_sticker["height"],
                        "is_animated": True,  # spoofed client flag must be overridden from disk (.webp)
                    },
                },
            )
            assert r_st_comment.status_code == 201
            assert r_st_comment.json()["media"]["is_animated"] is False
            await asyncio.sleep(0.1)
            assert len(fake_tg.sent_stickers) == 1
            assert "Sticker with caption" in fake_tg.sent_messages[-1]["text"]

            # Over-limit HTML (> 4096 chars) falls back cleanly to truncated plain text without cutting HTML tags
            r_long = await client.post(
                "/api/life/posts/100/comments",
                json={"body": "**a** " * 600},
            )
            assert r_long.status_code == 201
            await asyncio.sleep(0.1)
            assert fake_tg.sent_messages[-1]["parse_mode"] == ParseMode.DISABLED
            assert len(fake_tg.sent_messages[-1]["text"]) <= 4096

            # Verify rate limit on POST /comments/media (20/min)
            png_bytes = _make_png_bytes()
            hit_429 = False
            for _ in range(21):
                ru = await client.post(
                    "/api/life/comments/media",
                    files={"file": ("a.png", png_bytes, "image/png")},
                )
                if ru.status_code == 429:
                    hit_429 = True
                    break
            assert hit_429

        # TG discussion ingestion: captionless photo -> body_html == "" (no <em>[Photo]</em>!)
        _drain_sse(queue)
        fake_tg.downloads["tg_photo_1"] = _make_png_bytes((200, 100))
        fake_tg.downloads["tg_sticker_1"] = _make_webp_bytes((512, 512))
        disc_chat = SimpleNamespace(id=cfg.TG_DISCUSSION_CHAT_ID)

        tg_photo_msg = SimpleNamespace(
            id=8500,
            chat=disc_chat,
            from_user=SimpleNamespace(id=4242, first_name="TgPhotoUser"),
            reply_to_top_message_id=8000,
            reply_to_message_id=8000,
            photo=SimpleNamespace(file_id="tg_photo_1", width=200, height=100),
            date=datetime(2026, 11, 19, 11, 0, tzinfo=UTC),
        )
        c_tg_photo = await service.handle_discussion_message(tg_photo_msg)
        assert c_tg_photo is not None
        assert c_tg_photo.body_html == ""
        assert c_tg_photo.media is not None
        assert c_tg_photo.media["media_type"] == "photo"
        assert c_tg_photo.media["url"] == "comments/tg_1_8500.jpg"
        assert c_tg_photo.media["thumb"] == "comments/tg_1_8500_thumb.webp"
        assert (cfg.media_dir / c_tg_photo.media["thumb"]).is_file()

        ev_tg_photo = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert ev_tg_photo.data["media"]["url"] == "/api/life/media/comments/tg_1_8500.jpg"
        assert ev_tg_photo.data["media"]["thumb"] == "/api/life/media/comments/tg_1_8500_thumb.webp"

        # TG discussion ingestion: photo WITH caption -> downloads photo AND renders caption
        tg_captioned_msg = SimpleNamespace(
            id=8501,
            chat=disc_chat,
            from_user=SimpleNamespace(id=4242, first_name="TgPhotoUser"),
            reply_to_top_message_id=8000,
            reply_to_message_id=8000,
            caption="Look at this!",
            caption_entities=[SimpleNamespace(type="BOLD", offset=0, length=4)],
            photo=SimpleNamespace(file_id="tg_photo_1", width=200, height=100),
            date=datetime(2026, 11, 19, 11, 1, tzinfo=UTC),
        )
        c_tg_cap = await service.handle_discussion_message(tg_captioned_msg)
        assert c_tg_cap is not None
        assert "<b>Look</b>" in c_tg_cap.body_html
        assert c_tg_cap.media is not None and c_tg_cap.media["media_type"] == "photo"

        # TG discussion ingestion: animated sticker
        tg_sticker_msg = SimpleNamespace(
            id=8502,
            chat=disc_chat,
            from_user=SimpleNamespace(id=4242, first_name="TgPhotoUser"),
            reply_to_top_message_id=8000,
            reply_to_message_id=8000,
            sticker=SimpleNamespace(
                file_id="tg_sticker_1",
                file_unique_id="uid_st_1",
                emoji="🎉",
                is_animated=True,
                is_video=False,
                width=512,
                height=512,
            ),
            date=datetime(2026, 11, 19, 11, 2, tzinfo=UTC),
        )
        c_tg_st = await service.handle_discussion_message(tg_sticker_msg)
        assert c_tg_st is not None
        assert c_tg_st.body_html == ""
        assert c_tg_st.media is not None
        assert c_tg_st.media["media_type"] == "sticker"
        assert c_tg_st.media["is_animated"] is True
        assert c_tg_st.media["emoji"] == "🎉"

        # Captionless photo whose download fails must be ignored cleanly (no {"url": ""})
        tg_missing_photo = SimpleNamespace(
            id=8503,
            chat=disc_chat,
            from_user=SimpleNamespace(id=4242, first_name="TgPhotoUser"),
            reply_to_top_message_id=8000,
            reply_to_message_id=8000,
            photo=SimpleNamespace(file_id="missing_photo_id", width=100, height=100),
            date=datetime(2026, 11, 19, 11, 3, tzinfo=UTC),
        )
        assert await service.handle_discussion_message(tg_missing_photo) is None
    finally:
        app.state.sse_bus.unsubscribe(sub_id)
        await engine.dispose()


# ---------------------------------------------------------------------------
# 5. Custom Emoji & Sticker Catalogs + Owner TG DM Sticker Management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_emoji_sticker_catalogs_and_owner_dm_management(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    cfg = test_settings.model_copy(update={"OWNER_TG_ID": 777001})
    (cfg.exports_dir / "emoji").mkdir(parents=True, exist_ok=True)
    (cfg.exports_dir / "emoji" / "111222.webp").write_bytes(_make_webp_bytes((64, 64)))
    (cfg.media_dir / "epochs" / "2" / "emoji").mkdir(parents=True, exist_ok=True)
    (cfg.media_dir / "epochs" / "2" / "emoji" / "333444.tgs").write_bytes(b"fake-tgs")
    # Duplicate 111222 in epoch should be deduplicated
    (cfg.media_dir / "epochs" / "2" / "emoji" / "111222.tgs").write_bytes(b"dup")

    _, client = await _setup_app_and_client(cfg, db_session)
    fake_tg = _V3FakeTgClient()
    fake_tg.downloads["owner_st_1"] = _make_webp_bytes((512, 512))
    engine, factory = create_engine_and_factory(cfg)
    service = TelegramService(
        session_factory=factory,
        settings=cfg,
        client=fake_tg,
        bot_id=999000,
    )

    try:
        async with client:
            r_emojis = await client.get("/api/life/emojis/custom")
            assert r_emojis.status_code == 200
            emojis = r_emojis.json()["emojis"]
            assert emojis == [
                {"id": "111222", "url": "/api/life/exports/emoji/111222.webp", "format": "webp"},
                {"id": "333444", "url": "/api/life/exports/emoji/333444.tgs", "format": "tgs"},
            ]

            # GET /stickers returns empty list on empty dir (no Pillow starter pack auto-seeded)
            r_st = await client.get("/api/life/stickers")
            assert r_st.status_code == 200
            assert r_st.json()["packs"] == []

            # Owner DM: send a sticker -> saves into media/stickers/my_pack/
            owner_user = SimpleNamespace(id=777001, first_name="Owner")
            await service.handle_owner_dm(
                SimpleNamespace(
                    id=101,
                    from_user=owner_user,
                    text=None,
                    sticker=SimpleNamespace(
                        file_id="owner_st_1",
                        file_unique_id="custom_abc",
                        set_name="my_pack",
                        emoji="🦊",
                        width=512,
                        height=512,
                        is_animated=False,
                        is_video=False,
                    ),
                )
            )
            assert "✅ Saved sticker" in fake_tg.sent_messages[-1]["text"]
            assert (cfg.media_dir / "stickers" / "my_pack" / "custom_abc.webp").is_file()

            # /stickers list shows installed packs
            await service.handle_owner_dm(
                SimpleNamespace(id=102, from_user=owner_user, text="/stickers list")
            )
            assert "my_pack/custom_abc.webp" in fake_tg.sent_messages[-1]["text"]

            # GET /stickers now includes my_pack and serves valid WebP bytes
            r_st2 = await client.get("/api/life/stickers")
            packs2 = r_st2.json()["packs"]
            pack_names = [p["name"] for p in packs2]
            assert "my_pack" in pack_names
            r_file = await client.get(packs2[0]["stickers"][0]["url"])
            assert r_file.status_code == 200
            assert r_file.headers["content-type"] == "image/webp"

            # /stickers rm removes sticker cleanly
            await service.handle_owner_dm(
                SimpleNamespace(id=103, from_user=owner_user, text="/stickers rm my_pack/custom_abc.webp")
            )
            assert "🗑️ Removed sticker" in fake_tg.sent_messages[-1]["text"]
            assert not (cfg.media_dir / "stickers" / "my_pack" / "custom_abc.webp").exists()

            # Animated .tgs sticker served with application/x-tgsticker (no attachment header)
            fake_tg.downloads["owner_tgs_1"] = b"tgs-gzip-bytes"
            await service.handle_owner_dm(
                SimpleNamespace(
                    id=104,
                    from_user=owner_user,
                    text=None,
                    sticker=SimpleNamespace(
                        file_id="owner_tgs_1",
                        file_unique_id="anim_xyz",
                        set_name="anim_pack",
                        emoji="✨",
                        width=512,
                        height=512,
                        is_animated=True,
                        is_video=False,
                    ),
                )
            )
            r_tgs = await client.get("/api/life/media/stickers/anim_pack/anim_xyz.tgs")
            assert r_tgs.status_code == 200
            assert r_tgs.headers["content-type"] == "application/x-tgsticker"
            assert "content-disposition" not in r_tgs.headers

            # Anonymous media upload rejected (403) when ALLOW_ANON_COMMENTS=False
            cfg_no_anon = cfg.model_copy(update={"ALLOW_ANON_COMMENTS": False})
            _, no_anon_client = await _setup_app_and_client(cfg_no_anon, db_session)
            async with no_anon_client:
                r_no_anon_media = await no_anon_client.post(
                    "/api/life/comments/media",
                    files={"file": ("a.png", _make_png_bytes(), "image/png")},
                )
                assert r_no_anon_media.status_code == 403
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_discussion_album_grouping_and_rich_message_types(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    app, client = await _setup_app_and_client(test_settings, db_session)
    ch = await ensure_channel(
        db_session,
        channel_id=1,
        status="active",
        id_offset=0,
        tg_chat_id=-100111,
        tg_discussion_chat_id=-100222,
    )
    post = await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=900,
        canonical_id=900,
        date=datetime(2026, 11, 19, 10, 0, tzinfo=UTC),
        text_html="Channel post 900",
    )
    post.tg_discussion_root_id = 9000
    await db_session.commit()

    fake_tg = _V3FakeTgClient()
    fake_tg.downloads["alb_p1"] = _make_png_bytes((240, 180))
    fake_tg.downloads["alb_p2"] = _make_png_bytes((180, 240))
    fake_tg.downloads["alb_p3"] = _make_png_bytes((300, 300))

    engine, factory = create_engine_and_factory(test_settings)
    service = TelegramService(
        session_factory=factory,
        settings=test_settings,
        sse_publish=app.state.sse_publish,
        client=fake_tg,
        album_debounce_seconds=0.02,
        album_max_wait_seconds=0.1,
    )
    sub_id, sub_q, _ = app.state.sse_bus.subscribe(post_ids={900})
    try:
        disc_chat = SimpleNamespace(id=-100222)
        sender = SimpleNamespace(id=5151, first_name="AlbumFriend", username="albumfriend")

        # 1. Two-photo album in discussion group merges into ONE comment
        m1 = SimpleNamespace(
            id=9101,
            chat=disc_chat,
            from_user=sender,
            reply_to_top_message_id=9000,
            reply_to_message_id=9000,
            media_group_id="disc_grp_1",
            caption="Two photos together",
            photo=SimpleNamespace(file_id="alb_p1", width=240, height=180),
            date=datetime(2026, 11, 19, 11, 0, tzinfo=UTC),
        )
        m2 = SimpleNamespace(
            id=9102,
            chat=disc_chat,
            from_user=sender,
            reply_to_top_message_id=9000,
            reply_to_message_id=9000,
            media_group_id="disc_grp_1",
            caption=None,
            photo=SimpleNamespace(file_id="alb_p2", width=180, height=240),
            date=datetime(2026, 11, 19, 11, 0, tzinfo=UTC),
        )
        assert await service.handle_discussion_message(m1) is None
        assert await service.handle_discussion_message(m2) is None
        await service.discussion_album_buffer.wait_idle()

        async with client:
            r_list = await client.get("/api/life/posts/900/comments")
            assert r_list.status_code == 200
            comments = r_list.json()["comments"]
            assert len(comments) == 1
            c_alb = comments[0]
            assert c_alb["body_text"] == "Two photos together"
            assert c_alb["media"]["media_type"] == "album"
            assert len(c_alb["media"]["images"]) == 2
            assert c_alb["media"]["images"][0]["url"].startswith("/api/life/media/comments/")
            assert c_alb["media"]["images"][1]["url"].startswith("/api/life/media/comments/")

        ev1 = sub_q.get_nowait()
        assert ev1.event == "comment.created"
        assert len(ev1.data["media"]["images"]) == 2

        # 2. Slow-split 3rd photo with same media_group_id merges into existing comment & emits comment.updated
        m3 = SimpleNamespace(
            id=9103,
            chat=disc_chat,
            from_user=sender,
            reply_to_top_message_id=9000,
            reply_to_message_id=9000,
            media_group_id="disc_grp_1",
            caption=None,
            photo=SimpleNamespace(file_id="alb_p3", width=300, height=300),
            date=datetime(2026, 11, 19, 11, 0, 2, tzinfo=UTC),
        )
        assert await service.handle_discussion_message(m3) is None
        await service.discussion_album_buffer.wait_idle()

        ev2 = sub_q.get_nowait()
        assert ev2.event == "comment.updated"
        assert ev2.data["id"] == c_alb["id"]
        assert len(ev2.data["media"]["images"]) == 3

        # 3. Rich message types (location, contact, dice, poll) in discussion replies
        c_loc = await service.handle_discussion_message(
            SimpleNamespace(
                id=9201,
                chat=disc_chat,
                from_user=sender,
                reply_to_top_message_id=9000,
                reply_to_message_id=9000,
                venue=SimpleNamespace(
                    title="Coffee Shop",
                    address="123 Elm St",
                    location=SimpleNamespace(latitude=43.47, longitude=-80.54),
                ),
                date=datetime(2026, 11, 19, 11, 5, tzinfo=UTC),
            )
        )
        assert c_loc is not None and c_loc.media["media_type"] == "location"
        assert c_loc.media["title"] == "Coffee Shop"

        c_contact = await service.handle_discussion_message(
            SimpleNamespace(
                id=9202,
                chat=disc_chat,
                from_user=sender,
                reply_to_top_message_id=9000,
                reply_to_message_id=9000,
                contact=SimpleNamespace(phone_number="+15550199", first_name="Jane", last_name="Doe"),
                date=datetime(2026, 11, 19, 11, 6, tzinfo=UTC),
            )
        )
        assert c_contact is not None and c_contact.media["media_type"] == "contact"
        assert c_contact.media["phone_number"] == "+15550199"

        c_dice = await service.handle_discussion_message(
            SimpleNamespace(
                id=9203,
                chat=disc_chat,
                from_user=sender,
                reply_to_top_message_id=9000,
                reply_to_message_id=9000,
                dice=SimpleNamespace(emoji="🎯", value=6),
                date=datetime(2026, 11, 19, 11, 7, tzinfo=UTC),
            )
        )
        assert c_dice is not None and c_dice.media["media_type"] == "dice"
        assert c_dice.media["emoji"] == "🎯" and c_dice.media["value"] == 6

        c_poll = await service.handle_discussion_message(
            SimpleNamespace(
                id=9204,
                chat=disc_chat,
                from_user=sender,
                reply_to_top_message_id=9000,
                reply_to_message_id=9000,
                poll=SimpleNamespace(
                    id="disc_poll_1",
                    question="Tea or coffee?",
                    options=[SimpleNamespace(text="Tea", voter_count=2), SimpleNamespace(text="Coffee", voter_count=3)],
                    is_closed=False,
                    is_anonymous=True,
                    type="REGULAR",
                    allows_multiple_answers=False,
                    total_voter_count=5,
                ),
                date=datetime(2026, 11, 19, 11, 8, tzinfo=UTC),
            )
        )
        assert c_poll is not None and c_poll.media["media_type"] == "poll"
        assert c_poll.media["question"] == "Tea or coffee?"
    finally:
        app.state.sse_bus.unsubscribe(sub_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_discussion_root_race_catchup_and_historical_album_merge(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    from app.db.models import CommentTgReaction, CommentWebReaction

    engine, session_factory = create_engine_and_factory(test_settings)
    png_bytes = _make_png_bytes((200, 150))

    ch_id = -1004404597067
    disc_id = -1001184854241
    ch_chat = SimpleNamespace(id=ch_id, type="channel", title="Channel")
    disc_chat = SimpleNamespace(id=disc_id, type="supergroup", title="Discussion")
    sender = SimpleNamespace(
        id=879777248, first_name="Azalea", last_name=None, username="azalea", photo=None
    )

    root_autoforward = SimpleNamespace(
        id=88001,
        chat=disc_chat,
        is_automatic_forward=True,
        forward_from_chat=ch_chat,
        forward_from_message_id=5,
        text="album post",
        date=datetime(2026, 11, 21, 23, 48, 44, tzinfo=UTC),
    )
    chan_msg_4 = SimpleNamespace(
        id=4,
        chat=ch_chat,
        text="Prev post",
        date=datetime(2026, 11, 21, 0, 53, 23, tzinfo=UTC),
    )
    chan_msg_5 = SimpleNamespace(
        id=5,
        chat=ch_chat,
        media_group_id="mg_chan_5_6",
        text="Post body",
        date=datetime(2026, 11, 21, 23, 48, 44, tzinfo=UTC),
    )
    chan_msg_6 = SimpleNamespace(
        id=6,
        chat=ch_chat,
        media_group_id="mg_chan_5_6",
        date=datetime(2026, 11, 21, 23, 48, 44, tzinfo=UTC),
    )
    missed_reply_1 = SimpleNamespace(
        id=88002,
        chat=disc_chat,
        from_user=sender,
        reply_to_top_message_id=88001,
        reply_to_message_id=88001,
        text="Missed comment 1",
        date=datetime(2026, 11, 21, 23, 49, 0, tzinfo=UTC),
    )
    missed_reply_2a = SimpleNamespace(
        id=88003,
        chat=disc_chat,
        from_user=sender,
        reply_to_top_message_id=88001,
        reply_to_message_id=88001,
        media_group_id="mg_missed_2",
        caption="Missed album",
        photo=SimpleNamespace(file_id="f_m1", width=640, height=480, file_size=200),
        date=datetime(2026, 11, 21, 23, 50, 0, tzinfo=UTC),
    )
    missed_reply_2b = SimpleNamespace(
        id=88004,
        chat=disc_chat,
        from_user=sender,
        reply_to_top_message_id=88001,
        reply_to_message_id=88001,
        media_group_id="mg_missed_2",
        photo=SimpleNamespace(file_id="f_m2", width=480, height=640, file_size=250),
        date=datetime(2026, 11, 21, 23, 50, 0, tzinfo=UTC),
    )
    hist_msg_1 = SimpleNamespace(
        id=87921,
        chat=disc_chat,
        from_user=sender,
        media_group_id="mg_hist_9612",
        photo=SimpleNamespace(file_id="f_h1", width=800, height=600, file_size=300),
        reactions=SimpleNamespace(reactions=[SimpleNamespace(emoji="🔥", count=2)]),
        date=datetime(2026, 11, 21, 23, 12, 29, tzinfo=UTC),
    )
    hist_msg_2 = SimpleNamespace(
        id=87922,
        chat=disc_chat,
        from_user=sender,
        media_group_id="mg_hist_9612",
        photo=SimpleNamespace(file_id="f_h2", width=800, height=600, file_size=320),
        date=datetime(2026, 11, 21, 23, 12, 29, tzinfo=UTC),
    )

    class FakeUserClient:
        async def get_chat(self, cid: int):
            return SimpleNamespace(id=cid, is_restricted=False, is_banned=False, is_deactivated=False)

        async def get_discussion_message(self, cid: int, msg_id: int):
            if cid == ch_id and msg_id in (5, 6):
                return root_autoforward
            return None

        async def get_discussion_replies(self, cid: int, msg_id: int, limit: int = 100):
            if cid == ch_id and msg_id == 5:
                for item in (root_autoforward, missed_reply_1, missed_reply_2a, missed_reply_2b):
                    yield item

        async def get_messages(self, cid: int, ids: Any):
            id_list = ids if isinstance(ids, list) else [ids]
            all_objs = {
                4: chan_msg_4,
                5: chan_msg_5,
                6: chan_msg_6,
                88001: root_autoforward,
                88002: missed_reply_1,
                88003: missed_reply_2a,
                88004: missed_reply_2b,
                87921: hist_msg_1,
                87922: hist_msg_2,
            }
            res = [all_objs[i] for i in id_list if i in all_objs]
            return res if isinstance(ids, list) else (res[0] if res else None)

    class FakeBotClient:
        async def download_media(self, msg: Any, file_name: str, **_kw: Any) -> str:
            Path(file_name).parent.mkdir(parents=True, exist_ok=True)
            Path(file_name).write_bytes(png_bytes)
            return file_name

    user_client = FakeUserClient()
    bot_client = FakeBotClient()
    events: list[tuple[str, dict[str, Any]]] = []

    async def publish(ev: str, payload: dict[str, Any]) -> None:
        events.append((ev, payload))

    service = TelegramService(
        session_factory=session_factory,
        settings=test_settings,
        sse_publish=publish,
        client=bot_client,
        user_client=user_client,
        bot_id=99999,
    )

    try:
        ch = await ensure_channel(
            db_session,
            channel_id=1,
            status="active",
            id_offset=0,
            tg_chat_id=ch_id,
            tg_discussion_chat_id=disc_id,
        )
        p_album = Post(
            id=100,
            channel_id=ch.id,
            tg_msg_id=6,
            tg_all_msg_ids=[5, 6],
            tg_discussion_root_id=None,
            date=datetime(2026, 11, 21, 23, 48, 44, tzinfo=UTC),
            text_html="Post body",
        )
        p_prev = Post(
            id=99,
            channel_id=ch.id,
            tg_msg_id=4,
            tg_all_msg_ids=[4],
            tg_discussion_root_id=87857,
            date=datetime(2026, 11, 21, 0, 53, 23, tzinfo=UTC),
            text_html="Prev post",
        )
        db_session.add_all([p_album, p_prev])
        await db_session.flush()

        c_hist_b = Comment(
            id=35449,
            post_id=p_prev.id,
            source="telegram",
            channel_id=ch.id,
            tg_msg_id=87922,
            tg_author_id=879777248,
            tg_author_name="Azalea",
            body_text="",
            body_html="",
            media={"media_type": "photo", "url": "comments/87922.jpg", "width": 800, "height": 600},
            created_at=datetime(2026, 11, 21, 23, 12, 29, tzinfo=UTC),
        )
        c_hist_a = Comment(
            id=35450,
            post_id=p_prev.id,
            source="telegram",
            channel_id=ch.id,
            tg_msg_id=87921,
            tg_author_id=879777248,
            tg_author_name="Azalea",
            body_text="",
            body_html="",
            media={"media_type": "photo", "url": "comments/87921.jpg", "width": 800, "height": 600},
            created_at=datetime(2026, 11, 21, 23, 12, 29, tzinfo=UTC),
        )
        db_session.add_all([c_hist_b, c_hist_a])
        await db_session.flush()

        # Child reply to the duplicate second row (35450) + reaction on 35450
        c_child = Comment(
            id=35451,
            post_id=p_prev.id,
            parent_id=35450,
            source="web",
            channel_id=ch.id,
            body_text="Nice photos!",
            body_html="<p>Nice photos!</p>",
            created_at=datetime(2026, 11, 21, 23, 13, 0, tzinfo=UTC),
        )
        db_session.add(c_child)
        db_session.add(CommentTgReaction(comment_id=35450, reaction_key="e:🔥", emoji="🔥", count=2))
        db_session.add(
            CommentWebReaction(
                comment_id=35450, reaction_key="e:❤", emoji="❤", actor_key="web:u1"
            )
        )
        await db_session.commit()

        # Run PeriodicReconciler once: links post 100 root, backfills missed replies, merges 35449+35450
        reconciler = PeriodicReconciler(
            session_factory=session_factory,
            settings=test_settings,
            telegram_service=service,
            sse_publish=publish,
        )
        stats = await reconciler.reconcile_once()
        assert stats["linked_discussion_roots"] == 1
        assert stats["backfilled_comments"] == 2
        assert stats["merged_comment_albums"] == 1

        async with session_factory() as session:
            p100 = await session.get(Post, 100)
            assert p100 is not None and p100.tg_discussion_root_id == 88001

            p100_comments = list(
                (
                    await session.execute(
                        select(Comment).where(Comment.post_id == 100).order_by(Comment.tg_msg_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(p100_comments) == 2
            assert p100_comments[0].body_text == "Missed comment 1"
            assert p100_comments[1].media["media_type"] == "album"
            assert len(p100_comments[1].media["images"]) == 2
            assert p100_comments[1].body_text == "Missed album"

            # Historical 35449 + 35450 merged into keeper 35449, with 35451 re-parented & reactions preserved
            kept = await session.get(Comment, 35449)
            gone = await session.get(Comment, 35450)
            child = await session.get(Comment, 35451)
            assert kept is not None and gone is None
            assert kept.tg_msg_id == 87921
            assert kept.tg_media_group_id == "mg_hist_9612"
            assert kept.media["media_type"] == "album"
            assert len(kept.media["images"]) == 2
            assert child is not None and child.parent_id == 35449

            tg_rx = (
                await session.execute(
                    select(CommentTgReaction).where(CommentTgReaction.comment_id == 35449)
                )
            ).scalars().all()
            web_rx = (
                await session.execute(
                    select(CommentWebReaction).where(CommentWebReaction.comment_id == 35449)
                )
            ).scalars().all()
            assert len(tg_rx) == 1 and tg_rx[0].reaction_key == "e:🔥"
            assert len(web_rx) == 1 and web_rx[0].actor_key == "web:u1"

        # Test real-time autoforward race cache when channel album flushes AFTER autoforward arrives
        await service.handle_discussion_message(
            SimpleNamespace(
                id=88010,
                chat=disc_chat,
                is_automatic_forward=True,
                forward_from_chat=ch_chat,
                forward_from_message_id=7,
                text="race post",
                date=datetime(2026, 11, 21, 23, 55, 0, tzinfo=UTC),
            )
        )
        assert (1, 7) in service._unlinked_autoforwards

        # Even if not cached yet, on-demand discussion thread walk links missing root on reply
        async with session_factory() as session:
            session.add(
                Post(
                    id=101,
                    channel_id=1,
                    tg_msg_id=8,
                    tg_all_msg_ids=[7, 8],
                    tg_discussion_root_id=None,
                    date=datetime(2026, 11, 21, 23, 55, 0, tzinfo=UTC),
                    text_html="Race post",
                )
            )
            await session.commit()

        FakeUserClient.get_messages = lambda self_u, cid, ids: asyncio.sleep(  # type: ignore[method-assign]
            0,
            result=SimpleNamespace(
                id=88010,
                chat=disc_chat,
                is_automatic_forward=True,
                forward_from_chat=ch_chat,
                forward_from_message_id=7,
            ),
        )
        late_c = await service.handle_discussion_message(
            SimpleNamespace(
                id=88011,
                chat=disc_chat,
                from_user=sender,
                reply_to_top_message_id=88010,
                reply_to_message_id=88010,
                text="Arrived before link!",
                date=datetime(2026, 11, 21, 23, 55, 2, tzinfo=UTC),
            )
        )
        assert late_c is not None and late_c.post_id == 101
        async with session_factory() as session:
            p101 = await session.get(Post, 101)
            assert p101 is not None and p101.tg_discussion_root_id == 88010
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_channel_edit_race_and_album_prompts_originals_once(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    from app.db.models import OriginalUploadToken

    engine, session_factory = create_engine_and_factory(test_settings)
    png_bytes = _make_png_bytes((240, 180))
    ch_id = -100777888
    ch_chat = SimpleNamespace(id=ch_id, type="channel", title="Channel")
    now = datetime.now(UTC)

    class FakeClient:
        def __init__(self) -> None:
            self.sent_dms: list[str] = []

        async def download_media(self, msg: Any, file_name: str, **_kw: Any) -> str:
            await asyncio.sleep(0.01)
            Path(file_name).parent.mkdir(parents=True, exist_ok=True)
            Path(file_name).write_bytes(png_bytes)
            return file_name

        async def send_message(self, chat_id: int, text: str, **_kw: Any) -> Any:
            self.sent_dms.append(text)
            return SimpleNamespace(id=500 + len(self.sent_dms))

    fake_client = FakeClient()
    prompt_settings = test_settings.model_copy(update={"OWNER_TG_ID": 987654})
    app, _ = await _setup_app_and_client(prompt_settings, db_session)
    service = TelegramService(
        session_factory=session_factory,
        settings=prompt_settings,
        sse_publish=app.state.sse_publish,
        client=fake_client,
        album_debounce_seconds=0.05,
        album_max_wait_seconds=0.2,
    )
    try:
        await ensure_channel(
            db_session,
            channel_id=1,
            status="active",
            id_offset=0,
            tg_chat_id=ch_id,
        )
        await db_session.commit()

        m1 = SimpleNamespace(
            id=8,
            chat=ch_chat,
            media_group_id="mg_race_9616",
            photo=SimpleNamespace(file_id="p8", width=240, height=180),
            date=now,
        )
        m2 = SimpleNamespace(
            id=9,
            chat=ch_chat,
            media_group_id="mg_race_9616",
            photo=SimpleNamespace(file_id="p9", width=240, height=180),
            date=now,
        )

        # Simulate EditedMessage arriving concurrently with album ingestion creating the post with is_edit=True first
        await asyncio.gather(
            service.handle_channel_edit(m1),
            service._ingest_channel_messages([m1, m2], is_edit=False),
        )

        # Should prompt OWNER_TG_ID exactly once even though edit arrived first
        assert len(fake_client.sent_dms) == 1
        assert "Post #" in fake_client.sent_dms[0]

        # Subsequent caption edit on the same post does NOT send duplicate prompts
        await service.handle_channel_edit(
            SimpleNamespace(
                id=8,
                chat=ch_chat,
                media_group_id="mg_race_9616",
                caption="Edited caption",
                photo=SimpleNamespace(file_id="p8", width=240, height=180),
                date=now,
                edit_date=now,
            )
        )
        assert len(fake_client.sent_dms) == 1

        async with session_factory() as session:
            tokens = (await session.execute(select(OriginalUploadToken))).scalars().all()
            assert len(tokens) == 1
    finally:
        await engine.dispose()
