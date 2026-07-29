"""Inventory mutations -- docs/03_Inventory.md. The weighted-average
recompute (§2) runs under a row-level FOR UPDATE lock (§9); every cache
update happens in the same transaction as the movement row that
justifies it (§1). Caller owns the transaction."""

from __future__ import annotations

import decimal
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import InsufficientStockError
from backend.models import Inventory, InventoryMovement
from backend.models.enums import MovementType

FOUR_PLACES = decimal.Decimal("0.0001")
THREE_PLACES = decimal.Decimal("0.001")
ZERO = decimal.Decimal("0")


class InventoryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _locked_row(
        self, org_id: uuid.UUID, product_id: uuid.UUID, warehouse_id: uuid.UUID
    ) -> Inventory:
        # first-ever movement race: ON CONFLICT DO NOTHING then lock --
        # docs/03_Inventory.md §9
        await self._session.execute(
            pg_insert(Inventory)
            .values(
                org_id=org_id,
                product_id=product_id,
                warehouse_id=warehouse_id,
                qty_on_hand=ZERO,
                weighted_avg_cost=ZERO,
            )
            .on_conflict_do_nothing(
                index_elements=[Inventory.org_id, Inventory.product_id, Inventory.warehouse_id]
            )
        )
        stmt = (
            select(Inventory)
            .where(
                Inventory.org_id == org_id,
                Inventory.product_id == product_id,
                Inventory.warehouse_id == warehouse_id,
            )
            .with_for_update()
        )
        return (await self._session.execute(stmt)).scalar_one()

    async def record_purchase_movement(
        self,
        org_id: uuid.UUID,
        *,
        product_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        qty: decimal.Decimal,
        landed_cost_per_unit: decimal.Decimal,
        source_id: uuid.UUID,
        created_by: uuid.UUID,
    ) -> InventoryMovement:
        inventory = await self._locked_row(org_id, product_id, warehouse_id)

        old_qty = inventory.qty_on_hand
        old_avg = inventory.weighted_avg_cost
        new_qty = (old_qty + qty).quantize(THREE_PLACES)
        if old_qty <= ZERO:
            # negative/zero stock before a purchase: the old average has no
            # meaningful weight to contribute
            new_avg = landed_cost_per_unit.quantize(FOUR_PLACES)
        else:
            new_avg = ((old_qty * old_avg + qty * landed_cost_per_unit) / (old_qty + qty)).quantize(
                FOUR_PLACES
            )

        movement = InventoryMovement(
            org_id=org_id,
            product_id=product_id,
            warehouse_id=warehouse_id,
            movement_type=MovementType.PURCHASE,
            qty_delta=qty,
            unit_cost=landed_cost_per_unit.quantize(FOUR_PLACES),
            resulting_qty_on_hand=new_qty,
            resulting_avg_cost=new_avg,
            source_type="purchase_line",
            source_id=source_id,
            created_by=created_by,
        )
        self._session.add(movement)
        inventory.qty_on_hand = new_qty
        inventory.weighted_avg_cost = new_avg
        await self._session.flush()
        return movement

    async def peek(
        self, org_id: uuid.UUID, product_id: uuid.UUID, warehouse_id: uuid.UUID
    ) -> tuple[decimal.Decimal, decimal.Decimal]:
        """(qty_on_hand, weighted_avg_cost) without locking -- for
        pre-flight checks and warnings before a transaction starts."""
        from sqlalchemy import select as sa_select

        row = (
            await self._session.execute(
                sa_select(Inventory.qty_on_hand, Inventory.weighted_avg_cost).where(
                    Inventory.org_id == org_id,
                    Inventory.product_id == product_id,
                    Inventory.warehouse_id == warehouse_id,
                )
            )
        ).first()
        return (row[0], row[1]) if row is not None else (ZERO, ZERO)

    async def record_sale_movement(
        self,
        org_id: uuid.UUID,
        *,
        product_id: uuid.UUID,
        product_code: str,
        warehouse_id: uuid.UUID,
        qty: decimal.Decimal,
        source_id: uuid.UUID,
        created_by: uuid.UUID,
        allow_negative: bool = False,
        unit_code: str = "",
    ) -> InventoryMovement:
        """Sales reduce quantity and never change the average cost --
        docs/03_Inventory.md §2. Going negative is blocked unless the
        user explicitly overrode (§3)."""
        inventory = await self._locked_row(org_id, product_id, warehouse_id)
        old_qty = inventory.qty_on_hand
        if qty > old_qty and not allow_negative:
            raise InsufficientStockError(product_code, old_qty, qty, unit_code)

        new_qty = (old_qty - qty).quantize(THREE_PLACES)
        avg_cost = inventory.weighted_avg_cost  # unchanged by sales
        movement = InventoryMovement(
            org_id=org_id,
            product_id=product_id,
            warehouse_id=warehouse_id,
            movement_type=MovementType.SALE,
            qty_delta=-qty,
            unit_cost=avg_cost,
            resulting_qty_on_hand=new_qty,
            resulting_avg_cost=avg_cost,
            source_type="sales_line",
            source_id=source_id,
            created_by=created_by,
        )
        self._session.add(movement)
        inventory.qty_on_hand = new_qty
        await self._session.flush()
        return movement

    async def record_sale_return_movement(
        self,
        org_id: uuid.UUID,
        *,
        product_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        qty: decimal.Decimal,
        avg_cost_at_sale_time: decimal.Decimal,
        source_id: uuid.UUID,
        created_by: uuid.UUID,
        reason: str | None = None,
    ) -> InventoryMovement:
        """Stock comes back at the cost it left at, and the running
        average is *not* recomputed -- docs/03_Inventory.md §2. Using
        today's average here would let an old sale's reversal
        retroactively distort current costing, which is exactly what the
        historical snapshot on sales_lines exists to prevent.
        """
        inventory = await self._locked_row(org_id, product_id, warehouse_id)
        new_qty = (inventory.qty_on_hand + qty).quantize(THREE_PLACES)
        avg_cost = inventory.weighted_avg_cost  # unchanged, per the §2 table

        movement = InventoryMovement(
            org_id=org_id,
            product_id=product_id,
            warehouse_id=warehouse_id,
            movement_type=MovementType.SALE_RETURN,
            qty_delta=qty,
            unit_cost=avg_cost_at_sale_time,
            resulting_qty_on_hand=new_qty,
            resulting_avg_cost=avg_cost,
            source_type="sales_line",
            source_id=source_id,
            reason=reason,
            created_by=created_by,
        )
        self._session.add(movement)
        inventory.qty_on_hand = new_qty
        await self._session.flush()
        return movement

    async def restate_cost(
        self,
        org_id: uuid.UUID,
        *,
        product_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        value_delta: decimal.Decimal,
        source_id: uuid.UUID,
        created_by: uuid.UUID,
        reason: str,
    ) -> tuple[InventoryMovement, decimal.Decimal]:
        """Change what the stock on hand *cost*, without moving any of it.

        Used when a bill's rate is corrected after the goods arrived: the
        quantity is right, the price was not. `qty_delta` is zero, so
        `qty_on_hand` still equals the signed sum of movements and the
        nightly reconciliation is untouched -- only the value moves.

        Returns (movement, new average). The average cannot go negative:
        a correction large enough to do that means something else is
        wrong, and inventing a negative cost basis would poison every
        later sale silently.
        """
        inventory = await self._locked_row(org_id, product_id, warehouse_id)
        old_qty = inventory.qty_on_hand
        old_avg = inventory.weighted_avg_cost

        if old_qty <= ZERO:
            # nothing left to revalue; the cost of what was sold is
            # already in COGS and restating that is a different job
            new_avg = old_avg
        else:
            new_avg = max(ZERO, ((old_qty * old_avg + value_delta) / old_qty)).quantize(FOUR_PLACES)

        movement = InventoryMovement(
            org_id=org_id,
            product_id=product_id,
            warehouse_id=warehouse_id,
            movement_type=MovementType.ADJUSTMENT_INCREASE
            if value_delta >= ZERO
            else MovementType.ADJUSTMENT_DECREASE,
            qty_delta=ZERO,
            unit_cost=new_avg,
            resulting_qty_on_hand=old_qty,
            resulting_avg_cost=new_avg,
            source_type="purchase_line",
            source_id=source_id,
            reason=reason,
            created_by=created_by,
        )
        self._session.add(movement)
        inventory.weighted_avg_cost = new_avg
        await self._session.flush()
        return movement, new_avg

    async def record_purchase_return_movement(
        self,
        org_id: uuid.UUID,
        *,
        product_id: uuid.UUID,
        warehouse_id: uuid.UUID,
        qty: decimal.Decimal,
        landed_cost_per_unit: decimal.Decimal,
        source_id: uuid.UUID,
        created_by: uuid.UUID,
        reason: str | None = None,
    ) -> tuple[InventoryMovement, bool]:
        """Unwind a purchase from the weighted average -- the inverse of
        the §2 formula, using the *original* landed cost:

            new_avg = (old_qty * old_avg - qty * landed_cost) / (old_qty - qty)

        Returns (movement, was_approximated).

        The exact reversal is only possible while that purchase's
        contribution is still in the pool. Once most of the batch has
        been sold and mixed with later purchases, the subtraction drives
        remaining value or quantity to zero or below and the answer
        stops being meaningful -- docs/03_Inventory.md §4 says so
        explicitly. In that case the average is left alone and only the
        quantity falls, which removes stock at the current average
        (value reduced proportionally), and the caller is told so it can
        flag the return for manual review. Silently emitting a negative
        or wildly wrong average would corrupt the cost basis of every
        later sale, and nothing downstream would raise.
        """
        inventory = await self._locked_row(org_id, product_id, warehouse_id)
        old_qty = inventory.qty_on_hand
        old_avg = inventory.weighted_avg_cost

        new_qty = (old_qty - qty).quantize(THREE_PLACES)
        remaining_value = old_qty * old_avg - qty * landed_cost_per_unit

        if new_qty > ZERO and remaining_value >= ZERO:
            new_avg = (remaining_value / new_qty).quantize(FOUR_PLACES)
            approximated = False
        else:
            # can't unwind exactly; hold the average and let value fall
            # proportionally with quantity
            new_avg = old_avg
            approximated = True

        movement = InventoryMovement(
            org_id=org_id,
            product_id=product_id,
            warehouse_id=warehouse_id,
            movement_type=MovementType.PURCHASE_RETURN,
            qty_delta=-qty,
            unit_cost=landed_cost_per_unit,
            resulting_qty_on_hand=new_qty,
            resulting_avg_cost=new_avg,
            source_type="purchase_line",
            source_id=source_id,
            reason=reason,
            created_by=created_by,
        )
        self._session.add(movement)
        inventory.qty_on_hand = new_qty
        inventory.weighted_avg_cost = new_avg
        await self._session.flush()
        return movement, approximated
