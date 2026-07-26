"""Payments in and out -- `received` (docs/05_Sales.md §7) and `paid`
(docs/04_Purchases.md §9).

FIFO allocation against the oldest outstanding invoice first, with an
explicit `against <ref>` override. Overpayment is never silently
clamped: it's reported and, once confirmed, recorded as an advance.
"""

from __future__ import annotations

import dataclasses
import decimal
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundError, ValidationError
from backend.models import Customer, PurchaseHeader, SalesHeader, Supplier, User
from backend.models.enums import AccountCode, LedgerEntryType
from backend.repositories.accounting_repository import LedgerRepository, business_today
from backend.repositories.party_repository import CustomerRepository, SupplierRepository
from backend.services.audit_service import AuditService
from backend.services.journal_service import JournalService

TWO = decimal.Decimal("0.01")
ZERO = decimal.Decimal("0")


@dataclasses.dataclass(frozen=True)
class Allocation:
    reference: str
    applied: decimal.Decimal
    remaining: decimal.Decimal


@dataclasses.dataclass(frozen=True)
class SettlementResult:
    party_name: str
    amount: decimal.Decimal
    via: str
    allocations: list[Allocation]
    advance: decimal.Decimal
    outstanding_after: decimal.Decimal
    ledger_balance: decimal.Decimal


def _validate_amount(amount: decimal.Decimal) -> decimal.Decimal:
    if amount <= ZERO:
        raise ValidationError("Amount must be greater than zero.")
    if amount != amount.quantize(TWO):
        raise ValidationError("Amount can have at most 2 decimal places.")
    return amount.quantize(TWO)


