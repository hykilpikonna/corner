from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import (
    SESSION_COOKIE_NAME,
    create_session,
    validate_reaction_emoji,
)
from app.config import Settings, get_settings
from app.db.models import NotificationQueue, User, UserIdentity
from app.db.repo import ensure_channel, set_allowed_reactions, upsert_post
from app.db.session import get_db_session
from app.main import create_app


def solve_altcha(challenge_json: dict[str, Any]) -> str:
    salt = challenge_json["salt"]
    target = challenge_json["challenge"]
    maxnumber = int(challenge_json["maxnumber"])
    for n in range(maxnumber + 1):
        if hashlib.sha256(f"{salt}{n}".encode()).hexdigest() == target:
            payload = {
                "algorithm": challenge_json["algorithm"],
                "challenge": target,
                "number": n,
                "salt": salt,
                "signature": challenge_json["signature"],
            }
            return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    raise AssertionError("Could not solve ALTCHA")


async def _setup_app_and_client(
    settings: Settings, db_session: AsyncSession
) -> tuple[Any, AsyncClient]:
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    settings.exports_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(settings)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db_session] = _override_session

    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://aza.moe",
    )
    return app, client


async def _seed_user_session(
    db_session: AsyncSession,
    *,
    display_name: str,
    email: str | None = None,
    email_verified: bool = True,
    notify_replies: bool = True,
    role: str = "user",
    provider: str = "github",
    username: str | None = None,
) -> tuple[User, str]:
    user = User(
        role=role,
        display_name=display_name,
        email=email,
        email_verified=email_verified,
        notify_replies=notify_replies,
    )
    db_session.add(user)
    await db_session.flush()
    db_session.add(
        UserIdentity(
            provider=provider,
            provider_user_id=f"{provider}-{user.id}",
            user_id=user.id,
            username=username or display_name.lower(),
        )
    )
    token = await create_session(db_session, user.id)
    await db_session.commit()
    return user, token


@pytest.mark.asyncio
async def test_interactions_reactions_toggle_and_sse_broadcast(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    ch = await ensure_channel(db_session, channel_id=1, status="active", id_offset=0)
    await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=10,
        canonical_id=10,
        date=datetime(2026, 11, 19, 8, 0, tzinfo=UTC),
        text_html="Hello world",
        # Store bare TG heart without FE0F (\u2764) to verify VS-normalized merging with "❤️" (\u2764\ufe0f)
        tg_reactions=[{"emoji": "\u2764", "count": 5}],
    )
    await db_session.commit()

    app, client = await _setup_app_and_client(test_settings, db_session)
    sub_id, queue, _ = app.state.sse_bus.subscribe(post_ids={10})

    try:
        async with client:
            # Batch interactions sets life_vid cookie and merges TG counts
            r = await client.get("/api/life/interactions", params={"ids": "10"})
            assert r.status_code == 200
            assert "life_vid" in client.cookies
            data = r.json()
            assert data["allow_anon_comments"] is True
            assert data["captcha_provider"] == "altcha"
            assert data["me"] is None
            assert data["posts"]["10"]["reactions"][0]["tg_count"] == 5
            assert data["posts"]["10"]["reactions"][0]["web_count"] == 0
            assert data["posts"]["10"]["reactions"][0]["count"] == 5
            assert data["posts"]["10"]["reactions"][0]["reacted"] is False

            # Invalid toggle (both or neither) returns 400
            assert (
                await client.post("/api/life/posts/10/reactions/toggle", json={})
            ).status_code == 400
            assert (
                await client.post(
                    "/api/life/posts/10/reactions/toggle",
                    json={"emoji": "❤️", "custom_emoji_id": "12345"},
                )
            ).status_code == 400

            # Toggle ON with "❤️" (\u2764\ufe0f) merges into TG's "\u2764" -> single reaction with count=6!
            r_on = await client.post(
                "/api/life/posts/10/reactions/toggle",
                json={"emoji": "❤️"},
            )
            assert r_on.status_code == 200
            assert len(r_on.json()["reactions"]) == 1
            heart = r_on.json()["reactions"][0]
            assert heart["tg_count"] == 5
            assert heart["web_count"] == 1
            assert heart["count"] == 6
            assert heart["reacted"] is True

            # Verify SSE broadcast omits per-user `reacted`
            sse_ev = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert sse_ev.event == "reaction.updated"
            assert sse_ev.data["post_id"] == 10
            assert sse_ev.data["reactions"][0]["count"] == 6
            assert "reacted" not in sse_ev.data["reactions"][0]

            # Toggle OFF returns count -> 5
            r_off = await client.post(
                "/api/life/posts/10/reactions/toggle",
                json={"emoji": "❤️"},
            )
            assert r_off.status_code == 200
            heart_off = r_off.json()["reactions"][0]
            assert heart_off["count"] == 5
            assert heart_off["web_count"] == 0
            assert heart_off["reacted"] is False
    finally:
        app.state.sse_bus.unsubscribe(sub_id)


