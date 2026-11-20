from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError

from app.media.fingerprint import compute_image_fingerprint

# Protect against decompression bombs (~80 MP max by default).
# Set once at module import time so concurrent asyncio.to_thread calls never mutate global state.
MAX_IMAGE_PIXELS = 80_000_000
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MiB
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

# Only allow raster formats Pillow can validate and round-trip cleanly
ALLOWED_IMAGE_FORMATS: frozenset[str] = frozenset({"JPEG", "PNG", "WEBP", "TIFF", "MPO"})

FORMAT_TO_EXT: dict[str, str] = {
    "JPEG": ".jpg",
    "MPO": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "TIFF": ".tiff",
}

# EXIF GPSInfo IFD tag (0x8825 = 34853) and Orientation tag (0x0112 = 274)
_GPS_IFD_TAG = int(ExifTags.IFD.GPSInfo)
_ORIENTATION_TAG = 274
_TRANSPOSE_ORIENTATIONS = frozenset({5, 6, 7, 8})

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SVG_PREFIX_RE = re.compile(rb"^\s*(?:<\?xml\b[^>]*>\s*)?<svg\b", re.IGNORECASE)
_XMP_GPS_RE = re.compile(
    rb"GPS(?:Latitude|Longitude|Altitude|Position|DestLatitude|DestLongitude|ImgDirection)",
    re.IGNORECASE,
)


class InvalidImageError(ValueError):
    """Raised when an uploaded file is not a valid supported raster image."""


@dataclass(slots=True)
class ValidatedImageInfo:
    format: str
    width: int
    height: int
    size: int
    extension: str


@dataclass(slots=True)
class OriginalUpgradeArtifacts:
    original_rel_url: str
    display_rel_url: str
    thumb_rel_url: str
    original_size: int
    display_size: int
    original_name: str
    width: int
    height: int
    phash: str
    rgb_sig: bytes
    gps_stripped: bool


