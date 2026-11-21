from __future__ import annotations

import base64
import logging
import mimetypes
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Comment, ModerationWhitelist

logger = logging.getLogger(__name__)

_OPENAI_MODERATIONS_URL = "https://api.openai.com/v1/moderations"
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_LINK_RE = re.compile(r"(?:https?://|tg://|t\.me/)", re.IGNORECASE)


@dataclass(slots=True)
class ModerationDecision:
    status: str  # 'approved' | 'pending'
    meta: dict[str, Any] | None = None


def normalize_moderation_text(raw: str) -> str:
    cleaned = _ZERO_WIDTH_RE.sub("", raw or "")
    return unicodedata.normalize("NFKC", cleaned).strip()


def resolve_sender_whitelist_identity(comment: Comment) -> tuple[str, str] | None:
    """Return `(kind, key)` for whitelisting a comment sender, if identifiable."""
    if comment.user_id is not None:
        return ("user", str(comment.user_id))
    if comment.actor_key_hash:
        return ("actor", str(comment.actor_key_hash))
    return None


async def is_sender_whitelisted(
    db: AsyncSession,
    *,
    user_id: int | None,
    actor_key_hash: str | None,
) -> bool:
    checks: list[tuple[str, str]] = []
    if user_id is not None:
        checks.append(("user", str(user_id)))
    if actor_key_hash:
        checks.append(("actor", actor_key_hash))
    if not checks:
        return False

    for kind, key in checks:
        row = (
            await db.execute(
                select(ModerationWhitelist.id).where(
                    ModerationWhitelist.kind == kind,
                    ModerationWhitelist.key == key,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            return True
    return False


async def upsert_moderation_whitelist(
    db: AsyncSession,
    *,
    kind: str,
    key: str,
    label: str | None,
    created_by_user_id: int | None,
) -> ModerationWhitelist:
    stmt = pg_insert(ModerationWhitelist).values(
        kind=kind,
        key=key,
        label=(label or "").strip()[:120] or None,
        created_by_user_id=created_by_user_id,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_moderation_whitelist_kind_key",
        set_={
            "label": stmt.excluded.label,
        },
    ).returning(ModerationWhitelist.id)
    wl_id = (await db.execute(stmt)).scalar_one()
    row = await db.get(ModerationWhitelist, int(wl_id))
    assert row is not None
    return row


def _resolve_comment_photo_path(settings: Settings, media: dict[str, Any] | None) -> Path | None:
    if not media or str(media.get("media_type") or "").lower() != "photo":
        return None
    raw_url = str(media.get("url") or "").strip().lstrip("/")
    if not raw_url:
        return None
    prefix = settings.API_PREFIX.strip("/")
    for strip_prefix in (f"{prefix}/media/", "media/"):
        if strip_prefix and raw_url.startswith(strip_prefix):
            raw_url = raw_url[len(strip_prefix) :]
            break
    media_root = settings.media_dir.resolve()
    candidate = (media_root / raw_url).resolve()
    if candidate.is_relative_to(media_root) and candidate.is_file():
        return candidate
    return None


async def _call_openai_moderation(
    *,
    api_key: str,
    model: str,
    text: str,
    photo_path: Path | None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    inputs: list[dict[str, Any]] = []
    if text:
        inputs.append({"type": "text", "text": text})
    if photo_path is not None:
        raw_bytes = photo_path.read_bytes()
        if len(raw_bytes) <= 20 * 1024 * 1024:
            mime, _ = mimetypes.guess_type(str(photo_path))
            if not mime or not mime.startswith("image/"):
                mime = "image/webp"
            b64 = base64.b64encode(raw_bytes).decode("ascii")
            inputs.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

    if not inputs:
        return {"flagged": False, "categories": {}, "category_scores": {}}

    payload: dict[str, Any] = {
        "model": model,
        "input": inputs,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    if http_client is not None:
        resp = await http_client.post(_OPENAI_MODERATIONS_URL, json=payload, headers=headers)
    else:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(_OPENAI_MODERATIONS_URL, json=payload, headers=headers)

    resp.raise_for_status()
    data = resp.json()
    results = data.get("results") or []
    return dict(results[0]) if results else {"flagged": False, "categories": {}, "category_scores": {}}


async def evaluate_comment_moderation(
    *,
    settings: Settings,
    body_text: str,
    media: dict[str, Any] | None,
    is_anonymous: bool,
    is_owner: bool,
    is_whitelisted: bool,
    http_client: httpx.AsyncClient | None = None,
) -> ModerationDecision:
    """Evaluate a web comment with OpenAI `omni-moderation-latest` and sender rules."""
    if is_owner or is_whitelisted:
        return ModerationDecision(status="approved")
    if not settings.MODERATION_ENABLED or not settings.OPENAI_API_KEY:
        return ModerationDecision(status="approved")

    normalized = normalize_moderation_text(body_text)
    photo_path = _resolve_comment_photo_path(settings, media)
    media_type = str(media.get("media_type") or "").lower() if media else ""

    # Stickers are curated server assets only; still run text moderation if body_text exists
    if not normalized and photo_path is None:
        return ModerationDecision(status="approved")

    scores: dict[str, float] = {}
    categories: dict[str, bool] = {}
    flagged = False
    top_category: str | None = None
    top_score = 0.0

    try:
        res = await _call_openai_moderation(
            api_key=settings.OPENAI_API_KEY,
            model=settings.OPENAI_MODERATION_MODEL,
            text=normalized,
            photo_path=photo_path,
            http_client=http_client,
        )
        flagged = bool(res.get("flagged"))
        raw_scores = res.get("category_scores") or {}
        scores = {str(k): round(float(v), 4) for k, v in raw_scores.items()}
        raw_cats = res.get("categories") or {}
        categories = {str(k): bool(v) for k, v in raw_cats.items() if v}
        if scores:
            top_category, top_score = max(scores.items(), key=lambda kv: kv[1])
    except Exception as exc:
        logger.warning("OpenAI moderation request failed; holding comment for review: %s", exc)
        return ModerationDecision(
            status="pending",
            meta={
                "reason": "moderation_unavailable",
                "error": type(exc).__name__,
            },
        )

    if flagged or top_score >= settings.MODERATION_SCORE_THRESHOLD:
        return ModerationDecision(
            status="pending",
            meta={
                "reason": "openai_flagged",
                "flagged": flagged,
                "top_category": top_category,
                "top_score": top_score,
                "categories": categories,
                "category_scores": {k: v for k, v in scores.items() if v >= 0.05},
            },
        )

    if is_anonymous and media_type == "photo" and settings.MODERATION_HOLD_ANON_PHOTOS:
        return ModerationDecision(
            status="pending",
            meta={
                "reason": "anonymous_photo",
                "top_category": top_category,
                "top_score": top_score,
                "category_scores": {k: v for k, v in scores.items() if v >= 0.05},
            },
        )

    if is_anonymous and normalized and _LINK_RE.search(normalized) and settings.MODERATION_HOLD_ANON_LINKS:
        return ModerationDecision(
            status="pending",
            meta={
                "reason": "anonymous_link",
                "top_category": top_category,
                "top_score": top_score,
                "category_scores": {k: v for k, v in scores.items() if v >= 0.05},
            },
        )

    return ModerationDecision(status="approved")