@pytest.mark.asyncio
async def test_polls_regular_multi_quiz_and_closed(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    ch = await ensure_channel(db_session, channel_id=1, status="active", id_offset=0)
    await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=1,
        canonical_id=1,
        date=datetime(2026, 11, 19, 8, 0, tzinfo=UTC),
        poll={
            "id": "poll_single",
            "question": "Coffee or Tea?",
            "type": "REGULAR",
            "allow_multiple_answers": False,
            "is_closed": False,
            "options": [
                {"text": "Coffee", "tg_voter_count": 3},
                {"text": "Tea", "tg_voter_count": 2},
            ],
        },
    )
    await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=2,
        canonical_id=2,
        date=datetime(2026, 11, 19, 8, 1, tzinfo=UTC),
        poll={
            "id": "poll_multi",
            "question": "Languages?",
            "type": "REGULAR",
            "allow_multiple_answers": True,
            "is_closed": False,
            "options": [
                {"text": "Python", "tg_voter_count": 1},
                {"text": "Rust", "tg_voter_count": 1},
                {"text": "TypeScript", "tg_voter_count": 0},
            ],
        },
    )
    await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=3,
        canonical_id=3,
        date=datetime(2026, 11, 19, 8, 2, tzinfo=UTC),
        poll={
            "id": "poll_quiz",
            "question": "2+2?",
            "type": "QUIZ",
            "allow_multiple_answers": False,
            "is_closed": False,
            "correct_option_idx": 1,
            "explanation": "Basic arithmetic!",
            "options": [
                {"text": "3", "tg_voter_count": 0},
                {"text": "4", "tg_voter_count": 2},
            ],
        },
    )
    await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=4,
        canonical_id=4,
        date=datetime(2026, 11, 19, 8, 3, tzinfo=UTC),
        poll={
            "id": "poll_closed",
            "question": "Closed?",
            "type": "REGULAR",
            "allow_multiple_answers": False,
            "is_closed": True,
            "options": [{"text": "Yes", "tg_voter_count": 1}],
        },
    )
    await db_session.commit()

    app, client = await _setup_app_and_client(test_settings, db_session)
    sub_id, queue, _ = app.state.sse_bus.subscribe()

    try:
        async with client:
            # Single-choice rejects multiple options
            r_bad = await client.post(
                "/api/life/polls/poll_single/vote",
                json={"option_indices": [0, 1]},
            )
            assert r_bad.status_code == 400

            # Vote option 0
            r_v0 = await client.post(
                "/api/life/polls/poll_single/vote",
                json={"option_indices": [0]},
            )
            assert r_v0.status_code == 200
            p0 = r_v0.json()["poll"]
            assert p0["my_votes"] == [0]
            assert p0["options"][0]["voter_count"] == 4
            assert p0["total_voter_count"] == 6

            # SSE event omits my_votes
            ev = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert ev.event == "poll.updated"
            assert "my_votes" not in ev.data["poll"]

            # Retract vote with []
            r_retract = await client.post(
                "/api/life/polls/poll_single/vote",
                json={"option_indices": []},
            )
            assert r_retract.status_code == 200
            assert r_retract.json()["poll"]["my_votes"] == []
            assert r_retract.json()["poll"]["options"][0]["voter_count"] == 3
            await asyncio.wait_for(queue.get(), timeout=1.0)

            # Multi-choice vote
            r_multi = await client.post(
                "/api/life/polls/poll_multi/vote",
                json={"option_indices": [0, 2]},
            )
            assert r_multi.status_code == 200
            assert r_multi.json()["poll"]["my_votes"] == [0, 2]
            assert r_multi.json()["poll"]["total_voter_count"] == 3
            await asyncio.wait_for(queue.get(), timeout=1.0)

            # QUIZ: before vote, answer is hidden in /interactions
            inter = (await client.get("/api/life/interactions", params={"ids": "3"})).json()
            assert inter["posts"]["3"]["poll"]["correct_option_idx"] is None
            assert inter["posts"]["3"]["poll"]["explanation"] is None

            # QUIZ: voting reveals correct_option_idx & explanation to voter
            r_quiz = await client.post(
                "/api/life/polls/poll_quiz/vote",
                json={"option_indices": [0]},
            )
            assert r_quiz.status_code == 200
            quiz_data = r_quiz.json()["poll"]
            assert quiz_data["my_votes"] == [0]
            assert quiz_data["correct_option_idx"] == 1
            assert quiz_data["explanation"] == "Basic arithmetic!"

            # Public SSE for open QUIZ still does NOT leak correct answer!
            quiz_ev = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert quiz_ev.data["poll"]["correct_option_idx"] is None
            assert quiz_ev.data["poll"]["explanation"] is None

            # QUIZ: second vote / change is locked (409)
            r_quiz_lock = await client.post(
                "/api/life/polls/poll_quiz/vote",
                json={"option_indices": [1]},
            )
            assert r_quiz_lock.status_code == 409

            # Closed poll returns 409
            r_closed = await client.post(
                "/api/life/polls/poll_closed/vote",
                json={"option_indices": [0]},
            )
            assert r_closed.status_code == 409
    finally:
        app.state.sse_bus.unsubscribe(sub_id)


