from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import struct
import subprocess
import tempfile
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pillow_heif
import pillow_jxl  # noqa: F401
from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError

from app.media.fingerprint import compute_image_fingerprint

pillow_heif.register_heif_opener()

# Protect against decompression bombs (~80 MP max by default).
# Set once at module import time so concurrent asyncio.to_thread calls never mutate global state.
MAX_IMAGE_PIXELS = 80_000_000
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MiB
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

# Only allow raster formats Pillow can validate and round-trip cleanly
ALLOWED_IMAGE_FORMATS: frozenset[str] = frozenset(
    {"JPEG", "PNG", "WEBP", "TIFF", "MPO", "HEIF"}
)

FORMAT_TO_EXT: dict[str, str] = {
    "JPEG": ".jpg",
    "MPO": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "TIFF": ".tiff",
    "HEIF": ".heic",
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
_XMP_GPS_ATTR_RE = re.compile(
    rb'(?<=\s)[A-Za-z0-9_]+:GPS[A-Za-z0-9_]*\s*=\s*(?:"[^"]*"|\'[^\']*\')',
    re.IGNORECASE,
)
_XMP_GPS_ELEM_RE = re.compile(
    rb"<([A-Za-z0-9_]+:GPS[A-Za-z0-9_]*)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TIFF_TYPE_SIZES: dict[int, int] = {
    1: 1,   # BYTE
    2: 1,   # ASCII
    3: 2,   # SHORT
    4: 4,   # LONG
    5: 8,   # RATIONAL
    6: 1,   # SBYTE
    7: 1,   # UNDEFINED
    8: 2,   # SSHORT
    9: 4,   # SLONG
    10: 8,  # SRATIONAL
    11: 4,  # FLOAT
    12: 8,  # DOUBLE
    13: 4,  # IFD
}


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
    display_jxl_rel_url: str | None
    thumb_jxl_rel_url: str | None
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


def _scrub_xmp_gps_in_place(buf: bytearray, start: int, end: int) -> bool:
    """Replace XMP GPS attributes/elements with equal-length ASCII spaces to keep offsets intact."""
    if start < 0 or end > len(buf) or start >= end:
        return False
    segment = bytes(buf[start:end])
    changed = False
    for pattern in (_XMP_GPS_ELEM_RE, _XMP_GPS_ATTR_RE):
        for match in pattern.finditer(segment):
            m_start = start + match.start()
            m_end = start + match.end()
            buf[m_start:m_end] = b" " * (m_end - m_start)
            changed = True
    if changed:
        segment = bytes(buf[start:end])
    # Catch any leftover attribute/element naming variants if still matched
    return changed and not _XMP_GPS_RE.search(segment)


def _scrub_tiff_gps_in_place(
    buf: bytearray,
    tiff_start: int,
    tiff_end: int,
    *,
    scrub_xmp_tag: bool = False,
) -> bool:
    """Losslessly remove EXIF GPSInfo IFD (0x8825) inside a TIFF payload without changing its size."""
    if tiff_start < 0 or tiff_end > len(buf) or (tiff_end - tiff_start) < 8:
        return False

    order = bytes(buf[tiff_start : tiff_start + 2])
    endian = "<" if order == b"II" else (">" if order == b"MM" else "")
    if not endian:
        return False

    magic, ifd0_rel = struct.unpack_from(endian + "HI", buf, tiff_start + 2)
    if magic != 42:
        return False

    ifd0_abs = tiff_start + ifd0_rel
    if ifd0_abs < tiff_start + 8 or ifd0_abs + 2 > tiff_end:
        return False

    entry_count = struct.unpack_from(endian + "H", buf, ifd0_abs)[0]
    entries_start = ifd0_abs + 2
    entries_end = entries_start + entry_count * 12
    if entries_end + 4 > tiff_end:
        return False

    changed = False
    if scrub_xmp_tag:
        for i in range(entry_count):
            e_off = entries_start + i * 12
            tag, typ, cnt = struct.unpack_from(endian + "HHI", buf, e_off)
            if tag == 700 and typ in (1, 2, 7) and cnt > 4:
                xmp_rel = struct.unpack_from(endian + "I", buf, e_off + 8)[0]
                xmp_abs = tiff_start + xmp_rel
                if tiff_start <= xmp_abs and xmp_abs + cnt <= tiff_end:
                    if _scrub_xmp_gps_in_place(buf, xmp_abs, xmp_abs + cnt):
                        changed = True

    gps_idx: int | None = None
    gps_rel = 0
    for i in range(entry_count):
        e_off = entries_start + i * 12
        tag, typ, cnt = struct.unpack_from(endian + "HHI", buf, e_off)
        if tag == _GPS_IFD_TAG:
            gps_idx = i
            if typ in (4, 13) and cnt == 1:
                gps_rel = struct.unpack_from(endian + "I", buf, e_off + 8)[0]
            break

    if gps_idx is None:
        return changed

    if gps_rel:
        gps_abs = tiff_start + gps_rel
        if tiff_start + 8 <= gps_abs and gps_abs + 2 <= tiff_end:
            gps_count = struct.unpack_from(endian + "H", buf, gps_abs)[0]
            gps_entries_end = gps_abs + 2 + gps_count * 12
            if gps_entries_end <= tiff_end:
                for k in range(gps_count):
                    ge_off = gps_abs + 2 + k * 12
                    _, g_typ, g_cnt = struct.unpack_from(endian + "HHI", buf, ge_off)
                    byte_len = _TIFF_TYPE_SIZES.get(g_typ, 0) * g_cnt
                    if byte_len > 4:
                        v_rel = struct.unpack_from(endian + "I", buf, ge_off + 8)[0]
                        v_abs = tiff_start + v_rel
                        if tiff_start <= v_abs and v_abs + byte_len <= tiff_end:
                            buf[v_abs : v_abs + byte_len] = b"\x00" * byte_len
                zero_end = min(tiff_end, gps_entries_end + 4)
                buf[gps_abs:zero_end] = b"\x00" * (zero_end - gps_abs)

    # Shift trailing IFD0 entries + next_ifd_offset left by 12 bytes and decrement count
    struct.pack_into(endian + "H", buf, ifd0_abs, entry_count - 1)
    remove_off = entries_start + gps_idx * 12
    tail_end = entries_end + 4
    buf[remove_off : tail_end - 12] = buf[remove_off + 12 : tail_end]
    buf[tail_end - 12 : tail_end] = b"\x00" * 12
    return True


def _iter_isobmff_boxes(
    buf: bytes | bytearray,
    start: int,
    end: int,
) -> list[tuple[bytes, int, int]]:
    boxes: list[tuple[bytes, int, int]] = []
    pos = start
    while pos + 8 <= end:
        sz, typ = struct.unpack_from(">I4s", buf, pos)
        hdr = 8
        if sz == 1:
            if pos + 16 > end:
                break
            sz = struct.unpack_from(">Q", buf, pos + 8)[0]
            hdr = 16
        elif sz == 0:
            sz = end - pos
        if sz < hdr or pos + sz > end:
            break
        boxes.append((typ, pos + hdr, pos + sz))
        pos += sz
    return boxes


def _read_be_uint(buf: bytes | bytearray, offset: int, nbytes: int) -> int:
    val = 0
    for b in buf[offset : offset + nbytes]:
        val = (val << 8) | b
    return val


def _strip_jpeg_gps_losslessly(buf: bytearray) -> bool:
    if len(buf) < 4 or buf[:2] != b"\xff\xd8":
        return False
    pos = 2
    changed = False
    n = len(buf)
    while pos + 2 <= n:
        while pos < n and buf[pos] == 0xFF:
            pos += 1
        if pos >= n:
            break
        marker = buf[pos]
        pos += 1
        if marker in (0xD8, 0xD9, 0x01) or (0xD0 <= marker <= 0xD7):
            if marker == 0xD9:
                break
            continue
        if marker == 0xDA:  # Start of Scan (compressed bitstream starts here)
            break
        if pos + 2 > n:
            break
        seg_len = struct.unpack_from(">H", buf, pos)[0]
        if seg_len < 2 or pos + seg_len > n:
            break
        seg_start = pos + 2
        seg_end = pos + seg_len
        if marker == 0xE1:
            if buf[seg_start : seg_start + 6] == b"Exif\x00\x00":
                if _scrub_tiff_gps_in_place(buf, seg_start + 6, seg_end):
                    changed = True
            elif _XMP_GPS_RE.search(buf[seg_start:seg_end]):
                if _scrub_xmp_gps_in_place(buf, seg_start, seg_end):
                    changed = True
        pos = seg_end
    return changed


def _strip_heif_gps_losslessly(buf: bytearray) -> bool:
    top_boxes = _iter_isobmff_boxes(buf, 0, len(buf))
    meta_range: tuple[int, int] | None = None
    for typ, h_end, b_end in top_boxes:
        if typ == b"meta":
            meta_range = (h_end + 4, b_end)
            break
    if meta_range is None or meta_range[0] > meta_range[1]:
        return False

    item_types: dict[int, tuple[bytes, bytes]] = {}
    item_locs: dict[int, tuple[int, int, list[tuple[int, int]]]] = {}
    idat_payload_start: int | None = None

    for c_typ, c_start, c_end in _iter_isobmff_boxes(buf, meta_range[0], meta_range[1]):
        if c_typ == b"idat":
            idat_payload_start = c_start
        elif c_typ == b"iinf" and c_start + 4 <= c_end:
            iinf_ver = buf[c_start]
            cnt_off = c_start + 4
            if iinf_ver == 0:
                if cnt_off + 2 > c_end:
                    continue
                cnt = struct.unpack_from(">H", buf, cnt_off)[0]
                cursor = cnt_off + 2
            else:
                if cnt_off + 4 > c_end:
                    continue
                cnt = struct.unpack_from(">I", buf, cnt_off)[0]
                cursor = cnt_off + 4
            for b_typ, b_start, b_end2 in _iter_isobmff_boxes(buf, cursor, c_end)[:cnt]:
                if b_typ != b"infe" or b_start + 8 > b_end2:
                    continue
                infe_ver = buf[b_start]
                if infe_ver == 2 and b_start + 12 <= b_end2:
                    item_id, _ = struct.unpack_from(">HH", buf, b_start + 4)
                    itype = bytes(buf[b_start + 8 : b_start + 12])
                    rest = bytes(buf[b_start + 12 : b_end2])
                elif infe_ver == 3 and b_start + 14 <= b_end2:
                    item_id, _ = struct.unpack_from(">IH", buf, b_start + 4)
                    itype = bytes(buf[b_start + 8 : b_start + 12])
                    rest = bytes(buf[b_start + 14 : b_end2])
                else:
                    continue
                parts = rest.split(b"\x00", 2)
                ctype = parts[1] if len(parts) > 1 else b""
                item_types[item_id] = (itype, ctype)
        elif c_typ == b"iloc" and c_start + 6 <= c_end:
            iloc_ver = buf[c_start]
            b0 = buf[c_start + 4]
            b1 = buf[c_start + 5]
            off_sz = (b0 >> 4) & 0x0F
            len_sz = b0 & 0x0F
            base_sz = (b1 >> 4) & 0x0F
            idx_sz = (b1 & 0x0F) if iloc_ver in (1, 2) else 0
            p = c_start + 6
            if iloc_ver < 2:
                if p + 2 > c_end:
                    continue
                item_cnt = struct.unpack_from(">H", buf, p)[0]
                p += 2
            else:
                if p + 4 > c_end:
                    continue
                item_cnt = struct.unpack_from(">I", buf, p)[0]
                p += 4
            for _ in range(item_cnt):
                id_sz = 2 if iloc_ver < 2 else 4
                if p + id_sz > c_end:
                    break
                item_id = _read_be_uint(buf, p, id_sz)
                p += id_sz
                cm = 0
                if iloc_ver in (1, 2):
                    if p + 2 > c_end:
                        break
                    cm = struct.unpack_from(">H", buf, p)[0] & 0x0F
                    p += 2
                if p + 2 + base_sz + 2 > c_end:
                    break
                p += 2  # data_reference_index
                base_off = _read_be_uint(buf, p, base_sz)
                p += base_sz
                ext_cnt = struct.unpack_from(">H", buf, p)[0]
                p += 2
                extents: list[tuple[int, int]] = []
                for _ in range(ext_cnt):
                    need = idx_sz + off_sz + len_sz
                    if p + need > c_end:
                        p = c_end
                        break
                    p += idx_sz
                    e_off = _read_be_uint(buf, p, off_sz)
                    p += off_sz
                    e_len = _read_be_uint(buf, p, len_sz)
                    p += len_sz
                    extents.append((e_off, e_len))
                item_locs[item_id] = (cm, base_off, extents)

    changed = False
    for item_id, (itype, ctype) in item_types.items():
        loc = item_locs.get(item_id)
        if loc is None:
            continue
        cm, base_off, extents = loc
        if cm not in (0, 1):
            continue
        origin = idat_payload_start if cm == 1 else 0
        if origin is None:
            continue

        if itype == b"Exif" and len(extents) == 1:
            e_off, e_len = extents[0]
            start = origin + base_off + e_off
            end = start + e_len
            if start >= 0 and end <= len(buf) and (end - start) >= 12:
                tiff_hdr_off = struct.unpack_from(">I", buf, start)[0]
                tiff_start = start + 4 + tiff_hdr_off
                if _scrub_tiff_gps_in_place(buf, tiff_start, end):
                    changed = True
        elif itype == b"mime" and (
            b"rdf+xml" in ctype.lower() or b"xmp" in ctype.lower() or b"xml" in ctype.lower()
        ):
            for e_off, e_len in extents:
                start = origin + base_off + e_off
                end = start + e_len
                if start >= 0 and end <= len(buf):
                    if _scrub_xmp_gps_in_place(buf, start, end):
                        changed = True
    return changed


def _strip_webp_gps_losslessly(buf: bytearray) -> bool:
    if len(buf) < 12 or buf[:4] != b"RIFF" or buf[8:12] != b"WEBP":
        return False
    pos = 12
    n = len(buf)
    changed = False
    while pos + 8 <= n:
        fourcc = bytes(buf[pos : pos + 4])
        c_len = struct.unpack_from("<I", buf, pos + 4)[0]
        c_start = pos + 8
        c_end = c_start + c_len
        if c_end > n:
            break
        if fourcc == b"EXIF":
            t_start = c_start + 6 if buf[c_start : c_start + 6] == b"Exif\x00\x00" else c_start
            if _scrub_tiff_gps_in_place(buf, t_start, c_end):
                changed = True
        elif fourcc == b"XMP ":
            if _scrub_xmp_gps_in_place(buf, c_start, c_end):
                changed = True
        pos = c_end + (c_len & 1)
    return changed


def _strip_png_gps_losslessly(buf: bytearray) -> bool:
    if len(buf) < 8 or buf[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    pos = 8
    n = len(buf)
    changed = False
    while pos + 12 <= n:
        c_len = struct.unpack_from(">I", buf, pos)[0]
        c_type = bytes(buf[pos + 4 : pos + 8])
        c_start = pos + 8
        c_end = c_start + c_len
        if c_end + 4 > n:
            break
        chunk_changed = False
        if c_type == b"eXIf":
            chunk_changed = _scrub_tiff_gps_in_place(buf, c_start, c_end)
        elif c_type in (b"tEXt", b"iTXt") and _XMP_GPS_RE.search(buf[c_start:c_end]):
            chunk_changed = _scrub_xmp_gps_in_place(buf, c_start, c_end)
        if chunk_changed:
            crc = zlib.crc32(buf[pos + 4 : c_end]) & 0xFFFFFFFF
            struct.pack_into(">I", buf, c_end, crc)
            changed = True
        if c_type == b"IEND":
            break
        pos = c_end + 4
    return changed


def _strip_gps_losslessly(data: bytes, fmt: str) -> bytes | None:
    """Attempt container-level GPS EXIF/XMP removal without decoding or re-encoding pixels."""
    buf = bytearray(data)
    if fmt in ("JPEG", "MPO"):
        ok = _strip_jpeg_gps_losslessly(buf)
    elif fmt == "HEIF":
        ok = _strip_heif_gps_losslessly(buf)
    elif fmt == "WEBP":
        ok = _strip_webp_gps_losslessly(buf)
    elif fmt == "PNG":
        ok = _strip_png_gps_losslessly(buf)
    elif fmt == "TIFF":
        ok = _scrub_tiff_gps_in_place(buf, 0, len(buf), scrub_xmp_tag=True)
    else:
        return None

    if not ok:
        return None

    candidate = bytes(buf)
    try:
        with Image.open(io.BytesIO(candidate)) as check_im:
            if _GPS_IFD_TAG in check_im.getexif() or _has_xmp_gps(check_im, candidate):
                return None
    except Exception:
        return None
    return candidate


def strip_gps_from_image_bytes(
    data: bytes,
) -> tuple[bytes, bool, str]:
    """Strip EXIF GPSInfo IFD and XMP GPS metadata while preserving camera EXIF and ICC profile.

    Prefers container-level lossless metadata scrubbing so image bitstreams are never re-encoded.

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

        lossless = _strip_gps_losslessly(data, fmt)
        if lossless is not None:
            return lossless, True, out_ext

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
        if fmt == "HEIF":
            im.save(buf, "HEIF", quality=95, **save_kwargs)
            return buf.getvalue(), True, ".heic"

        raise InvalidImageError(f"Unsupported image format: {fmt}")


def _prepare_oriented_image(raw_im: Image.Image) -> tuple[Image.Image, bytes | None]:
    """Apply EXIF orientation and normalize mode for lossless/WebP derivative saving."""
    icc_profile = raw_im.info.get("icc_profile")
    oriented = ImageOps.exif_transpose(raw_im)
    if oriented.mode not in ("RGB", "RGBA"):
        oriented = oriented.convert(
            "RGBA" if ("A" in oriented.mode or oriented.mode == "P") else "RGB"
        )
    return oriented, icc_profile


def generate_preview_thumbnail(
    source_path: Path,
    preview_path: Path,
    *,
    max_size: tuple[int, int] = (480, 480),
    quality: int = 80,
) -> Path:
    """Render an EXIF-oriented WebP preview thumbnail atomically for confirmation UI."""
    with Image.open(source_path) as raw_im:
        oriented, icc_profile = _prepare_oriented_image(raw_im)
        preview_im = oriented.copy()
        preview_im.thumbnail(max_size, Image.Resampling.LANCZOS)
        extra_kwargs: dict[str, Any] = {}
        if icc_profile:
            extra_kwargs["icc_profile"] = icc_profile
        _atomic_save_pil_image(
            preview_im,
            preview_path,
            "WEBP",
            quality=quality,
            method=4,
            **extra_kwargs,
        )
    return preview_path


def _write_hdr_heif_png16(
    source_path: Path,
    target_size: tuple[int, int],
    png_path: Path,
) -> tuple[bool, str | None]:
    """Extract >8-bit HDR HEIF pixels cleanly to a 16-bit PNG via ffmpeg when available."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return False, None

    try:
        hf = pillow_heif.open_heif(source_path, convert_hdr_to_8bit=False)
        img0 = hf[0]
        bit_depth = int(img0.info.get("bit_depth", 8))
        if bit_depth <= 8 or img0.mode not in ("RGB;10", "RGB;12", "RGB;16", "RGBA;10", "RGBA;12", "RGBA;16", "RGB", "RGBA"):
            return False, None
        has_alpha = "A" in img0.mode
        channels = 4 if has_alpha else 3
        arr = np.frombuffer(img0.data, dtype=np.uint16)
        src_w, src_h = img0.size
        if arr.size < src_w * src_h * channels:
            return False, None
        shift = max(0, 16 - bit_depth)
        raw16 = (arr[: src_w * src_h * channels] << shift).astype("<u2", copy=False).tobytes()
        in_fmt = "rgba64le" if has_alpha else "rgb48le"
        out_fmt = "rgba64be" if has_alpha else "rgb48be"
        tw, th = target_size
        subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-f",
                "rawvideo",
                "-pix_fmt",
                in_fmt,
                "-s",
                f"{src_w}x{src_h}",
                "-i",
                "-",
                "-vf",
                f"scale={tw}:{th}:flags=lanczos",
                "-pix_fmt",
                out_fmt,
                str(png_path),
            ],
            input=raw16,
            check=True,
            capture_output=True,
        )
        color_space_arg: str | None = None
        if not img0.info.get("icc_profile"):
            tc = img0.info.get("transfer_characteristics")
            if tc == 16:
                color_space_arg = "color_space=RGB_D65_202_Rel_PeQ"
            elif tc == 18:
                color_space_arg = "color_space=RGB_D65_202_Rel_HLG"
        return True, color_space_arg
    except Exception:
        return False, None


