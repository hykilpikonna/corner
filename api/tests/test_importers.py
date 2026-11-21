from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import import_epoch1_comments, import_epoch1_posts
from app.config import Settings
from app.db.models import Channel, Comment, Poll, Post
from app.db.repo import get_post_by_id, serialize_post


def _create_sample_image(path: Path, color: tuple[int, int, int], size: tuple[int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color=color)
    # Add a contrasting block so DCT pHash has non-uniform frequencies
    for x in range(size[0] // 2):
        for y in range(size[1] // 2):
            img.putpixel((x, y), (255 - color[0], 255 - color[1], color[2]))
    img.save(path)


def _create_epoch1_fixture_dir(base: Path) -> Path:
    src = base / "epoch1_export"
    (src / "media").mkdir(parents=True, exist_ok=True)
    (src / "emoji").mkdir(parents=True, exist_ok=True)

    _create_sample_image(src / "media" / "10.jpg", (200, 40, 40), (800, 600))
    _create_sample_image(src / "media" / "11.jpg", (40, 200, 40), (640, 480))
    _create_sample_image(src / "media" / "10_thumb.webp", (200, 40, 40), (200, 150))
    (src / "emoji" / "5368324170671202286.webp").write_bytes(b"RIFFmockwebp")
    (src / "media" / "clip.mp4").write_bytes(b"mock-video-bytes")

    posts = [
        {
            "id": 1,
            "date": "2025-01-10T08:00:00Z",
            "text": 'First post with custom emoji <i class="custom-emoji" emoji-src="emoji/5368324170671202286"></i>',
            "views": "150",
            "author": "Aza",
            "reactions": [
                {"emoji": "❤️", "count": 5},
                {"custom_emoji_id": "5368324170671202286", "count": 2},
            ],
        },
        {
            "id": 10,
            "date": "2025-01-11T10:30:00Z",
            "text": "Album of two photos",
            "views": 320,
            "reply": {
                "id": 1,
                "text": "First post",
                "thumb": "media/10_thumb.webp",
            },
            "images": [
                {
                    "url": "media/10.jpg",
                    "thumb": "media/10_thumb.webp",
                    "width": 800,
                    "height": 600,
                    "size": 12345,
                },
                {
                    "url": "media/11.jpg",
                    "width": 640,
                    "height": 480,
                },
            ],
            "files": [
                {
                    "url": "media/clip.mp4",
                    "mime_type": "video/mp4",
                    "media_type": "video_file",
                    "original_name": "clip.mp4",
                }
            ],
        },
        {
            "id": 25,
            "date": "2025-01-12T12:00:00Z",
            "text": "Vote below!",
            "files": [
                {
                    "media_type": "poll",
                    "id": "poll_epoch1_25",
                    "question": "Tea or coffee?",
                    "is_closed": 0,
                    "is_anonymous": 1,
                    "type": "REGULAR",
                    "allow_multiple_answers": False,
                    "options": [
                        {"text": "Tea", "voter_count": 4, "data": "0"},
                        {"text": "Coffee", "voter_count": 9, "data": "1"},
                    ],
                }
            ],
        },
    ]
    (src / "posts.json").write_text(json.dumps(posts), encoding="utf-8")
    return src


@pytest.mark.asyncio
async def test_import_epoch1_posts_and_idempotency(
    db_session: AsyncSession, test_settings: Settings, tmp_path: Path
) -> None:
    src_dir = _create_epoch1_fixture_dir(tmp_path)

    stats = await import_epoch1_posts(
        db_session,
        test_settings,
        src_dir,
        channel_id=1,
        tg_chat_id=-1001111111,
        title="Banned Channel",
        media_mode="hardlink",
    )
    assert stats["posts_imported"] == 3
    assert stats["images_imported"] == 2
    assert stats["images_fingerprinted"] == 2
    assert stats["polls_imported"] == 1
    assert stats["max_id"] == 25

    channel = await db_session.get(Channel, 1)
    assert channel is not None
    assert channel.status == "archived"
    assert channel.id_offset == 0
    assert channel.last_crawled_msg_id == 25

    # Media hardlinked under DATA_DIR/media/epochs/1/ (not symlinked!)
    dst_jpg = test_settings.media_dir / "epochs" / "1" / "media" / "10.jpg"
    assert dst_jpg.is_file() and not dst_jpg.is_symlink()
    assert os.path.samefile(src_dir / "media" / "10.jpg", dst_jpg)
    assert (test_settings.media_dir / "epochs" / "1" / "emoji" / "5368324170671202286.webp").is_file()

    # Post 1: reactions and inline emoji URL rewrite
    p1 = await get_post_by_id(db_session, 1)
    assert p1 is not None
    assert "/api/life/media/epochs/1/emoji/5368324170671202286" in (p1.text_html or "")
    assert len(p1.tg_reactions) == 2

    # Post 10: album images fingerprinted + tg_all_msg_ids includes 10 & 11 + reply linked
    p10 = await get_post_by_id(db_session, 10)
    assert p10 is not None
    assert p10.reply_to_post_id == 1
    assert p10.tg_all_msg_ids == [10, 11]
    assert len(p10.images) == 2
    assert p10.images[0].url == "epochs/1/media/10.jpg"
    assert p10.images[0].thumb_url == "epochs/1/media/10_thumb.webp"
    assert p10.images[0].phash is not None and len(p10.images[0].phash) == 16
    assert p10.images[0].rgb_sig is not None and len(p10.images[0].rgb_sig) == 16 * 16 * 3

    # Post 25: poll stored in polls table, not duplicated in post.files JSONB
    p25 = await get_post_by_id(db_session, 25)
    assert p25 is not None
    assert p25.files is None
    assert p25.poll is not None
    assert p25.poll.id == "poll_epoch1_25"
    assert len(p25.poll.options) == 2

    serialized_25 = serialize_post(p25, api_prefix=test_settings.API_PREFIX)
    assert serialized_25["files"][0]["media_type"] == "poll"
    assert serialized_25["files"][0]["total_voter_count"] == 13

    # Re-running import must be idempotent
    stats2 = await import_epoch1_posts(
        db_session, test_settings, src_dir, channel_id=1, media_mode="copy"
    )
    assert stats2["posts_imported"] == 3
    posts_count = len((await db_session.execute(select(Post))).scalars().all())
    polls_count = len((await db_session.execute(select(Poll))).scalars().all())
    assert posts_count == 3
    assert polls_count == 1


@pytest.mark.asyncio
async def test_import_epoch1_comments_telethon_jsonl_and_foreign_channel_rejection(
    db_session: AsyncSession, test_settings: Settings, tmp_path: Path
) -> None:
    src_dir = _create_epoch1_fixture_dir(tmp_path)
    await import_epoch1_posts(
        db_session,
        test_settings,
        src_dir,
        channel_id=1,
        tg_chat_id=-1001111111,
        title="Banned Channel",
    )

    # Telethon raw to_dict() JSONL dump with:
    # - genuine channel forward (channel_id 1111111 matches -1001111111)
    # - foreign channel forward (channel_id 9999999) attempting to hijack post #1!
    # - reply that happens to forward a message (must NOT become a root!)
    # - internal Telethon access_hash/file_reference in media (must be stripped!)
    # - scheme-less URL entity (must gain https://)
    jsonl_lines = [
        # Foreign channel forward with same channel_post=1 -> MUST BE REJECTED!
        {
            "_": "Message",
            "id": 490,
            "date": "2025-01-10T09:00:00Z",
            "message": "Foreign post #1",
            "fwd_from": {
                "_": "MessageFwdHeader",
                "from_id": {"_": "PeerChannel", "channel_id": 9999999},
                "channel_post": 1,
            },
        },
        # Comment replying to foreign forward 490 -> MUST BE SKIPPED!
        {
            "_": "Message",
            "id": 491,
            "date": "2025-01-10T09:01:00Z",
            "message": "Comment on foreign channel post",
            "from_id": {"_": "PeerUser", "user_id": 333},
            "reply_to": {"reply_to_msg_id": 490, "reply_to_top_id": 490},
        },
        # Auto-forwarded channel post #10 (discussion msg 500)
        {
            "_": "Message",
            "id": 500,
            "date": "2025-01-11T10:30:05Z",
            "message": "Album of two photos",
            "fwd_from": {
                "_": "MessageFwdHeader",
                "from_id": {"_": "PeerChannel", "channel_id": 1111111},
                "channel_post": 10,
            },
        },
        # Auto-forwarded album sibling #11 (discussion msg 501)
        {
            "_": "Message",
            "id": 501,
            "date": "2025-01-11T10:30:05Z",
            "message": "",
            "fwd_from": {
                "_": "MessageFwdHeader",
                "from_id": {"_": "PeerChannel", "channel_id": 1111111},
                "saved_from_msg_id": 11,
            },
        },
        # Top-level comment replying to root 500 (also includes media with internal access_hash + scheme-less URL)
        {
            "_": "Message",
            "id": 502,
            "date": "2025-01-11T11:00:00Z",
            "message": "Great photo! example.com <script>alert(1)</script>",
            "from_id": {"_": "PeerUser", "user_id": 777},
            "sender": {"first_name": "Alice", "last_name": "L", "username": "alice"},
            "reply_to": {"reply_to_msg_id": 500, "reply_to_top_id": 500},
            "entities": [
                {"_": "MessageEntityBold", "offset": 0, "length": 5},
                {"_": "MessageEntityUrl", "offset": 13, "length": 11},
                {"_": "MessageEntityBold", "offset": 0, "length": 0},
            ],
            "media": {
                "_": "MessageMediaPhoto",
                "photo": {
                    "id": 12345,
                    "access_hash": 9876543210,
                    "file_reference": "b'secret_ref'",
                },
            },
        },
        # Nested reply to comment 502 that ALSO has fwd_from pointing to channel post #1 -> must stay a comment on #10, NOT hijack #1!
        {
            "_": "Message",
            "id": 503,
            "date": "2025-01-11T11:05:00Z",
            "message": "Agreed!",
            "from_id": {"_": "PeerUser", "user_id": 888},
            "sender": {"first_name": "Bob", "username": "bob"},
            "fwd_from": {
                "_": "MessageFwdHeader",
                "from_id": {"_": "PeerChannel", "channel_id": 1111111},
                "channel_post": 1,
            },
            "reply_to": {"reply_to_msg_id": 502, "reply_to_top_id": 500},
        },
        # Comment replying directly to album sibling root 501
        {
            "_": "Message",
            "id": 504,
            "date": "2025-01-11T11:10:00Z",
            "message": "Love the second shot",
            "from_id": {"_": "PeerUser", "user_id": 999},
            "sender": {"first_name": "Carol"},
            "reply_to": {"reply_to_msg_id": 501},
        },
        # Unrelated group chatter (should be skipped)
        {
            "_": "Message",
            "id": 505,
            "date": "2025-01-11T11:20:00Z",
            "message": "Random off-topic chat",
            "from_id": {"_": "PeerUser", "user_id": 999},
        },
    ]
    dump_file = tmp_path / "telethon_discussion.jsonl"
    dump_file.write_text("\n".join(json.dumps(row) for row in jsonl_lines), encoding="utf-8")

    summary = await import_epoch1_comments(
        db_session, dump_file, channel_id=1, discussion_chat_id=-1002222222
    )
    assert summary["roots_linked"] == 1
    assert summary["comments_imported"] == 3
    assert summary["skipped_unlinked"] == 3  # 490 (foreign), 491 (reply to foreign), 505 (chatter)

    # Post #1 must NOT have been hijacked by foreign forward 490 or forwarded reply 503
    post1 = await db_session.get(Post, 1)
    assert post1 is not None and post1.tg_discussion_root_id is None

    post10 = await db_session.get(Post, 10)
    assert post10 is not None and post10.tg_discussion_root_id == 500

    comments = list(
        (
            await db_session.execute(
                select(Comment).where(Comment.post_id == 10).order_by(Comment.tg_msg_id)
            )
        )
        .scalars()
        .all()
    )
    assert [c.tg_msg_id for c in comments] == [502, 503, 504]
    c502, c503, c504 = comments
    assert c502.parent_id is None
    assert c502.tg_author_name == "Alice L"
    assert c502.tg_author_username == "alice"
    assert "<b>Great</b>" in c502.body_html
    assert 'href="https://example.com"' in c502.body_html
    assert "<script>" not in c502.body_html
    # Internal Telethon fields stripped from media JSONB
    assert c502.media is not None
    assert "access_hash" not in c502.media["photo"]
    assert "file_reference" not in c502.media["photo"]

    assert c503.parent_id == c502.id
    assert c504.parent_id is None

    # Idempotency check on re-run
    summary2 = await import_epoch1_comments(db_session, dump_file, channel_id=1)
    assert summary2["comments_imported"] == 0
    assert summary2["comments_updated"] == 3
    total = len((await db_session.execute(select(Comment))).scalars().all())
    assert total == 3


@pytest.mark.asyncio
async def test_import_epoch1_comments_tdesktop_json(
    db_session: AsyncSession, test_settings: Settings, tmp_path: Path
) -> None:
    src_dir = _create_epoch1_fixture_dir(tmp_path)
    await import_epoch1_posts(
        db_session,
        test_settings,
        src_dir,
        channel_id=1,
        tg_chat_id=-1001111111,
        title="Banned Channel",
    )

    # Telegram Desktop result.json export with 3-level nested reply chain + foreign forward rejection
    tdesktop_export = {
        "name": "Life Discussion",
        "type": "public_supergroup",
        "id": 2222222,
        "messages": [
            {
                "id": 1,
                "type": "service",
                "date": "2025-01-10T07:00:00",
                "action": "create_group",
            },
            # Foreign channel forward with same timestamp & text -> MUST BE REJECTED!
            {
                "id": 899,
                "type": "message",
                "date": "2025-01-12T12:00:01Z",
                "from": "Other Channel",
                "from_id": "channel9999999",
                "forwarded_from": "Other Channel",
                "text": "Vote below!",
            },
            # Genuine auto-forwarded channel post #25 without explicit channel_post ID (matched by channel + timestamp + text)
            {
                "id": 900,
                "type": "message",
                "date": "2025-01-12T12:00:03Z",
                "from": "Banned Channel",
                "from_id": "channel1111111",
                "forwarded_from": "Banned Channel",
                "text": "Vote below!",
            },
            # Level 1 comment -> replies to root 900
            {
                "id": 901,
                "type": "message",
                "date": "2025-01-12T12:05:00Z",
                "from": "Dave",
                "from_id": "user444",
                "reply_to_message_id": 900,
                "text": [
                    "Definitely ",
                    {"type": "bold", "text": "coffee"},
                    " ",
                    {"type": "text_link", "text": "guide", "href": "https://example.com/coffee"},
                    " ",
                    {"type": "link", "text": "coffee.org"},
                ],
            },
            # Level 2 comment -> replies to 901
            {
                "id": 902,
                "type": "message",
                "date": "2025-01-12T12:06:00Z",
                "from": "Eve",
                "from_id": "user555",
                "reply_to_message_id": 901,
                "text": "Matcha tea is better!",
            },
            # Level 3 comment -> replies to 902
            {
                "id": 903,
                "type": "message",
                "date": "2025-01-12T12:07:00Z",
                "from": "Dave",
                "from_id": "user444",
                "reply_to_message_id": 902,
                "text": "Fair enough!",
            },
        ],
    }
    tdesktop_file = tmp_path / "result.json"
    tdesktop_file.write_text(json.dumps(tdesktop_export), encoding="utf-8")

    summary = await import_epoch1_comments(db_session, tdesktop_file, channel_id=1)
    assert summary["roots_linked"] == 1
    assert summary["comments_imported"] == 3
    assert summary["skipped_unlinked"] == 1  # msg 899 rejected

    post25 = await db_session.get(Post, 25)
    assert post25 is not None and post25.tg_discussion_root_id == 900

    comments = list(
        (
            await db_session.execute(
                select(Comment).where(Comment.post_id == 25).order_by(Comment.tg_msg_id)
            )
        )
        .scalars()
        .all()
    )
    assert [c.tg_msg_id for c in comments] == [901, 902, 903]
    c901, c902, c903 = comments
    assert c901.parent_id is None
    assert c901.tg_author_id == 444
    assert c901.tg_author_name == "Dave"
    assert "<b>coffee</b>" in c901.body_html
    assert 'href="https://example.com/coffee"' in c901.body_html
    assert 'href="https://coffee.org"' in c901.body_html
    assert c902.parent_id == c901.id
    assert c903.parent_id == c902.id

    # Idempotent re-run
    summary2 = await import_epoch1_comments(db_session, tdesktop_file, channel_id=1)
    assert summary2["comments_imported"] == 0
    assert summary2["comments_updated"] == 3
    assert datetime.now(UTC) > c901.created_at


@pytest.mark.asyncio
async def test_import_epoch1_comments_tdesktop_media_reactions_and_renamed_channel(
    db_session: AsyncSession, test_settings: Settings, tmp_path: Path
) -> None:
    src_dir = _create_epoch1_fixture_dir(tmp_path)
    await import_epoch1_posts(
        db_session,
        test_settings,
        src_dir,
        channel_id=1,
        tg_chat_id=-1001111111,
        title="Banned Channel",
    )

    export_dir = tmp_path / "chat_export"
    (export_dir / "stickers").mkdir(parents=True)
    (export_dir / "stickers" / "hi.webp").write_bytes(b"mock-sticker-webp")

    tdesktop_export = {
        "name": "Discussion",
        "type": "private_supergroup",
        "id": 2222222,
        "messages": [
            # Edited post #10 whose text changed slightly, with channel title renamed + album sibling #902
            {
                "id": 901,
                "type": "message",
                "date_unixtime": str(int(datetime(2025, 1, 11, 10, 30, 4, tzinfo=UTC).timestamp())),
                "from": "Banned Channel 📒",
                "from_id": "channel1111111",
                "forwarded_from": "Banned Channel 📒",
                "text": "Edited album text mismatch",
            },
            {
                "id": 902,
                "type": "message",
                "date_unixtime": str(int(datetime(2025, 1, 11, 10, 30, 5, tzinfo=UTC).timestamp())),
                "from": "Banned Channel 📒",
                "from_id": "channel1111111",
                "forwarded_from": "Banned Channel 📒",
                "text": "",
            },
            # Sticker-only reply to album sibling #902 with emoji reaction
            {
                "id": 903,
                "type": "message",
                "date_unixtime": str(int(datetime(2025, 1, 11, 10, 32, 0, tzinfo=UTC).timestamp())),
                "from": "StickerFan",
                "from_id": "user555",
                "reply_to_message_id": 902,
                "file": "stickers/hi.webp",
                "media_type": "sticker",
                "sticker_emoji": "😺",
                "text": "",
                "reactions": [{"type": "emoji", "count": 3, "emoji": "❤"}],
            },
        ],
    }
    json_path = export_dir / "result.json"
    json_path.write_text(json.dumps(tdesktop_export), encoding="utf-8")

    summary = await import_epoch1_comments(
        db_session,
        json_path,
        channel_id=1,
        discussion_chat_id=-1002222222,
        settings=test_settings,
        media_mode="copy",
    )
    assert summary["roots_linked"] == 1
    assert summary["comments_imported"] == 1
    assert summary["media_copied"] == 1

    c903 = (
        await db_session.execute(select(Comment).where(Comment.tg_msg_id == 903))
    ).scalar_one()
    assert c903.post_id == 10
    assert c903.body_text == ""
    assert c903.body_html == ""
    assert c903.media == {
        "media_type": "sticker",
        "url": "comments/epoch1/stickers/hi.webp",
        "emoji": "😺",
        "is_animated": False,
        "is_video": False,
    }
    assert (test_settings.media_dir / "comments" / "epoch1" / "stickers" / "hi.webp").is_file()
    await db_session.refresh(c903, attribute_names=["tg_reactions"])
    assert len(c903.tg_reactions) == 1
    assert c903.tg_reactions[0].count == 3
