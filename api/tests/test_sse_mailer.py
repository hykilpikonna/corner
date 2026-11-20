from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import SESSION_COOKIE_NAME, create_session, verify_unsubscribe_token
from app.config import Settings, get_settings
from app.db.models import Comment, NotificationQueue, User
from app.db.repo import ensure_channel, upsert_post
from app.db.session import get_db_session
from app.mailer import Mailer
from app.main import create_app
from app.sse import SSEBus


@pytest.mark.asyncio
async def test_sse_bus_replay_filter_viewer_tracking_and_slow_consumer() -> None:
    bus = SSEBus(max_history=50, subscriber_queue_size=2)

    ev1 = bus.publish("reaction.updated", {"post_id": 1, "reactions": []}, post_id=1)
    ev2 = bus.publish("reaction.updated", {"post_id": 2, "reactions": []}, post_id=2)
    ev3 = bus.publish("comment.created", {"id": 9, "post_id": 1}, post_id=1)
    ev4 = bus.publish("post.created", {"id": 999, "text": "Brand new post"}, post_id=999)

    assert [ev1.id, ev2.id, ev3.id, ev4.id] == [1, 2, 3, 4]

    # Subscribe with Last-Event-ID=1 and filter posts={1}:
    # MUST replay comment.created for post 1 AND post.created for brand-new post 999!
    sub_id, queue, replayed = bus.subscribe(
        post_ids={1},
        user_id=42,
        last_event_id=1,
    )
    assert [e.id for e in replayed] == [3, 4]
    assert bus.is_user_viewing(42, 1) is True
    assert bus.is_user_viewing(42, 2) is False
    assert bus.is_user_viewing(99, 1) is False

    # Live post.created for post 1000 is ALSO delivered despite post_ids={1} filter!
    bus.publish("post.created", {"id": 1000, "text": "Live new post"}, post_id=1000)
    assert queue.get_nowait().data["id"] == 1000

    # Overflow subscriber queue (maxsize=2) -> drops oldest safely without raising
    bus.publish("post.updated", {"post_id": 1, "n": 1}, post_id=1)
    bus.publish("post.updated", {"post_id": 1, "n": 2}, post_id=1)
    bus.publish("post.updated", {"post_id": 1, "n": 3}, post_id=1)
    # Filtered out event for post 2 should not affect queue
    bus.publish("post.updated", {"post_id": 2, "n": 99}, post_id=2)

    items = [queue.get_nowait().data["n"], queue.get_nowait().data["n"]]
    assert items == [2, 3]

    bus.unsubscribe(sub_id)
    assert bus.is_user_viewing(42, 1) is False
    assert bus.subscriber_count == 0


async def _collect_sse_asgi(
    app: Any,
    *,
    query_string: str,
    headers: list[tuple[bytes, bytes]] | None = None,
    stop_when: Any,
    on_connected: Any | None = None,
) -> tuple[int, dict[str, str], str]:
    disconnect_event = asyncio.Event()
    received_chunks: list[str] = []
    status_code = 0
    resp_headers: dict[str, str] = {}
    connected_triggered = False

    async def receive() -> dict[str, Any]:
        await disconnect_event.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code, connected_triggered
        if message["type"] == "http.response.start":
            status_code = int(message["status"])
            for k, v in message.get("headers", []):
                resp_headers[k.decode("latin1").lower()] = v.decode("latin1")
            if on_connected is not None and not connected_triggered:
                connected_triggered = True
                on_connected()
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                received_chunks.append(body.decode("utf-8"))
                if stop_when("".join(received_chunks)):
                    disconnect_event.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/api/life/stream",
        "raw_path": b"/api/life/stream",
        "query_string": query_string.encode("ascii"),
        "headers": headers or [],
        "client": ("127.0.0.1", 12345),
        "server": ("aza.moe", 443),
    }

    await asyncio.wait_for(app(scope, receive, send), timeout=3.0)
    return status_code, resp_headers, "".join(received_chunks)


