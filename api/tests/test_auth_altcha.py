from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.captcha import create_altcha_challenge
from app.auth.oauth import OAuthProfile
from app.auth.security import make_unsubscribe_token
from app.config import Settings, get_settings
from app.db.models import User
from app.db.session import get_db_session
from app.mailer import Mailer
from app.main import create_app


def solve_altcha(challenge_json: dict[str, Any]) -> str:
    salt = challenge_json["salt"]
    target = challenge_json["challenge"]
    maxnumber = int(challenge_json["maxnumber"])
    for n in range(maxnumber + 1):
        digest = hashlib.sha256(f"{salt}{n}".encode()).hexdigest()
        if digest == target:
            payload = {
                "algorithm": challenge_json["algorithm"],
                "challenge": target,
                "number": n,
                "salt": salt,
                "signature": challenge_json["signature"],
            }
            return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    raise AssertionError("Failed to solve ALTCHA challenge within maxnumber")


def sign_telegram_payload(data: dict[str, Any], bot_token: str) -> dict[str, Any]:
    pairs = [f"{k}={data[k]}" for k in sorted(data.keys()) if data[k] is not None]
    check_string = "\n".join(pairs)
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    digest = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return {**data, "hash": digest}


async def _make_client(
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
        follow_redirects=False,
    )
    return app, client


