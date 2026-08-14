"""Correcting one line of a confirmed purchase.

Re-pointing a line to a different product is not an edit to a field.
Brand lives on the *product*, so "this bill's LALA was labelled MKD"
means the line should point at a different product entirely -- and its
stock movements have to go with it, or the two products' costing is
wrong in opposite directions.

Every path ends in a replay for that reason: moving a movement between
products changes the weighted average on both sides, and recomputing
from history is the only way to be sure of both.
"""

from __future__ import annotations

import decimal
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundError, ValidationError
from backend.models import (
    Brand,
    InventoryMovement,
    Product,
    PurchaseHeader,
    PurchaseLine,
    User,
)
from backend.models.enums import PurchaseStatus
from backend.services.admin.guard import guarded
from backend.services.audit_service import AuditService
from backend.services.cost_replay_service import CostReplayService
from backend.services.receipt_correction_service import RateChangeService


class PurchaseLineFixService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fix(
        self,
        org_id: uuid.UUID,
        actor: User,
        *,
        invoice_no: str,
        line_no: int,
        code: str | None = None,
        brand: str | None = None,
        description: str | None = None,
        rate: decimal.Decimal | None = None,
    ) -> dict[str, Any]:
        header = (
            (
                await self._session.execute(
                    select(PurchaseHeader).where(
                        PurchaseHeader.org_id == org_id,
                        PurchaseHeader.invoice_no == invoice_no.strip(),
                        PurchaseHeader.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )
        if header is None:
            raise NotFoundError("purchase", invoice_no)
        if header.status is not PurchaseStatus.CONFIRMED:
            raise ValidationError(
                f"{header.invoice_no} is {header.status.value} — only confirmed bills carry stock"
            )

        line = (
            (
                await self._session.execute(
                    select(PurchaseLine).where(
                        PurchaseLine.purchase_header_id == header.id,
                        PurchaseLine.line_no == line_no,
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

        notes: list[str] = []
        async with guarded(self._session, org_id) as report:
            if description is not None:
                line.description = description
                notes.append(f"description → {description}")

            if code or brand:
                target = await self._target_product(org_id, actor, old, code, brand)
                if target.id != old.id:
                    movements = list(
                        (
                            await self._session.execute(
                                select(InventoryMovement).where(
                                    InventoryMovement.org_id == org_id,
                                    InventoryMovement.source_type == "purchase_line",
                                    InventoryMovement.source_id == line.id,
                                )
                            )
                        ).scalars()
                    )
                    for movement in movements:
                        movement.product_id = target.id
                    line.product_id = target.id
                    await self._session.flush()
                    notes.append(f"{old.code} → {target.code}, {len(movements)} movement(s) moved")
                    replay = CostReplayService(self._session)
                    for product_id in (old.id, target.id):
                        await replay.replay_product(org_id, product_id)

            if rate is not None:
                await RateChangeService(self._session).change(
                    actor, invoice_no=header.invoice_no, new_rate=rate, codes=[old.code]
                )
                notes.append(f"rate → {rate}")

            if not notes:
                raise ValidationError("nothing to change")

            await AuditService(self._session).record(
                org_id,
                actor.id,
                action="purchase.fixed",
                entity_type="purchase_headers",
                entity_id=header.id,
                after_state={"line": line_no, "changes": notes},
                channel="cli",
            )
            for note in notes:
                report.note(note)

        return {
            "invoice_no": header.invoice_no,
            "line_no": line_no,
            "notes": report.notes,
            "committed": report.committed,
        }

    async def _target_product(
        self,
        org_id: uuid.UUID,
        actor: User,
        old: Product,
        code: str | None,
        brand: str | None,
    ) -> Product:
        """The product this line should point at instead.

        Created when the brand exists but does not yet carry the code --
        which is the ordinary case for a mislabelled bill, and refusing
        would make the repair impossible rather than safe. Everything
        except code and brand is inherited, so the new row is the same
        goods under the right label.
        """
        wanted = " ".join((code or old.code).split()).upper()
        if brand is None:
            brand_id = old.brand_id
        else:
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

        existing = (
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
        if existing is not None:
            return existing

        created = Product(
            org_id=org_id,
            product_type_id=old.product_type_id,
            code=wanted,
            description=old.description,
            unit_id=old.unit_id,
            brand_id=brand_id,
            reorder_level=old.reorder_level,
            created_by=actor.id,
        )
        self._session.add(created)
        await self._session.flush()
        return created
