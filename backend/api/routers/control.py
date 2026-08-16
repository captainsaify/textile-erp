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
from backend.models import Brand, Product, ProductType, PurchaseHeader
from backend.models.enums import CapitalEntryType, SalePaymentType
from backend.repositories.purchase_repository import PurchaseRepository
from backend.services import message_log
from backend.services.admin.billedit import (
    BillEditService,
    EditedBill,
    EditedLine,
    SaleEditService,
)
from backend.services.admin.contacts import ContactAdminService
from backend.services.admin.diagnostics import DiagnosticsService
from backend.services.admin.fixline import PurchaseLineFixService
from backend.services.admin.guard import GuardRegression, guarded
from backend.services.admin.merge import PartyMergeService
from backend.services.admin.products import ProductAdminService, replay_after_reversal
from backend.services.admin.purge import PurgeService
from backend.services.admin.salefix import SaleFixService
from backend.services.admin.stock import StockAdminService
from backend.services.backup_service import BackupService
from backend.services.capital_service import CapitalService
from backend.services.money_service import MoneyService
from backend.services.purchase_service import Draft, DraftLine, PurchaseService
from backend.services.receipt_correction_service import ChargeService
from backend.services.reconciliation_service import ReconciliationService
from backend.services.reversal_service import ReversalService
from backend.services.sales_service import SaleDraft, SaleDraftLine, SalesService
from backend.services.settlement_service import (
    PaymentEditService,
    PaymentReversalService,
    SettlementService,
)

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


class NewItemIn(BaseModel):
    """A product created from inside the entry form.

    The CLI refuses to create products mid-bill; a form does not, and
    the difference is that a person is looking at the screen. But it
    remains the easiest way to turn a typo into a second product quietly
    holding half the stock, so description and brand are required rather
    than optional: a product with a blank description is one nobody can
    identify in a stock list three weeks later.
    """

    code: str = Field(min_length=1, max_length=40)
    brand: str = Field(min_length=1, max_length=60)
    description: str = Field(min_length=2, max_length=200)


