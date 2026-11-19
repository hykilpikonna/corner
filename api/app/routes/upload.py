from __future__ import annotations

import asyncio
import hashlib
import html
import inspect
import json
import logging
import os
import re
import secrets
import shutil
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import OriginalUploadToken, Post, PostImage
from app.db.repo import get_post_by_id, resolve_media_url, serialize_post
from app.db.session import get_db_session
from app.exports import generate_exports
from app.media.matcher import (
    CandidatePhoto,
    TargetPostImage,
    build_candidate_photo,
    match_originals_to_post_images,
)
from app.media.optimize import (
    MAX_UPLOAD_BYTES,
    InvalidImageError,
    process_original_upgrade,
    sanitize_original_filename,
    validate_image_file,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["upload"])

# Accepts both synchronous SSEBus.publish(event, data) and async callables
SSEPublishFn = Callable[[str, dict[str, Any]], Any]

DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_STAGING_TTL_HOURS = 24
MAX_BATCH_BYTES = 200 * 1024 * 1024  # 200 MiB total per upload batch
_STREAM_CHUNK_BYTES = 1024 * 1024  # 1 MiB streaming chunk

_UPLOAD_PAGE_CSP = (
    "default-src 'none'; "
    "img-src 'self' data:; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class ConfirmMatchItem(BaseModel):
    file_key: str = Field(..., min_length=1, max_length=128)
    image_id: int = Field(..., ge=1)


class UploadConfirmRequest(BaseModel):
    staging_id: str = Field(..., min_length=1, max_length=128)
    matches: list[ConfirmMatchItem] = Field(..., min_length=1)


def hash_upload_token(raw_token: str) -> str:
    """Hash a high-entropy raw upload token using SHA-256."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


async def create_upload_token(
    session: AsyncSession,
    post_id: int,
    dm_msg_id: int | None = None,
    ttl_hours: int = 72,
) -> tuple[str, OriginalUploadToken]:
    """Create a high-entropy one-time upload token for a post and persist its SHA-256 hash."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_upload_token(raw_token)
    expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)

    row = OriginalUploadToken(
        token_hash=token_hash,
        post_id=post_id,
        dm_msg_id=dm_msg_id,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return raw_token, row


def get_sse_publisher(request: Request) -> SSEPublishFn | None:
    """Resolve an optional sync or async SSE publish callable from FastAPI app state."""
    publish = getattr(request.app.state, "sse_publish", None)
    if callable(publish):
        return publish
    bus = getattr(request.app.state, "sse_bus", None)
    if bus is not None and callable(getattr(bus, "publish", None)):
        return bus.publish
    return None


SSEPublishDep = Annotated[SSEPublishFn | None, Depends(get_sse_publisher)]


async def invoke_sse_publish(
    sse_publish: SSEPublishFn | None,
    event: str,
    data: dict[str, Any],
) -> None:
    """Invoke either a synchronous (`SSEBus.publish`) or asynchronous SSE publisher safely."""
    if sse_publish is None:
        return
    try:
        res = sse_publish(event, data)
        if inspect.isawaitable(res):
            await res
    except Exception:
        logger.exception("Failed to publish SSE event %s", event)


def _staging_root(settings: Settings) -> Path:
    return settings.DATA_DIR / "staging"


def _resolve_staging_dir(settings: Settings, staging_id: str) -> Path:
    """Strictly validate staging_id as a UUID and return its directory inside DATA_DIR/staging."""
    if "/" in staging_id or "\\" in staging_id or ".." in staging_id:
        raise HTTPException(status_code=400, detail="Invalid staging_id")
    try:
        parsed = uuid.UUID(staging_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid staging_id") from exc

    canonical_id = str(parsed)
    root = _staging_root(settings).resolve()
    target = (root / canonical_id).resolve()
    if not target.is_relative_to(root) or target == root:
        raise HTTPException(status_code=400, detail="Invalid staging_id")
    return target


def _cleanup_stale_staging_dirs(settings: Settings) -> None:
    root = _staging_root(settings)
    if not root.is_dir():
        return
    cutoff = datetime.now(UTC).timestamp() - (_STAGING_TTL_HOURS * 3600)
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


async def _rmtree_async(path: Path) -> None:
    await asyncio.to_thread(shutil.rmtree, path, True)


def _write_chunk_sync(fh: Any, chunk: bytes) -> None:
    fh.write(chunk)


async def _stream_upload_to_file(
    upload: UploadFile,
    dest_path: Path,
    *,
    max_file_bytes: int,
    remaining_batch_bytes: int,
) -> int:
    """Stream an UploadFile to disk in chunks, enforcing per-file and total batch size caps."""
    total_written = 0
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "wb") as fh:
        while True:
            chunk = await upload.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            total_written += len(chunk)
            if total_written > max_file_bytes:
                raise InvalidImageError(
                    f"File '{upload.filename}' exceeds maximum allowed size ({max_file_bytes} bytes)"
                )
            if total_written > remaining_batch_bytes:
                raise InvalidImageError(
                    f"Upload batch exceeds maximum total size ({MAX_BATCH_BYTES} bytes)"
                )
            await asyncio.to_thread(_write_chunk_sync, fh, chunk)
    return total_written


async def _get_valid_token_and_post(
    session: AsyncSession,
    raw_token: str,
) -> tuple[OriginalUploadToken, Post]:
    if not raw_token or len(raw_token) > 256:
        raise HTTPException(status_code=404, detail="Upload link not found")

    token_hash = hash_upload_token(raw_token)
    token_row = await session.get(OriginalUploadToken, token_hash)
    if token_row is None:
        raise HTTPException(status_code=404, detail="Upload link not found")

    expires_at = token_row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=410, detail="Upload link has expired")

    post = await get_post_by_id(session, token_row.post_id, include_deleted=False)
    if post is None:
        raise HTTPException(status_code=404, detail="Target post not found")

    return token_row, post


