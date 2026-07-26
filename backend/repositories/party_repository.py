"""Supplier / customer / partner lookups. Soft-delete filtered on every
path -- docs/02_Database.md §4."""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Customer, Partner, Supplier

_SIMILARITY_THRESHOLD = 0.3
ZERO = decimal.Decimal("0")


@dataclasses.dataclass(frozen=True)
class AgingBuckets:
    d0_30: decimal.Decimal
    d31_60: decimal.Decimal
    d61_90: decimal.Decimal
    d90_plus: decimal.Decimal


@dataclasses.dataclass(frozen=True)
class LastInvoice:
    reference: str
    date: datetime.date
    grand_total: decimal.Decimal


@dataclasses.dataclass(frozen=True)
class PartyStats:
    """Everything `supplier NAME`/`customer NAME` show beyond the name
    match itself -- docs/08_WhatsApp.md #supplier-name, #customer-name."""

    outstanding: decimal.Decimal
    aging: AgingBuckets
    last_invoice: LastInvoice | None
    this_month_count: int
    this_month_total: decimal.Decimal


@dataclasses.dataclass(frozen=True)
class StatementEntry:
    """One line of a `ledger supplier/customer NAME` statement -- signed
    by its effect on what's owed (invoice: +, payment: -), not by its
    effect on cash, so a running balance is a plain cumulative sum."""

    date: datetime.date
    created_at: datetime.datetime
    description: str
    amount: decimal.Decimal


async def _payment_entries(
    session: AsyncSession, org_id: uuid.UUID, source_type: str, party_id: uuid.UUID
) -> list[tuple[datetime.date, decimal.Decimal, datetime.datetime]]:
    """Payments can be posted via cash or bank -- docs/06_Accounting.md
    §9 -- so both partitioned ledgers are checked."""
    from backend.models import BankLedger, CashLedger

    rows: list[tuple[datetime.date, decimal.Decimal, datetime.datetime]] = []
    for model in (CashLedger, BankLedger):
        stmt = select(model.entry_date, model.amount, model.created_at).where(
            model.org_id == org_id,
            model.source_type == source_type,
            model.source_id == party_id,
        )
        rows.extend(tuple(row) for row in (await session.execute(stmt)).all())
    return rows


