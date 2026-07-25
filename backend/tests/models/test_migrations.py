"""Migration discipline check from docs/02_Database.md §6: every
migration must survive upgrade -> downgrade -> upgrade against a real
database, seed data included."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.tests.conftest import purge_business_rows, run_alembic


async def test_upgrade_downgrade_upgrade_round_trip(
    migrated_test_db: str, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # rows from other tests would block the seed migration's downgrade
    await purge_business_rows(session_factory)
    run_alembic("downgrade", "base")
    run_alembic("upgrade", "head")
    run_alembic("downgrade", "base")
    run_alembic("upgrade", "head")