@pytest.mark.asyncio
async def test_sse_http_stream_replay_header_query_live_and_ping(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    test_settings.media_dir.mkdir(parents=True, exist_ok=True)
    test_settings.exports_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(test_settings)
    app.state.sse_ping_interval = 0.05

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_db_session] = _override_session

    # Seed authenticated user so we verify SSE viewer tracking too
    viewer = User(role="user", display_name="Viewer", email="v@example.com", email_verified=True)
    db_session.add(viewer)
    await db_session.flush()
    viewer_token = await create_session(db_session, viewer.id)
    await db_session.commit()

    # Pre-publish events for replay tests
    app.state.sse_bus.publish("reaction.updated", {"post_id": 10, "reactions": []}, post_id=10)
    app.state.sse_bus.publish("reaction.updated", {"post_id": 20, "reactions": []}, post_id=20)
    app.state.sse_bus.publish("comment.deleted", {"post_id": 10, "comment_id": 5}, post_id=10)
    app.state.sse_bus.publish("post.created", {"id": 999, "text": "New post"}, post_id=999)

    saw_viewer_live = False

    def _publish_live_and_check_viewer() -> None:
        async def _delayed() -> None:
            nonlocal saw_viewer_live
            # Wait briefly so event_generator() enters try: and calls bus.subscribe()
            for _ in range(20):
                if app.state.sse_bus.is_user_viewing(viewer.id, 10):
                    saw_viewer_live = True
                    break
                await asyncio.sleep(0.005)
            app.state.sse_bus.publish(
                "comment.created",
                {"id": 99, "post_id": 10, "body_text": "hi"},
                post_id=10,
            )

        asyncio.create_task(_delayed())

    # 1. Replay via Last-Event-ID header + post filter + post.created passthrough + live + ping
    status, headers, streamed = await _collect_sse_asgi(
        app,
        query_string="posts=10",
        headers=[
            (b"last-event-id", b"1"),
            (b"cookie", f"{SESSION_COOKIE_NAME}={viewer_token}".encode()),
        ],
        stop_when=lambda s: "event: comment.created" in s and ": ping\n\n" in s,
        on_connected=_publish_live_and_check_viewer,
    )
    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert headers.get("x-accel-buffering") == "no"
    assert saw_viewer_live is True
    assert "id: 3\nevent: comment.deleted" in streamed
    assert "id: 4\nevent: post.created" in streamed
    assert "id: 5\nevent: comment.created" in streamed
    assert '"post_id":20' not in streamed
    assert ": ping\n\n" in streamed
    assert app.state.sse_bus.subscriber_count == 0
    assert app.state.sse_bus.is_user_viewing(viewer.id, 10) is False
    assert app.state.sse_ip_counts == {}

    # 2. Replay via ?last_event_id= query param (browser EventSource reconnect fallback)
    status2, _, streamed2 = await _collect_sse_asgi(
        app,
        query_string="posts=10&last_event_id=0",
        stop_when=lambda s: "id: 5\nevent: comment.created" in s,
    )
    assert status2 == 200
    assert "id: 1\nevent: reaction.updated" in streamed2
    assert "id: 3\nevent: comment.deleted" in streamed2
    assert "id: 4\nevent: post.created" in streamed2
    assert "id: 5\nevent: comment.created" in streamed2
    assert app.state.sse_bus.subscriber_count == 0

    # 3. Per-IP connection cap returns 429
    app.state.sse_max_per_ip = 1
    app.state.sse_ip_counts["127.0.0.1"] = 1
    status3, _, _ = await _collect_sse_asgi(
        app,
        query_string="posts=10",
        stop_when=lambda _s: True,
    )
    assert status3 == 429
    app.state.sse_ip_counts.clear()


