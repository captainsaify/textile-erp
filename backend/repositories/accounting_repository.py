"""Accounting aggregates: expenses, income, cash/bank ledgers, partner
capital, journal. Ledger balance reads are O(1) off the latest row's
resulting_balance snapshot -- docs/06_Accounting.md §9; appends compute
the new snapshot under a per-(org, ledger) advisory lock so two
concurrent posts can't both read the same previous balance."""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid
from collections.abc import Sequence
from typing import cast

from sqlalchemy import Text, func, select, text
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    BankLedger,
    CashLedger,
    Expense,
    Income,
    Journal,
    JournalLine,
    PartnerCapital,
    User,
)
from backend.models.enums import CapitalEntryType, LedgerEntryType

LedgerModel = type[CashLedger] | type[BankLedger]


@dataclasses.dataclass(frozen=True)
class ExpenseRow:
    """One expense, with the person who recorded it."""

    id: uuid.UUID
    entry_date: datetime.date
    category: str
    amount: decimal.Decimal
    paid_via: str
    description: str | None
    spent_by: str


_LEDGERS: dict[str, LedgerModel] = {"cash": CashLedger, "bank": BankLedger}


#: Source types that exist only to cancel an earlier entry.
REVERSAL_SUFFIXES = ("_reversal", "_undo")


