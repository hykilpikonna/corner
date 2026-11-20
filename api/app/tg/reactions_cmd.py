from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.security import (
    _EXTRA_EMOJI_CODEPOINTS,
    _is_regional_indicator,
    _is_skin_tone_modifier,
    validate_reaction_emoji,
)
from app.db.repo import (
    canonical_reaction_key,
    default_allowed_reactions,
    get_allowed_reactions,
    normalize_allowed_reactions,
    set_allowed_reactions,
)

logger = logging.getLogger(__name__)

_KEYCAP_PREFIX_RE = re.compile(r"[0-9#*]\ufe0f?\u20e3")
_AT_POS_RE = re.compile(r"^#?(\d+)$")
_CMD_HEAD_RE = re.compile(r"^/([a-zA-Z0-9_]+)(?:@[a-zA-Z0-9_]+)?(?:\s+(.*))?$", re.DOTALL)


def _is_tag_char(cp: int) -> bool:
    return 0xE0020 <= cp <= 0xE007F


def _is_base_emoji_char(ch: str) -> bool:
    cp = ord(ch)
    if cp < 128:
        return False
    return unicodedata.category(ch) in ("So", "Sk") or cp in _EXTRA_EMOJI_CODEPOINTS


def _consume_emoji_cluster(word: str, idx: int) -> tuple[str, int]:
    """Consume a single Unicode emoji grapheme starting at `idx` in `word`."""
    n = len(word)
    keycap = _KEYCAP_PREFIX_RE.match(word, idx)
    if keycap is not None:
        return keycap.group(0), keycap.end()

    ch = word[idx]
    cp = ord(ch)
    if _is_regional_indicator(cp):
        if idx + 1 < n and _is_regional_indicator(ord(word[idx + 1])):
            return word[idx : idx + 2], idx + 2
        raise ValueError(f"Invalid regional indicator in {word!r}")

    def _consume_segment(pos: int) -> int:
        if pos >= n or not _is_base_emoji_char(word[pos]):
            bad = word[pos:] if pos < n else word
            raise ValueError(f"Unsupported reaction token {bad!r}")
        pos += 1
        while pos < n:
            c = word[pos]
            oc = ord(c)
            if _is_skin_tone_modifier(oc) or oc in (0xFE0E, 0xFE0F) or _is_tag_char(oc):
                pos += 1
                continue
            break
        return pos

    end = _consume_segment(idx)
    while end < n and word[end] == "\u200d":
        end += 1
        end = _consume_segment(end)

    cluster = word[idx:end]
    try:
        validated = validate_reaction_emoji(cluster)
    except HTTPException as exc:
        raise ValueError(f"Invalid reaction emoji {cluster!r}") from exc
    return validated, end


def _parse_text_chunk(chunk: str) -> list[dict[str, str | None]]:
    items: list[dict[str, str | None]] = []
    for word in chunk.split():
        if not word:
            continue
        if word.startswith("c:") and word[2:].isascii() and word[2:].isdigit() and 1 <= len(word[2:]) <= 32:
            items.append({"emoji": None, "custom_emoji_id": word[2:]})
            continue
        if word.isascii() and word.isdigit() and 5 <= len(word) <= 32:
            items.append({"emoji": None, "custom_emoji_id": word})
            continue
        pos = 0
        while pos < len(word):
            emoji, pos = _consume_emoji_cluster(word, pos)
            items.append({"emoji": emoji, "custom_emoji_id": None})
    return items


def parse_reaction_tokens(
    raw_text: str,
    entities: Sequence[Any] | None = None,
    *,
    utf16_offset_base: int = 0,
) -> list[dict[str, str | None]]:
    """Parse standard emojis, custom emoji IDs (`c:<id>` / >=5 digits), and TG CUSTOM_EMOJI entities."""
    if not raw_text:
        return []
    if not entities:
        return _parse_text_chunk(raw_text)

    utf16 = raw_text.encode("utf-16-le")
    code_units = len(utf16) // 2

    def _slice(start_u16: int, end_u16: int) -> str:
        s = max(0, min(start_u16, code_units))
        e = max(s, min(end_u16, code_units))
        return utf16[s * 2 : e * 2].decode("utf-16-le", errors="replace")

    custom_spans: list[tuple[int, int, str]] = []
    for en in entities:
        etype = str(getattr(en, "type", "")).upper().split(".")[-1]
        cid = getattr(en, "custom_emoji_id", None)
        off = getattr(en, "offset", None)
        length = getattr(en, "length", None)
        if etype == "CUSTOM_EMOJI" and cid is not None and off is not None and length is not None:
            rel_start = int(off) - utf16_offset_base
            rel_end = rel_start + int(length)
            if rel_end <= 0 or rel_start >= code_units:
                continue
            cid_str = str(cid).strip()
            if cid_str.isascii() and cid_str.isdigit() and len(cid_str) <= 32:
                custom_spans.append((max(0, rel_start), min(code_units, rel_end), cid_str))

    custom_spans.sort(key=lambda t: t[0])
    out: list[dict[str, str | None]] = []
    cursor = 0
    for s, e, cid_str in custom_spans:
        if s < cursor:
            continue
        if s > cursor:
            out.extend(_parse_text_chunk(_slice(cursor, s)))
        out.append({"emoji": None, "custom_emoji_id": cid_str})
        cursor = e
    if cursor < code_units:
        out.extend(_parse_text_chunk(_slice(cursor, code_units)))
    return out


