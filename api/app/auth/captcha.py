from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import constant_time_equals, hmac_sha256_hex
from app.config import Settings
from app.db.models import UsedCaptchaSalt

DEFAULT_CHALLENGE_TTL_SECONDS = 300
MAX_ESCALATED_NUMBER = 2_000_000
MAX_ALTCHA_PAYLOAD_B64_LEN = 4096
SALT_CLEANUP_INTERVAL_SECONDS = 300.0

# Canonical ALTCHA salt with mandatory trailing '&' so `salt + str(number)`
# can never collide across salt/number digit boundaries.
_CANONICAL_SALT_RE = re.compile(r"[0-9a-f]{24}\?expires=(\d+)&")

_last_salt_cleanup_mono: float = 0.0


class AltchaError(HTTPException):
    def __init__(self, detail: str) -> None:
        super().__init__(status_code=400, detail=detail)


def compute_escalated_maxnumber(base_maxnumber: int, recent_hits: int) -> int:
    """Scale ALTCHA PoW difficulty smoothly on IP bursts."""
    if recent_hits <= 5:
        return base_maxnumber
    exponent = min(recent_hits - 5, 4)
    return min(base_maxnumber * (2**exponent), MAX_ESCALATED_NUMBER)


def create_altcha_challenge(
    settings: Settings,
    *,
    maxnumber: int | None = None,
    ttl_seconds: int = DEFAULT_CHALLENGE_TTL_SECONDS,
    now_ts: int | None = None,
) -> dict[str, Any]:
    effective_max = maxnumber if maxnumber is not None else settings.ALTCHA_MAX_NUMBER
    effective_max = max(1, int(effective_max))

    now = int(time.time()) if now_ts is None else int(now_ts)
    expires = now + ttl_seconds
    raw_salt = secrets.token_hex(12)
    salt = f"{raw_salt}?expires={expires}&"

    secret_number = secrets.randbelow(effective_max + 1)
    challenge = hashlib.sha256(f"{salt}{secret_number}".encode()).hexdigest()
    signature = hmac_sha256_hex(settings.SECRET_KEY, challenge)

    return {
        "algorithm": "SHA-256",
        "challenge": challenge,
        "maxnumber": effective_max,
        "salt": salt,
        "signature": signature,
    }


def _parse_canonical_salt_expiry(salt: str) -> int:
    match = _CANONICAL_SALT_RE.fullmatch(salt)
    if match is None:
        raise AltchaError("Invalid ALTCHA salt format")
    return int(match.group(1))


async def _maybe_cleanup_expired_salts(db: AsyncSession, now_dt: datetime) -> None:
    global _last_salt_cleanup_mono
    now_mono = time.monotonic()
    if now_mono - _last_salt_cleanup_mono < SALT_CLEANUP_INTERVAL_SECONDS:
        return
    _last_salt_cleanup_mono = now_mono
    await db.execute(delete(UsedCaptchaSalt).where(UsedCaptchaSalt.expires_at < now_dt))


async def verify_altcha_payload(
    db: AsyncSession,
    payload_b64: str | None,
    settings: Settings,
    *,
    now_ts: int | None = None,
) -> None:
    if not payload_b64 or not isinstance(payload_b64, str):
        raise AltchaError("Missing ALTCHA verification payload")

    raw_b64 = payload_b64.strip()
    if len(raw_b64) > MAX_ALTCHA_PAYLOAD_B64_LEN:
        raise AltchaError("ALTCHA payload exceeds maximum allowed size")

    padding = (-len(raw_b64)) % 4
    try:
        decoded_bytes = base64.b64decode(raw_b64 + ("=" * padding), validate=True)
        payload = json.loads(decoded_bytes.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AltchaError("Invalid ALTCHA payload encoding") from exc

    if not isinstance(payload, dict):
        raise AltchaError("Invalid ALTCHA payload format")

    algorithm = payload.get("algorithm")
    challenge = payload.get("challenge")
    number = payload.get("number")
    salt = payload.get("salt")
    signature = payload.get("signature")

    if algorithm != "SHA-256":
        raise AltchaError("Unsupported ALTCHA algorithm")
    if (
        not isinstance(challenge, str)
        or not challenge
        or not isinstance(salt, str)
        or not salt
        or not isinstance(signature, str)
        or not signature
    ):
        raise AltchaError("Incomplete ALTCHA payload fields")
    if isinstance(number, bool) or not isinstance(number, int) or number < 0:
        raise AltchaError("Invalid ALTCHA solution number")

    # 1. Strictly enforce canonical `<24hex>?expires=<digits>&` salt format
    expires_ts = _parse_canonical_salt_expiry(salt)

    # 2. Verify HMAC-SHA256 signature (constant-time)
    expected_sig = hmac_sha256_hex(settings.SECRET_KEY, challenge)
    if not constant_time_equals(expected_sig, signature):
        raise AltchaError("Invalid ALTCHA signature")

    # 3. Verify expiry embedded in canonical salt
    now = int(time.time()) if now_ts is None else int(now_ts)
    if expires_ts < now:
        raise AltchaError("ALTCHA challenge expired")

    # 4. Verify SHA-256 PoW solution
    computed_challenge = hashlib.sha256(f"{salt}{number}".encode()).hexdigest()
    if not constant_time_equals(computed_challenge, challenge):
        raise AltchaError("Invalid ALTCHA proof-of-work solution")

    # 5. Replay protection keyed on `challenge` in PostgreSQL used_captcha_salts
    now_dt = datetime.fromtimestamp(now, tz=UTC)
    expires_dt = datetime.fromtimestamp(expires_ts, tz=UTC)

    await _maybe_cleanup_expired_salts(db, now_dt)
    insert_stmt = (
        pg_insert(UsedCaptchaSalt)
        .values(salt=challenge, expires_at=expires_dt)
        .on_conflict_do_nothing(index_elements=[UsedCaptchaSalt.salt])
        .returning(UsedCaptchaSalt.salt)
    )
    res = await db.execute(insert_stmt)
    inserted = res.scalar_one_or_none()
    if inserted is None:
        raise AltchaError("ALTCHA challenge has already been used")
    await db.flush()
