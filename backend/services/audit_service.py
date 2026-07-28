"""Every mutating service method records exactly one audit row in the
same transaction -- docs/02_Database.md §3.18, docs/14_Security.md."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import AuditLog
from backend.repositories.audit_repository import AuditRepository
from backend.services import dashboard_cache


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self._repo = AuditRepository(session)

    async def record(
        self,
        org_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        *,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID,
        after_state: dict[str, Any] | None = None,
        before_state: dict[str, Any] | None = None,
        channel: str = "whatsapp",
        whatsapp_message_id: str | None = None,
    ) -> AuditLog:
        entry = await self._repo.insert(
            AuditLog(
                org_id=org_id,
                actor_user_id=actor_user_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                before_state=before_state,
                after_state=after_state,
                channel=channel,
                whatsapp_message_id=whatsapp_message_id,
            )
        )
        # Every business mutation writes an audit row (CLAUDE.md rule 3),
        # which makes this the one place that can invalidate the
        # dashboard cache without a per-service list to keep in sync --
        # including for services not written yet. Never raises: a cache
        # that can't be invalidated must not fail the write.
        await dashboard_cache.invalidate(org_id)
        return entry
