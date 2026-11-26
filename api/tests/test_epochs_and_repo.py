from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import WebPollVote, WebReaction
from app.db.repo import (
    compute_merged_poll,
    compute_merged_reactions,
    ensure_channel,
    get_post_by_id,
    resolve_absolute_media_url,
    resolve_media_url,
    serialize_post,
    upsert_post,
)


@pytest.mark.asyncio
async def test_epoch2_canonical_id_continuation_zero_collisions(db_session: AsyncSession) -> None:
    # Epoch 1 (archived) with non-contiguous IDs ending at 42
    epoch1 = await ensure_channel(
        db_session,
        channel_id=1,
        tg_chat_id=-1001000001,
        title="Epoch 1",
        status="archived",
        id_offset=0,
    )
    for mid in (1, 5, 42):
        await upsert_post(
            db_session,
            channel=epoch1,
            canonical_id=mid,
            tg_msg_id=mid,
            date=datetime(2025, 1, 1, 12, mid % 60, tzinfo=UTC),
            text_html=f"Epoch 1 post {mid}",
        )
    await db_session.commit()

    # Calling ensure_channel on existing channel without status/id_offset preserves them
    refreshed_e1 = await ensure_channel(db_session, channel_id=1, title="Epoch 1 Renamed")
    assert refreshed_e1.status == "archived"
    assert refreshed_e1.id_offset == 0
    assert refreshed_e1.title == "Epoch 1 Renamed"

    # Epoch 2 (active) should auto-assign id=2 and id_offset=42
    epoch2 = await ensure_channel(
        db_session,
        tg_chat_id=-1002000002,
        title="Epoch 2",
        status="active",
    )
    assert epoch2.id == 2
    assert epoch2.id_offset == 42

    # Insert Epoch 2 posts with tg_msg_id=1, 2, 5 (which exist in Epoch 1!)
    p_e2_1 = await upsert_post(
        db_session,
        channel=epoch2,
        tg_msg_id=1,
        date=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        text_html="Epoch 2 first post",
    )
    p_e2_2 = await upsert_post(
        db_session,
        channel=epoch2,
        tg_msg_id=2,
        date=datetime(2026, 1, 1, 12, 5, tzinfo=UTC),
        text_html="Epoch 2 second post",
    )
    p_e2_5 = await upsert_post(
        db_session,
        channel=epoch2,
        tg_msg_id=5,
        date=datetime(2026, 1, 1, 12, 10, tzinfo=UTC),
        text_html="Epoch 2 fifth post",
    )
    await db_session.commit()

    assert p_e2_1.id == 43
    assert p_e2_2.id == 44
    assert p_e2_5.id == 47

    # Re-upserting (channel=2, tg_msg_id=1) updates canonical ID 43 in place
    p_e2_1_updated = await upsert_post(
        db_session,
        channel=epoch2,
        tg_msg_id=1,
        date=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        edited_at=datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
        text_html="Epoch 2 first post (edited)",
    )
    await db_session.commit()
    assert p_e2_1_updated.id == 43
    assert p_e2_1_updated.text_html == "Epoch 2 first post (edited)"


