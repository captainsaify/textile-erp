"""Double-entry backbone -- docs/06_Accounting.md §1/§3. Called inside
the same transaction as every simplified-ledger write; refuses to post
an unbalanced entry, so an unbalanced transaction cannot commit."""

from __future__ import annotations

import datetime
import decimal
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Journal, JournalLine
from backend.models.enums import AccountCode
from backend.repositories.accounting_repository import JournalRepository

ZERO = decimal.Decimal("0")


class UnbalancedJournalError(Exception):
    """Programming error, not a domain outcome: a posting table entry
    (docs/06_Accounting.md §3) was implemented wrong."""


class JournalService:
    def __init__(self, session: AsyncSession) -> None:
        self._repo = JournalRepository(session)

    async def post(
        self,
        org_id: uuid.UUID,
        *,
        entry_date: datetime.date,
        description: str,
        source_type: str,
        source_id: uuid.UUID,
        created_by: uuid.UUID,
        debits: list[tuple[AccountCode, decimal.Decimal]],
        credits: list[tuple[AccountCode, decimal.Decimal]],
    ) -> Journal:
        total_debit = sum((amount for _, amount in debits), ZERO)
        total_credit = sum((amount for _, amount in credits), ZERO)
        if total_debit != total_credit or total_debit <= ZERO:
            raise UnbalancedJournalError(
                f"debits {total_debit} != credits {total_credit} for {description!r}"
            )
        lines = [
            JournalLine(account_code=account.value, debit=amount, credit=ZERO)
            for account, amount in debits
        ] + [
            JournalLine(account_code=account.value, debit=ZERO, credit=amount)
            for account, amount in credits
        ]
        return await self._repo.insert(
            org_id,
            entry_date=entry_date,
            description=description,
            source_type=source_type,
            source_id=source_id,
            created_by=created_by,
            lines=lines,
        )