@router.post("/items", status_code=status.HTTP_201_CREATED)
async def create_item(body: NewItemIn, user: ControlUser, session: Session) -> dict[str, Any]:
    """Create a product, and its brand if that is new too.

    A code is unique *per brand*, so `55X` under a new label is a new
    product rather than a clash -- which is exactly the case this exists
    for, and the one the picker could not previously express.
    """
    from backend.models import Product

    service = PurchaseService(session)
    code = " ".join(body.code.split()).upper()
    brand = await service.resolve_or_create_brand(user.org_id, body.brand)

    existing = (
        (
            await session.execute(
                select(Product).where(
                    Product.org_id == user.org_id,
                    func.upper(Product.code) == code,
                    Product.brand_id == brand.id,
                    Product.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        # Not an error: someone typed a code that already exists under
        # that label. Hand back what is there rather than refusing a
        # form they will only fill in again.
        return {
            "product_id": str(existing.id),
            "code": existing.code,
            "brand": brand.name,
            "description": existing.description,
            "on_hand": "0",
            "created": False,
        }

    product_type = (
        (await session.execute(select(ProductType).where(ProductType.org_id == user.org_id)))
        .scalars()
        .first()
    )
    if product_type is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="no product type configured",
        )

    product = Product(
        org_id=user.org_id,
        product_type_id=product_type.id,
        code=code,
        description=body.description.strip(),
        unit_id=product_type.default_unit_id,
        brand_id=brand.id,
        created_by=user.id,
    )
    session.add(product)
    await session.flush()
    await session.commit()
    return {
        "product_id": str(product.id),
        "code": product.code,
        "brand": brand.name,
        "description": product.description,
        "on_hand": "0",
        "created": True,
    }


@router.get("/brands")
async def list_brands(user: ControlUser, session: Session) -> dict[str, Any]:
    """Every label, for the create-item form's dropdown."""
    from backend.models import Brand

    rows = list(
        (
            await session.execute(
                select(Brand.name)
                .where(Brand.org_id == user.org_id, Brand.deleted_at.is_(None))
                .order_by(Brand.name)
            )
        ).scalars()
    )
    return {"items": rows}


class PurgeIn(BaseModel):
    kind: str = Field(default="purchase", pattern="^(purchase|sale)$")
    reference: str = Field(min_length=1)
    confirm: str | None = None


@router.post("/purge/preview")
async def purge_preview(body: PurgeIn, user: ControlUser, session: Session) -> dict[str, Any]:
    """What removing this would do, computed by doing it and discarding."""
    service = PurgeService(session)
    plan = await service.plan(user.org_id, kind=body.kind, reference=body.reference)
    if not plan.ok:
        return plan.as_dict()
    try:
        return await service.apply(user.org_id, user, plan, dry_run=True)
    except GuardRegression as exc:
        return {**plan.as_dict(), "ok": False, "blockers": exc.problems, "dry_run": True}


@router.post("/purge")
async def purge_apply(body: PurgeIn, user: ControlUser, session: Session) -> dict[str, Any]:
    """Take it out of the books. Reversible with /purge/restore."""
    service = PurgeService(session)
    plan = await service.plan(user.org_id, kind=body.kind, reference=body.reference)
    if (body.confirm or "").strip() != plan.label:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"type {plan.label!r} to confirm; nothing was changed",
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


@router.post("/purge/restore")
async def purge_restore(body: PurgeIn, user: ControlUser, session: Session) -> dict[str, Any]:
    """Bring a purged record back, stock and all."""
    result = await PurgeService(session).restore(user.org_id, user, body.reference)
    await session.commit()
    return result


@router.get("/purchases/recent")
async def recent_purchases(
    user: ControlUser,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=50)] = 15,
) -> dict[str, Any]:
    """The last few bills, so removing one starts from a list rather
    than from remembering an invoice number."""
    from backend.models import Supplier

    rows = (
        await session.execute(
            select(
                PurchaseHeader.invoice_no,
                PurchaseHeader.invoice_date,
                Supplier.name,
                PurchaseHeader.grand_total,
                PurchaseHeader.amount_paid,
                PurchaseHeader.status,
            )
            .join(Supplier, Supplier.id == PurchaseHeader.supplier_id)
            .where(
                PurchaseHeader.org_id == user.org_id,
                PurchaseHeader.deleted_at.is_(None),
            )
            .order_by(PurchaseHeader.created_at.desc())
            .limit(limit)
        )
    ).all()
    return {
        "items": [
            {
                "invoice_no": invoice,
                "date": date.isoformat(),
                "supplier": supplier,
                "grand_total": money_str(total),
                "amount_paid": money_str(paid),
                "status": status_value.value
                if hasattr(status_value, "value")
                else str(status_value),
            }
            for invoice, date, supplier, total, paid, status_value in rows
        ]
    }


# --- money in and out --------------------------------------------------


class SettleIn(BaseModel):
    """Money moving, in either direction.

    `against` names a specific bill; left out, the amount is allocated
    oldest-first across whatever is open, which is what a payment on
    account actually does.
    """

    party: str = Field(min_length=1)
    amount: decimal.Decimal = Field(gt=0)
    via: str = Field(default="cash", pattern="^(cash|bank)$")
    against: str | None = None
    on: str | None = None
    note: str | None = None


@router.post("/receive", status_code=status.HTTP_201_CREATED)
async def receive(body: SettleIn, user: ControlUser, session: Session) -> dict[str, Any]:
    """Money in from a customer."""
    result = await SettlementService(session).receive_from_customer(
        user,
        customer_name=body.party,
        amount=body.amount,
        via=body.via,
        against=body.against,
        on=body.on,
        note=body.note,
        allow_advance=True,
    )
    await session.commit()
    return {
        "reference": result.reference,
        "party": result.party_name,
        "amount": money_str(result.amount),
        "allocations": [
            {"reference": a.reference, "applied": money_str(a.applied)} for a in result.allocations
        ],
        "advance": money_str(result.advance),
        "outstanding_after": money_str(result.outstanding_after),
    }


@router.post("/pay", status_code=status.HTTP_201_CREATED)
async def pay(body: SettleIn, user: ControlUser, session: Session) -> dict[str, Any]:
    """Money out to a supplier."""
    result = await SettlementService(session).pay_supplier(
        user,
        supplier_name=body.party,
        amount=body.amount,
        via=body.via,
        against=body.against,
        on=body.on,
        note=body.note,
        allow_advance=True,
    )
    await session.commit()
    return {
        "reference": result.reference,
        "party": result.party_name,
        "amount": money_str(result.amount),
        "allocations": [
            {"reference": a.reference, "applied": money_str(a.applied)} for a in result.allocations
        ],
        "advance": money_str(result.advance),
        "outstanding_after": money_str(result.outstanding_after),
    }


@router.post("/payments/{reference}/reverse")
async def reverse_payment(reference: str, user: ControlUser, session: Session) -> dict[str, Any]:
    """Take a payment back off the bills it settled, and out of the ledger.

    Both halves or neither -- reversing only the ledger would leave
    bills showing paid that nobody paid, which understates the payable
    in the direction that loses money quietly.
    """
    result = await PaymentReversalService(session).reverse(user, reference=reference)
    await session.commit()
    return {
        "party": result.party_name,
        "amount": money_str(result.amount),
        "unapplied": result.unapplied,
    }


class ExpenseIn(BaseModel):
    category: str = Field(min_length=1, max_length=60)
    amount: decimal.Decimal = Field(gt=0)
    via: str = Field(default="cash", pattern="^(cash|bank)$")
    description: str | None = None
    on: str | None = None


@router.post("/expenses", status_code=status.HTTP_201_CREATED)
async def record_expense(body: ExpenseIn, user: ControlUser, session: Session) -> dict[str, Any]:
    result = await MoneyService(session).record_expense(
        user,
        category=body.category,
        amount=body.amount,
        via=body.via,
        description=body.description,
        on=body.on,
    )
    await session.commit()
    return {
        "category": result.category,
        "amount": money_str(result.amount),
        "via": result.via,
        "balance_after": money_str(result.new_balance),
        # The service notices a category that looks like one already in
        # use -- "packing" against "packaging" -- and says so rather than
        # silently creating a second bucket that splits the reporting.
        "similar_category": result.similar_category,
    }


# --- correcting what is there -----------------------------------------


class FixLineIn(BaseModel):
    """One line of a confirmed bill.

    Only what a person can sensibly change from a screen. Quantity moves
    the stock with it and the guard checks the result, so a correction
    that would strand something below zero is refused rather than saved.
    """

    invoice_no: str = Field(min_length=1)
    line_no: int = Field(ge=1)
    code: str | None = None
    brand: str | None = None
    description: str | None = None
    rate: decimal.Decimal | None = Field(default=None, ge=0)


@router.post("/purchases/fix-line")
async def fix_purchase_line(body: FixLineIn, user: ControlUser, session: Session) -> dict[str, Any]:
    """Correct an item, its label, its description or its price.

    Re-pointing a line to another product moves its stock movements with
    it and recosts both sides -- brand lives on the product, so "this
    bill's LALA was labelled MKD" is a different product, not an edit to
    a field.
    """
    result = await PurchaseLineFixService(session).fix(
        user.org_id,
        user,
        invoice_no=body.invoice_no,
        line_no=body.line_no,
        code=body.code,
        brand=body.brand,
        description=body.description,
        rate=body.rate,
    )
    await session.commit()
    return result


# --- stock -------------------------------------------------------------


class StockAdjustIn(BaseModel):
    code: str = Field(min_length=1)
    brand: str | None = None
    qty_delta: decimal.Decimal
    reason: str = Field(pattern="^(damaged|adjust-up|adjust-down)$")
    note: str | None = None


@router.post("/stock/adjust")
async def stock_adjust(body: StockAdjustIn, user: ControlUser, session: Session) -> dict[str, Any]:
    """Move stock with no purchase or sale behind it.

    Always a typed movement, never an edit of the balance: the balance
    is derived from movements, and writing it directly makes the two
    disagree at the next reconciliation.
    """
    result = await StockAdminService(session).adjust(
        user.org_id,
        user,
        code=body.code,
        brand=body.brand,
        qty_delta=body.qty_delta,
        reason=body.reason,
        note=body.note,
    )
    await session.commit()
    return result


@router.post("/stock/recost")
async def stock_recost(user: ControlUser, session: Session) -> dict[str, Any]:
    """Rebuild every weighted average from movement history."""
    result = await StockAdminService(session).recost_all(user.org_id)
    await session.commit()
    return result


# --- reversals ---------------------------------------------------------


@router.get("/reversals")
async def list_reversals(user: ControlUser, session: Session) -> dict[str, Any]:
    """Operations that can still be put back."""
    manifests = await ReversalService(session).open_manifests(user.org_id)
    return {
        "items": [
            {
                "id": str(m.id)[:8],
                "when": m.created_at.isoformat(),
                "operation": m.operation,
                "subject": m.subject,
                "rows": len(m.payload.get("moved", [])),
            }
            for m in manifests
        ]
    }


@router.post("/reversals/{reference}/preview")
async def reversal_preview(reference: str, user: ControlUser, session: Session) -> dict[str, Any]:
    """Row by row, whether putting this back is honest."""
    service = ReversalService(session)
    manifest = await service.get(user.org_id, reference)
    plan = await service.plan(manifest)
    return {
        "subject": manifest.subject,
        "operation": manifest.operation,
        "ok": plan.ok,
        "rows": [
            {"table": r.table, "id": str(r.row_id)[:8], "state": r.state, "detail": r.detail}
            for r in plan.rows
        ],
        "blocked": [f"{r.table} {str(r.row_id)[:8]}: {r.detail}" for r in plan.blocked],
    }


@router.post("/reversals/{reference}")
async def reversal_apply(reference: str, user: ControlUser, session: Session) -> dict[str, Any]:
    service = ReversalService(session)
    manifest = await service.get(user.org_id, reference)
    plan = await service.plan(manifest)
    if not plan.ok:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "blocked — nothing was changed",
                "problems": [f"{r.table} {str(r.row_id)[:8]}: {r.detail}" for r in plan.blocked],
            },
        )
    async with guarded(session, user.org_id) as report:
        moved = await service.apply(plan, user)
        report.note(f"{moved} row(s) put back")
        # Moving rows back is only half of undoing a product merge: the
        # weighted average is a running function of the movements that
        # just moved. Skipping this does not ship a wrong number -- the
        # guard would roll the whole reversal back -- but it would make
        # undoing a merge impossible rather than wrong.
        for note in await replay_after_reversal(session, user.org_id, manifest):
            report.note(note)
    await session.commit()
    return {"subject": manifest.subject, "moved": moved, "notes": report.notes}


