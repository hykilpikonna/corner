from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.repo import ensure_channel, upsert_post
from app.main import create_app
from app.media.fingerprint import compute_image_fingerprint
from app.routes.upload import create_upload_token
from app.tg import PeriodicReconciler, TelegramBridge, TelegramService


def _make_distinct_photo(path: Path) -> bytes:
    """Create a distinctive non-uniform JPEG image for high-confidence Workflow B matching."""
    img = Image.new("RGB", (640, 480))
    pixels = img.load()
    assert pixels is not None
    for y in range(480):
        for x in range(640):
            r = (x * 255) // 640
            g = (y * 255) // 480
            b = ((x + y) * 127) // 1120
            if 100 < x < 300 and 80 < y < 260:
                r, g, b = 240, 40, 200
            pixels[x, y] = (r, g, b)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="JPEG", quality=92)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=98)
    return buf.getvalue()


def test_create_app_mounts_all_contract_routes(test_settings: Settings) -> None:
    app = create_app(test_settings)
    openapi_paths = app.openapi()["paths"]
    mounted: set[tuple[str, str]] = {
        (method.upper(), path)
        for path, ops in openapi_paths.items()
        for method in ops
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }

    prefix = test_settings.API_PREFIX.rstrip("/")
    expected_routes = {
        # Health
        ("GET", "/health"),
        # Section 1: Posts, Media & Static Exports
        ("GET", f"{prefix}/exports/posts.json"),
        ("GET", f"{prefix}/exports/pages/meta.json"),
        ("GET", f"{prefix}/exports/pages/{{page}}.json"),
        ("GET", f"{prefix}/posts"),
        ("GET", f"{prefix}/posts/{{post_id}}"),
        ("GET", f"{prefix}/media/{{path}}"),
        ("GET", f"{prefix}/emojis/custom"),
        ("GET", f"{prefix}/stickers"),
        ("GET", f"{prefix}/feeds/rss.xml"),
        ("GET", f"{prefix}/feeds/atom.xml"),
        ("GET", f"{prefix}/feeds/feed.json"),
        # Section 2-5: Interactions, Reactions, Polls & Comments
        ("GET", f"{prefix}/interactions"),
        ("POST", f"{prefix}/posts/view"),
        ("POST", f"{prefix}/posts/{{post_id}}/reactions/toggle"),
        ("POST", f"{prefix}/comments/{{comment_id}}/reactions/toggle"),
        ("POST", f"{prefix}/polls/{{poll_id}}/vote"),
        ("GET", f"{prefix}/posts/{{post_id}}/comments"),
        ("POST", f"{prefix}/posts/{{post_id}}/comments"),
        ("POST", f"{prefix}/comments/media"),
        ("DELETE", f"{prefix}/comments/{{comment_id}}"),
        # Section 6: Auth & ALTCHA CAPTCHA
        ("GET", f"{prefix}/auth/me"),
        ("PATCH", f"{prefix}/auth/me"),
        ("POST", f"{prefix}/auth/logout"),
        ("GET", f"{prefix}/auth/oauth/{{provider}}/login"),
        ("GET", f"{prefix}/auth/oauth/{{provider}}/callback"),
        ("POST", f"{prefix}/auth/telegram/callback"),
        ("POST", f"{prefix}/auth/magic-link/request"),
        ("GET", f"{prefix}/auth/magic-link/verify"),
        ("GET", f"{prefix}/auth/unsubscribe"),
        ("POST", f"{prefix}/auth/unsubscribe"),
        ("GET", f"{prefix}/captcha/challenge"),
        # Section 7: Live SSE Stream
        ("GET", f"{prefix}/stream"),
        # Section 8: Workflow B Original Photo Upload
        ("GET", f"{prefix}/upload/{{token}}"),
        ("POST", f"{prefix}/upload/{{token}}"),
        ("POST", f"{prefix}/upload/{{token}}/confirm"),
    }

    missing = expected_routes - mounted
    assert not missing, f"Missing expected contract routes: {sorted(missing)}"


@pytest.mark.asyncio
async def test_app_lifespan_noop_tg_and_upload_to_real_sse_bus(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    """Verify full create_app() lifespan starts/stops cleanly with TG_BOT_TOKEN=None and wires upload -> SSEBus."""
    assert test_settings.TG_BOT_TOKEN is None

    # Seed a post with a fingerprinted TG photo + upload token before entering app lifespan
    epoch = await ensure_channel(db_session, channel_id=1, status="active", id_offset=0)
    tg_photo_rel = "epochs/1/media/42.jpg"
    tg_photo_path = test_settings.media_dir / tg_photo_rel
    original_bytes = _make_distinct_photo(tg_photo_path)
    phash, rgb_sig, width, height = compute_image_fingerprint(tg_photo_path)

    post = await upsert_post(
        db_session,
        channel=epoch,
        tg_msg_id=42,
        date=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        text_html="Lifespan wiring test photo",
        images=[
            {
                "sort_order": 0,
                "tg_msg_id": 42,
                "url": tg_photo_rel,
                "width": width,
                "height": height,
                "phash": phash,
                "rgb_sig": rgb_sig,
            }
        ],
    )
    raw_token, _ = await create_upload_token(db_session, post_id=post.id)
    await db_session.commit()

    app = create_app(test_settings)
    assert app.state.sse_publish == app.state.sse_bus.publish

    # Run real FastAPI lifespan (init_db, mailer, TelegramBridge/TelegramService/PeriodicReconciler)
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.tg_bridge, TelegramBridge)
        assert isinstance(app.state.tg_service, TelegramService)
        assert isinstance(app.state.reconciler, PeriodicReconciler)
        assert app.state.tg_service.is_running is False
        assert app.state.tg_bridge.client is None
        assert app.state.tg_service.bridge is app.state.tg_bridge
        assert app.state.reconciler.telegram_service is app.state.tg_service

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Real DB health check via lifespan-initialized session factory
            r_health = await client.get("/health")
            assert r_health.status_code == 200
            assert r_health.json() == {"status": "ok", "database": "ok"}

            # Subscribe directly to real app.state.sse_bus before uploading original photo
            sub_id, queue, _ = app.state.sse_bus.subscribe(post_ids={post.id})
            try:
                r_page = await client.get(f"/api/life/upload/{raw_token}")
                assert r_page.status_code == 200

                r_upload = await client.post(
                    f"/api/life/upload/{raw_token}",
                    files=[("files", ("DSC00042.JPG", original_bytes, "image/jpeg"))],
                )
                assert r_upload.status_code == 200, r_upload.text
                body = r_upload.json()
                assert body["status"] == "applied"
                assert len(body["matches"]) == 1

                # Real SSEBus.publish (sync) must have emitted post.updated with zero TypeError
                ev = await asyncio.wait_for(queue.get(), timeout=2.0)
                assert ev.event == "post.updated"
                assert ev.post_id == post.id
                assert ev.data["id"] == post.id
                assert ev.data["images"][0]["original_name"] == "DSC00042.JPG"
                assert ev.data["images"][0]["original_url"].startswith(
                    "/api/life/media/epochs/1/originals/"
                )
            finally:
                app.state.sse_bus.unsubscribe(sub_id)

    # After lifespan exit, background tasks & reconciler/service/mailer are cleanly stopped
    assert app.state.reconciler._task is None
    assert app.state.tg_service.is_running is False
