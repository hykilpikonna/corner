from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.repo import ensure_channel, upsert_post
from app.main import create_app


@pytest.mark.asyncio
async def test_health_posts_exports_feeds_and_media_routes(
    api_client: AsyncClient, db_session: AsyncSession, test_settings: Settings, tmp_path: Path
) -> None:
    # 1. Health endpoint
    r_health = await api_client.get("/health")
    assert r_health.status_code == 200
    assert r_health.json() == {"status": "ok", "database": "ok"}

    # Seed 3 posts (one soft-deleted)
    epoch = await ensure_channel(db_session, channel_id=1, status="active", id_offset=0)
    await upsert_post(
        db_session,
        channel=epoch,
        tg_msg_id=1,
        date=datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
        text_html="Hello #1",
    )
    await upsert_post(
        db_session,
        channel=epoch,
        tg_msg_id=2,
        date=datetime(2026, 4, 2, 10, 0, tzinfo=UTC),
        text_html="Deleted #2",
        is_deleted=True,
    )
    await upsert_post(
        db_session,
        channel=epoch,
        tg_msg_id=3,
        date=datetime(2026, 4, 3, 10, 0, tzinfo=UTC),
        text_html="Hello #3",
        images=[{"url": "epochs/1/media/pic.jpg", "width": 100, "height": 100}],
    )
    await db_session.commit()

    # Create media files + outside secret file to verify path-traversal rejection
    valid_media = test_settings.media_dir / "epochs" / "1" / "media" / "pic.jpg"
    valid_media.parent.mkdir(parents=True, exist_ok=True)
    valid_media.write_bytes(b"\xff\xd8\xff\xe0mock-jpeg")

    double_dot_media = test_settings.media_dir / "epochs" / "1" / "media" / "photo..jpg"
    double_dot_media.write_bytes(b"\xff\xd8\xff\xe0double-dot-jpeg")

    svg_file = test_settings.media_dir / "epochs" / "1" / "media" / "xss.svg"
    svg_file.write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')

    html_file = test_settings.media_dir / "epochs" / "1" / "media" / "evil.html"
    html_file.write_text("<html><body><script>alert(1)</script></body></html>")

    js_file = test_settings.media_dir / "epochs" / "1" / "media" / "payload.js"
    js_file.write_text("alert(1)")

    outside_secret = tmp_path / "secret.txt"
    outside_secret.write_text("TOP_SECRET_DO_NOT_LEAK", encoding="utf-8")

    # Symlink inside media pointing outside media_dir
    escape_symlink = test_settings.media_dir / "escape.txt"
    escape_symlink.symlink_to(outside_secret)

    # 2. Exports routes (generated on demand if missing)
    r_posts_json = await api_client.get("/api/life/exports/posts.json")
    assert r_posts_json.status_code == 200
    assert [p["id"] for p in r_posts_json.json()] == [1, 3]

    r_meta = await api_client.get("/api/life/exports/pages/meta.json")
    assert r_meta.status_code == 200
    assert r_meta.json()["total_posts"] == 2
    assert r_meta.json()["latest_id"] == 3

    r_page0 = await api_client.get("/api/life/exports/pages/0.json")
    assert r_page0.status_code == 200
    assert [p["id"] for p in r_page0.json()] == [3, 1]

    r_page_missing = await api_client.get("/api/life/exports/pages/99.json")
    assert r_page_missing.status_code == 404

    # 3. Incremental posts & single post routes
    r_inc = await api_client.get("/api/life/posts?after_id=1&limit=10")
    assert r_inc.status_code == 200
    assert [p["id"] for p in r_inc.json()["posts"]] == [3]

    r_single = await api_client.get("/api/life/posts/3")
    assert r_single.status_code == 200
    assert r_single.json()["id"] == 3

    assert (await api_client.get("/api/life/posts/2")).status_code == 404  # deleted
    assert (await api_client.get("/api/life/posts/999")).status_code == 404  # missing

    # 4. Feed endpoints (inline browser viewing + nosniff for XML & JSON feeds)
    r_rss = await api_client.get("/api/life/feeds/rss.xml")
    assert r_rss.status_code == 200
    assert "application/xml" in r_rss.headers["content-type"]
    assert r_rss.headers["content-disposition"] == "inline"
    assert r_rss.headers["x-content-type-options"] == "nosniff"
    ET.fromstring(r_rss.text)

    r_atom = await api_client.get("/api/life/feeds/atom.xml")
    assert r_atom.status_code == 200
    assert "application/xml" in r_atom.headers["content-type"]
    assert r_atom.headers["content-disposition"] == "inline"
    assert r_atom.headers["x-content-type-options"] == "nosniff"
    ET.fromstring(r_atom.text)

    r_json = await api_client.get("/api/life/feeds/feed.json")
    assert r_json.status_code == 200
    assert "application/feed+json" in r_json.headers["content-type"]
    assert r_json.headers["content-disposition"] == "inline"
    assert r_json.headers["x-content-type-options"] == "nosniff"
    assert r_json.json()["version"] == "https://jsonfeed.org/version/1.1"

    # 5. Media serving: safe inline JPEG + security headers + double-dot filename
    r_ok = await api_client.get("/api/life/media/epochs/1/media/pic.jpg")
    assert r_ok.status_code == 200
    assert r_ok.content == b"\xff\xd8\xff\xe0mock-jpeg"
    assert r_ok.headers["content-type"].startswith("image/jpeg")
    assert "immutable" in r_ok.headers["cache-control"]
    assert r_ok.headers["x-content-type-options"] == "nosniff"
    assert r_ok.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert "content-disposition" not in r_ok.headers

    # Legitimate filename containing consecutive dots ("photo..jpg") must succeed!
    r_dd = await api_client.get("/api/life/media/epochs/1/media/photo..jpg")
    assert r_dd.status_code == 200
    assert r_dd.content == b"\xff\xd8\xff\xe0double-dot-jpeg"
    assert r_dd.headers["content-type"].startswith("image/jpeg")
    assert r_dd.headers["x-content-type-options"] == "nosniff"

    # Same-origin stored XSS protection: SVG/HTML/JS must be forced to attachment + octet-stream
    for bad_asset in ("xss.svg", "evil.html", "payload.js"):
        r_bad = await api_client.get(f"/api/life/media/epochs/1/media/{bad_asset}")
        assert r_bad.status_code == 200
        assert r_bad.headers["content-type"] == "application/octet-stream"
        assert r_bad.headers["x-content-type-options"] == "nosniff"
        assert r_bad.headers["content-security-policy"] == "default-src 'none'; sandbox"
        assert r_bad.headers["content-disposition"].startswith("attachment;")

    assert (await api_client.get("/api/life/media/does-not-exist.jpg")).status_code == 404

    traversal_paths = [
        "/api/life/media/../../secret.txt",
        "/api/life/media/epochs/1/../../../../secret.txt",
        "/api/life/media/%2e%2e/%2e%2e/secret.txt",
        "/api/life/media/%252e%252e/%252e%252e/secret.txt",
        "/api/life/media/epochs/1/%2e%2e/%2e%2e/%2e%2e/secret.txt",
        "/api/life/media/epochs/1%2f..%2f..%2f..%2fsecret.txt",
        "/api/life/media/escape.txt",
    ]
    for bad_url in traversal_paths:
        resp = await api_client.get(bad_url)
        assert resp.status_code in (
            400,
            403,
            404,
        ), f"Expected rejection for {bad_url}, got {resp.status_code}"
        assert "TOP_SECRET_DO_NOT_LEAK" not in resp.text