def format_reaction_item(item: dict[str, str | None]) -> str:
    if item.get("emoji"):
        return str(item["emoji"])
    if item.get("custom_emoji_id"):
        return f"c:{item['custom_emoji_id']}"
    return str(item.get("reaction_key") or "?")


def format_allowed_reactions_reply(
    items: Sequence[dict[str, str | None]],
    *,
    header: str = "Global allowed reactions (in display order):",
) -> str:
    lines = [f"{header} ({len(items)})"]
    for idx, item in enumerate(items, start=1):
        lines.append(f"{idx}. {format_reaction_item(item)}")
    lines.append("")
    lines.append(
        "Commands:\n"
        "/reactions set <emojis...>\n"
        "/reactions add <emojis...> [at <pos>]\n"
        "/reactions rm <emojis...|#pos>\n"
        "/reactions move <emoji|#pos> <pos>\n"
        "/reactions reset"
    )
    text = "\n".join(lines)
    if len(text) <= 4000:
        return text
    compact_items = " ".join(
        f"{idx}:{format_reaction_item(item)}" for idx, item in enumerate(items, start=1)
    )
    compact = f"{header} ({len(items)})\n{compact_items}\n\n" + lines[-1]
    return compact[:4000]


def _help_text() -> str:
    return (
        "Reaction allowlist commands:\n"
        "/reactions — list ordered allowed reactions\n"
        "/reactions set ❤️ 👍 🔥 — replace full ordered allowlist\n"
        "/reactions add 🫡 [at 1] — add (or move if `at <pos>` given)\n"
        "/reactions rm 💩 [#3] — remove emoji(s) or #index\n"
        "/reactions move 🔥 1 — move emoji or #index to position\n"
        "/reactions reset — restore default Telegram list\n\n"
        "You can send standard emojis, Telegram custom emojis, or `c:<id>` tokens."
    )


def _parse_pos_token(tok: str, upper_bound: int) -> int | None:
    m = _AT_POS_RE.fullmatch(tok.strip())
    if m is None:
        return None
    pos = int(m.group(1))
    return max(1, min(upper_bound, pos))


def _apply_remove_tokens(
    current: list[dict[str, str | None]],
    args_text: str,
    entities: Sequence[Any] | None,
    arg_utf16_offset: int,
) -> tuple[list[dict[str, str | None]], int]:
    """Remove items matching `#<1-based-index>` or parsed emojis/custom IDs."""
    words = args_text.split()
    remove_indices: set[int] = set()
    remaining_parts: list[str] = []
    for word in words:
        if word.startswith("#") and word[1:].isdigit():
            idx = int(word[1:]) - 1
            if 0 <= idx < len(current):
                remove_indices.add(idx)
            else:
                raise ValueError(f"Index {word} out of range (1..{len(current)}).")
        else:
            remaining_parts.append(word)

    remove_keys: set[str] = set()
    if remaining_parts:
        # Check whether entities exist; if so, parse with exact UTF-16 alignment on raw args_text
        if entities:
            parsed = parse_reaction_tokens(
                args_text, entities, utf16_offset_base=arg_utf16_offset
            ) if not remove_indices else None
        else:
            parsed = None

        if parsed is not None:
            for item in parsed:
                remove_keys.add(
                    canonical_reaction_key(
                        emoji=item.get("emoji"), custom_emoji_id=item.get("custom_emoji_id")
                    )
                )
        else:
            # Remove any #index substrings cleanly while preserving emoji offsets by spacing them out
            masked = re.sub(r"(?<!\S)#\d+(?!\S)", lambda m: " " * len(m.group(0)), args_text)
            for item in parse_reaction_tokens(
                masked, entities, utf16_offset_base=arg_utf16_offset
            ):
                remove_keys.add(
                    canonical_reaction_key(
                        emoji=item.get("emoji"), custom_emoji_id=item.get("custom_emoji_id")
                    )
                )

    if not remove_indices and not remove_keys:
        raise ValueError("Specify at least one emoji or #index to remove.")

    next_list: list[dict[str, str | None]] = []
    removed_count = 0
    for idx, item in enumerate(current):
        key = item.get("reaction_key") or canonical_reaction_key(
            emoji=item.get("emoji"), custom_emoji_id=item.get("custom_emoji_id")
        )
        if idx in remove_indices or key in remove_keys:
            removed_count += 1
        else:
            next_list.append(item)

    if not next_list:
        raise ValueError(
            "Cannot leave the allowlist empty via remove. Use /reactions set <emojis...> or /reactions reset."
        )
    return next_list, removed_count


