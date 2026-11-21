from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image
from pyrogram import enums
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Comment, User
from app.db.repo import ensure_channel, upsert_post
from app.db.session import create_engine_and_factory
from app.tg.avatar_emoji import (
    _ANON_ASSETS_DIR,
    _load_avatar_image_bytes,
    _pick_anon_identity,
    _render_anon_badge_png,
    _render_circular_avatar_png,
    ensure_sender_avatar_custom_emoji,
)
from app.tg.bridge import TelegramBridge


def _make_png_bytes(size: tuple[int, int] = (160, 120), color: tuple[int, int, int] = (200, 80, 60)) -> bytes:
    im = Image.new("RGB", size, color)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class _FakeStickerApiTgClient:
    def __init__(self, *, bot_username: str = "corner_test_bot") -> None:
        self.bot_username = bot_username
        self.sets: dict[str, list[Any]] = {}
        self.created_calls: list[dict[str, Any]] = []
        self.added_calls: list[dict[str, Any]] = []
        self.sent_messages: list[dict[str, Any]] = []
        self.next_msg_id = 100
        self.next_cid = 9001000

    async def get_me(self) -> Any:
        return SimpleNamespace(id=999000, username=self.bot_username, is_bot=True)

    async def get_sticker_set(self, name: str) -> Any:
        stickers = self.sets.get(name)
        if stickers is None:
            raise RuntimeError("STICKERSET_INVALID")
        return SimpleNamespace(name=name, stickers=list(stickers))

    async def create_new_sticker_set(
        self,
        *,
        user_id: int | str,
        name: str,
        title: str,
        stickers: list[Any],
        sticker_type: Any = None,
    ) -> Any:
        assert sticker_type == enums.StickerType.CUSTOM_EMOJI
        self.next_cid += 1
        item = SimpleNamespace(custom_emoji_id=str(self.next_cid))
        self.sets[name] = [item]
        self.created_calls.append({"user_id": user_id, "name": name, "title": title, "stickers": stickers})
        return SimpleNamespace(name=name, stickers=list(self.sets[name]))

    async def add_sticker_to_set(
        self,
        *,
        user_id: int | str,
        name: str,
        sticker: Any,
    ) -> Any:
        self.next_cid += 1
        item = SimpleNamespace(custom_emoji_id=str(self.next_cid))
        self.sets[name].append(item)
        self.added_calls.append({"user_id": user_id, "name": name, "sticker": sticker})
        return SimpleNamespace(name=name, stickers=list(self.sets[name]))

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.next_msg_id += 1
        self.sent_messages.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(id=self.next_msg_id)


def test_circular_png_renderers() -> None:
    raw = _make_png_bytes((200, 120), (30, 120, 220))
    circ = _render_circular_avatar_png(raw)
    with Image.open(io.BytesIO(circ)) as im:
        assert im.format == "PNG"
        assert im.size == (100, 100)
        assert im.mode == "RGBA"
        # Corners transparent, center opaque
        assert im.getpixel((0, 0))[3] == 0
        assert im.getpixel((50, 50))[3] == 255

    color, chosen_path, source_key = _pick_anon_identity("Anon #9f8f")
    assert chosen_path is not None and chosen_path.parent == _ANON_ASSETS_DIR
    assert source_key.startswith("anon2:anon-")
    anon_png = _render_anon_badge_png(color, chosen_path)
    with Image.open(io.BytesIO(anon_png)) as im2:
        assert im2.format == "PNG"
        assert im2.size == (100, 100)
        assert im2.getpixel((0, 0))[3] == 0
        # Outer ring pixel uses ring color, center renders the actual animal avatar
        assert im2.getpixel((50, 2))[3] == 255


@pytest.mark.asyncio
async def test_ssrf_blocks_private_and_non_https_avatar_urls(test_settings: Settings) -> None:
    assert await _load_avatar_image_bytes("http://example.com/a.png", settings=test_settings) is None
    assert await _load_avatar_image_bytes("https://127.0.0.1/a.png", settings=test_settings) is None
    assert await _load_avatar_image_bytes("https://localhost/a.png", settings=test_settings) is None


