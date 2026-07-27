"""Fixtures for the WhatsApp transport tests: real test DB (migrated),
real local Redis (skipped when unreachable), recorded fake sender."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.whatsapp_dispatcher import WhatsAppDispatcher
from backend.models import User
from backend.models.enums import UserRole
from backend.tests.conftest import SEEDED_ORG_ID


@pytest.fixture(autouse=True)
async def _reset_global_redis() -> AsyncIterator[None]:
    """The lazy global redis client binds to the first event loop that
    touches it; pytest-asyncio gives every test its own loop, so reset
    the singleton in teardown (while its loop is still alive)."""
    yield
    from backend.core.redis import close_redis

    await close_redis()


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, to_number: str, body: str) -> bool:
        self.sent.append((to_number, body))
        return True


@pytest.fixture
async def redis_client() -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(  # type: ignore[no-untyped-call]
        "redis://localhost:6379/9", decode_responses=True
    )
    try:
        await client.ping()
    except Exception:  # noqa: BLE001 -- unreachable Redis means skip, not error
        pytest.skip("local Redis not reachable")
    yield client
    await client.aclose()


@pytest.fixture
async def staff_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[User]:
    number = f"+9197{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        user = User(
            org_id=uuid.UUID(SEEDED_ORG_ID),
            full_name="Staff Probe",
            whatsapp_number=number,
            role=UserRole.STAFF,
        )
        session.add(user)
        await session.commit()
        user_id = user.id
    yield user
    async with session_factory() as session:
        try:
            await session.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": user_id})
            await session.commit()
        except sa.exc.IntegrityError:
            # rows this test wrote still reference the user; the owning
            # test file's purge fixture removes both
            await session.rollback()


@pytest.fixture
async def owner_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[User]:
    number = f"+9195{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        user = User(
            org_id=uuid.UUID(SEEDED_ORG_ID),
            full_name="Owner Probe",
            whatsapp_number=number,
            role=UserRole.OWNER,
        )
        session.add(user)
        await session.commit()
        user_id = user.id
    yield user
    async with session_factory() as session:
        try:
            await session.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": user_id})
            await session.commit()
        except sa.exc.IntegrityError:
            await session.rollback()


@pytest.fixture
def fake_sender() -> FakeSender:
    return FakeSender()


@pytest.fixture
def dispatcher(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: aioredis.Redis,
    fake_sender: FakeSender,
) -> WhatsAppDispatcher:
    return WhatsAppDispatcher(
        session_factory=session_factory, redis=redis_client, client=fake_sender
    )


def text_message(from_number: str, body: str, message_id: str | None = None) -> dict[str, Any]:
    return {
        "id": message_id or f"wamid.{uuid.uuid4().hex}",
        "from": from_number.lstrip("+"),
        "timestamp": "1753500000",
        "type": "text",
        "text": {"body": body},
    }


def meta_payload(*messages: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123456",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550000000",
                                "phone_number_id": "111111",
                            },
                            "messages": list(messages),
                        },
                    }
                ],
            }
        ],
    }
