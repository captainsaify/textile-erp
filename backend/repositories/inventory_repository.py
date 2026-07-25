"""Inventory reads hit the `inventory` cache, never a movement replay
-- docs/03_Inventory.md §12. Movement queries are for display
(last-movement line in `stock CODE`)."""

from __future__ import annotations

import dataclasses
import decimal
import uuid

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Inventory, InventoryMovement, Product


@dataclasses.dataclass(frozen=True)
class StockTotals:
    total_value: decimal.Decimal
    low_count: int
    negative_count: int


@dataclasses.dataclass(frozen=True)
class LowStockRow:
    code: str
    description: str
    unit_code: str
    qty_on_hand: decimal.Decimal
    reorder_level: decimal.Decimal | None


class InventoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_product(self, org_id: uuid.UUID, product_id: uuid.UUID) -> Inventory | None:
        stmt = select(Inventory).where(
            Inventory.org_id == org_id, Inventory.product_id == product_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def last_movement(
        self, org_id: uuid.UUID, product_id: uuid.UUID
    ) -> InventoryMovement | None:
        stmt = (
            select(InventoryMovement)
            .where(
                InventoryMovement.org_id == org_id,
                InventoryMovement.product_id == product_id,
            )
            .order_by(InventoryMovement.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    def _low_stock_condition(self) -> ColumnElement[bool]:
        # below reorder level, or negative regardless of reorder config
        # -- docs/03_Inventory.md §7
        return or_(
            Inventory.qty_on_hand < 0,
            (Product.reorder_level.is_not(None)) & (Inventory.qty_on_hand <= Product.reorder_level),
        )

    async def totals(self, org_id: uuid.UUID) -> StockTotals:
        join = (
            select(
                func.coalesce(func.sum(Inventory.qty_on_hand * Inventory.weighted_avg_cost), 0),
                func.count().filter(self._low_stock_condition()),
                func.count().filter(Inventory.qty_on_hand < 0),
            )
            .select_from(Inventory)
            .join(Product, Product.id == Inventory.product_id)
            .where(
                Inventory.org_id == org_id,
                Product.deleted_at.is_(None),
                Product.is_active.is_(True),
            )
        )
        value, low, negative = (await self._session.execute(join)).one()
        return StockTotals(
            total_value=decimal.Decimal(value),
            low_count=int(low),
            negative_count=int(negative),
        )

    async def low_stock_rows(
        self, org_id: uuid.UUID, *, negative_only: bool = False
    ) -> list[LowStockRow]:
        from backend.models import Unit

        condition = Inventory.qty_on_hand < 0 if negative_only else self._low_stock_condition()
        stmt = (
            select(
                Product.code,
                Product.description,
                Unit.code,
                Inventory.qty_on_hand,
                Product.reorder_level,
            )
            .select_from(Inventory)
            .join(Product, Product.id == Inventory.product_id)
            .join(Unit, Unit.id == Product.unit_id)
            .where(
                Inventory.org_id == org_id,
                Product.deleted_at.is_(None),
                Product.is_active.is_(True),
                condition,
            )
            .order_by(Inventory.qty_on_hand.asc())
        )
        rows = (await self._session.execute(stmt)).all()
        return [
            LowStockRow(
                code=r[0],
                description=r[1],
                unit_code=r[2],
                qty_on_hand=r[3],
                reorder_level=r[4],
            )
            for r in rows
        ]