@pytest.mark.asyncio
async def test_avatar_custom_emoji_pack_creation_rollover_and_bridge(
    test_settings: Settings,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    cfg = test_settings.model_copy(
        update={
            "DATA_DIR": tmp_path / "data",
            "OWNER_TG_ID": 777001,
            "TG_CHANNEL_ID": -100111222,
            "TG_DISCUSSION_CHAT_ID": -100111333,
        }
    )
    avatars_dir = cfg.media_dir / "avatars"
    avatars_dir.mkdir(parents=True, exist_ok=True)
    (avatars_dir / "alice.png").write_bytes(_make_png_bytes((150, 150), (40, 180, 90)))
    (avatars_dir / "bob.png").write_bytes(_make_png_bytes((150, 150), (180, 40, 120)))

    engine, factory = create_engine_and_factory(cfg)
    tg = _FakeStickerApiTgClient()
    try:
        alice = User(
            role="user",
            display_name="Alice",
            avatar_url="/api/life/media/avatars/alice.png",
        )
        bob = User(
            role="user",
            display_name="Bob",
            avatar_url="/api/life/media/avatars/bob.png",
        )
        db_session.add_all([alice, bob])
        await db_session.flush()

        ch = await ensure_channel(
            db_session,
            tg_chat_id=cfg.TG_CHANNEL_ID,
            tg_discussion_chat_id=cfg.TG_DISCUSSION_CHAT_ID,
            status="active",
        )
        post = await upsert_post(
            db_session,
            channel=ch,
            tg_msg_id=10,
            tg_all_msg_ids=[10],
            date=datetime(2026, 11, 21, 4, 0, tzinfo=UTC),
            text_html="Hello",
        )
        post.tg_discussion_root_id = 300

        c_alice = Comment(
            post_id=post.id,
            channel_id=ch.id,
            source="web",
            user_id=alice.id,
            body_text="Hi from Alice",
            body_html="<p>Hi from Alice</p>",
            created_at=datetime(2026, 11, 21, 4, 1, tzinfo=UTC),
        )
        c_alice2 = Comment(
            post_id=post.id,
            channel_id=ch.id,
            source="web",
            user_id=alice.id,
            body_text="Second msg from Alice",
            body_html="<p>Second msg from Alice</p>",
            created_at=datetime(2026, 11, 21, 4, 2, tzinfo=UTC),
        )
        c_anon = Comment(
            post_id=post.id,
            channel_id=ch.id,
            source="web",
            anon_tag="Anon #9f8f",
            body_text="Anon hello",
            body_html="<p>Anon hello</p>",
            created_at=datetime(2026, 11, 21, 4, 3, tzinfo=UTC),
        )
        db_session.add_all([c_alice, c_alice2, c_anon])
        await db_session.commit()

        bridge = TelegramBridge(session_factory=factory, settings=cfg, client=tg)

        # 1. Alice first comment -> creates web_avatars_1_by_corner_test_bot
        await bridge.bridge_web_comment(c_alice.id)
        assert len(tg.created_calls) == 1
        assert tg.created_calls[0]["name"] == "web_avatars_1_by_corner_test_bot"
        assert '<tg-emoji emoji-id="9001001">👤</tg-emoji> <b>Alice</b>:\nHi from Alice' == tg.sent_messages[0]["text"]

        # 2. Alice second comment -> DB cache hit, NO new create/add sticker API calls!
        await bridge.bridge_web_comment(c_alice2.id)
        assert len(tg.created_calls) == 1
        assert len(tg.added_calls) == 0
        assert '<tg-emoji emoji-id="9001001">👤</tg-emoji> <b>Alice</b>:\nSecond msg from Alice' == tg.sent_messages[1]["text"]

        # 3. Anon comment -> adds badge to existing pack #1
        await bridge.bridge_web_comment(c_anon.id)
        assert len(tg.added_calls) == 1
        assert tg.added_calls[0]["name"] == "web_avatars_1_by_corner_test_bot"
        assert '<tg-emoji emoji-id="9001002">👤</tg-emoji> <b>Anon #9f8f</b>:\nAnon hello' == tg.sent_messages[2]["text"]

        # 4. Fill pack #1 to 200 stickers -> Bob rotates to web_avatars_2_by_corner_test_bot
        tg.sets["web_avatars_1_by_corner_test_bot"].extend(
            [SimpleNamespace(custom_emoji_id=str(i)) for i in range(198)]
        )
        res_bob = await ensure_sender_avatar_custom_emoji(
            session_factory=factory,
            settings=cfg,
            client=tg,
            user=bob,
            anon_display_name=None,
        )
        assert res_bob is not None
        assert len(tg.created_calls) == 2
        assert tg.created_calls[1]["name"] == "web_avatars_2_by_corner_test_bot"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_load_avatar_image_bytes_follows_safe_https_redirects_only(
    monkeypatch: pytest.MonkeyPatch,
    test_settings: Settings,
) -> None:
    import httpx

    png = _make_png_bytes()
    requested: list[str] = []

    monkeypatch.setattr(
        "app.tg.avatar_emoji._is_public_hostname",
        lambda host: host in ("t.me", "cdn5.telesco.pe"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "t.me" and "ok" in request.url.path:
            return httpx.Response(
                302,
                headers={"location": "https://cdn5.telesco.pe/file/avatar.png"},
            )
        if request.url.host == "t.me" and "ssrf" in request.url.path:
            return httpx.Response(
                302,
                headers={"location": "https://127.0.0.1/secret.png"},
            )
        if request.url.host == "cdn5.telesco.pe":
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        ok_bytes = await _load_avatar_image_bytes(
            "https://t.me/i/userpic/320/ok.jpg",
            settings=test_settings,
            http_client=client,
        )
        assert ok_bytes == png
        assert requested == [
            "https://t.me/i/userpic/320/ok.jpg",
            "https://cdn5.telesco.pe/file/avatar.png",
        ]

        requested.clear()
        blocked = await _load_avatar_image_bytes(
            "https://t.me/i/userpic/320/ssrf.jpg",
            settings=test_settings,
            http_client=client,
        )
        assert blocked is None
        assert requested == ["https://t.me/i/userpic/320/ssrf.jpg"]
