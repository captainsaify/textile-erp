"""Cross-cutting aggregates for `dashboard`/`summary`
(docs/12_Dashboard.md, docs/13_Reports.md) that don't belong to any
single aggregate's own repository: period purchase/sale totals,
org-wide receivables/payables, top sellers, slow-moving stock.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    Customer,
    Inventory,
    InventoryMovement,
    Product,
    PurchaseHeader,
    SalesHeader,
    SalesLine,
    Setting,
    Supplier,
)
from backend.models.enums import MovementType

ZERO = decimal.Decimal("0")
DEFAULT_SLOW_MOVING_DAYS = 60
_RECEIVABLE_STATUSES = ("confirmed", "partially_returned", "returned")


@dataclasses.dataclass(frozen=True)
class TopSeller:
    code: str
    description: str
    revenue: decimal.Decimal


@dataclasses.dataclass(frozen=True)
class SlowMover:
    code: str
    description: str
    days_since_sale: int | None  # None = never sold


class ReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def purchases_total(
        self, org_id: uuid.UUID, start: datetime.date, end: datetime.date
    ) -> decimal.Decimal:
        """Gross purchase total for the period -- distinct from
        ProfitService's revenue/COGS, which read the journal instead."""
        stmt = select(func.coalesce(func.sum(PurchaseHeader.grand_total), ZERO)).where(
            PurchaseHeader.org_id == org_id,
            PurchaseHeader.deleted_at.is_(None),
            PurchaseHeader.status == "confirmed",
            PurchaseHeader.invoice_date >= start,
            PurchaseHeader.invoice_date <= end,
        )
        return decimal.Decimal((await self._session.execute(stmt)).scalar_one())

    async def sales_total(
        self, org_id: uuid.UUID, start: datetime.date, end: datetime.date
    ) -> decimal.Decimal:
        stmt = select(func.coalesce(func.sum(SalesHeader.grand_total), ZERO)).where(
            SalesHeader.org_id == org_id,
            SalesHeader.deleted_at.is_(None),
            SalesHeader.status.in_(["confirmed", "partially_returned"]),
            SalesHeader.sale_date >= start,
            SalesHeader.sale_date <= end,
        )
        return decimal.Decimal((await self._session.execute(stmt)).scalar_one())

    async def receivables_total(self, org_id: uuid.UUID) -> tuple[decimal.Decimal, int]:
        """(total outstanding across all customers, count of customers
        with a nonzero balance) -- one grouped query rather than N+1
        calls to CustomerRepository.outstanding()."""
        unpaid = func.coalesce(
            func.sum(
                case(
                    (
                        SalesHeader.grand_total > SalesHeader.amount_paid,
                        SalesHeader.grand_total - SalesHeader.amount_paid,
                    ),
                    else_=ZERO,
                )
            ),
            ZERO,
        )
        per_customer = (
            select((Customer.opening_balance + unpaid).label("outstanding"))
            .select_from(Customer)
            .outerjoin(
                SalesHeader,
                (SalesHeader.customer_id == Customer.id)
                & (SalesHeader.deleted_at.is_(None))
                & (SalesHeader.status.in_(_RECEIVABLE_STATUSES)),
            )
            .where(Customer.org_id == org_id, Customer.deleted_at.is_(None))
            .group_by(Customer.id, Customer.opening_balance)
            .subquery()
        )
        stmt = select(
            func.coalesce(func.sum(per_customer.c.outstanding), ZERO),
            func.count().filter(per_customer.c.outstanding > 0),
        )
        total, count = (await self._session.execute(stmt)).one()
        return decimal.Decimal(total), int(count)

    async def payables_total(self, org_id: uuid.UUID) -> tuple[decimal.Decimal, int]:
        unpaid = func.coalesce(
            func.sum(
                case(
                    (
                        PurchaseHeader.grand_total > PurchaseHeader.amount_paid,
                        PurchaseHeader.grand_total - PurchaseHeader.amount_paid,
                    ),
                    else_=ZERO,
                )
            ),
            ZERO,
        )
        per_supplier = (
            select((Supplier.opening_balance + unpaid).label("outstanding"))
            .select_from(Supplier)
            .outerjoin(
                PurchaseHeader,
                (PurchaseHeader.supplier_id == Supplier.id)
                & (PurchaseHeader.deleted_at.is_(None))
                & (PurchaseHeader.status == "confirmed"),
            )
            .where(Supplier.org_id == org_id, Supplier.deleted_at.is_(None))
            .group_by(Supplier.id, Supplier.opening_balance)
            .subquery()
        )
        stmt = select(
            func.coalesce(func.sum(per_supplier.c.outstanding), ZERO),
            func.count().filter(per_supplier.c.outstanding > 0),
        )
        total, count = (await self._session.execute(stmt)).one()
        return decimal.Decimal(total), int(count)

    async def top_sellers(
        self, org_id: uuid.UUID, start: datetime.date, end: datetime.date, limit: int = 5
    ) -> list[TopSeller]:
        stmt = (
            select(Product.code, Product.description, func.sum(SalesLine.line_total))
            .select_from(SalesLine)
            .join(SalesHeader, SalesHeader.id == SalesLine.sales_header_id)
            .join(Product, Product.id == SalesLine.product_id)
            .where(
                SalesHeader.org_id == org_id,
                SalesHeader.deleted_at.is_(None),
                SalesHeader.status.in_(["confirmed", "partially_returned"]),
                SalesHeader.sale_date >= start,
                SalesHeader.sale_date <= end,
            )
            .group_by(Product.id, Product.code, Product.description)
            .order_by(func.sum(SalesLine.line_total).desc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            TopSeller(code=code, description=description, revenue=decimal.Decimal(revenue))
            for code, description, revenue in rows
        ]

    async def slow_movers(
        self,
        org_id: uuid.UUID,
        *,
        slow_moving_days: int,
        today: datetime.date,
        limit: int = 10,
    ) -> list[SlowMover]:
        """Stock on hand with no SALE movement inside the window --
        docs/12_Dashboard.md §2. `today` at midnight UTC minus the window
        is precise enough at day granularity; this doesn't need
        business-timezone precision the way ledger entry_date does."""
        cutoff = datetime.datetime.combine(
            today - datetime.timedelta(days=slow_moving_days),
            datetime.time.min,
            tzinfo=datetime.UTC,
        )
        last_sale = (
            select(func.max(InventoryMovement.created_at))
            .where(
                InventoryMovement.org_id == org_id,
                InventoryMovement.product_id == Product.id,
                InventoryMovement.movement_type == MovementType.SALE,
            )
            .correlate(Product)
            .scalar_subquery()
        )
        recent_sale_exists = (
            select(InventoryMovement.id)
            .where(
                InventoryMovement.org_id == org_id,
                InventoryMovement.product_id == Product.id,
                InventoryMovement.movement_type == MovementType.SALE,
                InventoryMovement.created_at >= cutoff,
            )
            .correlate(Product)
            .exists()
        )
        stmt = (
            select(Product.code, Product.description, last_sale)
            .select_from(Inventory)
            .join(Product, Product.id == Inventory.product_id)
            .where(
                Inventory.org_id == org_id,
                Product.deleted_at.is_(None),
                Product.is_active.is_(True),
                Inventory.qty_on_hand > 0,
                ~recent_sale_exists,
            )
            .order_by(last_sale.asc().nulls_first())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        results: list[SlowMover] = []
        for code, description, last_sale_at in rows:
            days = (today - last_sale_at.date()).days if last_sale_at is not None else None
            results.append(SlowMover(code=code, description=description, days_since_sale=days))
        return results

    async def slow_moving_days(self, org_id: uuid.UUID) -> int:
        """`settings.slow_moving_days`, default 60 -- docs/12_Dashboard.md
        §2. The `settings` command isn't built yet, so this reads the
        table directly; once it ships, values it writes are picked up
        here with no change needed."""
        stmt = select(Setting.value).where(
            Setting.org_id == org_id, Setting.key == "slow_moving_days"
        )
        value = (await self._session.execute(stmt)).scalar_one_or_none()
        return int(value) if isinstance(value, int | float) else DEFAULT_SLOW_MOVING_DAYS
