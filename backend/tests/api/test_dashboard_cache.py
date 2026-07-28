"""Dashboard caching -- docs/12_Dashboard.md §4.

Caching a figure is only safe if what comes back is what went in, and if
a write makes the old answer unreachable. Those are the two tests here;
everything else about caching is a performance detail.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import User
from backend.repositories.inventory_repository import StockTotals
from backend.repositories.report_repository import SlowMover, TopSeller
from backend.services import dashboard_cache
from backend.services.dashboard_service import DashboardData, PartnerBalance
from backend.services.profit_service import ProfitReport
from backend.tests.conftest import SEEDED_ORG_ID, purge_business_rows

D = decimal.Decimal
ORG = uuid.UUID(SEEDED_ORG_ID)


@pytest.fixture(autouse=True)
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    await purge_business_rows(session_factory)


def _snapshot() -> DashboardData:
    """Every field populated, including the awkward ones: nested
    dataclasses, lists of them, and a role-gated None."""
    return DashboardData(
        today=datetime.date(2026, 7, 28),
        cash_balance=D("40920.55"),
        bank_balance=D("-1.00"),
        stock=StockTotals(
            total_value=D("4092000.00"),
            total_qty=D("27280.000"),
            low_count=2,
            negative_count=0,
        ),
        active_products=26,
        today_sales=D("0.00"),
        today_purchases=D("4092000.00"),
        month_profit=ProfitReport(
            start=datetime.date(2026, 7, 1),
            end=datetime.date(2026, 7, 28),
            revenue=D("100.10"),
            cogs=D("50.05"),
            gross_profit=D("50.05"),
            operating_expenses=D("10.01"),
            other_income=D("1.00"),
            damage_loss=D("0.00"),
            net_profit=D("41.04"),
        ),
        receivables_total=D("123456.78"),
        receivables_count=3,
        payables_total=D("4091999.00"),
        payables_count=1,
        top_sellers=[TopSeller(code="TRP", description="Jogging Pant", revenue=D("7380.00"))],
        # None here is "never sold", a distinct fact from 0 days
        slow_movers=[SlowMover(code="IL", description="Interlock Bottom", days_since_sale=None)],
        partner_balances=[PartnerBalance(display_name="Rahul", balance=D("250000.00"))],
    )


def test_the_round_trip_is_exact() -> None:
    """A cache that returns a float where a Decimal went in would break
    the project's one absolute rule about money, quietly and everywhere
    at once."""
    original = _snapshot()

    restored = dashboard_cache.decode(DashboardData, dashboard_cache.encode(original))

    assert restored == original
    assert isinstance(restored.cash_balance, decimal.Decimal)
    assert isinstance(restored.month_profit.net_profit, decimal.Decimal)
    assert isinstance(restored.top_sellers[0].revenue, decimal.Decimal)
    assert restored.slow_movers[0].days_since_sale is None
    assert isinstance(restored.today, datetime.date)
    # exact, not merely equal: 40920.55 must not come back as 40920.550001
    assert str(restored.cash_balance) == "40920.55"


def test_a_role_gated_none_survives_as_none() -> None:
    """None means "you may not see this", which is different from an
    empty list meaning "there are none" (docs/12 §6)."""
    original = dataclasses_replace_partner_balances(_snapshot(), None)

    restored = dashboard_cache.decode(DashboardData, dashboard_cache.encode(original))

    assert restored.partner_balances is None


def dataclasses_replace_partner_balances(
    data: DashboardData, value: list[PartnerBalance] | None
) -> DashboardData:
    import dataclasses

    return dataclasses.replace(data, partner_balances=value)


async def test_a_cached_dashboard_is_returned_without_recomputing(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User, redis_client: object
) -> None:
    from backend.services.dashboard_service import DashboardService

    await dashboard_cache.invalidate(ORG)
    async with session_factory() as session:
        service = DashboardService(session)
        first = await service.summary(ORG, include_partner_capital=False)

        calls = 0
        original = service._compute

        async def counting(*args: object, **kwargs: object) -> DashboardData:
            nonlocal calls
            calls += 1
            return await original(*args, **kwargs)  # type: ignore[arg-type]

        service._compute = counting  # type: ignore[method-assign]
        second = await service.summary(ORG, include_partner_capital=False)

    assert second == first
    assert calls == 0, "the second read should have been served from cache"


async def test_any_audited_write_makes_the_cached_answer_unreachable(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User, redis_client: object
) -> None:
    """Invalidation hangs off audit_logs rather than a list of mutating
    services, because every business write must write an audit row
    (CLAUDE.md rule 3) and a per-service list is something you can
    forget to add to."""
    from backend.services.audit_service import AuditService

    before = await dashboard_cache.current_version(ORG)

    async with session_factory() as session, session.begin():
        await AuditService(session).record(
            ORG,
            staff_user.id,
            action="purchase.confirmed",
            entity_type="purchase_headers",
            entity_id=uuid.uuid4(),
        )

    assert await dashboard_cache.current_version(ORG) > before

    # and the payload stored under the old version is no longer reachable
    hit, _ = await dashboard_cache.load(ORG, DashboardData, variant="plain")
    assert hit is None


async def test_a_slow_read_cannot_overwrite_a_fresher_value(redis_client: object) -> None:
    """The read-modify-write race: a read that began before a write must
    not store its stale result afterwards."""
    await dashboard_cache.invalidate(ORG)
    stale = _snapshot()
    version_at_read_start = await dashboard_cache.current_version(ORG)

    # a write lands while the slow read is still computing
    await dashboard_cache.invalidate(ORG)

    await dashboard_cache.store(ORG, stale, variant="plain", version=version_at_read_start)

    hit, _ = await dashboard_cache.load(ORG, DashboardData, variant="plain")
    assert hit is None, "the stale value landed on a key nobody reads"


async def test_a_cache_failure_degrades_to_computing_directly(
    session_factory: async_sessionmaker[AsyncSession],
    staff_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/12 §4: cache unavailable falls back to computing directly and
    never fails outright."""
    from backend.services.dashboard_service import DashboardService

    def exploding() -> object:
        raise RuntimeError("redis is down")

    monkeypatch.setattr("backend.services.dashboard_cache.get_redis", exploding)

    async with session_factory() as session:
        data = await DashboardService(session).summary(ORG, include_partner_capital=False)

    assert data.active_products >= 0
