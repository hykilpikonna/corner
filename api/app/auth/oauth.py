from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.security import (
    constant_time_equals,
    sanitize_external_display_name,
)
from app.config import Settings
from app.db.models import User, UserIdentity

OAUTH_STATE_MAX_AGE_SECONDS = 600
TELEGRAM_AUTH_MAX_AGE_SECONDS = 86400  # 24 hours


@dataclass(frozen=True, slots=True)
class OAuthProfile:
    provider: str
    provider_user_id: str
    display_name: str
    username: str | None
    email: str | None
    email_verified: bool
    avatar_url: str | None


def _state_serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key=secret_key, salt="life-oauth-state")


def create_oauth_state_cookie(
    *, provider: str, next_url: str, secret_key: str
) -> tuple[str, str]:
    state = secrets.token_urlsafe(24)
    token = _state_serializer(secret_key).dumps(
        {"provider": provider, "state": state, "next": next_url}
    )
    return state, token


def verify_oauth_state_cookie(
    cookie_value: str | None,
    *,
    expected_provider: str,
    received_state: str | None,
    secret_key: str,
) -> str:
    if not cookie_value or not received_state:
        raise HTTPException(status_code=400, detail="Missing OAuth state")
    try:
        data = _state_serializer(secret_key).loads(
            cookie_value, max_age=OAUTH_STATE_MAX_AGE_SECONDS
        )
    except SignatureExpired as exc:
        raise HTTPException(status_code=400, detail="OAuth state expired") from exc
    except BadSignature as exc:
        raise HTTPException(status_code=400, detail="Invalid OAuth state") from exc

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    cookie_provider = str(data.get("provider") or "")
    cookie_state = str(data.get("state") or "")
    next_url = str(data.get("next") or "")

    if cookie_provider != expected_provider or not constant_time_equals(
        cookie_state, received_state
    ):
        raise HTTPException(status_code=400, detail="OAuth state mismatch")

    return next_url


def build_oauth_authorize_url(
    provider: str, *, state: str, settings: Settings
) -> str:
    redirect_uri = f"{settings.api_base_url}/auth/oauth/{provider}/callback"

    if provider == "github":
        if not settings.GITHUB_CLIENT_ID or not settings.GITHUB_CLIENT_SECRET:
            raise HTTPException(status_code=503, detail="GitHub OAuth is not configured")
        params = {
            "client_id": settings.GITHUB_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": "read:user user:email",
            "state": state,
        }
        return f"https://github.com/login/oauth/authorize?{urlencode(params)}"

    if provider == "google":
        if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
            raise HTTPException(status_code=503, detail="Google OAuth is not configured")
        params = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        }
        return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"

    raise HTTPException(status_code=404, detail="Unsupported OAuth provider")