@pytest.mark.asyncio
async def test_upsert_post_preserves_workflow_b_original_images(db_session: AsyncSession) -> None:
    """Re-syncing a post via upsert_post must preserve Workflow B original_url, display url & fingerprints."""
    epoch = await ensure_channel(db_session, channel_id=1, status="active", id_offset=0)
    sig_bytes = bytes(range(256)) * 3

    post = await upsert_post(
        db_session,
        channel=epoch,
        tg_msg_id=100,
        date=datetime(2026, 2, 10, 12, 0, tzinfo=UTC),
        text_html="Initial caption",
        images=[
            {
                "sort_order": 0,
                "tg_msg_id": 100,
                "url": "epochs/1/media/100_upgraded_2048.webp",
                "thumb_url": "epochs/1/media/100_thumb.webp",
                "original_url": "epochs/1/originals/100_0_deadbeef.dng",
                "original_size": 31_457_280,
                "original_name": "IMG_100.DNG",
                "width": 2048,
                "height": 1365,
                "phash": "0123456789abcdef",
                "rgb_sig": sig_bytes,
            },
            {
                "sort_order": 1,
                "tg_msg_id": 101,
                "url": "epochs/1/media/101.jpg",
                "width": 1280,
                "height": 960,
                "phash": "fedcba9876543210",
                "rgb_sig": sig_bytes,
            },
        ],
    )
    await db_session.commit()

    before = await get_post_by_id(db_session, post.id)
    assert before is not None
    orig_img0_id = before.images[0].id

    # Simulate a subsequent TG edit / 15-min reconciler sweep with compressed TG URL and no original_url
    await upsert_post(
        db_session,
        channel=epoch,
        tg_msg_id=100,
        date=datetime(2026, 2, 10, 12, 0, tzinfo=UTC),
        edited_at=datetime(2026, 2, 10, 12, 15, tzinfo=UTC),
        text_html="Edited caption",
        views=42,
        images=[
            {
                "sort_order": 0,
                "tg_msg_id": 100,
                "url": "epochs/1/media/100_tg_compressed.jpg",
                "width": 1280,
                "height": 853,
            },
            {
                "sort_order": 1,
                "tg_msg_id": 101,
                "url": "epochs/1/media/101_updated.jpg",
                "width": 1280,
                "height": 960,
            },
        ],
    )
    await db_session.commit()

    after = await get_post_by_id(db_session, post.id)
    assert after is not None
    assert after.text_html == "Edited caption"
    assert after.views == 42
    assert len(after.images) == 2

    img0, img1 = after.images[0], after.images[1]
    # Updated in place, Workflow B fields + upgraded 2048px display url + fingerprints preserved!
    assert img0.id == orig_img0_id
    assert img0.url == "epochs/1/media/100_upgraded_2048.webp"
    assert img0.width == 2048
    assert img0.height == 1365
    assert img0.original_url == "epochs/1/originals/100_0_deadbeef.dng"
    assert img0.original_size == 31_457_280
    assert img0.original_name == "IMG_100.DNG"
    assert img0.phash == "0123456789abcdef"
    assert img0.rgb_sig == sig_bytes

    # Non-upgraded image updates its URL while still preserving its fingerprints!
    assert img1.url == "epochs/1/media/101_updated.jpg"
    assert img1.phash == "fedcba9876543210"
    assert img1.rgb_sig == sig_bytes


