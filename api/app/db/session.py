from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_engine_and_factory(
    settings: Settings | None = None,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    cfg = settings or get_settings()
    engine = create_async_engine(
        cfg.DATABASE_URL,
        pool_pre_ping=True,
        future=True,
    )
    factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    return engine, factory


def init_db(settings: Settings | None = None) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    global _engine, _session_factory
    if _engine is None or _session_factory is None:
        _engine, _session_factory = create_engine_and_factory(settings)
    return _engine, _session_factory


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    _, factory = init_db()
    return factory


async def close_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def get_db_session() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        yield session
