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
import decimal
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

ZERO = decimal.Decimal("0")
TWO = decimal.Decimal("0.01")


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
        quantity: decimal.Decimal | None = None,
        rate: decimal.Decimal | None = None,
        remove: bool = False,
    ) -> dict[str, Any]:
        """One sale, under its own guard, for a caller that has none."""
        async with guarded(self._session, org_id) as report:
            notes = await self.fix_in_transaction(
                org_id,
                actor,
                reference=reference,
                customer=customer,
                line_no=line_no,
                code=code,
                brand=brand,
                quantity=quantity,
                rate=rate,
                remove=remove,
            )
            for note in notes:
                report.note(note)
        return {
            "sale_id": reference,
            "notes": report.notes,
            "committed": report.committed,
        }

    async def fix_in_transaction(
        self,
        org_id: uuid.UUID,
        actor: User,
        *,
        reference: str,
        customer: str | None = None,
        line_no: int | None = None,
        code: str | None = None,
        brand: str | None = None,
        quantity: decimal.Decimal | None = None,
        rate: decimal.Decimal | None = None,
        remove: bool = False,
    ) -> list[str]:
        """The work, for a caller that already holds the transaction --
        the sale editor runs several of these together, and a guard
        inside a guard commits the outer transaction to own its own."""
        header = await self._sale(org_id, reference)
        notes: list[str] = []

        if True:
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

            if line_no is not None and (quantity is not None or rate is not None or remove):
                notes.extend(
                    await self._line_money(
                        org_id, header, line_no, quantity=quantity, rate=rate, remove=remove
                    )
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
            return notes

    async def _line_money(
        self,
        org_id: uuid.UUID,
        header: SalesHeader,
        line_no: int,
        *,
        quantity: decimal.Decimal | None,
        rate: decimal.Decimal | None,
        remove: bool,
    ) -> list[str]:
        """Quantity, price, or the whole line -- on the way *out*.

        The mirror of the purchase side with one sign flipped and one
        consequence added. A sale's movement is negative, so correcting a
        quantity upward takes more stock out and can strand a product
        below zero; the guard re-checks the books and throws the edit away
        if it does.

        `avg_cost_at_sale_time` is deliberately left alone. It is what the
        goods cost when they left, not a price -- rewriting it would
        restate profit already reported on a sale that did happen.
        """
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
        product = await self._session.get(Product, line.product_id)
        notes: list[str] = []
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

        if remove:
            for movement in movements:
                await self._session.delete(movement)
            qty, code = line.qty, product.code if product else "?"
            await self._session.delete(line)
            await self._session.flush()
            notes.append(
                f"line {line_no} removed ({code}, {qty}), {len(movements)} movement(s) deleted"
            )
        else:
            if quantity is not None and quantity != line.qty:
                if quantity <= ZERO:
                    raise ValidationError(
                        "A quantity of zero is a removed line -- remove it instead, "
                        "which also puts its stock back."
                    )
                notes.append(f"line {line_no}: qty {line.qty} → {quantity}")
                line.qty = quantity
                if line.total_weight_kg is not None:
                    line.total_weight_kg = quantity
                for movement in movements:
                    if movement.qty_delta != ZERO:
                        # Out, so negative. Taking the sign from the
                        # quantity rather than the existing row keeps a
                        # movement that was already wrong from staying
                        # wrong in the same direction.
                        movement.qty_delta = -quantity
            if rate is not None and rate != line.rate:
                notes.append(f"line {line_no}: rate {line.rate} → {rate}")
                line.rate = rate
            line.line_total = (line.qty * line.rate).quantize(TWO)
            await self._session.flush()

        await self._retotal(header)
        await self._session.flush()
        if product is not None:
            await CostReplayService(self._session).replay_product(org_id, product.id)
        notes.append(f"sale total → {header.grand_total}")
        return notes

    async def _retotal(self, header: SalesHeader) -> None:
        """Re-add the sale. The money already received is not touched --
        what changes is how much of it the sale needed."""
        lines = list(
            (
                await self._session.execute(
                    select(SalesLine).where(SalesLine.sales_header_id == header.id)
                )
            ).scalars()
        )
        header.subtotal = sum((row.line_total for row in lines), ZERO).quantize(TWO)
        header.grand_total = (
            header.subtotal + header.freight + header.other_charges - header.discount
        ).quantize(TWO)
        if header.amount_paid <= ZERO:
            header.payment_status = "unpaid"
        elif header.amount_paid < header.grand_total:
            header.payment_status = "partial"
        else:
            header.payment_status = "paid"

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
