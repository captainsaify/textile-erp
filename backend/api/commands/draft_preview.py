"""`sheet` -- the draft as a spreadsheet, before it is confirmed.

docs/24_DraftPreview.md. A 26-line purchase preview is a wall of text in
a chat window, and CONFIRM against something you cannot comfortably read
is how a wrong figure gets saved. The same draft, as the .xlsx the
partners already read, is checkable.

Nothing is posted. The draft stays exactly where it was, so `confirm`,
`discard` and every correction still work afterwards -- this is a
viewing aid, not a step in the flow.
"""

from __future__ import annotations

import datetime
import decimal
import tempfile
import uuid
from pathlib import Path

from backend.api.command_types import CommandResult, RequestContext
from backend.reports.excel.purchase_sheet_template import (
    PurchaseBill,
    PurchaseSheetRow,
    build_purchase_sheet,
)
from backend.services.session_service import (
    AWAITING_PURCHASE_CONFIRMATION,
    AWAITING_SALE_CONFIRMATION,
    SessionService,
)

ZERO = decimal.Decimal("0")


def _draft_dir() -> Path:
    directory = Path(tempfile.gettempdir()) / "textile-erp-drafts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


async def handle_sheet(args: str, ctx: RequestContext) -> CommandResult:
    state = await SessionService(ctx.session_factory).get(ctx.user.org_id, ctx.user.id)

    if state.state == AWAITING_PURCHASE_CONFIRMATION:
        return _purchase_sheet(state.context)
    if state.state == AWAITING_SALE_CONFIRMATION:
        return _sale_sheet(state.context)
    # No draft, but almost always a bill: `sheet` right after CONFIRM
    # used to answer "there's no draft waiting", which is true and
    # useless -- the thing being asked for exists, it is just saved now.
    return await _last_document(ctx)


async def _last_document(ctx: RequestContext) -> CommandResult:
    """The most recent purchase or sale, as its current sheet."""
    from sqlalchemy import select

    from backend.models import PurchaseHeader, SalesHeader
    from backend.services.document_service import DocumentService

    async with ctx.session_factory() as session:
        purchase = (
            await session.execute(
                select(PurchaseHeader.id, PurchaseHeader.created_at)
                .where(
                    PurchaseHeader.org_id == ctx.user.org_id, PurchaseHeader.deleted_at.is_(None)
                )
                .order_by(PurchaseHeader.created_at.desc())
                .limit(1)
            )
        ).first()
        sale = (
            await session.execute(
                select(SalesHeader.id, SalesHeader.created_at)
                .where(SalesHeader.org_id == ctx.user.org_id, SalesHeader.deleted_at.is_(None))
                .order_by(SalesHeader.created_at.desc())
                .limit(1)
            )
        ).first()

        if purchase is None and sale is None:
            return CommandResult(
                reply="There's no draft waiting, and nothing saved yet. "
                "Send a photo of a sheet, or use 'purchase' or 'sale'."
            )
        service = DocumentService(session)
        if purchase is None or (sale is not None and sale[1] > purchase[1]):
            assert sale is not None
            document = await service.sale(ctx.user.org_id, sale[0])
        else:
            document = await service.purchase(ctx.user.org_id, purchase[0])

    return CommandResult(
        reply=f"{document.summary}\nThis is the current version, including any corrections.",
        attachment=str(document.path),
        attachment_caption=document.caption,
    )


def _purchase_sheet(context: dict[str, object]) -> CommandResult:
    from backend.services.purchase_service import Draft

    draft = Draft.from_context(context)
    rows = [
        PurchaseSheetRow(
            serial=index,
            pieces=line.pieces,
            description=line.description or "",
            code=line.code,
            label=draft.brand_name or "",
            weight_per_unit=line.weight_per_unit,
            total_weight=line.qty,
            rate=line.rate,
            amount=(line.rate * line.qty) if line.rate else ZERO,
        )
        for index, line in enumerate(draft.lines, start=1)
    ]
    bill = PurchaseBill(
        supplier=draft.supplier_name or "(not set)",
        invoice_no=draft.invoice_no or "(draft)",
        invoice_date=draft.invoice_date,
        rows=rows,
    )
    path = _draft_dir() / f"draft-purchase-{uuid.uuid4().hex[:8]}.xlsx"
    build_purchase_sheet([bill], title="Draft").save(path)

    return CommandResult(
        reply=(
            f"📄 Draft sheet for {bill.invoice_no} — {len(rows)} line(s).\n"
            "Nothing is saved yet. Reply CONFIRM when it looks right, or 'discard'."
        ),
        attachment=str(path),
        attachment_caption=f"Draft — {bill.supplier}, {bill.invoice_no}",
    )


def _sale_sheet(context: dict[str, object]) -> CommandResult:
    """A sale reuses the purchase layout deliberately: it is the sheet
    the partners already know how to read, and inventing a second one to
    show four columns would be a second thing to keep right."""
    from backend.services.sales_service import SaleDraft

    draft = SaleDraft.from_context(context)
    rows = [
        PurchaseSheetRow(
            serial=index,
            pieces=None,
            description="",
            code=line.code,
            label="",
            weight_per_unit=None,
            total_weight=line.qty,
            rate=line.rate,
            amount=line.line_total,
        )
        for index, line in enumerate(draft.lines, start=1)
    ]
    bill = PurchaseBill(
        supplier=draft.customer_name or "(not set)",
        invoice_no="(draft sale)",
        invoice_date=datetime.date.today(),
        rows=rows,
    )
    path = _draft_dir() / f"draft-sale-{uuid.uuid4().hex[:8]}.xlsx"
    build_purchase_sheet([bill], title="Draft").save(path)

    return CommandResult(
        reply=(
            f"📄 Draft sale for {bill.supplier} — {len(rows)} line(s).\n"
            "Nothing is saved yet. Reply CONFIRM when it looks right, or 'discard'."
        ),
        attachment=str(path),
        attachment_caption=f"Draft sale — {bill.supplier}",
    )
