from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config as AlembicConfig
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.session import create_engine_and_factory, get_db_session
from app.main import create_app

API_ROOT = Path(__file__).resolve().parent.parent


def get_test_database_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://corner_test:corner_test@127.0.0.1:55432/corner_test",
        ),
    )


def make_alembic_config(db_url: str) -> AlembicConfig:
    cfg = AlembicConfig(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


@pytest.fixture(scope="session")
def test_db_url() -> str:
    url = get_test_database_url()
    os.environ["DATABASE_URL"] = url
    cfg = make_alembic_config(url)
    command.upgrade(cfg, "head")
    return url


@pytest.fixture
def test_settings(tmp_path: Path, test_db_url: str) -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL=test_db_url,
        DATA_DIR=tmp_path / "data",
        BASE_URL="https://aza.moe",
        SITE_URL="https://aza.moe/life",
        API_PREFIX="/api/life",
        SECRET_KEY="test-secret-key-for-unit-tests-0123456789abcdef",
        EXPORT_PAGE_SIZE=2,
        FEED_TITLE="Test Life",
        FEED_DESCRIPTION="Test life feed",
        FEED_LANGUAGE="en",
        FEED_AUTHOR="Tester",
    )


@pytest_asyncio.fixture
async def db_session(test_settings: Settings) -> AsyncIterator[AsyncSession]:
    engine, factory = create_engine_and_factory(test_settings)
    async with factory() as session:
        await session.execute(
            text(
                "TRUNCATE TABLE "
                "site_settings, used_captcha_salts, original_upload_tokens, notification_queue, "
                "comment_web_reactions, comment_tg_reactions, comments, "
                "magic_links, sessions, user_identities, users, "
                "web_poll_votes, poll_options, polls, web_reactions, tg_reactions, "
                "post_views, post_images, posts, channels "
                "RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()

    async with factory() as session:
        yield session

    await engine.dispose()


@pytest_asyncio.fixture
async def api_client(
    test_settings: Settings, db_session: AsyncSession
) -> AsyncIterator[AsyncClient]:
    test_settings.media_dir.mkdir(parents=True, exist_ok=True)
    test_settings.exports_dir.mkdir(parents=True, exist_ok=True)

    app = create_app(test_settings)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_db_session] = _override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
