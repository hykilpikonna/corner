from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import logging
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageDraw, ImageOps
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import AvatarCustomEmoji, User

logger = logging.getLogger(__name__)

_MAX_AVATAR_BYTES = 5 * 1024 * 1024
_CUSTOM_EMOJI_DIM = 100
_MAX_STICKERS_PER_SET = 200
_AVATAR_FALLBACK_EMOJI = "👤"

# Matches tg-blog's ANON_RING_PALETTE in src/logic/anonAvatar.ts
_ANON_RING_PALETTE = (
    "#e05263",
    "#e87a3d",
    "#d99b26",
    "#65a32d",
    "#1ea861",
    "#14a39b",
    "#2094d8",
    "#4668d9",
    "#7b52d6",
    "#b44ac4",
    "#d94c9a",
    "#c85a32",
    "#2c8c99",
    "#5c7cfa",
    "#e0315a",
    "#38a169",
)

_PACK_MUTEX = asyncio.Lock()
_BOT_USERNAME_CACHE: dict[int, str] = {}
_ANON_ASSETS_DIR = Path(__file__).resolve().parent / "anons"


def _fnv1a_murmur32(text: str, seed: int) -> int:
    h = seed & 0xFFFFFFFF
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x01000193) & 0xFFFFFFFF
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return h & 0xFFFFFFFF


def _normalize_anon_display_name(seed: str | None) -> str:
    trimmed = (seed or "").strip()
    if not trimmed or trimmed == "Anonymous":
        return "Anon"
    if trimmed.startswith("Anonymous #"):
        return f"Anon #{trimmed[len('Anonymous #'):]}"
    return trimmed


def _pick_anon_identity(anon_display_name: str) -> tuple[str, Path | None, str]:
    """Match tg-blog's `pickAnonIdentity` palette & `/anons/anon-1..9.webp` selection."""
    normalized = _normalize_anon_display_name(anon_display_name)
    ring_idx = _fnv1a_murmur32(f"ring:{normalized}", 0x9E3779B1) % len(_ANON_RING_PALETTE)
    ring_color = _ANON_RING_PALETTE[ring_idx]
    files = (
        sorted(
            _ANON_ASSETS_DIR.glob("anon-*.webp"),
            key=lambda p: (0, int(p.stem.split("-", 1)[1]))
            if p.stem.split("-", 1)[1].isdigit()
            else (1, 0),
        )
        if _ANON_ASSETS_DIR.is_dir()
        else []
    )
    if not files:
        return ring_color, None, f"anon:{ring_color}"
    avatar_idx = _fnv1a_murmur32(f"avatar:{normalized}", 0x811C9DC5) % len(files)
    chosen = files[avatar_idx]
    return ring_color, chosen, f"anon2:{chosen.stem}:{ring_color}"


def _render_circular_avatar_png(raw_bytes: bytes) -> bytes:
    """Crop any valid raster image to a 100x100 circular PNG with transparent corners."""
    with Image.open(io.BytesIO(raw_bytes)) as img:
        img = ImageOps.exif_transpose(img).convert("RGBA")
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        cropped = img.crop((left, top, left + side, top + side))
        resized = cropped.resize((_CUSTOM_EMOJI_DIM, _CUSTOM_EMOJI_DIM), Image.Resampling.LANCZOS)

        # 4x supersampled alpha mask for smooth circular edges
        ss = _CUSTOM_EMOJI_DIM * 4
        mask = Image.new("L", (ss, ss), 0)
        draw = ImageDraw.Draw(mask)
        draw.ellipse((0, 0, ss - 1, ss - 1), fill=255)
        alpha = mask.resize((_CUSTOM_EMOJI_DIM, _CUSTOM_EMOJI_DIM), Image.Resampling.LANCZOS)

        r, g, b, orig_a = resized.split()
        combined_a = Image.composite(orig_a, Image.new("L", alpha.size, 0), alpha)
        out_img = Image.merge("RGBA", (r, g, b, combined_a))
        buf = io.BytesIO()
        out_img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


