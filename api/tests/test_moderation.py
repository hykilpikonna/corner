from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import SESSION_COOKIE_NAME, create_session
from app.config import Settings
from app.db.models import User, UserIdentity
from app.db.repo import ensure_channel, upsert_post
from app.db.session import create_engine_and_factory, get_db_session
from app.main import create_app
from app.tg.bridge import TelegramBridge


class _RecordingTgClient:
    def __init__(self) -> None:
        self.next_id = 500
        self.sent_messages: list[dict[str, Any]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.next_id += 1
        self.sent_messages.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(id=self.next_id)


async def _solve_altcha(client: AsyncClient) -> str:
    challenge = (await client.get("/api/life/captcha/challenge")).json()
    salt = str(challenge["salt"])
    target = str(challenge["challenge"])
    max_num = int(challenge["maxnumber"])
    for n in range(max_num + 1):
        if hashlib.sha256(f"{salt}{n}".encode()).hexdigest() == target:
            raw = json.dumps(
                {
                    "algorithm": challenge["algorithm"],
                    "challenge": target,
                    "number": n,
                    "salt": salt,
                    "signature": challenge["signature"],
                }
            )
            return base64.b64encode(raw.encode()).decode()
    raise AssertionError("Failed solving challenge")


@pytest.mark.asyncio
async def test_moderation_hold_ping_approve_whitelist_and_reject(
    test_settings: Settings,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = test_settings.model_copy(
        update={
            "OWNER_TG_ID": 777001,
            "OPENAI_API_KEY": "sk-test-fake",
            "ALLOW_ANON_COMMENTS": True,
            "TG_CHANNEL_ID": -100111222,
            "TG_DISCUSSION_CHAT_ID": -100111333,
        }
    )
    engine, factory = create_engine_and_factory(cfg)

    owner = User(role="owner", display_name="Owner")
    regular = User(role="user", display_name="Regular")
    db_session.add_all([owner, regular])
    await db_session.flush()
    db_session.add(
        UserIdentity(
            provider="telegram",
            provider_user_id=str(cfg.OWNER_TG_ID),
            user_id=owner.id,
            username="owner",
        )
    )
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
        date=datetime(2026, 11, 21, 2, 0, tzinfo=UTC),
        text_html="Hello",
    )
    post.tg_discussion_root_id = 900
    await db_session.commit()

    # Fake OpenAI moderation response based on substring
    openai_calls: list[str] = []

    async def _fake_call_openai(**kwargs: Any) -> dict[str, Any]:
        text = kwargs.get("text") or ""
        openai_calls.append(text)
        if "toxic" in text.lower():
            return {
                "flagged": True,
                "categories": {"harassment": True},
                "category_scores": {"harassment": 0.91},
            }
        return {
            "flagged": False,
            "categories": {},
            "category_scores": {"harassment": 0.01},
        }

    monkeypatch.setattr("app.moderation._call_openai_moderation", _fake_call_openai)

    tg_client = _RecordingTgClient()
    bridge = TelegramBridge(session_factory=factory, settings=cfg, client=tg_client)
    app = create_app(cfg)
    app.state.db_engine = engine
    app.state.db_session_factory = factory
    app.state.tg_bridge = bridge

    async def _override_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = _override_session

    owner_client = AsyncClient(transport=ASGITransport(app=app), base_url="https://test")
    anon_client = AsyncClient(transport=ASGITransport(app=app), base_url="https://test")
    other_client = AsyncClient(transport=ASGITransport(app=app), base_url="https://test")
    try:
        owner_token = await create_session(db_session, owner.id)
        reg_token = await create_session(db_session, regular.id)
        await db_session.commit()
        owner_client.cookies.set(SESSION_COOKIE_NAME, owner_token)
        other_client.cookies.set(SESSION_COOKIE_NAME, reg_token)

        # Non-owner forbidden from moderation queue
        assert (await anon_client.get("/api/life/moderation/comments")).status_code == 401
        assert (await other_client.get("/api/life/moderation/comments")).status_code == 403

        # 1. Anon sends flagged comment -> held pending, safe DM ping (NO raw bad text in DM!)
        r_bad = await anon_client.post(
            f"/api/life/posts/{post.id}/comments",
            json={"body": "toxic payload https://evil.example", "altcha_payload": await _solve_altcha(anon_client)},
        )
        assert r_bad.status_code == 201
        bad_dto = r_bad.json()
        assert bad_dto["moderation_status"] == "pending"

        # Wait briefly for background moderation ping task
        await asyncio.sleep(0.05)
        assert len(tg_client.sent_messages) == 1
        dm = tg_client.sent_messages[0]
        assert dm["chat_id"] == cfg.OWNER_TG_ID
        assert "pending review" in dm["text"]
        assert "toxic payload" not in dm["text"]
        assert "evil.example" not in dm["text"]
        assert f"?review={bad_dto['id']}" in dm["text"]

        # Hidden from other users, visible with 'pending' to caller, count remains 0
        other_list = (await other_client.get(f"/api/life/posts/{post.id}/comments")).json()["comments"]
        assert other_list == []
        anon_list = (await anon_client.get(f"/api/life/posts/{post.id}/comments")).json()["comments"]
        assert len(anon_list) == 1 and anon_list[0]["moderation_status"] == "pending"
        interactions = (await other_client.get(f"/api/life/interactions?ids={post.id}")).json()
        assert interactions["posts"][str(post.id)]["comment_count"] == 0

        # 2. Owner views queue & approves + whitelists sender
        q_res = await owner_client.get("/api/life/moderation/comments")
        assert q_res.status_code == 200
        q_data = q_res.json()
        assert q_data["pending_count"] == 1
        assert q_data["comments"][0]["id"] == bad_dto["id"]
        assert q_data["comments"][0]["can_whitelist"] is True

        dec = await owner_client.post(
            f"/api/life/moderation/comments/{bad_dto['id']}/decision",
            json={"action": "approve", "whitelist_sender": True},
        )
        assert dec.status_code == 200
        assert dec.json()["moderation_status"] == "approved"
        assert dec.json()["whitelisted"] is True

        await asyncio.sleep(0.05)
        # Now visible to everyone AND bridged to discussion group
        other_list2 = (await other_client.get(f"/api/life/posts/{post.id}/comments")).json()["comments"]
        assert len(other_list2) == 1 and other_list2[0]["moderation_status"] == "approved"
        assert any(m["chat_id"] == cfg.TG_DISCUSSION_CHAT_ID for m in tg_client.sent_messages)

        # 3. Same whitelisted anon sender can now post link/text immediately without OpenAI call
        calls_before = len(openai_calls)
        r_wl = await anon_client.post(
            f"/api/life/posts/{post.id}/comments",
            json={"body": "toxic link https://ok.example", "altcha_payload": await _solve_altcha(anon_client)},
        )
        assert r_wl.status_code == 201
        assert r_wl.json()["moderation_status"] == "approved"
        assert len(openai_calls) == calls_before

        # 4. Owner removes sender from whitelist -> anon link from them is held again, then rejected
        q_res2 = (await owner_client.get("/api/life/moderation/comments")).json()
        assert len(q_res2["whitelist"]) == 1
        wl_id = q_res2["whitelist"][0]["id"]
        rm_res = await owner_client.delete(f"/api/life/moderation/whitelist/{wl_id}")
        assert rm_res.status_code == 200

        r_link = await anon_client.post(
            f"/api/life/posts/{post.id}/comments",
            json={"body": "check https://example.org", "altcha_payload": await _solve_altcha(anon_client)},
        )
        assert r_link.status_code == 201
        assert r_link.json()["moderation_status"] == "pending"

        rej = await owner_client.post(
            f"/api/life/moderation/comments/{r_link.json()['id']}/decision",
            json={"action": "reject", "whitelist_sender": False},
        )
        assert rej.status_code == 200
        assert rej.json()["moderation_status"] == "rejected"
        # Rejected comment disappears for everyone including caller
        anon_list_after = (await anon_client.get(f"/api/life/posts/{post.id}/comments")).json()["comments"]
        assert all(c["id"] != r_link.json()["id"] for c in anon_list_after)
    finally:
        await owner_client.aclose()
        await anon_client.aclose()
        await other_client.aclose()
        await engine.dispose()
