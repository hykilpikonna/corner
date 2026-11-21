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
async def test_telegram_oidc_id_token_callback(
    test_settings: Settings,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from joserfc import jwt
    from joserfc.jwk import KeySet, RSAKey

    from app.auth import oauth as oauth_mod

    rsa_key = RSAKey.generate_key(2048, {"kid": "tg-test-1", "alg": "RS256", "use": "sig"})
    public_jwks = KeySet.import_key_set({"keys": [rsa_key.as_dict(private=False)]})

    async def fake_fetch_jwks(*, force_refresh: bool = False) -> KeySet:
        return public_jwks

    monkeypatch.setattr(oauth_mod, "_fetch_telegram_jwks", fake_fetch_jwks)

    cfg = test_settings.model_copy(
        update={
            "TG_BOT_TOKEN": "123456789:DUMMY_TEST_BOT_TOKEN_NOT_REAL",
            "TG_LOGIN_BOT_USERNAME": "dummy_test_bot",
            "OWNER_TG_ID": 777001,
        }
    )
    _, client = await _make_client(cfg, db_session)

    now = int(time.time())
    async with client:
        # 1. Expired ID token -> 400
        expired_tok = jwt.encode(
            {"alg": "RS256", "kid": "tg-test-1"},
            {
                "iss": "https://oauth.telegram.org",
                "aud": "123456789",
                "sub": "pair-1",
                "iat": now - 7200,
                "exp": now - 300,
                "id": 777001,
                "name": "Owner",
            },
            rsa_key,
        )
        assert (
            await client.post("/api/life/auth/telegram/callback", json={"id_token": expired_tok})
        ).status_code == 400

        # 2. Wrong aud -> 400
        wrong_aud_tok = jwt.encode(
            {"alg": "RS256", "kid": "tg-test-1"},
            {
                "iss": "https://oauth.telegram.org",
                "aud": "999999999",
                "sub": "pair-1",
                "iat": now,
                "exp": now + 600,
                "id": 777001,
                "name": "Owner",
            },
            rsa_key,
        )
        assert (
            await client.post("/api/life/auth/telegram/callback", json={"id_token": wrong_aud_tok})
        ).status_code == 400

        # 3. Wrong signature -> 400
        other_key = RSAKey.generate_key(2048, {"kid": "tg-test-1", "alg": "RS256", "use": "sig"})
        bad_sig_tok = jwt.encode(
            {"alg": "RS256", "kid": "tg-test-1"},
            {
                "iss": "https://oauth.telegram.org",
                "aud": "123456789",
                "sub": "pair-1",
                "iat": now,
                "exp": now + 600,
                "id": 777001,
                "name": "Owner",
            },
            other_key,
        )
        assert (
            await client.post("/api/life/auth/telegram/callback", json={"id_token": bad_sig_tok})
        ).status_code == 400

        # 4. Valid OIDC ID token -> 200 and owner role
        valid_tok = jwt.encode(
            {"alg": "RS256", "kid": "tg-test-1"},
            {
                "iss": "https://oauth.telegram.org",
                "aud": "123456789",
                "sub": "pair-owner-1",
                "iat": now,
                "exp": now + 600,
                "id": 777001,
                "name": "Owner OIDC",
                "preferred_username": "siteowner",
                "picture": "https://cdn4.telesco.pe/file/avatar.jpg",
            },
            rsa_key,
        )
        r_ok = await client.post("/api/life/auth/telegram/callback", json={"id_token": valid_tok})
        assert r_ok.status_code == 200
        user = r_ok.json()["user"]
        assert user["role"] == "owner"
        assert user["provider"] == "telegram"
        assert user["display_name"] == "Owner OIDC"
        assert user["avatar_url"] == "https://cdn4.telesco.pe/file/avatar.jpg"


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

        # GET /auth/me exposes telegram_bot_username and telegram_client_id when telegram is enabled
        me_tg = (await client.get("/api/life/auth/me")).json()
        assert "telegram" in me_tg["providers"]
        assert me_tg["telegram_bot_username"] == "dummy_test_bot"
        assert me_tg["telegram_client_id"] == "123456789"

        # Auto-detected bot_username from running TelegramService works without TG_LOGIN_BOT_USERNAME
        cfg_auto = test_settings.model_copy(
            update={"TG_BOT_TOKEN": dummy_bot_token, "TG_LOGIN_BOT_USERNAME": None}
        )
        app_auto, client_auto = await _make_client(cfg_auto, db_session)
        app_auto.state.tg_service = type("Svc", (), {"bot_username": "@auto_detected_bot"})()
        async with client_auto:
            me_auto = (await client_auto.get("/api/life/auth/me")).json()
            assert "telegram" in me_auto["providers"]
            assert me_auto["telegram_bot_username"] == "auto_detected_bot"

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
async def test_oauth_cancel_redirect_and_cross_origin_next_url(
    test_settings: Settings,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = test_settings.model_copy(
        update={
            "BASE_URL": "https://api.aza.moe",
            "SITE_URL": "https://aza.moe/life",
            "CORS_ORIGINS": ["https://aza.moe", "https://www.aza.moe"],
            "GOOGLE_CLIENT_ID": "dummy_google_id",
            "GOOGLE_CLIENT_SECRET": "dummy_google_secret",
        }
    )
    _, client = await _make_client(cfg, db_session)

    async def _fake_exchange(provider: str, *, code: str, settings: Settings) -> OAuthProfile:
        assert provider == "google"
        assert code == "google-code"
        return OAuthProfile(
            provider="google",
            provider_user_id="g-123",
            display_name="G User",
            username=None,
            email="guser@example.com",
            email_verified=True,
            avatar_url=None,
        )

    monkeypatch.setattr("app.routes.auth.exchange_oauth_code", _fake_exchange)

    async with client:
        # 1. Relative next path resolves to SITE_URL's origin (not api.aza.moe)
        r_rel_login = await client.get(
            "/api/life/auth/oauth/google/login",
            params={"next": "/life?shared=99"},
        )
        assert r_rel_login.status_code == 302
        rel_state = parse_qs(urlsplit(r_rel_login.headers["location"]).query)["state"][0]

        r_rel_cb = await client.get(
            "/api/life/auth/oauth/google/callback",
            params={"code": "google-code", "state": rel_state},
        )
        assert r_rel_cb.status_code == 302
        assert r_rel_cb.headers["location"] == "https://aza.moe/life?shared=99"

        # 2. CORS_ORIGINS URL (e.g. www.aza.moe) is allowed cleanly
        r_www_login = await client.get(
            "/api/life/auth/oauth/google/login",
            params={"next": "https://www.aza.moe/life"},
        )
        assert r_www_login.status_code == 302
        www_state = parse_qs(urlsplit(r_www_login.headers["location"]).query)["state"][0]

        # 3. Provider cancellation (?error=access_denied) redirects back to frontend with auth_error
        r_cancel = await client.get(
            "/api/life/auth/oauth/google/callback",
            params={"error": "access_denied", "state": www_state},
        )
        assert r_cancel.status_code == 302
        cancel_loc = urlsplit(r_cancel.headers["location"])
        cancel_qs = parse_qs(cancel_loc.query)
        assert f"{cancel_loc.scheme}://{cancel_loc.netloc}{cancel_loc.path}" == "https://www.aza.moe/life"
        assert cancel_qs["auth_error"] == ["cancelled"]
        assert cancel_qs["auth_provider"] == ["google"]

        # 4. Cancellation or missing code without state cookie falls back safely to SITE_URL
        r_no_state_cancel = await client.get(
            "/api/life/auth/oauth/google/callback",
            params={"error": "access_denied", "state": "stale"},
        )
        assert r_no_state_cancel.status_code == 302
        ns_loc = urlsplit(r_no_state_cancel.headers["location"])
        ns_qs = parse_qs(ns_loc.query)
        assert f"{ns_loc.scheme}://{ns_loc.netloc}{ns_loc.path}" == "https://aza.moe/life"
        assert ns_qs["auth_error"] == ["cancelled"]

        r_missing_code = await client.get("/api/life/auth/oauth/google/callback")
        assert r_missing_code.status_code == 302
        assert parse_qs(urlsplit(r_missing_code.headers["location"]).query)["auth_error"] == ["oauth_failed"]


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


@pytest.mark.asyncio
async def test_oidc_discovery_pkce_login_and_callback_flow(
    test_settings: Settings,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from app.auth.oauth import (
        clear_oidc_discovery_cache,
        compute_pkce_challenge_s256,
    )
    from app.db.models import UserIdentity

    clear_oidc_discovery_cache()

    issuer = "https://idp.example.com/oauth/"
    issuer_norm = "https://idp.example.com/oauth"
    cfg = test_settings.model_copy(
        update={
            "OIDC_ISSUER_URL": issuer,
            "OIDC_CLIENT_ID": "dummy_oidc_client_id",
            "OIDC_CLIENT_SECRET": "dummy_oidc_client_secret",
            "OIDC_PROVIDER_NAME": "Logto",
            "OIDC_SCOPES": "openid email profile",
        }
    )
    _, client = await _make_client(cfg, db_session)

    discovery_hits = 0
    captured_token_form: dict[str, str] = {}

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal discovery_hits
        url = str(request.url)
        if url == f"{issuer_norm}/.well-known/openid-configuration":
            discovery_hits += 1
            return httpx.Response(
                200,
                json={
                    "issuer": issuer_norm,
                    "authorization_endpoint": f"{issuer_norm}/authorize",
                    "token_endpoint": f"{issuer_norm}/token",
                    "userinfo_endpoint": f"{issuer_norm}/userinfo",
                },
            )
        if url == f"{issuer_norm}/token" and request.method == "POST":
            form_qs = parse_qs(request.content.decode("utf-8"))
            captured_token_form.update({k: v[0] for k, v in form_qs.items()})
            assert captured_token_form.get("grant_type") == "authorization_code"
            assert captured_token_form.get("client_id") == "dummy_oidc_client_id"
            assert captured_token_form.get("client_secret") == "dummy_oidc_client_secret"
            assert captured_token_form.get("code") == "oidc-auth-code-123"
            return httpx.Response(
                200,
                json={"access_token": "oidc-access-token-xyz", "token_type": "Bearer"},
            )
        if url == f"{issuer_norm}/userinfo" and request.method == "GET":
            assert request.headers.get("authorization") == "Bearer oidc-access-token-xyz"
            return httpx.Response(
                200,
                json={
                    "sub": "usr_789",
                    "name": "Clara SSO",
                    "preferred_username": "clara",
                    "email": "Clara@Example.com",
                    "email_verified": True,
                    "picture": "https://cdn.example.com/clara.png",
                },
            )
        return httpx.Response(404, json={"error": "not_found"})

    mock_transport = httpx.MockTransport(_mock_handler)
    real_async_client = httpx.AsyncClient

    def _patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = mock_transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("app.auth.oauth.httpx.AsyncClient", _patched_async_client)

    try:
        async with client:
            # 1. GET /auth/me advertises "oidc" and oidc_provider_name
            me_before = (await client.get("/api/life/auth/me")).json()
            assert "oidc" in me_before["providers"]
            assert me_before["oidc_provider_name"] == "Logto"

            # 2. OIDC login triggers discovery and redirects with PKCE S256 challenge
            r_login = await client.get(
                "/api/life/auth/oauth/oidc/login",
                params={"next": "/life?shared=9"},
            )
            assert r_login.status_code == 302
            assert discovery_hits == 1

            loc = r_login.headers["location"]
            assert loc.startswith(f"{issuer_norm}/authorize?")
            qs = parse_qs(urlsplit(loc).query)
            state_val = qs["state"][0]
            code_challenge = qs["code_challenge"][0]
            assert qs["code_challenge_method"][0] == "S256"
            assert qs["response_type"][0] == "code"
            assert qs["client_id"][0] == "dummy_oidc_client_id"
            assert qs["scope"][0] == "openid email profile"

            # 3. Callback exchanges code + PKCE code_verifier matching code_challenge!
            r_cb = await client.get(
                "/api/life/auth/oauth/oidc/callback",
                params={"code": "oidc-auth-code-123", "state": state_val},
            )
            assert r_cb.status_code == 302
            assert r_cb.headers["location"] == "/life?shared=9"
            # Discovery document was cached (still only 1 fetch!)
            assert discovery_hits == 1

            sent_verifier = captured_token_form.get("code_verifier")
            assert sent_verifier is not None
            assert compute_pkce_challenge_s256(sent_verifier) == code_challenge

            # 4. User is logged in with provider="oidc" and issuer-scoped provider_user_id
            me_after = (await client.get("/api/life/auth/me")).json()["user"]
            assert me_after["display_name"] == "Clara SSO"
            assert me_after["provider"] == "oidc"
            assert me_after["email_verified"] is True
            assert me_after["avatar_url"] == "https://cdn.example.com/clara.png"

            ident = (
                await db_session.execute(
                    select(UserIdentity).where(UserIdentity.provider == "oidc")
                )
            ).scalar_one()
            assert ident.provider_user_id == f"{issuer_norm}:usr_789"
            assert ident.username == "clara"

        # 5. Non-HTTPS remote OIDC issuer URL is rejected (502)
        clear_oidc_discovery_cache()
        bad_cfg = cfg.model_copy(update={"OIDC_ISSUER_URL": "http://insecure.example.com"})
        _, bad_client = await _make_client(bad_cfg, db_session)
        async with bad_client:
            r_insecure = await bad_client.get("/api/life/auth/oauth/oidc/login")
            assert r_insecure.status_code == 502
    finally:
        clear_oidc_discovery_cache()


@pytest.mark.asyncio
async def test_oidc_basic_auth_error_handling_and_security_edge_cases(
    test_settings: Settings,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from app.auth.oauth import (
        clear_oidc_discovery_cache,
        create_oauth_state_cookie,
    )
    from app.auth.security import OAUTH_STATE_COOKIE_NAME
    from app.db.models import UserIdentity

    clear_oidc_discovery_cache()
    issuer = "https://idp-basic.example.com"
    real_async_client = httpx.AsyncClient

    # Seed an existing verified user to ensure unverified OIDC emails never hijack their account
    existing_user = User(
        role="user",
        display_name="Victim",
        email="victim@example.com",
        email_verified=True,
        notify_replies=True,
    )
    db_session.add(existing_user)
    await db_session.commit()

    # Whitespace OIDC_PROVIDER_NAME falls back to "SSO"; scopes without "openid" prepend it
    cfg = test_settings.model_copy(
        update={
            "OIDC_ISSUER_URL": issuer,
            "OIDC_CLIENT_ID": "basic_cid",
            "OIDC_CLIENT_SECRET": "basic_csecret",
            "OIDC_PROVIDER_NAME": "   ",
            "OIDC_SCOPES": "email profile",
        }
    )

    mode: dict[str, Any] = {"case": "basic_ok", "userinfo": {}}
    captured_basic_auth: dict[str, Any] = {}

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{issuer}/.well-known/openid-configuration":
            if mode["case"] == "issuer_mismatch":
                return httpx.Response(
                    200,
                    json={
                        "issuer": "https://other-idp.example.com",
                        "authorization_endpoint": f"{issuer}/auth",
                        "token_endpoint": f"{issuer}/token",
                        "userinfo_endpoint": f"{issuer}/userinfo",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "issuer": issuer,
                    "authorization_endpoint": f"{issuer}/auth",
                    "token_endpoint": f"{issuer}/token",
                    "userinfo_endpoint": f"{issuer}/userinfo",
                    "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                },
            )
        if url == f"{issuer}/token":
            if mode["case"] == "token_timeout":
                raise httpx.ConnectTimeout("upstream timed out")
            if mode["case"] == "token_non_json":
                return httpx.Response(200, text="<html>Bad Gateway</html>")
            captured_basic_auth["auth_header"] = request.headers.get("authorization")
            captured_basic_auth["form"] = parse_qs(request.content.decode("utf-8"))
            return httpx.Response(200, json={"access_token": "tok-basic"})
        if url == f"{issuer}/userinfo":
            if mode["case"] == "userinfo_non_json":
                return httpx.Response(200, text="not-json")
            return httpx.Response(200, json=mode["userinfo"])
        return httpx.Response(404)

    mock_transport = httpx.MockTransport(_mock_handler)

    def _patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = mock_transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("app.auth.oauth.httpx.AsyncClient", _patched_async_client)

    try:
        _, client = await _make_client(cfg, db_session)
        async with client:
            # 1. Whitespace OIDC_PROVIDER_NAME normalized to "SSO"
            me_meta = (await client.get("/api/life/auth/me")).json()
            assert me_meta["oidc_provider_name"] == "SSO"

            # 2. Discovery issuer mismatch -> 502
            mode["case"] = "issuer_mismatch"
            r_mismatch = await client.get("/api/life/auth/oauth/oidc/login")
            assert r_mismatch.status_code == 502

            # 3. Valid login with client_secret_basic + scope normalization ("openid" prepended)
            clear_oidc_discovery_cache()
            mode["case"] = "basic_ok"
            mode["userinfo"] = {
                "sub": "logto_42",
                "username": "logto_user",
                "email": "victim@example.com",
                "email_verified": False,
            }
            r_login = await client.get("/api/life/auth/oauth/oidc/login")
            assert r_login.status_code == 302
            qs = parse_qs(urlsplit(r_login.headers["location"]).query)
            assert qs["scope"][0] == "openid email profile"
            state_val = qs["state"][0]

            # Callback uses HTTP Basic auth (omits client_secret from body) & does NOT link unverified email
            r_cb = await client.get(
                "/api/life/auth/oauth/oidc/callback",
                params={"code": "c1", "state": state_val},
            )
            assert r_cb.status_code == 302
            expected_basic = "Basic " + base64.b64encode(
                b"basic_cid:basic_csecret"
            ).decode("ascii")
            assert captured_basic_auth["auth_header"] == expected_basic
            assert "client_secret" not in captured_basic_auth["form"]

            me_unverified = (await client.get("/api/life/auth/me")).json()["user"]
            assert me_unverified["id"] != existing_user.id
            assert me_unverified["display_name"] == "logto_user"
            assert me_unverified["email_verified"] is False
            ident = (
                await db_session.execute(
                    select(UserIdentity).where(
                        UserIdentity.provider == "oidc",
                        UserIdentity.provider_user_id == f"{issuer}:logto_42",
                    )
                )
            ).scalar_one()
            assert ident.username == "logto_user"

            # 4. String "false" email_verified also must NOT link to existing verified user
            mode["userinfo"] = {
                "sub": "logto_43",
                "name": "String False",
                "email": "victim@example.com",
                "email_verified": "false",
            }
            r_l2 = await client.get("/api/life/auth/oauth/oidc/login")
            st2 = parse_qs(urlsplit(r_l2.headers["location"]).query)["state"][0]
            r_cb2 = await client.get(
                "/api/life/auth/oauth/oidc/callback",
                params={"code": "c2", "state": st2},
            )
            assert r_cb2.status_code == 302
            me_str_false = (await client.get("/api/life/auth/me")).json()["user"]
            assert me_str_false["id"] != existing_user.id
            assert me_str_false["email_verified"] is False

            # 5. Valid state cookie missing PKCE `cv` -> 400
            st_no_cv, cookie_no_cv = create_oauth_state_cookie(
                provider="oidc",
                next_url="/life",
                secret_key=cfg.SECRET_KEY,
                code_verifier=None,
            )
            client.cookies.set(OAUTH_STATE_COOKIE_NAME, cookie_no_cv)
            r_no_cv = await client.get(
                "/api/life/auth/oauth/oidc/callback",
                params={"code": "c3", "state": st_no_cv},
            )
            assert r_no_cv.status_code == 400
            client.cookies.clear()

            # 6. Upstream network timeout, non-JSON token/userinfo, and whitespace `sub` -> 502
            for bad_case, bad_info in (
                ("token_timeout", {}),
                ("token_non_json", {}),
                ("userinfo_non_json", {}),
                ("basic_ok", {"sub": "   "}),
            ):
                mode["case"] = bad_case
                mode["userinfo"] = bad_info
                r_l = await client.get("/api/life/auth/oauth/oidc/login")
                st = parse_qs(urlsplit(r_l.headers["location"]).query)["state"][0]
                r_err = await client.get(
                    "/api/life/auth/oauth/oidc/callback",
                    params={"code": "c_err", "state": st},
                )
                assert r_err.status_code == 502, f"Expected 502 for {bad_case}/{bad_info}"

        # 7. Embedded URL credentials in OIDC_ISSUER_URL rejected with 502
        clear_oidc_discovery_cache()
        cred_cfg = cfg.model_copy(
            update={"OIDC_ISSUER_URL": "https://user:pass@idp-basic.example.com"}
        )
        _, cred_client = await _make_client(cred_cfg, db_session)
        async with cred_client:
            r_cred = await cred_client.get("/api/life/auth/oauth/oidc/login")
            assert r_cred.status_code == 502
    finally:
        clear_oidc_discovery_cache()
