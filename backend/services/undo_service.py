"""`undo` -- docs/08_WhatsApp.md #undo, docs/04_Purchases.md §8.

**Compensating entries, never row deletion.** A confirmed purchase that
is undone becomes `cancelled` and gains reversing movements; the
original header, its lines and its movements all stay exactly as they
were. The record of what happened and the record that it was reversed
are both history, and neither is allowed to disappear -- that is the
whole reason `undo` is not implemented as a delete.

A reversal is recorded in `audit_logs` under `<action>.undone`, which is
also what stops the same entry being reversed twice.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid

from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundError, ValidationError
from backend.core.security import role_at_least
from backend.models import (
    AuditLog,
    Expense,
    Income,
    PartnerCapital,
    PurchaseHeader,
    PurchaseLine,
    SalesHeader,
    SalesLine,
    User,
    Warehouse,
)
from backend.models.enums import (
    AccountCode,
    CapitalEntryType,
    LedgerEntryType,
    PurchaseStatus,
    SalePaymentType,
    UserRole,
)
from backend.repositories.accounting_repository import (
    LedgerRepository,
    PartnerCapitalRepository,
    business_today,
)
from backend.repositories.audit_repository import AuditRepository
from backend.repositories.settings_repository import SettingsRepository
from backend.services.audit_service import AuditService
from backend.services.inventory_service import InventoryService
from backend.services.journal_service import JournalService

TWO = decimal.Decimal("0.01")
ZERO = decimal.Decimal("0")


@dataclasses.dataclass(frozen=True)
class UndoResult:
    action: str
    description: str
    #: docs/03_Inventory.md §4 -- a reversal that could not unwind the
    #: weighted average exactly, surfaced rather than hidden
    cost_approximated: bool
    #: undoing a sale whose stock was already re-sold can leave the
    #: balance negative; flagged, never silently allowed to pass
    negative_stock_codes: list[str]


class UndoService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit_repo = AuditRepository(session)
        self._audit = AuditService(session)
        self._settings = SettingsRepository(session)
        self._inventory = InventoryService(session)
        self._ledgers = LedgerRepository(session)
        self._capital = PartnerCapitalRepository(session)
        self._journal = JournalService(session)

    async def _default_warehouse(self, org_id: uuid.UUID) -> Warehouse:
        return (
            await self._session.execute(
                select(Warehouse).where(Warehouse.org_id == org_id, Warehouse.is_default.is_(True))
            )
        ).scalar_one()

    # --- resolution ---------------------------------------------------

    async def _resolve_entry(
        self, actor: User, entity: str | None, reference: str | None
    ) -> AuditLog:
        org_id = actor.org_id
        hours = await self._settings.undo_window_hours(org_id)
        since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=hours)

        if entity is None:
            entry = await self._audit_repo.latest_undoable(org_id, actor.id, since)
            if entry is None:
                raise NotFoundError(
                    "undoable action",
                    f"anything of yours in the last {hours}h",
                )
            return entry

        entity_id = await self._lookup_entity(org_id, entity, reference or "")
        entry = await self._audit_repo.find_action(
            org_id, UNDOABLE_ACTIONS_BY_ENTITY[entity], entity_id
        )
        if entry is None:
            raise NotFoundError("undoable action", reference or entity)
        if entry.created_at < since:
            raise ValidationError(
                f"That was recorded more than {hours}h ago and can no longer be undone. "
                "Record a correcting entry instead."
            )
        return entry

    async def _lookup_entity(self, org_id: uuid.UUID, entity: str, reference: str) -> uuid.UUID:
        if entity == "purchase":
            header = (
                (
                    await self._session.execute(
                        select(PurchaseHeader).where(
                            PurchaseHeader.org_id == org_id,
                            PurchaseHeader.invoice_no.ilike(reference),
                            PurchaseHeader.deleted_at.is_(None),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if header is None:
                raise NotFoundError("purchase invoice", reference)
            return header.id
        if entity == "sale":
            from backend.repositories.party_repository import CustomerRepository

            # A sale's own short ref first. It is what `ledger`, `search`
            # and the delete picker all quote, and it names *one* sale --
            # where a customer name resolves to "their latest", which is
            # a guess at which sale you meant.
            by_ref = (
                (
                    await self._session.execute(
                        select(SalesHeader).where(
                            SalesHeader.org_id == org_id,
                            SalesHeader.deleted_at.is_(None),
                            SalesHeader.status == "confirmed",
                            cast(SalesHeader.id, String).like(f"{reference.lower()}%"),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if by_ref is not None:
                return by_ref.id

            customers = await CustomerRepository(self._session).search(org_id, reference, limit=1)
            if not customers:
                raise NotFoundError("sale", reference)
            sale = (
                (
                    await self._session.execute(
                        select(SalesHeader)
                        .where(
                            SalesHeader.org_id == org_id,
                            SalesHeader.customer_id == customers[0].id,
                            SalesHeader.deleted_at.is_(None),
                            SalesHeader.status == "confirmed",
                        )
                        .order_by(SalesHeader.created_at.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if sale is None:
                raise NotFoundError("recent sale", reference)
            return sale.id
        raise ValidationError(
            f"'{entity}' can't be undone. Try: undo purchase <invoice-no>, "
            "undo sale <ref>, or undo expense <ref>."
        )

    def _check_permission(self, actor: User, entry: AuditLog) -> None:
        """Owner for confirmed transactions; staff only for their own
        entries (docs/08_WhatsApp.md #undo)."""
        if role_at_least(actor.role, UserRole.OWNER):
            return
        if entry.actor_user_id != actor.id:
            raise ValidationError(
                "That entry was recorded by someone else — an owner needs to undo it."
            )

    # --- entry point --------------------------------------------------

    async def undo(
        self,
        actor: User,
        *,
        entity: str | None = None,
        reference: str | None = None,
        whatsapp_message_id: str | None = None,
    ) -> UndoResult:
        org_id = actor.org_id
        async with self._session.begin():
            entry = await self._resolve_entry(actor, entity, reference)
            self._check_permission(actor, entry)
            if await self._audit_repo.was_undone(org_id, entry.entity_id):
                raise ValidationError("That entry has already been undone.")

            today = await business_today(self._session, org_id)
            handlers = {
                "purchase.confirmed": self._undo_purchase,
                "sale.created": self._undo_sale,
                "expense.created": self._undo_expense,
                "income.created": self._undo_income,
                "capital.contribution": self._undo_capital,
                "capital.withdrawal": self._undo_capital,
            }
            result = await handlers[entry.action](actor, entry, today)

            await self._audit.record(
                org_id,
                actor.id,
                action=f"{entry.action}.undone",
                entity_type=entry.entity_type,
                entity_id=entry.entity_id,
                before_state=entry.after_state,
                after_state={"undone": True, "original_action": entry.action},
                whatsapp_message_id=whatsapp_message_id,
            )
            return result

    # --- per-type reversals -------------------------------------------

    async def _undo_purchase(
        self, actor: User, entry: AuditLog, today: datetime.date
    ) -> UndoResult:
        org_id = actor.org_id
        header = await self._session.get(PurchaseHeader, entry.entity_id)
        if header is None:
            raise NotFoundError("purchase", str(entry.entity_id))
        if header.amount_paid > ZERO:
            # money already left; undoing the purchase alone would leave
            # a payment against nothing. The partner settles that first.
            raise ValidationError(
                f"{header.invoice_no} has {header.amount_paid} already paid against it. "
                "Undo would leave that payment orphaned — reverse the payment first, "
                "or record a purchase return instead."
            )

        warehouse = await self._default_warehouse(org_id)
        lines = list(
            (
                await self._session.execute(
                    select(PurchaseLine).where(PurchaseLine.purchase_header_id == header.id)
                )
            ).scalars()
        )
        approximated = False
        for line in lines:
            remaining = line.qty - line.returned_qty
            if remaining <= ZERO:
                continue
            _, was_approx = await self._inventory.record_purchase_return_movement(
                org_id,
                product_id=line.product_id,
                warehouse_id=warehouse.id,
                qty=remaining,
                landed_cost_per_unit=line.landed_cost_per_unit or line.rate,
                source_id=line.id,
                created_by=actor.id,
                reason=f"undo of purchase {header.invoice_no}",
            )
            approximated = approximated or was_approx
            line.returned_qty = line.qty

        # cancelled, not soft-deleted: the record and the fact that it was
        # cancelled both stay visible (docs/04_Purchases.md §8)
        header.status = PurchaseStatus.CANCELLED
        await self._journal.post(
            org_id,
            entry_date=today,
            description=f"undo purchase {header.invoice_no}",
            source_type="purchase_undo",
            source_id=header.id,
            created_by=actor.id,
            debits=[(AccountCode.ACCOUNTS_PAYABLE, header.grand_total)],
            credits=[(AccountCode.INVENTORY, header.grand_total)],
        )
        return UndoResult(
            action="purchase",
            description=f"purchase {header.invoice_no} cancelled",
            cost_approximated=approximated,
            negative_stock_codes=[],
        )

    async def _undo_sale(self, actor: User, entry: AuditLog, today: datetime.date) -> UndoResult:
        org_id = actor.org_id
        header = await self._session.get(SalesHeader, entry.entity_id)
        if header is None:
            raise NotFoundError("sale", str(entry.entity_id))

        warehouse = await self._default_warehouse(org_id)
        lines = list(
            (
                await self._session.execute(
                    select(SalesLine).where(SalesLine.sales_header_id == header.id)
                )
            ).scalars()
        )
        negative: list[str] = []
        cogs_total = ZERO
        for line in lines:
            remaining = line.qty - line.returned_qty
            if remaining <= ZERO:
                continue
            await self._inventory.record_sale_return_movement(
                org_id,
                product_id=line.product_id,
                warehouse_id=warehouse.id,
                qty=remaining,
                avg_cost_at_sale_time=line.avg_cost_at_sale_time,
                source_id=line.id,
                created_by=actor.id,
                reason="undo of sale",
            )
            cogs_total += (remaining * line.avg_cost_at_sale_time).quantize(TWO)
            line.returned_qty = line.qty

        header.status = "cancelled"
        value = header.grand_total

        # A cash/bank sale recorded money as received. If the sale never
        # happened, that receipt never happened either -- unlike a
        # *return*, where goods come back later and whether cash left the
        # drawer is a separate fact. So this one is reversed, not asked.
        if header.payment_type is not SalePaymentType.CREDIT and header.amount_paid > ZERO:
            via = "cash" if header.payment_type is SalePaymentType.CASH else "bank"
            await self._ledgers.append(
                org_id,
                via,
                entry_type=LedgerEntryType.SALE_RECEIPT,
                amount=-header.amount_paid,
                source_type="sale_undo",
                source_id=header.id,
                entry_date=today,
                notes="undo of sale",
                created_by=actor.id,
            )
            credit_account = AccountCode.CASH if via == "cash" else AccountCode.BANK
        else:
            credit_account = AccountCode.ACCOUNTS_RECEIVABLE

        await self._journal.post(
            org_id,
            entry_date=today,
            description="undo sale",
            source_type="sale_undo",
            source_id=header.id,
            created_by=actor.id,
            debits=[(AccountCode.SALES_REVENUE, value), (AccountCode.INVENTORY, cogs_total)],
            credits=[(credit_account, value), (AccountCode.COGS, cogs_total)],
        )

        for line in lines:
            qty_now, _ = await self._inventory.peek(org_id, line.product_id, warehouse.id)
            if qty_now < ZERO:
                negative.append(str(line.product_id))
        return UndoResult(
            action="sale",
            description="sale cancelled",
            cost_approximated=False,
            negative_stock_codes=negative,
        )

    async def _undo_expense(self, actor: User, entry: AuditLog, today: datetime.date) -> UndoResult:
        org_id = actor.org_id
        expense = await self._session.get(Expense, entry.entity_id)
        if expense is None or expense.deleted_at is not None:
            raise NotFoundError("expense", str(entry.entity_id))
        expense.deleted_at = datetime.datetime.now(datetime.UTC)

        if expense.paid_by_partner_id is None:
            await self._ledgers.append(
                org_id,
                expense.paid_via,
                entry_type=LedgerEntryType.EXPENSE,
                amount=expense.amount,  # positive: putting the money back
                source_type="expense_undo",
                source_id=expense.id,
                entry_date=today,
                notes=f"undo of expense: {expense.category}",
                created_by=actor.id,
            )
            credit = AccountCode.CASH if expense.paid_via == "cash" else AccountCode.BANK
        else:
            await self._capital.append(
                org_id,
                expense.paid_by_partner_id,
                entry_type=CapitalEntryType.WITHDRAWAL,
                amount=-expense.amount,
                settled_via=expense.paid_via,
                entry_date=today,
                notes=f"undo of partner-paid expense: {expense.category}",
                created_by=actor.id,
            )
            credit = AccountCode.PARTNER_CAPITAL

        debit_account = (
            AccountCode.FREIGHT_EXPENSE
            if expense.category == "freight"
            else AccountCode.OPERATING_EXPENSES
        )
        await self._journal.post(
            org_id,
            entry_date=today,
            description=f"undo expense: {expense.category}",
            source_type="expense_undo",
            source_id=expense.id,
            created_by=actor.id,
            debits=[(credit, expense.amount)],
            credits=[(debit_account, expense.amount)],
        )
        return UndoResult(
            action="expense",
            description=f"expense {expense.category} ({expense.amount}) reversed",
            cost_approximated=False,
            negative_stock_codes=[],
        )

    async def _undo_income(self, actor: User, entry: AuditLog, today: datetime.date) -> UndoResult:
        org_id = actor.org_id
        income = await self._session.get(Income, entry.entity_id)
        if income is None or income.deleted_at is not None:
            raise NotFoundError("income", str(entry.entity_id))
        income.deleted_at = datetime.datetime.now(datetime.UTC)

        await self._ledgers.append(
            org_id,
            income.received_via,
            entry_type=LedgerEntryType.INCOME,
            amount=-income.amount,
            source_type="income_undo",
            source_id=income.id,
            entry_date=today,
            notes=f"undo of income: {income.category}",
            created_by=actor.id,
        )
        account = AccountCode.CASH if income.received_via == "cash" else AccountCode.BANK
        await self._journal.post(
            org_id,
            entry_date=today,
            description=f"undo income: {income.category}",
            source_type="income_undo",
            source_id=income.id,
            created_by=actor.id,
            debits=[(AccountCode.OTHER_INCOME, income.amount)],
            credits=[(account, income.amount)],
        )
        return UndoResult(
            action="income",
            description=f"income {income.category} ({income.amount}) reversed",
            cost_approximated=False,
            negative_stock_codes=[],
        )

    async def _undo_capital(self, actor: User, entry: AuditLog, today: datetime.date) -> UndoResult:
        org_id = actor.org_id
        row = await self._session.get(PartnerCapital, entry.entity_id)
        if row is None:
            raise NotFoundError("capital entry", str(entry.entity_id))
        if row.status != "posted":
            raise ValidationError(
                "That withdrawal hasn't been approved yet — reject it instead of undoing it."
            )

        via = row.settled_via or "cash"
        # row.amount is already signed; the reversal is its negation
        reversal = -row.amount
        await self._capital.append(
            org_id,
            row.partner_id,
            entry_type=(
                CapitalEntryType.WITHDRAWAL if reversal < ZERO else CapitalEntryType.CONTRIBUTION
            ),
            amount=reversal,
            settled_via=via,
            entry_date=today,
            notes="undo of capital entry",
            created_by=actor.id,
        )
        await self._ledgers.append(
            org_id,
            via,
            entry_type=(
                LedgerEntryType.CAPITAL_OUT if reversal < ZERO else LedgerEntryType.CAPITAL_IN
            ),
            amount=reversal,
            source_type="capital_undo",
            source_id=row.id,
            entry_date=today,
            notes="undo of capital entry",
            created_by=actor.id,
        )
        magnitude = abs(row.amount)
        ledger_account = AccountCode.CASH if via == "cash" else AccountCode.BANK
        if row.amount > ZERO:  # original was a contribution -> reverse it
            debits = [(AccountCode.PARTNER_CAPITAL, magnitude)]
            credits = [(ledger_account, magnitude)]
        else:
            debits = [(ledger_account, magnitude)]
            credits = [(AccountCode.PARTNER_CAPITAL, magnitude)]
        await self._journal.post(
            org_id,
            entry_date=today,
            description="undo capital entry",
            source_type="capital_undo",
            source_id=row.id,
            created_by=actor.id,
            debits=debits,
            credits=credits,
        )
        return UndoResult(
            action="capital",
            description=f"capital entry of {magnitude} reversed",
            cost_approximated=False,
            negative_stock_codes=[],
        )


UNDOABLE_ACTIONS_BY_ENTITY = {
    "purchase": "purchase_headers",
    "sale": "sales_headers",
}