def build_target_post_images(post: Post) -> list[TargetPostImage]:
    """Convert ORM Post.images into matcher TargetPostImage descriptors."""
    return [
        TargetPostImage(
            image_id=img.id,
            sort_order=img.sort_order,
            url=img.url,
            thumb_url=img.thumb_url,
            phash=img.phash,
            rgb_sig=img.rgb_sig,
            width=img.width,
            height=img.height,
        )
        for img in sorted(post.images, key=lambda item: item.sort_order)
    ]


async def apply_original_matches(
    session: AsyncSession,
    settings: Settings,
    post: Post,
    file_mapping: Sequence[tuple[Path, str, PostImage]],
    *,
    sse_publish: SSEPublishFn | None = None,
    strip_gps_exif: bool = True,
) -> list[dict[str, Any]]:
    """Apply matched original photos to PostImage rows, regenerate exports, and emit SSE."""
    applied: list[dict[str, Any]] = []

    for source_path, original_filename, target_img in file_mapping:
        artifacts = await asyncio.to_thread(
            process_original_upgrade,
            source_path=source_path,
            original_filename=original_filename,
            media_root=settings.media_dir,
            epoch_id=post.channel_id,
            post_id=post.id,
            sort_order=target_img.sort_order,
            strip_gps_exif=strip_gps_exif,
        )

        target_img.url = artifacts.display_rel_url
        target_img.thumb_url = artifacts.thumb_rel_url
        target_img.original_url = artifacts.original_rel_url
        target_img.original_size = artifacts.original_size
        target_img.original_name = artifacts.original_name
        target_img.size = artifacts.display_size
        target_img.width = artifacts.width
        target_img.height = artifacts.height
        target_img.phash = artifacts.phash
        target_img.rgb_sig = artifacts.rgb_sig

        applied.append(
            {
                "image_id": target_img.id,
                "sort_order": target_img.sort_order,
                "url": resolve_media_url(target_img.url, settings.API_PREFIX),
                "thumb_url": resolve_media_url(target_img.thumb_url, settings.API_PREFIX),
                "original_url": resolve_media_url(target_img.original_url, settings.API_PREFIX),
                "original_size": target_img.original_size,
                "original_name": target_img.original_name,
                "width": target_img.width,
                "height": target_img.height,
                "gps_stripped": artifacts.gps_stripped,
            }
        )

    post_id = post.id
    await session.flush()
    await session.commit()
    session.expire_all()

    # Reload cleanly for export & SSE serialization
    refreshed = await get_post_by_id(session, post_id, include_deleted=False)
    await generate_exports(session, settings)

    if sse_publish is not None and refreshed is not None:
        payload = serialize_post(refreshed, api_prefix=settings.API_PREFIX)
        await invoke_sse_publish(sse_publish, "post.updated", payload)

    return applied


