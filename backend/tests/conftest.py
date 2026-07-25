"""Shared fixtures. DB-backed tests run against TEST_DATABASE_URL and are
skipped (not failed) when it isn't configured, so the pure-metadata tests
still run anywhere."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


def run_alembic(*args: str) -> None:
    env = {**os.environ, "ALEMBIC_DATABASE_URL": TEST_DATABASE_URL}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)} failed:\n{result.stderr}")


@pytest.fixture(scope="session")
def migrated_test_db() -> str:
    """Test database migrated to head (idempotent if already there)."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    run_alembic("upgrade", "head")
    return TEST_DATABASE_URL


# fixed UUIDs from the seed migration (7cb2a37b2a4f)
SEEDED_ORG_ID = "00000000-0000-4000-a000-000000000001"
SEEDED_KG_UNIT_ID = "00000000-0000-4000-a000-000000000101"
SEEDED_TEXTILE_TYPE_ID = "00000000-0000-4000-a000-000000000201"
SEEDED_MAIN_WAREHOUSE_ID = "00000000-0000-4000-a000-000000000301"


@pytest.fixture
async def engine(migrated_test_db: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migrated_test_db)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False)


# FK-safe delete order: children before parents, users last (everything
# has created_by). Seed rows (org, units, product type, warehouse) stay.
_PURGE_ORDER = (
    "journal_lines",
    "journal",
    "audit_logs",
    "cash_ledger",
    "bank_ledger",
    "partner_capital",
    "inventory_movements",
    "inventory",
    "purchase_lines",
    "purchase_headers",
    "sales_lines",
    "sales_headers",
    "expenses",
    "income",
    "ocr_learning_dictionary",
    "ocr_templates",
    "attachments",
    "products",
    "suppliers",
    "customers",
    "whatsapp_sessions",
    "partners",
    "users",
)


async def purge_business_rows(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Remove all business rows written by tests, keeping seed data."""
    import sqlalchemy as sa

    async with session_factory() as session:
        for table in _PURGE_ORDER:
            await session.execute(sa.text(f"DELETE FROM {table}"))
        await session.commit()
