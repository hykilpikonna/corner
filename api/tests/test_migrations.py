from __future__ import annotations

import asyncio

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import make_alembic_config

EXPECTED_TABLES = {
    "channels",
    "posts",
    "post_images",
    "tg_reactions",
    "web_reactions",
    "polls",
    "poll_options",
    "web_poll_votes",
    "users",
    "user_identities",
    "sessions",
    "magic_links",
    "comments",
    "notification_queue",
    "original_upload_tokens",
    "used_captcha_salts",
}


async def _fetch_public_tables(session: AsyncSession) -> set[str]:
    res = await session.execute(
        text(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename != 'alembic_version'"
        )
    )
    return {row[0] for row in res.fetchall()}


@pytest.mark.asyncio
async def test_all_tables_exist_and_roundtrip_migration(
    db_session: AsyncSession, test_db_url: str
) -> None:
    tables_before = await _fetch_public_tables(db_session)
    assert EXPECTED_TABLES.issubset(tables_before)
    await db_session.commit()

    cfg = make_alembic_config(test_db_url)
    try:
        await asyncio.to_thread(command.downgrade, cfg, "base")
        tables_after_down = await _fetch_public_tables(db_session)
        assert EXPECTED_TABLES.isdisjoint(tables_after_down)
        await db_session.commit()
    finally:
        await asyncio.to_thread(command.upgrade, cfg, "head")

    tables_after_up = await _fetch_public_tables(db_session)
    assert EXPECTED_TABLES.issubset(tables_after_up)
