"""Master Control — plan.md.

Everything here is reached with a *control* token, which is a separate
credential and a separate token type from the dashboard's. The router
takes `ControlUser` on every route rather than checking inside handlers,
so a new endpoint cannot be added without the check.

Nothing in here mutates yet. This is the shell the guarded write
endpoints get built into.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.amounts import money_str, qty_display
from backend.api.deps import ControlUser, Session
from backend.core.exceptions import DuplicateSaleError, ExactDuplicateInvoiceError
from backend.models import Product
from backend.models.enums import SalePaymentType
from backend.repositories.purchase_repository import PurchaseRepository
from backend.services.admin.guard import GuardRegression
from backend.services.admin.merge import PartyMergeService
from backend.services.purchase_service import Draft, DraftLine, PurchaseService
from backend.services.reconciliation_service import ReconciliationService
from backend.services.sales_service import SaleDraft, SaleDraftLine, SalesService

ZERO = decimal.Decimal("0")

router = APIRouter(prefix="/api/v1/control", tags=["control"])


@router.get("/whoami")
async def whoami(user: ControlUser) -> dict[str, Any]:
    """Who is signed in, and to which books.

    Exists so the shell can prove a control session is live before it
    renders anything -- and so the token type has one endpoint that
    tests can point at without touching data.
    """
    return {
        "user_id": str(user.id),
        "full_name": user.full_name,
        "org_id": str(user.org_id),
        "role": user.role.value,
    }


@router.get("/health")
async def books_health(user: ControlUser, session: Session) -> dict[str, Any]:
    """Do the books balance? The same check `erp check` runs.

    First thing on the Master Control screen, because every repair below
    it is only trustworthy if this is green -- and because a person who
    opens this page usually opens it *because* something looks wrong.
    """
    service = ReconciliationService(session)
    outcomes = [
        await service.check_inventory(user.org_id),
        await service.check_ledgers(user.org_id),
    ]
    return {
        "ok": all(outcome.ok for outcome in outcomes),
        "checks": [
            {
                "kind": outcome.kind,
                "checked": outcome.checked,
                "ok": outcome.ok,
                "discrepancies": [d.as_dict() for d in outcome.discrepancies],
            }
            for outcome in outcomes
        ],
    }


class LineIn(BaseModel):
    """One row of the invoice grid.

    `qty` is the costing quantity in kilograms, as everywhere else in the
    system. `pieces` and `weight_kg` are what the person counted -- bales
    and kilograms per bale -- and are carried through so the document can
    say "10 bales" and a later correction can work in bales.
    """

    code: str = Field(min_length=1)
    brand: str | None = None
    qty: decimal.Decimal = Field(gt=0)
    rate: decimal.Decimal = Field(ge=0)
    pieces: decimal.Decimal | None = Field(default=None, gt=0)
    weight_kg: decimal.Decimal | None = Field(default=None, gt=0)
    description: str | None = None


class PurchaseIn(BaseModel):
    supplier: str = Field(min_length=1)
    invoice_no: str = Field(min_length=1)
    invoice_date: datetime.date
    lines: list[LineIn] = Field(min_length=1)
    freight: decimal.Decimal = Field(default=ZERO, ge=0)
    discount: decimal.Decimal = Field(default=ZERO, ge=0)
    #: {"GST": "1200", "Packing": "800"} -- itemised, so each can be
    #: corrected on its own later rather than being one opaque total.
    charges: dict[str, decimal.Decimal] = Field(default_factory=dict)
    notes: str | None = None


class SaleIn(BaseModel):
    customer: str = Field(min_length=1)
    lines: list[LineIn] = Field(min_length=1)
    payment_type: str = "credit"
    freight: decimal.Decimal = Field(default=ZERO, ge=0)
    discount: decimal.Decimal = Field(default=ZERO, ge=0)
    charges: dict[str, decimal.Decimal] = Field(default_factory=dict)
    #: Money handed over with the goods. Becomes a real receipt in the
    #: same transaction as the sale, not a number on it.
    paid_now: decimal.Decimal = Field(default=ZERO, ge=0)
    paid_via: str = "cash"
    #: Minted when the form is rendered. A form can be submitted twice by
    #: a slow connection and an impatient thumb; a terminal command
    #: cannot. Replaying the same key returns the first sale rather than
    #: recording a second.
    idempotency_key: str | None = None
    sale_date: str | None = None


@router.post("/purchases", status_code=status.HTTP_201_CREATED)
async def create_purchase(body: PurchaseIn, user: ControlUser, session: Session) -> dict[str, Any]:
    """Record a purchase from the web form.

    Goes through `PurchaseService.confirm` -- the same call WhatsApp
    makes -- so duplicate detection, freight allocation, landed cost and
    the audit row all happen here without being reimplemented. The form
    is a second way in, never a second implementation.

    **Idempotency is the invoice number itself.** A purchase is uniquely
    a supplier plus an invoice number, enforced by a partial unique index
    on confirmed bills, so a double submit hits it. Rather than returning
    an error to someone whose thumb slipped, the existing bill is
    returned -- which is what they asked for and what already exists.
    """
    service = PurchaseService(session)
    supplier = await service.resolve_supplier(user.org_id, body.supplier)
    if supplier is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"no supplier named {body.supplier!r}",
        )

    lines: list[DraftLine] = []
    for line in body.lines:
        product = await _resolve_product(session, user.org_id, line)
        lines.append(
            DraftLine(
                code=product.code,
                qty=line.qty,
                rate=line.rate,
                product_id=product.id,
                resolved_code=product.code,
                unit_code=None,
                description=line.description,
                pieces=line.pieces,
                weight_per_unit=line.weight_kg,
                brand_id=product.brand_id,
            )
        )

    draft = Draft(
        supplier_id=supplier.id,
        supplier_name=supplier.name,
        invoice_no=body.invoice_no.strip(),
        invoice_date=body.invoice_date,
        brand_id=None,
        brand_name=None,
        lines=lines,
        freight=body.freight,
        other_charges=sum(body.charges.values(), ZERO),
        discount=body.discount,
        charges=dict(body.charges),
    )

    try:
        confirmed = await service.confirm(user, draft)
    except ExactDuplicateInvoiceError:
        existing = await PurchaseRepository(session).get_confirmed_by_invoice(
            user.org_id, supplier.id, draft.invoice_no
        )
        if existing is None:
            raise
        # The same bill, not a second one. Returning it is the honest
        # answer to a repeated submission of the same form.
        return {
            "purchase_id": str(existing.id),
            "invoice_no": existing.invoice_no,
            "grand_total": money_str(existing.grand_total),
            "already_existed": True,
        }

    await session.commit()
    return {
        "purchase_id": str(confirmed.header_id),
        "invoice_no": confirmed.invoice_no,
        "grand_total": money_str(confirmed.grand_total),
        "lines": len(confirmed.lines),
        "already_existed": False,
    }


@router.post("/sales", status_code=status.HTTP_201_CREATED)
async def create_sale(body: SaleIn, user: ControlUser, session: Session) -> dict[str, Any]:
    """Record a sale from the web form.

    Goes through `SalesService.record`, so below-cost and negative-stock
    checks, the COGS posting and any part-payment all behave exactly as
    they do over WhatsApp.
    """
    service = SalesService(session)
    match = await service.resolve_customer(user.org_id, body.customer)
    if match.exact is None:
        near = f" Did you mean {match.near[0].name!r}?" if match.near else ""
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"no customer named {body.customer!r}.{near}",
        )

    lines: list[SaleDraftLine] = []
    for line in body.lines:
        product = await _resolve_product(session, user.org_id, line)
        lines.append(
            SaleDraftLine(
                code=product.code,
                qty=line.qty,
                rate=line.rate,
                product_id=product.id,
                resolved_code=product.code,
                brand_id=product.brand_id,
                weight_per_unit=line.weight_kg,
            )
        )

    try:
        payment_type = SalePaymentType(body.payment_type.lower())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"payment must be one of: {', '.join(p.value for p in SalePaymentType)}",
        ) from None

    draft = SaleDraft(
        customer_id=match.exact.id,
        customer_name=match.exact.name,
        payment_type=payment_type,
        lines=lines,
        freight=body.freight,
        other_charges=sum(body.charges.values(), ZERO),
        discount=body.discount,
        charges=dict(body.charges),
        paid_now=body.paid_now,
        paid_via=body.paid_via,
        idempotency_key=body.idempotency_key,
        on=body.sale_date,
    )

    hydrated = await service.hydrate(user.org_id, draft)
    try:
        confirmed = await service.record(user, hydrated, below_cost_confirmed=True)
    except DuplicateSaleError as exc:
        # The same form submitted twice. Return what the first one made.
        existing = (exc.details or {}).get("sales_header_id")
        return {"sale_id": str(existing), "already_existed": True}

    await session.commit()
    return {
        "sale_id": str(confirmed.sale_id),
        "customer": confirmed.customer_name,
        "grand_total": money_str(confirmed.grand_total),
        "outstanding_after": money_str(confirmed.outstanding_after),
        "lines": len(confirmed.lines),
        "already_existed": False,
    }


async def _resolve_product(session: AsyncSession, org_id: uuid.UUID, line: LineIn) -> Product:
    """A code names a product only together with its brand.

    Three products share `55X` on these books. The form's picker sends
    the brand it displayed, so the ambiguity is resolved by what the
    person looked at rather than by a guess here -- and a code that still
    names several is an error naming them, never a pick.
    """
    from backend.admin.harness import AdminError
    from backend.admin.resolve import product_by_code

    try:
        return await product_by_code(session, org_id, line.code, line.brand)
    except AdminError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None


@router.get("/items")
async def item_picker(
    user: ControlUser,
    session: Session,
    q: Annotated[str, Query(min_length=1)],
    kind: Annotated[str, Query(pattern="^(purchase|sale)$")] = "purchase",
    limit: Annotated[int, Query(ge=1, le=25)] = 12,
) -> dict[str, Any]:
    """What the entry form's item dropdown shows.

    `CODE · BRAND · qty on hand`, because a code names a product only
    together with its brand -- three products share `55X` here, and
    something picking between them silently is what produced bills 007
    and 007B. On a screen the choice is made by looking, which is the
    single reason the form beats the chat.

    Searches code *and* description, so `zipper` finds what `55X` finds.
    The last rate comes along because the question after "which item" is
    almost always "what did we pay last time".
    """
    from sqlalchemy import or_

    from backend.models import Brand, Inventory, PurchaseLine, SalesLine

    needle = f"%{q.strip()}%"
    line_model = PurchaseLine if kind == "purchase" else SalesLine
    last_rate = (
        select(line_model.product_id, func.max(line_model.created_at).label("seen"))
        .group_by(line_model.product_id)
        .subquery()
    )

    rows = (
        await session.execute(
            select(
                Product.id,
                Product.code,
                Brand.name,
                Product.description,
                func.coalesce(Inventory.qty_on_hand, 0),
                func.coalesce(Inventory.weighted_avg_cost, 0),
                line_model.rate,
            )
            .join(Brand, Brand.id == Product.brand_id, isouter=True)
            .join(Inventory, Inventory.product_id == Product.id, isouter=True)
            .join(last_rate, last_rate.c.product_id == Product.id, isouter=True)
            .join(
                line_model,
                (line_model.product_id == Product.id) & (line_model.created_at == last_rate.c.seen),
                isouter=True,
            )
            .where(
                Product.org_id == user.org_id,
                Product.deleted_at.is_(None),
                Product.is_active.is_(True),
                or_(Product.code.ilike(needle), Product.description.ilike(needle)),
            )
            .order_by(Product.code, Brand.name)
            .limit(limit)
        )
    ).all()

    return {
        "items": [
            {
                "product_id": str(pid),
                "code": code,
                "brand": brand,
                "description": description,
                "on_hand": qty_display(qty),
                "avg_cost": money_str(decimal.Decimal(cost or 0)),
                "last_rate": money_str(decimal.Decimal(rate)) if rate is not None else None,
            }
            for pid, code, brand, description, qty, cost, rate in rows
        ]
    }


@router.get("/parties")
async def party_picker(
    user: ControlUser,
    session: Session,
    q: Annotated[str, Query(min_length=1)],
    kind: Annotated[str, Query(pattern="^(supplier|customer)$")],
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
) -> dict[str, Any]:
    """Suppliers or customers for the header field, with what they owe.

    The outstanding figure is there because the question behind picking
    a party is often "how much is already open with them", and having to
    leave the form to answer it is how a bill gets entered without it
    being asked at all.
    """
    from backend.models import Customer, Supplier
    from backend.repositories.party_repository import CustomerRepository, SupplierRepository

    needle = f"%{q.strip()}%"
    repo: Any
    rows: list[Any]
    if kind == "supplier":
        repo = SupplierRepository(session)
        rows = list(
            (
                await session.execute(
                    select(Supplier)
                    .where(
                        Supplier.org_id == user.org_id,
                        Supplier.deleted_at.is_(None),
                        Supplier.name.ilike(needle),
                    )
                    .order_by(Supplier.name)
                    .limit(limit)
                )
            ).scalars()
        )
    else:
        repo = CustomerRepository(session)
        rows = list(
            (
                await session.execute(
                    select(Customer)
                    .where(
                        Customer.org_id == user.org_id,
                        Customer.deleted_at.is_(None),
                        Customer.name.ilike(needle),
                    )
                    .order_by(Customer.name)
                    .limit(limit)
                )
            ).scalars()
        )
    return {
        "items": [
            {
                "id": str(row.id),
                "name": row.name,
                "outstanding": money_str(await repo.outstanding(user.org_id, row.id)),
            }
            for row in rows
        ]
    }


class MergeIn(BaseModel):
    """Preview and confirm take the same body.

    The confirmation is a *typed* value, not a checkbox: `confirm` must
    equal the surviving name exactly. A checkbox is clicked before it is
    read, and this is the request that makes one party stop existing.
    """

    kind: str = Field(pattern="^(supplier|customer)$")
    loser: str = Field(min_length=1)
    winner: str = Field(min_length=1)
    confirm: str | None = None


@router.post("/merge/preview")
async def merge_preview(body: MergeIn, user: ControlUser, session: Session) -> dict[str, Any]:
    """What this merge would do, computed by doing it and throwing it away.

    Not an estimate. The operation genuinely runs inside a transaction
    that is rolled back, so the figures shown are the ones the commit
    would produce -- a preview that disagrees with its commit is worse
    than no preview.
    """
    service = PartyMergeService(session)
    plan = await service.plan(user.org_id, kind=body.kind, loser=body.loser, winner=body.winner)
    try:
        result = await service.apply(user.org_id, user, plan, dry_run=True)
    except GuardRegression as exc:
        return {**plan.as_dict(), "ok": False, "blockers": exc.problems, "dry_run": True}
    return result


@router.post("/merge")
async def merge_apply(body: MergeIn, user: ControlUser, session: Session) -> dict[str, Any]:
    """Do it, having been shown what it does.

    Refuses unless `confirm` matches the surviving name exactly, and the
    guard still re-checks the books before committing -- the preview
    proves the shape, the guard proves the result.
    """
    service = PartyMergeService(session)
    plan = await service.plan(user.org_id, kind=body.kind, loser=body.loser, winner=body.winner)
    if (body.confirm or "").strip() != plan.winner_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"type {plan.winner_name!r} to confirm; nothing was changed",
        )
    try:
        result = await service.apply(user.org_id, user, plan)
    except GuardRegression as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "rolled back — the books would not have balanced",
                "problems": exc.problems,
            },
        ) from None
    await session.commit()
    return result