def _save_progressive_jxl(
    *,
    source_path: Path | None,
    resized_im: Image.Image,
    dest_path: Path,
    quality: int,
    icc_profile: bytes | None,
    effort: int = 7,
) -> Path:
    """Encode a progressive JXL preserving wide-gamut ICC or >8-bit HDR when supported."""
    cjxl_bin = shutil.which("cjxl")
    if cjxl_bin:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=dest_path.parent, prefix=".jxl_") as td:
            td_path = Path(td)
            in_png = td_path / "input.png"
            out_jxl = td_path / "output.jxl"
            color_arg: str | None = None
            used_hdr = False
            if source_path is not None:
                used_hdr, color_arg = _write_hdr_heif_png16(
                    source_path, resized_im.size, in_png
                )
            if not used_hdr:
                png_kwargs: dict[str, Any] = {"compress_level": 1}
                if icc_profile:
                    png_kwargs["icc_profile"] = icc_profile
                resized_im.save(in_png, "PNG", **png_kwargs)

            cmd = [
                cjxl_bin,
                str(in_png),
                str(out_jxl),
                "-p",
                "-q",
                str(quality),
                "-e",
                str(effort),
                "-x",
                "strip=exif",
            ]
            if color_arg:
                cmd.extend(["-x", color_arg])
            subprocess.run(cmd, check=True, capture_output=True)
            tmp_target: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=dest_path.parent,
                    prefix=f".{dest_path.name}.",
                    suffix=".tmp.jxl",
                    delete=False,
                ) as tmp:
                    tmp_target = Path(tmp.name)
                    tmp.write(out_jxl.read_bytes())
                os.replace(tmp_target, dest_path)
                return dest_path
            except Exception:
                if tmp_target is not None:
                    tmp_target.unlink(missing_ok=True)
                raise

    _atomic_save_pil_image(
        resized_im,
        dest_path,
        "JXL",
        quality=quality,
        effort=effort,
        exif=b"",
    )
    return dest_path


