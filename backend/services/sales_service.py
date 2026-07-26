"""Sale recording -- docs/05_Sales.md.

Sales auto-confirm (§3): a mistaken sale is cheaply reversible via
`undo`, and confirming every one would slow the highest-frequency
command in the system. Warnings (below cost §4, credit limit §8,
insufficient stock docs/03_Inventory.md §3, near-duplicate §5) are
collected pre-flight and raised together so the user answers once.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import hashlib
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import DuplicateSaleError, ValidationError
from backend.models import Customer, SalesHeader, SalesLine, User, Warehouse
from backend.models.enums import AccountCode, LedgerEntryType, SalePaymentType
from backend.repositories.accounting_repository import LedgerRepository, business_today
from backend.repositories.party_repository import CustomerRepository
from backend.repositories.product_repository import ProductRepository
from backend.services.audit_service import AuditService
from backend.services.inventory_service import InventoryService
from backend.services.journal_service import JournalService

TWO = decimal.Decimal("0.01")
FOUR = decimal.Decimal("0.0001")
ZERO = decimal.Decimal("0")

BELOW_COST_TOLERANCE = decimal.Decimal("0")  # §4 default: any sale below cost warns
DEDUP_WINDOW_MINUTES = 10  # §5 settings.sale_dedup_window_minutes
CUSTOMER_MATCH_THRESHOLD = 80  # §9 fuzzy >= 0.8

_WHITESPACE = re.compile(r"\s+")


def idempotency_key(sender_number: str, message_text: str) -> str:
    """sha256(sender + normalized text) -- §5."""
    normalized = _WHITESPACE.sub(" ", message_text.strip().lower())
    return hashlib.sha256(f"{sender_number}|{normalized}".encode()).hexdigest()


@dataclasses.dataclass
class SaleDraftLine:
    code: str
    qty: decimal.Decimal
    rate: decimal.Decimal
    product_id: uuid.UUID | None = None
    resolved_code: str | None = None
    unit_code: str | None = None
    avg_cost: decimal.Decimal = ZERO
    qty_on_hand: decimal.Decimal = ZERO

    @property
    def line_total(self) -> decimal.Decimal:
        return (self.qty * self.rate).quantize(TWO)


@dataclasses.dataclass
class SaleDraft:
    customer_id: uuid.UUID | None
    customer_name: str
    payment_type: SalePaymentType
    lines: list[SaleDraftLine]
    idempotency_key: str | None = None
    allow_negative_stock: bool = False
    warnings_acknowledged: bool = False

    @property
    def grand_total(self) -> decimal.Decimal:
        return sum((line.line_total for line in self.lines), ZERO)

    @property
    def total_cogs(self) -> decimal.Decimal:
        return sum(((line.qty * line.avg_cost).quantize(TWO) for line in self.lines), ZERO)

    @property
    def unresolved_codes(self) -> list[str]:
        return [line.code for line in self.lines if line.product_id is None]

    def to_context(self) -> dict[str, Any]:
        return {
            "customer_id": str(self.customer_id) if self.customer_id else None,
            "customer_name": self.customer_name,
            "payment_type": self.payment_type.value,
            "idempotency_key": self.idempotency_key,
            "allow_negative_stock": self.allow_negative_stock,
            "warnings_acknowledged": self.warnings_acknowledged,
            "lines": [
                {
                    "code": line.code,
                    "qty": str(line.qty),
                    "rate": str(line.rate),
                    "product_id": str(line.product_id) if line.product_id else None,
                    "resolved_code": line.resolved_code,
                    "unit_code": line.unit_code,
                    "avg_cost": str(line.avg_cost),
                    "qty_on_hand": str(line.qty_on_hand),
                }
                for line in self.lines
            ],
        }

    @classmethod
    def from_context(cls, context: dict[str, Any]) -> SaleDraft:
        return cls(
            customer_id=uuid.UUID(context["customer_id"]) if context["customer_id"] else None,
            customer_name=context["customer_name"],
            payment_type=SalePaymentType(context["payment_type"]),
            idempotency_key=context.get("idempotency_key"),
            allow_negative_stock=context.get("allow_negative_stock", False),
            warnings_acknowledged=context.get("warnings_acknowledged", False),
            lines=[
                SaleDraftLine(
                    code=line["code"],
                    qty=decimal.Decimal(line["qty"]),
                    rate=decimal.Decimal(line["rate"]),
                    product_id=uuid.UUID(line["product_id"]) if line["product_id"] else None,
                    resolved_code=line["resolved_code"],
                    unit_code=line["unit_code"],
                    avg_cost=decimal.Decimal(line["avg_cost"]),
                    qty_on_hand=decimal.Decimal(line["qty_on_hand"]),
                )
                for line in context["lines"]
            ],
        )


@dataclasses.dataclass(frozen=True)
class SaleWarnings:
    insufficient_stock: list[SaleDraftLine]
    below_cost: list[SaleDraftLine]
    credit_limit: tuple[decimal.Decimal, decimal.Decimal] | None  # (limit, projected)
    near_duplicate: SalesHeader | None

    @property
    def any(self) -> bool:
        return bool(
            self.insufficient_stock or self.below_cost or self.credit_limit or self.near_duplicate
        )

    @property
    def blocks_without_override(self) -> bool:
        return bool(self.insufficient_stock)


@dataclasses.dataclass(frozen=True)
class ConfirmedSaleLine:
    code: str
    qty: decimal.Decimal
    rate: decimal.Decimal
    line_total: decimal.Decimal
    unit_code: str
    resulting_qty: decimal.Decimal


@dataclasses.dataclass(frozen=True)
class ConfirmedSale:
    customer_name: str
    payment_type: SalePaymentType
    lines: list[ConfirmedSaleLine]
    grand_total: decimal.Decimal
    outstanding_before: decimal.Decimal
    outstanding_after: decimal.Decimal
    ledger_balance: decimal.Decimal | None


class SalesService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._customers = CustomerRepository(session)
        self._products = ProductRepository(session)
        self._inventory = InventoryService(session)
        self._ledgers = LedgerRepository(session)
        self._journal = JournalService(session)
        self._audit = AuditService(session)

    async def resolve_customer(self, org_id: uuid.UUID, name: str) -> list[Customer]:
        """Returns [] (none), [one] (resolved), or several when the match
        is ambiguous -- never auto-picked (docs/05_Sales.md §10)."""
        from rapidfuzz import fuzz

        candidates = await self._customers.search(org_id, name, limit=5)
        exact = [c for c in candidates if c.name.lower() == name.lower()]
        if exact:
            return exact[:1]
        scored = [
            (c, fuzz.ratio(c.name.lower(), name.lower()))
            for c in candidates
            if fuzz.ratio(c.name.lower(), name.lower()) >= CUSTOMER_MATCH_THRESHOLD
        ]
        if not scored:
            return []
        best = max(score for _, score in scored)
        tied = [c for c, score in scored if score == best]
        return tied if len(tied) > 1 else [tied[0]]

    async def create_customer(self, actor: User, name: str) -> Customer:
        customer = Customer(org_id=actor.org_id, name=name, created_by=actor.id)
        self._session.add(customer)
        await self._session.flush()
        await self._audit.record(
            actor.org_id,
            actor.id,
            action="customer.created",
            entity_type="customers",
            entity_id=customer.id,
            after_state={"name": name},
        )
        return customer

    async def _default_warehouse(self, org_id: uuid.UUID) -> Warehouse:
        return (
            await self._session.execute(
                select(Warehouse).where(Warehouse.org_id == org_id, Warehouse.is_default.is_(True))
            )
        ).scalar_one()

    async def hydrate(self, org_id: uuid.UUID, draft: SaleDraft) -> SaleDraft:
        """Resolve products and snapshot current stock/cost for warnings."""
        warehouse = await self._default_warehouse(org_id)
        for line in draft.lines:
            if line.product_id is None:
                product = await self._products.get_by_code(org_id, line.code)
                if product is None:
                    matches = await self._products.search(org_id, line.code, limit=1)
                    product = matches[0] if matches else None
                if product is not None:
                    line.product_id = product.id
                    line.resolved_code = product.code
                    line.unit_code = product.unit.code
            if line.product_id is not None:
                qty_on_hand, avg_cost = await self._inventory.peek(
                    org_id, line.product_id, warehouse.id
                )
                line.qty_on_hand = qty_on_hand
                line.avg_cost = avg_cost
        return draft

    async def check_warnings(self, org_id: uuid.UUID, draft: SaleDraft) -> SaleWarnings:
        insufficient = [
            line
            for line in draft.lines
            if line.product_id is not None and line.qty > line.qty_on_hand
        ]
        below_cost = [
            line
            for line in draft.lines
            if line.product_id is not None
            and line.avg_cost > ZERO
            and line.rate < line.avg_cost * (1 - BELOW_COST_TOLERANCE)
        ]

        credit_limit: tuple[decimal.Decimal, decimal.Decimal] | None = None
        if draft.payment_type is SalePaymentType.CREDIT and draft.customer_id is not None:
            customer = await self._session.get(Customer, draft.customer_id)
            if customer is not None and customer.credit_limit is not None:
                outstanding = await self._customers.outstanding(org_id, draft.customer_id)
                projected = outstanding + draft.grand_total
                if projected > customer.credit_limit:
                    credit_limit = (customer.credit_limit, projected)

        near_duplicate: SalesHeader | None = None
        if draft.customer_id is not None:
            since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
                minutes=DEDUP_WINDOW_MINUTES
            )
            recent = list(
                (
                    await self._session.execute(
                        select(SalesHeader).where(
                            SalesHeader.org_id == org_id,
                            SalesHeader.customer_id == draft.customer_id,
                            SalesHeader.deleted_at.is_(None),
                            SalesHeader.created_at >= since,
                        )
                    )
                ).scalars()
            )
            near_duplicate = next(
                (
                    header
                    for header in recent
                    if header.grand_total == draft.grand_total
                    and header.idempotency_key != draft.idempotency_key
                ),
                None,
            )

        return SaleWarnings(
            insufficient_stock=insufficient,
            below_cost=below_cost,
            credit_limit=credit_limit,
            near_duplicate=near_duplicate,
        )

    def validate(self, draft: SaleDraft) -> None:
        if not draft.lines:
            raise ValidationError("Send at least one item line.")
        if draft.customer_id is None:
            raise ValidationError(f"Customer '{draft.customer_name}' is not resolved yet.")
        if draft.unresolved_codes:
            raise ValidationError("Unresolved products: " + ", ".join(draft.unresolved_codes))
        for line in draft.lines:
            if line.qty <= ZERO:
                raise ValidationError(f"Quantity for {line.code} must be greater than zero.")
            if line.rate < ZERO:
                raise ValidationError(f"Rate for {line.code} can't be negative.")

    async def find_by_idempotency_key(self, org_id: uuid.UUID, key: str) -> SalesHeader | None:
        since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            minutes=DEDUP_WINDOW_MINUTES
        )
        return (
            await self._session.execute(
                select(SalesHeader).where(
                    SalesHeader.org_id == org_id,
                    SalesHeader.idempotency_key == key,
                    SalesHeader.deleted_at.is_(None),
                    SalesHeader.created_at >= since,
                )
            )
        ).scalar_one_or_none()

    async def record(
        self,
        actor: User,
        draft: SaleDraft,
        *,
        below_cost_confirmed: bool = False,
        whatsapp_message_id: str | None = None,
    ) -> ConfirmedSale:
        org_id = actor.org_id
        async with self._session.begin():
            self.validate(draft)
            if draft.idempotency_key is not None:
                existing = await self.find_by_idempotency_key(org_id, draft.idempotency_key)
                if existing is not None:
                    raise DuplicateSaleError(
                        "identical sale already recorded",
                        details={"sales_header_id": str(existing.id)},
                    )

            today = await business_today(self._session, org_id)
            warehouse = await self._default_warehouse(org_id)
            assert draft.customer_id is not None
            outstanding_before = await self._customers.outstanding(org_id, draft.customer_id)

            paid_immediately = draft.payment_type is not SalePaymentType.CREDIT
            header = SalesHeader(
                org_id=org_id,
                customer_id=draft.customer_id,
                warehouse_id=warehouse.id,
                sale_date=today,
                payment_type=draft.payment_type,
                subtotal=draft.grand_total,
                grand_total=draft.grand_total,
                amount_paid=draft.grand_total if paid_immediately else ZERO,
                payment_status="paid" if paid_immediately else "unpaid",
                status="confirmed",
                idempotency_key=draft.idempotency_key,
                created_by=actor.id,
            )
            self._session.add(header)
            try:
                await self._session.flush()
            except IntegrityError as exc:  # concurrent identical resend
                raise DuplicateSaleError("identical sale already recorded", details={}) from exc

            confirmed_lines: list[ConfirmedSaleLine] = []
            for index, line in enumerate(draft.lines):
                assert line.product_id is not None
                row = SalesLine(
                    org_id=org_id,
                    sales_header_id=header.id,
                    line_no=index + 1,
                    product_id=line.product_id,
                    qty=line.qty,
                    rate=line.rate,
                    line_total=line.line_total,
                    # snapshot for margin reporting and return costing (§3)
                    avg_cost_at_sale_time=line.avg_cost,
                )
                self._session.add(row)
                await self._session.flush()
                movement = await self._inventory.record_sale_movement(
                    org_id,
                    product_id=line.product_id,
                    product_code=line.resolved_code or line.code,
                    warehouse_id=warehouse.id,
                    qty=line.qty,
                    source_id=row.id,
                    created_by=actor.id,
                    allow_negative=draft.allow_negative_stock,
                    unit_code=line.unit_code or "",
                )
                confirmed_lines.append(
                    ConfirmedSaleLine(
                        code=line.resolved_code or line.code,
                        qty=line.qty,
                        rate=line.rate,
                        line_total=line.line_total,
                        unit_code=line.unit_code or "KG",
                        resulting_qty=movement.resulting_qty_on_hand,
                    )
                )

            ledger_balance: decimal.Decimal | None = None
            if paid_immediately:
                ledger = "cash" if draft.payment_type is SalePaymentType.CASH else "bank"
                row_ledger = await self._ledgers.append(
                    org_id,
                    ledger,
                    entry_type=LedgerEntryType.SALE_RECEIPT,
                    amount=draft.grand_total,
                    source_type="sales_header",
                    source_id=header.id,
                    entry_date=today,
                    notes=f"sale to {draft.customer_name}",
                    created_by=actor.id,
                )
                ledger_balance = row_ledger.resulting_balance
                money_account = (
                    AccountCode.CASH
                    if draft.payment_type is SalePaymentType.CASH
                    else AccountCode.BANK
                )
            else:
                money_account = AccountCode.ACCOUNTS_RECEIVABLE

            # revenue + COGS in one balanced entry -- docs/06_Accounting.md §3
            cogs = draft.total_cogs
            debits = [(money_account, draft.grand_total)]
            credits = [(AccountCode.SALES_REVENUE, draft.grand_total)]
            if cogs > ZERO:
                debits.append((AccountCode.COGS, cogs))
                credits.append((AccountCode.INVENTORY, cogs))
            await self._journal.post(
                org_id,
                entry_date=today,
                description=f"sale to {draft.customer_name}",
                source_type="sales_header",
                source_id=header.id,
                created_by=actor.id,
                debits=debits,
                credits=credits,
            )
            await self._audit.record(
                org_id,
                actor.id,
                action="sale.created",
                entity_type="sales_headers",
                entity_id=header.id,
                after_state={
                    "customer_id": str(draft.customer_id),
                    "payment_type": draft.payment_type.value,
                    "grand_total": str(draft.grand_total),
                    "lines": len(draft.lines),
                    "below_cost_confirmed": below_cost_confirmed,
                    "negative_stock_override": draft.allow_negative_stock,
                },
                whatsapp_message_id=whatsapp_message_id,
            )
            outstanding_after = (
                outstanding_before if paid_immediately else outstanding_before + draft.grand_total
            )

        return ConfirmedSale(
            customer_name=draft.customer_name,
            payment_type=draft.payment_type,
            lines=confirmed_lines,
            grand_total=draft.grand_total,
            outstanding_before=outstanding_before,
            outstanding_after=outstanding_after,
            ledger_balance=ledger_balance,
        )
