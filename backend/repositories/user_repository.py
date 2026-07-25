"""User lookups. Soft-delete filter applied on every query path --
docs/02_Database.md §4."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active_by_whatsapp_number(self, whatsapp_number: str) -> User | None:
        """Sender resolution -- docs/08_WhatsApp.md §2. Only active,
        non-deleted users may issue commands; number is globally unique
        so no org scoping on the lookup (the user row carries org_id)."""
        stmt = select(User).where(
            User.whatsapp_number == whatsapp_number,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
