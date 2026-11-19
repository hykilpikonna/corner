from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.security import (
    sanitize_external_display_name,
    sha256_hex,
    validate_display_name,
)
from app.config import Settings
from app.db.models import MagicLink, User, UserIdentity

MAGIC_LINK_TTL_MINUTES = 15

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+$")


def normalize_email(raw_email: str) -> str:
    cleaned = raw_email.strip().lower()
    if not cleaned or len(cleaned) > 254 or not _EMAIL_RE.match(cleaned):
        raise HTTPException(status_code=400, detail="Invalid email address")
    return cleaned


async def create_magic_link(
    db: AsyncSession,
    *,
    email: str,
    display_name: str | None,
    settings: Settings,
) -> tuple[str, str]:
    normalized_email = normalize_email(email)
    cleaned_name: str | None = None
    if display_name is not None and display_name.strip():
        cleaned_name = validate_display_name(
            display_name, is_owner=False, settings=settings
        )

    raw_token = secrets.token_urlsafe(32)
    token_hash = sha256_hex(raw_token)
    expires_at = datetime.now(UTC) + timedelta(minutes=MAGIC_LINK_TTL_MINUTES)

    db.add(
        MagicLink(
            token_hash=token_hash,
            email=normalized_email,
            display_name=cleaned_name,
            expires_at=expires_at,
        )
    )
    await db.flush()
    return normalized_email, raw_token


async def consume_magic_link(
    db: AsyncSession,
    *,
    raw_token: str,
    settings: Settings,
) -> User:
    if not raw_token or not raw_token.strip():
        raise HTTPException(status_code=400, detail="Missing magic link token")

    token_hash = sha256_hex(raw_token.strip())
    now = datetime.now(UTC)

    stmt = (
        select(MagicLink)
        .where(
            MagicLink.token_hash == token_hash,
            MagicLink.used_at.is_(None),
            MagicLink.expires_at > now,
        )
        .with_for_update()
    )
    link = (await db.execute(stmt)).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=400, detail="Invalid or expired magic link")

    link.used_at = now
    await db.flush()

    email = link.email
    user_stmt = (
        select(User)
        .options(selectinload(User.identities))
        .where(User.email == email)
    )
    user = (await db.execute(user_stmt)).scalar_one_or_none()

    if user is None:
        local_part = email.split("@", 1)[0]
        safe_name = sanitize_external_display_name(
            link.display_name or local_part,
            fallback="Reader",
            is_owner=False,
            settings=settings,
        )
        user = User(
            role="user",
            display_name=safe_name,
            email=email,
            email_verified=True,
            notify_replies=True,
        )
        try:
            async with db.begin_nested():
                db.add(user)
                await db.flush()
        except IntegrityError:
            user = (
                await db.execute(
                    select(User)
                    .options(selectinload(User.identities))
                    .where(User.email == email)
                )
            ).scalar_one()
            user.email_verified = True
            await db.flush()
    else:
        user.email_verified = True
        await db.flush()

    existing_ident = (
        await db.execute(
            select(UserIdentity).where(
                UserIdentity.provider == "email",
                UserIdentity.provider_user_id == email,
            )
        )
    ).scalar_one_or_none()
    if existing_ident is None:
        try:
            async with db.begin_nested():
                db.add(
                    UserIdentity(
                        provider="email",
                        provider_user_id=email,
                        user_id=user.id,
                        username=None,
                    )
                )
                await db.flush()
        except IntegrityError:
            pass

    reloaded = (
        await db.execute(
            select(User).options(selectinload(User.identities)).where(User.id == user.id)
        )
    ).scalar_one()
    return reloaded