@pytest.mark.asyncio
async def test_comments_verified_anon_xss_tombstones_bridge_and_notifications(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    cfg = test_settings.model_copy(
        update={
            "ALTCHA_MAX_NUMBER": 100,
            "EMAIL_DEBOUNCE_SECONDS": 120,
        }
    )
    ch = await ensure_channel(db_session, channel_id=1, status="active", id_offset=0)
    await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=100,
        canonical_id=100,
        date=datetime(2026, 11, 19, 9, 0, tzinfo=UTC),
        text_html="Post 100",
    )
    await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=101,
        canonical_id=101,
        date=datetime(2026, 11, 19, 9, 1, tzinfo=UTC),
        text_html="Post 101",
    )
    alice, alice_token = await _seed_user_session(
        db_session,
        display_name="Alice",
        email="alice@example.com",
        email_verified=True,
        notify_replies=True,
    )
    _, bob_token = await _seed_user_session(
        db_session,
        display_name="Bob",
        email="bob@example.com",
    )
    _, owner_token = await _seed_user_session(
        db_session,
        display_name="Owner",
        role="owner",
    )

    app, client = await _setup_app_and_client(cfg, db_session)
    bridged_ids: list[int] = []

    async def _fake_tg_bridge(comment_id: int) -> None:
        bridged_ids.append(comment_id)

    app.state.tg_bridge = _fake_tg_bridge
    sub_id, queue, _ = app.state.sse_bus.subscribe(post_ids={100})

    try:
        async with client:
            # 1. Verified user (Alice) comments without CAPTCHA, with XSS payload
            client.cookies.set(SESSION_COOKIE_NAME, alice_token)
            r_c1 = await client.post(
                "/api/life/posts/100/comments",
                json={
                    "body": "<script>alert(1)</script><img src=x onerror=alert(1)>"
                    "Hello <b>world</b> <a href=\"javascript:alert(1)\">click</a>"
                },
            )
            assert r_c1.status_code == 201
            c1 = r_c1.json()
            assert c1["author"]["display_name"] == "Alice"
            assert c1["author"]["verified"] is True
            assert c1["is_mine"] is True
            assert c1["can_delete"] is True
            assert "<script" not in c1["body_html"].lower()
            assert "onerror" not in c1["body_html"].lower()
            assert "javascript:" not in c1["body_html"].lower()
            assert "<b>world</b>" in c1["body_html"]

            # SSE comment.created payload is raw CommentDTO directly (no wrapper!)
            ev_c1 = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert ev_c1.event == "comment.created"
            assert "comment" not in ev_c1.data
            assert ev_c1.data["post_id"] == 100
            assert ev_c1.data["id"] == c1["id"]
            assert ev_c1.data["is_mine"] is False

            # Let background bridge task run
            await asyncio.sleep(0.05)
            assert c1["id"] in bridged_ids

            # 2. Anonymous user posts reply to Alice (requires ALTCHA)
            client.cookies.clear()
            r_no_captcha = await client.post(
                "/api/life/posts/100/comments",
                json={"body": "Missing captcha", "parent_id": c1["id"]},
            )
            assert r_no_captcha.status_code == 400

            ch1 = (await client.get("/api/life/captcha/challenge")).json()
            r_anon1 = await client.post(
                "/api/life/posts/100/comments",
                json={
                    "body": "Nice post!",
                    "parent_id": c1["id"],
                    "altcha_payload": solve_altcha(ch1),
                },
            )
            assert r_anon1.status_code == 201
            c2 = r_anon1.json()
            assert c2["author"]["verified"] is False
            assert c2["author"]["display_name"].startswith("Anonymous #")
            assert len(c2["author"]["display_name"]) == len("Anonymous #") + 4
            anon_tag_post_100 = c2["author"]["display_name"]

            # Same anon visitor on same post gets deterministic same tripcode
            assert c2["is_mine"] is True
            assert c2["can_delete"] is False

            # Reloading GET /posts/100/comments with the same `life_vid` cookie preserves is_mine=True
            # for the anon visitor's own comment (and False for Alice's), with can_delete=False!
            r_anon_reload = await client.get("/api/life/posts/100/comments")
            assert r_anon_reload.status_code == 200
            reloaded_by_id = {c["id"]: c for c in r_anon_reload.json()["comments"]}
            assert reloaded_by_id[c2["id"]]["is_mine"] is True
            assert reloaded_by_id[c2["id"]]["can_delete"] is False
            assert reloaded_by_id[c1["id"]]["is_mine"] is False

            # Another anonymous visitor without `life_vid` cookie sees is_mine=False and no cookie minted
            saved_vid = client.cookies.get("life_vid")
            client.cookies.clear()
            r_fresh_anon = await client.get("/api/life/posts/100/comments")
            assert r_fresh_anon.status_code == 200
            assert "life_vid" not in r_fresh_anon.headers.get("set-cookie", "")
            fresh_by_id = {c["id"]: c for c in r_fresh_anon.json()["comments"]}
            assert fresh_by_id[c2["id"]]["is_mine"] is False
            if saved_vid:
                client.cookies.set("life_vid", saved_vid)

            ch2 = (await client.get("/api/life/captcha/challenge")).json()
            r_anon2 = await client.post(
                "/api/life/posts/100/comments",
                json={"body": "Second reply", "altcha_payload": solve_altcha(ch2)},
            )
            assert r_anon2.status_code == 201
            assert r_anon2.json()["author"]["display_name"] == anon_tag_post_100

            # Same anon visitor on DIFFERENT post gets a different tripcode (per-thread!)
            ch3 = (await client.get("/api/life/captcha/challenge")).json()
            r_anon_other = await client.post(
                "/api/life/posts/101/comments",
                json={"body": "Other thread", "altcha_payload": solve_altcha(ch3)},
            )
            assert r_anon_other.status_code == 201
            assert r_anon_other.json()["author"]["display_name"].startswith("Anonymous #")

            # Verify debounced notification was queued for Alice (reply c2 -> c1)
            notif_rows = (
                await db_session.execute(
                    select(NotificationQueue).where(
                        NotificationQueue.recipient_user_id == alice.id
                    )
                )
            ).scalars().all()
            assert len(notif_rows) == 1
            assert notif_rows[0].comment_id == c2["id"]
            assert notif_rows[0].post_id == 100

            # 3. Bob cannot delete Alice's comment (403)
            client.cookies.set(SESSION_COOKIE_NAME, bob_token)
            assert (await client.delete(f"/api/life/comments/{c1['id']}")).status_code == 403

            # 4. Alice deletes her own parent comment (which has live child c2 -> tombstone!)
            client.cookies.set(SESSION_COOKIE_NAME, alice_token)
            r_del_c1 = await client.delete(f"/api/life/comments/{c1['id']}")
            assert r_del_c1.status_code == 200

            # Owner deletes leaf anon comment r_anon2 -> omitted completely from GET
            leaf_id = r_anon2.json()["id"]
            client.cookies.set(SESSION_COOKIE_NAME, owner_token)
            r_del_leaf = await client.delete(f"/api/life/comments/{leaf_id}")
            assert r_del_leaf.status_code == 200

            # Replying to a soft-deleted comment must be rejected (404)
            r_reply_deleted = await client.post(
                "/api/life/posts/100/comments",
                json={"body": "Replying to ghost", "parent_id": leaf_id},
            )
            assert r_reply_deleted.status_code == 404

            # Control chars rejected in comment body (400), newlines/tabs allowed
            r_ctrl = await client.post(
                "/api/life/posts/100/comments",
                json={"body": "bad\x00null"},
            )
            assert r_ctrl.status_code == 400

            # Owner posting as_anonymous=True gets is_mine=True AND retains can_delete=True
            ch_owner = (await client.get("/api/life/captcha/challenge")).json()
            r_owner_anon = await client.post(
                "/api/life/posts/100/comments",
                json={
                    "body": "Owner incognito",
                    "as_anonymous": True,
                    "altcha_payload": solve_altcha(ch_owner),
                },
            )
            assert r_owner_anon.status_code == 201
            owner_anon_id = r_owner_anon.json()["id"]
            assert r_owner_anon.json()["is_mine"] is True
            assert r_owner_anon.json()["can_delete"] is True

            # Regular logged-in user (Bob) posting as_anonymous=True gets is_mine=True, can_delete=False
            client.cookies.set(SESSION_COOKIE_NAME, bob_token)
            ch_bob = (await client.get("/api/life/captcha/challenge")).json()
            r_bob_anon = await client.post(
                "/api/life/posts/100/comments",
                json={
                    "body": "Bob incognito",
                    "as_anonymous": True,
                    "altcha_payload": solve_altcha(ch_bob),
                },
            )
            assert r_bob_anon.status_code == 201
            bob_anon_id = r_bob_anon.json()["id"]
            assert r_bob_anon.json()["is_mine"] is True
            assert r_bob_anon.json()["can_delete"] is False

            # Reload as Bob -> Bob's own anon comment has is_mine=True, owner's has is_mine=False
            bob_reload = {
                c["id"]: c
                for c in (await client.get("/api/life/posts/100/comments")).json()["comments"]
            }
            assert bob_reload[bob_anon_id]["is_mine"] is True
            assert bob_reload[bob_anon_id]["can_delete"] is False
            assert bob_reload[owner_anon_id]["is_mine"] is False

            # Reload as Owner -> Owner's anon comment has is_mine=True and can_delete=True
            client.cookies.set(SESSION_COOKIE_NAME, owner_token)
            owner_reload = {
                c["id"]: c
                for c in (await client.get("/api/life/posts/100/comments")).json()["comments"]
            }
            assert owner_reload[owner_anon_id]["is_mine"] is True
            assert owner_reload[owner_anon_id]["can_delete"] is True
            assert owner_reload[bob_anon_id]["is_mine"] is False

            # Fetch thread: c1 is tombstone, c2 is visible, leaf_id is omitted!
            thread = (await client.get("/api/life/posts/100/comments")).json()["comments"]
            ids_returned = [c["id"] for c in thread]
            assert c1["id"] in ids_returned
            assert c2["id"] in ids_returned
            assert leaf_id not in ids_returned

            tomb = next(c for c in thread if c["id"] == c1["id"])
            assert tomb["deleted"] is True
            assert tomb["body_text"] == "[deleted]"
            assert tomb["body_html"] == "<p>[deleted]</p>"
    finally:
        app.state.sse_bus.unsubscribe(sub_id)


