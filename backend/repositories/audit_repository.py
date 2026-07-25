"""audit_logs writes -- append-only, one row per mutation in the same
transaction as the mutation itself (docs/02_Database.md §3.18)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import AuditLog


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(self, entry: AuditLog) -> AuditLog:
        self._session.add(entry)
        await self._session.flush()
        return entry