async def execute_owner_reaction_command(
    raw_text: str,
    entities: Sequence[Any] | None,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, list[dict[str, str | None]] | None]:
    """Parse and execute an owner bot command. Returns `(reply_text, updated_allowed_list_or_None)`."""
    stripped = raw_text.strip()
    m = _CMD_HEAD_RE.match(stripped)
    if m is None:
        return "Unrecognized command. Send /reactions or /help.", None

    cmd = m.group(1).lower()
    args = (m.group(2) or "").strip()
    if cmd in ("start", "help"):
        return _help_text(), None
    if cmd not in ("reactions", "reaction", "emoji", "emojis"):
        return "Unknown command. Send /reactions or /help.", None

    if not args:
        subcmd = "list"
        rest = ""
    else:
        parts = args.split(None, 1)
        subcmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

    # Compute exact UTF-16 offset of `rest` in `raw_text` so Pyrogram CUSTOM_EMOJI entities align
    rest_utf16_offset = 0
    if rest:
        idx = raw_text.find(rest)
        if idx >= 0:
            rest_utf16_offset = len(raw_text[:idx].encode("utf-16-le")) // 2

    async with session_factory() as session:
        if subcmd in ("help", "?"):
            return _help_text(), None

        if subcmd == "list":
            current = await get_allowed_reactions(session)
            return format_allowed_reactions_reply(current), None

        if subcmd == "reset":
            defaults = default_allowed_reactions()
            updated = await set_allowed_reactions(session, defaults)
            await session.commit()
            return (
                format_allowed_reactions_reply(
                    updated, header="Reset allowed reactions to default:"
                ),
                updated,
            )

        if subcmd == "set":
            if not rest:
                return "Usage: /reactions set ❤️ 👍 🔥 ...", None
            try:
                parsed = parse_reaction_tokens(
                    rest, entities, utf16_offset_base=rest_utf16_offset
                )
            except ValueError as exc:
                return f"❌ {exc}", None
            normalized = normalize_allowed_reactions(parsed)
            if not normalized:
                return "❌ Provide at least one valid reaction emoji or custom emoji ID.", None
            updated = await set_allowed_reactions(session, normalized)
            await session.commit()
            return (
                format_allowed_reactions_reply(updated, header="Updated allowed reactions:"),
                updated,
            )

        if subcmd in ("add", "append", "insert"):
            if not rest:
                return "Usage: /reactions add ❤️ [at 1]", None
            current = list(await get_allowed_reactions(session))
            target_pos: int | None = None
            target_source = rest
            at_match = re.search(r"\s+at\s+#?(\d+)\s*$", rest, flags=re.IGNORECASE)
            if at_match:
                target_pos = max(1, int(at_match.group(1)))
                target_source = rest[: at_match.start()].rstrip()
            try:
                parsed = parse_reaction_tokens(
                    target_source, entities, utf16_offset_base=rest_utf16_offset
                )
            except ValueError as exc:
                return f"❌ {exc}", None
            to_add = normalize_allowed_reactions(parsed)
            if not to_add:
                return "❌ Provide at least one valid reaction emoji or custom emoji ID.", None

            if target_pos is None:
                existing_keys = {item["reaction_key"] for item in current}
                added = 0
                for item in to_add:
                    if item["reaction_key"] not in existing_keys:
                        current.append(item)
                        existing_keys.add(item["reaction_key"])
                        added += 1
                updated = await set_allowed_reactions(session, current)
                await session.commit()
                return (
                    format_allowed_reactions_reply(
                        updated,
                        header=f"Added {added} reaction(s) (skipped {len(to_add) - added} already present):",
                    ),
                    updated,
                )

            move_keys = {item["reaction_key"] for item in to_add}
            remaining = [item for item in current if item["reaction_key"] not in move_keys]
            insert_idx = max(0, min(len(remaining), target_pos - 1))
            next_list = remaining[:insert_idx] + list(to_add) + remaining[insert_idx:]
            updated = await set_allowed_reactions(session, next_list)
            await session.commit()
            return (
                format_allowed_reactions_reply(
                    updated, header=f"Placed {len(to_add)} reaction(s) at #{insert_idx + 1}:"
                ),
                updated,
            )

        if subcmd in ("rm", "remove", "del", "delete"):
            if not rest:
                return "Usage: /reactions rm <emojis...|#pos>", None
            current = list(await get_allowed_reactions(session))
            try:
                next_list, removed_count = _apply_remove_tokens(
                    current, rest, entities, rest_utf16_offset
                )
            except ValueError as exc:
                return f"❌ {exc}", None
            if removed_count == 0:
                return "No matching reactions were found in the current allowlist.", None
            updated = await set_allowed_reactions(session, next_list)
            await session.commit()
            return (
                format_allowed_reactions_reply(
                    updated, header=f"Removed {removed_count} reaction(s):"
                ),
                updated,
            )

        if subcmd in ("move", "mv"):
            mv_parts = rest.rsplit(None, 1)
            if len(mv_parts) != 2:
                return "Usage: /reactions move <emoji|#pos> <new_pos>", None
            target_tok, pos_tok = mv_parts[0].strip(), mv_parts[1].strip()
            current = list(await get_allowed_reactions(session))
            if not current:
                return "❌ Allowlist is empty.", None
            new_pos = _parse_pos_token(pos_tok, len(current))
            if new_pos is None:
                return "❌ Target position must be a positive integer.", None

            src_idx: int | None = None
            if target_tok.startswith("#") and target_tok[1:].isdigit():
                idx = int(target_tok[1:]) - 1
                if 0 <= idx < len(current):
                    src_idx = idx
            else:
                try:
                    parsed = parse_reaction_tokens(
                        target_tok, entities, utf16_offset_base=rest_utf16_offset
                    )
                except ValueError as exc:
                    return f"❌ {exc}", None
                if len(parsed) != 1:
                    return "❌ Specify exactly one emoji or #index to move.", None
                key = canonical_reaction_key(
                    emoji=parsed[0].get("emoji"), custom_emoji_id=parsed[0].get("custom_emoji_id")
                )
                for i, item in enumerate(current):
                    if item.get("reaction_key") == key:
                        src_idx = i
                        break

            if src_idx is None:
                return "❌ Target reaction is not currently in the allowlist.", None

            item = current.pop(src_idx)
            insert_idx = max(0, min(len(current), new_pos - 1))
            current.insert(insert_idx, item)
            updated = await set_allowed_reactions(session, current)
            await session.commit()
            return (
                format_allowed_reactions_reply(
                    updated,
                    header=f"Moved {format_reaction_item(item)} to #{insert_idx + 1}:",
                ),
                updated,
            )

        # Shorthand `/reactions ❤️ 👍 🔥` -> treat as `list`? No, safer to show explicit subcommand hint
        return (
            f"Unknown subcommand {subcmd!r}. Try `/reactions list`, `/reactions set ...`, "
            "`/reactions add ...`, `/reactions rm ...`, `/reactions move ...`, or `/reactions reset`."
        ), None