# --- system ------------------------------------------------------------


@router.get("/audit")
async def audit_trail(
    user: ControlUser,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """What has happened, most recent first, with who and how."""
    from backend.models import AuditLog
    from backend.models import User as UserModel

    rows = (
        await session.execute(
            select(
                AuditLog.created_at,
                AuditLog.action,
                AuditLog.channel,
                AuditLog.entity_type,
                UserModel.full_name,
            )
            .join(UserModel, UserModel.id == AuditLog.actor_user_id, isouter=True)
            .where(AuditLog.org_id == user.org_id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
    ).all()
    return {
        "items": [
            {
                "when": when.isoformat(),
                "action": action,
                "channel": channel,
                "entity": entity,
                "who": who or "—",
            }
            for when, action, channel, entity, who in rows
        ]
    }


@router.get("/backups")
async def list_backups(user: ControlUser, session: Session) -> dict[str, Any]:
    from pathlib import Path

    records = BackupService(session).list_backups()
    return {
        "items": [
            {
                "name": Path(r.file_path).name,
                "taken": r.created_at.isoformat(),
                "size_kb": round(r.size_bytes / 1024),
            }
            for r in records
        ]
    }


@router.post("/backups", status_code=status.HTTP_201_CREATED)
async def make_backup(user: ControlUser, session: Session) -> dict[str, Any]:
    from pathlib import Path

    record = await BackupService(session).create_backup(user.org_id)
    return {"name": Path(record.file_path).name, "size_kb": round(record.size_bytes / 1024)}


class NewPartyIn(BaseModel):
    kind: str = Field(pattern="^(supplier|customer)$")
    name: str = Field(min_length=2, max_length=120)


@router.post("/parties", status_code=status.HTTP_201_CREATED)
async def create_party(body: NewPartyIn, user: ControlUser, session: Session) -> dict[str, Any]:
    """Add a supplier or customer without leaving the bill.

    A new party is the ordinary case for a growing business, and being
    forced to choose from a list means either abandoning a half-typed
    bill or -- worse -- picking the nearest existing name, which is
    exactly how three sales ended up under the wrong customer.

    An existing name is returned rather than refused: someone typing a
    name that is already there means to use it, and a duplicate party is
    the thing merges exist to clean up afterwards.
    """
    name = " ".join(body.name.split())
    if body.kind == "supplier":
        service = PurchaseService(session)
        found = await service.resolve_supplier(user.org_id, name)
        if found is not None:
            return {"id": str(found.id), "name": found.name, "created": False}
        supplier = await service.create_supplier(user, name)
        await session.commit()
        return {"id": str(supplier.id), "name": supplier.name, "created": True}

    sales = SalesService(session)
    match = await sales.resolve_customer(user.org_id, name)
    if match.exact is not None:
        return {"id": str(match.exact.id), "name": match.exact.name, "created": False}
    customer = await sales.create_customer(user, name)
    await session.commit()
    return {"id": str(customer.id), "name": customer.name, "created": True}


class FixSaleIn(BaseModel):
    """Correcting a sale after the fact.

    The commonest repair here by some distance -- three sales went to the
    wrong customer in one month. Moving a sale touches no stock, since
    the goods left either way; changing the item does, and the guard
    checks the result.
    """

    reference: str = Field(min_length=1)
    customer: str | None = None
    line_no: int | None = Field(default=None, ge=1)
    code: str | None = None
    brand: str | None = None


@router.post("/sales/fix")
async def fix_sale(body: FixSaleIn, user: ControlUser, session: Session) -> dict[str, Any]:
    result = await SaleFixService(session).fix(
        user.org_id,
        user,
        reference=body.reference,
        customer=body.customer,
        line_no=body.line_no,
        code=body.code,
        brand=body.brand,
    )
    await session.commit()
    return result


@router.get("/sales/recent")
async def recent_sales(
    user: ControlUser,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=50)] = 15,
) -> dict[str, Any]:
    from backend.models import Customer, SalesHeader

    rows = (
        await session.execute(
            select(
                SalesHeader.id,
                SalesHeader.sale_date,
                Customer.name,
                SalesHeader.grand_total,
                SalesHeader.amount_paid,
            )
            .join(Customer, Customer.id == SalesHeader.customer_id)
            .where(SalesHeader.org_id == user.org_id, SalesHeader.deleted_at.is_(None))
            .order_by(SalesHeader.created_at.desc())
            .limit(limit)
        )
    ).all()
    return {
        "items": [
            {
                "reference": str(sale_id)[:8],
                "date": date.isoformat(),
                "customer": customer,
                "grand_total": money_str(total),
                "amount_paid": money_str(paid),
            }
            for sale_id, date, customer, total, paid in rows
        ]
    }


# --- products ----------------------------------------------------------


@router.get("/products")
async def list_products(
    user: ControlUser,
    session: Session,
    q: Annotated[str, Query(max_length=60)] = "",
) -> dict[str, Any]:
    """The catalogue, with what has happened to each row.

    The counts are what tell a duplicate from a real product, and what
    tell a deletable row from one carrying history. Without them the
    screen would be a list of codes and the person would be guessing.
    """
    return {"items": await ProductAdminService(session).catalogue(user.org_id, query=q)}


class ProductMergeIn(BaseModel):
    loser_code: str = Field(min_length=1)
    loser_brand: str | None = None
    winner_code: str = Field(min_length=1)
    winner_brand: str | None = None
    confirm: str | None = None


@router.post("/products/merge/preview")
async def product_merge_preview(
    body: ProductMergeIn, user: ControlUser, session: Session
) -> dict[str, Any]:
    """Really done, then thrown away. The averages shown are the ones the
    commit would produce, because they were produced."""
    service = ProductAdminService(session)
    plan = await service.merge_plan(
        user.org_id,
        loser_code=body.loser_code,
        loser_brand=body.loser_brand,
        winner_code=body.winner_code,
        winner_brand=body.winner_brand,
    )
    if not plan.ok:
        return plan.as_dict()
    try:
        return await service.merge_apply(user.org_id, user, plan, dry_run=True)
    except GuardRegression as exc:
        return {**plan.as_dict(), "ok": False, "blockers": exc.problems, "dry_run": True}


@router.post("/products/merge")
async def product_merge(
    body: ProductMergeIn, user: ControlUser, session: Session
) -> dict[str, Any]:
    service = ProductAdminService(session)
    plan = await service.merge_plan(
        user.org_id,
        loser_code=body.loser_code,
        loser_brand=body.loser_brand,
        winner_code=body.winner_code,
        winner_brand=body.winner_brand,
    )
    # Typed back, not clicked. This one folds two histories into one and
    # the surviving product's cost changes as a result.
    if (body.confirm or "").strip() != plan.winner_label:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"type {plan.winner_label!r} to confirm; nothing was changed",
        )
    try:
        result = await service.merge_apply(user.org_id, user, plan)
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


