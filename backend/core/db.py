"""Async SQLAlchemy engine/session setup.

Per docs/01_Architecture.md: async throughout, since most latency in
this app is external I/O (WhatsApp API, OCR) or DB round-trips, and a
sync ORM would block the FastAPI event loop.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.core.config import get_settings
from backend.core.lifecycle import on_release


def create_engine(database_url: str | None = None) -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        database_url or settings.database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=5,
    )


# Engine/session factory are created lazily, not at import time: importing
# this module must never require DATABASE_URL to be set (alembic and tests
# construct their own engines), and tests must be able to swap the URL
# before first use.
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False, autoflush=False
        )
    return _session_factory


@on_release
async def dispose_engine() -> None:
    """App-shutdown hook; also lets tests and Celery tasks reset the
    module state.

    The globals are cleared even when disposal itself fails -- an engine
    whose loop has already closed raises on dispose, and keeping the
    reference would hand the *next* caller the same dead engine.
    """
    global _engine, _session_factory
    engine, _engine, _session_factory = _engine, None, None
    if engine is not None:
        await engine.dispose()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a session, closes it after the request."""
    async with get_session_factory()() as session:
        yield session


async def check_db_connection() -> bool:
    """Used by /healthz -- a cheap round-trip, not a full pool check."""
    from sqlalchemy import text

    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 -- health check must never raise
        return False
