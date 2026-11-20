from __future__ import annotations

import logging
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.captcha import (
    compute_escalated_maxnumber,
    create_altcha_challenge,
    verify_altcha_payload,
)
from app.auth.magic import consume_magic_link, create_magic_link
from app.auth.oauth import (
    OAUTH_STATE_MAX_AGE_SECONDS,
    SUPPORTED_OAUTH_PROVIDERS,
    OAuthProfile,
    build_oauth_authorize_url,
    compute_pkce_challenge_s256,
    create_oauth_state_cookie,
    exchange_oauth_code,
    generate_pkce_verifier,
    upsert_oauth_user,
    verify_oauth_state_cookie,
    verify_telegram_login_payload,
)
from app.auth.security import (
    OAUTH_STATE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SESSION_TTL_DAYS,
    SlidingWindowRateLimiter,
    clear_cookie,
    create_session,
    delete_session_by_token,
    get_client_ip,
    get_current_user_optional,
    set_cookie_secure,
    validate_display_name,
    validate_next_url,
    verify_unsubscribe_token,
)
from app.config import Settings, get_settings
from app.db.models import User
from app.db.session import get_db_session
from app.schemas import (
    AltchaChallengeDTO,
    AuthMeResponse,
    MagicLinkRequest,
    OkResponse,
    TelegramAuthPayload,
    UpdateMeRequest,
    UpdateMeResponse,
    UserDTO,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def serialize_user_dto(user: User) -> UserDTO:
    provider = "email"
    if user.identities:
        provider = user.identities[0].provider
    return UserDTO(
        id=user.id,
        display_name=user.display_name,
        role=user.role,
        avatar_url=user.avatar_url,
        provider=provider,
        email_verified=user.email_verified,
        notify_replies=user.notify_replies,
    )


def _resolve_telegram_bot_username(
    settings: Settings,
    bot_username_fallback: str | None = None,
) -> str | None:
    raw = (settings.TG_LOGIN_BOT_USERNAME or "").strip().lstrip("@")
    if raw:
        return raw
    fallback = (bot_username_fallback or "").strip().lstrip("@")
    return fallback or None


def get_enabled_providers(
    settings: Settings,
    *,
    bot_username_fallback: str | None = None,
) -> list[str]:
    providers: list[str] = []
    if settings.GITHUB_CLIENT_ID and settings.GITHUB_CLIENT_SECRET:
        providers.append("github")
    if settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET:
        providers.append("google")
    if (
        settings.OIDC_ISSUER_URL
        and settings.OIDC_CLIENT_ID
        and settings.OIDC_CLIENT_SECRET
    ):
        providers.append("oidc")
    if settings.TG_BOT_TOKEN and _resolve_telegram_bot_username(
        settings, bot_username_fallback
    ):
        providers.append("telegram")
    providers.append("email")
    return providers


def _get_rate_limiter(request: Request) -> SlidingWindowRateLimiter:
    limiter: SlidingWindowRateLimiter | None = getattr(
        request.app.state, "rate_limiter", None
    )
    if limiter is None:
        limiter = SlidingWindowRateLimiter()
        request.app.state.rate_limiter = limiter
    return limiter


@router.get("/captcha/challenge", response_model=AltchaChallengeDTO)
async def get_captcha_challenge(
    request: Request,
    settings: SettingsDep,
) -> AltchaChallengeDTO:
    limiter = _get_rate_limiter(request)
    ip = get_client_ip(request, settings)
    allowed, recent_hits = limiter.hit("captcha", ip, limit=60, window_seconds=60.0)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many CAPTCHA requests; please try again shortly",
        )
    maxnumber = compute_escalated_maxnumber(settings.ALTCHA_MAX_NUMBER, recent_hits)
    challenge = create_altcha_challenge(settings, maxnumber=maxnumber)
    return AltchaChallengeDTO(**challenge)


