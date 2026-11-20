from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
import time
import unicodedata
from collections import deque
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import nh3
from fastapi import HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.db.models import Session, User

VISITOR_COOKIE_NAME = "life_vid"
SESSION_COOKIE_NAME = "life_session"
OAUTH_STATE_COOKIE_NAME = "life_oauth_state"

VISITOR_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year
SESSION_TTL_DAYS = 30

_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\u00ad]")
_MULTI_SPACE_RE = re.compile(r"\s+")
_BLANK_LINE_RE = re.compile(r"\r?\n\s*\r?\n+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# Common Cyrillic / Greek / Latin-lookalike confusables -> Latin ASCII
_CONFUSABLES_MAP: dict[str, str] = str.maketrans(
    {
        # Cyrillic lowercase & uppercase
        "\u0430": "a",  # а
        "\u0410": "a",  # А
        "\u0432": "b",  # в
        "\u0412": "b",  # В
        "\u0441": "c",  # с
        "\u0421": "c",  # С
        "\u0501": "d",  # ԁ
        "\u0435": "e",  # е
        "\u0415": "e",  # Е
        "\u0451": "e",  # ё
        "\u0401": "e",  # Ё
        "\u0261": "g",  # ɡ
        "\u04bb": "h",  # һ
        "\u041d": "h",  # Н
        "\u043d": "h",  # н
        "\u0456": "i",  # і
        "\u0406": "i",  # І
        "\u0457": "i",  # ї
        "\u0458": "j",  # ј
        "\u0408": "j",  # Ј
        "\u043a": "k",  # к
        "\u041a": "k",  # К
        "\u04cf": "l",  # ӏ
        "\u04c0": "l",  # Ӏ
        "\u043c": "m",  # м
        "\u041c": "m",  # М
        "\u043f": "n",  # п
        "\u043e": "o",  # о
        "\u041e": "o",  # О
        "\u0440": "p",  # р
        "\u0420": "p",  # Р
        "\u0433": "r",  # г
        "\u0455": "s",  # ѕ
        "\u0405": "s",  # Ѕ
        "\u0442": "t",  # т
        "\u0422": "t",  # Т
        "\u0443": "y",  # у
        "\u0423": "y",  # У
        "\u0445": "x",  # х
        "\u0425": "x",  # Х
        "\u0437": "3",  # з
        # Greek lowercase & uppercase
        "\u03b1": "a",  # α
        "\u0391": "a",  # Α
        "\u03b2": "b",  # β
        "\u0392": "b",  # Β
        "\u03b5": "e",  # ε
        "\u0395": "e",  # Ε
        "\u03b6": "z",  # ζ
        "\u0396": "z",  # Ζ
        "\u03b7": "n",  # η
        "\u0397": "h",  # Η
        "\u03b9": "i",  # ι
        "\u0399": "i",  # Ι
        "\u03ba": "k",  # κ
        "\u039a": "k",  # Κ
        "\u03bc": "m",  # μ
        "\u039c": "m",  # Μ
        "\u03bd": "v",  # ν
        "\u039d": "n",  # Ν
        "\u03bf": "o",  # ο
        "\u039f": "o",  # Ο
        "\u03c1": "p",  # ρ
        "\u03a1": "p",  # Ρ
        "\u03c4": "t",  # τ
        "\u03a4": "t",  # Τ
        "\u03c5": "u",  # υ
        "\u03a5": "y",  # Υ
        "\u03c7": "x",  # χ
        "\u03a7": "x",  # Χ
        "\u03c9": "w",  # ω
        # Dotless i / Turkish / common Latin lookalikes
        "\u0131": "i",  # ı
        "\u0237": "j",  # ȷ
    }
)

_COMMENT_ALLOWED_TAGS: set[str] = {
    "p",
    "br",
    "strong",
    "b",
    "em",
    "i",
    "u",
    "s",
    "del",
    "code",
    "pre",
    "blockquote",
    "a",
}
_COMMENT_ALLOWED_ATTRS: dict[str, set[str]] = {
    "a": {"href", "title"},
}

MAX_EMOJI_CODEPOINTS = 16
_KEYCAP_EMOJI_RE = re.compile(r"[0-9#*]\ufe0f?\u20e3")
# Standard Unicode emoji codepoints whose general category is not So/Sk
_EXTRA_EMOJI_CODEPOINTS: frozenset[int] = frozenset(
    {
        0x203C,  # ‼️ DOUBLE EXCLAMATION MARK
        0x2049,  # ⁉️ EXCLAMATION QUESTION MARK
        0x2139,  # ℹ️ INFORMATION SOURCE
        0x2194,  # ↔️ LEFT RIGHT ARROW
        0x25FB,  # ◻️ WHITE MEDIUM SQUARE
        0x25FC,  # ◼️ BLACK MEDIUM SQUARE
        0x25FD,  # ◽ WHITE MEDIUM SMALL SQUARE
        0x25FE,  # ◾ BLACK MEDIUM SMALL SQUARE
        0x2934,  # ⤴️ ARROW POINTING RIGHTWARDS THEN CURVING UPWARDS
        0x2935,  # ⤵️ ARROW POINTING RIGHTWARDS THEN CURVING DOWNWARDS
        0x3030,  # 〰️ WAVY DASH
        0x303D,  # 〽️ PART ALTERNATION MARK
    }
)


def _is_regional_indicator(cp: int) -> bool:
    return 0x1F1E6 <= cp <= 0x1F1FF


def _is_skin_tone_modifier(cp: int) -> bool:
    return 0x1F3FB <= cp <= 0x1F3FF


def validate_reaction_emoji(raw_emoji: str) -> str:
    """Validate that `raw_emoji` is a single Unicode emoji grapheme (no ASCII/multi-emoji spam)."""
    emoji = raw_emoji.strip()
    if not emoji or len(emoji) > MAX_EMOJI_CODEPOINTS:
        raise HTTPException(status_code=400, detail="Invalid reaction emoji")

    for ch in emoji:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cs", "Zs", "Zl", "Zp") or ch.isspace():
            raise HTTPException(status_code=400, detail="Invalid reaction emoji")

    # 1. Keycap sequence (e.g. 1️⃣, #️⃣, *️⃣)
    if "\u20e3" in emoji:
        if _KEYCAP_EMOJI_RE.fullmatch(emoji) is None:
            raise HTTPException(status_code=400, detail="Invalid reaction emoji")
        return emoji

    # 2. Regional Indicator flag pair (e.g. 🇺🇸)
    if any(_is_regional_indicator(ord(ch)) for ch in emoji):
        if len(emoji) != 2 or not all(_is_regional_indicator(ord(ch)) for ch in emoji):
            raise HTTPException(status_code=400, detail="Invalid reaction emoji")
        return emoji

    # 3. Single emoji or ZWJ emoji sequence:
    # Reject ASCII and require each ZWJ segment to have exactly 1 base emoji char.
    segments = emoji.split("\u200d")
    if any(not seg for seg in segments):
        raise HTTPException(status_code=400, detail="Invalid reaction emoji")

    for seg in segments:
        base_count = 0
        skin_tone_count = 0
        vs_count = 0
        for ch in seg:
            cp = ord(ch)
            if cp < 128:
                raise HTTPException(status_code=400, detail="Invalid reaction emoji")
            if _is_skin_tone_modifier(cp):
                skin_tone_count += 1
                continue
            if cp in (0xFE0E, 0xFE0F):
                vs_count += 1
                continue
            if 0xE0020 <= cp <= 0xE007F:
                continue
            cat = unicodedata.category(ch)
            if cat in ("So", "Sk") or cp in _EXTRA_EMOJI_CODEPOINTS:
                base_count += 1
            else:
                raise HTTPException(status_code=400, detail="Invalid reaction emoji")

        if base_count != 1 or skin_tone_count > 1 or vs_count > 1:
            raise HTTPException(status_code=400, detail="Invalid reaction emoji")

    return emoji


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hmac_sha256_hex(secret: str, message: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ---------------------------------------------------------------------------
# Cookies (HttpOnly, SameSite=Lax, Secure on HTTPS)
# ---------------------------------------------------------------------------


def set_cookie_secure(
    response: Response,
    key: str,
    value: str,
    settings: Settings,
    *,
    max_age: int | None = None,
    path: str = "/",
) -> None:
    response.set_cookie(
        key=key,
        value=value,
        max_age=max_age,
        path=path,
        httponly=True,
        samesite="lax",
        secure=settings.is_https,
    )


def clear_cookie(
    response: Response,
    key: str,
    settings: Settings,
    *,
    path: str = "/",
) -> None:
    response.delete_cookie(
        key=key,
        path=path,
        httponly=True,
        samesite="lax",
        secure=settings.is_https,
    )


def generate_visitor_id() -> str:
    return secrets.token_urlsafe(16)


def sign_visitor_id(vid: str, secret: str) -> str:
    sig = hmac_sha256_hex(secret, f"vid:{vid}")[:32]
    return f"{vid}.{sig}"


def verify_visitor_cookie(cookie_value: str | None, secret: str) -> str | None:
    if not cookie_value or "." not in cookie_value:
        return None
    vid, sig = cookie_value.rsplit(".", 1)
    if not vid or not sig:
        return None
    expected = hmac_sha256_hex(secret, f"vid:{vid}")[:32]
    if not constant_time_equals(sig, expected):
        return None
    return vid


def ensure_visitor_id(request: Request, response: Response, settings: Settings) -> str:
    existing = verify_visitor_cookie(
        request.cookies.get(VISITOR_COOKIE_NAME), settings.SECRET_KEY
    )
    if existing is not None:
        return existing
    vid = generate_visitor_id()
    signed = sign_visitor_id(vid, settings.SECRET_KEY)
    set_cookie_secure(
        response,
        VISITOR_COOKIE_NAME,
        signed,
        settings,
        max_age=VISITOR_COOKIE_MAX_AGE,
    )
    return vid


# ---------------------------------------------------------------------------
# Sessions (SHA-256 hashed tokens stored in PostgreSQL)
# ---------------------------------------------------------------------------


async def create_session(
    db: AsyncSession,
    user_id: int,
    *,
    ttl_days: int = SESSION_TTL_DAYS,
) -> str:
    raw_token = secrets.token_urlsafe(32)
    token_hash = sha256_hex(raw_token)
    expires_at = datetime.now(UTC) + timedelta(days=ttl_days)
    db.add(Session(token_hash=token_hash, user_id=user_id, expires_at=expires_at))
    await db.flush()
    return raw_token


async def get_user_by_session_token(
    db: AsyncSession, raw_token: str | None
) -> User | None:
    if not raw_token:
        return None
    token_hash = sha256_hex(raw_token)
    now = datetime.now(UTC)
    stmt = (
        select(User)
        .join(Session, Session.user_id == User.id)
        .options(selectinload(User.identities))
        .where(Session.token_hash == token_hash, Session.expires_at > now)
        .limit(1)
    )
    res = await db.execute(stmt)
    return res.scalar_one_or_none()


async def delete_session_by_token(db: AsyncSession, raw_token: str | None) -> None:
    if not raw_token:
        return
    token_hash = sha256_hex(raw_token)
    await db.execute(delete(Session).where(Session.token_hash == token_hash))
    await db.flush()


async def get_current_user_optional(
    request: Request, db: AsyncSession
) -> User | None:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    return await get_user_by_session_token(db, raw_token)


# ---------------------------------------------------------------------------
# Signed Unsubscribe & Anonymous Tripcode
# ---------------------------------------------------------------------------


def make_unsubscribe_token(user_id: int, secret: str) -> str:
    sig = hmac_sha256_hex(secret, f"unsub:{user_id}")
    return f"{user_id}.{sig}"


def verify_unsubscribe_token(token: str | None, secret: str) -> int | None:
    if not token or "." not in token:
        return None
    uid_str, sig = token.split(".", 1)
    if not uid_str.isdigit() or not sig:
        return None
    user_id = int(uid_str)
    expected = hmac_sha256_hex(secret, f"unsub:{user_id}")
    if not constant_time_equals(sig, expected):
        return None
    return user_id


def make_anon_tag(post_id: int, actor_key: str, secret: str) -> str:
    digest = hmac_sha256_hex(secret, f"anon:{post_id}:{actor_key}")
    return f"Anonymous #{digest[:4]}"


# ---------------------------------------------------------------------------
# Control-Char Guards & Anti-Impersonation (Homoglyph + Combining Marks)
# ---------------------------------------------------------------------------


def reject_control_chars(value: str, *, allow_newlines: bool) -> None:
    allowed = {"\t", "\n", "\r"} if allow_newlines else set()
    for ch in value:
        code = ord(ch)
        if code == 0 or code == 127 or (code < 32 and ch not in allowed):
            raise HTTPException(
                status_code=400,
                detail="Input contains disallowed control characters",
            )


def normalize_display_name(name: str) -> str:
    # NFKD decomposes diacritics into base + combining Mn marks
    decomposed = unicodedata.normalize("NFKD", name)
    filtered = "".join(
        ch
        for ch in decomposed
        if unicodedata.category(ch) not in ("Cf", "Mn", "Cc")
    )
    filtered = _ZERO_WIDTH_RE.sub("", filtered)
    folded = filtered.translate(_CONFUSABLES_MAP)
    folded = unicodedata.normalize("NFKC", folded)
    folded = _MULTI_SPACE_RE.sub(" ", folded).strip().lower()
    return folded


def is_reserved_owner_name(name: str, settings: Settings) -> bool:
    norm = normalize_display_name(name)
    if not norm:
        return False
    compact = norm.replace(" ", "")
    alnum = _NON_ALNUM_RE.sub("", norm)
    for reserved in settings.OWNER_RESERVED_NAMES:
        r_norm = normalize_display_name(reserved)
        if not r_norm:
            continue
        r_compact = r_norm.replace(" ", "")
        r_alnum = _NON_ALNUM_RE.sub("", r_norm)
        if norm == r_norm or compact == r_compact or (alnum and alnum == r_alnum):
            return True
    return False


def validate_display_name(
    name: str, *, is_owner: bool, settings: Settings
) -> str:
    reject_control_chars(name, allow_newlines=False)
    cleaned = "".join(
        ch
        for ch in name
        if unicodedata.category(ch) not in ("Cf", "Cc")
    )
    cleaned = _ZERO_WIDTH_RE.sub("", cleaned)
    cleaned = _MULTI_SPACE_RE.sub(" ", cleaned).strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Display name cannot be empty")
    if len(cleaned) > 64:
        raise HTTPException(
            status_code=400, detail="Display name must be at most 64 characters"
        )
    if not is_owner and is_reserved_owner_name(cleaned, settings):
        raise HTTPException(status_code=400, detail="Display name is reserved")
    return cleaned


def sanitize_external_display_name(
    name: str | None, *, fallback: str, is_owner: bool, settings: Settings
) -> str:
    raw = name or ""
    cleaned = "".join(
        ch for ch in raw if unicodedata.category(ch) not in ("Cf", "Cc")
    )
    candidate = _MULTI_SPACE_RE.sub(" ", _ZERO_WIDTH_RE.sub("", cleaned)).strip()[:64]
    if not candidate:
        candidate = fallback[:64] or "User"
    if not is_owner and is_reserved_owner_name(candidate, settings):
        candidate = f"{candidate[:56]} (user)"
    return candidate


# ---------------------------------------------------------------------------
# Strict Redirect Validation (Zero Open Redirects)
# ---------------------------------------------------------------------------


def validate_next_url(next_url: str | None, settings: Settings) -> str:
    if not next_url:
        return settings.SITE_URL

    candidate = next_url.strip()
    if not candidate:
        return settings.SITE_URL

    if any(ord(ch) < 32 or ord(ch) == 127 for ch in candidate):
        raise HTTPException(status_code=400, detail="Invalid redirect URL")
    if "\\" in candidate:
        raise HTTPException(status_code=400, detail="Invalid redirect URL")
    if candidate.startswith("//"):
        raise HTTPException(status_code=400, detail="Invalid redirect URL")

    parsed = urlsplit(candidate)

    if not parsed.scheme and not parsed.netloc:
        if not candidate.startswith("/"):
            raise HTTPException(status_code=400, detail="Invalid redirect URL")
        return candidate

    site_parsed = urlsplit(settings.SITE_URL)
    if (
        parsed.scheme in ("http", "https")
        and parsed.scheme.lower() == site_parsed.scheme.lower()
        and parsed.netloc.lower() == site_parsed.netloc.lower()
    ):
        return candidate

    raise HTTPException(status_code=400, detail="Invalid redirect URL")


# ---------------------------------------------------------------------------
# Comment HTML Sanitization (nh3)
# ---------------------------------------------------------------------------


def render_and_sanitize_comment_html(body_text: str) -> str:
    stripped = body_text.strip()
    if not stripped:
        return ""
    paragraphs = _BLANK_LINE_RE.split(stripped)
    raw_html_parts: list[str] = []
    for para in paragraphs:
        p = para.strip()
        if not p:
            continue
        p_html = p.replace("\r\n", "\n").replace("\n", "<br>")
        raw_html_parts.append(f"<p>{p_html}</p>")

    raw_html = "".join(raw_html_parts)
    cleaned = nh3.clean(
        raw_html,
        tags=_COMMENT_ALLOWED_TAGS,
        attributes=_COMMENT_ALLOWED_ATTRS,
        url_schemes={"http", "https"},
        link_rel="noopener noreferrer nofollow",
    )
    return cleaned


# ---------------------------------------------------------------------------
# Trusted-Proxy-Aware Client IP Extraction & Sliding-Window Rate Limiter
# ---------------------------------------------------------------------------


def _parse_ip(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    candidate = raw.strip()
    if not candidate:
        return None
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def _is_ip_trusted(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    trusted_proxies: list[str],
) -> bool:
    for entry in trusted_proxies:
        cleaned = entry.strip()
        if not cleaned:
            continue
        try:
            net = ipaddress.ip_network(cleaned, strict=False)
        except ValueError:
            continue
        if ip.version == net.version and ip in net:
            return True
    return False


def get_client_ip(request: Request, settings: Settings | None = None) -> str:
    peer_raw = (
        request.client.host if (request.client and request.client.host) else "127.0.0.1"
    )
    peer_ip = _parse_ip(peer_raw)
    cfg = settings or getattr(request.app.state, "settings", None)
    if cfg is None or peer_ip is None:
        return peer_raw

    if not _is_ip_trusted(peer_ip, cfg.TRUSTED_PROXIES):
        return str(peer_ip)

    if cfg.TRUST_CF_HEADERS:
        cf_raw = request.headers.get("cf-connecting-ip")
        if cf_raw:
            cf_ip = _parse_ip(cf_raw)
            if cf_ip is not None:
                return str(cf_ip)

    xff = request.headers.get("x-forwarded-for")
    if xff:
        # Walk right-to-left picking the rightmost untrusted valid IP address
        hops = [part.strip() for part in xff.split(",") if part.strip()]
        valid_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for hop in reversed(hops):
            hop_ip = _parse_ip(hop)
            if hop_ip is None:
                break
            valid_ips.append(hop_ip)
            if not _is_ip_trusted(hop_ip, cfg.TRUSTED_PROXIES):
                return str(hop_ip)
        if valid_ips:
            return str(valid_ips[-1])

    return str(peer_ip)


class SlidingWindowRateLimiter:
    """In-memory per-(bucket, IP) sliding-window rate limiter with stale-key pruning."""

    def __init__(self, *, max_keys: int = 10_000) -> None:
        self._max_keys = max_keys
        # key -> (timestamps_deque, window_seconds)
        self._windows: dict[tuple[str, str], tuple[deque[float], float]] = {}

    @staticmethod
    def _prune(dq: deque[float], cutoff: float) -> None:
        while dq and dq[0] < cutoff:
            dq.popleft()

    def _evict_if_needed(self, now: float) -> None:
        if len(self._windows) <= self._max_keys:
            return
        # First pass: prune all expired keys cleanly
        expired_keys: list[tuple[str, str]] = []
        for key, (dq, win) in self._windows.items():
            self._prune(dq, now - win)
            if not dq:
                expired_keys.append(key)
        for key in expired_keys:
            del self._windows[key]

        # Second pass: if still above cap, evict oldest-activity keys
        if len(self._windows) > self._max_keys:
            overflow = len(self._windows) - self._max_keys
            oldest = sorted(
                self._windows.items(),
                key=lambda kv: kv[1][0][-1] if kv[1][0] else 0.0,
            )[:overflow]
            for key, _ in oldest:
                del self._windows[key]

    def count(self, bucket: str, ip: str, *, window_seconds: float) -> int:
        key = (bucket, ip)
        entry = self._windows.get(key)
        if entry is None:
            return 0
        dq, _ = entry
        self._prune(dq, time.monotonic() - window_seconds)
        if not dq:
            self._windows.pop(key, None)
            return 0
        return len(dq)

    def hit(
        self,
        bucket: str,
        ip: str,
        *,
        limit: int,
        window_seconds: float,
    ) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - window_seconds
        key = (bucket, ip)
        entry = self._windows.get(key)
        if entry is None:
            self._evict_if_needed(now)
            dq: deque[float] = deque()
            self._windows[key] = (dq, window_seconds)
        else:
            dq, prev_win = entry
            if window_seconds > prev_win:
                self._windows[key] = (dq, window_seconds)
        self._prune(dq, cutoff)
        if len(dq) >= limit:
            return False, len(dq)
        dq.append(now)
        return True, len(dq)

    def record(self, bucket: str, ip: str, *, window_seconds: float) -> int:
        now = time.monotonic()
        cutoff = now - window_seconds
        key = (bucket, ip)
        entry = self._windows.get(key)
        if entry is None:
            self._evict_if_needed(now)
            dq: deque[float] = deque()
            self._windows[key] = (dq, window_seconds)
        else:
            dq, prev_win = entry
            if window_seconds > prev_win:
                self._windows[key] = (dq, window_seconds)
        self._prune(dq, cutoff)
        dq.append(now)
        return len(dq)