@pytest.mark.asyncio
async def test_merged_reactions_polls_and_original_image_serialization(
    db_session: AsyncSession,
) -> None:
    epoch = await ensure_channel(db_session, channel_id=1, status="active", id_offset=0)
    post = await upsert_post(
        db_session,
        channel=epoch,
        tg_msg_id=10,
        date=datetime(2026, 2, 1, 9, 0, tzinfo=UTC),
        text_html="Original photo + reactions + poll",
        views=99,
        images=[
            {
                "url": "epochs/1/media/10_2048.webp",
                "thumb_url": "epochs/1/media/10_thumb.webp",
                "original_url": "epochs/1/originals/10_0_abc123.dng",
                "original_size": 24_500_000,
                "original_name": "DSC00010.DNG",
                "width": 2048,
                "height": 1365,
            }
        ],
        tg_reactions=[
            {"emoji": "❤️", "count": 5},
            {"emoji": "🔥", "count": 1},
        ],
        poll={
            "id": "poll_10",
            "question": "Pick one",
            "type": "REGULAR",
            "is_closed": False,
            "is_anonymous": True,
            "allow_multiple_answers": False,
            "options": [
                {"text": "Alpha", "tg_voter_count": 3, "data": "0"},
                {"text": "Beta", "tg_voter_count": 2, "data": "1"},
            ],
        },
    )
    # Add web reactions and web poll votes
    db_session.add_all(
        [
            WebReaction(post_id=post.id, reaction_key="e:❤️", emoji="❤️", actor_key="u:1"),
            WebReaction(post_id=post.id, reaction_key="e:❤️", emoji="❤️", actor_key="v:abc"),
            WebReaction(post_id=post.id, reaction_key="e:🎉", emoji="🎉", actor_key="u:1"),
            WebPollVote(poll_id="poll_10", option_idx=0, actor_key="u:1"),
            WebPollVote(poll_id="poll_10", option_idx=1, actor_key="v:abc"),
        ]
    )
    await db_session.commit()

    loaded = await get_post_by_id(db_session, post.id)
    assert loaded is not None

    merged_r = compute_merged_reactions(loaded, actor_key="u:1")
    by_key = {r["reaction_key"]: r for r in merged_r}
    assert by_key["e:❤️"]["tg_count"] == 5
    assert by_key["e:❤️"]["web_count"] == 2
    assert by_key["e:❤️"]["count"] == 7
    assert by_key["e:❤️"]["reacted"] is True
    assert by_key["e:🔥"]["reacted"] is False
    assert by_key["e:🎉"]["count"] == 1
    assert by_key["e:🎉"]["reacted"] is True

    assert loaded.poll is not None
    merged_p = compute_merged_poll(loaded.poll, actor_key="u:1")
    assert merged_p["total_voter_count"] == 7
    assert merged_p["my_votes"] == [0]
    assert merged_p["options"][0]["voter_count"] == 4
    assert merged_p["options"][1]["voter_count"] == 3

    canonical = serialize_post(loaded, api_prefix="/api/life")
    assert canonical["views"] == "99"
    img = canonical["images"][0]
    assert img["url"] == "/api/life/media/epochs/1/media/10_2048.webp"
    assert img["thumb"] == "/api/life/media/epochs/1/media/10_thumb.webp"
    assert img["original_url"] == "/api/life/media/epochs/1/originals/10_0_abc123.dng"
    assert img["original_size"] == 24_500_000
    assert img["original_name"] == "DSC00010.DNG"
    assert canonical["reactions"][0] == {"emoji": "❤️", "count": 7}
    assert canonical["files"][0]["media_type"] == "poll"
    assert canonical["files"][0]["total_voter_count"] == 7


def test_url_resolution_blocks_disallowed_schemes() -> None:
    for bad in ("data:text/html,<script>alert(1)</script>", "//evil.example/a.jpg", "javascript:alert(1)"):
        assert resolve_media_url(bad) is None
        assert resolve_absolute_media_url(bad, base_url="https://aza.moe") is None

    # Leading "./" stripped cleanly without stripping leading dots from filenames
    assert resolve_media_url("./epochs/1/media/.hidden.jpg") == "/api/life/media/epochs/1/media/.hidden.jpg"
    assert (
        resolve_absolute_media_url("./epochs/1/media/photo.jpg", base_url="https://aza.moe")
        == "https://aza.moe/api/life/media/epochs/1/media/photo.jpg"
    )


async def test_serialize_post_enriches_empty_reply_from_target_media(db_session: AsyncSession) -> None:
    ch = await ensure_channel(db_session, tg_chat_id=-100999888, status="banned")
    parent_gif = await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=8741,
        date=datetime(2026, 6, 24, 17, 26, tzinfo=UTC),
        text_html=None,
        files=[
            {
                "media_type": "animation",
                "url": "epochs/1/media/8741.mp4",
                "thumb": "epochs/1/media/8741.mp4_thumb_thumb.webp",
                "mime_type": "video/mp4",
            }
        ],
    )
    child = await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=8742,
        date=datetime(2026, 6, 24, 17, 56, tzinfo=UTC),
        text_html="Reply to GIF",
        reply_to_post_id=parent_gif.id,
        reply_snapshot={"id": parent_gif.id},
    )
    await db_session.commit()

    loaded = await get_post_by_id(db_session, child.id)
    assert loaded is not None
    dto = serialize_post(loaded, api_prefix="/api/life")
    assert dto["reply"]["id"] == parent_gif.id
    assert dto["reply"]["text"] == "GIF"
    assert dto["reply"]["thumb"] == "/api/life/media/epochs/1/media/8741.mp4_thumb_thumb.webp"


