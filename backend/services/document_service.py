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
    COLUMNS,
    MONEY_COLUMNS,
    TOTALLED_COLUMNS,
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


#: Actions that mean "this document was confirmed", as opposed to
#: changed afterwards. They belong in the history but must not trip the
#: MODIFIED banner -- every document has one.
_ORIGINALS = frozenset({"purchase.confirmed", "sale.created", "payment.paid", "payment.received"})


@dataclasses.dataclass(frozen=True)
class Document:
    path: Path
    caption: str
    #: What to say in the chat above the file.
    summary: str


@dataclasses.dataclass(frozen=True)
class _Built:
    """The document, before it is rendered as anything.

    Splitting this out is what lets the workbook and the dashboard's
    on-screen sheet come from one build. They were two renderings of the
    same numbers and, being derived separately, were free to disagree --
    which is exactly what a document exists to prevent.
    """

    bill: PurchaseBill
    stem: str
    caption: str
    summary: str


@dataclasses.dataclass(frozen=True)
class Change:
    """One audited change, kept structured rather than pre-rendered.

    The same entry has to reach the sheet three ways -- as a line in the
    CHANGES block, as a marker in the NOTE cell of the rows it touched,
    and as the count in the MODIFIED banner. Formatting it once at read
    time would leave the other two re-parsing prose.
    """

    when: datetime.datetime
    who: str
    action: str
    line: str
    #: The purchase line this changed, when it changed exactly one.
    line_id: uuid.UUID | None = None
    #: Product codes a bill-wide change applied to (a rate correction
    #: names them; nothing else does).
    codes: tuple[str, ...] = ()
    #: How this reads in a row's NOTE cell. Empty when the change isn't
    #: about a particular row.
    note: str = ""

    @property
    def is_modification(self) -> bool:
        return self.action not in _ORIGINALS


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

    async def _changes(self, org_id: uuid.UUID, *entity_ids: uuid.UUID) -> list[Change]:
        """Every audited change touching this transaction, oldest first.

        Several ids because a bill's corrections are recorded against
        its *lines* -- 38 receipt corrections against purchase_lines are
        changes to the bill, and a document that omitted them would say
        a total nobody could account for.
        """
        if not entity_ids:
            return []
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
        changes: list[Change] = []
        for entry, who in rows:
            when = entry.created_at.strftime("%d-%m-%Y %H:%M")
            what = _ACTION_WORDING.get(entry.action, entry.action)
            changes.append(
                Change(
                    when=entry.created_at,
                    who=who or "system",
                    action=entry.action,
                    line=f"{when} · {who or 'system'} · {what}{_describe(entry)}",
                    line_id=(entry.entity_id if entry.entity_type == "purchase_lines" else None),
                    codes=_codes(entry),
                    note=_row_note(entry),
                )
            )
        return changes

    @staticmethod
    def _banner(changes: list[Change]) -> str:
        """The warning printed above the column headers, or nothing.

        Counted from the modifications only -- "1 change" on every
        document ever confirmed would train everyone to ignore the row.
        """
        modifications = [change for change in changes if change.is_modification]
        if not modifications:
            return ""
        last = modifications[-1]
        return (
            f"⚠ MODIFIED — {len(modifications)} change(s) since this was confirmed, "
            f"last on {last.when.strftime('%d-%m-%Y %H:%M')} by {last.who}. "
            f"Changed rows are highlighted; the full list is under CHANGES at the bottom."
        )

    @staticmethod
    def _notes_by_line(changes: list[Change]) -> dict[uuid.UUID, list[str]]:
        by_line: dict[uuid.UUID, list[str]] = {}
        for change in changes:
            if change.line_id is not None and change.note:
                by_line.setdefault(change.line_id, []).append(change.note)
        return by_line

    @staticmethod
    def _notes_by_code(changes: list[Change]) -> dict[str, list[str]]:
        """Bill-wide changes, indexed by the codes they named.

        A rate correction is recorded once against the header but is
        visible on every line it repriced, and those lines are the ones
        whose AMOUNT stopped matching the photographed sheet.
        """
        by_code: dict[str, list[str]] = {}
        for change in changes:
            if not change.note:
                continue
            for code in change.codes:
                by_code.setdefault(code.upper(), []).append(change.note)
        return by_code

    # --------------------------------------------------------- purchase

    async def purchase(self, org_id: uuid.UUID, header_id: uuid.UUID) -> Document:
        return self._render(await self._purchase_bill(org_id, header_id))

    async def purchase_view(self, org_id: uuid.UUID, header_id: uuid.UUID) -> dict[str, Any]:
        return _view(await self._purchase_bill(org_id, header_id))

    async def _purchase_bill(self, org_id: uuid.UUID, header_id: uuid.UUID) -> _Built:
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

        changes = await self._changes(org_id, header_id, *[line.id for line, _, _ in lines])
        by_line = self._notes_by_line(changes)
        by_code = self._notes_by_code(changes)

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
                # A line corrected twice keeps both, oldest first: the
                # last correction is not the whole story when someone
                # corrected a correction.
                note="; ".join(
                    by_line.get(line.id, [])
                    + by_code.get((product.code if product else "").upper(), [])
                ),
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
            history=[change.line for change in changes],
            banner=self._banner(changes),
        )
        return _Built(
            bill=bill,
            stem=f"purchase-{header.invoice_no}",
            caption=f"Purchase {header.invoice_no} — {bill.supplier}",
            summary=(
                f"📄 Purchase {header.invoice_no} — {bill.supplier}, "
                f"{len(rows)} line(s), {_fmt_money(header.grand_total)}"
            ),
        )

    # ------------------------------------------------------------- sale

    async def sale(self, org_id: uuid.UUID, sale_id: uuid.UUID) -> Document:
        return self._render(await self._sale_bill(org_id, sale_id))

    async def sale_view(self, org_id: uuid.UUID, sale_id: uuid.UUID) -> dict[str, Any]:
        return _view(await self._sale_bill(org_id, sale_id))

    async def _sale_bill(self, org_id: uuid.UUID, sale_id: uuid.UUID) -> _Built:
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
        changes = await self._changes(org_id, sale_id, *[line.id for line, _, _ in lines])
        bill = PurchaseBill(
            supplier=customer.name if customer else "(unknown)",
            invoice_no=f"SALE-{reference}",
            invoice_date=header.sale_date,
            rows=rows,
            notes=notes,
            history=[change.line for change in changes],
            banner=self._banner(changes),
        )
        return _Built(
            bill=bill,
            stem=f"sale-{reference}",
            caption=f"Sale {reference} — {bill.supplier}",
            summary=(
                f"📄 Sale {reference} — {bill.supplier}, "
                f"{len(rows)} line(s), {_fmt_money(header.grand_total)}"
            ),
        )

    # ---------------------------------------------------------- payment

    async def payment(self, org_id: uuid.UUID, reference: str) -> Document:
        return self._render(await self._payment_bill(org_id, reference))

    async def payment_view(self, org_id: uuid.UUID, reference: str) -> dict[str, Any]:
        return _view(await self._payment_bill(org_id, reference))

    async def _payment_bill(self, org_id: uuid.UUID, reference: str) -> _Built:
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
        changes = await self._changes(org_id, entry.entity_id)
        bill = PurchaseBill(
            supplier=party.name if party else "(unknown)",
            invoice_no=f"{'PAYMENT' if is_payment else 'RECEIPT'}-{short}",
            invoice_date=datetime.date.fromisoformat(str(when)),
            rows=rows,
            notes=notes,
            # A party's audit trail carries every payment ever made to
            # them, so this receipt's banner would count changes that
            # belong to other receipts. The notes already say REVERSED.
            history=[change.line for change in changes],
        )
        label = "Payment" if is_payment else "Receipt"
        return _Built(
            bill=bill,
            stem=f"payment-{short}",
            caption=f"{label} {short} — {bill.supplier}",
            summary=(
                f"📄 {label} {short} — {bill.supplier}, "
                f"{_fmt_money(decimal.Decimal(str(state.get('amount', '0'))))}"
            ),
        )

    # ----------------------------------------------------------- saving

    def _render(self, built: _Built) -> Document:
        safe = "".join(
            character if character.isalnum() or character in "-_" else "-"
            for character in built.stem
        )
        path = _documents_dir() / f"{safe}-{uuid.uuid4().hex[:6]}.xlsx"
        build_purchase_sheet([built.bill], title=built.bill.invoice_no).save(path)
        return Document(path=path, caption=built.caption, summary=built.summary)