async def exchange_oauth_code(
    provider: str,
    *,
    code: str,
    settings: Settings,
) -> OAuthProfile:
    redirect_uri = f"{settings.api_base_url}/auth/oauth/{provider}/callback"

    async with httpx.AsyncClient(timeout=15.0) as client:
        if provider == "github":
            if not settings.GITHUB_CLIENT_ID or not settings.GITHUB_CLIENT_SECRET:
                raise HTTPException(status_code=503, detail="GitHub OAuth is not configured")

            token_resp = await client.post(
                "https://github.com/login/oauth/access_token",
                headers={"Accept": "application/json"},
                data={
                    "client_id": settings.GITHUB_CLIENT_ID,
                    "client_secret": settings.GITHUB_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            )
            if token_resp.status_code != 200:
                raise HTTPException(status_code=502, detail="GitHub token exchange failed")
            token_data = token_resp.json()
            access_token = token_data.get("access_token")
            if not access_token:
                raise HTTPException(status_code=400, detail="GitHub authorization failed")

            auth_headers = {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            }
            user_resp = await client.get("https://api.github.com/user", headers=auth_headers)
            if user_resp.status_code != 200:
                raise HTTPException(status_code=502, detail="Failed to fetch GitHub profile")
            user_data = user_resp.json()

            email: str | None = None
            email_verified = False
            emails_resp = await client.get(
                "https://api.github.com/user/emails", headers=auth_headers
            )
            if emails_resp.status_code == 200 and isinstance(emails_resp.json(), list):
                emails_list = emails_resp.json()
                primary = next(
                    (e for e in emails_list if e.get("primary") and e.get("verified")),
                    None,
                )
                fallback = next((e for e in emails_list if e.get("verified")), None)
                chosen = primary or fallback
                if chosen and chosen.get("email"):
                    email = str(chosen["email"]).strip().lower()
                    email_verified = True

            username = user_data.get("login")
            display_name = user_data.get("name") or username or "GitHub User"
            return OAuthProfile(
                provider="github",
                provider_user_id=str(user_data["id"]),
                display_name=str(display_name),
                username=str(username) if username else None,
                email=email,
                email_verified=email_verified,
                avatar_url=user_data.get("avatar_url"),
            )

        if provider == "google":
            if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
                raise HTTPException(status_code=503, detail="Google OAuth is not configured")

            token_resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            if token_resp.status_code != 200:
                raise HTTPException(status_code=502, detail="Google token exchange failed")
            token_data = token_resp.json()
            access_token = token_data.get("access_token")
            if not access_token:
                raise HTTPException(status_code=400, detail="Google authorization failed")

            info_resp = await client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if info_resp.status_code != 200:
                raise HTTPException(status_code=502, detail="Failed to fetch Google profile")
            info = info_resp.json()

            raw_email = info.get("email")
            email = str(raw_email).strip().lower() if raw_email else None
            email_verified = bool(info.get("email_verified")) and email is not None
            display_name = info.get("name") or (email.split("@")[0] if email else "Google User")
            return OAuthProfile(
                provider="google",
                provider_user_id=str(info["sub"]),
                display_name=str(display_name),
                username=None,
                email=email,
                email_verified=email_verified,
                avatar_url=info.get("picture"),
            )

    raise HTTPException(status_code=404, detail="Unsupported OAuth provider")


def verify_telegram_login_payload(
    payload: dict[str, Any],
    *,
    bot_token: str | None,
    now_ts: int | None = None,
) -> None:
    """Verify Telegram Login Widget HMAC-SHA256 signature and auth_date freshness."""
    if not bot_token:
        raise HTTPException(status_code=503, detail="Telegram login is not configured")

    received_hash = payload.get("hash")
    auth_date = payload.get("auth_date")
    if not isinstance(received_hash, str) or not received_hash:
        raise HTTPException(status_code=400, detail="Missing Telegram hash")
    if isinstance(auth_date, bool) or not isinstance(auth_date, int):
        raise HTTPException(status_code=400, detail="Invalid Telegram auth_date")

    now = int(time.time()) if now_ts is None else int(now_ts)
    if abs(now - auth_date) > TELEGRAM_AUTH_MAX_AGE_SECONDS:
        raise HTTPException(status_code=400, detail="Telegram login payload expired")

    check_pairs: list[str] = []
    for key in sorted(payload.keys()):
        if key == "hash":
            continue
        val = payload[key]
        if val is None:
            continue
        check_pairs.append(f"{key}={val}")
    data_check_string = "\n".join(check_pairs)

    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not constant_time_equals(expected_hash, received_hash):
        raise HTTPException(status_code=400, detail="Invalid Telegram login signature")


async def upsert_oauth_user(
    db: AsyncSession,
    profile: OAuthProfile,
    *,
    settings: Settings,
    is_owner: bool = False,
) -> User:
    # 1. Existing identity lookup
    ident_stmt = (
        select(UserIdentity)
        .options(selectinload(UserIdentity.user).selectinload(User.identities))
        .where(
            UserIdentity.provider == profile.provider,
            UserIdentity.provider_user_id == profile.provider_user_id,
        )
    )
    identity = (await db.execute(ident_stmt)).scalar_one_or_none()
    if identity is not None:
        user = identity.user
        if is_owner and user.role != "owner":
            user.role = "owner"
        if profile.username is not None and identity.username != profile.username:
            identity.username = profile.username
        if profile.avatar_url and not user.avatar_url:
            user.avatar_url = profile.avatar_url
        if profile.email and profile.email_verified and not user.email:
            # Only set email if not already claimed by another user
            existing_email_user = (
                await db.execute(select(User).where(User.email == profile.email))
            ).scalar_one_or_none()
            if existing_email_user is None:
                user.email = profile.email
                user.email_verified = True
        await db.flush()
        return user

    # 2. Link by verified email if present
    user: User | None = None
    if profile.email and profile.email_verified:
        user_stmt = (
            select(User)
            .options(selectinload(User.identities))
            .where(User.email == profile.email, User.email_verified.is_(True))
        )
        user = (await db.execute(user_stmt)).scalar_one_or_none()

    if user is None:
        safe_name = sanitize_external_display_name(
            profile.display_name,
            fallback=profile.username or profile.provider.title(),
            is_owner=is_owner,
            settings=settings,
        )
        email_to_store: str | None = None
        email_verified_to_store = False
        if profile.email and profile.email_verified:
            conflict = (
                await db.execute(select(User).where(User.email == profile.email))
            ).scalar_one_or_none()
            if conflict is None:
                email_to_store = profile.email
                email_verified_to_store = True

        user = User(
            role="owner" if is_owner else "user",
            display_name=safe_name,
            email=email_to_store,
            email_verified=email_verified_to_store,
            avatar_url=profile.avatar_url,
            notify_replies=True,
        )
        try:
            async with db.begin_nested():
                db.add(user)
                await db.flush()
        except IntegrityError:
            # Concurrent signup with same verified email
            if email_to_store:
                user = (
                    await db.execute(
                        select(User)
                        .options(selectinload(User.identities))
                        .where(User.email == email_to_store)
                    )
                ).scalar_one_or_none()
            if user is None:
                raise
    else:
        if is_owner and user.role != "owner":
            user.role = "owner"
        if profile.avatar_url and not user.avatar_url:
            user.avatar_url = profile.avatar_url

    new_identity = UserIdentity(
        provider=profile.provider,
        provider_user_id=profile.provider_user_id,
        user_id=user.id,
        username=profile.username,
    )
    try:
        async with db.begin_nested():
            db.add(new_identity)
            await db.flush()
    except IntegrityError:
        existing_ident = (
            await db.execute(
                select(UserIdentity)
                .options(selectinload(UserIdentity.user).selectinload(User.identities))
                .where(
                    UserIdentity.provider == profile.provider,
                    UserIdentity.provider_user_id == profile.provider_user_id,
                )
            )
        ).scalar_one_or_none()
        if existing_ident is not None:
            return existing_ident.user
        raise

    reloaded = (
        await db.execute(
            select(User).options(selectinload(User.identities)).where(User.id == user.id)
        )
    ).scalar_one()
    return reloaded
