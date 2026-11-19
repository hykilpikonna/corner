from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Post
from app.db.repo import ensure_channel, upsert_post
from app.exports import build_rss_xml, generate_exports

NS_CONTENT = "http://purl.org/rss/1.0/modules/content/"
NS_MEDIA = "http://search.yahoo.com/mrss/"
NS_ATOM = "http://www.w3.org/2005/Atom"


@pytest.mark.asyncio
async def test_atomic_exports_and_multi_image_feeds(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    epoch = await ensure_channel(db_session, channel_id=1, status="active", id_offset=0)

    # Create 3 posts so EXPORT_PAGE_SIZE=2 splits into page 0 (2 newest) and page 1 (1 oldest)
    await upsert_post(
        db_session,
        channel=epoch,
        tg_msg_id=1,
        date=datetime(2026, 3, 1, 10, 0, tzinfo=UTC),
        text_html="Oldest post with CDATA killer ]]> & special <chars>",
    )
    await upsert_post(
        db_session,
        channel=epoch,
        tg_msg_id=2,
        date=datetime(2026, 3, 2, 10, 0, tzinfo=UTC),
        edited_at=datetime(2026, 3, 2, 12, 30, tzinfo=UTC),
        text_html="Poll post",
        poll={
            "id": "poll_feed_2",
            "question": "Favorite season?",
            "options": [
                {"text": "Spring", "tg_voter_count": 4, "data": "0"},
                {"text": "Autumn", "tg_voter_count": 6, "data": "1"},
            ],
        },
    )
    await upsert_post(
        db_session,
        channel=epoch,
        tg_msg_id=3,
        date=datetime(2026, 3, 3, 15, 0, tzinfo=UTC),
        text_html="Sunset trip album",
        images=[
            {
                "url": "epochs/1/media/3a.jpg",
                "thumb_url": "epochs/1/media/3a_thumb.webp",
                "width": 1600,
                "height": 1200,
                "size": 100000,
            },
            {
                "url": "epochs/1/media/3b.jpg",
                "thumb_url": "epochs/1/media/3b_thumb.webp",
                "width": 1600,
                "height": 1200,
                "size": 110000,
            },
            {
                "url": "epochs/1/media/3c.jpg",
                "width": 1200,
                "height": 1600,
                "size": 120000,
            },
        ],
    )
    await db_session.commit()

    meta = await generate_exports(db_session, test_settings)
    assert meta["total_posts"] == 3
    assert meta["page_size"] == 2
    assert meta["latest_id"] == 3

    # Verify static JSON exports
    posts_json = json.loads((test_settings.exports_dir / "posts.json").read_text(encoding="utf-8"))
    assert [p["id"] for p in posts_json] == [1, 2, 3]

    page0 = json.loads((test_settings.exports_dir / "pages" / "0.json").read_text(encoding="utf-8"))
    page1 = json.loads((test_settings.exports_dir / "pages" / "1.json").read_text(encoding="utf-8"))
    assert [p["id"] for p in page0] == [3, 2]
    assert [p["id"] for p in page1] == [1]

    # Verify RSS 2.0 XML well-formedness, multi-image <content:encoded>, <media:content>, ?shared=
    rss_raw = (test_settings.exports_dir / "rss.xml").read_text(encoding="utf-8")
    rss_root = ET.fromstring(rss_raw)
    items = rss_root.findall("./channel/item")
    assert len(items) == 3

    # First item is newest (post #3 with 3 images)
    item3 = items[0]
    assert item3.findtext("link") == "https://aza.moe/life?shared=3"
    assert item3.findtext("guid") == "https://aza.moe/life?shared=3"
    encoded_3 = item3.findtext(f"{{{NS_CONTENT}}}encoded") or ""
    assert encoded_3.count("<figure><img ") == 3
    assert "https://aza.moe/api/life/media/epochs/1/media/3a.jpg" in encoded_3
    assert "https://aza.moe/api/life/media/epochs/1/media/3b.jpg" in encoded_3
    assert "https://aza.moe/api/life/media/epochs/1/media/3c.jpg" in encoded_3

    media_contents = item3.findall(f"{{{NS_MEDIA}}}content")
    assert len(media_contents) == 3
    assert media_contents[0].attrib["url"] == "https://aza.moe/api/life/media/epochs/1/media/3a.jpg"
    assert media_contents[0].find(f"{{{NS_MEDIA}}}thumbnail") is not None

    # Second item (post #2) includes readable poll HTML
    encoded_2 = items[1].findtext(f"{{{NS_CONTENT}}}encoded") or ""
    assert "Favorite season?" in encoded_2
    assert "Spring (4 votes)" in encoded_2
    assert "Total votes: 10" in encoded_2

    # Verify Atom 1.0 XML well-formedness & accurate <updated>
    atom_raw = (test_settings.exports_dir / "atom.xml").read_text(encoding="utf-8")
    atom_root = ET.fromstring(atom_raw)
    entries = atom_root.findall(f"{{{NS_ATOM}}}entry")
    assert len(entries) == 3
    assert entries[0].findtext(f"{{{NS_ATOM}}}id") == "https://aza.moe/life?shared=3"
    content_3 = entries[0].find(f"{{{NS_ATOM}}}content")
    assert content_3 is not None and content_3.attrib["type"] == "html"
    assert (content_3.text or "").count("<figure><img ") == 3
    assert entries[1].findtext(f"{{{NS_ATOM}}}updated") == "2026-03-02T12:30:00Z"

    # Verify JSON Feed 1.1
    jf = json.loads((test_settings.exports_dir / "feed.json").read_text(encoding="utf-8"))
    assert jf["version"] == "https://jsonfeed.org/version/1.1"
    assert len(jf["items"]) == 3
    jf_item3 = jf["items"][0]
    assert jf_item3["id"] == "3"
    assert jf_item3["url"] == "https://aza.moe/life?shared=3"
    assert len(jf_item3["attachments"]) == 3
    assert jf_item3["attachments"][0]["url"] == "https://aza.moe/api/life/media/epochs/1/media/3a.jpg"
    assert jf_item3["attachments"][0]["mime_type"] == "image/jpeg"
    assert jf_item3["content_html"].count("<figure><img ") == 3
    assert jf["items"][1]["date_modified"] == "2026-03-02T12:30:00Z"


@pytest.mark.asyncio
async def test_feeds_strip_xml_control_chars_and_sanitize_html(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    epoch = await ensure_channel(db_session, channel_id=1, status="active", id_offset=0)
    # Bypass upsert_post sanitization by writing raw dirty HTML directly to test defense-in-depth in feeds
    dirty_post = Post(
        id=77,
        channel_id=epoch.id,
        tg_msg_id=77,
        date=datetime(2026, 5, 1, 8, 0, tzinfo=UTC),
        text_html=(
            "Control\x07\x0b\x0c\x1f\x7fText "
            '<b>bold</b> <script>alert("xss")</script> '
            '<a href="javascript:alert(1)">badlink</a> '
            '<img src="data:image/svg+xml,<svg onload=alert(1)>" />'
        ),
    )
    db_session.add(dirty_post)
    await db_session.commit()

    await generate_exports(db_session, test_settings)

    rss_raw = (test_settings.exports_dir / "rss.xml").read_text(encoding="utf-8")
    atom_raw = (test_settings.exports_dir / "atom.xml").read_text(encoding="utf-8")
    jf = json.loads((test_settings.exports_dir / "feed.json").read_text(encoding="utf-8"))

    # Must parse cleanly as XML 1.0 with zero control characters or XSS payloads
    rss_root = ET.fromstring(rss_raw)
    atom_root = ET.fromstring(atom_raw)
    for ctrl in ("\x07", "\x0b", "\x0c", "\x1f", "\x7f"):
        assert ctrl not in rss_raw
        assert ctrl not in atom_raw

    item = rss_root.find("./channel/item")
    assert item is not None
    encoded = item.findtext(f"{{{NS_CONTENT}}}encoded") or ""
    assert "<b>bold</b>" in encoded
    assert "<script>" not in encoded
    assert "javascript:" not in encoded
    assert "data:image/svg+xml" not in encoded

    assert "<script>" not in jf["items"][0]["content_html"]
    assert "javascript:" not in jf["items"][0]["content_html"]

    # Naive updated_at datetime + NUL byte (\x00) stripped cleanly in build_rss_xml
    nul_post = Post(
        id=78,
        channel_id=epoch.id,
        tg_msg_id=78,
        date=datetime(2026, 5, 1, 8, 5, tzinfo=UTC),
        text_html="Null\x00ByteText",
    )
    nul_post.images = []
    nul_post.poll = None
    naive_rss = build_rss_xml([nul_post], test_settings, updated_at=datetime(2026, 5, 1, 9, 0))
    assert "\x00" not in naive_rss
    ET.fromstring(naive_rss)
    assert atom_root.tag.endswith("feed")


@pytest.mark.asyncio
async def test_empty_db_exports_and_stale_page_cleanup(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    # 1. Empty DB export
    empty_meta = await generate_exports(db_session, test_settings)
    assert empty_meta["total_posts"] == 0
    assert empty_meta["latest_id"] is None
    assert json.loads((test_settings.exports_dir / "posts.json").read_text(encoding="utf-8")) == []
    assert (
        json.loads((test_settings.exports_dir / "pages" / "0.json").read_text(encoding="utf-8"))
        == []
    )
    ET.fromstring((test_settings.exports_dir / "rss.xml").read_text(encoding="utf-8"))
    ET.fromstring((test_settings.exports_dir / "atom.xml").read_text(encoding="utf-8"))
    jf_empty = json.loads((test_settings.exports_dir / "feed.json").read_text(encoding="utf-8"))
    assert jf_empty["items"] == []

    # 2. Seed 5 posts (with page_size=2 -> pages 0, 1, 2)
    epoch = await ensure_channel(db_session, channel_id=1, status="active", id_offset=0)
    for i in range(1, 6):
        await upsert_post(
            db_session,
            channel=epoch,
            tg_msg_id=i,
            date=datetime(2026, 6, i, 12, 0, tzinfo=UTC),
            text_html=f"Post #{i}",
        )
    await db_session.commit()

    meta_full = await generate_exports(db_session, test_settings)
    assert meta_full["total_posts"] == 5
    pages_dir = test_settings.exports_dir / "pages"
    assert (pages_dir / "0.json").is_file()
    assert (pages_dir / "1.json").is_file()
    assert (pages_dir / "2.json").is_file()

    # 3. Soft-delete 4 posts so only 1 remains -> stale pages 1.json & 2.json must be cleaned up!
    for i in range(1, 5):
        p = await db_session.get(Post, i)
        assert p is not None
        p.is_deleted = True
    await db_session.commit()

    meta_shrunk = await generate_exports(db_session, test_settings)
    assert meta_shrunk["total_posts"] == 1
    assert (pages_dir / "0.json").is_file()
    assert not (pages_dir / "1.json").exists()
    assert not (pages_dir / "2.json").exists()