class ProductDeleteIn(BaseModel):
    code: str = Field(min_length=1)
    brand: str | None = None


@router.post("/products/delete")
async def product_delete(
    body: ProductDeleteIn, user: ControlUser, session: Session
) -> dict[str, Any]:
    """Only a product nothing has ever happened to. Anything else is a
    merge, and the error says so."""
    result = await ProductAdminService(session).delete(
        user.org_id, user, code=body.code, brand=body.brand
    )
    await session.commit()
    return result


# --- contacts ----------------------------------------------------------


@router.get("/contacts")
async def list_contacts(user: ControlUser, session: Session) -> dict[str, Any]:
    """Who can reach the system, and when they last did."""
    return {"items": await ContactAdminService(session).contacts(user.org_id)}


class RelinkIn(BaseModel):
    number: str = Field(min_length=6, max_length=20)
    user: str = Field(min_length=2, max_length=120)


@router.post("/contacts/relink")
async def relink_contact(body: RelinkIn, user: ControlUser, session: Session) -> dict[str, Any]:
    """Point a WhatsApp number at a person.

    The repair for a partner who changed SIM. Until it is run, their
    messages reach an unrecognised number and the system's correct
    response to an unrecognised number is silence -- so the symptom is
    "nothing happens", which is the hardest kind to diagnose.
    """
    result = await ContactAdminService(session).relink(
        user.org_id, user, number=body.number, to_name=body.user
    )
    await session.commit()
    return result