@pytest.mark.asyncio
async def test_mailer_debounce_coalesce_sse_skip_and_retry(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    ch = await ensure_channel(db_session, channel_id=1, status="active", id_offset=0)
    await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=50,
        canonical_id=50,
        date=datetime(2026, 11, 19, 8, 0, tzinfo=UTC),
        text_html="Post 50",
    )
    await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=51,
        canonical_id=51,
        date=datetime(2026, 11, 19, 8, 5, tzinfo=UTC),
        text_html="Post 51",
    )

    alice = User(
        role="user",
        display_name="Alice",
        email="alice@example.com",
        email_verified=True,
        notify_replies=True,
    )
    bob = User(
        role="user",
        display_name="Bob",
        email="bob@example.com",
        email_verified=True,
        notify_replies=True,
    )
    db_session.add_all([alice, bob])
    await db_session.flush()

    now = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
    c1 = Comment(
        post_id=50,
        source="web",
        channel_id=1,
        user_id=bob.id,
        body_text="First reply!",
        body_html="<p>First reply!</p>",
        created_at=now - timedelta(minutes=3),
    )
    c2 = Comment(
        post_id=50,
        source="web",
        channel_id=1,
        anon_tag="Anonymous #abcd",
        body_text="Second reply!",
        body_html="<p>Second reply!</p>",
        created_at=now - timedelta(minutes=1),
    )
    c3 = Comment(
        post_id=51,
        source="web",
        channel_id=1,
        user_id=bob.id,
        body_text="Live thread reply!",
        body_html="<p>Live thread reply!</p>",
        created_at=now - timedelta(minutes=3),
    )
    db_session.add_all([c1, c2, c3])
    await db_session.flush()

    # Enqueue c1 (due now) + c2 (still in debounce window for same post 50 -> coalesces!)
    # + c3 (due now on post 51 where Alice is live on SSE -> skipped!)
    q1 = NotificationQueue(
        recipient_user_id=alice.id,
        comment_id=c1.id,
        post_id=50,
        send_after=now - timedelta(seconds=10),
    )
    q2 = NotificationQueue(
        recipient_user_id=alice.id,
        comment_id=c2.id,
        post_id=50,
        send_after=now + timedelta(seconds=30),
    )
    q3 = NotificationQueue(
        recipient_user_id=alice.id,
        comment_id=c3.id,
        post_id=51,
        send_after=now - timedelta(seconds=10),
    )
    db_session.add_all([q1, q2, q3])
    await db_session.commit()

    bus = SSEBus()
    # Alice is currently viewing post 51 via SSE!
    sub_id, _, _ = bus.subscribe(post_ids={51}, user_id=alice.id)

    sent_messages: list[EmailMessage] = []

    async def _capture_smtp(msg: EmailMessage) -> None:
        sent_messages.append(msg)

    mailer = Mailer(test_settings, bus, smtp_sender=_capture_smtp)
    try:
        # Before send_after for ALL items -> nothing sent
        count_early = await mailer.process_once(
            db_session, now=now - timedelta(minutes=5)
        )
        assert count_early == 0
        assert len(sent_messages) == 0

        # At `now`: post 50 sends ONE coalesced email with both c1 & c2; post 51 is skipped!
        count_sent = await mailer.process_once(db_session, now=now)
        assert count_sent == 1
        assert len(sent_messages) == 1

        msg = sent_messages[0]
        assert msg["To"] == "alice@example.com"
        assert "2 new replies" in msg["Subject"]
        list_unsub = msg["List-Unsubscribe"]
        assert list_unsub.startswith("<https://aza.moe/api/life/auth/unsubscribe?token=")
        token = list_unsub.split("token=", 1)[1].rstrip(">")
        assert verify_unsubscribe_token(token, test_settings.SECRET_KEY) == alice.id

        plain = msg.get_body(preferencelist=("plain",)).get_content()
        assert "Bob: First reply!" in plain
        assert "Anon #abcd: Second reply!" in plain
        assert "https://aza.moe/life?shared=50" in plain

        # All 3 queue rows marked sent_at
        rows = (await db_session.execute(select(NotificationQueue))).scalars().all()
        assert all(r.sent_at is not None for r in rows)
    finally:
        bus.unsubscribe(sub_id)

    # Verify SMTP error backoff behavior
    c4 = Comment(
        post_id=50,
        source="web",
        channel_id=1,
        user_id=bob.id,
        body_text="Retry test",
        body_html="<p>Retry test</p>",
        created_at=now,
    )
    db_session.add(c4)
    await db_session.flush()
    q4 = NotificationQueue(
        recipient_user_id=alice.id,
        comment_id=c4.id,
        post_id=50,
        send_after=now - timedelta(seconds=5),
    )
    db_session.add(q4)
    await db_session.commit()

    async def _failing_smtp(_msg: EmailMessage) -> None:
        raise RuntimeError("SMTP connection refused")

    failing_mailer = Mailer(test_settings, bus, smtp_sender=_failing_smtp)
    failed_count = await failing_mailer.process_once(db_session, now=now)
    assert failed_count == 0
    await db_session.refresh(q4)
    assert q4.sent_at is None
    assert q4.attempts == 1
    assert "SMTP connection refused" in (q4.last_error or "")
    assert q4.send_after > now

    # Graceful no-op when SMTP_HOST is unconfigured
    noop_cfg = test_settings.model_copy(update={"SMTP_HOST": None})
    noop_mailer = Mailer(noop_cfg, bus)
    await noop_mailer.send_magic_link_email("dev@example.com", "https://aza.moe/verify?t=1")