def generate_display_and_thumb(
    source_path: Path,
    display_path: Path,
    thumb_path: Path,
    *,
    display_max_size: tuple[int, int] = (2048, 2048),
    thumb_max_size: tuple[int, int] = (1000, 1000),
    display_quality: int = 94,
    thumb_quality: int = 80,
    display_jxl_path: Path | None = None,
    thumb_jxl_path: Path | None = None,
    jxl_display_quality: int = 92,
    jxl_thumb_quality: int = 82,
) -> tuple[int, int, int]:
    """Generate crisp 2048px display and 1000px thumb WebPs (and optional progressive JXLs) atomically.

    Preserves ICC color profile if present.

    Returns:
        (display_width, display_height, display_size_bytes)
    """
    with Image.open(source_path) as raw_im:
        oriented, icc_profile = _prepare_oriented_image(raw_im)

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
        if display_jxl_path is not None:
            _save_progressive_jxl(
                source_path=source_path,
                resized_im=display_im,
                dest_path=display_jxl_path,
                quality=jxl_display_quality,
                icc_profile=icc_profile,
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
        if thumb_jxl_path is not None:
            _save_progressive_jxl(
                source_path=source_path,
                resized_im=thumb_im,
                dest_path=thumb_jxl_path,
                quality=jxl_thumb_quality,
                icc_profile=icc_profile,
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
    """Validate, optionally strip GPS EXIF/XMP, save original, and regenerate display + thumb WebPs & JXLs.

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
    display_jxl_rel = epoch_rel / "media" / f"{post_id}_{sort_order}_{short_hash}_display.jxl"
    thumb_jxl_rel = epoch_rel / "media" / f"{post_id}_{sort_order}_{short_hash}_thumb.jxl"

    orig_abs = media_root / orig_rel
    display_abs = media_root / display_rel
    thumb_abs = media_root / thumb_rel
    display_jxl_abs = media_root / display_jxl_rel
    thumb_jxl_abs = media_root / thumb_jxl_rel

    _atomic_write_bytes(orig_abs, final_original_bytes)

    display_w, display_h, display_size = generate_display_and_thumb(
        orig_abs,
        display_abs,
        thumb_abs,
        display_jxl_path=display_jxl_abs,
        thumb_jxl_path=thumb_jxl_abs,
    )

    phash, rgb_sig, _, _ = compute_image_fingerprint(orig_abs)

    return OriginalUpgradeArtifacts(
        original_rel_url=orig_rel.as_posix(),
        display_rel_url=display_rel.as_posix(),
        thumb_rel_url=thumb_rel.as_posix(),
        display_jxl_rel_url=display_jxl_rel.as_posix(),
        thumb_jxl_rel_url=thumb_jxl_rel.as_posix(),
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
_COMMENT_IMAGE_FORMATS: frozenset[str] = frozenset({"JPEG", "MPO", "PNG", "WEBP", "GIF", "HEIF"})
_COMMENT_FORMAT_META: dict[str, tuple[str, str]] = {
    "JPEG": (".jpg", "image/jpeg"),
    "MPO": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "WEBP": (".webp", "image/webp"),
    "GIF": (".gif", "image/gif"),
    "HEIF": (".webp", "image/webp"),
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
                elif fmt in ("WEBP", "HEIF") and oriented.mode not in ("RGB", "RGBA"):
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
