"""Turning what a person types into the row they meant.

Every resolver here follows one rule: **an ambiguous match is an error
that lists the candidates, never a guess.** Three separate incidents in
this system's short history came from silently picking one of several
plausible matches -- a sale filed under `Rais Bhai Lucknow` when
`Sohail Bhai Lucknow` was meant, twice more with other names, and a
purchase split across two brands both called `TOP` because a lookup
compared case but not whitespace. The CLI is the one place with a
terminal to ask on, so it asks.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.admin.harness import AdminError
from backend.models import (
    Brand,
    Customer,
    Product,
    PurchaseHeader,
    SalesHeader,
    Supplier,
)
from backend.models.enums import PurchaseStatus


def _normalise(name: str) -> str:
    return " ".join(name.split()).casefold()


async def purchase_by_invoice(
    session: AsyncSession, org_id: uuid.UUID, invoice_no: str, *, include_purged: bool = False
) -> PurchaseHeader:
    stmt: Select[tuple[PurchaseHeader]] = select(PurchaseHeader).where(
        PurchaseHeader.org_id == org_id,
        func.upper(PurchaseHeader.invoice_no) == invoice_no.strip().upper(),
    )
    stmt = (
        stmt.where(PurchaseHeader.purged_at.is_not(None))
        if include_purged
        else stmt.where(PurchaseHeader.deleted_at.is_(None))
    )
    rows = list((await session.execute(stmt.order_by(PurchaseHeader.invoice_date))).scalars())
    if not rows:
        where = "purged bills" if include_purged else "the books"
        raise AdminError(f"no bill {invoice_no!r} in {where}.")
    if len(rows) > 1:
        listed = ", ".join(f"{r.invoice_date} ({r.status.value})" for r in rows)
        raise AdminError(
            f"{len(rows)} bills are numbered {invoice_no!r}: {listed}. "
            "Merge them first: erp merge purchase <a> into <b>"
        )
    return rows[0]


async def sale_by_reference(
    session: AsyncSession, org_id: uuid.UUID, reference: str, *, include_purged: bool = False
) -> SalesHeader:
    """Sales have no invoice number a person types, so the reference is
    the first characters of the id -- which is what every message the
    system sends already shows."""
    token = reference.strip().lower()
    stmt: Select[tuple[SalesHeader]] = select(SalesHeader).where(
        SalesHeader.org_id == org_id,
        func.cast(SalesHeader.id, func.text().type).ilike(f"{token}%"),
    )
    stmt = (
        stmt.where(SalesHeader.purged_at.is_not(None))
        if include_purged
        else stmt.where(SalesHeader.deleted_at.is_(None))
    )
    rows = list((await session.execute(stmt.limit(6))).scalars())
    if not rows:
        raise AdminError(f"no sale starting {reference!r}.")
    if len(rows) > 1:
        listed = ", ".join(f"{str(r.id)[:8]} ({r.sale_date})" for r in rows)
        raise AdminError(f"{reference!r} matches {len(rows)} sales: {listed}. Use more characters.")
    return rows[0]


async def brand_by_name(session: AsyncSession, org_id: uuid.UUID, name: str) -> Brand:
    wanted = _normalise(name)
    rows = list(
        (
            await session.execute(
                select(Brand).where(Brand.org_id == org_id, Brand.deleted_at.is_(None))
            )
        ).scalars()
    )
    exact = [b for b in rows if _normalise(b.name) == wanted]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise AdminError(
            f"{len(exact)} brands are called {name!r} once whitespace and case are ignored. "
            "Merge them first: erp merge brand <a> into <b>"
        )
    near = [b for b in rows if wanted in _normalise(b.name)]
    if len(near) == 1:
        return near[0]
    if near:
        raise AdminError(f"{name!r} matches: {', '.join(sorted(b.name for b in near))}.")
    known = ", ".join(sorted(b.name for b in rows)) or "none defined"
    raise AdminError(f"no brand {name!r}. Known brands: {known}")


async def product_by_code(
    session: AsyncSession, org_id: uuid.UUID, code: str, brand: str | None
) -> Product:
    """Codes are unique *per brand*, not globally -- three products share
    `55X` on the live books. A code with no brand is therefore only
    resolvable when exactly one product carries it."""
    wanted = " ".join(code.split()).upper()
    stmt = select(Product).where(
        Product.org_id == org_id,
        func.upper(Product.code) == wanted,
        Product.deleted_at.is_(None),
    )
    rows = list((await session.execute(stmt)).scalars())
    if not rows:
        raise AdminError(f"no product with code {wanted!r}.")
    if brand is not None:
        target = await brand_by_name(session, org_id, brand)
        matched = [p for p in rows if p.brand_id == target.id]
        if not matched:
            carried = await _brand_names(session, org_id, [p.brand_id for p in rows])
            raise AdminError(
                f"{wanted} is not carried by {target.name}. It exists under: {carried}"
            )
        return matched[0]
    if len(rows) > 1:
        carried = await _brand_names(session, org_id, [p.brand_id for p in rows])
        raise AdminError(f"{len(rows)} brands carry {wanted}: {carried}. Add --brand.")
    return rows[0]


async def _brand_names(
    session: AsyncSession, org_id: uuid.UUID, brand_ids: list[uuid.UUID | None]
) -> str:
    ids = [b for b in brand_ids if b is not None]
    if not ids:
        return "no brand"
    names = list((await session.execute(select(Brand.name).where(Brand.id.in_(ids)))).scalars())
    return ", ".join(sorted(names))


async def supplier_by_name(session: AsyncSession, org_id: uuid.UUID, name: str) -> Supplier:
    return await _party_by_name(session, org_id, name, Supplier, "supplier")


async def customer_by_name(session: AsyncSession, org_id: uuid.UUID, name: str) -> Customer:
    return await _party_by_name(session, org_id, name, Customer, "customer")


async def _party_by_name[PartyT: (Supplier, Customer)](
    session: AsyncSession,
    org_id: uuid.UUID,
    name: str,
    model: type[PartyT],
    label: str,
) -> PartyT:
    # Constrained to the two concrete types rather than bound to a base:
    # mypy then checks the body once per party, so `.name` resolves on a
    # Supplier/Customer instead of falling back to Base and failing.
    wanted = _normalise(name)
    rows = list(
        (
            await session.execute(
                select(model).where(model.org_id == org_id, model.deleted_at.is_(None))
            )
        ).scalars()
    )
    exact = [p for p in rows if _normalise(p.name) == wanted]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise AdminError(
            f"{len(exact)} {label}s are called {name!r}. Merge them: erp merge {label} <a> into <b>"
        )
    near = [p for p in rows if wanted in _normalise(p.name) or _normalise(p.name) in wanted]
    if len(near) == 1:
        # Named, never assumed: this is where the misfiled sales came from.
        raise AdminError(
            f"no {label} exactly named {name!r}. Did you mean {near[0].name!r}? "
            "Type it exactly, or create it."
        )
    if near:
        raise AdminError(f"{name!r} matches: {', '.join(sorted(p.name for p in near))}.")
    raise AdminError(f"no {label} named {name!r}.")


async def search_parties(
    session: AsyncSession, org_id: uuid.UUID, query: str
) -> tuple[list[Supplier], list[Customer]]:
    like = f"%{query.strip()}%"
    suppliers = list(
        (
            await session.execute(
                select(Supplier).where(
                    Supplier.org_id == org_id,
                    Supplier.deleted_at.is_(None),
                    or_(Supplier.name.ilike(like), Supplier.phone.ilike(like)),
                )
            )
        ).scalars()
    )
    customers = list(
        (
            await session.execute(
                select(Customer).where(
                    Customer.org_id == org_id,
                    Customer.deleted_at.is_(None),
                    or_(Customer.name.ilike(like), Customer.phone.ilike(like)),
                )
            )
        ).scalars()
    )
    return suppliers, customers


def confirmed_only(header: PurchaseHeader) -> PurchaseHeader:
    if header.status is not PurchaseStatus.CONFIRMED:
        raise AdminError(
            f"bill {header.invoice_no} is {header.status.value}, not confirmed. "
            "Only confirmed bills carry stock and can be repaired."
        )
    return header