def _view(built: _Built) -> dict[str, Any]:
    """The document as JSON, formatted exactly as the workbook formats it.

    The browser is handed finished strings rather than raw numbers so
    that the sheet on screen and the sheet that downloads from the same
    button cannot round, group or truncate differently.
    """
    bill = built.bill
    return {
        "caption": bill.caption(),
        "title": built.caption,
        "banner": bill.banner,
        "columns": [header for header, _ in COLUMNS],
        "rows": [
            {
                "cells": [_present(row, attribute) for _, attribute in COLUMNS],
                "changed": bool(row.note),
            }
            for row in bill.rows
        ],
        "totals": _totals(bill),
        "notes": bill.notes,
        "history": bill.history,
    }


def _totals(bill: PurchaseBill) -> list[str]:
    cells = [""] * len(COLUMNS)
    cells[0] = "TOTAL"
    for index in TOTALLED_COLUMNS:
        attribute = COLUMNS[index - 1][1]
        total = sum((getattr(row, attribute) or ZERO for row in bill.rows), ZERO)
        cells[index - 1] = (
            _fmt_money(total) if index in MONEY_COLUMNS else f"{total:,.3f}".rstrip("0").rstrip(".")
        )
    return cells


#: attribute -> its 1-indexed column, so a cell can be formatted the way
#: its column is without re-scanning COLUMNS per cell.
_COLUMN_INDEX = {attribute: index for index, (_, attribute) in enumerate(COLUMNS, start=1)}


