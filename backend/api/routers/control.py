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
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.amounts import money_str
from backend.api.deps import ControlUser, Session
from backend.core.exceptions import DuplicateSaleError, ExactDuplicateInvoiceError
from backend.models import Product
from backend.models.enums import SalePaymentType
from backend.repositories.purchase_repository import PurchaseRepository
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
