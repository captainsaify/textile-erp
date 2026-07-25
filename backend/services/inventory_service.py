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
