"""Dashboard/summary data -- docs/12_Dashboard.md. One computation
backs both WhatsApp surfaces (`dashboard`, `summary`), never two
separate implementations of "what is today's profit" (§1).

Redis caching (§4) is not wired in here: every read goes straight to
Postgres. That is the documented graceful-degradation path ("cache
unavailable -> falls back to computing directly, never fails
outright"), just taken unconditionally rather than only on a cache
miss. See HANDOFF.md -- wiring invalidation into every mutating service
correctly (purchase/sale confirm, payment, expense/income, capital,
inventory adjustment) is real, separate, cross-cutting work, and a
missed invalidation call is exactly the kind of silent staleness this
project tries hard to avoid elsewhere; better to ship correct-but-
uncached than fast-but-sometimes-wrong.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.repositories.accounting_repository import (
    LedgerRepository,
    PartnerCapitalRepository,
    business_today,
)
from backend.repositories.inventory_repository import InventoryRepository, StockTotals
from backend.repositories.party_repository import PartnerRepository
from backend.repositories.product_repository import ProductRepository
from backend.repositories.report_repository import ReportRepository, SlowMover, TopSeller
from backend.services.profit_service import ProfitReport, ProfitService

ZERO = decimal.Decimal("0")


@dataclasses.dataclass(frozen=True)
class PartnerBalance:
    display_name: str
    balance: decimal.Decimal


@dataclasses.dataclass(frozen=True)
class DashboardData:
    today: datetime.date
    cash_balance: decimal.Decimal
    bank_balance: decimal.Decimal
    stock: StockTotals
    active_products: int
    today_sales: decimal.Decimal
    today_purchases: decimal.Decimal
    month_profit: ProfitReport
    receivables_total: decimal.Decimal
    receivables_count: int
    payables_total: decimal.Decimal
    payables_count: int
    top_sellers: list[TopSeller]
    slow_movers: list[SlowMover]
    # None (not an empty list) when the caller's role can't see this --
    # "simply absent," not "hidden," per docs/12_Dashboard.md §6.
    partner_balances: list[PartnerBalance] | None


class DashboardService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._ledgers = LedgerRepository(session)
        self._inventory = InventoryRepository(session)
        self._products = ProductRepository(session)
        self._reports = ReportRepository(session)
        self._profit = ProfitService(session)
        self._capital = PartnerCapitalRepository(session)
        self._partners = PartnerRepository(session)

    async def summary(self, org_id: uuid.UUID, *, include_partner_capital: bool) -> DashboardData:
        today = await business_today(self._session, org_id)
        month_start = today.replace(day=1)

        cash = await self._ledgers.balance(org_id, "cash")
        bank = await self._ledgers.balance(org_id, "bank")
        stock = await self._inventory.totals(org_id)
        active_products = await self._products.count_active(org_id)
        today_sales = await self._reports.sales_total(org_id, today, today)
        today_purchases = await self._reports.purchases_total(org_id, today, today)
        month_profit = await self._profit.calculate(org_id, month_start, today)
        receivables_total, receivables_count = await self._reports.receivables_total(org_id)
        payables_total, payables_count = await self._reports.payables_total(org_id)
        top_sellers = await self._reports.top_sellers(org_id, month_start, today)
        slow_days = await self._reports.slow_moving_days(org_id)
        slow_movers = await self._reports.slow_movers(
            org_id, slow_moving_days=slow_days, today=today
        )

        partner_balances: list[PartnerBalance] | None = None
        if include_partner_capital:
            partners = await self._partners.list_active(org_id)
            partner_balances = [
                PartnerBalance(
                    display_name=partner.display_name,
                    balance=await self._capital.balance(org_id, partner.id),
                )
                for partner in partners
            ]

        return DashboardData(
            today=today,
            cash_balance=cash,
            bank_balance=bank,
            stock=stock,
            active_products=active_products,
            today_sales=today_sales,
            today_purchases=today_purchases,
            month_profit=month_profit,
            receivables_total=receivables_total,
            receivables_count=receivables_count,
            payables_total=payables_total,
            payables_count=payables_count,
            top_sellers=top_sellers,
            slow_movers=slow_movers,
            partner_balances=partner_balances,
        )
