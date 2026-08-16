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
from backend.services.purchase_service import allocate
from backend.services.receipt_correction_service import RateChangeService

ZERO = decimal.Decimal("0")
TWO = decimal.Decimal("0.01")
FOUR = decimal.Decimal("0.0001")


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
        quantity: decimal.Decimal | None = None,
        remove: bool = False,
    ) -> dict[str, Any]:
        """One line, under its own guard, for a caller that has none."""
        async with guarded(self._session, org_id) as report:
            notes = await self.fix_in_transaction(
                org_id,
                actor,
                invoice_no=invoice_no,
                line_no=line_no,
                code=code,
                brand=brand,
                description=description,
                rate=rate,
                quantity=quantity,
                remove=remove,
            )
            for note in notes:
                report.note(note)
        return {
            "invoice_no": invoice_no,
            "line_no": line_no,
            "notes": report.notes,
            "committed": report.committed,
        }

    async def fix_in_transaction(
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
        quantity: decimal.Decimal | None = None,
        remove: bool = False,
    ) -> list[str]:
        """The work, for a caller that already holds the transaction.

        Editing a whole bill runs several of these together, and a guard
        inside a guard does not nest -- the inner one commits the outer
        transaction to take ownership of its own, which would defeat both
        the single rollback and the dry run. So the guard lives with the
        caller and this does the work.
        """
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

        if quantity is not None:
            notes.extend(await self._set_quantity(org_id, actor, header, line, quantity))

        if remove:
            notes.extend(await self._remove_line(org_id, header, line, old))

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
        return notes

    async def _set_quantity(
        self,
        org_id: uuid.UUID,
        actor: User,
        header: PurchaseHeader,
        line: PurchaseLine,
        quantity: decimal.Decimal,
    ) -> list[str]:
        """Correct what was billed on this line.

        The stock movement carries the quantity too, and the balance is
        derived from it -- so editing the line alone would leave the two
        disagreeing and the nightly reconciliation would find it.

        Lived in the CLI until now, which is why Master Control could
        change a line's price but not its weight.
        """
        if quantity <= ZERO:
            raise ValidationError(
                "A quantity of zero is a removed line -- remove it instead, "
                "which also takes its stock movements."
            )
        old_qty = line.qty
        if old_qty == quantity:
            return []
        line.qty = quantity
        line.line_total = (quantity * line.rate).quantize(TWO)
        if line.total_weight_kg is not None:
            line.total_weight_kg = quantity
        for movement in (
            await self._session.execute(
                select(InventoryMovement).where(
                    InventoryMovement.org_id == org_id,
                    InventoryMovement.source_type == "purchase_line",
                    InventoryMovement.source_id == line.id,
                    InventoryMovement.qty_delta != ZERO,
                )
            )
        ).scalars():
            movement.qty_delta = quantity
        await self._retotal(header)
        await self._session.flush()
        await self._replay(org_id, line.product_id)
        return [f"qty {old_qty} → {quantity}", f"bill total → {header.grand_total}"]

    async def _remove_line(
        self,
        org_id: uuid.UUID,
        header: PurchaseHeader,
        line: PurchaseLine,
        product: Product,
    ) -> list[str]:
        """Take a line off the bill, with its stock.

        Line numbers are deliberately not closed up afterwards. A line
        number is an identifier people have already used -- printed on
        the sheet the partners get, quoted in WhatsApp -- so sliding
        every later line up one would make "line 4 is wrong" point at
        different goods depending on when it was read. A gap is visible
        and harmless; a renumber is invisible and is not.
        """
        gone = list(
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
        for movement in gone:
            await self._session.delete(movement)
        qty, code, line_no = line.qty, product.code, line.line_no
        product_id = line.product_id
        await self._session.delete(line)
        await self._session.flush()
        await self._retotal(header)
        await self._session.flush()
        await self._replay(org_id, product_id)
        return [
            f"line {line_no} removed ({code}, {qty}), {len(gone)} movement(s) deleted",
            f"bill total → {header.grand_total}",
        ]

    async def _retotal(self, header: PurchaseHeader) -> None:
        """Re-spread freight and charges, then re-add the bill.

        Freight is what the transporter charged and does not change
        because a line was removed or a quantity corrected -- but its
        split across the lines is by quantity, so that split moves. A
        bill whose lines no longer add up to its total is worse than
        either number being wrong on its own.
        """
        lines = list(
            (
                await self._session.execute(
                    select(PurchaseLine)
                    .where(PurchaseLine.purchase_header_id == header.id)
                    .order_by(PurchaseLine.line_no)
                )
            ).scalars()
        )
        if not lines:
            header.subtotal = ZERO
            header.grand_total = (header.freight + header.other_charges).quantize(TWO)
            return

        header.subtotal = sum((row.line_total for row in lines), ZERO).quantize(TWO)
        header.grand_total = (
            header.subtotal + header.freight + header.other_charges - header.discount
        ).quantize(TWO)
        freight_shares = allocate(header.freight, [row.qty for row in lines])
        other_shares = allocate(header.other_charges, [row.line_total for row in lines])
        for index, row in enumerate(lines):
            row.freight_allocated = freight_shares[index]
            if row.qty > ZERO:
                row.landed_cost_per_unit = (
                    (row.line_total + freight_shares[index] + other_shares[index]) / row.qty
                ).quantize(FOUR)

    async def _replay(self, org_id: uuid.UUID, product_id: uuid.UUID) -> None:
        await CostReplayService(self._session).replay_product(org_id, product_id)

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
