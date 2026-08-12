"""Correcting what actually arrived -- docs/23_ReceiptCorrections.md.

A supplier bills 10 bales and 9 turn up. The invoice was wrong, so the
invoice is what gets corrected: the line reads 9, the bill total falls,
and the payable falls with it.

Quantities are expressed in **bales**, not kilograms, because that is
what someone counts on a loading bay. The kilograms follow from the
line's own per-bale weight -- 9 x 80 = 720 -- so a correction can never
disagree with the arithmetic the sheet was built on.

Three things this deliberately does *not* do:

- **It does not silently edit history.** The line changes, and a typed
  `adjustment_decrease`/`adjustment_increase` movement records the
  difference, so `qty_on_hand` still equals the signed sum of movements
  and the nightly reconciliation stays meaningful.
- **It does not restate other products' cost.** Freight is reallocated
  across the bill so the invoice is internally consistent, but only the
  corrected product's stock is moved. Restating the weighted average of
  products whose quantity never changed would mean unwinding every
  movement since, and doing that silently is how a cost basis rots.
  The reply says so.
- **It does not guess.** A line with no per-bale weight cannot be
  corrected in bales, and says so rather than inventing a conversion.
"""

from __future__ import annotations

import dataclasses
import decimal
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundError, ValidationError
from backend.core.logging import get_logger
from backend.models import (
    Product,
    PurchaseHeader,
    PurchaseLine,
    Supplier,
    User,
)
from backend.models.enums import AccountCode
from backend.services.audit_service import AuditService
from backend.services.inventory_service import InventoryService
from backend.services.journal_service import JournalService
from backend.services.purchase_service import allocate

logger = get_logger(__name__)


def _retype(movement: object, movement_type: str) -> None:
    """The inventory helpers stamp purchase/purchase_return because that
    is the arithmetic they perform. The *reason* here is a correction,
    and `stock` and the nightly reconciliation both read the type -- so
    it is restamped, rather than reusing a label that would read as
    goods physically going back to the supplier.

    Set on the object in the open transaction: inventory_movements is
    partitioned on (created_at, id), so it cannot be re-fetched by id
    alone.
    """
    movement.movement_type = movement_type  # type: ignore[attr-defined]


ZERO = decimal.Decimal("0")
TWO = decimal.Decimal("0.01")
THREE = decimal.Decimal("0.001")
FOUR = decimal.Decimal("0.0001")


@dataclasses.dataclass(frozen=True)
class CorrectionResult:
    #: The bill that changed, so the caller can send back its corrected
    #: document rather than describing the change alone.
    header_id: uuid.UUID
    invoice_no: str
    supplier_name: str
    code: str
    old_pieces: decimal.Decimal
    new_pieces: decimal.Decimal
    weight_per_piece: decimal.Decimal
    old_qty: decimal.Decimal
    new_qty: decimal.Decimal
    old_grand_total: decimal.Decimal
    new_grand_total: decimal.Decimal
    payable_after: decimal.Decimal
    resulting_qty_on_hand: decimal.Decimal
    resulting_avg_cost: decimal.Decimal
    #: True when the weighted average could not be unwound exactly --
    #: most of that batch has already been sold, so the cost basis is an
    #: approximation and a human should look at it.
    cost_approximated: bool = False
    #: True when the correction drops the bill below what has already
    #: been paid; the excess becomes an advance with the supplier.
    now_overpaid: bool = False
    freight_reallocated: bool = False


class ReceiptCorrectionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._inventory = InventoryService(session)
        self._journal = JournalService(session)
        self._audit = AuditService(session)

    async def correct(
        self,
        actor: User,
        *,
        invoice_no: str,
        code: str,
        received_pieces: decimal.Decimal,
        whatsapp_message_id: str | None = None,
    ) -> CorrectionResult:
        org_id = actor.org_id
        header, supplier = await self._find_invoice(org_id, invoice_no)
        line, product = await self._find_line(header, code)

        if line.weight_kg is None or line.weight_kg <= ZERO:
            raise ValidationError(
                f"{product.code} on {invoice_no} has no per-bale weight recorded, so I "
                "can't work in bales. Tell me the total quantity instead."
            )
        if received_pieces < ZERO:
            raise ValidationError("Received quantity can't be negative.")

        weight_per_piece = line.weight_kg
        old_qty = line.qty
        old_pieces = (old_qty / weight_per_piece).quantize(THREE)
        new_qty = (received_pieces * weight_per_piece).quantize(THREE)

        if new_qty == old_qty:
            raise ValidationError(
                f"{product.code} is already recorded as {old_pieces} × {weight_per_piece} "
                f"= {old_qty}. Nothing to change."
            )
        if new_qty < line.returned_qty:
            raise ValidationError(
                f"{line.returned_qty} of {product.code} has already gone back to the "
                f"supplier, so this can't drop below that."
            )

        old_grand_total = header.grand_total
        delta_qty = (new_qty - old_qty).quantize(THREE)

        # 1. the line itself
        line.qty = new_qty
        line.total_weight_kg = new_qty if line.total_weight_kg is not None else None
        line.line_total = (new_qty * line.rate).quantize(TWO)

        # 2. the bill. Freight is what the transporter charged and does
        #    not change because a bale is missing -- but its split across
        #    lines is by weight, so that split moves.
        lines = await self._all_lines(header)
        freight_shares = allocate(header.freight, [row.qty for row in lines])
        other_shares = allocate(header.other_charges, [row.line_total for row in lines])
        for index, row in enumerate(lines):
            row.freight_allocated = freight_shares[index]
            row.landed_cost_per_unit = (
                (row.line_total + freight_shares[index] + other_shares[index]) / row.qty
            ).quantize(FOUR)

        header.subtotal = sum((row.line_total for row in lines), ZERO).quantize(TWO)
        header.grand_total = (header.subtotal + header.freight + header.other_charges).quantize(TWO)

        # 3. stock, as a typed movement rather than an edit -- so
        #    qty_on_hand still equals the signed sum of its movements
        reason = f"receipt correction on {invoice_no}: {old_pieces} → {received_pieces} bales"
        if delta_qty < ZERO:
            movement, approximated = await self._inventory.record_purchase_return_movement(
                org_id,
                product_id=line.product_id,
                warehouse_id=header.warehouse_id,
                qty=-delta_qty,
                landed_cost_per_unit=line.landed_cost_per_unit or ZERO,
                source_id=line.id,
                created_by=actor.id,
                reason=reason,
            )
            _retype(movement, "adjustment_decrease")
        else:
            movement = await self._inventory.record_purchase_movement(
                org_id,
                product_id=line.product_id,
                warehouse_id=header.warehouse_id,
                qty=delta_qty,
                landed_cost_per_unit=line.landed_cost_per_unit or ZERO,
                source_id=line.id,
                created_by=actor.id,
            )
            approximated = False
            _retype(movement, "adjustment_increase")

        # 4. the books. A compensating entry, never a rewritten one.
        value_delta = (header.grand_total - old_grand_total).quantize(TWO)
        if value_delta != ZERO:
            magnitude = abs(value_delta)
            if value_delta < ZERO:
                debits = [(AccountCode.ACCOUNTS_PAYABLE, magnitude)]
                credits = [(AccountCode.INVENTORY, magnitude)]
            else:
                debits = [(AccountCode.INVENTORY, magnitude)]
                credits = [(AccountCode.ACCOUNTS_PAYABLE, magnitude)]
            await self._journal.post(
                org_id,
                entry_date=header.invoice_date,
                description=f"receipt correction {invoice_no} ({product.code})",
                source_type="purchase_header",
                source_id=header.id,
                created_by=actor.id,
                debits=debits,
                credits=credits,
            )

        await self._audit.record(
            org_id,
            actor.id,
            action="purchase.receipt_corrected",
            entity_type="purchase_lines",
            entity_id=line.id,
            whatsapp_message_id=whatsapp_message_id,
            before_state={
                "invoice_no": invoice_no,
                "code": product.code,
                "pieces": str(old_pieces),
                "qty": str(old_qty),
                "grand_total": str(old_grand_total),
            },
            after_state={
                "invoice_no": invoice_no,
                "code": product.code,
                "pieces": str(received_pieces),
                "qty": str(new_qty),
                "grand_total": str(header.grand_total),
            },
        )
        await self._session.flush()

        return CorrectionResult(
            header_id=header.id,
            invoice_no=invoice_no,
            supplier_name=supplier.name,
            code=product.code,
            old_pieces=old_pieces,
            new_pieces=received_pieces,
            weight_per_piece=weight_per_piece,
            old_qty=old_qty,
            new_qty=new_qty,
            old_grand_total=old_grand_total,
            new_grand_total=header.grand_total,
            payable_after=(header.grand_total - header.amount_paid).quantize(TWO),
            resulting_qty_on_hand=movement.resulting_qty_on_hand,
            resulting_avg_cost=movement.resulting_avg_cost,
            cost_approximated=approximated,
            now_overpaid=header.amount_paid > header.grand_total,
            freight_reallocated=header.freight > ZERO and len(lines) > 1,
        )

    async def _find_invoice(
        self, org_id: uuid.UUID, invoice_no: str
    ) -> tuple[PurchaseHeader, Supplier]:
        row = (
            await self._session.execute(
                select(PurchaseHeader, Supplier)
                .join(Supplier, Supplier.id == PurchaseHeader.supplier_id)
                .where(
                    PurchaseHeader.org_id == org_id,
                    func.lower(PurchaseHeader.invoice_no) == invoice_no.lower(),
                    PurchaseHeader.deleted_at.is_(None),
                    PurchaseHeader.status == "confirmed",
                )
            )
        ).first()
        if row is None:
            raise NotFoundError("purchase", invoice_no)
        return row[0], row[1]

    async def _find_line(self, header: PurchaseHeader, code: str) -> tuple[PurchaseLine, Product]:
        row = (
            await self._session.execute(
                select(PurchaseLine, Product)
                .join(Product, Product.id == PurchaseLine.product_id)
                .where(
                    PurchaseLine.purchase_header_id == header.id,
                    func.upper(Product.code) == code.upper(),
                )
            )
        ).first()
        if row is None:
            raise NotFoundError(f"{code} on invoice {header.invoice_no}", code)
        return row[0], row[1]

    async def _all_lines(self, header: PurchaseHeader) -> list[PurchaseLine]:
        return list(
            (
                await self._session.execute(
                    select(PurchaseLine)
                    .where(PurchaseLine.purchase_header_id == header.id)
                    .order_by(PurchaseLine.line_no)
                )
            ).scalars()
        )