class UnlinkIn(BaseModel):
    user: str = Field(min_length=2, max_length=120)


@router.post("/contacts/unlink")
async def unlink_contact(body: UnlinkIn, user: ControlUser, session: Session) -> dict[str, Any]:
    """For a SIM that is gone rather than moved."""
    result = await ContactAdminService(session).unlink(user.org_id, user, name=body.user)
    await session.commit()
    return result


@router.get("/purchases/{invoice_no}")
async def purchase_detail(invoice_no: str, user: ControlUser, session: Session) -> dict[str, Any]:
    """A confirmed bill, shaped for the entry form to reopen it."""
    return await BillEditService(session).detail(user.org_id, invoice_no)


class EditLineIn(BaseModel):
    line_no: int | None = None
    code: str = Field(min_length=1)
    brand: str | None = None
    description: str | None = None
    qty: decimal.Decimal = Field(gt=0)
    rate: decimal.Decimal = Field(ge=0)
    removed: bool = False


class EditBillIn(BaseModel):
    supplier: str | None = None
    invoice_no: str | None = None
    invoice_date: datetime.date | None = None
    lines: list[EditLineIn] = Field(default_factory=list)
    charges: dict[str, decimal.Decimal] | None = None


def _edited(body: EditBillIn) -> EditedBill:
    return EditedBill(
        supplier=body.supplier,
        invoice_no=body.invoice_no,
        invoice_date=body.invoice_date,
        lines=[
            EditedLine(
                line_no=row.line_no,
                code=row.code,
                brand=row.brand,
                description=row.description,
                qty=row.qty,
                rate=row.rate,
                removed=row.removed,
            )
            for row in body.lines
        ],
        charges=body.charges,
    )


@router.post("/purchases/{invoice_no}/edit/preview")
async def preview_bill_edit(
    invoice_no: str, body: EditBillIn, user: ControlUser, session: Session
) -> dict[str, Any]:
    """What saving would change, computed by doing it and rolling back.

    The same call as the save below with `dry_run` -- so the list shown
    is what the commit would produce, not a prediction of it. A preview
    that disagrees with its commit is worse than no preview.
    """
    try:
        return await BillEditService(session).apply(
            user.org_id, user, invoice_no=invoice_no, edited=_edited(body), dry_run=True
        )
    except GuardRegression as exc:
        return {"invoice_no": invoice_no, "changes": [], "ok": False, "blockers": exc.problems}


@router.post("/purchases/{invoice_no}/edit")
async def apply_bill_edit(
    invoice_no: str, body: EditBillIn, user: ControlUser, session: Session
) -> dict[str, Any]:
    """Save the edited bill.

    Every change runs the repair the terminal runs for it -- a quantity
    moves its stock movement and replays cost, a code or brand moves the
    movements to another product, a rate replays, charges re-allocate --
    inside one guard that throws the whole edit away if the books stop
    balancing.
    """
    result = await BillEditService(session).apply(
        user.org_id, user, invoice_no=invoice_no, edited=_edited(body)
    )
    await session.commit()
    return result


#: What each recorded action is called on the Activity page, and whether
#: this screen can take it back. "Undoable" is deliberately narrow: an
#: action is listed as undoable only where an inverse exists that keeps
#: stock, cost and the ledger honest. Everything else is shown with what
#: to do instead, which is more use than a button that half-works.
ACTIVITY_LABELS: dict[str, tuple[str, str]] = {
    "purchase.edited": ("Bill corrected", "restore_lines"),
    "sale.edited": ("Sale corrected", "restore_sale_lines"),
    "purchase.charge_added": ("Charge added to a bill", "charge"),
    "purchase.charge_removed": ("Charge taken off a bill", "charge"),
    "payment.paid": ("Paid a supplier", "payment"),
    "payment.received": ("Received from a customer", "payment"),
    "payment.edited": ("Payment corrected", "payment_edit"),
    "payment.reversed": ("Payment reversed", ""),
    # These five go through `UndoService`, which was already here and
    # already writes compensating entries rather than deleting rows. The
    # page dispatches to it rather than growing a second idea of what
    # undoing a purchase means.
    "purchase.confirmed": ("Purchase recorded", "undo:purchase"),
    "sale.created": ("Sale recorded", "undo:sale"),
    "expense.created": ("Expense recorded", "undo:expense"),
    "income.created": ("Income recorded", "undo:income"),
    "capital.contribution": ("Partner put capital in", "undo:capital"),
    "capital.withdrawal": ("Partner took capital out", "undo:capital"),
    "product.described": ("Product renamed", "rename"),
    "purchase.fixed": ("Bill line corrected", ""),
    "purchase.rate_corrected": ("Bill rate corrected", ""),
    "purchase.receipt_corrected": ("Receipt corrected", ""),
    "sale.fixed": ("Sale line corrected", ""),
}