def test_config_hardening_and_create_app_settings_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Comma-separated env strings work cleanly with NoDecode
    monkeypatch.setenv("CORS_ORIGINS", "https://aza.moe, https://preview.aza.moe")
    monkeypatch.setenv("OWNER_RESERVED_NAMES", "owner, admin, aza")
    monkeypatch.setenv("BASE_URL", "https://aza.moe")
    monkeypatch.setenv("SECRET_KEY", "a-very-strong-random-production-secret-key-0123456789")
    cfg = Settings(_env_file=None, DATA_DIR=tmp_path / "data")
    assert cfg.CORS_ORIGINS == ["https://aza.moe", "https://preview.aza.moe"]
    assert cfg.OWNER_RESERVED_NAMES == ["owner", "admin", "aza"]

    # create_app(custom_settings) registers get_settings override automatically
    app = create_app(cfg)
    assert get_settings in app.dependency_overrides
    assert app.dependency_overrides[get_settings]() is cfg

    # Wildcard '*' in CORS_ORIGINS rejected
    with pytest.raises(ValidationError, match="Wildcard"):
        Settings(
            _env_file=None,
            BASE_URL="http://localhost:8000",
            CORS_ORIGINS=["*"],
        )

    # Weak or placeholder SECRET_KEY rejected when BASE_URL is HTTPS
    for bad_secret in ("short-key", "change-me-in-production", "replace-with-a-random-64-char-hex-secret"):
        with pytest.raises(ValidationError, match="SECRET_KEY"):
            Settings(
                _env_file=None,
                BASE_URL="https://aza.moe",
                CORS_ORIGINS=["https://aza.moe"],
                SECRET_KEY=bad_secret,
            )