class SettlementService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._customers = CustomerRepository(session)
        self._suppliers = SupplierRepository(session)
        self._ledgers = LedgerRepository(session)
        self._journal = JournalService(session)
        self._audit = AuditService(session)

    async def _resolve_customer(self, org_id: uuid.UUID, name: str) -> Customer:
        matches = await self._customers.search(org_id, name, limit=1)
        if not matches:
            raise NotFoundError("customer", name)
        return matches[0]

    async def _resolve_supplier(self, org_id: uuid.UUID, name: str) -> Supplier:
        matches = await self._suppliers.search(org_id, name, limit=1)
        if not matches:
            raise NotFoundError("supplier", name)
        return matches[0]

    async def receive_from_customer(
        self,
        actor: User,
        *,
        customer_name: str,
        amount: decimal.Decimal,
        via: str,
        against: str | None = None,
        allow_advance: bool = False,
        whatsapp_message_id: str | None = None,
    ) -> SettlementResult:
        amount = _validate_amount(amount)
        org_id = actor.org_id
        async with self._session.begin():
            customer = await self._resolve_customer(org_id, customer_name)
            today = await business_today(self._session, org_id)

            stmt = (
                select(SalesHeader)
                .where(
                    SalesHeader.org_id == org_id,
                    SalesHeader.customer_id == customer.id,
                    SalesHeader.deleted_at.is_(None),
                    SalesHeader.grand_total > SalesHeader.amount_paid,
                )
                .order_by(SalesHeader.sale_date, SalesHeader.created_at)
            )
            open_sales = list((await self._session.execute(stmt)).scalars())
            if against is not None:
                open_sales = [
                    sale
                    for sale in open_sales
                    if str(sale.id).startswith(against.lower())
                    or (sale.notes or "").lower().find(against.lower()) >= 0
                ]
                if not open_sales:
                    raise NotFoundError("open sale", against)

            allocations, advance = self._allocate(open_sales, amount)
            if advance > ZERO and not allow_advance:
                raise ValidationError(
                    f"This payment exceeds {customer.name}'s outstanding by "
                    f"₹{advance}. Reply 'confirm advance' to record the extra as an "
                    "advance, or send a corrected amount."
                )

            ledger_row = await self._ledgers.append(
                org_id,
                via,
                entry_type=LedgerEntryType.SALE_RECEIPT,
                amount=amount,
                source_type="customer_payment",
                source_id=customer.id,
                entry_date=today,
                notes=f"received from {customer.name}",
                created_by=actor.id,
            )
            await self._journal.post(
                org_id,
                entry_date=today,
                description=f"payment received from {customer.name}",
                source_type="customer_payment",
                source_id=customer.id,
                created_by=actor.id,
                debits=[(AccountCode.CASH if via == "cash" else AccountCode.BANK, amount)],
                credits=[(AccountCode.ACCOUNTS_RECEIVABLE, amount)],
            )
            await self._audit.record(
                org_id,
                actor.id,
                action="payment.received",
                entity_type="customers",
                entity_id=customer.id,
                after_state={
                    "amount": str(amount),
                    "via": via,
                    "allocations": [
                        {"reference": a.reference, "applied": str(a.applied)} for a in allocations
                    ],
                    "advance": str(advance),
                },
                whatsapp_message_id=whatsapp_message_id,
            )
            outstanding_after = await self._customers.outstanding(org_id, customer.id)

        return SettlementResult(
            party_name=customer.name,
            amount=amount,
            via=via,
            allocations=allocations,
            advance=advance,
            outstanding_after=outstanding_after,
            ledger_balance=ledger_row.resulting_balance,
        )

    async def pay_supplier(
        self,
        actor: User,
        *,
        supplier_name: str,
        amount: decimal.Decimal,
        via: str,
        against: str | None = None,
        allow_advance: bool = False,
        whatsapp_message_id: str | None = None,
    ) -> SettlementResult:
        amount = _validate_amount(amount)
        org_id = actor.org_id
        async with self._session.begin():
            supplier = await self._resolve_supplier(org_id, supplier_name)
            today = await business_today(self._session, org_id)

            stmt = (
                select(PurchaseHeader)
                .where(
                    PurchaseHeader.org_id == org_id,
                    PurchaseHeader.supplier_id == supplier.id,
                    PurchaseHeader.deleted_at.is_(None),
                    PurchaseHeader.grand_total > PurchaseHeader.amount_paid,
                )
                .order_by(PurchaseHeader.invoice_date, PurchaseHeader.created_at)
            )
            open_purchases = list((await self._session.execute(stmt)).scalars())
            if against is not None:
                open_purchases = [
                    purchase
                    for purchase in open_purchases
                    if purchase.invoice_no.lower() == against.lower()
                ]
                if not open_purchases:
                    raise NotFoundError("open invoice", against)

            allocations, advance = self._allocate(open_purchases, amount)
            if advance > ZERO and not allow_advance:
                raise ValidationError(
                    f"This payment would exceed what's owed to {supplier.name} by "
                    f"₹{advance}. Reply 'confirm advance' to record the extra as an "
                    "advance, or send a corrected amount."
                )

            ledger_row = await self._ledgers.append(
                org_id,
                via,
                entry_type=LedgerEntryType.PURCHASE_PAYMENT,
                amount=-amount,
                source_type="supplier_payment",
                source_id=supplier.id,
                entry_date=today,
                notes=f"paid to {supplier.name}",
                created_by=actor.id,
            )
            await self._journal.post(
                org_id,
                entry_date=today,
                description=f"payment to {supplier.name}",
                source_type="supplier_payment",
                source_id=supplier.id,
                created_by=actor.id,
                debits=[(AccountCode.ACCOUNTS_PAYABLE, amount)],
                credits=[(AccountCode.CASH if via == "cash" else AccountCode.BANK, amount)],
            )
            await self._audit.record(
                org_id,
                actor.id,
                action="payment.paid",
                entity_type="suppliers",
                entity_id=supplier.id,
                after_state={
                    "amount": str(amount),
                    "via": via,
                    "allocations": [
                        {"reference": a.reference, "applied": str(a.applied)} for a in allocations
                    ],
                    "advance": str(advance),
                },
                whatsapp_message_id=whatsapp_message_id,
            )
            outstanding_after = sum(
                (
                    header.grand_total - header.amount_paid
                    for header in await self._open_purchases(org_id, supplier.id)
                ),
                ZERO,
            )

        return SettlementResult(
            party_name=supplier.name,
            amount=amount,
            via=via,
            allocations=allocations,
            advance=advance,
            outstanding_after=outstanding_after,
            ledger_balance=ledger_row.resulting_balance,
        )

    async def _open_purchases(
        self, org_id: uuid.UUID, supplier_id: uuid.UUID
    ) -> list[PurchaseHeader]:
        return list(
            (
                await self._session.execute(
                    select(PurchaseHeader).where(
                        PurchaseHeader.org_id == org_id,
                        PurchaseHeader.supplier_id == supplier_id,
                        PurchaseHeader.deleted_at.is_(None),
                        PurchaseHeader.grand_total > PurchaseHeader.amount_paid,
                    )
                )
            ).scalars()
        )

    def _allocate(
        self,
        headers: list[SalesHeader] | list[PurchaseHeader],
        amount: decimal.Decimal,
    ) -> tuple[list[Allocation], decimal.Decimal]:
        """FIFO across open invoices; leftover becomes an advance."""
        remaining = amount
        allocations: list[Allocation] = []
        for header in headers:
            if remaining <= ZERO:
                break
            outstanding = header.grand_total - header.amount_paid
            applied = min(outstanding, remaining)
            header.amount_paid = (header.amount_paid + applied).quantize(TWO)
            header.payment_status = (
                "paid" if header.amount_paid >= header.grand_total else "partial"
            )
            remaining -= applied
            reference = getattr(header, "invoice_no", None) or str(header.id)[:8]
            allocations.append(
                Allocation(
                    reference=reference,
                    applied=applied,
                    remaining=(header.grand_total - header.amount_paid),
                )
            )
        return allocations, remaining
