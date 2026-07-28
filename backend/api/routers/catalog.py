"""Products, inventory, purchases and sales -- docs/10_API.md §4.

Read paths only. Every mutating action stays on WhatsApp
(`CLAUDE.md` philosophy #5) except the two reversals docs/10_API.md
explicitly exposes, which delegate to the same UndoService the `undo`
command uses rather than reimplementing a reversal.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import select

from backend.api.amounts import money_str, qty_str
from backend.api.deps import CurrentUser, OwnerUser, Paging, Session
from backend.models import (
    Brand,
    Customer,
    Inventory,
    Product,
    PurchaseHeader,
    PurchaseLine,
    SalesHeader,
    SalesLine,
    Supplier,
)
from backend.repositories.inventory_repository import InventoryRepository
from backend.repositories.product_repository import ProductRepository

router = APIRouter(prefix="/api/v1", tags=["catalog"])


@router.get("/products")
async def list_products(
    user: CurrentUser,
    session: Session,
    paging: Paging,
    search: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    if search:
        rows = await ProductRepository(session).search(user.org_id, search, limit=paging.limit)
    else:
        rows = list(
            (
                await session.execute(
                    select(Product)
                    .where(
                        Product.org_id == user.org_id,
                        Product.deleted_at.is_(None),
                        Product.is_active.is_(True),
                    )
                    .order_by(Product.code)
                    .limit(paging.limit)
                )
            ).scalars()
        )
    return {
        "items": [
            {
                "id": str(p.id),
                "code": p.code,
                "description": p.description,
                "reorder_level": str(p.reorder_level) if p.reorder_level is not None else None,
            }
            for p in rows
        ],
        "next_cursor": None,
    }


@router.get("/inventory")
async def list_inventory(
    user: CurrentUser,
    session: Session,
    low_stock: Annotated[bool, Query()] = False,
    negative_only: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    repo = InventoryRepository(session)
    if low_stock or negative_only:
        rows = await repo.low_stock_rows(user.org_id, negative_only=negative_only)
        return {
            "items": [
                {
                    "code": r.code,
                    "description": r.description,
                    "qty_on_hand": qty_str(r.qty_on_hand),
                    "unit": r.unit_code,
                    "reorder_level": str(r.reorder_level) if r.reorder_level is not None else None,
                }
                for r in rows
            ]
        }

    # The brand comes along because a code is only unique *within* a
    # brand (`products_org_code_active_uq`). Two rows reading "VVP" with
    # no brand beside them are indistinguishable in a stock list, which
    # is the one place you most need to tell them apart.
    records = (
        await session.execute(
            select(Product, Inventory, Brand.name)
            .join(Inventory, Inventory.product_id == Product.id)
            .outerjoin(Brand, Brand.id == Product.brand_id)
            .where(
                Product.org_id == user.org_id,
                Product.deleted_at.is_(None),
                Product.is_active.is_(True),
            )
            .order_by(Brand.name.nulls_last(), Product.code)
        )
    ).all()
    totals = await repo.totals(user.org_id)
    return {
        "total_value": money_str(totals.total_value),
        "total_qty": qty_str(totals.total_qty),
        "items": [
            {
                "code": product.code,
                "brand": brand,
                "description": product.description,
                "qty_on_hand": qty_str(inv.qty_on_hand),
                "avg_cost": money_str(inv.weighted_avg_cost),
                "value": money_str(inv.qty_on_hand * inv.weighted_avg_cost),
            }
            for product, inv, brand in records
        ],
    }


@router.get("/inventory/{product_id}/movements")
async def product_movements(
    product_id: str, user: CurrentUser, session: Session, paging: Paging
) -> dict[str, Any]:
    try:
        pid = uuid.UUID(product_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such product") from None
    movements = await InventoryRepository(session).movement_history(
        user.org_id, pid, limit=paging.limit
    )
    return {
        "items": [
            {
                "at": m.created_at.isoformat(),
                "type": m.movement_type.value,
                "qty_delta": qty_str(m.qty_delta),
                "resulting_qty": qty_str(m.resulting_qty_on_hand),
                "resulting_avg_cost": money_str(m.resulting_avg_cost),
                "reason": m.reason,
            }
            for m in movements
        ],
        "next_cursor": None,
    }


@router.get("/purchases")
async def list_purchases(
    user: CurrentUser,
    session: Session,
    paging: Paging,
    date_from: Annotated[datetime.date | None, Query()] = None,
    date_to: Annotated[datetime.date | None, Query()] = None,
) -> dict[str, Any]:
    stmt = (
        select(PurchaseHeader, Supplier.name)
        .join(Supplier, Supplier.id == PurchaseHeader.supplier_id)
        .where(PurchaseHeader.org_id == user.org_id, PurchaseHeader.deleted_at.is_(None))
        .order_by(PurchaseHeader.invoice_date.desc(), PurchaseHeader.created_at.desc())
        .limit(paging.limit)
    )
    if date_from:
        stmt = stmt.where(PurchaseHeader.invoice_date >= date_from)
    if date_to:
        stmt = stmt.where(PurchaseHeader.invoice_date <= date_to)
    rows = (await session.execute(stmt)).all()
    return {
        "items": [
            {
                "id": str(h.id),
                "invoice_no": h.invoice_no,
                "date": h.invoice_date.isoformat(),
                "supplier": name,
                "grand_total": money_str(h.grand_total),
                "amount_paid": money_str(h.amount_paid),
                "status": h.status.value if hasattr(h.status, "value") else str(h.status),
                "payment_status": h.payment_status,
            }
            for h, name in rows
        ],
        "next_cursor": None,
    }


@router.get("/purchases/{purchase_id}")
async def get_purchase(purchase_id: str, user: CurrentUser, session: Session) -> dict[str, Any]:
    try:
        pid = uuid.UUID(purchase_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such purchase") from None
    header = await session.get(PurchaseHeader, pid)
    if header is None or header.org_id != user.org_id or header.deleted_at is not None:
        raise HTTPException(status_code=404, detail="no such purchase")
    supplier_name = (
        await session.execute(select(Supplier.name).where(Supplier.id == header.supplier_id))
    ).scalar_one_or_none() or ""
    lines = (
        await session.execute(
            select(PurchaseLine, Product.code)
            .join(Product, Product.id == PurchaseLine.product_id)
            .where(PurchaseLine.purchase_header_id == header.id)
            .order_by(PurchaseLine.line_no)
        )
    ).all()
    return {
        "id": str(header.id),
        "invoice_no": header.invoice_no,
        "date": header.invoice_date.isoformat(),
        "grand_total": money_str(header.grand_total),
        "amount_paid": money_str(header.amount_paid),
        "status": header.status.value if hasattr(header.status, "value") else str(header.status),
        "supplier": supplier_name,
        "invoice_date": header.invoice_date.isoformat(),
        "has_scan": header.ocr_source_attachment_id is not None,
        "scan_url": (f"/purchases/{header.id}/scan" if header.ocr_source_attachment_id else None),
        "lines": [
            {
                "line_no": line.line_no,
                "code": code,
                "description": line.description,
                "qty": qty_str(line.qty),
                "rate": money_str(line.rate),
                "line_total": money_str(line.line_total),
                "returned_qty": qty_str(line.returned_qty),
            }
            for line, code in lines
        ],
    }


@router.get("/purchases/{purchase_id}/scan")
async def purchase_scan(purchase_id: str, user: CurrentUser, session: Session) -> Response:
    """The original photographed sheet.

    This is the dashboard's one genuinely new capability over WhatsApp
    (docs/21 §3): the scan rendered beside the lines that were read out
    of it. That comparison is what builds trust in the OCR, and it is
    impossible in a chat window.

    Served through the API rather than as a static path because
    attachments are business data -- an unauthenticated URL would make
    every scanned invoice public to anyone who guessed a filename.
    """
    from pathlib import Path

    from backend.models import Attachment

    try:
        header = await session.get(PurchaseHeader, uuid.UUID(purchase_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="no such purchase") from None
    if header is None or header.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="no such purchase")
    if header.ocr_source_attachment_id is None:
        raise HTTPException(status_code=404, detail="this purchase has no scan")

    attachment = await session.get(Attachment, header.ocr_source_attachment_id)
    if attachment is None or attachment.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="this purchase has no scan")
    try:
        data = Path(attachment.file_path).read_bytes()
    except OSError:
        raise HTTPException(status_code=404, detail="the scan file is missing") from None

    return Response(
        content=data,
        media_type=attachment.mime_type or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.get("/receivables")
async def receivables(user: CurrentUser, session: Session) -> dict[str, Any]:
    """Who owes us, oldest first -- the aging question from docs/21 §2."""
    from backend.repositories.party_repository import CustomerRepository

    rows = await CustomerRepository(session).outstanding_parties(user.org_id)
    return {
        "data": [
            {
                "id": str(row.party_id),
                "name": row.name,
                "outstanding": money_str(row.outstanding),
                "oldest_date": row.oldest_date.isoformat() if row.oldest_date else None,
            }
            for row in rows
        ]
    }


@router.get("/payables")
async def payables(user: CurrentUser, session: Session) -> dict[str, Any]:
    from backend.repositories.party_repository import SupplierRepository

    rows = await SupplierRepository(session).outstanding_parties(user.org_id)
    return {
        "data": [
            {
                "id": str(row.party_id),
                "name": row.name,
                "outstanding": money_str(row.outstanding),
                "oldest_date": row.oldest_date.isoformat() if row.oldest_date else None,
            }
            for row in rows
        ]
    }


@router.get("/audit")
async def audit_log(user: OwnerUser, session: Session, paging: Paging) -> dict[str, Any]:
    """What changed and who changed it (docs/21 §3, Admin).

    Owner-only: the audit trail names people and amounts, which is
    partner-level information (docs/14 #rbac).
    """
    from backend.models import AuditLog
    from backend.models import User as UserModel

    stmt = (
        select(AuditLog, UserModel.full_name)
        .join(UserModel, UserModel.id == AuditLog.actor_user_id, isouter=True)
        .where(AuditLog.org_id == user.org_id)
        .order_by(AuditLog.created_at.desc())
        .limit(paging.limit)
    )
    after = paging.decode_after()
    if after is not None:
        stmt = stmt.where(AuditLog.created_at < after)
    rows = (await session.execute(stmt)).all()
    return {
        "data": [
            {
                "id": str(entry.id),
                "created_at": entry.created_at.isoformat(),
                "action": entry.action,
                "entity_type": entry.entity_type,
                "actor": actor,
                "channel": entry.channel,
            }
            for entry, actor in rows
        ]
    }


@router.post("/purchases/{purchase_id}/undo")
async def undo_purchase(purchase_id: str, user: OwnerUser, session: Session) -> dict[str, Any]:
    """Delegates to UndoService -- the compensating-entry reversal, not a
    second implementation of it (docs/04_Purchases.md §8)."""
    from backend.services.undo_service import UndoService

    header = await session.get(PurchaseHeader, uuid.UUID(purchase_id))
    if header is None or header.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="no such purchase")
    result = await UndoService(session).undo(user, entity="purchase", reference=header.invoice_no)
    return {
        "undone": True,
        "description": result.description,
        "cost_approximated": result.cost_approximated,
    }


@router.get("/sales")
async def list_sales(
    user: CurrentUser,
    session: Session,
    paging: Paging,
    date_from: Annotated[datetime.date | None, Query()] = None,
    date_to: Annotated[datetime.date | None, Query()] = None,
) -> dict[str, Any]:
    stmt = (
        select(SalesHeader, Customer.name)
        .join(Customer, Customer.id == SalesHeader.customer_id)
        .where(SalesHeader.org_id == user.org_id, SalesHeader.deleted_at.is_(None))
        .order_by(SalesHeader.sale_date.desc(), SalesHeader.created_at.desc())
        .limit(paging.limit)
    )
    if date_from:
        stmt = stmt.where(SalesHeader.sale_date >= date_from)
    if date_to:
        stmt = stmt.where(SalesHeader.sale_date <= date_to)
    rows = (await session.execute(stmt)).all()
    return {
        "items": [
            {
                "id": str(h.id),
                "date": h.sale_date.isoformat(),
                "customer": name,
                "grand_total": money_str(h.grand_total),
                "amount_paid": money_str(h.amount_paid),
                "payment_type": h.payment_type.value,
                "status": h.status,
            }
            for h, name in rows
        ],
        "next_cursor": None,
    }


@router.get("/sales/{sale_id}")
async def get_sale(sale_id: str, user: CurrentUser, session: Session) -> dict[str, Any]:
    try:
        sid = uuid.UUID(sale_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such sale") from None
    header = await session.get(SalesHeader, sid)
    if header is None or header.org_id != user.org_id or header.deleted_at is not None:
        raise HTTPException(status_code=404, detail="no such sale")
    lines = (
        await session.execute(
            select(SalesLine, Product.code)
            .join(Product, Product.id == SalesLine.product_id)
            .where(SalesLine.sales_header_id == header.id)
            .order_by(SalesLine.line_no)
        )
    ).all()
    return {
        "id": str(header.id),
        "date": header.sale_date.isoformat(),
        "grand_total": money_str(header.grand_total),
        "status": header.status,
        "lines": [
            {
                "line_no": line.line_no,
                "code": code,
                "qty": qty_str(line.qty),
                "rate": money_str(line.rate),
                "line_total": money_str(line.line_total),
                "avg_cost_at_sale_time": money_str(line.avg_cost_at_sale_time),
                "returned_qty": qty_str(line.returned_qty),
            }
            for line, code in lines
        ],
    }
