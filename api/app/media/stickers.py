from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.config import DEFAULT_STICKER_PACKS, Settings
from app.db.repo import resolve_media_url

EMOJI_EXTENSIONS: frozenset[str] = frozenset({".webp", ".webm", ".tgs", ".png", ".apng", ".gif"})
STICKER_EXTENSIONS: frozenset[str] = frozenset({".webp", ".tgs", ".webm"})
_SAFE_PACK_RE = re.compile(r"[^A-Za-z0-9_-]+")
_SAFE_FILE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_STICKER_SET_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_STICKER_SET_HOSTS = frozenset({"t.me", "www.t.me", "telegram.me", "www.telegram.me"})
_DISMISSED_DEFAULTS_FILENAME = ".dismissed_defaults.json"
_DEFAULT_PACK_ORDER: dict[str, int] = {
    name.lower(): idx for idx, name in enumerate(DEFAULT_STICKER_PACKS)
}


def _epoch_sort_key(entry: Path) -> tuple[int, int, str]:
    name = entry.name
    if name.isdigit():
        return (1, int(name), name)
    return (0, 0, name)


def list_custom_emojis(settings: Settings) -> list[dict[str, str]]:
    """Enumerate locally cached custom emoji assets across exports and epochs."""
    candidate_roots: list[Path] = [settings.exports_dir / "emoji"]
    epochs_dir = settings.media_dir / "epochs"
    if epochs_dir.is_dir():
        sorted_epochs = sorted(
            (p for p in epochs_dir.iterdir() if p.is_dir()),
            key=_epoch_sort_key,
            reverse=True,
        )
        candidate_roots.extend(epoch / "emoji" for epoch in sorted_epochs)

    prefix = settings.API_PREFIX.rstrip("/")
    seen: dict[str, dict[str, str]] = {}
    for root in candidate_roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            if not entry.is_file():
                continue
            ext = entry.suffix.lower()
            if ext not in EMOJI_EXTENSIONS:
                continue
            stem = entry.stem
            if not (stem.isascii() and stem.isdigit() and 1 <= len(stem) <= 32):
                continue
            if stem in seen:
                continue
            seen[stem] = {
                "id": stem,
                "url": f"{prefix}/exports/emoji/{entry.name}",
                "format": ext.lstrip("."),
            }

    return sorted(seen.values(), key=lambda item: int(item["id"]))


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
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
            tmp.write(raw)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _pack_sort_key(pack_dir: Path) -> tuple[int, int, str]:
    lower = pack_dir.name.lower()
    if lower in _DEFAULT_PACK_ORDER:
        return (0, _DEFAULT_PACK_ORDER[lower], lower)
    return (1, 0, lower)


def load_dismissed_default_packs(stickers_root: Path) -> set[str]:
    """Return lowercase pack names dismissed by the owner (`.dismissed_defaults.json`)."""
    path = stickers_root / _DISMISSED_DEFAULTS_FILENAME
    if not path.is_file():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    items = raw.get("dismissed") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return set()
    return {str(item).strip().lower() for item in items if str(item).strip()}


def mark_default_pack_dismissed(stickers_root: Path, pack_name: str) -> None:
    """Record a removed pack in `.dismissed_defaults.json` so startup sync does not re-pull it."""
    key = pack_name.strip().lower()
    if not key:
        return
    dismissed = load_dismissed_default_packs(stickers_root)
    if key in dismissed:
        return
    dismissed.add(key)
    stickers_root.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(
        stickers_root / _DISMISSED_DEFAULTS_FILENAME,
        {"dismissed": sorted(dismissed)},
    )


def clear_default_pack_dismissed(stickers_root: Path, pack_name: str) -> None:
    """Remove a pack from `.dismissed_defaults.json` when explicitly re-installed."""
    key = pack_name.strip().lower()
    path = stickers_root / _DISMISSED_DEFAULTS_FILENAME
    if not key or not path.is_file():
        return
    dismissed = load_dismissed_default_packs(stickers_root)
    if key not in dismissed:
        return
    dismissed.discard(key)
    _atomic_write_json(path, {"dismissed": sorted(dismissed)})


def _has_any_stickers(stickers_root: Path) -> bool:
    if not stickers_root.is_dir():
        return False
    for pack_dir in stickers_root.iterdir():
        if not pack_dir.is_dir():
            continue
        for entry in pack_dir.iterdir():
            if entry.is_file() and entry.suffix.lower() in STICKER_EXTENSIONS:
                return True
    return False


