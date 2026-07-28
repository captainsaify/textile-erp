"""WhatsApp session state machine storage -- docs/08_WhatsApp.md §5.

Redis is the low-latency source of truth (30-min TTL); every write is
mirrored to whatsapp_sessions in Postgres so an in-progress draft
survives a Redis restart. Reads fall back to the mirror on a Redis miss
and re-warm Redis.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import uuid
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.redis import get_redis
from backend.models import WhatsappSession

SESSION_TTL_SECONDS = 30 * 60

IDLE = "idle"
AWAITING_PURCHASE_CONFIRMATION = "awaiting_purchase_confirmation"
AWAITING_SALE_CONFIRMATION = "awaiting_sale_confirmation"
AWAITING_SETTLEMENT_CONFIRMATION = "awaiting_settlement_confirmation"
AWAITING_RETURN_REFUND_CHOICE = "awaiting_return_refund_choice"
#: docs/20_ConversationalIntake.md -- what is this photo, and then one
#: question per field the sheet didn't carry
AWAITING_INTENT = "awaiting_intent"
AWAITING_SLOT = "awaiting_slot"
#: docs/20 §7 -- a command invoked with missing arguments asks for them
#: instead of printing usage
AWAITING_COMMAND_SLOT = "awaiting_command_slot"


@dataclasses.dataclass(frozen=True)
class SessionState:
    state: str
    context: dict[str, Any]

    @property
    def is_idle(self) -> bool:
        return self.state == IDLE


class SessionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: aioredis.Redis | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis or get_redis()

    @staticmethod
    def _key(org_id: uuid.UUID, user_id: uuid.UUID) -> str:
        return f"wa:session:{org_id}:{user_id}"

    async def get(self, org_id: uuid.UUID, user_id: uuid.UUID) -> SessionState:
        raw = await self._redis.get(self._key(org_id, user_id))
        if raw is not None:
            payload = json.loads(raw)
            return SessionState(state=payload["state"], context=payload["context"])

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(WhatsappSession).where(
                        WhatsappSession.org_id == org_id, WhatsappSession.user_id == user_id
                    )
                )
            ).scalar_one_or_none()
        now = datetime.datetime.now(datetime.UTC)
        if row is None or row.expires_at < now:
            return SessionState(state=IDLE, context={})
        state = SessionState(state=row.state, context=dict(row.context))
        await self._redis.set(
            self._key(org_id, user_id),
            json.dumps({"state": state.state, "context": state.context}),
            ex=SESSION_TTL_SECONDS,
        )
        return state

    async def set(
        self, org_id: uuid.UUID, user_id: uuid.UUID, state: str, context: dict[str, Any]
    ) -> None:
        await self._redis.set(
            self._key(org_id, user_id),
            json.dumps({"state": state, "context": context}),
            ex=SESSION_TTL_SECONDS,
        )
        expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
            seconds=SESSION_TTL_SECONDS
        )
        async with self._session_factory() as session:
            stmt = pg_insert(WhatsappSession).values(
                org_id=org_id,
                user_id=user_id,
                state=state,
                context=context,
                expires_at=expires_at,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[WhatsappSession.org_id, WhatsappSession.user_id],
                set_={"state": state, "context": context, "expires_at": expires_at},
            )
            await session.execute(stmt)
            await session.commit()

    async def clear(self, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
        await self.set(org_id, user_id, IDLE, {})