@pytest.mark.asyncio
async def test_mailer_releases_db_locks_before_smtp_and_supports_port_465(
    test_settings: Settings,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db.session import create_engine_and_factory

    ch = await ensure_channel(db_session, channel_id=1, status="active", id_offset=0)
    await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=60,
        canonical_id=60,
        date=datetime(2026, 11, 19, 8, 0, tzinfo=UTC),
        text_html="Post 60",
    )
    alice = User(
        role="user",
        display_name="Alice",
        email="alice60@example.com",
        email_verified=True,
        notify_replies=True,
    )
    db_session.add(alice)
    await db_session.flush()
    now = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
    c1 = Comment(
        post_id=60,
        source="web",
        channel_id=1,
        anon_tag="Anonymous #1234",
        body_text="Lock check",
        body_html="<p>Lock check</p>",
        created_at=now - timedelta(minutes=2),
    )
    db_session.add(c1)
    await db_session.flush()
    q = NotificationQueue(
        recipient_user_id=alice.id,
        comment_id=c1.id,
        post_id=60,
        send_after=now - timedelta(seconds=5),
    )
    db_session.add(q)
    await db_session.commit()

    engine, factory = create_engine_and_factory(test_settings)
    saw_unlocked_leased_row = False

    async def _smtp_checking_lock(_msg: EmailMessage) -> None:
        nonlocal saw_unlocked_leased_row
        # Open an independent Postgres session while SMTP I/O is in flight!
        # Row MUST NOT be locked (FOR UPDATE NOWAIT succeeds) and send_after MUST be leased > now!
        async with factory() as other_db:
            row = (
                await other_db.execute(
                    select(NotificationQueue)
                    .where(NotificationQueue.id == q.id)
                    .with_for_update(nowait=True)
                )
            ).scalar_one()
            saw_unlocked_leased_row = row.sent_at is None and row.send_after > now

    try:
        mailer = Mailer(
            test_settings,
            SSEBus(),
            session_factory=factory,
            smtp_sender=_smtp_checking_lock,
        )
        sent = await mailer.process_once(now=now)
        assert sent == 1
        assert saw_unlocked_leased_row is True
    finally:
        await engine.dispose()

    # Verify port 465 uses implicit TLS (use_tls=True, start_tls=False)
    captured_kwargs: dict[str, Any] = {}

    async def _fake_aiosmtplib_send(msg: EmailMessage, **kwargs: Any) -> None:
        captured_kwargs.update(kwargs)

    monkeypatch.setattr("app.mailer.aiosmtplib.send", _fake_aiosmtplib_send)
    cfg_465 = test_settings.model_copy(
        update={"SMTP_HOST": "smtp.example.com", "SMTP_PORT": 465, "SMTP_USE_TLS": True}
    )
    m465 = Mailer(cfg_465, SSEBus())
    await m465.send_magic_link_email("u@example.com", "https://aza.moe/v")
    assert captured_kwargs["use_tls"] is True
    assert captured_kwargs["start_tls"] is False
