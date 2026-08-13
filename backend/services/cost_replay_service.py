"""Rebuild a product's stock and weighted-average cost from its movements.

This is the primitive underneath `erp stock recost`, the per-line brand
and code repairs, and merges -- anything that changes *which* product a
movement belongs to has to leave both sides' costing correct, and the
only way to be sure of that is to replay.

**The rule that matters, and that was got wrong once already.**

A movement with `qty_delta == 0` is not a no-op to be skipped. It is a
*restatement*: `restate_cost` writes it when a bill's rate is corrected
after the goods have landed, and its `unit_cost` carries the new average
outright. A replay that filters out zero-quantity rows -- which reads as
an obvious optimisation -- silently discards every rate correction ever
made. That exact bug, in an ad-hoc script, threw away corrections across
28 products and overstated stock by roughly 1.3 lakh; it was caught only
because the owner knew what one product had cost.

The three cases, in full:

    qty_delta == 0   restatement. avg = unit_cost. qty unchanged.
    qty_delta  > 0   inbound. weighted average of old stock and new.
    qty_delta  < 0   outbound. qty falls; **the average does not move**.
                     Cost leaves at the current average by definition, so
                     recomputing it here would drift the books every time
                     something was sold.
"""

from __future__ import annotations

import dataclasses
import decimal
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Inventory, InventoryMovement

FOUR_PLACES = decimal.Decimal("0.0001")
THREE_PLACES = decimal.Decimal("0.001")
ZERO = decimal.Decimal("0")


@dataclasses.dataclass(frozen=True)
class ReplayResult:
    product_id: uuid.UUID
    warehouse_id: uuid.UUID
    movements: int
    qty_before: decimal.Decimal
    qty_after: decimal.Decimal
    avg_before: decimal.Decimal
    avg_after: decimal.Decimal

    @property
    def changed(self) -> bool:
        return self.qty_before != self.qty_after or self.avg_before != self.avg_after


class CostReplayService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replay(
        self, org_id: uuid.UUID, product_id: uuid.UUID, warehouse_id: uuid.UUID
    ) -> ReplayResult:
        """Replay one product in one warehouse, in movement order.

        Also rewrites each movement's `resulting_qty_on_hand` and
        `resulting_avg_cost`. Those columns are how the ledger explains
        itself afterwards; leaving them describing a history that no
        longer happened would make every later investigation lie."""
        movements = list(
            (
                await self._session.execute(
                    select(InventoryMovement)
                    .where(
                        InventoryMovement.org_id == org_id,
                        InventoryMovement.product_id == product_id,
                        InventoryMovement.warehouse_id == warehouse_id,
                    )
                    # created_at then id: several movements share a
                    # timestamp when one command writes them together, and
                    # an unstable order would give a different average on
                    # every replay.
                    .order_by(InventoryMovement.created_at, InventoryMovement.id)
                )
            ).scalars()
        )

        inventory = (
            await self._session.execute(
                select(Inventory).where(
                    Inventory.org_id == org_id,
                    Inventory.product_id == product_id,
                    Inventory.warehouse_id == warehouse_id,
                )
            )
        ).scalar_one_or_none()

        qty_before = inventory.qty_on_hand if inventory is not None else ZERO
        avg_before = inventory.weighted_avg_cost if inventory is not None else ZERO

        qty = ZERO
        avg = ZERO
        for movement in movements:
            delta = movement.qty_delta
            if delta == ZERO:
                # Restatement -- see the module docstring. Never skip.
                avg = movement.unit_cost.quantize(FOUR_PLACES)
            elif delta > ZERO:
                new_qty = (qty + delta).quantize(THREE_PLACES)
                if qty <= ZERO:
                    # No meaningful weight in the old average to carry.
                    avg = movement.unit_cost.quantize(FOUR_PLACES)
                else:
                    avg = ((qty * avg + delta * movement.unit_cost) / (qty + delta)).quantize(
                        FOUR_PLACES
                    )
                qty = new_qty
            else:
                qty = (qty + delta).quantize(THREE_PLACES)
            movement.resulting_qty_on_hand = qty
            movement.resulting_avg_cost = avg

        if inventory is None:
            if not movements:
                return ReplayResult(
                    product_id, warehouse_id, 0, qty_before, qty_before, avg_before, avg_before
                )
            inventory = Inventory(
                org_id=org_id,
                product_id=product_id,
                warehouse_id=warehouse_id,
                qty_on_hand=qty,
                weighted_avg_cost=avg,
            )
            self._session.add(inventory)
        else:
            inventory.qty_on_hand = qty
            inventory.weighted_avg_cost = avg

        await self._session.flush()
        return ReplayResult(
            product_id=product_id,
            warehouse_id=warehouse_id,
            movements=len(movements),
            qty_before=qty_before,
            qty_after=qty,
            avg_before=avg_before,
            avg_after=avg,
        )

    async def replay_product(self, org_id: uuid.UUID, product_id: uuid.UUID) -> list[ReplayResult]:
        """Every warehouse this product has ever moved in.

        Driven off movements rather than off `inventory` rows: a
        re-pointed movement can be the first this product has seen in a
        warehouse, and there is no inventory row to find it by yet."""
        warehouses = set(
            (
                await self._session.execute(
                    select(InventoryMovement.warehouse_id)
                    .where(
                        InventoryMovement.org_id == org_id,
                        InventoryMovement.product_id == product_id,
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        warehouses |= set(
            (
                await self._session.execute(
                    select(Inventory.warehouse_id).where(
                        Inventory.org_id == org_id, Inventory.product_id == product_id
                    )
                )
            )
            .scalars()
            .all()
        )
        return [await self.replay(org_id, product_id, w) for w in sorted(warehouses, key=str)]

    async def replay_all(self, org_id: uuid.UUID) -> list[ReplayResult]:
        pairs = (
            await self._session.execute(
                select(InventoryMovement.product_id, InventoryMovement.warehouse_id)
                .where(InventoryMovement.org_id == org_id)
                .distinct()
            )
        ).all()
        return [await self.replay(org_id, p, w) for p, w in sorted(pairs, key=str)]
