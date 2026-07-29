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
from typing import Any

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


@dataclasses.dataclass(frozen=True)
class PaymentReversal:
    kind: str
    party_name: str
    amount: decimal.Decimal
    via: str
    ledger_balance: decimal.Decimal
    outstanding_after: decimal.Decimal
    unapplied: list[str]


class PaymentReversalService:
    """Undo a `paid` or `received` -- docs/25_PaymentReversals.md.

    A settlement moves money *and* marks bills as paid. Reversing only
    the ledger would leave those bills still showing settled, and the
    payable would be wrong in the direction that loses money quietly. So
    the allocations are unwound too, off the same bills they were
    applied to, in the same transaction.

    This is why a payment must be reversible on its own rather than as
    part of undoing a bill: the money and the bill are separate events,
    and the person who mis-typed an amount wants only the payment back.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._ledgers = LedgerRepository(session)
        self._journal = JournalService(session)
        self._audit = AuditService(session)

    async def reverse(
        self,
        actor: User,
        *,
        reference: str,
        whatsapp_message_id: str | None = None,
    ) -> PaymentReversal:
        from sqlalchemy import String, cast, select

        from backend.models import AuditLog

        org_id = actor.org_id
        async with self._session.begin():
            entry = (
                (
                    await self._session.execute(
                        select(AuditLog).where(
                            AuditLog.org_id == org_id,
                            AuditLog.action.in_(["payment.paid", "payment.received"]),
                            cast(AuditLog.id, String).like(f"{reference.lower()}%"),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if entry is None:
                raise NotFoundError("payment", reference)
            if entry.after_state and entry.after_state.get("reversed"):
                raise ValidationError("That payment has already been reversed.")

            state = entry.after_state or {}
            amount = decimal.Decimal(str(state.get("amount", "0")))
            via = str(state.get("via", "cash"))
            allocations = state.get("allocations") or []
            is_payment = entry.action == "payment.paid"

            party_name, outstanding_after, unapplied = await self._unapply(
                org_id, entry, allocations, is_payment=is_payment
            )

            # money back the way it came
            ledger_row = await self._ledgers.append(
                org_id,
                via,
                entry_type=(
                    LedgerEntryType.PURCHASE_PAYMENT if is_payment else LedgerEntryType.SALE_RECEIPT
                ),
                amount=amount if is_payment else -amount,
                source_type="payment_reversal",
                source_id=entry.entity_id,
                entry_date=await business_today(self._session, org_id),
                notes=f"reversed: {'paid to' if is_payment else 'received from'} {party_name}",
                created_by=actor.id,
            )
            cash_or_bank = AccountCode.CASH if via == "cash" else AccountCode.BANK
            counter = (
                AccountCode.ACCOUNTS_PAYABLE if is_payment else AccountCode.ACCOUNTS_RECEIVABLE
            )
            await self._journal.post(
                org_id,
                entry_date=ledger_row.entry_date,
                description=f"reversed payment {'to' if is_payment else 'from'} {party_name}",
                source_type="payment_reversal",
                source_id=entry.entity_id,
                created_by=actor.id,
                debits=[(cash_or_bank if is_payment else counter, amount)],
                credits=[(counter if is_payment else cash_or_bank, amount)],
            )

            # stamp the original so it can't be reversed twice
            entry.after_state = {**state, "reversed": True}
            await self._audit.record(
                org_id,
                actor.id,
                action="payment.reversed",
                entity_type=entry.entity_type,
                entity_id=entry.entity_id,
                whatsapp_message_id=whatsapp_message_id,
                before_state={"amount": str(amount), "via": via, "allocations": allocations},
                after_state={"reversed": True},
            )

            return PaymentReversal(
                kind="paid" if is_payment else "received",
                party_name=party_name,
                amount=amount,
                via=via,
                ledger_balance=ledger_row.resulting_balance,
                outstanding_after=outstanding_after,
                unapplied=unapplied,
            )

    async def _unapply(
        self,
        org_id: uuid.UUID,
        entry: Any,
        allocations: list[Any],
        *,
        is_payment: bool,
    ) -> tuple[str, decimal.Decimal, list[str]]:
        """Take the money back off the bills it was applied to.

        Reversing the ledger without this leaves bills marked settled
        that nobody has paid -- the payable understated, which is the
        direction that loses money without anyone noticing.
        """
        from sqlalchemy import func, select

        from backend.models import Customer, PurchaseHeader, SalesHeader

        unapplied: list[str] = []
        if is_payment:
            supplier = await self._session.get(Supplier, entry.entity_id)
            party_name = supplier.name if supplier else "(unknown)"
            for allocation in allocations:
                reference = str(allocation.get("reference", ""))
                applied = decimal.Decimal(str(allocation.get("applied", "0")))
                header = (
                    (
                        await self._session.execute(
                            select(PurchaseHeader).where(
                                PurchaseHeader.org_id == org_id,
                                PurchaseHeader.supplier_id == entry.entity_id,
                                func.lower(PurchaseHeader.invoice_no) == reference.lower(),
                                PurchaseHeader.deleted_at.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if header is None:
                    continue
                header.amount_paid = max(ZERO, header.amount_paid - applied)
                header.payment_status = _status_for(header.amount_paid, header.grand_total)
                unapplied.append(f"{reference} ({applied})")
            outstanding = (
                await self._session.execute(
                    select(
                        func.coalesce(
                            func.sum(PurchaseHeader.grand_total - PurchaseHeader.amount_paid), ZERO
                        )
                    ).where(
                        PurchaseHeader.org_id == org_id,
                        PurchaseHeader.supplier_id == entry.entity_id,
                        PurchaseHeader.deleted_at.is_(None),
                        PurchaseHeader.status == "confirmed",
                    )
                )
            ).scalar_one()
        else:
            customer = await self._session.get(Customer, entry.entity_id)
            party_name = customer.name if customer else "(unknown)"
            for allocation in allocations:
                reference = str(allocation.get("reference", ""))
                applied = decimal.Decimal(str(allocation.get("applied", "0")))
                sale_header = (
                    (
                        await self._session.execute(
                            select(SalesHeader).where(
                                SalesHeader.org_id == org_id,
                                SalesHeader.customer_id == entry.entity_id,
                                cast_text(SalesHeader.id).like(f"{reference.lower()}%"),
                                SalesHeader.deleted_at.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if sale_header is None:
                    continue
                sale_header.amount_paid = max(ZERO, sale_header.amount_paid - applied)
                sale_header.payment_status = _status_for(
                    sale_header.amount_paid, sale_header.grand_total
                )
                unapplied.append(f"{reference} ({applied})")
            outstanding = (
                await self._session.execute(
                    select(
                        func.coalesce(
                            func.sum(SalesHeader.grand_total - SalesHeader.amount_paid), ZERO
                        )
                    ).where(
                        SalesHeader.org_id == org_id,
                        SalesHeader.customer_id == entry.entity_id,
                        SalesHeader.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
        return party_name, decimal.Decimal(outstanding), unapplied


def cast_text(column: Any) -> Any:
    from sqlalchemy import String, cast

    return cast(column, String)


def _status_for(paid: decimal.Decimal, total: decimal.Decimal) -> str:
    if paid <= ZERO:
        return "unpaid"
    return "paid" if paid >= total else "partial"