async def test_enrich_incomplete_web_pages_backfills_only_existing_web_page_entries(
    test_settings: Settings, db_session: AsyncSession
) -> None:
    from io import BytesIO

    import httpx
    from PIL import Image

    from app.cli import enrich_incomplete_web_pages

    ch = await ensure_channel(db_session, tg_chat_id=-100777666, status="banned")
    with_wp = await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=9501,
        date=datetime(2026, 10, 25, 14, 14, tzinfo=UTC),
        text_html='<a href="https://youtu.be/BLgNIDmk8KI">link</a>',
        files=[{"media_type": "web_page", "url": "https://youtu.be/BLgNIDmk8KI", "prefer_large_media": True}],
    )
    without_wp = await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=9025,
        date=datetime(2026, 8, 1, 4, 56, tzinfo=UTC),
        text_html='#戴上耳机 <a href="https://youtu.be/eTfEPt96JSM">https://youtu.be/eTfEPt96JSM</a>',
        files=None,
    )
    embedded_link = await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=2938,
        date=datetime(2026, 7, 1, 1, 0, tzinfo=UTC),
        text_html='<a href="https://github.com/hykilpikonna/OpenHardware">源码在这里</a>',
        files=None,
    )
    long_essay = await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=138,
        date=datetime(2026, 7, 2, 1, 0, tzinfo=UTC),
        text_html=("A" * 240) + ' <a href="https://youtu.be/job52mBUvjc">https://youtu.be/job52mBUvjc</a>',
        files=None,
    )
    await db_session.commit()

    buf = BytesIO()
    Image.new("RGB", (120, 90), (40, 120, 200)).save(buf, format="JPEG")
    img_bytes = buf.getvalue()

    def _handler(req: httpx.Request) -> httpx.Response:
        if "oembed" in str(req.url):
            return httpx.Response(
                200,
                json={
                    "title": "Test Video Title",
                    "author_name": "Test Author",
                    "provider_name": "YouTube",
                    "thumbnail_url": "https://i.ytimg.com/vi/BLgNIDmk8KI/hqdefault.jpg",
                },
            )
        return httpx.Response(200, content=img_bytes)

    summary = await enrich_incomplete_web_pages(
        db_session,
        test_settings,
        channel_id=ch.id,
        transport=httpx.MockTransport(_handler),
    )
    assert summary == {"updated_posts": 1, "enriched_items": 1, "backfilled_links": 0}

    reloaded_wp = await get_post_by_id(db_session, with_wp.id)
    reloaded_no = await get_post_by_id(db_session, without_wp.id)
    assert reloaded_wp is not None and reloaded_wp.files is not None
    assert reloaded_wp.files[0]["title"] == "Test Video Title"
    assert reloaded_wp.files[0]["site_name"] == "YouTube"
    assert reloaded_wp.files[0]["author"] == "Test Author"
    assert reloaded_wp.files[0]["thumb"].endswith("_thumb.webp")
    assert reloaded_no is not None and reloaded_no.files is None

    summary2 = await enrich_incomplete_web_pages(
        db_session,
        test_settings,
        channel_id=ch.id,
        backfill_short_links=True,
        max_text_len=200,
        transport=httpx.MockTransport(_handler),
    )
    assert summary2 == {"updated_posts": 1, "enriched_items": 0, "backfilled_links": 1}

    reloaded_backfilled = await get_post_by_id(db_session, without_wp.id)
    reloaded_embedded = await get_post_by_id(db_session, embedded_link.id)
    reloaded_long = await get_post_by_id(db_session, long_essay.id)
    assert reloaded_backfilled is not None and reloaded_backfilled.files is not None
    assert reloaded_backfilled.files[0]["media_type"] == "web_page"
    assert reloaded_backfilled.files[0]["title"] == "Test Video Title"
    assert reloaded_embedded is not None and reloaded_embedded.files is None
    assert reloaded_long is not None and reloaded_long.files is None