async def cache_custom_emoji_stickers(
    client: Any,
    exports_dir: Path,
    custom_emoji_ids: Sequence[str],
) -> None:
    """Best-effort download of TG custom emoji assets into `exports_dir/emoji/{id}.{ext}`."""
    if client is None or not custom_emoji_ids:
        return
    get_fn = getattr(client, "get_custom_emoji_stickers", None)
    dl_fn = getattr(client, "download_media", None)
    if not callable(get_fn) or not callable(dl_fn):
        return

    emoji_dir = exports_dir / "emoji"
    emoji_dir.mkdir(parents=True, exist_ok=True)
    missing_ids: list[int] = []
    for raw_id in custom_emoji_ids:
        if not raw_id.isdigit():
            continue
        if any((emoji_dir / f"{raw_id}{ext}").is_file() for ext in (".webp", ".webm", ".tgs", ".png", ".apng", ".gif")):
            continue
        missing_ids.append(int(raw_id))

    if not missing_ids:
        return

    try:
        stickers = await get_fn(missing_ids)
    except Exception:
        logger.debug("Failed to fetch custom emoji stickers %s", missing_ids, exc_info=True)
        return

    for sticker in stickers or []:
        cid = getattr(sticker, "custom_emoji_id", None)
        if cid is None:
            continue
        cid_str = str(cid)
        is_animated = bool(getattr(sticker, "is_animated", False))
        is_video = bool(getattr(sticker, "is_video", False))
        ext = ".tgs" if is_animated else (".webm" if is_video else ".webp")
        dest = emoji_dir / f"{cid_str}{ext}"
        if dest.is_file():
            continue
        try:
            await dl_fn(sticker, file_name=str(dest))
        except Exception:
            logger.debug("Failed to download custom emoji %s", cid_str, exc_info=True)