@pytest.mark.asyncio
async def test_anon_comments_disabled_returns_403(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    cfg = test_settings.model_copy(update={"ALLOW_ANON_COMMENTS": False})
    ch = await ensure_channel(db_session, channel_id=1, status="active", id_offset=0)
    await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=200,
        canonical_id=200,
        date=datetime(2026, 11, 19, 9, 0, tzinfo=UTC),
        text_html="Post 200",
    )
    await db_session.commit()

    _, client = await _setup_app_and_client(cfg, db_session)
    async with client:
        r = await client.post(
            "/api/life/posts/200/comments",
            json={"body": "Trying anon"},
        )
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_reaction_validation_cap_and_concurrent_races(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    from app.db.session import create_engine_and_factory

    ch = await ensure_channel(db_session, channel_id=1, status="active", id_offset=0)
    await upsert_post(
        db_session,
        channel=ch,
        tg_msg_id=300,
        canonical_id=300,
        date=datetime(2026, 11, 19, 9, 0, tzinfo=UTC),
        text_html="Post 300",
        poll={
            "id": "poll_race_quiz",
            "question": "Race quiz?",
            "type": "QUIZ",
            "allow_multiple_answers": False,
            "is_closed": False,
            "correct_option_idx": 0,
            "explanation": "Zero!",
            "options": [
                {"text": "A", "tg_voter_count": 0},
                {"text": "B", "tg_voter_count": 0},
            ],
        },
    )
    _, alice_token = await _seed_user_session(
        db_session,
        display_name="Racer",
        email="racer@example.com",
    )
    await db_session.commit()

    # Use a real per-request session factory so concurrent HTTP requests use real separate Postgres transactions!
    engine, factory = create_engine_and_factory(test_settings)
    test_settings.media_dir.mkdir(parents=True, exist_ok=True)
    test_settings.exports_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(test_settings)

    async def _per_req_session() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s

    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_db_session] = _per_req_session

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://aza.moe",
        ) as client:
            client.cookies.set(SESSION_COOKIE_NAME, alice_token)

            # 1. Spam / ASCII / control-char / non-numeric custom_emoji_id rejected
            for bad_emoji in ("abc", "123", "hello world", "a❤️", "❤️ ❤️", "🔥" * 20, "!!!"):
                r_bad = await client.post(
                    "/api/life/posts/300/reactions/toggle",
                    json={"emoji": bad_emoji},
                )
                assert r_bad.status_code == 400, f"Should reject emoji {bad_emoji!r}"

            r_bad_custom = await client.post(
                "/api/life/posts/300/reactions/toggle",
                json={"custom_emoji_id": "not-digits"},
            )
            assert r_bad_custom.status_code == 400

            # Structural validator accepts skin-tone & keycap graphemes, while global allowlist rejects unlisted emojis
            assert validate_reaction_emoji("👍🏻") == "👍🏻"
            assert validate_reaction_emoji("1️⃣") == "1️⃣"
            for unlisted in ("👍🏻", "1️⃣", "🍆"):
                r_unlisted = await client.post(
                    "/api/life/posts/300/reactions/toggle",
                    json={"emoji": unlisted},
                )
                assert r_unlisted.status_code == 400, f"Unlisted emoji {unlisted!r} should be rejected"

            # 2. Allowlisted emojis up to cap of 5 succeed; 6th returns 400; toggling off succeeds
            for valid_emoji in ("❤️", "🔥", "👍", "👏", "✨"):
                r_ok = await client.post(
                    "/api/life/posts/300/reactions/toggle",
                    json={"emoji": valid_emoji},
                )
                assert r_ok.status_code == 200, f"Should accept emoji {valid_emoji!r}"

            # 2b. Even if owner later removes an emoji from allowlist, actor can still toggle their existing reaction OFF
            await set_allowed_reactions(db_session, [{"emoji": "❤️"}, {"emoji": "🔥"}])
            await db_session.commit()
            r_remove_unlisted = await client.post(
                "/api/life/posts/300/reactions/toggle",
                json={"emoji": "👍"},
            )
            assert r_remove_unlisted.status_code == 200
            # Re-add after removal fails against narrowed allowlist
            r_readd_unlisted = await client.post(
                "/api/life/posts/300/reactions/toggle",
                json={"emoji": "👍"},
            )
            assert r_readd_unlisted.status_code == 400

            # Restore default list for remaining cap + race assertions
            from app.db.repo import default_allowed_reactions

            await set_allowed_reactions(db_session, default_allowed_reactions())
            await db_session.commit()
            r_readd_thumb = await client.post(
                "/api/life/posts/300/reactions/toggle",
                json={"emoji": "👍"},
            )
            assert r_readd_thumb.status_code == 200

            r_sixth = await client.post(
                "/api/life/posts/300/reactions/toggle",
                json={"emoji": "🎉"},
            )
            assert r_sixth.status_code == 400

            # Toggling an existing reaction OFF is allowed even at cap
            r_remove = await client.post(
                "/api/life/posts/300/reactions/toggle",
                json={"emoji": "✨"},
            )
            assert r_remove.status_code == 200

            # 3. Concurrent rapid double-clicks on reaction toggle -> zero 500 UniqueViolation!
            toggle_results = await asyncio.gather(
                *[
                    client.post(
                        "/api/life/posts/300/reactions/toggle",
                        json={"emoji": "🎉"},
                    )
                    for _ in range(4)
                ]
            )
            assert all(r.status_code == 200 for r in toggle_results)

            # 4. Concurrent QUIZ poll votes from same actor -> exactly ONE 200, rest 409!
            quiz_results = await asyncio.gather(
                client.post("/api/life/polls/poll_race_quiz/vote", json={"option_indices": [0]}),
                client.post("/api/life/polls/poll_race_quiz/vote", json={"option_indices": [1]}),
                client.post("/api/life/polls/poll_race_quiz/vote", json={"option_indices": [0]}),
            )
            statuses = sorted(r.status_code for r in quiz_results)
            assert statuses == [200, 409, 409]
    finally:
        await engine.dispose()