def _load_pack_manifest(pack_dir: Path) -> dict[str, Any]:
    manifest_path = pack_dir / "manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def list_sticker_packs(
    media_dir: Path,
    api_prefix: str = "/api/life",
    *,
    auto_seed: bool = False,
) -> list[dict[str, Any]]:
    """Scan `media_dir/stickers/<pack>/` and return ordered sticker packs."""
    del auto_seed
    stickers_root = media_dir / "stickers"
    if not stickers_root.is_dir():
        return []

    packs_out: list[dict[str, Any]] = []
    for pack_dir in sorted(
        (p for p in stickers_root.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=_pack_sort_key,
    ):
        pack_name = pack_dir.name
        manifest = _load_pack_manifest(pack_dir)
        title = str(manifest.get("title") or pack_name.replace("_", " ").title())

        meta_by_file: dict[str, dict[str, Any]] = {}
        ordered_files: list[str] = []
        for item in manifest.get("stickers") or []:
            if not isinstance(item, dict):
                continue
            fname = str(item.get("file") or "").strip()
            if not fname:
                continue
            meta_by_file[fname] = item
            ordered_files.append(fname)

        disk_files = {
            p.name: p
            for p in sorted(pack_dir.iterdir(), key=lambda p: p.name)
            if p.is_file() and p.suffix.lower() in STICKER_EXTENSIONS
        }
        if not disk_files:
            continue

        seen_files: set[str] = set()
        final_names: list[str] = []
        for fname in ordered_files:
            if fname in disk_files and fname not in seen_files:
                seen_files.add(fname)
                final_names.append(fname)
        for fname in sorted(disk_files):
            if fname not in seen_files:
                seen_files.add(fname)
                final_names.append(fname)

        stickers_out: list[dict[str, Any]] = []
        for fname in final_names:
            ext = Path(fname).suffix.lower()
            meta = meta_by_file.get(fname, {})
            is_animated = bool(meta.get("is_animated", ext == ".tgs"))
            is_video = bool(meta.get("is_video", ext == ".webm"))
            url = resolve_media_url(f"stickers/{pack_name}/{fname}", api_prefix)
            if not url:
                continue
            stickers_out.append(
                {
                    "id": f"{pack_name}/{fname}",
                    "file": fname,
                    "url": url,
                    "format": ext.lstrip("."),
                    "thumb": None,
                    "emoji": meta.get("emoji") or None,
                    "width": int(meta["width"]) if meta.get("width") is not None else 512,
                    "height": int(meta["height"]) if meta.get("height") is not None else 512,
                    "is_animated": is_animated,
                    "is_video": is_video,
                }
            )

        if stickers_out:
            packs_out.append(
                {
                    "id": pack_name,
                    "name": pack_name,
                    "title": title,
                    "stickers": stickers_out,
                }
            )

    return packs_out


def sanitize_sticker_pack_name(raw_name: str | None) -> str:
    cleaned = _SAFE_PACK_RE.sub("_", (raw_name or "").strip()).strip("_")
    return cleaned[:64] or "owner"


def sanitize_sticker_filename(raw_name: str) -> str:
    cleaned = _SAFE_FILE_RE.sub("_", raw_name.strip()).strip("._")
    return cleaned[:80] or "sticker.webp"


def parse_sticker_set_url(raw: str) -> str | None:
    """Extract and validate a Telegram sticker pack short name from an explicit sticker URL."""
    text = (raw or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return None

    lower = text.lower()
    if lower.startswith("tg://"):
        parsed = urlsplit(text)
        if parsed.scheme.lower() != "tg" or parsed.netloc.lower() != "addstickers":
            return None
        if parsed.path not in ("", "/"):
            return None
        sets = parse_qs(parsed.query, keep_blank_values=False).get("set") or []
        if len(sets) != 1:
            return None
        candidate = sets[0].strip()
        return candidate if _STICKER_SET_NAME_RE.fullmatch(candidate) else None

    if "://" in text:
        parsed = urlsplit(text)
        if parsed.scheme.lower() not in ("http", "https"):
            return None
        if parsed.netloc.lower() not in _STICKER_SET_HOSTS:
            return None
    elif lower.startswith(("t.me/", "www.t.me/", "telegram.me/", "www.telegram.me/")):
        parsed = urlsplit(f"https://{text}")
        if parsed.netloc.lower() not in _STICKER_SET_HOSTS:
            return None
    else:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0].lower() != "addstickers":
        return None
    candidate = parts[1].strip()
    return candidate if _STICKER_SET_NAME_RE.fullmatch(candidate) else None


def parse_sticker_set_ref(raw: str) -> str | None:
    """Parse a Telegram sticker set URL (`t.me/addstickers/...`, `tg://addstickers?set=...`) or short name."""
    text = (raw or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return None

    from_url = parse_sticker_set_url(text)
    if from_url is not None:
        return from_url

    if "://" in text or "/" in text or "\\" in text or "?" in text or "#" in text:
        return None

    return text if _STICKER_SET_NAME_RE.fullmatch(text) else None


def _normalize_manifest_entry(
    *,
    file_name: str,
    emoji: str | None = None,
    width: int | None = None,
    height: int | None = None,
    is_animated: bool = False,
    is_video: bool = False,
) -> dict[str, Any]:
    return {
        "file": file_name,
        "emoji": emoji or None,
        "width": int(width) if width is not None else 512,
        "height": int(height) if height is not None else 512,
        "is_animated": bool(is_animated),
        "is_video": bool(is_video),
    }


def save_sticker_pack_manifest(
    pack_dir: Path,
    *,
    pack_name: str,
    pack_title: str | None = None,
    entries: Sequence[dict[str, Any]],
) -> None:
    """Persist `manifest.json` for a sticker pack, storing title and merging existing entries."""
    manifest = _load_pack_manifest(pack_dir)
    title = str(
        (pack_title or "").strip()
        or manifest.get("title")
        or pack_name.replace("_", " ").title()
    )
    normalized_new: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    for raw in entries:
        fname = str(raw.get("file") or "").strip()
        if not fname or fname in seen_files:
            continue
        seen_files.add(fname)
        normalized_new.append(
            _normalize_manifest_entry(
                file_name=fname,
                emoji=raw.get("emoji"),
                width=raw.get("width"),
                height=raw.get("height"),
                is_animated=bool(raw.get("is_animated", False)),
                is_video=bool(raw.get("is_video", False)),
            )
        )

    preserved_existing = [
        item
        for item in (manifest.get("stickers") or [])
        if isinstance(item, dict)
        and item.get("file")
        and item.get("file") not in seen_files
        and (pack_dir / str(item["file"])).is_file()
    ]
    _atomic_write_json(
        pack_dir / "manifest.json",
        {
            "name": pack_name,
            "title": title,
            "stickers": normalized_new + preserved_existing,
        },
    )
    clear_default_pack_dismissed(pack_dir.parent, pack_name)


def upsert_sticker_manifest_entry(
    pack_dir: Path,
    *,
    pack_name: str,
    file_name: str,
    emoji: str | None = None,
    width: int | None = None,
    height: int | None = None,
    is_animated: bool = False,
    is_video: bool = False,
    pack_title: str | None = None,
) -> None:
    manifest = _load_pack_manifest(pack_dir)
    title = str(
        (pack_title or "").strip()
        or manifest.get("title")
        or pack_name.replace("_", " ").title()
    )
    existing = [
        item
        for item in (manifest.get("stickers") or [])
        if isinstance(item, dict) and item.get("file") != file_name
    ]
    existing.append(
        _normalize_manifest_entry(
            file_name=file_name,
            emoji=emoji,
            width=width,
            height=height,
            is_animated=is_animated,
            is_video=is_video,
        )
    )
    _atomic_write_json(
        pack_dir / "manifest.json",
        {
            "name": pack_name,
            "title": title,
            "stickers": existing,
        },
    )
    clear_default_pack_dismissed(pack_dir.parent, pack_name)


def remove_sticker_by_id(media_dir: Path, sticker_id: str) -> tuple[bool, str]:
    """Remove a sticker identified by `<pack>/<file>` from `media_dir/stickers`."""
    cleaned = sticker_id.strip().lstrip("/")
    if "/" not in cleaned or "\\" in cleaned or ".." in cleaned or "\x00" in cleaned:
        return False, "Usage: /stickers rm <pack/file>"

    pack_part, file_part = cleaned.split("/", 1)
    if not pack_part or not file_part or "/" in file_part:
        return False, "Usage: /stickers rm <pack/file>"

    stickers_root = (media_dir / "stickers").resolve()
    target = (stickers_root / pack_part / file_part).resolve()
    if not target.is_relative_to(stickers_root) or not target.is_file():
        return False, f"Sticker `{cleaned}` not found."

    if target.suffix.lower() not in STICKER_EXTENSIONS:
        return False, f"`{cleaned}` is not a sticker file."

    target.unlink(missing_ok=True)
    pack_dir = target.parent
    manifest = _load_pack_manifest(pack_dir)
    if manifest:
        updated_stickers = [
            item
            for item in (manifest.get("stickers") or [])
            if isinstance(item, dict) and item.get("file") != file_part
        ]
        _atomic_write_json(
            pack_dir / "manifest.json",
            {
                "name": manifest.get("name") or pack_part,
                "title": manifest.get("title") or pack_part.replace("_", " ").title(),
                "stickers": updated_stickers,
            },
        )

    if not _has_any_stickers(stickers_root):
        stickers_root.mkdir(parents=True, exist_ok=True)
        (stickers_root / ".no_auto_seed").touch()

    return True, f"🗑️ Removed sticker `{pack_part}/{file_part}`."


def remove_sticker_pack(media_dir: Path, pack_name: str) -> tuple[bool, str]:
    """Remove an entire sticker pack directory from `media_dir/stickers`."""
    cleaned = pack_name.strip().strip("/")
    if (
        not cleaned
        or "/" in cleaned
        or "\\" in cleaned
        or ".." in cleaned
        or "\x00" in cleaned
        or cleaned.startswith(".")
    ):
        return False, "Usage: /stickers rmpack <pack>"

    stickers_root = (media_dir / "stickers").resolve()
    target_dir = (stickers_root / cleaned).resolve()
    if (
        target_dir == stickers_root
        or not target_dir.is_relative_to(stickers_root)
        or not target_dir.is_dir()
    ):
        return False, f"Sticker pack `{cleaned}` not found."

    shutil.rmtree(target_dir)
    mark_default_pack_dismissed(stickers_root, cleaned)

    if not _has_any_stickers(stickers_root):
        stickers_root.mkdir(parents=True, exist_ok=True)
        (stickers_root / ".no_auto_seed").touch()

    return True, f"🗑️ Removed sticker pack `{cleaned}`."