def sanitize_original_filename(filename: str | None, fallback_ext: str = ".jpg") -> str:
    """Return a clean basename safe for display and metadata storage."""
    if not filename:
        return f"original{fallback_ext}"
    base = Path(filename).name.strip()
    base = _SAFE_FILENAME_RE.sub("_", base).strip("._")
    if not base:
        return f"original{fallback_ext}"
    if len(base) > 180:
        stem = Path(base).stem[:160]
        suffix = Path(base).suffix[:16]
        base = f"{stem}{suffix}"
    return base


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _atomic_save_pil_image(
    im: Image.Image,
    target: Path,
    fmt: str,
    **save_kwargs: Any,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=f".tmp{target.suffix}",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
        im.save(tmp_path, fmt, **save_kwargs)
        os.replace(tmp_path, target)
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _oriented_dimensions(im: Image.Image) -> tuple[int, int]:
    """Compute oriented (width, height) using EXIF Orientation tag without allocating a rotated copy."""
    w, h = im.size
    try:
        orientation = im.getexif().get(_ORIENTATION_TAG)
    except Exception:
        orientation = None
    if orientation in _TRANSPOSE_ORIENTATIONS:
        return h, w
    return w, h


def _has_xmp_gps(im: Image.Image, raw_data: bytes) -> bool:
    for key in ("xmp", "XML:com.adobe.xmp"):
        val = im.info.get(key)
        if isinstance(val, bytes) and _XMP_GPS_RE.search(val):
            return True
        if isinstance(val, str) and _XMP_GPS_RE.search(val.encode("utf-8", errors="ignore")):
            return True
    # Fallback raw-byte scan catches APP1 XMP segments in JPEG/TIFF even if not parsed into im.info
    return bool(_XMP_GPS_RE.search(raw_data))


def validate_image_bytes(
    data: bytes,
    *,
    max_bytes: int = MAX_UPLOAD_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> ValidatedImageInfo:
    """Validate that raw bytes represent a real, safe, round-trippable raster image."""
    if not data:
        raise InvalidImageError("Uploaded file is empty")
    if len(data) > max_bytes:
        raise InvalidImageError(f"Uploaded file exceeds size limit ({max_bytes} bytes)")

    head = data[:512]
    if _SVG_PREFIX_RE.search(head) or b"<svg" in head.lower():
        raise InvalidImageError("SVG files are not allowed")

    try:
        # Step 1: verify structural integrity & format
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").upper()
            if fmt not in ALLOWED_IMAGE_FORMATS:
                raise InvalidImageError(f"Unsupported image format: {fmt or 'unknown'}")
            w, h = img.size
            if w <= 0 or h <= 0:
                raise InvalidImageError("Image has invalid dimensions")
            if w * h > max_pixels:
                raise InvalidImageError("Image dimensions exceed maximum allowed pixel count")
            img.verify()

        # Step 2: re-open to verify pixel decoding and compute oriented dimensions without copying pixels
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").upper()
            w, h = img.size
            if w <= 0 or h <= 0:
                raise InvalidImageError("Image has invalid dimensions")
            if w * h > max_pixels:
                raise InvalidImageError("Image dimensions exceed maximum allowed pixel count")
            img.load()
            ow, oh = _oriented_dimensions(img)
            ext = FORMAT_TO_EXT[fmt]
            return ValidatedImageInfo(
                format=fmt,
                width=ow,
                height=oh,
                size=len(data),
                extension=ext,
            )
    except InvalidImageError:
        raise
    except Image.DecompressionBombError as exc:
        raise InvalidImageError("Image exceeds decompression bomb pixel limit") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise InvalidImageError(f"Invalid or corrupted image file: {exc}") from exc


def validate_image_file(
    image_path: Path,
    *,
    max_bytes: int = MAX_UPLOAD_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> ValidatedImageInfo:
    """Validate an image file on disk."""
    path = Path(image_path)
    if not path.is_file():
        raise InvalidImageError(f"Image file not found: {path}")
    file_size = path.stat().st_size
    if file_size > max_bytes:
        raise InvalidImageError(f"Uploaded file exceeds size limit ({max_bytes} bytes)")
    return validate_image_bytes(
        path.read_bytes(),
        max_bytes=max_bytes,
        max_pixels=max_pixels,
    )


def strip_gps_from_image_bytes(
    data: bytes,
) -> tuple[bytes, bool, str]:
    """Strip EXIF GPSInfo IFD and XMP GPS metadata while preserving camera EXIF and ICC profile.

    Returns:
        (output_bytes, gps_stripped, output_extension)
        If the image contains neither EXIF GPS nor XMP GPS metadata, returns the original
        bytes untouched (zero recompression loss).
    """
    with Image.open(io.BytesIO(data)) as im:
        fmt = (im.format or "").upper()
        if fmt not in ALLOWED_IMAGE_FORMATS:
            raise InvalidImageError(f"Unsupported image format: {fmt or 'unknown'}")

        out_ext = FORMAT_TO_EXT[fmt]
        exif = im.getexif()
        has_exif_gps = _GPS_IFD_TAG in exif
        has_xmp_gps = _has_xmp_gps(im, data)

        if not has_exif_gps and not has_xmp_gps:
            return data, False, out_ext

        if has_exif_gps:
            del exif[_GPS_IFD_TAG]

        exif_bytes = exif.tobytes() if len(exif) > 0 else b""
        icc_profile = im.info.get("icc_profile")

        save_kwargs: dict[str, Any] = {}
        if exif_bytes:
            save_kwargs["exif"] = exif_bytes
        if icc_profile:
            save_kwargs["icc_profile"] = icc_profile

        buf = io.BytesIO()
        if fmt in ("JPEG", "MPO"):
            rgb = im.convert("RGB") if im.mode not in ("RGB", "L") else im
            rgb.save(buf, "JPEG", quality=95, subsampling=0, **save_kwargs)
            return buf.getvalue(), True, ".jpg"
        if fmt == "WEBP":
            im.save(buf, "WEBP", quality=95, method=6, **save_kwargs)
            return buf.getvalue(), True, ".webp"
        if fmt == "PNG":
            im.save(buf, "PNG", **save_kwargs)
            return buf.getvalue(), True, ".png"
        if fmt == "TIFF":
            im.save(buf, "TIFF", **save_kwargs)
            return buf.getvalue(), True, ".tiff"

        raise InvalidImageError(f"Unsupported image format: {fmt}")


def generate_display_and_thumb(
    source_path: Path,
    display_path: Path,
    thumb_path: Path,
    *,
    display_max_size: tuple[int, int] = (2048, 2048),
    thumb_max_size: tuple[int, int] = (1000, 1000),
    display_quality: int = 88,
    thumb_quality: int = 80,
) -> tuple[int, int, int]:
    """Generate crisp 2048px WebP display image and 1000px WebP thumbnail atomically.

    Preserves ICC color profile if present.

    Returns:
        (display_width, display_height, display_size_bytes)
    """
    with Image.open(source_path) as raw_im:
        icc_profile = raw_im.info.get("icc_profile")
        oriented = ImageOps.exif_transpose(raw_im)
        if oriented.mode not in ("RGB", "RGBA"):
            oriented = oriented.convert(
                "RGBA" if ("A" in oriented.mode or oriented.mode == "P") else "RGB"
            )

        extra_kwargs: dict[str, Any] = {}
        if icc_profile:
            extra_kwargs["icc_profile"] = icc_profile

        # Display image (max 2048px)
        display_im = oriented.copy()
        display_im.thumbnail(display_max_size, Image.Resampling.LANCZOS)
        display_w, display_h = display_im.size
        _atomic_save_pil_image(
            display_im,
            display_path,
            "WEBP",
            quality=display_quality,
            method=6,
            **extra_kwargs,
        )

        # Thumbnail image (max 1000px)
        thumb_im = display_im.copy()
        thumb_im.thumbnail(thumb_max_size, Image.Resampling.LANCZOS)
        _atomic_save_pil_image(
            thumb_im,
            thumb_path,
            "WEBP",
            quality=thumb_quality,
            method=6,
            **extra_kwargs,
        )

    return display_w, display_h, display_path.stat().st_size


def process_original_upgrade(
    *,
    source_path: Path,
    original_filename: str | None,
    media_root: Path,
    epoch_id: int,
    post_id: int,
    sort_order: int,
    strip_gps_exif: bool = True,
) -> OriginalUpgradeArtifacts:
    """Validate, optionally strip GPS EXIF/XMP, save original, and regenerate display + thumb WebPs.

    All files are written atomically inside `media_root/epochs/{epoch_id}/`.
    """
    raw_bytes = Path(source_path).read_bytes()
    info = validate_image_bytes(raw_bytes)

    gps_stripped = False
    final_original_bytes = raw_bytes
    ext = info.extension
    if strip_gps_exif:
        final_original_bytes, gps_stripped, ext = strip_gps_from_image_bytes(raw_bytes)

    short_hash = hashlib.sha256(final_original_bytes).hexdigest()[:12]
    safe_name = sanitize_original_filename(original_filename, fallback_ext=ext)

    epoch_rel = Path("epochs") / str(epoch_id)
    orig_rel = epoch_rel / "originals" / f"{post_id}_{sort_order}_{short_hash}{ext}"
    display_rel = epoch_rel / "media" / f"{post_id}_{sort_order}_{short_hash}_display.webp"
    thumb_rel = epoch_rel / "media" / f"{post_id}_{sort_order}_{short_hash}_thumb.webp"

    orig_abs = media_root / orig_rel
    display_abs = media_root / display_rel
    thumb_abs = media_root / thumb_rel

    _atomic_write_bytes(orig_abs, final_original_bytes)

    display_w, display_h, display_size = generate_display_and_thumb(
        orig_abs,
        display_abs,
        thumb_abs,
    )

    phash, rgb_sig, _, _ = compute_image_fingerprint(orig_abs)

    return OriginalUpgradeArtifacts(
        original_rel_url=orig_rel.as_posix(),
        display_rel_url=display_rel.as_posix(),
        thumb_rel_url=thumb_rel.as_posix(),
        original_size=len(final_original_bytes),
        display_size=display_size,
        original_name=safe_name,
        width=display_w,
        height=display_h,
        phash=phash,
        rgb_sig=rgb_sig,
        gps_stripped=gps_stripped,
    )


MAX_COMMENT_MEDIA_BYTES = 10 * 1024 * 1024  # 10 MiB
MAX_ANIMATED_FRAMES = 300
_COMMENT_IMAGE_FORMATS: frozenset[str] = frozenset({"JPEG", "MPO", "PNG", "WEBP", "GIF"})
_COMMENT_FORMAT_META: dict[str, tuple[str, str]] = {
    "JPEG": (".jpg", "image/jpeg"),
    "MPO": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "WEBP": (".webp", "image/webp"),
    "GIF": (".gif", "image/gif"),
}


def process_comment_image_upload(
    data: bytes,
    media_root: Path,
    *,
    max_bytes: int = MAX_COMMENT_MEDIA_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> dict[str, Any]:
    """Validate and normalize a web comment image upload (JPEG/PNG/WEBP/GIF) + WebP thumb."""
    if not data:
        raise InvalidImageError("Uploaded file is empty")
    if len(data) > max_bytes:
        raise InvalidImageError(f"Uploaded file exceeds size limit ({max_bytes} bytes)")

    head = data[:512]
    if _SVG_PREFIX_RE.search(head) or b"<svg" in head.lower():
        raise InvalidImageError("SVG files are not allowed")

    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").upper()
            if fmt not in _COMMENT_IMAGE_FORMATS:
                raise InvalidImageError(f"Unsupported image format: {fmt or 'unknown'}")
            w, h = img.size
            if w <= 0 or h <= 0:
                raise InvalidImageError("Image has invalid dimensions")
            if w * h > max_pixels:
                raise InvalidImageError("Image dimensions exceed maximum allowed pixel count")
            img.verify()

        uid = uuid.uuid4().hex[:16]
        ext, mime_type = _COMMENT_FORMAT_META[fmt]
        rel_url = f"comments/web_{uid}{ext}"
        rel_thumb = f"comments/web_{uid}_thumb.webp"
        abs_url = media_root / rel_url
        abs_thumb = media_root / rel_thumb

        with Image.open(io.BytesIO(data)) as img:
            is_animated = bool(getattr(img, "is_animated", False))
            if fmt == "GIF" or (fmt == "WEBP" and is_animated):
                n_frames = int(getattr(img, "n_frames", 1) or 1)
                w, h = img.size
                if n_frames > MAX_ANIMATED_FRAMES or (w * h * n_frames) > max_pixels:
                    raise InvalidImageError(
                        "Animated image exceeds maximum frame or pixel limit"
                    )
            img.load()

            if fmt == "GIF" or (fmt == "WEBP" and is_animated):
                final_w, final_h = img.size
                save_kwargs: dict[str, Any] = {"save_all": True} if is_animated else {}
                _atomic_save_pil_image(img, abs_url, fmt, **save_kwargs)

                img.seek(0)
                first_frame = img.convert("RGBA" if "A" in img.mode or img.mode == "P" else "RGB")
                first_frame.thumbnail((480, 480), Image.Resampling.LANCZOS)
                _atomic_save_pil_image(first_frame, abs_thumb, "WEBP", quality=80, method=6)
            else:
                oriented = ImageOps.exif_transpose(img)
                icc_profile = oriented.info.get("icc_profile")
                if fmt in ("JPEG", "MPO") and oriented.mode not in ("RGB", "L"):
                    oriented = oriented.convert("RGB")
                elif fmt == "WEBP" and oriented.mode not in ("RGB", "RGBA"):
                    oriented = oriented.convert(
                        "RGBA" if ("A" in oriented.mode or oriented.mode == "P") else "RGB"
                    )
                oriented.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                final_w, final_h = oriented.size

                save_kwargs = {}
                if icc_profile:
                    save_kwargs["icc_profile"] = icc_profile
                if fmt in ("JPEG", "MPO"):
                    _atomic_save_pil_image(
                        oriented, abs_url, "JPEG", quality=90, **save_kwargs
                    )
                elif fmt == "PNG":
                    _atomic_save_pil_image(oriented, abs_url, "PNG", **save_kwargs)
                else:
                    _atomic_save_pil_image(
                        oriented, abs_url, "WEBP", quality=90, method=6, **save_kwargs
                    )

                thumb_im = oriented.copy()
                if thumb_im.mode not in ("RGB", "RGBA"):
                    thumb_im = thumb_im.convert(
                        "RGBA" if ("A" in thumb_im.mode or thumb_im.mode == "P") else "RGB"
                    )
                thumb_im.thumbnail((480, 480), Image.Resampling.LANCZOS)
                _atomic_save_pil_image(thumb_im, abs_thumb, "WEBP", quality=80, method=6)

        return {
            "media_type": "photo",
            "url": rel_url,
            "thumb": rel_thumb,
            "width": final_w,
            "height": final_h,
            "size": abs_url.stat().st_size,
            "mime_type": mime_type,
        }
    except InvalidImageError:
        raise
    except Image.DecompressionBombError as exc:
        raise InvalidImageError("Image exceeds decompression bomb pixel limit") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise InvalidImageError(f"Invalid or corrupted image file: {exc}") from exc