@router.get("/activity")
async def activity(
    user: ControlUser, session: Session, limit: Annotated[int, Query(ge=1, le=100)] = 40
) -> dict[str, Any]:
    """Everything that has been done, newest first, with what can be undone.

    Read from `audit_logs`, which is the only record that spans every
    kind of change -- bills, payments, capital, renames. Reversal
    manifests (merges, purges, re-links) have their own undo on the
    System page and are not repeated here.
    """
    from backend.models import AuditLog, User

    rows = list(
        (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.org_id == user.org_id)
                .order_by(AuditLog.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    actors: dict[uuid.UUID, str] = {}
    items = []
    for row in rows:
        if row.actor_user_id not in actors:
            who = await session.get(User, row.actor_user_id)
            actors[row.actor_user_id] = who.full_name if who else "—"
        label, undo_kind = ACTIVITY_LABELS.get(row.action, (row.action, ""))
        before = row.before_state or {}
        after = row.after_state or {}
        removed = before.get("removed_lines") or []
        if undo_kind in {"restore_lines", "restore_sale_lines"} and not removed:
            # A correction that removed nothing has nothing this screen
            # can put back: the field changes are undone by editing the
            # bill again, which is a different act with its own record.
            undo_kind = ""
        detail = after.get("changes") or []
        items.append(
            {
                "id": str(row.id)[:8],
                "at": row.created_at.isoformat(),
                "action": row.action,
                "label": label,
                "who": actors[row.actor_user_id],
                "channel": row.channel,
                "detail": detail if isinstance(detail, list) else [str(detail)],
                "subject": before.get("invoice_no") or after.get("invoice_no") or "",
                "undo": undo_kind,
                "undone": bool(after.get("reversed") or after.get("undone")),
            }
        )
    return {"items": items}


@router.post("/activity/{reference}/undo")
async def undo_activity(reference: str, user: ControlUser, session: Session) -> dict[str, Any]:
    """Take back one recorded action.

    Each kind goes through the inverse that already exists for it rather
    than through anything written specially here -- a payment through the
    reversal that unwinds its allocations, a charge through the charge
    path with the sign flipped, a removed line through the restore that
    brings its stock movement back with it.
    """
    from sqlalchemy import String, cast

    from backend.models import AuditLog

    entry = (
        (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.org_id == user.org_id,
                    cast(AuditLog.id, String).like(f"{reference.lower()}%"),
                )
            )
        )
        .scalars()
        .first()
    )
    if entry is None:
        raise HTTPException(status_code=404, detail=f"no activity {reference!r}")

    _, kind = ACTIVITY_LABELS.get(entry.action, ("", ""))
    before = entry.before_state or {}
    after = entry.after_state or {}
    notes: list[str] = []

    if kind == "payment":
        undone_payment = await PaymentReversalService(session).reverse(user, reference=reference)
        notes.append(
            f"{undone_payment.kind} {money_str(undone_payment.amount)} "
            f"to {undone_payment.party_name} reversed"
        )

    elif kind == "charge":
        label = str(after.get("label") or "CHARGES")
        amount = decimal.Decimal(str(after.get("amount") or "0"))
        invoice = str(after.get("invoice_no") or before.get("invoice_no") or "")
        service = ChargeService(session)
        if entry.action == "purchase.charge_added":
            await service.remove_in_transaction(user, reference=invoice, label=label, amount=amount)
            notes.append(f"{label} {money_str(amount)} taken back off {invoice}")
        else:
            await service.add_in_transaction(user, reference=invoice, label=label, amount=amount)
            notes.append(f"{label} {money_str(amount)} put back on {invoice}")

    elif kind == "restore_lines":
        invoice = str(before.get("invoice_no") or "")
        fixer = PurchaseLineFixService(session)
        async with guarded(session, user.org_id) as report:
            for line in before.get("removed_lines") or []:
                notes.extend(
                    await fixer.restore_line(user.org_id, user, invoice_no=invoice, line=line)
                )
            for note in notes:
                report.note(note)
        if not report.committed:
            raise HTTPException(status_code=409, detail="the books did not balance; nothing saved")

    elif kind.startswith("undo:"):
        # purchase, sale, expense, income, capital -- all of which
        # `UndoService` already reverses with compensating entries.
        from backend.services.undo_service import UndoService

        entity = kind.split(":", 1)[1]
        subject = str(
            after.get("invoice_no") or before.get("invoice_no") or str(entry.entity_id)[:8]
        )
        undone = await UndoService(session).undo(user, entity=entity, reference=subject)
        notes.append(getattr(undone, "summary", f"{entity} {subject} undone"))

    elif kind == "rename":
        old_name = str((before or {}).get("description") or "")
        if not old_name:
            raise HTTPException(status_code=400, detail="the previous name was not recorded")
        product = await session.get(Product, entry.entity_id)
        if product is None:
            raise HTTPException(status_code=404, detail="that product no longer exists")
        brand_row = await session.get(Brand, product.brand_id) if product.brand_id else None
        await ProductAdminService(session).describe(
            user.org_id,
            user,
            code=product.code,
            brand=brand_row.name if brand_row else None,
            description=old_name,
        )
        notes.append(f"{product.code} named back to {old_name}")

    elif kind == "payment_edit":
        # The edit reversed the old payment and recorded a new one, so
        # undoing it is editing back to the figures it started from --
        # which the audit row kept for exactly this.
        target = str(after.get("reference") or "")
        previous = before.get("amount")
        if not target or previous is None:
            raise HTTPException(status_code=400, detail="the original figures were not recorded")
        rolled_back = await PaymentEditService(session).edit(
            user,
            reference=target,
            amount=decimal.Decimal(str(previous)),
            via=str(before.get("via") or "cash"),
            on=str(before.get("entry_date")) if before.get("entry_date") else None,
        )
        notes.append(
            f"{rolled_back.party_name}: {money_str(rolled_back.old_amount)} → "
            f"{money_str(rolled_back.new_amount)}"
        )

    elif kind == "restore_sale_lines":
        sale_ref = str(before.get("reference") or "")
        sale_fixer = SaleFixService(session)
        async with guarded(session, user.org_id) as report:
            for line in before.get("removed_lines") or []:
                notes.extend(
                    await sale_fixer.restore_line(
                        user.org_id, user, reference=sale_ref, line=line
                    )
                )
            for note in notes:
                report.note(note)
        if not report.committed:
            raise HTTPException(status_code=409, detail="the books did not balance; nothing saved")

    else:
        raise HTTPException(
            status_code=400,
            detail=(
                "That one cannot be taken back from here. Correct it with a new "
                "entry, or use the System page for merges, purges and re-links."
            ),
        )

    # Stamp it, so the same action cannot be undone twice.
    entry.after_state = {**after, "undone": True}
    await session.commit()
    return {"reference": reference, "notes": notes}