def _plain_excerpt(text_html: str | None, max_len: int = 160) -> str:
    if not text_html:
        return ""
    stripped = _HTML_TAG_RE.sub(" ", text_html)
    stripped = html.unescape(stripped)
    normalized = " ".join(stripped.split())
    if len(normalized) > max_len:
        return normalized[: max_len - 3].rstrip() + "..."
    return normalized


def _safe_json_for_script(data: Any) -> str:
    """Serialize JSON safe for embedding inside <script type="application/json">."""
    raw = json.dumps(data, ensure_ascii=False)
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _render_upload_page(post: Post, token: str, settings: Settings) -> str:
    """Render a self-contained, XSS-safe drag-and-drop upload page."""
    prefix = settings.API_PREFIX.rstrip("/")
    encoded_token = url_quote(token, safe="")
    post_url = f"{prefix}/upload/{encoded_token}"
    confirm_url = f"{prefix}/upload/{encoded_token}/confirm"
    excerpt = _plain_excerpt(post.text_html)

    sorted_images = sorted(post.images, key=lambda img: img.sort_order)
    images_meta = [
        {
            "id": img.id,
            "sort_order": img.sort_order,
            "url": resolve_media_url(img.url, settings.API_PREFIX),
            "thumb": resolve_media_url(img.thumb_url or img.url, settings.API_PREFIX),
            "width": img.width,
            "height": img.height,
            "has_original": bool(img.original_url),
            "original_name": img.original_name,
        }
        for img in sorted_images
    ]

    thumbs_html_parts: list[str] = []
    for item in images_meta:
        thumb_src = html.escape(str(item["thumb"] or ""), quote=True)
        badge = (
            '<span class="badge ok">Original uploaded</span>'
            if item["has_original"]
            else '<span class="badge">Compressed</span>'
        )
        thumbs_html_parts.append(
            f'<div class="card" data-image-id="{item["id"]}">'
            f'<img src="{thumb_src}" alt="Photo #{item["sort_order"] + 1}" loading="lazy" />'
            f'<div class="meta">'
            f'<strong>Photo #{item["sort_order"] + 1}</strong>'
            f'<span>{item["width"]}&times;{item["height"]}</span>'
            f"{badge}"
            f"</div></div>"
        )
    thumbs_html = "\n".join(thumbs_html_parts) or "<p>This post has no photos.</p>"

    config_json = _safe_json_for_script(
        {
            "postId": post.id,
            "uploadUrl": post_url,
            "confirmUrl": confirm_url,
            "images": images_meta,
        }
    )

    escaped_title = html.escape(f"Upload Original Photos — Post #{post.id}")
    escaped_excerpt = html.escape(excerpt) if excerpt else "No text caption"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escaped_title}</title>
  <style>
    :root {{ color-scheme: light dark; font-family: system-ui, -apple-system, sans-serif; }}
    body {{ max-width: 860px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }}
    h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
    .excerpt {{ opacity: 0.8; margin-top: 0; margin-bottom: 1.5rem; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }}
    .card {{ border: 1px solid rgba(128,128,128,0.3); border-radius: 10px; overflow: hidden; display: flex; flex-direction: column; }}
    .card img {{ width: 100%; height: 150px; object-fit: cover; background: rgba(128,128,128,0.1); }}
    .meta {{ padding: 0.5rem 0.75rem; font-size: 0.85rem; display: flex; flex-direction: column; gap: 0.2rem; }}
    .badge {{ display: inline-block; padding: 0.1rem 0.45rem; border-radius: 999px; font-size: 0.75rem; background: rgba(128,128,128,0.2); width: fit-content; }}
    .badge.ok {{ background: rgba(22,163,74,0.2); color: #16a34a; }}
    .dropzone {{ border: 2px dashed rgba(128,128,128,0.5); border-radius: 12px; padding: 2.25rem 1rem; text-align: center; cursor: pointer; transition: border-color 0.15s, background 0.15s; }}
    .dropzone.dragover {{ border-color: #2563eb; background: rgba(37,99,235,0.08); }}
    .btn {{ background: #2563eb; color: #fff; border: 0; border-radius: 8px; padding: 0.6rem 1.2rem; font-size: 0.95rem; cursor: pointer; }}
    .btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
    .progress-wrap {{ margin-top: 1rem; display: none; }}
    progress {{ width: 100%; height: 12px; }}
    .status {{ margin-top: 1rem; padding: 0.75rem 1rem; border-radius: 8px; display: none; }}
    .status.ok {{ display: block; background: rgba(22,163,74,0.15); }}
    .status.err {{ display: block; background: rgba(220,38,38,0.15); }}
    .status.warn {{ display: block; background: rgba(217,119,6,0.15); }}
    .confirm-box {{ margin-top: 1.25rem; display: none; border: 1px solid rgba(128,128,128,0.3); border-radius: 10px; padding: 1rem; }}
    table {{ width: 100%; border-collapse: collapse; margin: 0.75rem 0; font-size: 0.9rem; }}
    th, td {{ text-align: left; padding: 0.5rem; border-bottom: 1px solid rgba(128,128,128,0.2); }}
    select {{ padding: 0.35rem 0.5rem; border-radius: 6px; }}
  </style>
</head>
<body>
  <h1>{escaped_title}</h1>
  <p class="excerpt">{escaped_excerpt}</p>

  <div class="grid" id="post-thumbs">
    {thumbs_html}
  </div>

  <div class="dropzone" id="dropzone" tabindex="0" role="button" aria-label="Drop original photos here">
    <p><strong>Drop original photos here</strong> or click to browse</p>
    <p style="font-size:0.85rem;opacity:0.75;">Supports JPEG, PNG, WebP, TIFF. GPS EXIF/XMP is stripped automatically.</p>
    <input type="file" id="file-input" multiple accept="image/*" style="display:none;" />
  </div>

  <div class="progress-wrap" id="progress-wrap">
    <progress id="progress-bar" value="0" max="100"></progress>
    <div id="progress-label" style="font-size:0.85rem;margin-top:0.25rem;">Uploading...</div>
  </div>

  <div class="status" id="status-box"></div>

  <div class="confirm-box" id="confirm-box">
    <h3>Confirm Photo Matching</h3>
    <p style="font-size:0.9rem;opacity:0.85;">Some uploaded photos looked similar (e.g. burst shots). Please verify each assignment before applying:</p>
    <table>
      <thead>
        <tr><th>Uploaded File</th><th>Match Score</th><th>Target Post Photo</th></tr>
      </thead>
      <tbody id="confirm-tbody"></tbody>
    </table>
    <button class="btn" id="confirm-btn" type="button">Apply Confirmed Mapping</button>
  </div>

  <script type="application/json" id="upload-config">{config_json}</script>
  <script>
    (function () {{
      const cfg = JSON.parse(document.getElementById("upload-config").textContent);
      const dropzone = document.getElementById("dropzone");
      const fileInput = document.getElementById("file-input");
      const progressWrap = document.getElementById("progress-wrap");
      const progressBar = document.getElementById("progress-bar");
      const progressLabel = document.getElementById("progress-label");
      const statusBox = document.getElementById("status-box");
      const confirmBox = document.getElementById("confirm-box");
      const confirmTbody = document.getElementById("confirm-tbody");
      const confirmBtn = document.getElementById("confirm-btn");

      let activeStagingId = null;

      function formatErrorDetail(data, fallback) {{
        if (!data || !data.detail) return fallback;
        if (typeof data.detail === "string") return data.detail;
        if (Array.isArray(data.detail)) {{
          return data.detail
            .map(function (item) {{
              if (typeof item === "string") return item;
              if (item && typeof item.msg === "string") return item.msg;
              return JSON.stringify(item);
            }})
            .join("; ");
        }}
        return String(data.detail);
      }}

      function setStatus(kind, message) {{
        statusBox.className = "status " + kind;
        statusBox.textContent = message;
      }}

      function uploadFiles(fileList) {{
        if (!fileList || fileList.length === 0) return;
        const form = new FormData();
        for (const f of fileList) {{
          form.append("files[]", f, f.name);
        }}
        confirmBox.style.display = "none";
        statusBox.className = "status";
        progressWrap.style.display = "block";
        progressBar.value = 0;
        progressLabel.textContent = "Uploading " + fileList.length + " file(s)...";

        const xhr = new XMLHttpRequest();
        xhr.open("POST", cfg.uploadUrl, true);
        xhr.upload.onprogress = function (evt) {{
          if (evt.lengthComputable) {{
            const pct = Math.round((evt.loaded / evt.total) * 100);
            progressBar.value = pct;
            progressLabel.textContent = "Uploading... " + pct + "%";
          }}
        }};
        xhr.onload = function () {{
          progressWrap.style.display = "none";
          let data = null;
          try {{ data = JSON.parse(xhr.responseText); }} catch (_) {{}}
          if (xhr.status >= 200 && xhr.status < 300 && data) {{
            if (data.status === "applied") {{
              setStatus("ok", "Applied " + data.matches.length + " original photo(s) successfully!");
            }} else if (data.status === "needs_confirmation") {{
              activeStagingId = data.staging_id;
              renderConfirmation(data.proposed || []);
              setStatus("warn", "Ambiguous match detected. Please review and confirm below.");
            }}
          }} else {{
            setStatus("err", formatErrorDetail(data, "Upload failed (HTTP " + xhr.status + ")"));
          }}
        }};
        xhr.onerror = function () {{
          progressWrap.style.display = "none";
          setStatus("err", "Network error during upload.");
        }};
        xhr.send(form);
      }}

      function renderConfirmation(proposed) {{
        confirmTbody.textContent = "";
        for (const item of proposed) {{
          const tr = document.createElement("tr");

          const tdFile = document.createElement("td");
          tdFile.textContent = item.filename || item.file_key;
          tr.appendChild(tdFile);

          const tdScore = document.createElement("td");
          const scoreText = "dist=" + item.distance + (item.margin !== null ? (", margin=" + item.margin) : "");
          tdScore.textContent = scoreText;
          tr.appendChild(tdScore);

          const tdSelect = document.createElement("td");
          const select = document.createElement("select");
          select.dataset.fileKey = item.file_key;
          for (const img of cfg.images) {{
            const opt = document.createElement("option");
            opt.value = String(img.id);
            opt.textContent = "Photo #" + (img.sort_order + 1) + " (" + img.width + "x" + img.height + ")";
            if (img.id === item.image_id) opt.selected = true;
            select.appendChild(opt);
          }}
          tdSelect.appendChild(select);
          tr.appendChild(tdSelect);
          confirmTbody.appendChild(tr);
        }}
        confirmBox.style.display = "block";
      }}

      confirmBtn.addEventListener("click", async function () {{
        if (!activeStagingId) return;
        const selects = confirmTbody.querySelectorAll("select");
        const matches = [];
        for (const sel of selects) {{
          matches.push({{ file_key: sel.dataset.fileKey, image_id: Number(sel.value) }});
        }}
        confirmBtn.disabled = true;
        try {{
          const resp = await fetch(cfg.confirmUrl, {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ staging_id: activeStagingId, matches }}),
          }});
          const data = await resp.json();
          if (resp.ok) {{
            confirmBox.style.display = "none";
            activeStagingId = null;
            setStatus("ok", "Confirmed and applied " + data.matches.length + " original photo(s)!");
          }} else {{
            setStatus("err", formatErrorDetail(data, "Confirmation failed (HTTP " + resp.status + ")"));
          }}
        }} catch (err) {{
          setStatus("err", "Network error during confirmation.");
        }} finally {{
          confirmBtn.disabled = false;
        }}
      }});

      dropzone.addEventListener("click", function () {{ fileInput.click(); }});
      dropzone.addEventListener("keydown", function (e) {{
        if (e.key === "Enter" || e.key === " ") {{ e.preventDefault(); fileInput.click(); }}
      }});
      fileInput.addEventListener("change", function () {{
        uploadFiles(fileInput.files);
        fileInput.value = "";
      }});
      dropzone.addEventListener("dragover", function (e) {{
        e.preventDefault();
        dropzone.classList.add("dragover");
      }});
      dropzone.addEventListener("dragleave", function () {{
        dropzone.classList.remove("dragover");
      }});
      dropzone.addEventListener("drop", function (e) {{
        e.preventDefault();
        dropzone.classList.remove("dragover");
        if (e.dataTransfer && e.dataTransfer.files) {{
          uploadFiles(e.dataTransfer.files);
        }}
      }});
    }})();
  </script>
</body>
</html>
"""


@router.get("/upload/{token}", response_class=HTMLResponse)
async def get_upload_page(
    token: str,
    session: DbSessionDep,
    settings: SettingsDep,
) -> HTMLResponse:
    _, post = await _get_valid_token_and_post(session, token)
    page_html = _render_upload_page(post, token, settings)
    return HTMLResponse(
        content=page_html,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": _UPLOAD_PAGE_CSP,
        },
    )


async def _extract_uploaded_files(request: Request) -> list[UploadFile]:
    form = await request.form()
    uploads: list[UploadFile] = []
    for key in ("files[]", "files", "file"):
        for item in form.getlist(key):
            if hasattr(item, "read") and hasattr(item, "filename"):
                uploads.append(item)  # type: ignore[arg-type]
    if not uploads:
        for _, item in form.multi_items():
            if hasattr(item, "read") and hasattr(item, "filename"):
                uploads.append(item)  # type: ignore[arg-type]
    return uploads


@router.post("/upload/{token}")
async def upload_original_photos(
    token: str,
    request: Request,
    session: DbSessionDep,
    settings: SettingsDep,
    sse_publish: SSEPublishDep,
) -> dict[str, Any]:
    token_row, post = await _get_valid_token_and_post(session, token)
    if not post.images:
        raise HTTPException(status_code=400, detail="Post has no images to upgrade")

    uploads = await _extract_uploaded_files(request)
    if not uploads:
        raise HTTPException(status_code=400, detail="No files uploaded")

    max_files = max(20, len(post.images) * 2)
    if len(uploads) > max_files:
        for up in uploads:
            await up.close()
        raise HTTPException(
            status_code=400,
            detail=f"Too many files uploaded (maximum {max_files} files per batch)",
        )

    await asyncio.to_thread(_cleanup_stale_staging_dirs, settings)
    staging_id = str(uuid.uuid4())
    staging_dir = _staging_root(settings) / staging_id
    staging_dir.mkdir(parents=True, exist_ok=True)

    candidates: list[CandidatePhoto] = []
    manifest_files: dict[str, dict[str, Any]] = {}
    batch_bytes = 0
    keep_staging = False

    try:
        for idx, up in enumerate(uploads):
            file_key = f"f_{idx}_{uuid.uuid4().hex[:8]}"
            raw_staged = staging_dir / f"{file_key}.upload"
            written = await _stream_upload_to_file(
                up,
                raw_staged,
                max_file_bytes=MAX_UPLOAD_BYTES,
                remaining_batch_bytes=MAX_BATCH_BYTES - batch_bytes,
            )
            batch_bytes += written

            info = await asyncio.to_thread(validate_image_file, raw_staged)
            safe_filename = sanitize_original_filename(up.filename, fallback_ext=info.extension)
            stored_name = f"{file_key}{info.extension}"
            staged_path = staging_dir / stored_name
            await asyncio.to_thread(os.replace, raw_staged, staged_path)

            cand = await asyncio.to_thread(
                build_candidate_photo,
                file_key,
                safe_filename,
                staged_path,
            )
            candidates.append(cand)
            manifest_files[file_key] = {
                "filename": safe_filename,
                "stored_name": stored_name,
                "size": written,
            }

        targets = build_target_post_images(post)
        match_result = await asyncio.to_thread(
            match_originals_to_post_images,
            candidates,
            targets,
            media_root=settings.media_dir,
        )

        post_id = post.id
        if match_result.status == "confident":
            image_by_id = {img.id: img for img in post.images}
            cand_by_key = {c.file_key: c for c in candidates}
            file_mapping: list[tuple[Path, str, PostImage]] = []
            for pm in match_result.matches:
                cand = cand_by_key[pm.file_key]
                target_img = image_by_id[pm.image_id]
                file_mapping.append((cand.path, cand.filename, target_img))

            applied = await apply_original_matches(
                session,
                settings,
                post,
                file_mapping,
                sse_publish=sse_publish,
            )

            score_by_img = {pm.image_id: pm.to_dict() for pm in match_result.matches}
            for item in applied:
                score = score_by_img.get(item["image_id"])
                if score:
                    item["distance"] = score["distance"]
                    item["margin"] = score["margin"]
                    item["file_key"] = score["file_key"]

            return {
                "status": "applied",
                "post_id": post_id,
                "matches": applied,
            }

        # Ambiguous match: persist staging manifest for explicit confirmation
        manifest = {
            "staging_id": staging_id,
            "post_id": post_id,
            "token_hash": token_row.token_hash,
            "created_at": datetime.now(UTC).isoformat(),
            "files": manifest_files,
            "proposed": [pm.to_dict() for pm in match_result.matches],
        }
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        await asyncio.to_thread((staging_dir / "manifest.json").write_bytes, manifest_bytes)
        keep_staging = True
        return {
            "status": "needs_confirmation",
            "post_id": post_id,
            "staging_id": staging_id,
            "proposed": [pm.to_dict() for pm in match_result.matches],
            "unmatched_files": match_result.unmatched_files,
        }

    except InvalidImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        for up in uploads:
            try:
                await up.close()
            except Exception:
                pass
        if not keep_staging:
            await _rmtree_async(staging_dir)


@router.post("/upload/{token}/confirm")
async def confirm_original_photos(
    token: str,
    body: UploadConfirmRequest,
    session: DbSessionDep,
    settings: SettingsDep,
    sse_publish: SSEPublishDep,
) -> dict[str, Any]:
    token_row, post = await _get_valid_token_and_post(session, token)
    staging_dir = _resolve_staging_dir(settings, body.staging_id)
    manifest_path = staging_dir / "manifest.json"
    processing_path = staging_dir / "manifest.processing.json"

    # Atomically claim the manifest to prevent concurrent TOCTOU double-submits
    try:
        await asyncio.to_thread(os.replace, manifest_path, processing_path)
    except FileNotFoundError as exc:
        if processing_path.is_file():
            raise HTTPException(
                status_code=409, detail="Staged upload is already being processed"
            ) from exc
        raise HTTPException(
            status_code=404, detail="Staged upload not found or already applied"
        ) from exc

    def _restore_manifest_sync() -> None:
        if processing_path.is_file() and not manifest_path.exists():
            try:
                os.replace(processing_path, manifest_path)
            except OSError:
                pass

    try:
        raw_manifest = await asyncio.to_thread(processing_path.read_text, "utf-8")
        manifest = json.loads(raw_manifest)
    except (OSError, json.JSONDecodeError) as exc:
        await asyncio.to_thread(_restore_manifest_sync)
        raise HTTPException(status_code=400, detail="Corrupted staging manifest") from exc

    if manifest.get("post_id") != post.id or manifest.get("token_hash") != token_row.token_hash:
        await asyncio.to_thread(_restore_manifest_sync)
        raise HTTPException(status_code=403, detail="Staging batch does not match upload token")

    manifest_files: dict[str, dict[str, Any]] = manifest.get("files") or {}
    image_by_id = {img.id: img for img in post.images}

    seen_files: set[str] = set()
    seen_images: set[int] = set()
    file_mapping: list[tuple[Path, str, PostImage]] = []

    try:
        for item in body.matches:
            if item.file_key in seen_files:
                raise HTTPException(status_code=400, detail=f"Duplicate file_key: {item.file_key}")
            if item.image_id in seen_images:
                raise HTTPException(status_code=400, detail=f"Duplicate image_id: {item.image_id}")
            seen_files.add(item.file_key)
            seen_images.add(item.image_id)

            finfo = manifest_files.get(item.file_key)
            if not finfo:
                raise HTTPException(status_code=400, detail=f"Unknown file_key: {item.file_key}")

            target_img = image_by_id.get(item.image_id)
            if target_img is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Image ID {item.image_id} does not belong to post {post.id}",
                )

            staged_file = (staging_dir / Path(str(finfo["stored_name"])).name).resolve()
            if not staged_file.is_relative_to(staging_dir.resolve()) or not staged_file.is_file():
                raise HTTPException(status_code=404, detail=f"Staged file missing: {item.file_key}")

            file_mapping.append(
                (staged_file, str(finfo.get("filename") or staged_file.name), target_img)
            )
    except HTTPException:
        await asyncio.to_thread(_restore_manifest_sync)
        raise

    post_id = post.id
    try:
        applied = await apply_original_matches(
            session,
            settings,
            post,
            file_mapping,
            sse_publish=sse_publish,
        )
    except InvalidImageError as exc:
        await _rmtree_async(staging_dir)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        # Transient DB/IO failure: restore manifest so user can retry safely
        await asyncio.to_thread(_restore_manifest_sync)
        raise

    await _rmtree_async(staging_dir)
    return {
        "status": "applied",
        "post_id": post_id,
        "matches": applied,
    }
