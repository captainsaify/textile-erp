"""Supplier / customer / partner lookups. Soft-delete filtered on every
path -- docs/02_Database.md §4."""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Customer, Partner, Supplier

_SIMILARITY_THRESHOLD = 0.3


class SupplierRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search(self, org_id: uuid.UUID, query: str, limit: int = 5) -> list[Supplier]:
        score = func.similarity(Supplier.name, query)
        stmt = (
            select(Supplier)
            .where(
                Supplier.org_id == org_id,
                Supplier.deleted_at.is_(None),
                or_(Supplier.name.ilike(f"%{query}%"), score > _SIMILARITY_THRESHOLD),
            )
            .order_by(score.desc(), Supplier.name)
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())


class CustomerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search(self, org_id: uuid.UUID, query: str, limit: int = 5) -> list[Customer]:
        score = func.similarity(Customer.name, query)
        stmt = (
            select(Customer)
            .where(
                Customer.org_id == org_id,
                Customer.deleted_at.is_(None),
                or_(Customer.name.ilike(f"%{query}%"), score > _SIMILARITY_THRESHOLD),
            )
            .order_by(score.desc(), Customer.name)
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())


class PartnerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_display_name(self, org_id: uuid.UUID, name: str) -> Partner | None:
        stmt = select(Partner).where(
            Partner.org_id == org_id,
            Partner.deleted_at.is_(None),
            func.lower(Partner.display_name) == name.lower(),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
