"""Editing a whole confirmed bill from one screen.

The screen shows a bill the way it was typed and lets several things be
corrected at once. What it must never be is an overwrite: a bill is not
a form, it is stock movements, a landed cost per line, a journal and a
payable. So this takes the edited bill, works out what *changed*, and
runs the same repairs the terminal runs, one at a time, in an order that
holds:

    description → code/brand → quantity → rate → removals → charges

Rate last of the line edits because a rate change replays cost for every
line carrying that code, and doing it before a quantity moves would
replay against the old weight. Removals after edits, so line numbers
stay stable while the edits are matched up. Charges last because their
allocation is spread across whatever lines survive.

Everything happens inside one guard: one backup, one transaction, and
the whole edit is thrown away if the books stop balancing afterwards.
Preview is the same code with `dry_run`, so what it lists is what a save
would do rather than a guess at it.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundError, ValidationError
from backend.models import (
    Brand,
    Product,
    PurchaseHeader,
    PurchaseLine,
    Supplier,
    User,
)
from backend.models.enums import PurchaseStatus
from backend.services.admin.fixline import PurchaseLineFixService
from backend.services.admin.guard import guarded
from backend.services.admin.salefix import SaleFixService
from backend.services.audit_service import AuditService
from backend.services.receipt_correction_service import ChargeService

ZERO = decimal.Decimal("0")
TWO = decimal.Decimal("0.01")


@dataclasses.dataclass(frozen=True)
class EditedLine:
    """One row of the form. `line_no` is None for a row that was added."""

    line_no: int | None
    code: str
    brand: str | None
    description: str | None
    qty: decimal.Decimal
    rate: decimal.Decimal
    removed: bool = False


@dataclasses.dataclass(frozen=True)
class EditedBill:
    supplier: str | None = None
    invoice_no: str | None = None
    invoice_date: datetime.date | None = None
    lines: list[EditedLine] = dataclasses.field(default_factory=list)
    #: {"GST": "4340.00"} -- the charges the bill should end up carrying.
    #: Absent means "leave charges alone"; a label set to 0 takes it off.
    charges: dict[str, decimal.Decimal] | None = None


class BillEditService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- reading ------------------------------------------------------

    async def detail(self, org_id: uuid.UUID, invoice_no: str) -> dict[str, Any]:
        """The bill as the form should show it.

        Includes the figures the form derives from (bales and kg per
        bale) so a reopened bill looks like the one that was typed,
        rather than a total the person has to work backwards from.
        """
        header = await self._header(org_id, invoice_no)
        supplier = await self._session.get(Supplier, header.supplier_id)
        rows = list(
            (
                await self._session.execute(
                    select(PurchaseLine, Product, Brand)
                    .join(Product, Product.id == PurchaseLine.product_id)
                    .join(Brand, Brand.id == Product.brand_id, isouter=True)
                    .where(PurchaseLine.purchase_header_id == header.id)
                    .order_by(PurchaseLine.line_no)
                )
            ).all()
        )
        return {
            "invoice_no": header.invoice_no,
            "invoice_date": header.invoice_date.isoformat(),
            "supplier": supplier.name if supplier else "",
            "status": header.status.value,
            "freight": str(header.freight),
            "discount": str(header.discount),
            "other_charges": str(header.other_charges),
            "subtotal": str(header.subtotal),
            "grand_total": str(header.grand_total),
            "amount_paid": str(header.amount_paid),
            "lines": [
                {
                    "line_no": line.line_no,
                    "code": product.code,
                    "brand": brand.name if brand else None,
                    "description": line.description or product.description,
                    "qty": str(line.qty),
                    "rate": str(line.rate),
                    "pieces": str(line.qty / line.weight_kg) if line.weight_kg else None,
                    "weight_kg": str(line.weight_kg) if line.weight_kg else None,
                    "line_total": str(line.line_total),
                    "landed_cost_per_unit": str(line.landed_cost_per_unit or ZERO),
                }
                for line, product, brand in rows
            ],
        }

    async def _header(self, org_id: uuid.UUID, invoice_no: str) -> PurchaseHeader:
        header = (
            (
                await self._session.execute(
                    select(PurchaseHeader).where(
                        PurchaseHeader.org_id == org_id,
                        func.lower(PurchaseHeader.invoice_no) == invoice_no.strip().lower(),
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
                f"{header.invoice_no} is {header.status.value} — only a confirmed bill "
                "carries stock, and only stock is worth correcting"
            )
        return header

    # --- writing ------------------------------------------------------

    async def apply(
        self,
        org_id: uuid.UUID,
        actor: User,
        *,
        invoice_no: str,
        edited: EditedBill,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Work out what changed and run the repairs for it.

        Adding a line is deliberately not supported here. A line that was
        never on the bill is a *different purchase* -- it has no stock
        movement, no landed cost and no place in the freight split -- and
        pretending a form can conjure one would produce a bill whose
        history does not explain its stock. Enter it as its own bill, or
        remove this one and re-enter it.
        """
        header = await self._header(org_id, invoice_no)
        existing = {
            line.line_no: line
            for line in (
                await self._session.execute(
                    select(PurchaseLine).where(PurchaseLine.purchase_header_id == header.id)
                )
            ).scalars()
        }
        for row in edited.lines:
            if row.line_no is None:
                raise ValidationError(
                    "A new line cannot be added to a bill that is already confirmed — "
                    "it would have no stock movement behind it. Enter it as its own bill."
                )
            if row.line_no not in existing:
                raise NotFoundError("line", str(row.line_no))

        changes: list[str] = []
        #: What each removed line held, so the removal can be undone. A
        #: destructive step that does not record what it destroyed leaves
        #: the backup as the only way back.
        removed: list[dict[str, Any]] = []
        async with guarded(self._session, org_id, dry_run=dry_run) as report:
            changes.extend(await self._header_changes(org_id, header, edited))

            fixer = PurchaseLineFixService(self._session)
            for row in sorted(
                (r for r in edited.lines if not r.removed), key=lambda r: r.line_no or 0
            ):
                assert row.line_no is not None  # every row is checked above
                changes.extend(
                    await self._line_changes(org_id, actor, header, existing[row.line_no], row)
                )

            # Removals last: matching edits up by line number is only
            # safe while the numbers still mean what the form showed.
            for row in edited.lines:
                if not row.removed:
                    continue
                assert row.line_no is not None
                # Keep what the service says, not a summary of it. The
                # service's note carries the code and quantity; a bare
                # "line 4 removed" cannot be undone from, and undoing a
                # removal is the one repair someone always wants.
                gone = existing[row.line_no]
                removed.append(
                    {
                        "line_no": row.line_no,
                        "code": row.code,
                        "brand": row.brand,
                        "description": gone.description,
                        "qty": str(gone.qty),
                        "rate": str(gone.rate),
                        "weight_kg": str(gone.weight_kg) if gone.weight_kg else None,
                    }
                )
                changes.extend(
                    await fixer.fix_in_transaction(
                        org_id,
                        actor,
                        invoice_no=header.invoice_no,
                        line_no=row.line_no,
                        remove=True,
                    )
                )

            if edited.charges is not None:
                changes.extend(await self._charge_changes(actor, header, edited.charges))

            if not changes:
                raise ValidationError("Nothing on this bill would change.")

            await AuditService(self._session).record(
                org_id,
                actor.id,
                action="purchase.edited",
                entity_type="purchase_headers",
                entity_id=header.id,
                before_state={"invoice_no": invoice_no, "removed_lines": removed},
                after_state={"changes": changes},
                channel="dashboard",
            )
            for note in changes:
                report.note(note)
            # Read while the transaction is still open. A dry run rolls
            # back on the way out, which expires every loaded row, and
            # touching one afterwards re-queries on a closed transaction.
            invoice_after = header.invoice_no
            total_after = str(header.grand_total)

        return {
            "invoice_no": invoice_after,
            "changes": changes,
            "committed": report.committed,
            "grand_total": total_after,
            "dry_run": dry_run,
        }

    async def _header_changes(
        self, org_id: uuid.UUID, header: PurchaseHeader, edited: EditedBill
    ) -> list[str]:
        notes: list[str] = []
        if edited.supplier is not None and edited.supplier.strip():
            current = await self._session.get(Supplier, header.supplier_id)
            if current is None or current.name.lower() != edited.supplier.strip().lower():
                party = (
                    (
                        await self._session.execute(
                            select(Supplier).where(
                                Supplier.org_id == org_id,
                                func.lower(Supplier.name) == edited.supplier.strip().lower(),
                                Supplier.deleted_at.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if party is None:
                    raise NotFoundError("supplier", edited.supplier)
                header.supplier_id = party.id
                notes.append(f"supplier → {party.name}")

        if edited.invoice_no is not None and edited.invoice_no.strip() != header.invoice_no:
            notes.append(f"invoice no {header.invoice_no} → {edited.invoice_no.strip()}")
            header.invoice_no = edited.invoice_no.strip()

        if edited.invoice_date is not None and edited.invoice_date != header.invoice_date:
            notes.append(f"date {header.invoice_date} → {edited.invoice_date}")
            header.invoice_date = edited.invoice_date

        return notes

    async def _line_changes(
        self,
        org_id: uuid.UUID,
        actor: User,
        header: PurchaseHeader,
        line: PurchaseLine,
        row: EditedLine,
    ) -> list[str]:
        """One line's worth, through the repair service rather than by
        assignment -- each of these moves stock or cost behind it."""
        product = await self._session.get(Product, line.product_id)
        if product is None:
            raise ValidationError("that line points at a product that no longer exists")
        brand = await self._session.get(Brand, product.brand_id) if product.brand_id else None

        wanted_code = row.code.strip().upper()
        wanted_brand = (row.brand or "").strip() or None
        current_brand = brand.name if brand else None

        fields: dict[str, Any] = {}
        if wanted_code != product.code.upper():
            fields["code"] = wanted_code
        # A missing brand means "no opinion", not "clear it". The form
        # always sends back the brand it loaded, so None can only come
        # from a caller that did not mention brands -- and treating that
        # as a change produced an edit that changed nothing and was then
        # refused as empty.
        if wanted_brand is not None and wanted_brand.lower() != (current_brand or "").lower():
            fields["brand"] = wanted_brand
        if row.description is not None and row.description.strip() != (line.description or ""):
            fields["description"] = row.description.strip()
        if row.qty != line.qty:
            fields["quantity"] = row.qty
        if row.rate != line.rate:
            fields["rate"] = row.rate

        if not fields:
            return []

        # `fix_in_transaction`, not `fix`: this already holds the guard,
        # and a guard inside a guard commits the outer transaction to take
        # ownership of its own.
        notes = await PurchaseLineFixService(self._session).fix_in_transaction(
            org_id,
            actor,
            invoice_no=header.invoice_no,
            line_no=line.line_no,
            **fields,
        )
        return [f"line {line.line_no}: {note}" for note in notes]

    async def _charge_changes(
        self,
        actor: User,
        header: PurchaseHeader,
        wanted: dict[str, decimal.Decimal],
    ) -> list[str]:
        """Charges are given as a total per label, not as a delta.

        The form shows what the bill carries; the person edits the figure
        to what it should be. The difference is what gets added or taken
        off, which is why the same GST typed twice can be corrected here
        by typing the right number rather than by knowing which of the
        two to remove.
        """
        notes: list[str] = []
        charges = ChargeService(self._session)
        current_total = header.other_charges
        wanted_total = sum(wanted.values(), ZERO).quantize(TWO)
        if wanted_total == current_total:
            return notes

        delta = (wanted_total - current_total).quantize(TWO)
        label = next(iter(wanted), "CHARGES") if wanted else "CHARGES"
        if delta > ZERO:
            await charges.add_in_transaction(
                actor, reference=header.invoice_no, label=label, amount=delta
            )
            notes.append(f"charges {current_total} → {wanted_total}")
        else:
            await charges.remove_in_transaction(
                actor, reference=header.invoice_no, label=label, amount=-delta
            )
            notes.append(f"charges {current_total} → {wanted_total}")
        return notes


class SaleEditService:
    """The same screen, for a sale.

    Two differences from a bill, and both matter.

    Stock goes *out*, so correcting a quantity upward takes more of it
    and can strand a product below zero. The guard re-checks the books
    and throws the whole edit away if it does, which is why nothing here
    checks it by hand.

    **Money already received is left exactly where it is.** Editing a
    sale down to less than the customer has paid does not move their
    money back; it makes them overpaid, and says so. Silently
    re-allocating someone's payment is how a business loses track of who
    owes what -- so the difference is reported before the save and again
    after it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def detail(self, org_id: uuid.UUID, reference: str) -> dict[str, Any]:
        from backend.models import Customer, SalesLine

        header = await SaleFixService(self._session)._sale(org_id, reference)
        customer = await self._session.get(Customer, header.customer_id)
        rows = list(
            (
                await self._session.execute(
                    select(SalesLine, Product, Brand)
                    .join(Product, Product.id == SalesLine.product_id)
                    .join(Brand, Brand.id == Product.brand_id, isouter=True)
                    .where(SalesLine.sales_header_id == header.id)
                    .order_by(SalesLine.line_no)
                )
            ).all()
        )
        return {
            "reference": str(header.id)[:8],
            "sale_date": header.sale_date.isoformat(),
            "customer": customer.name if customer else "",
            "status": header.status,
            "freight": str(header.freight),
            "discount": str(header.discount),
            "other_charges": str(header.other_charges),
            "subtotal": str(header.subtotal),
            "grand_total": str(header.grand_total),
            "amount_paid": str(header.amount_paid),
            "payment_status": header.payment_status,
            "lines": [
                {
                    "line_no": line.line_no,
                    "code": product.code,
                    "brand": brand.name if brand else None,
                    "description": product.description,
                    "qty": str(line.qty),
                    "rate": str(line.rate),
                    "pieces": str(line.qty / line.weight_kg) if line.weight_kg else None,
                    "weight_kg": str(line.weight_kg) if line.weight_kg else None,
                    "line_total": str(line.line_total),
                }
                for line, product, brand in rows
            ],
        }

    async def apply(
        self,
        org_id: uuid.UUID,
        actor: User,
        *,
        reference: str,
        edited: EditedBill,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        from backend.models import SalesLine

        fixer = SaleFixService(self._session)
        header = await fixer._sale(org_id, reference)
        existing = {
            line.line_no: line
            for line in (
                await self._session.execute(
                    select(SalesLine).where(SalesLine.sales_header_id == header.id)
                )
            ).scalars()
        }
        for row in edited.lines:
            if row.line_no is None:
                raise ValidationError(
                    "A new line cannot be added to a recorded sale — it would have no "
                    "stock movement behind it. Record it as its own sale."
                )
            if row.line_no not in existing:
                raise NotFoundError("line", str(row.line_no))

        paid = header.amount_paid
        total_before = header.grand_total
        changes: list[str] = []
        removed: list[dict[str, Any]] = []

        async with guarded(self._session, org_id, dry_run=dry_run) as report:
            if edited.supplier is not None and edited.supplier.strip():
                changes.extend(
                    await fixer.fix_in_transaction(
                        org_id, actor, reference=reference, customer=edited.supplier.strip()
                    )
                )

            for row in sorted(
                (r for r in edited.lines if not r.removed), key=lambda r: r.line_no or 0
            ):
                assert row.line_no is not None
                line = existing[row.line_no]
                product = await self._session.get(Product, line.product_id)
                brand = (
                    await self._session.get(Brand, product.brand_id)
                    if product and product.brand_id
                    else None
                )
                fields: dict[str, Any] = {}
                if product is not None and row.code.strip().upper() != product.code.upper():
                    fields["code"] = row.code.strip().upper()
                wanted_brand = (row.brand or "").strip() or None
                if wanted_brand is not None and wanted_brand.lower() != (
                    brand.name.lower() if brand else ""
                ):
                    fields["brand"] = wanted_brand
                if row.qty != line.qty:
                    fields["quantity"] = row.qty
                if row.rate != line.rate:
                    fields["rate"] = row.rate
                if fields:
                    changes.extend(
                        await fixer.fix_in_transaction(
                            org_id, actor, reference=reference, line_no=row.line_no, **fields
                        )
                    )

            for row in edited.lines:
                if not row.removed:
                    continue
                assert row.line_no is not None
                gone = existing[row.line_no]
                removed.append(
                    {
                        "line_no": row.line_no,
                        "code": row.code,
                        "brand": row.brand,
                        "qty": str(gone.qty),
                        "rate": str(gone.rate),
                        "weight_kg": str(gone.weight_kg) if gone.weight_kg else None,
                    }
                )
                changes.extend(
                    await fixer.fix_in_transaction(
                        org_id, actor, reference=reference, line_no=row.line_no, remove=True
                    )
                )

            if not changes:
                raise ValidationError("Nothing on this sale would change.")

            total_after = header.grand_total
            await AuditService(self._session).record(
                org_id,
                actor.id,
                action="sale.edited",
                entity_type="sales_headers",
                entity_id=header.id,
                before_state={
                    "reference": reference,
                    "grand_total": str(total_before),
                    "removed_lines": removed,
                },
                after_state={"changes": changes, "grand_total": str(total_after)},
                channel="dashboard",
            )
            for note in changes:
                report.note(note)
            committed = report.committed

        # The payment sits where it was. What changed is how much of it
        # this sale needed, which is the thing to say out loud.
        balance = (total_after - paid).quantize(TWO)
        return {
            "reference": reference,
            "changes": changes,
            "committed": committed,
            "dry_run": dry_run,
            "amount_paid": str(paid),
            "total_before": str(total_before),
            "grand_total": str(total_after),
            "balance": str(balance),
            "overpaid": str(-balance) if balance < ZERO else "0",
            "payment_moved": False,
        }