@router.get("/auth/me", response_model=AuthMeResponse)
async def get_auth_me(
    request: Request,
    db: DbSessionDep,
    settings: SettingsDep,
) -> AuthMeResponse:
    user = await get_current_user_optional(request, db)
    tg_service_username = getattr(
        getattr(request.app.state, "tg_service", None), "bot_username", None
    )
    resolved_tg_bot = _resolve_telegram_bot_username(settings, tg_service_username)
    providers = get_enabled_providers(
        settings, bot_username_fallback=tg_service_username
    )
    return AuthMeResponse(
        user=serialize_user_dto(user) if user is not None else None,
        providers=providers,
        allow_anon_comments=settings.ALLOW_ANON_COMMENTS,
        telegram_bot_username=(resolved_tg_bot if "telegram" in providers else None),
        oidc_provider_name=(
            (settings.OIDC_PROVIDER_NAME.strip() or "SSO")
            if "oidc" in providers
            else None
        ),
    )


@router.post("/auth/logout", response_model=OkResponse)
async def post_auth_logout(
    request: Request,
    response: Response,
    db: DbSessionDep,
    settings: SettingsDep,
) -> OkResponse:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    await delete_session_by_token(db, raw_token)
    await db.commit()
    clear_cookie(response, SESSION_COOKIE_NAME, settings)
    return OkResponse(ok=True)


