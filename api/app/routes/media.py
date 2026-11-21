from __future__ import annotations

import asyncio
import mimetypes
import re
from pathlib import Path, PurePosixPath
from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.config import Settings, get_settings
from app.media.stickers import list_custom_emojis, list_sticker_packs
from app.schemas import CustomEmojisResponse, StickersResponse

router = APIRouter(tags=["media"])

SettingsDep = Annotated[Settings, Depends(get_settings)]

SAFE_INLINE_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/apng",
        "image/webp",
        "image/gif",
        "image/avif",
        "video/mp4",
        "video/webm",
        "audio/mpeg",
        "audio/ogg",
        "audio/mp4",
        "application/pdf",
    }
)

_EXT_INLINE_FALLBACK: dict[str, str] = {
    ".apng": "image/apng",
    ".webp": "image/webp",
    ".webm": "video/webm",
}

_RAW_PATH_SPLIT_RE = re.compile(r"/|%2[fF]")
_DISPOSITION_UNSAFE_RE = re.compile(r'[\r\n"\\;]+')


def _safe_attachment_filename(name: str) -> str:
    cleaned = _DISPOSITION_UNSAFE_RE.sub("_", name).strip()
    return cleaned or "download"


def _validate_raw_media_path(request: Request) -> None:
    """Inspect raw ASGI path segment-by-segment to catch dot-segments before normalization."""
    raw_path = request.scope.get("raw_path", b"").decode("latin-1", errors="ignore")
    raw_lower = raw_path.lower()
    if "\\" in raw_path or "%5c" in raw_lower or "%00" in raw_lower:
        raise HTTPException(status_code=400, detail="Invalid media path")

    for raw_seg in _RAW_PATH_SPLIT_RE.split(raw_path):
        once = unquote(raw_seg)
        twice = unquote(once)
        if once in (".", "..") or twice in (".", ".."):
            raise HTTPException(status_code=400, detail="Invalid media path")


def resolve_safe_media_path(media_root: Path, requested_path: str) -> Path:
    """Resolve requested_path strictly inside media_root, rejecting traversal or symlink escapes."""
    if not requested_path or "\x00" in requested_path or "\\" in requested_path:
        raise HTTPException(status_code=400, detail="Invalid media path")

    # Decode once more to catch double-encoded traversal attempts like %2e%2e
    decoded = unquote(requested_path)
    if "\x00" in decoded or "\\" in decoded or decoded.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid media path")

    parts = PurePosixPath(decoded).parts
    if not parts or any(part in ("..", ".", "") for part in parts):
        raise HTTPException(status_code=400, detail="Invalid media path")

    root_resolved = media_root.resolve()
    candidate = (root_resolved / Path(*parts)).resolve()

    if candidate == root_resolved or not candidate.is_relative_to(root_resolved):
        raise HTTPException(status_code=403, detail="Access denied")

    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")

    return candidate


def _build_safe_media_response(
    file_path: Path, *, allow_tgsticker: bool = False
) -> FileResponse:
    suffix = file_path.suffix.lower()
    guessed_mime = _EXT_INLINE_FALLBACK.get(suffix)
    if guessed_mime is None:
        guessed_mime, _ = mimetypes.guess_type(str(file_path))

    headers = {
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    }

    if allow_tgsticker and suffix == ".tgs":
        media_type = "application/x-tgsticker"
    elif guessed_mime in SAFE_INLINE_MIME_TYPES:
        media_type = guessed_mime
    else:
        media_type = "application/octet-stream"
        safe_name = _safe_attachment_filename(file_path.name)
        headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'

    return FileResponse(
        path=file_path,
        media_type=media_type,
        headers=headers,
    )


def _epoch_sort_key(entry: Path) -> tuple[int, int, str]:
    name = entry.name
    if name.isdigit():
        return (1, int(name), name)
    return (0, 0, name)


@router.get("/media/{path:path}")
async def serve_media(
    path: str,
    request: Request,
    settings: SettingsDep,
) -> FileResponse:
    _validate_raw_media_path(request)
    file_path = resolve_safe_media_path(settings.media_dir, path)
    rel_path = file_path.relative_to(settings.media_dir.resolve()).as_posix()
    return _build_safe_media_response(
        file_path, allow_tgsticker=rel_path.startswith(("stickers/", "comments/"))
    )


@router.get("/exports/emoji/{path:path}")
async def serve_export_emoji(
    path: str,
    request: Request,
    settings: SettingsDep,
) -> FileResponse:
    _validate_raw_media_path(request)

    candidate_roots: list[Path] = [settings.exports_dir / "emoji"]
    epochs_dir = settings.media_dir / "epochs"
    if epochs_dir.is_dir():
        sorted_epochs = sorted(
            (p for p in epochs_dir.iterdir() if p.is_dir()),
            key=_epoch_sort_key,
            reverse=True,
        )
        candidate_roots.extend(epoch / "emoji" for epoch in sorted_epochs)

    for root in candidate_roots:
        try:
            file_path = resolve_safe_media_path(root, path)
        except HTTPException as exc:
            if exc.status_code == 404:
                continue
            raise
        return _build_safe_media_response(file_path, allow_tgsticker=True)

    raise HTTPException(status_code=404, detail="Emoji file not found")


@router.get("/emojis/custom", response_model=CustomEmojisResponse, response_model_exclude_none=True)
async def get_custom_emojis(settings: SettingsDep) -> CustomEmojisResponse:
    emojis = await asyncio.to_thread(list_custom_emojis, settings)
    return CustomEmojisResponse(emojis=emojis)


@router.get("/stickers", response_model=StickersResponse)
async def get_stickers(settings: SettingsDep) -> StickersResponse:
    packs = await asyncio.to_thread(
        list_sticker_packs, settings.media_dir, settings.API_PREFIX
    )
    return StickersResponse(packs=packs)