def _is_reversal(source_type: str | None) -> bool:
    return bool(source_type) and source_type.endswith(REVERSAL_SUFFIXES)  # type: ignore[union-attr]


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

    @staticmethod
    def cancelled_ids(entries: Sequence[CashLedger | BankLedger]) -> set[uuid.UUID]:
        """Which of these rows are a reversal, or the row one reversed.

        Reversals are compensating entries -- nothing is ever deleted --
        so the ledger correctly holds both halves of an undone payment.
        That is right for the audit trail and wrong for "money out this
        month", where a reversed 29,20,030 and its 29,20,030 refund
        between them claimed 58,40,060 of movement that never happened.

        Pairing is by source_id and amount rather than a stored link:
        a reversal always names the same entity as the entry it undoes
        and always carries exactly the opposite amount, and where two
        identical entries make the pairing ambiguous, either choice
        removes the same two numbers.
        """
        by_key: dict[tuple[uuid.UUID | None, decimal.Decimal], list[CashLedger | BankLedger]] = {}
        for entry in entries:
            if _is_reversal(entry.source_type):
                continue
            by_key.setdefault((entry.source_id, entry.amount), []).append(entry)

        cancelled: set[uuid.UUID] = set()
        for entry in entries:
            if not _is_reversal(entry.source_type):
                continue
            cancelled.add(entry.id)
            candidates = by_key.get((entry.source_id, -entry.amount)) or []
            original = next((row for row in candidates if row.id not in cancelled), None)
            if original is not None:
                cancelled.add(original.id)
        return cancelled

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

    @staticmethod
    def running_balances(
        entries: list[CashLedger | BankLedger],
        *,
        cancelled: frozenset[uuid.UUID] | set[uuid.UUID] = frozenset(),
        opening: decimal.Decimal = decimal.Decimal("0"),
    ) -> dict[uuid.UUID, decimal.Decimal]:
        """The balance after each row, computed **in the order given**.

        `resulting_balance` on the row cannot answer this. It is a
        snapshot of the balance when the row was *written*, so it runs in
        insertion order -- while a cashbook is read in date order. Those
        two agree only until something is backdated.

        A payment to Iqbal Bhai dated 20-07 but entered on 01-08 made
        them disagree: on the sheet it sat between 22-07 and 18-07
        carrying a balance from the end of the book, so the BALANCE
        column stopped reconciling with the IN and OUT columns beside it.

        A cancelled row does not move the balance. It is still shown --
        nothing is hidden -- but it is already excluded from money in and
        money out, and a balance that stepped over it would disagree with
        those totals.
        """
        balances: dict[uuid.UUID, decimal.Decimal] = {}
        balance = opening
        for entry in entries:
            if entry.id not in cancelled:
                balance += entry.amount
            balances[entry.id] = balance
        return balances

    async def balance_before(
        self, org_id: uuid.UUID, ledger: str, before: datetime.date
    ) -> decimal.Decimal:
        """What the account held before a period opened.

        A period-filtered cashbook whose running balance starts at zero
        describes a business that began on the first of the month.
        """
        model = _LEDGERS[ledger]
        stmt = select(func.coalesce(func.sum(model.amount), 0)).where(
            model.org_id == org_id, model.entry_date < before
        )
        return decimal.Decimal(str((await self._session.execute(stmt)).scalar_one()))

    async def entries_between(
        self,
        org_id: uuid.UUID,
        ledger: str,
        start: datetime.date,
        end: datetime.date,
    ) -> list[CashLedger | BankLedger]:
        """A period's rows, oldest first -- the order a cashbook is read.

        `recent_entries` is newest-first and capped, which is right for a
        dashboard panel and wrong for an export: a cashbook whose rows
        run backwards has a running balance column that means nothing.
        """
        model = _LEDGERS[ledger]
        stmt = (
            select(model)
            .where(
                model.org_id == org_id,
                model.entry_date >= start,
                model.entry_date <= end,
            )
            .order_by(model.entry_date, model.created_at, model.id)
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

    async def _lock(self, org_id: uuid.UUID, partner_id: uuid.UUID) -> None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"capital:{org_id}:{partner_id}"},
        )

    async def balance(self, org_id: uuid.UUID, partner_id: uuid.UUID) -> decimal.Decimal:
        """Latest *posted* row. A pending withdrawal has not moved equity
        (docs/06_Accounting.md §8) and is excluded until it posts; the
        chain is ordered by posted_at because an approval can land long
        after the request that created the row."""
        stmt = (
            select(PartnerCapital.resulting_balance)
            .where(
                PartnerCapital.org_id == org_id,
                PartnerCapital.partner_id == partner_id,
                PartnerCapital.status == "posted",
            )
            .order_by(PartnerCapital.posted_at.desc(), PartnerCapital.id.desc())
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
        """Append an immediately-effective (posted) entry."""
        await self._lock(org_id, partner_id)
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
            status="posted",
            posted_at=datetime.datetime.now(datetime.UTC),
            created_by=created_by,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def create_pending(
        self,
        org_id: uuid.UUID,
        partner_id: uuid.UUID,
        *,
        amount: decimal.Decimal,
        settled_via: str,
        entry_date: datetime.date,
        notes: str | None,
        created_by: uuid.UUID,
        entry_type: CapitalEntryType = CapitalEntryType.WITHDRAWAL,
    ) -> PartnerCapital:
        """A capital movement awaiting a second partner (§8). `amount` is
        stored as the signed effect it *will* have; `resulting_balance`
        holds the balance as it stands now, unchanged, and is never read
        while the row is pending.

        `entry_type` because money *in* now waits for a signature too: a
        contribution decides ownership and profit share, so claiming one
        is as consequential as taking money out.
        """
        await self._lock(org_id, partner_id)
        row = PartnerCapital(
            org_id=org_id,
            partner_id=partner_id,
            entry_type=entry_type,
            amount=amount,
            resulting_balance=await self.balance(org_id, partner_id),
            settled_via=settled_via,
            entry_date=entry_date,
            notes=notes,
            status="pending",
            posted_at=None,
            created_by=created_by,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_pending(self, org_id: uuid.UUID, request_id: uuid.UUID) -> PartnerCapital | None:
        stmt = select(PartnerCapital).where(
            PartnerCapital.org_id == org_id,
            PartnerCapital.id == request_id,
            PartnerCapital.status == "pending",
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_pending_by_prefix(self, org_id: uuid.UUID, prefix: str) -> list[PartnerCapital]:
        """WhatsApp users type the short id shown in the request message,
        not a full UUID."""
        stmt = select(PartnerCapital).where(
            PartnerCapital.org_id == org_id,
            PartnerCapital.status == "pending",
            sql_cast(PartnerCapital.id, Text).like(f"{prefix.lower()}%"),
        )
        return list((await self._session.execute(stmt)).scalars())

    async def post_pending(
        self, row: PartnerCapital, *, approver_partner_id: uuid.UUID
    ) -> PartnerCapital:
        """Approve: recompute against the balance as it stands *now* --
        contributions may have posted while this sat waiting -- then join
        the chain at the current instant."""
        await self._lock(row.org_id, row.partner_id)
        row.resulting_balance = await self.balance(row.org_id, row.partner_id) + row.amount
        row.approved_by_partner_ids = [*row.approved_by_partner_ids, approver_partner_id]
        row.status = "posted"
        row.posted_at = datetime.datetime.now(datetime.UTC)
        await self._session.flush()
        return row

    async def reject_pending(self, row: PartnerCapital) -> PartnerCapital:
        """Kept rather than deleted: an audited refusal is itself history
        (docs/02_Database.md soft-delete rationale)."""
        row.status = "rejected"
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

    async def recent(
        self, org_id: uuid.UUID, *, limit: int = 9
    ) -> list[tuple[str, str, datetime.date, decimal.Decimal]]:
        """Recent expenses, for picking one rather than remembering a
        uuid. An expense has no invoice number, so it is identified by
        the short id shown beside what and when."""
        stmt = (
            select(Expense.id, Expense.category, Expense.expense_date, Expense.amount)
            .where(Expense.org_id == org_id, Expense.deleted_at.is_(None))
            .order_by(Expense.expense_date.desc(), Expense.created_at.desc())
            .limit(limit)
        )
        return [
            (str(row[0])[:8], row[1], row[2], row[3])
            for row in (await self._session.execute(stmt)).all()
        ]

    async def distinct_categories(self, org_id: uuid.UUID) -> list[str]:
        stmt = (
            select(Expense.category)
            .where(Expense.org_id == org_id, Expense.deleted_at.is_(None))
            .distinct()
        )
        return list((await self._session.execute(stmt)).scalars())

    async def in_period(
        self, org_id: uuid.UUID, start: datetime.date, end: datetime.date, *, limit: int = 500
    ) -> list[ExpenseRow]:
        """Every expense in the window, newest first, with who paid it.

        Joined to `users` rather than resolved caller-side: three
        partners spend from one cash box, and an expenses page that
        cannot say who spent it answers half the question people
        actually have about it.
        """
        stmt = (
            select(
                Expense.id,
                Expense.expense_date,
                Expense.category,
                Expense.amount,
                Expense.paid_via,
                Expense.description,
                User.full_name,
            )
            .join(User, User.id == Expense.created_by)
            .where(
                Expense.org_id == org_id,
                Expense.deleted_at.is_(None),
                Expense.expense_date >= start,
                Expense.expense_date <= end,
            )
            .order_by(Expense.expense_date.desc(), Expense.created_at.desc())
            .limit(limit)
        )
        return [
            ExpenseRow(
                id=row[0],
                entry_date=row[1],
                category=row[2],
                amount=row[3],
                paid_via=row[4],
                description=row[5],
                spent_by=row[6],
            )
            for row in (await self._session.execute(stmt)).all()
        ]

    async def by_category(
        self, org_id: uuid.UUID, start: datetime.date, end: datetime.date
    ) -> list[tuple[str, decimal.Decimal, int]]:
        """(category, total, count), biggest first -- which is the order
        the question "where is the money going" wants them in."""
        total = func.sum(Expense.amount)
        stmt = (
            select(Expense.category, total, func.count())
            .where(
                Expense.org_id == org_id,
                Expense.deleted_at.is_(None),
                Expense.expense_date >= start,
                Expense.expense_date <= end,
            )
            .group_by(Expense.category)
            .order_by(total.desc())
        )
        return [(row[0], row[1], row[2]) for row in (await self._session.execute(stmt)).all()]


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


async def entry_day(session: AsyncSession, org_id: uuid.UUID, on: str | None) -> datetime.date:
    """The day the money moved, which is not always the day it was
    typed: a ledger copied out of a paper book is entered weeks later,
    and filing those under today would misstate every cash-flow report.

    `on` is still raw text here because "today" can only be resolved
    against the org's own calendar date, which this function is the one
    that knows.
    """
    from backend.core.dates import parse_date
    from backend.core.exceptions import ValidationError

    today = await business_today(session, org_id)
    if on is None:
        return today
    when = parse_date(on, today=today)
    if when > today:
        raise ValidationError(f"{when.strftime('%d-%m-%Y')} is in the future.")
    return when