@router.patch("/auth/me", response_model=UpdateMeResponse)
async def patch_auth_me(
    payload: UpdateMeRequest,
    request: Request,
    db: DbSessionDep,
    settings: SettingsDep,
) -> UpdateMeResponse:
    user = await get_current_user_optional(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    if payload.display_name is not None:
        cleaned = validate_display_name(
            payload.display_name,
            is_owner=(user.role == "owner"),
            settings=settings,
        )
        user.display_name = cleaned

    if payload.notify_replies is not None:
        user.notify_replies = payload.notify_replies

    await db.commit()
    return UpdateMeResponse(user=serialize_user_dto(user))


@router.get("/auth/oauth/{provider}/login")
async def oauth_login(
    provider: str,
    settings: SettingsDep,
    next: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    if provider not in SUPPORTED_OAUTH_PROVIDERS:
        raise HTTPException(status_code=404, detail="Unsupported OAuth provider")

    safe_next = validate_next_url(next, settings)
    code_verifier = generate_pkce_verifier() if provider == "oidc" else None
    code_challenge = (
        compute_pkce_challenge_s256(code_verifier)
        if code_verifier is not None
        else None
    )
    state, state_cookie = create_oauth_state_cookie(
        provider=provider,
        next_url=safe_next,
        secret_key=settings.SECRET_KEY,
        code_verifier=code_verifier,
    )
    authorize_url = await build_oauth_authorize_url(
        provider,
        state=state,
        settings=settings,
        code_challenge=code_challenge,
    )

    redirect = RedirectResponse(url=authorize_url, status_code=302)
    set_cookie_secure(
        redirect,
        OAUTH_STATE_COOKIE_NAME,
        state_cookie,
        settings,
        max_age=OAUTH_STATE_MAX_AGE_SECONDS,
    )
    return redirect


@router.get("/auth/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    db: DbSessionDep,
    settings: SettingsDep,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    if provider not in SUPPORTED_OAUTH_PROVIDERS:
        raise HTTPException(status_code=404, detail="Unsupported OAuth provider")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    state_cookie = request.cookies.get(OAUTH_STATE_COOKIE_NAME)
    state_data = verify_oauth_state_cookie(
        state_cookie,
        expected_provider=provider,
        received_state=state,
        secret_key=settings.SECRET_KEY,
    )
    safe_next = validate_next_url(state_data.next_url, settings)

    if state_data.code_verifier is not None:
        profile = await exchange_oauth_code(
            provider,
            code=code,
            settings=settings,
            code_verifier=state_data.code_verifier,
        )
    else:
        profile = await exchange_oauth_code(provider, code=code, settings=settings)

    user = await upsert_oauth_user(db, profile, settings=settings, is_owner=False)
    session_token = await create_session(db, user.id)
    await db.commit()

    redirect = RedirectResponse(url=safe_next, status_code=302)
    clear_cookie(redirect, OAUTH_STATE_COOKIE_NAME, settings)
    set_cookie_secure(
        redirect,
        SESSION_COOKIE_NAME,
        session_token,
        settings,
        max_age=SESSION_TTL_DAYS * 86400,
    )
    return redirect


@router.post("/auth/telegram/callback", response_model=UpdateMeResponse)
async def telegram_login_callback(
    payload: TelegramAuthPayload,
    response: Response,
    db: DbSessionDep,
    settings: SettingsDep,
) -> UpdateMeResponse:
    verify_telegram_login_payload(
        payload.model_dump(exclude_none=True),
        bot_token=settings.TG_BOT_TOKEN,
    )

    is_owner = settings.OWNER_TG_ID is not None and payload.id == settings.OWNER_TG_ID
    full_name = " ".join(
        part
        for part in (payload.first_name.strip(), (payload.last_name or "").strip())
        if part
    )
    profile = OAuthProfile(
        provider="telegram",
        provider_user_id=str(payload.id),
        display_name=full_name or payload.username or f"tg_{payload.id}",
        username=payload.username,
        email=None,
        email_verified=False,
        avatar_url=payload.photo_url,
    )
    user = await upsert_oauth_user(db, profile, settings=settings, is_owner=is_owner)
    session_token = await create_session(db, user.id)
    await db.commit()

    set_cookie_secure(
        response,
        SESSION_COOKIE_NAME,
        session_token,
        settings,
        max_age=SESSION_TTL_DAYS * 86400,
    )
    return UpdateMeResponse(user=serialize_user_dto(user))


@router.post("/auth/magic-link/request", response_model=OkResponse)
async def magic_link_request(
    payload: MagicLinkRequest,
    request: Request,
    db: DbSessionDep,
    settings: SettingsDep,
) -> OkResponse:
    limiter = _get_rate_limiter(request)
    ip = get_client_ip(request, settings)
    allowed, _ = limiter.hit("magic-link", ip, limit=5, window_seconds=300.0)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many sign-in requests; please try again later",
        )

    safe_next = (
        validate_next_url(payload.next, settings) if payload.next is not None else None
    )
    await verify_altcha_payload(db, payload.altcha_payload, settings)

    email, raw_token = await create_magic_link(
        db,
        email=payload.email,
        display_name=payload.display_name,
        settings=settings,
    )
    await db.commit()

    params: dict[str, str] = {"token": raw_token}
    if safe_next is not None:
        params["next"] = safe_next
    verify_url = f"{settings.api_base_url}/auth/magic-link/verify?{urlencode(params)}"

    mailer = getattr(request.app.state, "mailer", None)
    if mailer is not None:
        try:
            await mailer.send_magic_link_email(email, verify_url)
        except Exception as exc:
            logger.warning("Failed sending magic link email to %s: %s", email, exc)
            raise HTTPException(
                status_code=503,
                detail="Failed to send sign-in email; please try again later",
            ) from exc

    return OkResponse(ok=True)


@router.get("/auth/magic-link/verify")
async def magic_link_verify(
    db: DbSessionDep,
    settings: SettingsDep,
    token: Annotated[str, Query()],
    next: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    safe_next = validate_next_url(next, settings)
    user = await consume_magic_link(db, raw_token=token, settings=settings)
    session_token = await create_session(db, user.id)
    await db.commit()

    redirect = RedirectResponse(url=safe_next, status_code=302)
    set_cookie_secure(
        redirect,
        SESSION_COOKIE_NAME,
        session_token,
        settings,
        max_age=SESSION_TTL_DAYS * 86400,
    )
    return redirect


@router.api_route(
    "/auth/unsubscribe",
    methods=["GET", "POST"],
    response_class=HTMLResponse,
)
async def unsubscribe_replies(
    db: DbSessionDep,
    settings: SettingsDep,
    token: Annotated[str, Query()],
) -> HTMLResponse:
    """Handle both browser GET links and RFC 8058 One-Click POST unsubscribes."""
    user_id = verify_unsubscribe_token(token, settings.SECRET_KEY)
    if user_id is None:
        return HTMLResponse(
            content=(
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<title>Invalid Unsubscribe Link</title></head>"
                "<body><h1>Invalid unsubscribe link</h1>"
                "<p>This unsubscribe link is invalid or corrupted.</p></body></html>"
            ),
            status_code=400,
        )

    user = await db.get(User, user_id)
    if user is not None:
        user.notify_replies = False
        await db.commit()

    return HTMLResponse(
        content=(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Unsubscribed</title></head>"
            "<body><h1>Unsubscribed</h1>"
            "<p>You have been unsubscribed from reply email notifications.</p></body></html>"
        ),
        status_code=200,
    )
