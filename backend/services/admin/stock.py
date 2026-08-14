"""Stock corrections that have no purchase or sale behind them."""

from __future__ import annotations

import decimal
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundError, ValidationError
from backend.models import Brand, InventoryMovement, Product, User
from backend.models.enums import MovementType
from backend.services.admin.guard import guarded
from backend.services.audit_service import AuditService
from backend.services.cost_replay_service import CostReplayService

ZERO = decimal.Decimal("0")

REASONS = {
    "damaged": MovementType.DAMAGE,
    "adjust-up": MovementType.ADJUSTMENT_INCREASE,
    "adjust-down": MovementType.ADJUSTMENT_DECREASE,
}


class StockAdminService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _product(self, org_id: uuid.UUID, code: str, brand: str | None) -> Product:
        """A code names a product only with its brand -- three share 55X."""
        wanted = " ".join(code.split()).upper()
        rows = list(
            (
                await self._session.execute(
                    select(Product).where(
                        Product.org_id == org_id,
                        func.upper(Product.code) == wanted,
                        Product.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )
        if not rows:
            raise NotFoundError("product", wanted)
        if brand:
            target = " ".join(brand.split()).casefold()
            names = {
                row_id: name
                for row_id, name in (
                    await self._session.execute(
                        select(Brand.id, Brand.name).where(Brand.org_id == org_id)
                    )
                ).all()
            }
            matched = [
                p
                for p in rows
                if p.brand_id and " ".join(names.get(p.brand_id, "").split()).casefold() == target
            ]
            if not matched:
                raise ValidationError(f"{wanted} is not carried by {brand}")
            return matched[0]
        if len(rows) > 1:
            raise ValidationError(f"{len(rows)} brands carry {wanted} — say which")
        return rows[0]

    async def adjust(
        self,
        org_id: uuid.UUID,
        actor: User,
        *,
        code: str,
        brand: str | None,
        qty_delta: decimal.Decimal,
        reason: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        if reason not in REASONS:
            raise ValidationError(f"reason must be one of: {', '.join(sorted(REASONS))}")
        if qty_delta == ZERO:
            raise ValidationError("an adjustment of zero would only add noise to the ledger")

        product = await self._product(org_id, code, brand)
        warehouse_id = (
            await self._session.execute(
                select(InventoryMovement.warehouse_id)
                .where(
                    InventoryMovement.org_id == org_id,
                    InventoryMovement.product_id == product.id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if warehouse_id is None:
            raise ValidationError(
                f"{product.code} has never moved, so there is no warehouse to adjust in"
            )

        replay = CostReplayService(self._session)
        async with guarded(self._session, org_id) as report:
            current = await replay.replay(org_id, product.id, warehouse_id)
            self._session.add(
                InventoryMovement(
                    org_id=org_id,
                    product_id=product.id,
                    warehouse_id=warehouse_id,
                    movement_type=REASONS[reason],
                    qty_delta=qty_delta,
                    # The current average: stock appearing or leaving
                    # without a transaction behind it does not change
                    # what the remaining stock cost.
                    unit_cost=current.avg_after,
                    resulting_qty_on_hand=current.qty_after + qty_delta,
                    resulting_avg_cost=current.avg_after,
                    source_type="admin_adjustment",
                    source_id=uuid.uuid4(),
                    created_by=actor.id,
                    notes=note,
                )
            )
            await self._session.flush()
            result = await replay.replay(org_id, product.id, warehouse_id)
            report.note(f"{product.code}: {qty_delta} → {result.qty_after} on hand")
            await AuditService(self._session).record(
                org_id,
                actor.id,
                action="stock.adjusted",
                entity_type="inventory",
                entity_id=product.id,
                after_state={"code": product.code, "qty_delta": str(qty_delta), "reason": reason},
                channel="cli",
            )
            after = result

        return {
            "code": product.code,
            "on_hand": str(after.qty_after),
            "avg_cost": str(after.avg_after),
            "notes": report.notes,
            "committed": report.committed,
        }

    async def recost_all(self, org_id: uuid.UUID) -> dict[str, Any]:
        """Replay every product's cost from its movements.

        Honours rate corrections, which are zero-quantity movements whose
        unit cost carries the new average -- skipping those is what once
        overstated this business's stock by about 1.3 lakh.
        """
        replay = CostReplayService(self._session)
        async with guarded(self._session, org_id) as report:
            results = await replay.replay_all(org_id)
            changed = [r for r in results if r.changed]
            report.note(f"{len(results)} product/warehouse pair(s) replayed")
            if not changed:
                report.note("nothing moved — the books already agreed with history")
            moved = []
            for result in changed:
                product = await self._session.get(Product, result.product_id)
                moved.append(
                    {
                        "code": product.code if product else str(result.product_id)[:8],
                        "qty_before": str(result.qty_before),
                        "qty_after": str(result.qty_after),
                        "avg_before": str(result.avg_before),
                        "avg_after": str(result.avg_after),
                    }
                )
        return {"changed": moved, "notes": report.notes, "committed": report.committed}
