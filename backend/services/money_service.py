"""expense / income recording -- docs/08_WhatsApp.md #expense/#income,
postings per docs/06_Accounting.md §3, partner-paid expenses per §13.

Each public method is one use case in one transaction: business row +
simplified ledger + journal + audit, all-or-nothing.
"""

from __future__ import annotations

import dataclasses
import decimal
import difflib

from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundError, ValidationError
from backend.models import Expense, Income, User
from backend.models.enums import AccountCode, CapitalEntryType, LedgerEntryType
from backend.repositories.accounting_repository import (
    ExpenseRepository,
    IncomeRepository,
    LedgerRepository,
    PartnerCapitalRepository,
    business_today,
)
from backend.repositories.party_repository import PartnerRepository
from backend.services.audit_service import AuditService
from backend.services.journal_service import JournalService

TWO_PLACES = decimal.Decimal("0.01")


@dataclasses.dataclass(frozen=True)
class MoneyRecorded:
    category: str
    amount: decimal.Decimal
    via: str
    new_balance: decimal.Decimal  # ledger balance, or partner capital when partner-paid
    paid_by_partner: str | None
    similar_category: str | None


def _validate_amount(raw: decimal.Decimal) -> decimal.Decimal:
    if raw <= 0:
        raise ValidationError("Amount must be greater than zero.")
    if raw != raw.quantize(TWO_PLACES):
        raise ValidationError("Amount can have at most 2 decimal places.")
    return raw.quantize(TWO_PLACES)


def _similar(category: str, existing: list[str]) -> str | None:
    others = [c for c in existing if c != category]
    matches = difflib.get_close_matches(category, others, n=1, cutoff=0.75)
    return matches[0] if matches else None


class MoneyService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._ledgers = LedgerRepository(session)
        self._capital = PartnerCapitalRepository(session)
        self._expenses = ExpenseRepository(session)
        self._income = IncomeRepository(session)
        self._partners = PartnerRepository(session)
        self._journal = JournalService(session)
        self._audit = AuditService(session)

    async def record_expense(
        self,
        actor: User,
        *,
        category: str,
        amount: decimal.Decimal,
        via: str,
        description: str | None,
        paid_by_partner_name: str | None = None,
        whatsapp_message_id: str | None = None,
    ) -> MoneyRecorded:
        amount = _validate_amount(amount)
        category = category.strip().lower()
        org_id = actor.org_id

        async with self._session.begin():
            partner = None
            if paid_by_partner_name:
                partner = await self._partners.get_by_display_name(org_id, paid_by_partner_name)
                if partner is None:
                    raise NotFoundError("partner", paid_by_partner_name)

            today = await business_today(self._session, org_id)
            existing_categories = await self._expenses.distinct_categories(org_id)

            expense = await self._expenses.insert(
                Expense(
                    org_id=org_id,
                    category=category,
                    amount=amount,
                    paid_via=via,
                    paid_by_partner_id=partner.id if partner else None,
                    expense_date=today,
                    description=description,
                    created_by=actor.id,
                )
            )

            if partner is None:
                ledger_row = await self._ledgers.append(
                    org_id,
                    via,
                    entry_type=LedgerEntryType.EXPENSE,
                    amount=-amount,
                    source_type="expense",
                    source_id=expense.id,
                    entry_date=today,
                    notes=f"{category}" + (f" — {description}" if description else ""),
                    created_by=actor.id,
                )
                new_balance = ledger_row.resulting_balance
                credit_account = AccountCode.CASH if via == "cash" else AccountCode.BANK
            else:
                # paid personally by a partner: business cash/bank untouched;
                # implicit capital contribution -- docs/06_Accounting.md §13
                capital_row = await self._capital.append(
                    org_id,
                    partner.id,
                    entry_type=CapitalEntryType.CONTRIBUTION,
                    amount=amount,
                    settled_via=via,
                    entry_date=today,
                    notes=f"paid expense: {category}",
                    created_by=actor.id,
                )
                new_balance = capital_row.resulting_balance
                credit_account = AccountCode.PARTNER_CAPITAL

            debit_account = (
                AccountCode.FREIGHT_EXPENSE
                if category == "freight"
                else AccountCode.OPERATING_EXPENSES
            )
            await self._journal.post(
                org_id,
                entry_date=today,
                description=f"expense: {category}",
                source_type="expense",
                source_id=expense.id,
                created_by=actor.id,
                debits=[(debit_account, amount)],
                credits=[(credit_account, amount)],
            )
            await self._audit.record(
                org_id,
                actor.id,
                action="expense.created",
                entity_type="expenses",
                entity_id=expense.id,
                after_state={
                    "category": category,
                    "amount": str(amount),
                    "paid_via": via,
                    "paid_by_partner_id": str(partner.id) if partner else None,
                    "description": description,
                },
                whatsapp_message_id=whatsapp_message_id,
            )

        return MoneyRecorded(
            category=category,
            amount=amount,
            via=via,
            new_balance=new_balance,
            paid_by_partner=partner.display_name if partner else None,
            similar_category=_similar(category, existing_categories),
        )

    async def record_income(
        self,
        actor: User,
        *,
        category: str,
        amount: decimal.Decimal,
        via: str,
        description: str | None,
        whatsapp_message_id: str | None = None,
    ) -> MoneyRecorded:
        amount = _validate_amount(amount)
        category = category.strip().lower()
        org_id = actor.org_id

        async with self._session.begin():
            today = await business_today(self._session, org_id)
            existing_categories = await self._income.distinct_categories(org_id)

            income = await self._income.insert(
                Income(
                    org_id=org_id,
                    category=category,
                    amount=amount,
                    received_via=via,
                    income_date=today,
                    description=description,
                    created_by=actor.id,
                )
            )
            ledger_row = await self._ledgers.append(
                org_id,
                via,
                entry_type=LedgerEntryType.INCOME,
                amount=amount,
                source_type="income",
                source_id=income.id,
                entry_date=today,
                notes=f"{category}" + (f" — {description}" if description else ""),
                created_by=actor.id,
            )
            await self._journal.post(
                org_id,
                entry_date=today,
                description=f"income: {category}",
                source_type="income",
                source_id=income.id,
                created_by=actor.id,
                debits=[(AccountCode.CASH if via == "cash" else AccountCode.BANK, amount)],
                credits=[(AccountCode.OTHER_INCOME, amount)],
            )
            await self._audit.record(
                org_id,
                actor.id,
                action="income.created",
                entity_type="income",
                entity_id=income.id,
                after_state={
                    "category": category,
                    "amount": str(amount),
                    "received_via": via,
                    "description": description,
                },
                whatsapp_message_id=whatsapp_message_id,
            )

        return MoneyRecorded(
            category=category,
            amount=amount,
            via=via,
            new_balance=ledger_row.resulting_balance,
            paid_by_partner=None,
            similar_category=_similar(category, existing_categories),
        )