@dataclasses.dataclass(frozen=True)
class RateChange:
    #: The bill that changed -- see CorrectionResult.header_id.
    header_id: uuid.UUID
    invoice_no: str
    supplier_name: str
    codes: list[str]
    old_rate: decimal.Decimal | None
    new_rate: decimal.Decimal
    old_grand_total: decimal.Decimal
    new_grand_total: decimal.Decimal
    payable_after: decimal.Decimal
    now_overpaid: bool
    #: Codes whose stock is partly or wholly sold. Their COGS was already
    #: booked at the old cost and is not restated here.
    partly_sold: list[str]


class RateChangeService:
    """Correct the rate on a confirmed bill -- docs/26_RateChanges.md.

    The quantity was right and the price was not, which is a different
    correction from a short delivery: nothing moves, but what the stock
    *cost* changes, and so does the bill and the payable.

    What it will not do is restate goods already sold. Their cost went
    into COGS when they were sold; reaching back through every later sale
    to re-derive margin is a different operation with a different blast
    radius, and doing it silently would rewrite profit figures the
    partners have already seen. Those codes are named in the reply.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._inventory = InventoryService(session)
        self._journal = JournalService(session)
        self._audit = AuditService(session)

    async def change(
        self,
        actor: User,
        *,
        invoice_no: str,
        new_rate: decimal.Decimal,
        codes: list[str] | None = None,
        whatsapp_message_id: str | None = None,
    ) -> RateChange:
        from backend.models import Inventory

        if new_rate <= ZERO:
            raise ValidationError("A rate has to be more than zero.")

        org_id = actor.org_id
        helper = ReceiptCorrectionService(self._session)
        header, supplier = await helper._find_invoice(org_id, invoice_no)
        lines = await helper._all_lines(header)

        wanted = {code.upper() for code in codes} if codes else None
        targets: list[tuple[PurchaseLine, Product]] = []
        for line in lines:
            product = await self._session.get(Product, line.product_id)
            if product is None:
                continue
            if wanted is None or product.code.upper() in wanted:
                targets.append((line, product))

        if not targets:
            raise NotFoundError(f"those codes on {invoice_no}", ", ".join(codes or []))
        if wanted is not None:
            missing = wanted - {product.code.upper() for _, product in targets}
            if missing:
                raise NotFoundError(f"on invoice {invoice_no}", ", ".join(sorted(missing)))

        old_rate = targets[0][0].rate if len({line.rate for line, _ in targets}) == 1 else None
        old_grand_total = header.grand_total
        old_landed = {line.id: (line.landed_cost_per_unit or ZERO) for line, _ in targets}

        for line, _ in targets:
            line.rate = new_rate
            line.line_total = (line.qty * new_rate).quantize(TWO)

        # freight splits by weight, which hasn't moved; other charges
        # split by line value, which has
        freight_shares = allocate(header.freight, [row.qty for row in lines])
        other_shares = allocate(header.other_charges, [row.line_total for row in lines])
        for index, row in enumerate(lines):
            row.freight_allocated = freight_shares[index]
            row.landed_cost_per_unit = (
                (row.line_total + freight_shares[index] + other_shares[index]) / row.qty
            ).quantize(FOUR)

        header.subtotal = sum((row.line_total for row in lines), ZERO).quantize(TWO)
        header.grand_total = (header.subtotal + header.freight + header.other_charges).quantize(TWO)

        partly_sold: list[str] = []
        for line, product in targets:
            delta = (line.landed_cost_per_unit or ZERO) - old_landed[line.id]
            if delta == ZERO:
                continue
            stock = (
                await self._session.execute(
                    select(Inventory).where(
                        Inventory.org_id == org_id,
                        Inventory.product_id == line.product_id,
                        Inventory.warehouse_id == header.warehouse_id,
                    )
                )
            ).scalar_one_or_none()
            on_hand = stock.qty_on_hand if stock is not None else ZERO
            if on_hand < line.qty:
                partly_sold.append(product.code)
            if on_hand <= ZERO:
                continue
            await self._inventory.restate_cost(
                org_id,
                product_id=line.product_id,
                warehouse_id=header.warehouse_id,
                value_delta=(delta * min(on_hand, line.qty)).quantize(TWO),
                source_id=line.id,
                created_by=actor.id,
                reason=f"rate corrected on {invoice_no}",
            )

        value_delta = (header.grand_total - old_grand_total).quantize(TWO)
        if value_delta != ZERO:
            magnitude = abs(value_delta)
            if value_delta < ZERO:
                debits = [(AccountCode.ACCOUNTS_PAYABLE, magnitude)]
                credits = [(AccountCode.INVENTORY, magnitude)]
            else:
                debits = [(AccountCode.INVENTORY, magnitude)]
                credits = [(AccountCode.ACCOUNTS_PAYABLE, magnitude)]
            await self._journal.post(
                org_id,
                entry_date=header.invoice_date,
                description=f"rate correction {invoice_no}",
                source_type="purchase_header",
                source_id=header.id,
                created_by=actor.id,
                debits=debits,
                credits=credits,
            )

        changed_codes = [product.code for _, product in targets]
        await self._audit.record(
            org_id,
            actor.id,
            action="purchase.rate_corrected",
            entity_type="purchase_headers",
            entity_id=header.id,
            whatsapp_message_id=whatsapp_message_id,
            before_state={
                "invoice_no": invoice_no,
                "codes": ", ".join(changed_codes),
                "rate": str(old_rate) if old_rate is not None else "mixed",
                "grand_total": str(old_grand_total),
            },
            after_state={
                "invoice_no": invoice_no,
                "codes": ", ".join(changed_codes),
                "rate": str(new_rate),
                "grand_total": str(header.grand_total),
            },
        )
        await self._session.flush()

        return RateChange(
            header_id=header.id,
            invoice_no=invoice_no,
            supplier_name=supplier.name,
            codes=changed_codes,
            old_rate=old_rate,
            new_rate=new_rate,
            old_grand_total=old_grand_total,
            new_grand_total=header.grand_total,
            payable_after=(header.grand_total - header.amount_paid).quantize(TWO),
            now_overpaid=header.amount_paid > header.grand_total,
            partly_sold=partly_sold,
        )


@dataclasses.dataclass(frozen=True)
class ChargeAdded:
    kind: str  # 'purchase' | 'sale'
    reference: str
    party: str
    label: str
    amount: decimal.Decimal
    other_charges: decimal.Decimal
    old_total: decimal.Decimal
    new_total: decimal.Decimal
    outstanding_after: decimal.Decimal
    partly_sold: list[str]


class ChargeService:
    """Put a charge on a bill that is already confirmed.

    Charges can be typed while a draft is open, and a draft closes the
    moment it is confirmed -- which for sales is immediately, since they
    auto-confirm when nothing looks wrong. So the words worked exactly
    when a sale was *problematic* and were unreachable when it was
    clean. GST that arrives with the paperwork an hour later had nowhere
    to go, and was being recorded as a separate operating expense: money
    the customer owes, filed on the side of the books where money leaves.

    A purchase charge is not the mirror of a sale charge:

    * On a purchase it is part of what the goods cost, so it is spread
      across the lines by value and the stock still on hand is restated.
      Goods already sold are left alone -- their cost went into COGS
      when they went out, and reaching back through every later sale
      would rewrite profit the partners have already seen.
    * On a sale it is not revenue. It credits OTHER_INCOME, so the
      revenue line and the gross margin stay about the goods.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._inventory = InventoryService(session)
        self._journal = JournalService(session)
        self._audit = AuditService(session)

    async def add(
        self,
        actor: User,
        *,
        reference: str,
        label: str,
        amount: decimal.Decimal,
        note: str | None = None,
        whatsapp_message_id: str | None = None,
    ) -> ChargeAdded:
        if amount <= ZERO:
            raise ValidationError("A charge has to be more than zero.")
        label = " ".join(label.split()).upper()

        # One transaction around the lookup *and* the work. Opening it
        # after the lookup raises "a transaction is already begun" -- the
        # select autobegins one (HANDOFF.md §5) -- and opening none at
        # all silently discards the change on session close.
        async with self._session.begin():
            header = await self._find_purchase(actor.org_id, reference)
            if header is not None:
                return await self._on_purchase(
                    actor, header, label, amount, note, whatsapp_message_id
                )
            sale = await self._find_sale(actor.org_id, reference)
            if sale is not None:
                return await self._on_sale(actor, sale, label, amount, note, whatsapp_message_id)
            raise NotFoundError("bill", reference)

    async def _find_purchase(self, org_id: uuid.UUID, reference: str) -> PurchaseHeader | None:
        return (
            (
                await self._session.execute(
                    select(PurchaseHeader).where(
                        PurchaseHeader.org_id == org_id,
                        func.lower(PurchaseHeader.invoice_no) == reference.lower(),
                        PurchaseHeader.status == "confirmed",
                        PurchaseHeader.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )

    async def _find_sale(self, org_id: uuid.UUID, reference: str) -> object | None:
        from sqlalchemy import String, cast

        from backend.models import SalesHeader

        return (
            (
                await self._session.execute(
                    select(SalesHeader).where(
                        SalesHeader.org_id == org_id,
                        SalesHeader.status == "confirmed",
                        SalesHeader.deleted_at.is_(None),
                        cast(SalesHeader.id, String).like(f"{reference.lower()}%"),
                    )
                )
            )
            .scalars()
            .first()
        )

    async def _on_sale(
        self,
        actor: User,
        sale: object,
        label: str,
        amount: decimal.Decimal,
        note: str | None,
        whatsapp_message_id: str | None,
    ) -> ChargeAdded:
        from backend.models import Customer

        org_id = actor.org_id
        if True:  # noqa: SIM108 - keeps the body's indentation stable
            customer = await self._session.get(Customer, sale.customer_id)  # type: ignore[attr-defined]
            party = customer.name if customer is not None else ""
            old_total = sale.grand_total  # type: ignore[attr-defined]
            sale.other_charges = (sale.other_charges + amount).quantize(TWO)  # type: ignore[attr-defined]
            sale.grand_total = (  # type: ignore[attr-defined]
                sale.subtotal + sale.freight + sale.other_charges  # type: ignore[attr-defined]
            ).quantize(TWO)
            if note:
                sale.notes = note  # type: ignore[attr-defined]
            # Paid in full before the charge, and now short of it.
            if sale.amount_paid < sale.grand_total:  # type: ignore[attr-defined]
                sale.payment_status = (  # type: ignore[attr-defined]
                    "partial" if sale.amount_paid > ZERO else "unpaid"  # type: ignore[attr-defined]
                )

            # Not revenue: the goods are what was sold. Folding a tax
            # into revenue inflates the gross margin with money never
            # earned on them.
            await self._journal.post(
                org_id,
                entry_date=sale.sale_date,  # type: ignore[attr-defined]
                description=f"{label} added to sale for {party}",
                source_type="sales_header_charge",
                source_id=sale.id,  # type: ignore[attr-defined]
                created_by=actor.id,
                debits=[(AccountCode.ACCOUNTS_RECEIVABLE, amount)],
                credits=[(AccountCode.OTHER_INCOME, amount)],
            )
            await self._audit.record(
                org_id,
                actor.id,
                action="sale.charge_added",
                entity_type="sales_headers",
                entity_id=sale.id,  # type: ignore[attr-defined]
                whatsapp_message_id=whatsapp_message_id,
                before_state={"grand_total": str(old_total)},
                after_state={
                    "label": label,
                    "amount": str(amount),
                    "grand_total": str(sale.grand_total),  # type: ignore[attr-defined]
                    **({"note": note} if note else {}),
                },
            )
            return ChargeAdded(
                kind="sale",
                reference=str(sale.id)[:8],  # type: ignore[attr-defined]
                party=party,
                label=label,
                amount=amount,
                other_charges=sale.other_charges,  # type: ignore[attr-defined]
                old_total=old_total,
                new_total=sale.grand_total,  # type: ignore[attr-defined]
                outstanding_after=(
                    sale.grand_total - sale.amount_paid  # type: ignore[attr-defined]
                ).quantize(TWO),
                partly_sold=[],
            )

    async def _on_purchase(
        self,
        actor: User,
        header: PurchaseHeader,
        label: str,
        amount: decimal.Decimal,
        note: str | None,
        whatsapp_message_id: str | None,
    ) -> ChargeAdded:
        from backend.models import Inventory

        org_id = actor.org_id
        if True:  # noqa: SIM108 - keeps the body's indentation stable
            supplier = await self._session.get(Supplier, header.supplier_id)
            lines = list(
                (
                    await self._session.execute(
                        select(PurchaseLine)
                        .where(PurchaseLine.purchase_header_id == header.id)
                        .order_by(PurchaseLine.line_no)
                    )
                )
                .scalars()
                .all()
            )
            if not lines:
                raise ValidationError(f"Invoice {header.invoice_no} has no lines to charge.")

            old_total = header.grand_total
            old_landed = {line.id: (line.landed_cost_per_unit or ZERO) for line in lines}
            header.other_charges = (header.other_charges + amount).quantize(TWO)

            # Spread by line value, the same basis a bill uses when it is
            # first confirmed -- so a charge added later lands where it
            # would have landed had it been typed at the time.
            freight_shares = allocate(header.freight, [row.qty for row in lines])
            other_shares = allocate(header.other_charges, [row.line_total for row in lines])
            for index, row in enumerate(lines):
                row.freight_allocated = freight_shares[index]
                row.landed_cost_per_unit = (
                    (row.line_total + freight_shares[index] + other_shares[index]) / row.qty
                ).quantize(FOUR)

            header.grand_total = (header.subtotal + header.freight + header.other_charges).quantize(
                TWO
            )
            if header.amount_paid < header.grand_total:
                header.payment_status = "partial" if header.amount_paid > ZERO else "unpaid"
            if note:
                header.notes = note

            partly_sold: list[str] = []
            for line in lines:
                delta = (line.landed_cost_per_unit or ZERO) - old_landed[line.id]
                if delta == ZERO:
                    continue
                product = await self._session.get(Product, line.product_id)
                stock = (
                    await self._session.execute(
                        select(Inventory).where(
                            Inventory.org_id == org_id,
                            Inventory.product_id == line.product_id,
                            Inventory.warehouse_id == header.warehouse_id,
                        )
                    )
                ).scalar_one_or_none()
                on_hand = stock.qty_on_hand if stock is not None else ZERO
                if on_hand < line.qty and product is not None:
                    # Its cost went into COGS when it went out. Restating
                    # that means rewriting profit already reported.
                    partly_sold.append(product.code)
                if on_hand <= ZERO:
                    continue
                await self._inventory.restate_cost(
                    org_id,
                    product_id=line.product_id,
                    warehouse_id=header.warehouse_id,
                    value_delta=(delta * min(on_hand, line.qty)).quantize(TWO),
                    source_id=line.id,
                    created_by=actor.id,
                    reason=f"{label} added to {header.invoice_no}",
                )

            # The goods cost more and the supplier is owed more.
            await self._journal.post(
                org_id,
                entry_date=header.invoice_date,
                description=f"{label} added to {header.invoice_no}",
                source_type="purchase_header",
                source_id=header.id,
                created_by=actor.id,
                debits=[(AccountCode.INVENTORY, amount)],
                credits=[(AccountCode.ACCOUNTS_PAYABLE, amount)],
            )
            await self._audit.record(
                org_id,
                actor.id,
                action="purchase.charge_added",
                entity_type="purchase_headers",
                entity_id=header.id,
                whatsapp_message_id=whatsapp_message_id,
                before_state={"grand_total": str(old_total)},
                after_state={
                    "label": label,
                    "amount": str(amount),
                    "grand_total": str(header.grand_total),
                    **({"note": note} if note else {}),
                },
            )
            return ChargeAdded(
                kind="purchase",
                reference=header.invoice_no,
                party=supplier.name if supplier else "",
                label=label,
                amount=amount,
                other_charges=header.other_charges,
                old_total=old_total,
                new_total=header.grand_total,
                outstanding_after=(header.grand_total - header.amount_paid).quantize(TWO),
                partly_sold=partly_sold,
            )
