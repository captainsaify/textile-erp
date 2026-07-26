"""Supplier / customer / partner lookups. Soft-delete filtered on every
path -- docs/02_Database.md §4."""

from __future__ import annotations

import decimal
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

    async def outstanding(self, org_id: uuid.UUID, customer_id: uuid.UUID) -> decimal.Decimal:
        """Receivable: unpaid portion of confirmed sales plus the
        opening balance -- docs/05_Sales.md §3."""
        from backend.models import SalesHeader

        unpaid = (
            await self._session.execute(
                select(
                    func.coalesce(
                        func.sum(SalesHeader.grand_total - SalesHeader.amount_paid),
                        decimal.Decimal("0"),
                    )
                ).where(
                    SalesHeader.org_id == org_id,
                    SalesHeader.customer_id == customer_id,
                    SalesHeader.deleted_at.is_(None),
                    SalesHeader.status.in_(["confirmed", "partially_returned", "returned"]),
                )
            )
        ).scalar_one()
        opening = (
            await self._session.execute(
                select(Customer.opening_balance).where(Customer.id == customer_id)
            )
        ).scalar_one_or_none() or decimal.Decimal("0")
        return decimal.Decimal(unpaid) + opening


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
