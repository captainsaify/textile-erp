"""Correcting a sale after it is recorded.

The commonest repair in this system's short history, by some distance:
three sales were filed under the wrong customer in a single month, and
two more sold the wrong code. Purchases had a correction path from the
start and sales did not, which is the reason those went unfixed for as
long as they did.

Moving a sale to another customer touches no stock -- the goods left
either way -- so it is far safer than it sounds. Changing which *item*
was sold does move stock, and the guard checks the result.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundError, ValidationError
from backend.models import (
    Brand,
    Customer,
    InventoryMovement,
    Product,
    SalesHeader,
    SalesLine,
    User,
)
from backend.services.admin.guard import guarded
from backend.services.audit_service import AuditService
from backend.services.cost_replay_service import CostReplayService


class SaleFixService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _sale(self, org_id: uuid.UUID, reference: str) -> SalesHeader:
        rows = list(
            (
                await self._session.execute(
                    select(SalesHeader)
                    .where(
                        SalesHeader.org_id == org_id,
                        SalesHeader.deleted_at.is_(None),
                        cast(SalesHeader.id, String).ilike(f"{reference.strip().lower()}%"),
                    )
                    .limit(5)
                )
            ).scalars()
        )
        if not rows:
            raise NotFoundError("sale", reference)
        if len(rows) > 1:
            raise ValidationError(f"{reference!r} matches {len(rows)} sales — use more characters")
        return rows[0]

    async def fix(
        self,
        org_id: uuid.UUID,
        actor: User,
        *,
        reference: str,
        customer: str | None = None,
        line_no: int | None = None,
        code: str | None = None,
        brand: str | None = None,
    ) -> dict[str, Any]:
        header = await self._sale(org_id, reference)
        notes: list[str] = []

        async with guarded(self._session, org_id) as report:
            if customer is not None:
                target = (
                    (
                        await self._session.execute(
                            select(Customer).where(
                                Customer.org_id == org_id,
                                func.lower(func.trim(Customer.name)) == customer.strip().lower(),
                                Customer.deleted_at.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if target is None:
                    raise NotFoundError("customer", customer)
                # The receivable follows the sale's customer, so both
                # parties' outstanding correct themselves. What does not
                # move is a receipt already banked against the old name.
                header.customer_id = target.id
                notes.append(f"customer → {target.name}")

            if line_no is not None and (code or brand):
                line = (
                    (
                        await self._session.execute(
                            select(SalesLine).where(
                                SalesLine.sales_header_id == header.id,
                                SalesLine.line_no == line_no,
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if line is None:
                    raise NotFoundError("line", str(line_no))
                old = await self._session.get(Product, line.product_id)
                if old is None:
                    raise ValidationError("that line points at a product that no longer exists")
                target_product = await self._target(org_id, old, code, brand)
                if target_product.id != old.id:
                    movements = list(
                        (
                            await self._session.execute(
                                select(InventoryMovement).where(
                                    InventoryMovement.org_id == org_id,
                                    InventoryMovement.source_type == "sales_line",
                                    InventoryMovement.source_id == line.id,
                                )
                            )
                        ).scalars()
                    )
                    for movement in movements:
                        movement.product_id = target_product.id
                    line.product_id = target_product.id
                    await self._session.flush()
                    replay = CostReplayService(self._session)
                    for product_id in (old.id, target_product.id):
                        await replay.replay_product(org_id, product_id)
                    notes.append(
                        f"line {line_no}: {old.code} → {target_product.code}, "
                        f"{len(movements)} movement(s) moved"
                    )

            if not notes:
                raise ValidationError("nothing to change")

            header.updated_at = datetime.datetime.now(datetime.UTC)
            await self._session.flush()
            await AuditService(self._session).record(
                org_id,
                actor.id,
                action="sale.fixed",
                entity_type="sales_headers",
                entity_id=header.id,
                after_state={"changes": notes},
                channel="cli",
            )
            for note in notes:
                report.note(note)

        return {
            "sale_id": str(header.id)[:8],
            "notes": report.notes,
            "committed": report.committed,
        }

    async def _target(
        self, org_id: uuid.UUID, old: Product, code: str | None, brand: str | None
    ) -> Product:
        """The product this line should have named.

        Unlike the purchase side this never *creates* one: selling an
        item that was never bought is how stock goes negative on paper,
        and the guard would refuse it a moment later anyway. Better to
        say so here.
        """
        wanted = " ".join((code or old.code).split()).upper()
        brand_id = old.brand_id
        if brand is not None:
            row = (
                (
                    await self._session.execute(
                        select(Brand).where(
                            Brand.org_id == org_id,
                            func.lower(func.trim(Brand.name)) == brand.strip().lower(),
                            Brand.deleted_at.is_(None),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                raise NotFoundError("brand", brand)
            brand_id = row.id

        target = (
            (
                await self._session.execute(
                    select(Product).where(
                        Product.org_id == org_id,
                        func.upper(Product.code) == wanted,
                        Product.brand_id == brand_id,
                        Product.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )
        if target is None:
            raise ValidationError(
                f"no product {wanted} under that label. Enter the purchase for it first — "
                "selling something never bought is how stock goes negative on paper."
            )
        return target