def _render_anon_badge_png(hex_color: str, avatar_path: Path | None = None) -> bytes:
    """Render a 100x100 circular anon animal avatar with its deterministic ring border."""
    ss = _CUSTOM_EMOJI_DIM * 4
    canvas = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    if avatar_path is not None and avatar_path.is_file():
        try:
            draw.ellipse((0, 0, ss - 1, ss - 1), fill=hex_color)
            inset = int(ss * 0.08)
            inner_size = ss - inset * 2
            with Image.open(avatar_path) as img:
                img = ImageOps.exif_transpose(img).convert("RGBA")
                w, h = img.size
                side = min(w, h)
                left = (w - side) // 2
                top = (h - side) // 2
                inner = img.crop((left, top, left + side, top + side)).resize(
                    (inner_size, inner_size), Image.Resampling.LANCZOS
                )
            inner_mask = Image.new("L", (inner_size, inner_size), 0)
            ImageDraw.Draw(inner_mask).ellipse((0, 0, inner_size - 1, inner_size - 1), fill=255)
            # Composite inner over neutral white/cream base inside ring so semi-transparent webps look crisp
            inner_bg = Image.new("RGBA", (inner_size, inner_size), (253, 249, 241, 255))
            inner_bg.paste(inner, (0, 0), inner)
            canvas.paste(inner_bg, (inset, inset), inner_mask)
            out_img = canvas.resize((_CUSTOM_EMOJI_DIM, _CUSTOM_EMOJI_DIM), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            out_img.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
        except Exception:
            logger.debug("Failed rendering anon avatar asset %s", avatar_path, exc_info=True)

    draw.ellipse((4, 4, ss - 5, ss - 5), fill=hex_color)
    draw.ellipse((ss * 0.34, ss * 0.24, ss * 0.66, ss * 0.56), fill=(255, 255, 255, 235))
    draw.pieslice(
        (ss * 0.22, ss * 0.54, ss * 0.78, ss * 0.96),
        start=180,
        end=360,
        fill=(255, 255, 255, 235),
    )
    out_img = canvas.resize((_CUSTOM_EMOJI_DIM, _CUSTOM_EMOJI_DIM), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    out_img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _is_public_hostname(host: str) -> bool:
    if not host:
        return False
    lowered = host.lower().strip("[]")
    if lowered in ("localhost", "localhost.localdomain"):
        return False
    try:
        ip_obj = ipaddress.ip_address(lowered)
        return ip_obj.is_global
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(lowered, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        addr = info[4][0].split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if not ip_obj.is_global:
            return False
    return True


async def _load_avatar_image_bytes(
    avatar_url: str,
    *,
    settings: Settings,
    http_client: httpx.AsyncClient | None = None,
) -> bytes | None:
    cleaned = (avatar_url or "").strip()
    if not cleaned:
        return None

    # Local media path (e.g. cached Telegram user avatar `/api/life/media/avatars/...`)
    parsed = urlsplit(cleaned)
    if not parsed.scheme and not parsed.netloc:
        rel = parsed.path.lstrip("/")
        prefix = settings.API_PREFIX.strip("/")
        for strip_prefix in (f"{prefix}/media/", "media/"):
            if strip_prefix and rel.startswith(strip_prefix):
                rel = rel[len(strip_prefix) :]
                break
        media_root = settings.media_dir.resolve()
        candidate: Path = (media_root / rel).resolve()
        if candidate.is_relative_to(media_root) and candidate.is_file():
            raw = candidate.read_bytes()
            return raw if len(raw) <= _MAX_AVATAR_BYTES else None
        return None

    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None
    allowed = await asyncio.to_thread(_is_public_hostname, parsed.hostname)
    if not allowed:
        return None

    try:
        if http_client is not None:
            resp = await http_client.get(cleaned)
        else:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                resp = await client.get(cleaned)
        resp.raise_for_status()
        content = resp.content
        if not content or len(content) > _MAX_AVATAR_BYTES:
            return None
        return content
    except Exception:
        logger.debug("Failed fetching avatar URL %s", cleaned, exc_info=True)
        return None


async def _resolve_bot_username(client: Any, settings: Settings) -> str | None:
    if settings.TG_LOGIN_BOT_USERNAME:
        return settings.TG_LOGIN_BOT_USERNAME.strip().lstrip("@")
    cached = _BOT_USERNAME_CACHE.get(id(client))
    if cached:
        return cached
    get_me = getattr(client, "get_me", None)
    if not callable(get_me):
        return None
    try:
        me = await get_me()
        uname = str(getattr(me, "username", "") or "").strip().lstrip("@")
        if uname:
            _BOT_USERNAME_CACHE[id(client)] = uname
            return uname
    except Exception:
        logger.debug("Could not resolve bot username for avatar emoji pack", exc_info=True)
    return None


def _build_avatar_pack_name(bot_username: str, index: int) -> str:
    suffix = f"_by_{bot_username}"
    prefix = f"web_avatars_{index}"
    max_prefix = 64 - len(suffix)
    if max_prefix <= 0:
        return f"a{index}{suffix}"[:64]
    return f"{prefix[:max_prefix]}{suffix}"


async def _upload_custom_emoji_to_rotating_pack(
    client: Any,
    *,
    owner_id: int,
    bot_username: str,
    png_bytes: bytes,
    fallback_emoji: str,
) -> tuple[str, str] | None:
    from pyrogram import enums, types

    buf = io.BytesIO(png_bytes)
    buf.name = "avatar.png"
    sticker_input = types.InputSticker(
        sticker=buf,
        format=enums.StickerFormat.STATIC,
        emoji_list=[fallback_emoji],
    )

    idx = 1
    while idx <= 50:
        pack_name = _build_avatar_pack_name(bot_username, idx)
        title = "Corner Web Avatars" if idx == 1 else f"Corner Web Avatars #{idx}"
        existing_set = None
        if hasattr(client, "get_sticker_set"):
            try:
                existing_set = await client.get_sticker_set(pack_name)
            except Exception:
                existing_set = None

        if existing_set is None:
            created_set = await client.create_new_sticker_set(
                user_id=owner_id,
                name=pack_name,
                title=title,
                stickers=[sticker_input],
                sticker_type=enums.StickerType.CUSTOM_EMOJI,
            )
            stickers = list(getattr(created_set, "stickers", None) or [])
            cid = getattr(stickers[-1], "custom_emoji_id", None) if stickers else None
            if cid is not None:
                return str(cid), pack_name
            return None

        stickers = list(getattr(existing_set, "stickers", None) or [])
        if len(stickers) >= _MAX_STICKERS_PER_SET:
            idx += 1
            continue

        updated_set = await client.add_sticker_to_set(
            user_id=owner_id,
            name=pack_name,
            sticker=sticker_input,
        )
        updated_stickers = list(getattr(updated_set, "stickers", None) or [])
        cid = getattr(updated_stickers[-1], "custom_emoji_id", None) if updated_stickers else None
        if cid is not None:
            return str(cid), pack_name
        return None
    return None


async def ensure_sender_avatar_custom_emoji(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    client: Any,
    user: User | None,
    anon_display_name: str | None,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[str, str] | None:
    """Return `(custom_emoji_id, fallback_emoji)` for a web commenter avatar, creating it once if needed."""
    if client is None or settings.OWNER_TG_ID is None:
        return None
    if not (
        hasattr(client, "create_new_sticker_set")
        and hasattr(client, "add_sticker_to_set")
    ):
        return None

    source_key: str | None = None
    avatar_url: str | None = None
    anon_color: str | None = None
    anon_avatar_path: Path | None = None
    if user is not None and user.avatar_url:
        avatar_url = user.avatar_url.strip()
        digest = hashlib.sha256(avatar_url.encode("utf-8")).hexdigest()[:40]
        source_key = f"url:{digest}"
    elif user is None and anon_display_name:
        anon_color, anon_avatar_path, source_key = _pick_anon_identity(anon_display_name)
    else:
        return None

    async with session_factory() as session:
        cached: AvatarCustomEmoji | None = await session.get(AvatarCustomEmoji, source_key)
        if cached is not None:
            return cached.custom_emoji_id, cached.fallback_emoji

    png_bytes: bytes | None = None
    if avatar_url is not None:
        raw_img = await _load_avatar_image_bytes(
            avatar_url,
            settings=settings,
            http_client=http_client,
        )
        if raw_img is None:
            return None
        try:
            png_bytes = await asyncio.to_thread(_render_circular_avatar_png, raw_img)
        except Exception:
            logger.debug("Failed rendering avatar PNG for %s", source_key, exc_info=True)
            return None
    elif anon_color is not None:
        png_bytes = await asyncio.to_thread(_render_anon_badge_png, anon_color, anon_avatar_path)

    if not png_bytes:
        return None

    bot_username = await _resolve_bot_username(client, settings)
    if not bot_username:
        return None

    async with _PACK_MUTEX:
        async with session_factory() as session:
            recheck = await session.get(AvatarCustomEmoji, source_key)
            if recheck is not None:
                return recheck.custom_emoji_id, recheck.fallback_emoji

        try:
            uploaded = await _upload_custom_emoji_to_rotating_pack(
                client,
                owner_id=settings.OWNER_TG_ID,
                bot_username=bot_username,
                png_bytes=png_bytes,
                fallback_emoji=_AVATAR_FALLBACK_EMOJI,
            )
        except Exception:
            logger.warning(
                "Failed uploading custom emoji avatar for %s",
                source_key,
                exc_info=True,
            )
            return None

        if uploaded is None:
            return None

        cid_str, pack_name = uploaded
        async with session_factory() as session:
            existing = await session.get(AvatarCustomEmoji, source_key)
            if existing is None:
                session.add(
                    AvatarCustomEmoji(
                        source_key=source_key,
                        custom_emoji_id=cid_str,
                        pack_name=pack_name,
                        fallback_emoji=_AVATAR_FALLBACK_EMOJI,
                    )
                )
                await session.commit()
        return cid_str, _AVATAR_FALLBACK_EMOJI