def _present(row: PurchaseSheetRow, attribute: str) -> str:
    value = getattr(row, attribute)
    if value is None or value == "":
        return ""
    if isinstance(value, decimal.Decimal):
        return _fmt_money(value) if _COLUMN_INDEX[attribute] in MONEY_COLUMNS else _trim(value)
    return str(value)


def _codes(entry: AuditLog) -> tuple[str, ...]:
    """The product codes a bill-wide change named, if any.

    `purchase.rate_corrected` stores them as one comma-joined string
    because that is how the confirmation message reads them back.
    """
    raw = (entry.after_state or {}).get("codes") or (entry.before_state or {}).get("codes")
    if not raw:
        return ()
    return tuple(code.strip() for code in str(raw).split(",") if code.strip())


def _row_note(entry: AuditLog) -> str:
    """How a change reads in the NOTE cell of the rows it touched.

    Short on purpose: it shares a cell width with the rest of the table,
    and the CHANGES block below carries the full record with times and
    names for anyone who needs it.
    """
    before, after = entry.before_state or {}, entry.after_state or {}
    day = entry.created_at.strftime("%d-%m")
    if entry.action == "purchase.receipt_corrected":
        # Bales are what was counted off the truck; KG is what the
        # costing runs on. Both, because the correction was made in
        # bales and the money moved in KG.
        if before.get("pieces") and after.get("pieces"):
            return (
                f"Received {_trim(before['pieces'])} → {_trim(after['pieces'])} bales "
                f"({_trim(before.get('qty'))} → {_trim(after.get('qty'))} KG) · {day}"
            )
        return f"Received {_trim(before.get('qty'))} → {_trim(after.get('qty'))} KG · {day}"
    if entry.action == "purchase.rate_corrected":
        return f"Rate {_trim(before.get('rate'))} → {_trim(after.get('rate'))} · {day}"
    return ""


def _trim(value: Any) -> str:
    """ "800.000" reads as 800 to everyone except a computer."""
    if value is None:
        return "?"
    try:
        number = decimal.Decimal(str(value)).normalize()
    except decimal.InvalidOperation:
        return str(value)
    # normalize() renders 800 as 8E+2
    return f"{number:f}"


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
