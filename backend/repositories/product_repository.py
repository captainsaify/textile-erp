"""Product lookups: exact code, fuzzy search -- docs/02_Database.md §7
provides the pg_trgm indexes these queries lean on."""

from __future__ import annotations

import uuid

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.models import Product

_SIMILARITY_THRESHOLD = 0.3


class ProductRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _active(self) -> list[ColumnElement[bool]]:
        return [Product.deleted_at.is_(None), Product.is_active.is_(True)]

    async def list_by_code(self, org_id: uuid.UUID, code: str) -> list[Product]:
        """Every active product carrying this code. Codes are unique only
        within a brand, so a bare code can legitimately return several."""
        stmt = (
            select(Product)
            .where(
                Product.org_id == org_id,
                func.upper(Product.code) == code.upper(),
                *self._active(),
            )
            .options(selectinload(Product.unit), selectinload(Product.brand))
            .order_by(Product.created_at)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def get_by_code(
        self, org_id: uuid.UUID, code: str, brand_id: uuid.UUID | None = None
    ) -> Product | None:
        """The one product meant by this code.

        With a brand, that brand's product wins, falling back to a
        brandless product of the same code (the catalog predates the
        brand being recorded). Without one, an unambiguous code resolves
        and an ambiguous one returns None -- the caller asks which brand
        rather than silently picking. Returning None here is why this
        can't stay a scalar_one_or_none: two brands sharing a code used
        to raise MultipleResultsFound.
        """
        matches = await self.list_by_code(org_id, code)
        if not matches:
            return None
        if brand_id is not None:
            for product in matches:
                if product.brand_id == brand_id:
                    return product
            unbranded = [p for p in matches if p.brand_id is None]
            if len(unbranded) == 1:
                return unbranded[0]
            return None
        if len(matches) == 1:
            return matches[0]
        return None

    async def count_active(self, org_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(Product.org_id == org_id, *self._active())
        return int((await self._session.execute(stmt)).scalar_one())

    async def search(
        self,
        org_id: uuid.UUID,
        query: str,
        limit: int = 5,
        brand_id: uuid.UUID | None = None,
    ) -> list[Product]:
        """Fuzzy: trigram similarity on code/description plus substring
        match, best first. A brand narrows the field -- fuzzy-matching a
        code onto another brand's product is a wrong answer, not a near
        miss, now that brands can share codes."""
        pattern = f"%{query}%"
        score = func.greatest(
            func.similarity(Product.code, query),
            func.similarity(Product.description, query),
        )
        brand_filter: list[ColumnElement[bool]] = []
        if brand_id is not None:
            brand_filter.append(or_(Product.brand_id == brand_id, Product.brand_id.is_(None)))
        stmt = (
            select(Product)
            .where(
                Product.org_id == org_id,
                *self._active(),
                *brand_filter,
                or_(
                    Product.code.ilike(pattern),
                    Product.description.ilike(pattern),
                    score > _SIMILARITY_THRESHOLD,
                ),
            )
            .options(selectinload(Product.unit))
            .order_by(score.desc(), Product.code)
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())
