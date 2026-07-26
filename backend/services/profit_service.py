"""Profit & Loss -- docs/06_Accounting.md §5.

Computed from the journal's account rollup (the double-entry backbone
that every money-moving service posts to in the same transaction as its
simplified-ledger write -- docs/06_Accounting.md §1), never re-derived
from `expenses`/`income`/`sales_headers` directly. That's what keeps
this figure from quietly drifting out of sync with the simplified
ledgers as new transaction types are added: whatever a service posts to
the journal *is* the P&L, by construction, not by a second calculation
that has to be kept in lockstep by hand.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.enums import AccountCode
from backend.repositories.accounting_repository import JournalRepository

ZERO = decimal.Decimal("0")


@dataclasses.dataclass(frozen=True)
class ProfitReport:
    start: datetime.date
    end: datetime.date
    revenue: decimal.Decimal
    cogs: decimal.Decimal
    gross_profit: decimal.Decimal
    operating_expenses: decimal.Decimal
    other_income: decimal.Decimal
    damage_loss: decimal.Decimal
    net_profit: decimal.Decimal


class ProfitService:
    def __init__(self, session: AsyncSession) -> None:
        self._journal = JournalRepository(session)

    async def calculate(
        self, org_id: uuid.UUID, start: datetime.date, end: datetime.date
    ) -> ProfitReport:
        rollup = await self._journal.account_rollup(org_id, start, end)

        def credit_normal(code: AccountCode) -> decimal.Decimal:
            debit, credit = rollup.get(code.value, (ZERO, ZERO))
            return credit - debit

        def debit_normal(code: AccountCode) -> decimal.Decimal:
            debit, credit = rollup.get(code.value, (ZERO, ZERO))
            return debit - credit

        revenue = credit_normal(AccountCode.SALES_REVENUE)
        cogs = debit_normal(AccountCode.COGS)
        gross_profit = revenue - cogs
        # freight_expense only ever carries standalone freight expense
        # entries here -- purchase freight is capitalized into inventory
        # at confirm time (docs/06_Accounting.md §3), so it never lands
        # in this account and can't double-count.
        operating_expenses = debit_normal(AccountCode.OPERATING_EXPENSES) + debit_normal(
            AccountCode.FREIGHT_EXPENSE
        )
        other_income = credit_normal(AccountCode.OTHER_INCOME)
        damage_loss = debit_normal(AccountCode.DAMAGE_LOSS)
        net_profit = gross_profit - operating_expenses + other_income - damage_loss

        return ProfitReport(
            start=start,
            end=end,
            revenue=revenue,
            cogs=cogs,
            gross_profit=gross_profit,
            operating_expenses=operating_expenses,
            other_income=other_income,
            damage_loss=damage_loss,
            net_profit=net_profit,
        )