@router.get("/sales/{reference}")
async def sale_detail(reference: str, user: ControlUser, session: Session) -> dict[str, Any]:
    """A recorded sale, shaped for the entry form to reopen it."""
    return await SaleEditService(session).detail(user.org_id, reference)


@router.post("/sales/{reference}/edit/preview")
async def preview_sale_edit(
    reference: str, body: EditBillIn, user: ControlUser, session: Session
) -> dict[str, Any]:
    """What saving would change, and what it does to what the customer owes.

    The payment is *not* moved, so the figures here are the ones the
    screen asks about before committing: what was paid, what the sale
    would come to, and the difference either way.
    """
    try:
        return await SaleEditService(session).apply(
            user.org_id, user, reference=reference, edited=_edited(body), dry_run=True
        )
    except GuardRegression as exc:
        return {"reference": reference, "changes": [], "ok": False, "blockers": exc.problems}


@router.post("/sales/{reference}/edit")
async def apply_sale_edit(
    reference: str, body: EditBillIn, user: ControlUser, session: Session
) -> dict[str, Any]:
    """Save the edited sale, leaving money already received where it is."""
    result = await SaleEditService(session).apply(
        user.org_id, user, reference=reference, edited=_edited(body)
    )
    await session.commit()
    return result


@router.get("/payments/recent")
async def recent_payments(
    user: ControlUser,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    include_reversed: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Payments in and out, newest first, so one can be picked to correct.

    Read from `audit_logs` because that row *is* the payment's handle --
    the reference on the receipt, what `undo payment` takes, and what the
    edit below takes. There is no payments table to read instead.

    **Reversed ones are left out unless asked for.** A reversed payment
    is not a payment any more: it settled nothing and moved nothing on
    balance, and correcting one is refused anyway. Every correction
    leaves one behind, so showing them means the list fills with pairs
    where only one half can be acted on. They are one checkbox away,
    because "where did that payment go" is still a question people ask
    and an absence cannot answer it.
    """
    from sqlalchemy import or_

    from backend.models import AuditLog, Customer, Supplier

    hide_reversed = (
        []
        if include_reversed
        else [
            or_(
                AuditLog.after_state["reversed"].astext.is_(None),
                AuditLog.after_state["reversed"].astext != "true",
            )
        ]
    )

    rows = list(
        (
            await session.execute(
                select(AuditLog)
                .where(
                    AuditLog.org_id == user.org_id,
                    AuditLog.action.in_(["payment.paid", "payment.received"]),
                    *hide_reversed,
                )
                .order_by(AuditLog.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    items = []
    for row in rows:
        state = row.after_state or {}
        paid_out = row.action == "payment.paid"
        party = (
            await session.get(Supplier, row.entity_id)
            if paid_out
            else await session.get(Customer, row.entity_id)
        )
        items.append(
            {
                "reference": str(row.id)[:8],
                "kind": "paid" if paid_out else "received",
                "party": party.name if party is not None else "—",
                "amount": money_str(decimal.Decimal(str(state.get("amount", "0")))),
                "via": state.get("via", "cash"),
                "date": state.get("entry_date"),
                "note": state.get("note"),
                # A reversed payment is shown rather than hidden: "where
                # did that payment go" is a question people ask, and an
                # absence cannot answer it.
                "reversed": bool(state.get("reversed")),
            }
        )
    return {"items": items}


class PaymentEditIn(BaseModel):
    amount: decimal.Decimal | None = Field(default=None, gt=0)
    via: str | None = Field(default=None, pattern="^(cash|bank)$")
    on: str | None = None
    note: str | None = None


@router.post("/payments/{reference}/edit")
async def edit_payment(
    reference: str, body: PaymentEditIn, user: ControlUser, session: Session
) -> dict[str, Any]:
    """Correct the amount, date, or cash-or-bank on a payment.

    Reverses and re-records rather than editing in place: a settlement is
    a ledger entry, a journal pair, and an allocation against every bill
    it settled, and changing the amount alone would leave those bills
    describing a payment that no longer happened. Three ledger rows
    result -- the original, the reversal, the correction -- which is the
    honest record of what occurred.
    """
    result = await PaymentEditService(session).edit(
        user,
        reference=reference,
        amount=body.amount,
        via=body.via,
        on=body.on,
        note=body.note,
    )
    await session.commit()
    return {
        "kind": result.kind,
        "party": result.party_name,
        "old_amount": money_str(result.old_amount),
        "amount": money_str(result.new_amount),
        "old_reference": result.old_reference,
        "reference": result.reference,
        "via": result.via,
        "date": result.entry_date.isoformat(),
        "outstanding_after": money_str(result.outstanding_after),
        "unapplied": result.unapplied,
    }


# --- partners ----------------------------------------------------------


@router.get("/partners")
async def list_partners(user: ControlUser, session: Session) -> dict[str, Any]:
    """Every partner and what their capital stands at.

    Doubles as the picker for the form below, so the name typed into it
    can only ever be a name the books already know.
    """
    from backend.repositories.accounting_repository import PartnerCapitalRepository
    from backend.repositories.party_repository import PartnerRepository

    capital = PartnerCapitalRepository(session)
    items = []
    for partner in await PartnerRepository(session).list_active(user.org_id):
        balance = await capital.balance(user.org_id, partner.id)
        items.append(
            {
                "name": partner.display_name,
                "balance": money_str(balance),
                "negative": balance < decimal.Decimal("0"),
            }
        )
    return {"items": items}


class CapitalIn(BaseModel):
    partner: str = Field(min_length=1, max_length=120)
    direction: str = Field(pattern="^(in|out)$")
    amount: decimal.Decimal = Field(gt=0)
    via: str = Field(default="cash", pattern="^(cash|bank)$")
    on: str | None = None


@router.post("/partners/capital", status_code=status.HTTP_201_CREATED)
async def move_capital(body: CapitalIn, user: ControlUser, session: Session) -> dict[str, Any]:
    """Put capital in or take it out, immediately.

    Over WhatsApp this waits for a second partner, because a message is
    a claim about what someone else did. Here it does not: this page is
    behind an owner's own password, and asking that same person to
    approve their own action would be a signature they hand themselves.

    Posted the moment it is submitted -- same ledger, same journal, same
    balance chain as an approved request. The audit row records
    `channel='dashboard'`, which is how anyone reading the log later can
    tell the two apart.
    """
    result = await CapitalService(session).post_directly(
        user,
        entry_type=(
            CapitalEntryType.CONTRIBUTION if body.direction == "in" else CapitalEntryType.WITHDRAWAL
        ),
        partner_name=body.partner,
        amount=body.amount,
        via=body.via,
        on=body.on,
    )
    await session.commit()
    return {
        "partner": result.partner_name,
        "direction": body.direction,
        "amount": money_str(result.amount),
        "via": result.via,
        "balance": money_str(result.new_balance),
        "negative": result.negative_balance,
    }


# --- messages ----------------------------------------------------------


@router.get("/messages")
async def list_messages(
    user: ControlUser,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    failed_only: bool = False,
) -> dict[str, Any]:
    """Every message in and out, newest first.

    Exists because seventeen failed overnight and the only way to find
    out was to read the container's stdout.
    """
    rows = await message_log.recent(session, limit=limit, failed_only=failed_only)
    return {
        "items": [
            {
                "when": row.created_at.isoformat(),
                "direction": row.direction,
                "transport": row.transport,
                "peer": row.peer,
                "kind": row.kind,
                "preview": row.preview,
                "ok": row.ok,
                "error_code": row.error_code or "",
                "error_detail": row.error_detail or "",
                "meaning": message_log.MEANINGS.get(str(row.error_code), ""),
            }
            for row in rows
        ]
    }


@router.get("/messages/health")
async def message_health(
    user: ControlUser,
    session: Session,
    hours: Annotated[int, Query(ge=1, le=720)] = 24,
) -> dict[str, Any]:
    """Failures grouped by cause. Seventeen failures with one cause are
    one problem, and a list of seventeen rows hides that."""
    return await message_log.failure_summary(session, since_hours=hours)


# --- diagnostics and restore -------------------------------------------


@router.get("/diagnostics")
async def diagnostics(user: ControlUser, session: Session) -> dict[str, Any]:
    """Size, disk, whether the nightly jobs are still running, and
    whether the running balances still equal what they summarise."""
    return await DiagnosticsService(session).report(user.org_id)


@router.post("/ledger/rebuild")
async def rebuild_ledgers(user: ControlUser, session: Session) -> dict[str, Any]:
    """Rewrite every running balance from the rows themselves.

    Computes rather than destroys, like recost: the amounts are never
    touched, only the derived snapshot beside each one.
    """
    try:
        result = await DiagnosticsService(session).rebuild_ledgers(user.org_id, user)
    except GuardRegression as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "rolled back", "problems": exc.problems},
        ) from None
    await session.commit()
    return result


# There is deliberately no restore endpoint here. It was built, shipped,
# and taken back out the same afternoon, and the reason is worth keeping
# where the next person will look for it.
#
# `pg_restore --clean` replaces every table, so it needs an ACCESS
# EXCLUSIVE lock on each one. The API is holding the database open, so it
# cannot get them -- and a queued exclusive lock in Postgres makes every
# later reader queue *behind* it, including ones that would otherwise be
# perfectly compatible. Within seconds the connection pool was empty,
# every request was 500ing with nginx's HTML error page, and WhatsApp was
# down. The restore itself never ran: it sat blocked on its first
# statement until it died.
#
# It cannot be fixed by asking more firmly. A restore has to happen with
# the application stopped, which is not a thing a page inside that
# application can arrange for itself. So it lives at the terminal --
# `erp restore <name>`, with the stack down -- where stopping first is
# the natural order rather than a step to remember.
#
# Taking backups stays here. That one is safe, and it is the half people
# actually need often.
