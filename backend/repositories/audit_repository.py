"""audit_logs writes -- append-only, one row per mutation in the same
transaction as the mutation itself (docs/02_Database.md §3.18).

Also the read side `undo` depends on: the audit trail is what makes
"the most recent thing you did" answerable at all, since no other table
records *who* did *what* across every aggregate.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import AuditLog

#: Actions `undo` knows how to reverse, and the entity each names.
#: Anything absent is refused by name rather than silently skipped --
#: quietly undoing an *older* action than the one the user meant would
#: be worse than saying no.
#: Action name -> the table it acts on. These are matched against what
#: services actually write, so a rename on either side breaks `undo`
#: silently -- `sale.confirmed` was listed here while sales_service has
#: always written `sale.created`, which meant no sale was ever undoable.
#: test_undo_actions_exist keeps the two honest.
UNDOABLE_ACTIONS: dict[str, str] = {
    "purchase.confirmed": "purchase_headers",
    "sale.created": "sales_headers",
    "expense.created": "expenses",
    "income.created": "income",
    "capital.contribution": "partner_capital",
    "capital.withdrawal": "partner_capital",
}

#: Recorded when an action is reversed, so it can't be reversed twice.
UNDONE_SUFFIX = ".undone"


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(self, entry: AuditLog) -> AuditLog:
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def latest_undoable(
        self, org_id: uuid.UUID, actor_user_id: uuid.UUID, since: datetime.datetime
    ) -> AuditLog | None:
        """The most recent reversible action this user took inside the
        window, skipping any already undone."""
        stmt = (
            select(AuditLog)
            .where(
                AuditLog.org_id == org_id,
                AuditLog.actor_user_id == actor_user_id,
                AuditLog.action.in_(list(UNDOABLE_ACTIONS)),
                AuditLog.created_at >= since,
            )
            .order_by(AuditLog.created_at.desc())
        )
        for candidate in (await self._session.execute(stmt)).scalars():
            if not await self.was_undone(org_id, candidate.entity_id):
                return candidate
        return None

    async def find_action(
        self, org_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID
    ) -> AuditLog | None:
        """The creating action for a named entity."""
        stmt = (
            select(AuditLog)
            .where(
                AuditLog.org_id == org_id,
                AuditLog.entity_type == entity_type,
                AuditLog.entity_id == entity_id,
                AuditLog.action.in_(list(UNDOABLE_ACTIONS)),
            )
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def was_undone(self, org_id: uuid.UUID, entity_id: uuid.UUID) -> bool:
        stmt = (
            select(AuditLog.id)
            .where(
                AuditLog.org_id == org_id,
                AuditLog.entity_id == entity_id,
                AuditLog.action.endswith(UNDONE_SUFFIX),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first() is not None
