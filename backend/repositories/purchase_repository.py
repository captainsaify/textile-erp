"""Purchase aggregate -- docs/01_Architecture.md §12 is the canonical
shape this follows."""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.models import PurchaseHeader


class PurchaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_confirmed_by_invoice(
        self, org_id: uuid.UUID, supplier_id: uuid.UUID, invoice_no: str
    ) -> PurchaseHeader | None:
        stmt = select(PurchaseHeader).where(
            PurchaseHeader.org_id == org_id,
            PurchaseHeader.supplier_id == supplier_id,
            func.lower(PurchaseHeader.invoice_no) == invoice_no.lower(),
            PurchaseHeader.deleted_at.is_(None),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

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
