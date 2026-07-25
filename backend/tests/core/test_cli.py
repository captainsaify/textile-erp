"""User onboarding via the operations CLI."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.cli import create_user_record
from backend.models.enums import UserRole


async def test_create_user_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    number = f"+9196{uuid.uuid4().int % 10**8:08d}"
    user = await create_user_record(session_factory, "Owner Probe", number, UserRole.OWNER)
    try:
        assert user.role is UserRole.OWNER
        assert user.whatsapp_number == number

        with pytest.raises(ValueError, match="already belongs"):
            await create_user_record(session_factory, "Dup", number, UserRole.STAFF)
    finally:
        async with session_factory() as session:
            await session.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": user.id})
            await session.commit()


async def test_create_user_rejects_non_e164(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(ValueError, match="E.164"):
        await create_user_record(session_factory, "Bad", "9876543210", UserRole.STAFF)
