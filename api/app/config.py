from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_WEAK_SECRET_EXACT = {
    "change-me",
    "change-me-in-production",
    "secret",
    "dev-secret",
    "placeholder",
}
_WEAK_SECRET_PREFIXES = (
    "replace-with",
    "change-me",
)


def _parse_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("["):
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        return [item.strip() for item in raw.split(",") if item.strip()]
    return [str(value).strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://corner:corner@localhost:5432/corner"

    # Runtime paths & public URLs
    DATA_DIR: Path = Path("./data")
    BASE_URL: str = "http://localhost:8000"
    SITE_URL: str = "http://localhost:3000/life"
    API_PREFIX: str = "/api/life"
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    # Security
    SECRET_KEY: str = "change-me-in-production"
    TRUSTED_PROXIES: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["127.0.0.1", "::1"]
    )
    TRUST_CF_HEADERS: bool = False

    # Identity & Comments
    ALLOW_ANON_COMMENTS: bool = True
    OWNER_TG_ID: int | None = None
    OWNER_RESERVED_NAMES: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["owner", "admin", "administrator", "root", "system"]
    )

    # Telegram (Kurigram MTProto Bot Client)
    TG_API_ID: int | None = None
    TG_API_HASH: str | None = None
    TG_BOT_TOKEN: str | None = None
    TG_CHANNEL_ID: int | None = None
    TG_DISCUSSION_CHAT_ID: int | None = None
    TG_SESSION_NAME: str = "bot"

    # OAuth & Telegram Login Widget
    GITHUB_CLIENT_ID: str | None = None
    GITHUB_CLIENT_SECRET: str | None = None
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    TG_LOGIN_BOT_USERNAME: str | None = None

    # SMTP Notifications
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM: str = "noreply@example.com"
    SMTP_USE_TLS: bool = True

    # Background intervals & ALTCHA
    RECONCILE_INTERVAL_SECONDS: int = 900
    EMAIL_DEBOUNCE_SECONDS: int = 120
    ALTCHA_MAX_NUMBER: int = 100000

    # Exports & Feeds
    EXPORT_PAGE_SIZE: int = 50
    FEED_TITLE: str = "Life"
    FEED_DESCRIPTION: str = "Life updates"
    FEED_LANGUAGE: str = "en"
    FEED_AUTHOR: str = "Author"
    FEED_IMAGE_URL: str | None = None
    FEED_MAX_ITEMS: int = 100

    @field_validator("CORS_ORIGINS", "OWNER_RESERVED_NAMES", "TRUSTED_PROXIES", mode="before")
    @classmethod
    def _validate_list(cls, v: Any) -> list[str]:
        return _parse_str_list(v)

    @field_validator("CORS_ORIGINS", mode="after")
    @classmethod
    def _reject_wildcard_cors(cls, v: list[str]) -> list[str]:
        if any(origin.strip() == "*" for origin in v):
            raise ValueError(
                "Wildcard '*' is not allowed in CORS_ORIGINS when credentials are enabled"
            )
        return v

    @field_validator("API_PREFIX", mode="after")
    @classmethod
    def _normalize_api_prefix(cls, v: str) -> str:
        cleaned = "/" + v.strip("/")
        return "" if cleaned == "/" else cleaned

    @field_validator("FEED_IMAGE_URL", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _validate_https_secret_key(self) -> Settings:
        if self.is_https:
            key = self.SECRET_KEY.strip()
            lowered = key.lower()
            if (
                len(key) < 32
                or lowered in _WEAK_SECRET_EXACT
                or any(lowered.startswith(prefix) for prefix in _WEAK_SECRET_PREFIXES)
            ):
                raise ValueError(
                    "SECRET_KEY must be a strong non-placeholder secret (>= 32 chars) "
                    "when BASE_URL uses HTTPS"
                )
        return self

    @property
    def media_dir(self) -> Path:
        return self.DATA_DIR / "media"

    @property
    def exports_dir(self) -> Path:
        return self.DATA_DIR / "exports"

    @property
    def api_base_url(self) -> str:
        base = self.BASE_URL.rstrip("/")
        prefix = self.API_PREFIX.rstrip("/")
        return f"{base}{prefix}"

    @property
    def is_https(self) -> bool:
        return self.BASE_URL.lower().startswith("https://")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
