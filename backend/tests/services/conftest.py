"""Fixtures for service-level tests.

Deliberately self-contained rather than importing the API suite's
conftest: these tests exercise services directly, with no WhatsApp
context, no Redis and no request plumbing, and borrowing that file's
fixtures would drag all of it in.

Cleanup is surgical -- each test removes exactly the rows it created --
rather than calling the shared `purge_business_rows`. That helper does a
whole-database sweep, and `DELETE FROM inventory_movements` does not
clear rows the suite has left in the table's partitions, so the later
`DELETE FROM products` fails on a foreign key. Reaching for it here
would have meant debugging someone else's fixture in order to test a
costing loop.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import User
from backend.models.enums import UserRole
from backend.tests.conftest import SEEDED_ORG_ID, purge_business_rows


@pytest.fixture
async def staff_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[User]:
    async with session_factory() as session:
        user = User(
            org_id=uuid.UUID(SEEDED_ORG_ID),
            full_name="Service Probe",
            whatsapp_number=f"+9197{uuid.uuid4().hex[:8]}",
            role=UserRole.STAFF,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        user_id = user.id
    yield user
    async with session_factory() as session:
        try:
            await session.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": user_id})
            await session.commit()
        except sa.exc.IntegrityError:
            # Products and movements carry created_by, and fixture
            # teardown order is not guaranteed relative to the cleanup
            # below. The probe user is harmless if it outlives the test;
            # failing the test over it would report a fixture ordering
            # detail as a broken costing loop. Same reasoning, and same
            # shape, as backend/tests/api/conftest.py.
            await session.rollback()


@pytest.fixture
def drop_product() -> Iterator[list[uuid.UUID]]:
    """Register product ids to remove, with their movements and stock.

    Ordered by foreign key: movements, then inventory, then the product.
    """
    registered: list[uuid.UUID] = []
    yield registered


@pytest.fixture(autouse=True)
async def _cleanup_registered(
    drop_product: list[uuid.UUID], session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[None]:
    yield
    registered_ids = list(drop_product)
    if not registered_ids:
        return
    async with session_factory() as session:
        for statement in (
            "DELETE FROM inventory_movements WHERE product_id = ANY(:ids)",
            "DELETE FROM inventory WHERE product_id = ANY(:ids)",
            "DELETE FROM products WHERE id = ANY(:ids)",
        ):
            await session.execute(sa.text(statement), {"ids": registered_ids})
        await session.commit()
    # Then the suite-wide sweep, matching every other test file. The
    # surgical delete above is what makes this one succeed: run on its
    # own, these tests would otherwise leave movements behind and the
    # *next* run's API tests would fail on a foreign key, blaming a
    # change that had nothing to do with it.
    await purge_business_rows(session_factory)
