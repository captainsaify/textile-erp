"""Purchase aggregate -- docs/01_Architecture.md §12 is the canonical
shape this follows."""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.models import PurchaseHeader
from backend.models.enums import PurchaseStatus


@dataclasses.dataclass(frozen=True)
class InvoiceLine:
    """One line of a bill, as much of it as a picker needs to show."""

    code: str
    description: str
    qty: decimal.Decimal
    #: None when the sheet gave no per-bale weight -- which is exactly
    #: when `receive` cannot work in bales, so the picker can say so
    #: rather than offering a line that will be refused.
    weight_per_piece: decimal.Decimal | None
    rate: decimal.Decimal

    @property
    def pieces(self) -> decimal.Decimal | None:
        if self.weight_per_piece is None or self.weight_per_piece <= 0:
            return None
        return (self.qty / self.weight_per_piece).quantize(decimal.Decimal("0.001"))


class PurchaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_confirmed_by_invoice(
        self, org_id: uuid.UUID, supplier_id: uuid.UUID, invoice_no: str
    ) -> PurchaseHeader | None:
        """The *confirmed* bill on this invoice number, if there is one.

        The status filter is the point of the method and was missing.
        Without it a cancelled bill went on claiming its invoice number
        for ever, and since an exact duplicate cannot be overridden, the
        correction this system actually recommends -- undo it and enter
        it again -- was impossible on any bill that had been entered once.
        Re-entering invoice 1051 failed for exactly this reason.

        `.limit(1)` because a number can legitimately be cancelled
        several times before it goes in correctly; `scalar_one_or_none`
        raised MultipleResultsFound on the second attempt.
        """
        stmt = (
            select(PurchaseHeader)
            .where(
                PurchaseHeader.org_id == org_id,
                PurchaseHeader.supplier_id == supplier_id,
                func.lower(PurchaseHeader.invoice_no) == invoice_no.lower(),
                PurchaseHeader.status == PurchaseStatus.CONFIRMED,
                PurchaseHeader.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def recent_invoices(
        self, org_id: uuid.UUID, *, limit: int = 9
    ) -> list[tuple[str, str, datetime.date]]:
        """Most recent confirmed bills, for picking one instead of
        remembering its number. (invoice_no, supplier name, date)."""
        from backend.models import Supplier

        stmt = (
            select(PurchaseHeader.invoice_no, Supplier.name, PurchaseHeader.invoice_date)
            .join(Supplier, Supplier.id == PurchaseHeader.supplier_id)
            .where(
                PurchaseHeader.org_id == org_id,
                PurchaseHeader.deleted_at.is_(None),
                PurchaseHeader.status == "confirmed",
            )
            .order_by(PurchaseHeader.invoice_date.desc(), PurchaseHeader.created_at.desc())
            .limit(limit)
        )
        return [(row[0], row[1], row[2]) for row in (await self._session.execute(stmt)).all()]

    async def invoice_lines(
        self, org_id: uuid.UUID, invoice_no: str, *, limit: int = 9
    ) -> list[InvoiceLine]:
        """What is actually on a bill, so a correction can pick a line
        instead of recalling a code.

        Bales rather than kilograms: `receive` counts what came off the
        truck, and quoting the line in the same unit the question is
        asked in is what stops 10 bales being answered with 800.
        """
        from backend.models import Product, PurchaseLine

        stmt = (
            select(
                Product.code,
                PurchaseLine.description,
                Product.description,
                PurchaseLine.qty,
                PurchaseLine.weight_kg,
                PurchaseLine.rate,
            )
            .join(PurchaseHeader, PurchaseHeader.id == PurchaseLine.purchase_header_id)
            .join(Product, Product.id == PurchaseLine.product_id)
            .where(
                PurchaseHeader.org_id == org_id,
                func.lower(PurchaseHeader.invoice_no) == invoice_no.lower(),
                PurchaseHeader.deleted_at.is_(None),
                PurchaseHeader.status == "confirmed",
            )
            .order_by(PurchaseLine.line_no)
            .limit(limit)
        )
        return [
            InvoiceLine(
                code=row[0],
                description=(row[1] or row[2] or ""),
                qty=row[3],
                weight_per_piece=row[4],
                rate=row[5],
            )
            for row in (await self._session.execute(stmt)).all()
        ]

    async def find_potential_duplicates(
        self,
        org_id: uuid.UUID,
        supplier_id: uuid.UUID,
        invoice_date: datetime.date,
        window_days: int = 3,
    ) -> list[PurchaseHeader]:
        """Candidate fetch only -- the fuzzy judgment happens in the
        service (docs/17_CodingStandards.md §3)."""
        stmt = (
            select(PurchaseHeader)
            .where(
                PurchaseHeader.org_id == org_id,
                PurchaseHeader.supplier_id == supplier_id,
                PurchaseHeader.deleted_at.is_(None),
                PurchaseHeader.invoice_date.between(
                    invoice_date - datetime.timedelta(days=window_days),
                    invoice_date + datetime.timedelta(days=window_days),
                ),
            )
            .options(selectinload(PurchaseHeader.lines))
        )
        return list((await self._session.execute(stmt)).scalars())


class SalesLookupRepository:
    """Recent sales, for picking one rather than remembering a uuid.

    A sale has no invoice number of its own -- the partners don't write
    one -- so it is identified by the short id every other surface
    already quotes, shown beside the date, customer and amount that make
    it recognisable.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def recent(
        self, org_id: uuid.UUID, *, limit: int = 9
    ) -> list[tuple[str, str, datetime.date, decimal.Decimal]]:
        from backend.models import Customer, SalesHeader

        stmt = (
            select(
                SalesHeader.id,
                Customer.name,
                SalesHeader.sale_date,
                SalesHeader.grand_total,
            )
            .join(Customer, Customer.id == SalesHeader.customer_id)
            .where(
                SalesHeader.org_id == org_id,
                SalesHeader.deleted_at.is_(None),
                SalesHeader.status == "confirmed",
            )
            .order_by(SalesHeader.sale_date.desc(), SalesHeader.created_at.desc())
            .limit(limit)
        )
        return [
            (str(row[0])[:8], row[1], row[2], row[3])
            for row in (await self._session.execute(stmt)).all()
        ]
