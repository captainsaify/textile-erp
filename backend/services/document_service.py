"""The document for one transaction -- docs/27_Documents.md.

Every confirmed purchase, sale and payment has a sheet, and the sheet
is **built from the database at the moment it is asked for**, never
stored and re-served. That is the whole design:

- A bill whose rate was corrected, or whose receipt came up short, has
  one current version and it is this one. Storing a file at confirmation
  time would leave the correction living only in the chat history while
  a stale spreadsheet kept circulating.
- Every change made since is printed on the document itself, with who
  made it and when, read straight out of `audit_logs`. A corrected sheet
  that does not say it was corrected is worse than no sheet at all --
  two copies in circulation and nothing on either saying which is
  current.

The same builder serves WhatsApp (attached to the confirmation) and the
dashboard (a download link on every row), so the two surfaces cannot
show different numbers.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import tempfile
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import String, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundError
from backend.models import (
    AuditLog,
    Brand,
    Customer,
    Product,
    PurchaseHeader,
    PurchaseLine,
    SalesHeader,
    SalesLine,
    Supplier,
    User,
)
from backend.reports.excel.purchase_sheet_template import (
    PurchaseBill,
    PurchaseSheetRow,
    build_purchase_sheet,
)

ZERO = decimal.Decimal("0")

#: How each audit action reads on a document. Anything not listed still
#: appears, by its raw action name -- an unexplained line is better than
#: a change nobody is told about.
_ACTION_WORDING = {
    "purchase.confirmed": "Purchase confirmed",
    "purchase.rate_corrected": "Rate corrected",
    "purchase.receipt_corrected": "Receipt corrected (short/excess bales)",
    "purchase.confirmed.undone": "Purchase reversed",
    "sale.created": "Sale recorded",
    "sale.created.undone": "Sale reversed",
    "payment.paid": "Payment made",
    "payment.received": "Payment received",
    "payment.reversed": "Payment reversed",
}


@dataclasses.dataclass(frozen=True)
class Document:
    path: Path
    caption: str
    #: What to say in the chat above the file.
    summary: str


def _documents_dir() -> Path:
    directory = Path(tempfile.gettempdir()) / "textile-erp-documents"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _fmt_money(value: decimal.Decimal | None) -> str:
    return f"{(value or ZERO):,.2f}"


class DocumentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------------------------------------------------------- history

    async def _history(self, org_id: uuid.UUID, *entity_ids: uuid.UUID) -> list[str]:
        """Every audited change touching this transaction, oldest first.

        Several ids because a bill's corrections are recorded against
        its *lines* -- 38 receipt corrections against purchase_lines are
        changes to the bill, and a document that omitted them would say
        a total nobody could account for.
        """
        rows = (
            (
                await self._session.execute(
                    select(AuditLog, User.full_name)
                    .join(User, User.id == AuditLog.actor_user_id, isouter=True)
                    .where(
                        AuditLog.org_id == org_id,
                        or_(*[AuditLog.entity_id == entity_id for entity_id in entity_ids]),
                    )
                    .order_by(AuditLog.created_at)
                )
            )
            .tuples()
            .all()
        )
        history: list[str] = []
        for entry, who in rows:
            when = entry.created_at.strftime("%d-%m-%Y %H:%M")
            what = _ACTION_WORDING.get(entry.action, entry.action)
            detail = _describe(entry)
            history.append(f"{when} · {who or 'system'} · {what}{detail}")
        return history

    # --------------------------------------------------------- purchase

    async def purchase(self, org_id: uuid.UUID, header_id: uuid.UUID) -> Document:
        header = await self._session.get(PurchaseHeader, header_id)
        if header is None or header.org_id != org_id:
            raise NotFoundError("purchase", str(header_id))

        supplier = await self._session.get(Supplier, header.supplier_id)
        lines = (
            (
                await self._session.execute(
                    select(PurchaseLine, Product, Brand)
                    .join(Product, Product.id == PurchaseLine.product_id, isouter=True)
                    .join(Brand, Brand.id == Product.brand_id, isouter=True)
                    .where(PurchaseLine.purchase_header_id == header_id)
                    .order_by(PurchaseLine.line_no)
                )
            )
            .tuples()
            .all()
        )

        rows = [
            PurchaseSheetRow(
                serial=index,
                # The sheet's QTY column is the piece/bale count and KG
                # the weight of one -- both derived, since a line stores
                # the costing quantity (total KG) and the per-unit weight.
                pieces=((line.qty / line.weight_kg) if line.weight_kg else None),
                description=line.description or (product.description if product else ""),
                code=product.code if product else "",
                label=brand.name if brand else "",
                weight_per_unit=line.weight_kg,
                total_weight=line.qty,
                rate=line.rate,
                amount=line.line_total,
            )
            for index, (line, product, brand) in enumerate(lines, start=1)
        ]

        notes = [
            f"Subtotal: {_fmt_money(header.subtotal)}",
            f"Freight: {_fmt_money(header.freight)}",
            f"Other charges: {_fmt_money(header.other_charges)}",
            f"Grand total: {_fmt_money(header.grand_total)}",
            f"Paid: {_fmt_money(header.amount_paid)}",
            f"Outstanding: {_fmt_money(header.grand_total - header.amount_paid)}",
        ]
        if header.status != "confirmed":
            notes.append(f"STATUS: {header.status.upper()}")

        bill = PurchaseBill(
            supplier=supplier.name if supplier else "(unknown)",
            invoice_no=header.invoice_no,
            invoice_date=header.invoice_date,
            rows=rows,
            notes=notes,
            history=await self._history(org_id, header_id, *[line.id for line, _, _ in lines]),
        )
        path = self._save(bill, f"purchase-{header.invoice_no}")
        return Document(
            path=path,
            caption=f"Purchase {header.invoice_no} — {bill.supplier}",
            summary=(
                f"📄 Purchase {header.invoice_no} — {bill.supplier}, "
                f"{len(rows)} line(s), {_fmt_money(header.grand_total)}"
            ),
        )

    # ------------------------------------------------------------- sale

    async def sale(self, org_id: uuid.UUID, sale_id: uuid.UUID) -> Document:
        header = await self._session.get(SalesHeader, sale_id)
        if header is None or header.org_id != org_id:
            raise NotFoundError("sale", str(sale_id))

        customer = await self._session.get(Customer, header.customer_id)
        lines = (
            (
                await self._session.execute(
                    select(SalesLine, Product, Brand)
                    .join(Product, Product.id == SalesLine.product_id, isouter=True)
                    .join(Brand, Brand.id == Product.brand_id, isouter=True)
                    .where(SalesLine.sales_header_id == sale_id)
                    .order_by(SalesLine.line_no)
                )
            )
            .tuples()
            .all()
        )

        rows = [
            PurchaseSheetRow(
                serial=index,
                pieces=None,
                description=product.description if product else "",
                code=product.code if product else "",
                label=brand.name if brand else "",
                weight_per_unit=None,
                total_weight=line.qty,
                rate=line.rate,
                amount=line.line_total,
            )
            for index, (line, product, brand) in enumerate(lines, start=1)
        ]

        notes = [
            f"Payment: {header.payment_type.value}",
            f"Total: {_fmt_money(header.grand_total)}",
            f"Received: {_fmt_money(header.amount_paid)}",
            f"Outstanding: {_fmt_money(header.grand_total - header.amount_paid)}",
        ]
        if header.status != "confirmed":
            notes.append(f"STATUS: {header.status.upper()}")

        reference = str(header.id)[:8]
        bill = PurchaseBill(
            supplier=customer.name if customer else "(unknown)",
            invoice_no=f"SALE-{reference}",
            invoice_date=header.sale_date,
            rows=rows,
            notes=notes,
            history=await self._history(org_id, sale_id, *[line.id for line, _, _ in lines]),
        )
        path = self._save(bill, f"sale-{reference}")
        return Document(
            path=path,
            caption=f"Sale {reference} — {bill.supplier}",
            summary=(
                f"📄 Sale {reference} — {bill.supplier}, "
                f"{len(rows)} line(s), {_fmt_money(header.grand_total)}"
            ),
        )

    # ---------------------------------------------------------- payment

    async def payment(self, org_id: uuid.UUID, reference: str) -> Document:
        """A payment's document is its receipt: what moved, and which
        bills it settled. Keyed by the audit entry, because that is what
        `undo payment` already takes and what the confirmation prints."""
        entry = (
            (
                await self._session.execute(
                    select(AuditLog).where(
                        AuditLog.org_id == org_id,
                        AuditLog.action.in_(["payment.paid", "payment.received"]),
                        cast(AuditLog.id, String).like(f"{reference.lower()}%"),
                    )
                )
            )
            .scalars()
            .first()
        )
        if entry is None:
            raise NotFoundError("payment", reference)

        state: dict[str, Any] = entry.after_state or {}
        is_payment = entry.action == "payment.paid"
        party: Supplier | Customer | None = (
            await self._session.get(Supplier, entry.entity_id)
            if is_payment
            else await self._session.get(Customer, entry.entity_id)
        )
        allocations = state.get("allocations") or []

        rows = [
            PurchaseSheetRow(
                serial=index,
                pieces=None,
                description="Settled against this bill",
                code=str(allocation.get("reference", "")),
                label="",
                weight_per_unit=None,
                total_weight=None,
                rate=None,
                amount=decimal.Decimal(str(allocation.get("applied", "0"))),
            )
            for index, allocation in enumerate(allocations, start=1)
        ]
        if not rows:
            rows = [
                PurchaseSheetRow(
                    serial=1,
                    pieces=None,
                    description="Advance — no open bill to settle",
                    code="",
                    label="",
                    weight_per_unit=None,
                    total_weight=None,
                    rate=None,
                    amount=decimal.Decimal(str(state.get("amount", "0"))),
                )
            ]

        when = state.get("entry_date") or entry.created_at.date().isoformat()
        notes = [
            f"{'Paid to' if is_payment else 'Received from'}: "
            f"{party.name if party else '(unknown)'}",
            f"Amount: {_fmt_money(decimal.Decimal(str(state.get('amount', '0'))))}",
            f"Method: {state.get('via', 'cash')}",
        ]
        if state.get("reversed"):
            notes.append("STATUS: REVERSED")

        short = str(entry.id)[:8]
        bill = PurchaseBill(
            supplier=party.name if party else "(unknown)",
            invoice_no=f"{'PAYMENT' if is_payment else 'RECEIPT'}-{short}",
            invoice_date=datetime.date.fromisoformat(str(when)),
            rows=rows,
            notes=notes,
            history=await self._history(org_id, entry.entity_id),
        )
        path = self._save(bill, f"payment-{short}")
        label = "Payment" if is_payment else "Receipt"
        return Document(
            path=path,
            caption=f"{label} {short} — {bill.supplier}",
            summary=(
                f"📄 {label} {short} — {bill.supplier}, "
                f"{_fmt_money(decimal.Decimal(str(state.get('amount', '0'))))}"
            ),
        )

    # ----------------------------------------------------------- saving

    def _save(self, bill: PurchaseBill, stem: str) -> Path:
        safe = "".join(
            character if character.isalnum() or character in "-_" else "-" for character in stem
        )
        path = _documents_dir() / f"{safe}-{uuid.uuid4().hex[:6]}.xlsx"
        build_purchase_sheet([bill], title=bill.invoice_no).save(path)
        return path


def _describe(entry: AuditLog) -> str:
    """The part of a change worth printing beside its name.

    Deliberately short: the document is a bill, not an audit export, and
    a paragraph per line would bury the numbers it exists to show.
    """
    before, after = entry.before_state or {}, entry.after_state or {}
    if entry.action == "purchase.rate_corrected":
        return f" — rate {before.get('rate', '?')} → {after.get('rate', '?')}"
    if entry.action == "purchase.receipt_corrected":
        return (
            f" — {before.get('code', '')} {before.get('qty', '?')} → {after.get('qty', '?')}"
        ).replace("  ", " ")
    if entry.action.endswith(".undone") or entry.action == "payment.reversed":
        return ""
    changed = [key for key in after if key in before and before[key] != after[key]]
    if changed:
        key = changed[0]
        return f" — {key} {before[key]} → {after[key]}"
    return ""