def test_telegram_channel_reaction_emojis_validation() -> None:
    from fastapi import HTTPException

    from app.auth.security import validate_reaction_emoji

    standard_tg_reactions = [
        "👍", "👎", "❤️", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬", "😢", "🎉", "🤩",
        "🤮", "💩", "🙏", "👌", "🕊", "🕊️", "🤡", "🥱", "🥴", "😍", "🐳", "❤️‍🔥", "🌚", "🌭",
        "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈", "😴",
        "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨", "🤝", "✍️", "🤗", "🫡", "🎅",
        "🎄", "☃️", "💅", "🤪", "🗿", "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾",
        "🤷‍♂️", "🤷", "🤷‍♀️", "😡", "😮", "✨", "😂", "1️⃣", "👍🏻", "🇺🇸", "‼️", "⁉️", "ℹ️",
    ]
    for emoji in standard_tg_reactions:
        assert validate_reaction_emoji(emoji) == emoji

    for invalid in (
        "",
        " ",
        "abc",
        "123",
        "hello",
        "a❤️",
        "❤️ ❤️",
        "!\x00",
        "🔥" * 20,
        "💩" * 16,
        "❤️❤️",
        "\u20e3",
        "12345\u20e3",
        "🇺",
        "🇺🇸🇺",
        "🏻",
        "\ufe0f",
        "\u200d",
        "❤️\u200d",
        "\u200d❤️",
        "❤️\u200d\u200d🔥",
        "👍🏻🏻",
        "!!!",
    ):
        with pytest.raises(HTTPException) as exc_info:
            validate_reaction_emoji(invalid)
        assert exc_info.value.status_code == 400, f"Should reject {invalid!r}"