def _bucket_for_age(days: int) -> str:
    if days <= 30:
        return "d0_30"
    if days <= 60:
        return "d31_60"
    if days <= 90:
        return "d61_90"
    return "d90_plus"


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

    async def outstanding(self, org_id: uuid.UUID, supplier_id: uuid.UUID) -> decimal.Decimal:
        """Payable: unpaid portion of confirmed purchases plus the
        opening balance -- mirrors Customer.outstanding, payable side."""
        from backend.models import PurchaseHeader

        unpaid = (
            await self._session.execute(
                select(
                    func.coalesce(
                        func.sum(PurchaseHeader.grand_total - PurchaseHeader.amount_paid), ZERO
                    )
                ).where(
                    PurchaseHeader.org_id == org_id,
                    PurchaseHeader.supplier_id == supplier_id,
                    PurchaseHeader.deleted_at.is_(None),
                    PurchaseHeader.status == "confirmed",
                )
            )
        ).scalar_one()
        opening = (
            await self._session.execute(
                select(Supplier.opening_balance).where(Supplier.id == supplier_id)
            )
        ).scalar_one_or_none() or ZERO
        return decimal.Decimal(unpaid) + opening

    async def stats(
        self, org_id: uuid.UUID, supplier_id: uuid.UUID, today: datetime.date
    ) -> PartyStats:
        """Aging is computed per open invoice against `today` (the org's
        business-local date), not a lump outstanding total -- so "who do
        we owe, and since when" has real detail, per
        docs/06_Accounting.md §10. The opening balance predates any
        invoice_date this system tracks, so it can't be freshly aged; it
        is bucketed into 90+ as the conservative assumption -- an old,
        untracked balance is treated as old debt, not recent debt."""
        from backend.models import PurchaseHeader

        opening = (
            await self._session.execute(
                select(Supplier.opening_balance).where(Supplier.id == supplier_id)
            )
        ).scalar_one_or_none() or ZERO
        buckets = {"d0_30": ZERO, "d31_60": ZERO, "d61_90": ZERO, "d90_plus": opening}

        open_invoices = (
            await self._session.execute(
                select(
                    PurchaseHeader.invoice_date,
                    PurchaseHeader.grand_total,
                    PurchaseHeader.amount_paid,
                ).where(
                    PurchaseHeader.org_id == org_id,
                    PurchaseHeader.supplier_id == supplier_id,
                    PurchaseHeader.deleted_at.is_(None),
                    PurchaseHeader.status == "confirmed",
                    PurchaseHeader.grand_total > PurchaseHeader.amount_paid,
                )
            )
        ).all()
        for invoice_date, grand_total, amount_paid in open_invoices:
            age_days = (today - invoice_date).days
            buckets[_bucket_for_age(age_days)] += grand_total - amount_paid
        outstanding = sum(buckets.values(), ZERO)

        last_row = (
            await self._session.execute(
                select(
                    PurchaseHeader.invoice_no,
                    PurchaseHeader.invoice_date,
                    PurchaseHeader.grand_total,
                )
                .where(
                    PurchaseHeader.org_id == org_id,
                    PurchaseHeader.supplier_id == supplier_id,
                    PurchaseHeader.deleted_at.is_(None),
                    PurchaseHeader.status == "confirmed",
                )
                .order_by(PurchaseHeader.invoice_date.desc(), PurchaseHeader.created_at.desc())
                .limit(1)
            )
        ).first()
        last_invoice = (
            LastInvoice(reference=last_row[0], date=last_row[1], grand_total=last_row[2])
            if last_row
            else None
        )

        month_start = today.replace(day=1)
        month_count, month_total = (
            await self._session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(PurchaseHeader.grand_total), ZERO),
                ).where(
                    PurchaseHeader.org_id == org_id,
                    PurchaseHeader.supplier_id == supplier_id,
                    PurchaseHeader.deleted_at.is_(None),
                    PurchaseHeader.status == "confirmed",
                    PurchaseHeader.invoice_date >= month_start,
                    PurchaseHeader.invoice_date <= today,
                )
            )
        ).one()

        return PartyStats(
            outstanding=outstanding,
            aging=AgingBuckets(**buckets),
            last_invoice=last_invoice,
            this_month_count=int(month_count),
            this_month_total=decimal.Decimal(month_total),
        )

    async def statement(self, org_id: uuid.UUID, supplier_id: uuid.UUID) -> list[StatementEntry]:
        """Full history, oldest first -- the caller slices for display
        (docs/08_WhatsApp.md #ledger: paginated statement)."""
        from backend.models import PurchaseHeader

        entries: list[StatementEntry] = []
        invoices = (
            await self._session.execute(
                select(
                    PurchaseHeader.invoice_date,
                    PurchaseHeader.invoice_no,
                    PurchaseHeader.grand_total,
                    PurchaseHeader.created_at,
                ).where(
                    PurchaseHeader.org_id == org_id,
                    PurchaseHeader.supplier_id == supplier_id,
                    PurchaseHeader.deleted_at.is_(None),
                    PurchaseHeader.status == "confirmed",
                )
            )
        ).all()
        for invoice_date, invoice_no, grand_total, created_at in invoices:
            entries.append(
                StatementEntry(
                    date=invoice_date,
                    created_at=created_at,
                    description=f"purchase {invoice_no}",
                    amount=grand_total,
                )
            )
        # a payment's effect on cash (negative, per SettlementService) is
        # the same sign as its effect on what's owed -- reused directly.
        for entry_date, amount, created_at in await _payment_entries(
            self._session, org_id, "supplier_payment", supplier_id
        ):
            entries.append(
                StatementEntry(
                    date=entry_date, created_at=created_at, description="payment", amount=amount
                )
            )
        entries.sort(key=lambda e: (e.date, e.created_at))
        return entries


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

    async def stats(
        self, org_id: uuid.UUID, customer_id: uuid.UUID, today: datetime.date
    ) -> PartyStats:
        """Mirrors SupplierRepository.stats -- receivable side, same
        status set as `outstanding()` above."""
        from backend.models import SalesHeader

        statuses = ["confirmed", "partially_returned", "returned"]
        opening = (
            await self._session.execute(
                select(Customer.opening_balance).where(Customer.id == customer_id)
            )
        ).scalar_one_or_none() or ZERO
        buckets = {"d0_30": ZERO, "d31_60": ZERO, "d61_90": ZERO, "d90_plus": opening}

        open_invoices = (
            await self._session.execute(
                select(
                    SalesHeader.sale_date, SalesHeader.grand_total, SalesHeader.amount_paid
                ).where(
                    SalesHeader.org_id == org_id,
                    SalesHeader.customer_id == customer_id,
                    SalesHeader.deleted_at.is_(None),
                    SalesHeader.status.in_(statuses),
                    SalesHeader.grand_total > SalesHeader.amount_paid,
                )
            )
        ).all()
        for sale_date, grand_total, amount_paid in open_invoices:
            age_days = (today - sale_date).days
            buckets[_bucket_for_age(age_days)] += grand_total - amount_paid
        outstanding = sum(buckets.values(), ZERO)

        last_row = (
            await self._session.execute(
                select(SalesHeader.id, SalesHeader.sale_date, SalesHeader.grand_total)
                .where(
                    SalesHeader.org_id == org_id,
                    SalesHeader.customer_id == customer_id,
                    SalesHeader.deleted_at.is_(None),
                    SalesHeader.status.in_(statuses),
                )
                .order_by(SalesHeader.sale_date.desc(), SalesHeader.created_at.desc())
                .limit(1)
            )
        ).first()
        last_invoice = (
            LastInvoice(reference=str(last_row[0])[:8], date=last_row[1], grand_total=last_row[2])
            if last_row
            else None
        )

        month_start = today.replace(day=1)
        month_count, month_total = (
            await self._session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(SalesHeader.grand_total), ZERO),
                ).where(
                    SalesHeader.org_id == org_id,
                    SalesHeader.customer_id == customer_id,
                    SalesHeader.deleted_at.is_(None),
                    SalesHeader.status.in_(statuses),
                    SalesHeader.sale_date >= month_start,
                    SalesHeader.sale_date <= today,
                )
            )
        ).one()

        return PartyStats(
            outstanding=outstanding,
            aging=AgingBuckets(**buckets),
            last_invoice=last_invoice,
            this_month_count=int(month_count),
            this_month_total=decimal.Decimal(month_total),
        )

    async def statement(self, org_id: uuid.UUID, customer_id: uuid.UUID) -> list[StatementEntry]:
        from backend.models import SalesHeader

        entries: list[StatementEntry] = []
        sales = (
            await self._session.execute(
                select(
                    SalesHeader.sale_date,
                    SalesHeader.id,
                    SalesHeader.grand_total,
                    SalesHeader.created_at,
                ).where(
                    SalesHeader.org_id == org_id,
                    SalesHeader.customer_id == customer_id,
                    SalesHeader.deleted_at.is_(None),
                    SalesHeader.status.in_(["confirmed", "partially_returned", "returned"]),
                )
            )
        ).all()
        for sale_date, sale_id, grand_total, created_at in sales:
            entries.append(
                StatementEntry(
                    date=sale_date,
                    created_at=created_at,
                    description=f"sale {str(sale_id)[:8]}",
                    amount=grand_total,
                )
            )
        # a payment's effect on cash (positive, per SettlementService) is
        # the *opposite* sign of its effect on what's owed -- negated.
        for entry_date, amount, created_at in await _payment_entries(
            self._session, org_id, "customer_payment", customer_id
        ):
            entries.append(
                StatementEntry(
                    date=entry_date, created_at=created_at, description="payment", amount=-amount
                )
            )
        entries.sort(key=lambda e: (e.date, e.created_at))
        return entries


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

    async def list_active(self, org_id: uuid.UUID) -> list[Partner]:
        stmt = (
            select(Partner)
            .where(Partner.org_id == org_id, Partner.deleted_at.is_(None))
            .order_by(Partner.display_name)
        )
        return list((await self._session.execute(stmt)).scalars())
