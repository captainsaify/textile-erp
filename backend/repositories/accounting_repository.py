"""Accounting aggregates: expenses, income, cash/bank ledgers, partner
capital, journal. Ledger balance reads are O(1) off the latest row's
resulting_balance snapshot -- docs/06_Accounting.md §9; appends compute
the new snapshot under a per-(org, ledger) advisory lock so two
concurrent posts can't both read the same previous balance."""

from __future__ import annotations

import datetime
import decimal
import uuid
from typing import cast

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    BankLedger,
    CashLedger,
    Expense,
    Income,
    Journal,
    JournalLine,
    PartnerCapital,
)
from backend.models.enums import CapitalEntryType, LedgerEntryType

LedgerModel = type[CashLedger] | type[BankLedger]

_LEDGERS: dict[str, LedgerModel] = {"cash": CashLedger, "bank": BankLedger}


class LedgerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _advisory_lock(self, org_id: uuid.UUID, scope: str) -> None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"{scope}:{org_id}"},
        )

    async def balance(self, org_id: uuid.UUID, ledger: str) -> decimal.Decimal:
        model = _LEDGERS[ledger]
        stmt = (
            select(model.resulting_balance)
            .where(model.org_id == org_id)
            .order_by(model.created_at.desc(), model.id.desc())
            .limit(1)
        )
        value = (await self._session.execute(stmt)).scalar_one_or_none()
        return value if value is not None else decimal.Decimal("0")

    async def recent_entries(
        self, org_id: uuid.UUID, ledger: str, limit: int = 5
    ) -> list[CashLedger | BankLedger]:
        model = _LEDGERS[ledger]
        stmt = (
            select(model)
            .where(model.org_id == org_id)
            .order_by(model.created_at.desc(), model.id.desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars()
        return cast("list[CashLedger | BankLedger]", list(rows))

    async def append(
        self,
        org_id: uuid.UUID,
        ledger: str,
        *,
        entry_type: LedgerEntryType,
        amount: decimal.Decimal,
        source_type: str,
        source_id: uuid.UUID | None,
        entry_date: datetime.date,
        notes: str | None,
        created_by: uuid.UUID,
    ) -> CashLedger | BankLedger:
        """Append a signed entry and return it with resulting_balance set."""
        await self._advisory_lock(org_id, f"ledger:{ledger}")
        previous = await self.balance(org_id, ledger)
        model = _LEDGERS[ledger]
        row = model(
            org_id=org_id,
            entry_type=entry_type,
            amount=amount,
            resulting_balance=previous + amount,
            source_type=source_type,
            source_id=source_id,
            entry_date=entry_date,
            notes=notes,
            created_by=created_by,
        )
        self._session.add(row)
        await self._session.flush()
        return row


class PartnerCapitalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def balance(self, org_id: uuid.UUID, partner_id: uuid.UUID) -> decimal.Decimal:
        stmt = (
            select(PartnerCapital.resulting_balance)
            .where(PartnerCapital.org_id == org_id, PartnerCapital.partner_id == partner_id)
            .order_by(PartnerCapital.created_at.desc(), PartnerCapital.id.desc())
            .limit(1)
        )
        value = (await self._session.execute(stmt)).scalar_one_or_none()
        return value if value is not None else decimal.Decimal("0")

    async def append(
        self,
        org_id: uuid.UUID,
        partner_id: uuid.UUID,
        *,
        entry_type: CapitalEntryType,
        amount: decimal.Decimal,
        settled_via: str | None,
        entry_date: datetime.date,
        notes: str | None,
        created_by: uuid.UUID,
    ) -> PartnerCapital:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"capital:{org_id}:{partner_id}"},
        )
        previous = await self.balance(org_id, partner_id)
        row = PartnerCapital(
            org_id=org_id,
            partner_id=partner_id,
            entry_type=entry_type,
            amount=amount,
            resulting_balance=previous + amount,
            settled_via=settled_via,
            entry_date=entry_date,
            notes=notes,
            created_by=created_by,
        )
        self._session.add(row)
        await self._session.flush()
        return row


class JournalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(
        self,
        org_id: uuid.UUID,
        *,
        entry_date: datetime.date,
        description: str,
        source_type: str,
        source_id: uuid.UUID,
        created_by: uuid.UUID,
        lines: list[JournalLine],
    ) -> Journal:
        journal = Journal(
            org_id=org_id,
            entry_date=entry_date,
            description=description,
            source_type=source_type,
            source_id=source_id,
            created_by=created_by,
        )
        self._session.add(journal)
        await self._session.flush()
        for line in lines:
            line.journal_id = journal.id
            self._session.add(line)
        await self._session.flush()
        return journal

    async def account_rollup(
        self, org_id: uuid.UUID, start: datetime.date, end: datetime.date
    ) -> dict[str, tuple[decimal.Decimal, decimal.Decimal]]:
        """SUM(debit), SUM(credit) per account_code for journal entries
        dated in [start, end] -- this *is* P&L's source of truth
        (docs/06_Accounting.md §5), not a re-derivation from the
        simplified cash/bank ledgers, so the two can never quietly
        diverge as new transaction types are added."""
        stmt = (
            select(
                JournalLine.account_code,
                func.coalesce(func.sum(JournalLine.debit), 0),
                func.coalesce(func.sum(JournalLine.credit), 0),
            )
            .join(Journal, Journal.id == JournalLine.journal_id)
            .where(
                Journal.org_id == org_id,
                Journal.entry_date >= start,
                Journal.entry_date <= end,
            )
            .group_by(JournalLine.account_code)
        )
        rows = (await self._session.execute(stmt)).all()
        return {
            str(code): (decimal.Decimal(debit), decimal.Decimal(credit))
            for code, debit, credit in rows
        }


class ExpenseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(self, expense: Expense) -> Expense:
        self._session.add(expense)
        await self._session.flush()
        return expense

    async def distinct_categories(self, org_id: uuid.UUID) -> list[str]:
        stmt = (
            select(Expense.category)
            .where(Expense.org_id == org_id, Expense.deleted_at.is_(None))
            .distinct()
        )
        return list((await self._session.execute(stmt)).scalars())


class IncomeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(self, income: Income) -> Income:
        self._session.add(income)
        await self._session.flush()
        return income

    async def distinct_categories(self, org_id: uuid.UUID) -> list[str]:
        stmt = (
            select(Income.category)
            .where(Income.org_id == org_id, Income.deleted_at.is_(None))
            .distinct()
        )
        return list((await self._session.execute(stmt)).scalars())


async def business_now(session: AsyncSession, org_id: uuid.UUID) -> datetime.datetime:
    """Wall-clock time in the org's local timezone -- for a `dashboard`
    header timestamp, not just the date. docs/02_Database.md §8."""
    import zoneinfo

    from backend.models import Organization

    tz_name = (
        await session.execute(select(Organization.timezone).where(Organization.id == org_id))
    ).scalar_one()
    return datetime.datetime.now(zoneinfo.ZoneInfo(tz_name))


async def business_today(session: AsyncSession, org_id: uuid.UUID) -> datetime.date:
    """The org's local calendar date -- docs/02_Database.md §8: DATE
    columns hold the business's local date, never UTC 'today'."""
    return (await business_now(session, org_id)).date()
