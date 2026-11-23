from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import ExifTags, Image, ImageDraw
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import OriginalUploadToken, Post
from app.db.repo import ensure_channel, get_post_by_id, upsert_post
from app.db.session import get_db_session
from app.media.fingerprint import compute_image_fingerprint
from app.media.matcher import (
    TargetPostImage,
    build_candidate_photo,
    match_originals_to_post_images,
)
from app.media.optimize import (
    InvalidImageError,
    process_original_upgrade,
    strip_gps_from_image_bytes,
    validate_image_bytes,
)
from app.routes import upload as upload_mod
from app.routes.upload import (
    create_upload_token,
    hash_upload_token,
)
from app.routes.upload import (
    router as upload_router,
)
from app.sse import SSEBus


def _draw_distinct_image(kind: int, size: tuple[int, int]) -> Image.Image:
    """Create visually distinct geometric images with rich frequency & color structure."""
    w, h = size
    im = Image.new("RGB", (w, h), (20, 20, 20))
    draw = ImageDraw.Draw(im)

    if kind == 0:
        for y in range(0, h, 8):
            r = min(255, int(200 + 55 * (y / h)))
            g = min(255, int(60 + 140 * (y / h)))
            b = 40
            draw.rectangle([0, y, w, y + 8], fill=(r, g, b))
        draw.ellipse([w * 0.15, h * 0.15, w * 0.55, h * 0.65], fill=(255, 240, 120))
        draw.polygon([(0, h), (w * 0.4, h * 0.55), (w, h)], fill=(45, 25, 65))
    elif kind == 1:
        cell = max(16, w // 10)
        for y in range(0, h, cell):
            for x in range(0, w, cell):
                c = (20, 110, 220) if ((x // cell + y // cell) % 2 == 0) else (10, 40, 110)
                draw.rectangle([x, y, x + cell, y + cell], fill=c)
        draw.rectangle([int(w * 0.7), 0, w, h], fill=(40, 220, 140))
    elif kind == 2:
        draw.rectangle([0, 0, w, h], fill=(245, 245, 240))
        draw.rectangle([0, 0, int(w * 0.45), int(h * 0.45)], fill=(210, 35, 50))
        draw.rectangle([int(w * 0.42), 0, int(w * 0.58), h], fill=(55, 20, 90))
        draw.rectangle([0, int(h * 0.6), w, int(h * 0.75)], fill=(30, 30, 40))
    else:
        for x in range(0, w, 10):
            g = min(255, int(80 + 150 * (x / w)))
            draw.rectangle([x, 0, x + 10, h], fill=(25, g, 65))
        draw.line([(0, 0), (w, h)], fill=(255, 230, 30), width=max(12, w // 18))
        draw.line([(0, h), (w, 0)], fill=(240, 90, 200), width=max(12, w // 18))
    return im


def _save_jpeg_bytes(im: Image.Image, *, quality: int = 92, add_gps: bool = False) -> bytes:
    buf = io.BytesIO()
    save_kwargs: dict[str, Any] = {"quality": quality}
    if add_gps:
        exif = im.getexif()
        exif[271] = "Fujifilm"  # Make
        exif[272] = "X-T5"  # Model
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        gps[1] = "N"
        gps[2] = (35.0, 40.0, 12.0)
        save_kwargs["exif"] = exif.tobytes()
    im.save(buf, "JPEG", **save_kwargs)
    return buf.getvalue()


def _make_tg_compressed(im: Image.Image, out_path: Path, max_dim: int = 960) -> Path:
    copy = im.copy()
    copy.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    copy.save(out_path, "JPEG", quality=72)
    return out_path


@pytest_asyncio.fixture
async def upload_client(
    test_settings: Settings,
    db_session: AsyncSession,
) -> AsyncIterator[tuple[AsyncClient, SSEBus]]:
    """Wire upload router to a REAL synchronous SSEBus() via app.state.sse_bus (B1 regression)."""
    test_settings.media_dir.mkdir(parents=True, exist_ok=True)
    test_settings.exports_dir.mkdir(parents=True, exist_ok=True)

    sse_bus = SSEBus()
    app = FastAPI()
    app.state.sse_bus = sse_bus
    app.include_router(upload_router, prefix=test_settings.API_PREFIX)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_db_session] = _override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, sse_bus


# ---------------------------------------------------------------------------
# 1. Optimizer & Validation Unit Tests (EXIF + XMP + WebP + ICC + Limits)
# ---------------------------------------------------------------------------


def test_validate_and_strip_gps_exif_and_webp_xmp(tmp_path: Path) -> None:
    # Reject SVG and non-image payloads
    with pytest.raises(InvalidImageError, match="SVG"):
        validate_image_bytes(
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        )

    with pytest.raises(InvalidImageError):
        validate_image_bytes(b"not-a-real-image-at-all")

    # Enforce pixel limit against decompression bombs
    big = _draw_distinct_image(0, (200, 200))
    big_bytes = _save_jpeg_bytes(big)
    with pytest.raises(InvalidImageError, match="pixel"):
        validate_image_bytes(big_bytes, max_pixels=10_000)

    # EXIF orientation 6 swaps width/height cleanly without copying full pixel buffer
    rect = _draw_distinct_image(0, (160, 100))
    orient_exif = rect.getexif()
    orient_exif[274] = 6
    buf_rot = io.BytesIO()
    rect.save(buf_rot, "JPEG", exif=orient_exif.tobytes())
    rot_info = validate_image_bytes(buf_rot.getvalue())
    assert (rot_info.width, rot_info.height) == (100, 160)

    # Strip JPEG GPS EXIF while keeping Make/Model intact
    with_gps = _save_jpeg_bytes(big, quality=95, add_gps=True)
    cleaned, stripped, out_ext = strip_gps_from_image_bytes(with_gps)
    assert stripped is True
    assert out_ext == ".jpg"

    with Image.open(io.BytesIO(cleaned)) as im:
        exif = im.getexif()
        assert int(ExifTags.IFD.GPSInfo) not in exif
        assert exif.get(271) == "Fujifilm"
        assert exif.get(272) == "X-T5"

    # Strip WebP XMP GPS + EXIF GPS while preserving camera EXIF and ICC profile (M5 regression)
    webp_buf = io.BytesIO()
    webp_exif = big.getexif()
    webp_exif[271] = "Leica"
    webp_gps = webp_exif.get_ifd(ExifTags.IFD.GPSInfo)
    webp_gps[1] = "N"
    webp_gps[2] = (35.0, 40.0, 0.0)
    xmp_gps = (
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        b'xmlns:exif="http://ns.adobe.com/exif/1.0/">'
        b'<rdf:Description exif:GPSLatitude="35,40N" exif:GPSLongitude="139,45E"/>'
        b"</rdf:RDF></x:xmpmeta>"
    )
    fake_icc = b"FAKE_ICC_PROFILE_BYTES_123456"
    big.save(
        webp_buf,
        "WEBP",
        quality=90,
        exif=webp_exif.tobytes(),
        xmp=xmp_gps,
        icc_profile=fake_icc,
    )
    cleaned_webp, webp_stripped, webp_ext = strip_gps_from_image_bytes(webp_buf.getvalue())
    assert webp_stripped is True
    assert webp_ext == ".webp"
    assert b"GPSLatitude" not in cleaned_webp
    assert b"GPSLongitude" not in cleaned_webp
    with Image.open(io.BytesIO(cleaned_webp)) as wim:
        assert wim.format == "WEBP"
        assert int(ExifTags.IFD.GPSInfo) not in wim.getexif()
        assert wim.getexif().get(271) == "Leica"
        assert "xmp" not in wim.info
        assert wim.info.get("icc_profile") == fake_icc

    # Non-GPS image is preserved verbatim without recompression
    no_gps = _save_jpeg_bytes(big, quality=95, add_gps=False)
    untouched, stripped2, ext2 = strip_gps_from_image_bytes(no_gps)
    assert stripped2 is False
    assert ext2 == ".jpg"
    assert untouched == no_gps

    # Full process_original_upgrade writes original + <=2048px display + <=1000px thumb atomically
    src = tmp_path / "DSC0001.JPG"
    large = _draw_distinct_image(0, (2600, 1800))
    src.write_bytes(_save_jpeg_bytes(large, quality=95, add_gps=True))

    media_root = tmp_path / "media"
    artifacts = process_original_upgrade(
        source_path=src,
        original_filename="../../evil/DSC0001.JPG",
        media_root=media_root,
        epoch_id=2,
        post_id=42,
        sort_order=0,
        strip_gps_exif=True,
    )
    assert artifacts.gps_stripped is True
    assert artifacts.original_name == "DSC0001.JPG"
    assert artifacts.width <= 2048 and artifacts.height <= 2048
    assert (media_root / artifacts.original_rel_url).is_file()
    assert (media_root / artifacts.display_rel_url).is_file()
    assert (media_root / artifacts.thumb_rel_url).is_file()
    with Image.open(media_root / artifacts.thumb_rel_url) as thumb_im:
        assert max(thumb_im.size) <= 1000

    # HEIC/HEIF upload validation, GPS stripping, original upgrade, and comment image conversion
    heic_buf = io.BytesIO()
    heic_exif = big.getexif()
    heic_exif[271] = "Apple"
    heic_gps = heic_exif.get_ifd(ExifTags.IFD.GPSInfo)
    heic_gps[1] = "N"
    heic_gps[2] = (43.0, 40.0, 0.0)
    big.save(heic_buf, "HEIF", quality=90, exif=heic_exif.tobytes())
    heic_raw = heic_buf.getvalue()

    heic_info = validate_image_bytes(heic_raw)
    assert heic_info.format == "HEIF"
    assert heic_info.extension == ".heic"

    cleaned_heic, heic_stripped, heic_ext = strip_gps_from_image_bytes(heic_raw)
    assert heic_stripped is True
    assert heic_ext == ".heic"
    with Image.open(io.BytesIO(cleaned_heic)) as him:
        assert him.format == "HEIF"
        assert int(ExifTags.IFD.GPSInfo) not in him.getexif()
        assert him.getexif().get(271) == "Apple"

    heic_src = tmp_path / "IMG_1234.HEIC"
    heic_src.write_bytes(heic_raw)
    heic_artifacts = process_original_upgrade(
        source_path=heic_src,
        original_filename="IMG_1234.HEIC",
        media_root=media_root,
        epoch_id=2,
        post_id=9616,
        sort_order=0,
        strip_gps_exif=True,
    )
    assert heic_artifacts.gps_stripped is True
    assert heic_artifacts.original_rel_url.endswith(".heic")
    assert (media_root / heic_artifacts.original_rel_url).is_file()
    assert (media_root / heic_artifacts.display_rel_url).is_file()
    assert (media_root / heic_artifacts.thumb_rel_url).is_file()

    from app.media.optimize import process_comment_image_upload

    comment_heic = process_comment_image_upload(heic_raw, media_root)
    assert comment_heic["media_type"] == "photo"
    assert comment_heic["mime_type"] == "image/webp"
    assert (media_root / comment_heic["url"]).is_file()


# ---------------------------------------------------------------------------
# 2. Visual Similarity Matcher Unit Tests (Hungarian + Burst Ambiguity)
# ---------------------------------------------------------------------------


def test_matcher_scrambled_full_album_and_partial_upload(tmp_path: Path) -> None:
    specs = [
        (0, (1600, 1200)),
        (1, (1200, 1600)),
        (2, (1800, 1000)),
        (3, (1400, 1400)),
    ]

    targets: list[TargetPostImage] = []
    orig_paths: list[Path] = []
    for idx, (kind, dims) in enumerate(specs):
        hi = _draw_distinct_image(kind, dims)
        orig_p = tmp_path / f"orig_{idx}.jpg"
        orig_p.write_bytes(_save_jpeg_bytes(hi, quality=95))
        orig_paths.append(orig_p)

        tg_p = _make_tg_compressed(hi, tmp_path / f"tg_{idx}.jpg")
        phash, rgb_sig, w, h = compute_image_fingerprint(tg_p)
        targets.append(
            TargetPostImage(
                image_id=100 + idx,
                sort_order=idx,
                url=f"epochs/2/media/tg_{idx}.jpg",
                thumb_url=None,
                phash=phash,
                rgb_sig=rgb_sig,
                width=w,
                height=h,
            )
        )

    scrambled_indices = [2, 0, 3, 1]
    candidates = [
        build_candidate_photo(f"cand_{orig_idx}", f"IMG_{orig_idx}.jpg", orig_paths[orig_idx])
        for orig_idx in scrambled_indices
    ]

    res = match_originals_to_post_images(candidates, targets)
    assert res.status == "confident"
    assert res.unmatched_files == []
    assert len(res.matches) == 4
    for m in res.matches:
        expected_idx = m.image_id - 100
        assert m.file_key == f"cand_{expected_idx}"
        assert m.confident is True
        assert m.distance < 0.12
        assert m.margin is not None and m.margin >= 0.06

    partial_candidates = [
        build_candidate_photo("p_3", "IMG_3.jpg", orig_paths[3]),
        build_candidate_photo("p_1", "IMG_1.jpg", orig_paths[1]),
    ]
    partial_res = match_originals_to_post_images(partial_candidates, targets)
    assert partial_res.status == "confident"
    assert len(partial_res.matches) == 2
    mapped = {m.file_key: m.image_id for m in partial_res.matches}
    assert mapped == {"p_1": 101, "p_3": 103}


def test_matcher_burst_shots_flagged_ambiguous(tmp_path: Path) -> None:
    base = _draw_distinct_image(0, (1200, 900))
    burst_a = base.copy()
    burst_b = base.copy()
    draw_b = ImageDraw.Draw(burst_b)
    draw_b.rectangle([50, 50, 120, 120], fill=(230, 210, 130))

    targets: list[TargetPostImage] = []
    candidates = []
    for idx, img in enumerate((burst_a, burst_b)):
        orig_p = tmp_path / f"burst_orig_{idx}.jpg"
        orig_p.write_bytes(_save_jpeg_bytes(img, quality=95))
        tg_p = _make_tg_compressed(img, tmp_path / f"burst_tg_{idx}.jpg")
        phash, rgb_sig, w, h = compute_image_fingerprint(tg_p)
        targets.append(
            TargetPostImage(
                image_id=200 + idx,
                sort_order=idx,
                url=f"epochs/2/media/burst_tg_{idx}.jpg",
                thumb_url=None,
                phash=phash,
                rgb_sig=rgb_sig,
                width=w,
                height=h,
            )
        )
        candidates.append(build_candidate_photo(f"burst_{idx}", f"BURST_{idx}.jpg", orig_p))

    res = match_originals_to_post_images(candidates, targets)
    assert res.status == "ambiguous"
    assert any(not m.confident and m.reason and "low_margin" in m.reason for m in res.matches)


# ---------------------------------------------------------------------------
# 3. Signed Upload UI & API Integration Tests (Real Postgres + Real SSEBus)
# ---------------------------------------------------------------------------


async def _seed_photo_post(
    db_session: AsyncSession,
    settings: Settings,
    pil_images: list[Image.Image],
    *,
    tg_msg_id: int = 10,
    text_html: str = "Album caption <script>alert(1)</script>",
) -> Post:
    channel = await ensure_channel(
        db_session,
        channel_id=2,
        tg_chat_id=-1002222222,
        tg_discussion_chat_id=-1003333333,
        title="Epoch 2",
        status="active",
        id_offset=0,
    )

    images_payload = []
    for idx, im in enumerate(pil_images):
        rel_url = f"epochs/2/media/seed_{tg_msg_id}_{idx}.jpg"
        abs_path = settings.media_dir / rel_url
        _make_tg_compressed(im, abs_path)
        phash, rgb_sig, w, h = compute_image_fingerprint(abs_path)
        images_payload.append(
            {
                "sort_order": idx,
                "tg_msg_id": tg_msg_id + idx,
                "url": rel_url,
                "thumb_url": rel_url,
                "width": w,
                "height": h,
                "phash": phash,
                "rgb_sig": rgb_sig,
            }
        )

    post = await upsert_post(
        db_session,
        channel=channel,
        tg_msg_id=tg_msg_id,
        tg_all_msg_ids=[tg_msg_id + i for i in range(len(pil_images))],
        date=datetime(2026, 11, 19, 10, 0, tzinfo=UTC),
        text_html=text_html,
        images=images_payload,
    )
    await db_session.commit()
    loaded = await get_post_by_id(db_session, post.id)
    assert loaded is not None
    return loaded


@pytest.mark.asyncio
async def test_upload_routes_get_invalid_expired_and_confident_apply_with_real_sse_bus(
    upload_client: tuple[AsyncClient, SSEBus],
    db_session: AsyncSession,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, sse_bus = upload_client
    sub_id, queue, _ = sse_bus.subscribe()

    img0 = _draw_distinct_image(0, (1600, 1200))
    img1 = _draw_distinct_image(1, (1200, 1600))
    post = await _seed_photo_post(
        db_session,
        test_settings,
        [img0, img1],
        text_html='Sunset <b>caption</b> <script>alert("xss")</script>',
    )

    # 1. Unknown token -> 404
    r_missing = await client.get("/api/life/upload/nonexistent-token")
    assert r_missing.status_code == 404

    # 2. Expired token -> 410
    expired_raw = "expired-token-value-12345"
    db_session.add(
        OriginalUploadToken(
            token_hash=hash_upload_token(expired_raw),
            post_id=post.id,
            expires_at=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    await db_session.commit()
    r_exp = await client.get(f"/api/life/upload/{expired_raw}")
    assert r_exp.status_code == 410

    # 3. Valid token GET renders clean XSS-safe HTML dropzone + security headers (M10)
    raw_token, _ = await create_upload_token(db_session, post.id, ttl_hours=72)
    await db_session.commit()

    r_page = await client.get(f"/api/life/upload/{raw_token}")
    assert r_page.status_code == 200
    assert "text/html" in r_page.headers["content-type"]
    assert r_page.headers.get("referrer-policy") == "no-referrer"
    assert "default-src 'none'" in (r_page.headers.get("content-security-policy") or "")
    assert f"Post #{post.id}" in r_page.text
    assert 'alert("xss")' not in r_page.text
    assert "dropzone" in r_page.text

    # 4. Reject SVG upload with 400
    r_svg = await client.post(
        f"/api/life/upload/{raw_token}",
        files=[("files[]", ("evil.svg", b"<svg><script>alert(1)</script></svg>", "image/svg+xml"))],
    )
    assert r_svg.status_code == 400

    # 5. Enforce MAX_UPLOAD_BYTES streaming cap (M7 regression)
    b0 = _save_jpeg_bytes(img0, quality=95, add_gps=True)
    b1 = _save_jpeg_bytes(img1, quality=95, add_gps=False)
    monkeypatch.setattr(upload_mod, "MAX_UPLOAD_BYTES", 512)
    r_too_big = await client.post(
        f"/api/life/upload/{raw_token}",
        files=[("files[]", ("huge.jpg", b0, "image/jpeg"))],
    )
    assert r_too_big.status_code == 400
    assert "size" in r_too_big.json()["detail"].lower()
    monkeypatch.undo()

    # 6. Upload scrambled originals -> auto-applied & published via synchronous SSEBus.publish (B1)!
    r_up = await client.post(
        f"/api/life/upload/{raw_token}",
        files=[
            ("files[]", ("second_shot.jpg", b1, "image/jpeg")),
            ("files[]", ("first_shot.jpg", b0, "image/jpeg")),
        ],
    )
    assert r_up.status_code == 200, r_up.text
    body = r_up.json()
    assert body["status"] == "applied"
    assert len(body["matches"]) == 2

    by_order = {m["sort_order"]: m for m in body["matches"]}
    assert by_order[0]["original_name"] == "first_shot.jpg"
    assert by_order[0]["gps_stripped"] is True
    assert by_order[1]["original_name"] == "second_shot.jpg"
    assert by_order[1]["gps_stripped"] is False

    refreshed = await get_post_by_id(db_session, post.id)
    assert refreshed is not None
    for img in refreshed.images:
        assert img.original_url is not None
        assert (test_settings.media_dir / img.original_url).is_file()
        assert (test_settings.media_dir / img.url).is_file()
        assert img.thumb_url and (test_settings.media_dir / img.thumb_url).is_file()

    posts_export = json.loads((test_settings.exports_dir / "posts.json").read_text(encoding="utf-8"))
    assert posts_export[0]["images"][0]["original_name"] == "first_shot.jpg"

    sse_event = queue.get_nowait()
    assert sse_event.event == "post.updated"
    assert sse_event.data["id"] == post.id
    assert sse_event.data["images"][0]["original_name"] == "first_shot.jpg"
    sse_bus.unsubscribe(sub_id)


@pytest.mark.asyncio
async def test_upload_routes_burst_confirm_security_and_double_submit(
    upload_client: tuple[AsyncClient, SSEBus],
    db_session: AsyncSession,
    test_settings: Settings,
) -> None:
    client, sse_bus = upload_client
    sub_id, queue, _ = sse_bus.subscribe()

    base = _draw_distinct_image(2, (1200, 900))
    burst_a = base.copy()
    burst_b = base.copy()
    ImageDraw.Draw(burst_b).rectangle([20, 20, 90, 90], fill=(220, 50, 60))

    post = await _seed_photo_post(db_session, test_settings, [burst_a, burst_b], tg_msg_id=10)
    other_post = await _seed_photo_post(
        db_session, test_settings, [_draw_distinct_image(3, (800, 800))], tg_msg_id=30
    )

    raw_token, _ = await create_upload_token(db_session, post.id)
    other_token, _ = await create_upload_token(db_session, other_post.id)
    await db_session.commit()

    # Upload near-identical burst pair -> needs_confirmation
    r_up = await client.post(
        f"/api/life/upload/{raw_token}",
        files=[
            ("files[]", ("burst_a.jpg", _save_jpeg_bytes(burst_a, quality=95), "image/jpeg")),
            ("files[]", ("burst_b.jpg", _save_jpeg_bytes(burst_b, quality=95), "image/jpeg")),
        ],
    )
    assert r_up.status_code == 200
    data = r_up.json()
    assert data["status"] == "needs_confirmation"
    staging_id = data["staging_id"]
    proposed = data["proposed"]
    assert len(proposed) == 2
    assert (test_settings.DATA_DIR / "staging" / staging_id / "manifest.json").is_file()
    assert queue.empty()

    img_ids = [img.id for img in sorted(post.images, key=lambda i: i.sort_order)]
    key_by_name = {item["filename"]: item["file_key"] for item in proposed}

    # Security 1: staging_id path traversal -> 400
    r_trav = await client.post(
        f"/api/life/upload/{raw_token}/confirm",
        json={
            "staging_id": "../../etc/passwd",
            "matches": [{"file_key": key_by_name["burst_a.jpg"], "image_id": img_ids[0]}],
        },
    )
    assert r_trav.status_code == 400

    # Security 2: cross-token confirm using another post's token -> 403 (and manifest restored!)
    r_cross = await client.post(
        f"/api/life/upload/{other_token}/confirm",
        json={
            "staging_id": staging_id,
            "matches": [
                {"file_key": key_by_name["burst_a.jpg"], "image_id": other_post.images[0].id}
            ],
        },
    )
    assert r_cross.status_code == 403
    assert (test_settings.DATA_DIR / "staging" / staging_id / "manifest.json").is_file()

    # Valid confirm applies cleanly
    payload = {
        "staging_id": staging_id,
        "matches": [
            {"file_key": key_by_name["burst_a.jpg"], "image_id": img_ids[0]},
            {"file_key": key_by_name["burst_b.jpg"], "image_id": img_ids[1]},
        ],
    }
    r_confirm = await client.post(f"/api/life/upload/{raw_token}/confirm", json=payload)
    assert r_confirm.status_code == 200, r_confirm.text
    conf_data = r_confirm.json()
    assert conf_data["status"] == "applied"
    assert len(conf_data["matches"]) == 2
    assert not (test_settings.DATA_DIR / "staging" / staging_id).exists()

    # Second submit after apply -> 404 (atomic claim prevents double-apply)
    r_dup = await client.post(f"/api/life/upload/{raw_token}/confirm", json=payload)
    assert r_dup.status_code == 404

    sse_event = queue.get_nowait()
    assert sse_event.event == "post.updated"
    assert sse_event.data["id"] == post.id
    sse_bus.unsubscribe(sub_id)