@pytest.mark.asyncio
async def test_exports_emoji_serving_epoch_fallback_and_traversal_rejection(
    api_client: AsyncClient, test_settings: Settings, tmp_path: Path
) -> None:
    # 1. Direct file in exports/emoji/ (.webp)
    export_emoji_dir = test_settings.exports_dir / "emoji"
    export_emoji_dir.mkdir(parents=True, exist_ok=True)
    (export_emoji_dir / "1001.webp").write_bytes(b"RIFF\x00\x00\x00\x00WEBPdirect")

    # 2. Epoch fallback: epoch 2 overrides epoch 1 (sorted descending), plus .tgs & unknown type
    epoch1_emoji = test_settings.media_dir / "epochs" / "1" / "emoji"
    epoch2_emoji = test_settings.media_dir / "epochs" / "2" / "emoji"
    epoch1_emoji.mkdir(parents=True, exist_ok=True)
    epoch2_emoji.mkdir(parents=True, exist_ok=True)
    (epoch1_emoji / "2002.webp").write_bytes(b"RIFF\x00\x00\x00\x00WEBPepoch1")
    (epoch2_emoji / "2002.webp").write_bytes(b"RIFF\x00\x00\x00\x00WEBPepoch2")
    (epoch1_emoji / "3003.tgs").write_bytes(b"\x1f\x8b\x08mock-tgs")
    (epoch1_emoji / "4004.apng").write_bytes(b"\x89PNGmock-apng")
    (epoch1_emoji / "evil.svg").write_text("<svg><script>alert(1)</script></svg>")

    outside_secret = tmp_path / "secret_emoji.txt"
    outside_secret.write_text("SECRET_EMOJI_DO_NOT_LEAK", encoding="utf-8")
    (export_emoji_dir / "escape.webp").symlink_to(outside_secret)

    # Direct exports/emoji hit
    r_direct = await api_client.get("/api/life/exports/emoji/1001.webp")
    assert r_direct.status_code == 200
    assert r_direct.content == b"RIFF\x00\x00\x00\x00WEBPdirect"
    assert r_direct.headers["content-type"].startswith("image/webp")
    assert "immutable" in r_direct.headers["cache-control"]
    assert r_direct.headers["x-content-type-options"] == "nosniff"
    assert r_direct.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert "content-disposition" not in r_direct.headers

    # Epoch fallback prefers higher epoch (epoch 2 over epoch 1)
    r_epoch = await api_client.get("/api/life/exports/emoji/2002.webp")
    assert r_epoch.status_code == 200
    assert r_epoch.content == b"RIFF\x00\x00\x00\x00WEBPepoch2"
    assert r_epoch.headers["content-type"].startswith("image/webp")

    # .tgs served with application/x-tgsticker + security headers
    r_tgs = await api_client.get("/api/life/exports/emoji/3003.tgs")
    assert r_tgs.status_code == 200
    assert r_tgs.content == b"\x1f\x8b\x08mock-tgs"
    assert r_tgs.headers["content-type"] == "application/x-tgsticker"
    assert r_tgs.headers["x-content-type-options"] == "nosniff"
    assert r_tgs.headers["content-security-policy"] == "default-src 'none'; sandbox"

    # .apng served inline as image/apng
    r_apng = await api_client.get("/api/life/exports/emoji/4004.apng")
    assert r_apng.status_code == 200
    assert r_apng.headers["content-type"] == "image/apng"

    # Unknown/active types (like .svg) forced to safe attachment download
    r_svg = await api_client.get("/api/life/exports/emoji/evil.svg")
    assert r_svg.status_code == 200
    assert r_svg.headers["content-type"] == "application/octet-stream"
    assert r_svg.headers["content-disposition"].startswith("attachment;")

    # Missing file -> 404
    assert (await api_client.get("/api/life/exports/emoji/missing.webp")).status_code == 404

    # Path traversal & symlink escape rejected (400/403/404)
    for bad_url in (
        "/api/life/exports/emoji/../../secret_emoji.txt",
        "/api/life/exports/emoji/%2e%2e/%2e%2e/secret_emoji.txt",
        "/api/life/exports/emoji/%252e%252e/secret_emoji.txt",
        "/api/life/exports/emoji/escape.webp",
    ):
        resp = await api_client.get(bad_url)
        assert resp.status_code in (400, 403, 404), f"Failed for {bad_url}: {resp.status_code}"
        assert "SECRET_EMOJI_DO_NOT_LEAK" not in resp.text