@pytest.mark.asyncio
async def test_altcha_challenge_solve_replay_expiry_and_escalation(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    cfg = test_settings.model_copy(update={"ALTCHA_MAX_NUMBER": 200})
    app, client = await _make_client(cfg, db_session)
    sent_emails: list[Any] = []

    async def _fake_send(msg: Any) -> None:
        sent_emails.append(msg)

    app.state.mailer = Mailer(cfg, app.state.sse_bus, smtp_sender=_fake_send)

    async with client:
        # 1. Fetch & solve valid challenge (canonical trailing '&')
        r = await client.get("/api/life/captcha/challenge")
        assert r.status_code == 200
        ch = r.json()
        assert ch["algorithm"] == "SHA-256"
        assert ch["maxnumber"] == 200
        assert "?expires=" in ch["salt"]
        assert ch["salt"].endswith("&")
        solved_b64 = solve_altcha(ch)

        # 1b. ALTCHA digit-shifting / non-canonical salt attack MUST be rejected
        non_canon_raw = ch["salt"].rstrip("&")
        non_canon_payload = {
            "algorithm": "SHA-256",
            "challenge": hashlib.sha256(f"{non_canon_raw}42".encode()).hexdigest(),
            "number": 42,
            "salt": non_canon_raw,
            "signature": hmac.new(
                cfg.SECRET_KEY.encode("utf-8"),
                hashlib.sha256(f"{non_canon_raw}42".encode()).hexdigest().encode("utf-8"),
                hashlib.sha256,
            ).hexdigest(),
        }
        non_canon_b64 = base64.b64encode(
            json.dumps(non_canon_payload).encode("utf-8")
        ).decode("ascii")
        r_non_canon = await client.post(
            "/api/life/auth/magic-link/request",
            json={"email": "attacker@example.com", "altcha_payload": non_canon_b64},
        )
        assert r_non_canon.status_code == 400

        # 1c. Oversized ALTCHA payload (>4096 bytes) rejected before decoding
        r_huge = await client.post(
            "/api/life/auth/magic-link/request",
            json={"email": "attacker@example.com", "altcha_payload": "A" * 5000},
        )
        assert r_huge.status_code == 400

        # 2. First verification succeeds (via magic-link request)
        ok_resp = await client.post(
            "/api/life/auth/magic-link/request",
            json={"email": "alice@example.com", "altcha_payload": solved_b64},
        )
        assert ok_resp.status_code == 200
        assert len(sent_emails) == 1

        # 3. Replay of the same salt must be rejected
        replay_resp = await client.post(
            "/api/life/auth/magic-link/request",
            json={"email": "alice@example.com", "altcha_payload": solved_b64},
        )
        assert replay_resp.status_code == 400
        assert "already been used" in replay_resp.json()["detail"].lower()

        # 4. Expired challenge must be rejected
        app.state.rate_limiter._windows.clear()
        expired_ch = create_altcha_challenge(cfg, maxnumber=50, ttl_seconds=-10)
        expired_b64 = solve_altcha(expired_ch)
        exp_resp = await client.post(
            "/api/life/auth/magic-link/request",
            json={"email": "bob@example.com", "altcha_payload": expired_b64},
        )
        assert exp_resp.status_code == 400
        assert "expired" in exp_resp.json()["detail"].lower()

        # Verify replay table stores `challenge` directly as PK
        from app.db.models import UsedCaptchaSalt

        used_row = await db_session.get(UsedCaptchaSalt, ch["challenge"])
        assert used_row is not None

        # 5. Tampered signature must be rejected
        bad_ch = create_altcha_challenge(cfg, maxnumber=50)
        bad_ch["signature"] = "0" * 64
        bad_b64 = solve_altcha(bad_ch)
        tamper_resp = await client.post(
            "/api/life/auth/magic-link/request",
            json={"email": "bob@example.com", "altcha_payload": bad_b64},
        )
        assert tamper_resp.status_code == 400
        assert "signature" in tamper_resp.json()["detail"].lower()

        # 6. Burst difficulty escalation on same IP
        maxnumbers: list[int] = []
        for _ in range(7):
            burst_r = await client.get("/api/life/captcha/challenge")
            assert burst_r.status_code == 200
            maxnumbers.append(burst_r.json()["maxnumber"])
        assert maxnumbers[-1] > 200


@pytest.mark.asyncio
async def test_magic_link_full_flow_open_redirect_and_owner_reserved_names(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    cfg = test_settings.model_copy(
        update={
            "ALTCHA_MAX_NUMBER": 100,
            "OWNER_RESERVED_NAMES": ["owner", "admin", "Aza"],
        }
    )
    app, client = await _make_client(cfg, db_session)
    captured_urls: list[str] = []

    async def _fake_send(msg: Any) -> None:
        text_body = msg.get_body(preferencelist=("plain",)).get_content()
        for line in text_body.splitlines():
            if "/auth/magic-link/verify?" in line:
                captured_urls.append(line.strip())

    app.state.mailer = Mailer(cfg, app.state.sse_bus, smtp_sender=_fake_send)

    async with client:
        # Reject reserved owner alias on magic-link request (even with zero-width chars!)
        ch1 = (await client.get("/api/life/captcha/challenge")).json()
        r_reserved = await client.post(
            "/api/life/auth/magic-link/request",
            json={
                "email": "impostor@example.com",
                "display_name": "A\u200bz a",
                "altcha_payload": solve_altcha(ch1),
            },
        )
        assert r_reserved.status_code == 400

        # Valid magic-link request
        ch2 = (await client.get("/api/life/captcha/challenge")).json()
        r_req = await client.post(
            "/api/life/auth/magic-link/request",
            json={
                "email": "Alice@Example.com",
                "display_name": "Alice",
                "altcha_payload": solve_altcha(ch2),
                "next": "/life?shared=42",
            },
        )
        assert r_req.status_code == 200
        assert len(captured_urls) == 1

        parsed = urlsplit(captured_urls[0])
        qs = parse_qs(parsed.query)
        raw_token = qs["token"][0]
        assert qs["next"][0] == "/life?shared=42"

        # Open-redirect attacks on verify endpoint must be rejected (without consuming token!)
        for evil in (
            "https://evil.com/phish",
            "//evil.com/phish",
            "javascript:alert(1)",
            "/\\evil.com",
            "https://aza.moe.evil.com/life",
        ):
            r_evil = await client.get(
                "/api/life/auth/magic-link/verify",
                params={"token": raw_token, "next": evil},
            )
            assert r_evil.status_code == 400, f"Failed to reject {evil}"

        # Legitimate verify succeeds, sets HttpOnly SameSite=Lax Secure session cookie
        r_verify = await client.get(
            "/api/life/auth/magic-link/verify",
            params={"token": raw_token, "next": "/life?shared=42"},
        )
        assert r_verify.status_code == 302
        assert r_verify.headers["location"] == "/life?shared=42"
        set_cookie = r_verify.headers.get("set-cookie", "")
        assert "life_session=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie or "SameSite=Lax" in set_cookie
        assert "Secure" in set_cookie

        # Token is single-use
        r_reuse = await client.get(
            "/api/life/auth/magic-link/verify",
            params={"token": raw_token, "next": "/life"},
        )
        assert r_reuse.status_code == 400

        # /auth/me returns verified user
        me_resp = await client.get("/api/life/auth/me")
        assert me_resp.status_code == 200
        me_data = me_resp.json()
        assert me_data["user"]["display_name"] == "Alice"
        assert me_data["user"]["email_verified"] is True
        assert me_data["user"]["provider"] == "email"
        assert me_data["user"]["notify_replies"] is True

        # PATCH /auth/me blocks owner-reserved alias, homoglyphs, combining marks, and control chars
        for forbidden_name in (
            "ADMIN\u200b",
            "\u0430dmin",  # Cyrillic 'а'
            "a\u0301dmin",  # combining acute accent
            "a.d.m.i.n",
            "Alice\x00",
            "Alice\x07",
        ):
            bad_patch = await client.patch(
                "/api/life/auth/me",
                json={"display_name": forbidden_name},
            )
            assert bad_patch.status_code == 400, f"Should reject {forbidden_name!r}"

        # PATCH /auth/me updates normal name & notify_replies
        good_patch = await client.patch(
            "/api/life/auth/me",
            json={"display_name": "Alice Cooper", "notify_replies": False},
        )
        assert good_patch.status_code == 200
        assert good_patch.json()["user"]["display_name"] == "Alice Cooper"
        assert good_patch.json()["user"]["notify_replies"] is False

        # Logout clears session
        logout_resp = await client.post("/api/life/auth/logout")
        assert logout_resp.status_code == 200
        after_logout = await client.get("/api/life/auth/me")
        assert after_logout.json()["user"] is None


@pytest.mark.asyncio
async def test_telegram_login_widget_and_owner_role_and_unsubscribe(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    dummy_bot_token = "123456789:DUMMY_TEST_BOT_TOKEN_NOT_REAL"
    cfg = test_settings.model_copy(
        update={
            "TG_BOT_TOKEN": dummy_bot_token,
            "TG_LOGIN_BOT_USERNAME": "dummy_test_bot",
            "OWNER_TG_ID": 777001,
            "OWNER_RESERVED_NAMES": ["owner", "admin"],
        }
    )
    _, client = await _make_client(cfg, db_session)

    async with client:
        now = int(time.time())

        # Invalid signature rejected
        bad_payload = {
            "id": 777001,
            "first_name": "Owner",
            "username": "siteowner",
            "auth_date": now,
            "hash": "deadbeef" * 8,
        }
        r_bad = await client.post("/api/life/auth/telegram/callback", json=bad_payload)
        assert r_bad.status_code == 400

        # Stale auth_date (> 24h) rejected
        stale_payload = sign_telegram_payload(
            {
                "id": 777001,
                "first_name": "Owner",
                "username": "siteowner",
                "auth_date": now - 90000,
            },
            dummy_bot_token,
        )
        r_stale = await client.post("/api/life/auth/telegram/callback", json=stale_payload)
        assert r_stale.status_code == 400

        # Valid owner login grants owner role and allows owner-reserved display name
        owner_payload = sign_telegram_payload(
            {
                "id": 777001,
                "first_name": "Owner",
                "username": "siteowner",
                "auth_date": now,
            },
            dummy_bot_token,
        )
        r_ok = await client.post("/api/life/auth/telegram/callback", json=owner_payload)
        assert r_ok.status_code == 200
        owner_user = r_ok.json()["user"]
        assert owner_user["role"] == "owner"
        assert owner_user["provider"] == "telegram"
        assert owner_user["display_name"] == "Owner"

        # GET /auth/me exposes telegram_bot_username when telegram is enabled
        me_tg = (await client.get("/api/life/auth/me")).json()
        assert "telegram" in me_tg["providers"]
        assert me_tg["telegram_bot_username"] == "dummy_test_bot"

        # Owner can update name to reserved alias
        r_owner_patch = await client.patch(
            "/api/life/auth/me",
            json={"display_name": "Admin"},
        )
        assert r_owner_patch.status_code == 200
        assert r_owner_patch.json()["user"]["display_name"] == "Admin"

        # Unsubscribe link with tampered token fails (both GET and POST)
        r_bad_unsub = await client.get(
            "/api/life/auth/unsubscribe",
            params={"token": f"{owner_user['id']}.invalid"},
        )
        assert r_bad_unsub.status_code == 400

        # RFC 8058 One-Click POST /auth/unsubscribe works even cross-site
        valid_token = make_unsubscribe_token(owner_user["id"], cfg.SECRET_KEY)
        r_post_unsub = await client.post(
            "/api/life/auth/unsubscribe",
            params={"token": valid_token},
            headers={
                "Origin": "https://mail-provider.example",
                "Sec-Fetch-Site": "cross-site",
            },
            content="List-Unsubscribe=One-Click",
        )
        assert r_post_unsub.status_code == 200
        assert "unsubscribed" in r_post_unsub.text.lower()

        db_user = (
            await db_session.execute(select(User).where(User.id == owner_user["id"]))
        ).scalar_one()
        await db_session.refresh(db_user)
        assert db_user.notify_replies is False

        # Re-enable and verify GET /auth/unsubscribe also works
        db_user.notify_replies = True
        await db_session.commit()
        r_unsub = await client.get(
            "/api/life/auth/unsubscribe",
            params={"token": valid_token},
        )
        assert r_unsub.status_code == 200
        await db_session.refresh(db_user)
        assert db_user.notify_replies is False


@pytest.mark.asyncio
async def test_oauth_csrf_state_and_callback_flow(
    test_settings: Settings,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = test_settings.model_copy(
        update={
            "GITHUB_CLIENT_ID": "dummy_gh_id",
            "GITHUB_CLIENT_SECRET": "dummy_gh_secret",
        }
    )
    _, client = await _make_client(cfg, db_session)

    async def _fake_exchange(provider: str, *, code: str, settings: Settings) -> OAuthProfile:
        assert provider == "github"
        assert code == "test-auth-code"
        return OAuthProfile(
            provider="github",
            provider_user_id="gh-999",
            display_name="Octocat",
            username="octocat",
            email="octocat@example.com",
            email_verified=True,
            avatar_url="https://avatars.example.com/u/999",
        )

    monkeypatch.setattr("app.routes.auth.exchange_oauth_code", _fake_exchange)

    async with client:
        # Open redirect on OAuth login blocked
        r_evil = await client.get(
            "/api/life/auth/oauth/github/login",
            params={"next": "https://evil.example/steal"},
        )
        assert r_evil.status_code == 400

        # Valid OAuth login sets state cookie and redirects to GitHub
        r_login = await client.get(
            "/api/life/auth/oauth/github/login",
            params={"next": "https://aza.moe/life?shared=7"},
        )
        assert r_login.status_code == 302
        loc = r_login.headers["location"]
        assert loc.startswith("https://github.com/login/oauth/authorize?")
        state_val = parse_qs(urlsplit(loc).query)["state"][0]

        # Mismatched CSRF state rejected
        r_csrf_fail = await client.get(
            "/api/life/auth/oauth/github/callback",
            params={"code": "test-auth-code", "state": "wrong-state"},
        )
        assert r_csrf_fail.status_code == 400

        # Matching state succeeds, creates user, sets session, redirects to safe next
        r_cb = await client.get(
            "/api/life/auth/oauth/github/callback",
            params={"code": "test-auth-code", "state": state_val},
        )
        assert r_cb.status_code == 302
        assert r_cb.headers["location"] == "https://aza.moe/life?shared=7"

        me = (await client.get("/api/life/auth/me")).json()["user"]
        assert me["display_name"] == "Octocat"
        assert me["provider"] == "github"
        assert me["email_verified"] is True


@pytest.mark.asyncio
async def test_csrf_origin_middleware_and_ip_spoofing_and_smtp_503(
    test_settings: Settings,
    db_session: AsyncSession,
) -> None:
    from starlette.requests import Request as StarletteRequest

    from app.auth.security import get_client_ip

    cfg = test_settings.model_copy(
        update={
            "ALTCHA_MAX_NUMBER": 50,
            "TRUSTED_PROXIES": ["127.0.0.1", "10.0.0.0/8"],
            "TRUST_CF_HEADERS": False,
            "CORS_ORIGINS": ["https://cors-ok.example"],
        }
    )
    app, client = await _make_client(cfg, db_session)

    async def _broken_smtp(_msg: Any) -> None:
        raise RuntimeError("SMTP down")

    app.state.mailer = Mailer(cfg, app.state.sse_bus, smtp_sender=_broken_smtp)

    # 1. Client IP spoofing checks
    def _make_req(peer: str, headers: list[tuple[bytes, bytes]]) -> StarletteRequest:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (peer, 1234),
            "app": app,
        }
        return StarletteRequest(scope)

    # Untrusted peer cannot spoof XFF or CF-Connecting-IP
    untrusted_req = _make_req(
        "198.51.100.9",
        [(b"x-forwarded-for", b"1.2.3.4"), (b"cf-connecting-ip", b"5.6.7.8")],
    )
    assert get_client_ip(untrusted_req, cfg) == "198.51.100.9"

    # Trusted proxy with TRUST_CF_HEADERS=False ignores CF-Connecting-IP and walks XFF right-to-left
    trusted_xff_req = _make_req(
        "127.0.0.1",
        [
            (b"cf-connecting-ip", b"9.9.9.9"),
            (b"x-forwarded-for", b"6.6.6.6, 203.0.113.42, 10.1.2.3"),
        ],
    )
    assert get_client_ip(trusted_xff_req, cfg) == "203.0.113.42"

    # Trusted proxy with TRUST_CF_HEADERS=True trusts CF-Connecting-IP
    cfg_cf = cfg.model_copy(update={"TRUST_CF_HEADERS": True})
    assert get_client_ip(trusted_xff_req, cfg_cf) == "9.9.9.9"

    # 2. CSRF Origin / Sec-Fetch-Site protection + Magic link SMTP 503
    async with client:
        r_evil_origin = await client.post(
            "/api/life/auth/logout",
            headers={"Origin": "https://evil.example"},
        )
        assert r_evil_origin.status_code == 403

        r_cross_site = await client.post(
            "/api/life/auth/logout",
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert r_cross_site.status_code == 403

        # Allowed SITE_URL origin passes
        r_same_origin = await client.post(
            "/api/life/auth/logout",
            headers={"Origin": "https://aza.moe", "Sec-Fetch-Site": "same-origin"},
        )
        assert r_same_origin.status_code == 200

        # Allowed CORS_ORIGINS passes even with Sec-Fetch-Site: cross-site
        r_cors_origin = await client.post(
            "/api/life/auth/logout",
            headers={
                "Origin": "https://cors-ok.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert r_cors_origin.status_code == 200

        # Magic link SMTP failure returns 503 instead of unhandled 500
        ch = (await client.get("/api/life/captcha/challenge")).json()
        r_smtp_err = await client.post(
            "/api/life/auth/magic-link/request",
            json={"email": "down@example.com", "altcha_payload": solve_altcha(ch)},
        )
        assert r_smtp_err.status_code == 503
