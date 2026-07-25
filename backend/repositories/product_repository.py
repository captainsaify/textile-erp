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

    async def get_by_code(self, org_id: uuid.UUID, code: str) -> Product | None:
        stmt = (
            select(Product)
            .where(
                Product.org_id == org_id,
                func.upper(Product.code) == code.upper(),
                *self._active(),
            )
            .options(selectinload(Product.unit), selectinload(Product.brand))
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def count_active(self, org_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(Product.org_id == org_id, *self._active())
        return int((await self._session.execute(stmt)).scalar_one())

    async def search(self, org_id: uuid.UUID, query: str, limit: int = 5) -> list[Product]:
        """Fuzzy: trigram similarity on code/description plus substring
        match, best first."""
        pattern = f"%{query}%"
        score = func.greatest(
            func.similarity(Product.code, query),
            func.similarity(Product.description, query),
        )
        stmt = (
            select(Product)
            .where(
                Product.org_id == org_id,
                *self._active(),
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
