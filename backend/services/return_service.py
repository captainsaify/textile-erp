"""Sale and purchase returns -- docs/08_WhatsApp.md #return,
docs/05_Sales.md §6, docs/03_Inventory.md §4.

Two rules drive everything here:

1. **Stock comes back at the cost it left at.** A sale return adds
   stock at `sales_lines.avg_cost_at_sale_time`, not today's average, so
   reversing an old sale can't distort current costing. A purchase
   return unwinds the average using that line's original landed cost.
2. **A ledger movement is never assumed.** Reversing a credit sale
   reduces the receivable, which is bookkeeping. Reversing an
   already-paid cash sale means money physically leaving a drawer --
   only the partner knows whether it did, so the system asks rather
   than posting it (docs/05_Sales.md §6).
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundError, ValidationError
from backend.models import (
    Customer,
    Product,
    PurchaseHeader,
    PurchaseLine,
    SalesHeader,
    SalesLine,
    Supplier,
    User,
    Warehouse,
)
from backend.models.enums import AccountCode, LedgerEntryType, SalePaymentType
from backend.repositories.accounting_repository import LedgerRepository, business_today
from backend.repositories.party_repository import CustomerRepository, SupplierRepository
from backend.services.audit_service import AuditService
from backend.services.inventory_service import InventoryService
from backend.services.journal_service import JournalService

TWO = decimal.Decimal("0.01")
ZERO = decimal.Decimal("0")

#: "last" resolves within this window -- docs/08_WhatsApp.md #return
LAST_LOOKBACK_DAYS = 7
#: returns against older transactions need owner (same doc, permissions)
STAFF_RETURN_WINDOW_HOURS = 24


@dataclasses.dataclass(frozen=True)
class ReturnPreview:
    """Everything needed to either execute now or ask about a refund
    first, without re-resolving anything."""

    kind: str  # 'sale' | 'purchase'
    header_id: uuid.UUID
    line_id: uuid.UUID
    product_id: uuid.UUID
    product_code: str
    unit_code: str
    qty: decimal.Decimal
    unit_cost: decimal.Decimal  # historical: avg_cost_at_sale_time or landed cost
    line_value: decimal.Decimal  # qty * original rate -- what the money side moves
    party_name: str
    transaction_date: datetime.date
    reason: str | None
    #: set for a fully-paid cash/bank sale: the partner must choose
    #: refund-now vs credit-against-next before anything is posted
    needs_refund_choice: bool

    def to_context(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "header_id": str(self.header_id),
            "line_id": str(self.line_id),
            "product_id": str(self.product_id),
            "product_code": self.product_code,
            "unit_code": self.unit_code,
            "qty": str(self.qty),
            "unit_cost": str(self.unit_cost),
            "line_value": str(self.line_value),
            "party_name": self.party_name,
            "transaction_date": self.transaction_date.isoformat(),
            "reason": self.reason,
            "needs_refund_choice": self.needs_refund_choice,
        }

    @classmethod
    def from_context(cls, context: dict[str, Any]) -> ReturnPreview:
        return cls(
            kind=context["kind"],
            header_id=uuid.UUID(context["header_id"]),
            line_id=uuid.UUID(context["line_id"]),
            product_id=uuid.UUID(context["product_id"]),
            product_code=context["product_code"],
            unit_code=context["unit_code"],
            qty=decimal.Decimal(context["qty"]),
            unit_cost=decimal.Decimal(context["unit_cost"]),
            line_value=decimal.Decimal(context["line_value"]),
            party_name=context["party_name"],
            transaction_date=datetime.date.fromisoformat(context["transaction_date"]),
            reason=context["reason"],
            needs_refund_choice=context["needs_refund_choice"],
        )


@dataclasses.dataclass(frozen=True)
class ReturnRecorded:
    kind: str
    product_code: str
    unit_code: str
    qty: decimal.Decimal
    party_name: str
    transaction_date: datetime.date
    line_value: decimal.Decimal
    qty_on_hand_after: decimal.Decimal
    outstanding_after: decimal.Decimal | None
    #: how the money side was settled: 'receivable' | 'payable' |
    #: 'refund_cash' | 'refund_bank' | 'credit_note'
    settlement: str
    #: docs/03_Inventory.md §4: the purchase-return average could not be
    #: unwound exactly and was approximated -- surfaced, never hidden
    cost_approximated: bool


class ReturnService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._inventory = InventoryService(session)
        self._ledgers = LedgerRepository(session)
        self._customers = CustomerRepository(session)
        self._suppliers = SupplierRepository(session)
        self._journal = JournalService(session)
        self._audit = AuditService(session)

    async def _default_warehouse(self, org_id: uuid.UUID) -> Warehouse:
        return (
            await self._session.execute(
                select(Warehouse).where(Warehouse.org_id == org_id, Warehouse.is_default.is_(True))
            )
        ).scalar_one()

    # --- resolution ---------------------------------------------------

    async def _resolve_sale(
        self, org_id: uuid.UUID, reference: str, today: datetime.date
    ) -> SalesHeader:
        stmt = (
            select(SalesHeader)
            .where(
                SalesHeader.org_id == org_id,
                SalesHeader.deleted_at.is_(None),
                SalesHeader.status.in_(["confirmed", "partially_returned"]),
            )
            .order_by(SalesHeader.sale_date.desc(), SalesHeader.created_at.desc())
        )
        if reference.lower() != "last":
            customers = await self._customers.search(org_id, reference, limit=1)
            if not customers:
                raise NotFoundError("customer", reference)
            stmt = stmt.where(SalesHeader.customer_id == customers[0].id)
        else:
            # "last" is only meaningful recently; beyond the window the
            # user is asked to name the transaction (docs/08_WhatsApp.md)
            stmt = stmt.where(
                SalesHeader.sale_date >= today - datetime.timedelta(days=LAST_LOOKBACK_DAYS)
            )
        sale = (await self._session.execute(stmt.limit(1))).scalars().first()
        if sale is None:
            raise NotFoundError(
                "recent sale",
                reference
                if reference.lower() != "last"
                else f"any in the last {LAST_LOOKBACK_DAYS} days",
            )
        return sale

    async def _resolve_purchase(
        self, org_id: uuid.UUID, reference: str, today: datetime.date
    ) -> PurchaseHeader:
        stmt = (
            select(PurchaseHeader)
            .where(
                PurchaseHeader.org_id == org_id,
                PurchaseHeader.deleted_at.is_(None),
                PurchaseHeader.status == "confirmed",
            )
            .order_by(PurchaseHeader.invoice_date.desc(), PurchaseHeader.created_at.desc())
        )
        if reference.lower() != "last":
            stmt = stmt.where(PurchaseHeader.invoice_no.ilike(reference))
        else:
            stmt = stmt.where(
                PurchaseHeader.invoice_date >= today - datetime.timedelta(days=LAST_LOOKBACK_DAYS)
            )
        purchase = (await self._session.execute(stmt.limit(1))).scalars().first()
        if purchase is None:
            raise NotFoundError("purchase invoice", reference)
        return purchase

    async def _product_for_code(self, org_id: uuid.UUID, code: str) -> Product:
        from backend.repositories.product_repository import ProductRepository

        matches = await ProductRepository(session=self._session).list_by_code(org_id, code)
        if not matches:
            raise NotFoundError("product", code)
        if len(matches) > 1:
            labels = ", ".join(p.brand.name if p.brand else "no brand" for p in matches)
            raise ValidationError(
                f"'{code.upper()}' exists under {len(matches)} brands ({labels}). "
                "Returns need the specific product — mention the brand when you record it."
            )
        return matches[0]

    def _check_permission(self, actor: User, transaction_created_at: datetime.datetime) -> None:
        """Staff may reverse their own recent entries; anything older
        needs an owner (docs/08_WhatsApp.md #return)."""
        from backend.core.security import role_at_least
        from backend.models.enums import UserRole

        if role_at_least(actor.role, UserRole.OWNER):
            return
        age = datetime.datetime.now(datetime.UTC) - transaction_created_at
        if age > datetime.timedelta(hours=STAFF_RETURN_WINDOW_HOURS):
            raise ValidationError(
                f"That transaction is older than {STAFF_RETURN_WINDOW_HOURS}h — "
                "an owner needs to record this return."
            )

    # --- preview ------------------------------------------------------

    async def preview(
        self,
        actor: User,
        *,
        kind: str,
        reference: str,
        code: str,
        qty: decimal.Decimal,
        reason: str | None,
    ) -> ReturnPreview:
        if qty <= ZERO:
            raise ValidationError("Return quantity must be greater than zero.")
        org_id = actor.org_id
        today = await business_today(self._session, org_id)
        product = await self._product_for_code(org_id, code)

        if kind == "sale":
            sale = await self._resolve_sale(org_id, reference, today)
            self._check_permission(actor, sale.created_at)
            line = (
                (
                    await self._session.execute(
                        select(SalesLine).where(
                            SalesLine.sales_header_id == sale.id,
                            SalesLine.product_id == product.id,
                        )
                    )
                )
                .scalars()
                .first()
            )
            if line is None:
                raise NotFoundError("item on that sale", code.upper())
            remaining = line.qty - line.returned_qty
            if qty > remaining:
                raise ValidationError(
                    f"Only {remaining} {product.unit.code} of {product.code} is still "
                    f"returnable on that sale ({line.qty} sold, {line.returned_qty} "
                    "already returned)."
                )
            customer = await self._session.get(Customer, sale.customer_id)
            paid_in_full = sale.amount_paid >= sale.grand_total
            needs_choice = sale.payment_type is not SalePaymentType.CREDIT and paid_in_full
            return ReturnPreview(
                kind="sale",
                header_id=sale.id,
                line_id=line.id,
                product_id=product.id,
                product_code=product.code,
                unit_code=product.unit.code,
                qty=qty,
                unit_cost=line.avg_cost_at_sale_time,
                line_value=(qty * line.rate).quantize(TWO),
                party_name=customer.name if customer else "customer",
                transaction_date=sale.sale_date,
                reason=reason,
                needs_refund_choice=needs_choice,
            )

        purchase = await self._resolve_purchase(org_id, reference, today)
        self._check_permission(actor, purchase.created_at)
        line_p = (
            (
                await self._session.execute(
                    select(PurchaseLine).where(
                        PurchaseLine.purchase_header_id == purchase.id,
                        PurchaseLine.product_id == product.id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if line_p is None:
            raise NotFoundError("item on that invoice", code.upper())
        remaining_p = line_p.qty - line_p.returned_qty
        if qty > remaining_p:
            raise ValidationError(
                f"Only {remaining_p} {product.unit.code} of {product.code} is still "
                f"returnable on {purchase.invoice_no} ({line_p.qty} purchased, "
                f"{line_p.returned_qty} already returned)."
            )
        supplier = await self._session.get(Supplier, purchase.supplier_id)
        return ReturnPreview(
            kind="purchase",
            header_id=purchase.id,
            line_id=line_p.id,
            product_id=product.id,
            product_code=product.code,
            unit_code=product.unit.code,
            qty=qty,
            unit_cost=line_p.landed_cost_per_unit or line_p.rate,
            line_value=(qty * line_p.rate).quantize(TWO),
            party_name=supplier.name if supplier else "supplier",
            transaction_date=purchase.invoice_date,
            reason=reason,
            needs_refund_choice=False,
        )

    # --- execution ----------------------------------------------------

    async def execute(
        self,
        actor: User,
        preview: ReturnPreview,
        *,
        settlement: str,
        whatsapp_message_id: str | None = None,
    ) -> ReturnRecorded:
        """`settlement` is 'auto' for the normal path (receivable for a
        sale, payable for a purchase), or one of 'refund_cash',
        'refund_bank', 'credit_note' once the partner has answered the
        refund question."""
        org_id = actor.org_id
        async with self._session.begin():
            today = await business_today(self._session, org_id)
            warehouse = await self._default_warehouse(org_id)
            if preview.kind == "sale":
                return await self._execute_sale_return(
                    actor,
                    preview,
                    settlement=settlement,
                    today=today,
                    warehouse_id=warehouse.id,
                    whatsapp_message_id=whatsapp_message_id,
                )
            return await self._execute_purchase_return(
                actor,
                preview,
                today=today,
                warehouse_id=warehouse.id,
                whatsapp_message_id=whatsapp_message_id,
            )

    async def _execute_sale_return(
        self,
        actor: User,
        preview: ReturnPreview,
        *,
        settlement: str,
        today: datetime.date,
        warehouse_id: uuid.UUID,
        whatsapp_message_id: str | None,
    ) -> ReturnRecorded:
        org_id = actor.org_id
        line = await self._session.get(SalesLine, preview.line_id)
        header = await self._session.get(SalesHeader, preview.header_id)
        if line is None or header is None:
            raise NotFoundError("sale line", str(preview.line_id))
        # re-check under the transaction: the preview may be minutes old
        if preview.qty > line.qty - line.returned_qty:
            raise ValidationError(
                f"{preview.product_code} was returned by someone else in the meantime — "
                "check the sale and try again."
            )

        movement = await self._inventory.record_sale_return_movement(
            org_id,
            product_id=preview.product_id,
            warehouse_id=warehouse_id,
            qty=preview.qty,
            avg_cost_at_sale_time=preview.unit_cost,
            source_id=line.id,
            created_by=actor.id,
            reason=preview.reason,
        )
        line.returned_qty = (line.returned_qty + preview.qty).quantize(decimal.Decimal("0.001"))

        fully_returned = all(
            candidate.returned_qty >= candidate.qty
            for candidate in (
                await self._session.execute(
                    select(SalesLine).where(SalesLine.sales_header_id == header.id)
                )
            ).scalars()
        )
        header.status = "returned" if fully_returned else "partially_returned"

        cogs_reversal = (preview.qty * preview.unit_cost).quantize(TWO)
        value = preview.line_value

        if settlement == "credit_note":
            # goods back, money stays with us as a credit the customer
            # can spend later: receivable falls, nothing leaves the till
            header.amount_paid = (header.amount_paid - value).quantize(TWO)
            credit_account = AccountCode.ACCOUNTS_RECEIVABLE
        elif settlement in {"refund_cash", "refund_bank"}:
            via = "cash" if settlement == "refund_cash" else "bank"
            header.amount_paid = (header.amount_paid - value).quantize(TWO)
            await self._ledgers.append(
                org_id,
                via,
                entry_type=LedgerEntryType.SALE_RECEIPT,
                amount=-value,
                source_type="sale_return",
                source_id=header.id,
                entry_date=today,
                notes=f"refund to {preview.party_name} for {preview.product_code}",
                created_by=actor.id,
            )
            credit_account = AccountCode.CASH if via == "cash" else AccountCode.BANK
        else:
            credit_account = AccountCode.ACCOUNTS_RECEIVABLE

        header.grand_total = (header.grand_total - value).quantize(TWO)
        if header.amount_paid >= header.grand_total:
            header.payment_status = "paid"
        elif header.amount_paid > ZERO:
            header.payment_status = "partial"
        else:
            header.payment_status = "unpaid"

        # docs/06_Accounting.md §3, sale-return row: inventory back at
        # historical cost, revenue and the receivable/cash side reversed
        await self._journal.post(
            org_id,
            entry_date=today,
            description=f"sale return: {preview.product_code} from {preview.party_name}",
            source_type="sale_return",
            source_id=header.id,
            created_by=actor.id,
            debits=[(AccountCode.SALES_REVENUE, value), (AccountCode.INVENTORY, cogs_reversal)],
            credits=[(credit_account, value), (AccountCode.COGS, cogs_reversal)],
        )
        await self._audit.record(
            org_id,
            actor.id,
            action="sale.returned",
            entity_type="sales_lines",
            entity_id=line.id,
            after_state={
                "qty": str(preview.qty),
                "settlement": settlement,
                "value": str(value),
                "header_status": header.status,
            },
            whatsapp_message_id=whatsapp_message_id,
        )
        outstanding = await self._customers.outstanding(org_id, header.customer_id)
        return ReturnRecorded(
            kind="sale",
            product_code=preview.product_code,
            unit_code=preview.unit_code,
            qty=preview.qty,
            party_name=preview.party_name,
            transaction_date=preview.transaction_date,
            line_value=value,
            qty_on_hand_after=movement.resulting_qty_on_hand,
            outstanding_after=outstanding,
            settlement=settlement if settlement != "auto" else "receivable",
            cost_approximated=False,
        )

    async def _execute_purchase_return(
        self,
        actor: User,
        preview: ReturnPreview,
        *,
        today: datetime.date,
        warehouse_id: uuid.UUID,
        whatsapp_message_id: str | None,
    ) -> ReturnRecorded:
        org_id = actor.org_id
        line = await self._session.get(PurchaseLine, preview.line_id)
        header = await self._session.get(PurchaseHeader, preview.header_id)
        if line is None or header is None:
            raise NotFoundError("purchase line", str(preview.line_id))
        if preview.qty > line.qty - line.returned_qty:
            raise ValidationError(
                f"{preview.product_code} was returned by someone else in the meantime — "
                "check the invoice and try again."
            )

        movement, approximated = await self._inventory.record_purchase_return_movement(
            org_id,
            product_id=preview.product_id,
            warehouse_id=warehouse_id,
            qty=preview.qty,
            landed_cost_per_unit=preview.unit_cost,
            source_id=line.id,
            created_by=actor.id,
            reason=preview.reason,
        )
        line.returned_qty = (line.returned_qty + preview.qty).quantize(decimal.Decimal("0.001"))

        value = preview.line_value
        # Unpaid invoices simply owe less. A already-settled invoice would
        # mean a refund from the supplier, which -- like the sale side --
        # is a fact only the partner knows; payable is reduced and the
        # money side is left to `received`/`paid` when it actually moves.
        header.grand_total = (header.grand_total - value).quantize(TWO)
        if header.amount_paid >= header.grand_total:
            header.payment_status = "paid"
        elif header.amount_paid > ZERO:
            header.payment_status = "partial"
        else:
            header.payment_status = "unpaid"

        await self._journal.post(
            org_id,
            entry_date=today,
            description=f"purchase return: {preview.product_code} to {preview.party_name}",
            source_type="purchase_return",
            source_id=line.id,
            created_by=actor.id,
            debits=[(AccountCode.ACCOUNTS_PAYABLE, value)],
            credits=[(AccountCode.INVENTORY, value)],
        )
        await self._audit.record(
            org_id,
            actor.id,
            action="purchase.returned",
            entity_type="purchase_lines",
            entity_id=line.id,
            after_state={
                "qty": str(preview.qty),
                "value": str(value),
                "cost_approximated": approximated,
                "resulting_avg_cost": str(movement.resulting_avg_cost),
            },
            whatsapp_message_id=whatsapp_message_id,
        )
        outstanding = await self._suppliers.outstanding(org_id, header.supplier_id)
        return ReturnRecorded(
            kind="purchase",
            product_code=preview.product_code,
            unit_code=preview.unit_code,
            qty=preview.qty,
            party_name=preview.party_name,
            transaction_date=preview.transaction_date,
            line_value=value,
            qty_on_hand_after=movement.resulting_qty_on_hand,
            outstanding_after=outstanding,
            settlement="payable",
            cost_approximated=approximated,
        )
