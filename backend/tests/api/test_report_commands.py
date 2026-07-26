"""`dashboard`, `summary`, `profit`, `supplier NAME`, `customer NAME`,
`ledger` -- docs/08_WhatsApp.md #dashboard, #summary, #profit,
#supplier-name, #customer-name, #ledger.

Journal/ledger rows are built through JournalService/LedgerRepository
(the real posting path) rather than hand-crafted, so the balance
invariant is enforced the same way it is in production; purchase/sale
headers are inserted directly since only their read side is under test
here -- the write side has its own suite in test_purchase_flow.py /
test_sale_flow.py.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import RequestContext
from backend.api.commands.report_commands import (
    handle_customer,
    handle_dashboard,
    handle_ledger,
    handle_profit,
    handle_summary,
    handle_supplier,
)
from backend.api.period import Period, parse_period
from backend.core.exceptions import ValidationError
from backend.models import (
    Brand,
    Customer,
    Product,
    PurchaseHeader,
    SalesHeader,
    Supplier,
    User,
)
from backend.models.enums import AccountCode, LedgerEntryType
from backend.repositories.accounting_repository import LedgerRepository, business_today
from backend.repositories.report_repository import ReportRepository
from backend.services.inventory_service import InventoryService
from backend.services.journal_service import JournalService
from backend.services.profit_service import ProfitService
from backend.tests.conftest import (
    SEEDED_KG_UNIT_ID,
    SEEDED_MAIN_WAREHOUSE_ID,
    SEEDED_ORG_ID,
    SEEDED_TEXTILE_TYPE_ID,
    purge_business_rows,
)

D = decimal.Decimal
ORG = uuid.UUID(SEEDED_ORG_ID)
WAREHOUSE = uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID)


@pytest.fixture(autouse=True)
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    await purge_business_rows(session_factory)


@pytest.fixture
def ctx(staff_user: User, session_factory: async_sessionmaker[AsyncSession]) -> RequestContext:
    return RequestContext(user=staff_user, session_factory=session_factory)


@pytest.fixture
def owner_ctx(
    owner_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> RequestContext:
    return RequestContext(user=owner_user, session_factory=session_factory)


# --------------------------------------------------------------------
# parse_period -- pure function, no DB
# --------------------------------------------------------------------


def test_parse_period_today_and_defaults() -> None:
    today = datetime.date(2026, 7, 27)
    assert parse_period("", today) == Period(today, today, "today")
    assert parse_period("today", today) == Period(today, today, "today")
    assert parse_period("TODAY", today) == Period(today, today, "today")


def test_parse_period_week_is_week_to_date_not_trailing_seven_days() -> None:
    monday = datetime.date(2026, 7, 27)  # a Monday
    period = parse_period("week", monday)
    assert period.start == monday  # week-to-date on a Monday is just today

    wednesday = datetime.date(2026, 7, 29)
    period = parse_period("week", wednesday)
    assert period.start == monday
    assert period.end == wednesday


def test_parse_period_month_and_year_are_to_date() -> None:
    today = datetime.date(2026, 7, 27)
    month = parse_period("month", today)
    assert month.start == datetime.date(2026, 7, 1)
    assert month.end == today

    year = parse_period("year", today)
    assert year.start == datetime.date(2026, 1, 1)
    assert year.end == today


def test_parse_period_explicit_range() -> None:
    today = datetime.date(2026, 7, 27)
    period = parse_period("01-07-2026 to 25-07-2026", today)
    assert period.start == datetime.date(2026, 7, 1)
    assert period.end == datetime.date(2026, 7, 25)


def test_parse_period_rejects_backwards_range() -> None:
    today = datetime.date(2026, 7, 27)
    with pytest.raises(ValidationError, match="after its end date"):
        parse_period("25-07-2026 to 01-07-2026", today)


def test_parse_period_rejects_garbage() -> None:
    today = datetime.date(2026, 7, 27)
    with pytest.raises(ValidationError):
        parse_period("whenever", today)


# --------------------------------------------------------------------
# ProfitService -- journal rollup is the source of truth (§5)
# --------------------------------------------------------------------


async def test_profit_service_matches_manually_summed_transactions(
    staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Accounting-parity check (docs/15_Testing.md): the journal-rollup
    P&L must equal a hand-computed figure from the same postings, for a
    scenario simple enough to sum by hand -- one sale, one expense, one
    income entry, no returns."""
    async with session_factory() as session:
        journal = JournalService(session)
        async with session.begin():
            today = await business_today(session, ORG)
            # sale: revenue 1000, cogs 600
            await journal.post(
                ORG,
                entry_date=today,
                description="test sale",
                source_type="test",
                source_id=uuid.uuid4(),
                created_by=staff_user.id,
                debits=[(AccountCode.CASH, D("1000")), (AccountCode.COGS, D("600"))],
                credits=[(AccountCode.SALES_REVENUE, D("1000")), (AccountCode.INVENTORY, D("600"))],
            )
            # expense: operating 150
            await journal.post(
                ORG,
                entry_date=today,
                description="test expense",
                source_type="test",
                source_id=uuid.uuid4(),
                created_by=staff_user.id,
                debits=[(AccountCode.OPERATING_EXPENSES, D("150"))],
                credits=[(AccountCode.CASH, D("150"))],
            )
            # income: other income 50
            await journal.post(
                ORG,
                entry_date=today,
                description="test income",
                source_type="test",
                source_id=uuid.uuid4(),
                created_by=staff_user.id,
                debits=[(AccountCode.CASH, D("50"))],
                credits=[(AccountCode.OTHER_INCOME, D("50"))],
            )

        report = await ProfitService(session).calculate(ORG, today, today)

    expected_revenue = D("1000")
    expected_cogs = D("600")
    expected_gross = expected_revenue - expected_cogs
    expected_opex = D("150")
    expected_other_income = D("50")
    expected_net = expected_gross - expected_opex + expected_other_income

    assert report.revenue == expected_revenue
    assert report.cogs == expected_cogs
    assert report.gross_profit == expected_gross
    assert report.operating_expenses == expected_opex
    assert report.other_income == expected_other_income
    assert report.net_profit == expected_net


async def test_profit_service_ignores_entries_outside_the_period(
    staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        journal = JournalService(session)
        async with session.begin():
            today = await business_today(session, ORG)
            long_ago = today - datetime.timedelta(days=400)
            await journal.post(
                ORG,
                entry_date=long_ago,
                description="old sale",
                source_type="test",
                source_id=uuid.uuid4(),
                created_by=staff_user.id,
                debits=[(AccountCode.CASH, D("5000"))],
                credits=[(AccountCode.SALES_REVENUE, D("5000"))],
            )
        report = await ProfitService(session).calculate(ORG, today, today)
    assert report.revenue == D("0")


# --------------------------------------------------------------------
# fixtures: a supplier/customer scenario with aging + a purchase/sale
# --------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Scenario:
    product: Product
    supplier: Supplier
    customer: Customer
    today: datetime.date


@pytest.fixture
async def scenario(
    staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[Scenario]:
    """One supplier with an old (90+d) unpaid invoice and a recent
    (0-30d) one; one customer with a recent unpaid sale; one product
    with a purchase movement so `ledger CODE` has something to show."""
    async with session_factory() as session:
        async with session.begin():
            today = await business_today(session, ORG)
            brand = Brand(org_id=ORG, name=f"Brand-{uuid.uuid4().hex[:6]}")
            session.add(brand)
            await session.flush()

            product = Product(
                org_id=ORG,
                product_type_id=uuid.UUID(SEEDED_TEXTILE_TYPE_ID),
                code=f"RPT{uuid.uuid4().hex[:4].upper()}",
                description="Report Test Fabric",
                brand_id=brand.id,
                unit_id=uuid.UUID(SEEDED_KG_UNIT_ID),
                created_by=staff_user.id,
            )
            session.add(product)
            await session.flush()

            supplier = Supplier(
                org_id=ORG, name=f"Report Supplier {uuid.uuid4().hex[:6]}", created_by=staff_user.id
            )
            customer = Customer(
                org_id=ORG, name=f"Report Customer {uuid.uuid4().hex[:6]}", created_by=staff_user.id
            )
            session.add_all([supplier, customer])
            await session.flush()

            old_invoice = PurchaseHeader(
                org_id=ORG,
                supplier_id=supplier.id,
                warehouse_id=WAREHOUSE,
                invoice_no="OLD-1",
                invoice_date=today - datetime.timedelta(days=95),
                grand_total=D("1000.00"),
                amount_paid=D("0.00"),
                status="confirmed",
                created_by=staff_user.id,
            )
            recent_invoice = PurchaseHeader(
                org_id=ORG,
                supplier_id=supplier.id,
                warehouse_id=WAREHOUSE,
                invoice_no="NEW-1",
                invoice_date=today - datetime.timedelta(days=5),
                grand_total=D("2000.00"),
                amount_paid=D("500.00"),
                status="confirmed",
                created_by=staff_user.id,
            )
            session.add_all([old_invoice, recent_invoice])

            sale = SalesHeader(
                org_id=ORG,
                customer_id=customer.id,
                warehouse_id=WAREHOUSE,
                sale_date=today - datetime.timedelta(days=3),
                payment_type="credit",
                grand_total=D("800.00"),
                amount_paid=D("200.00"),
                status="confirmed",
                created_by=staff_user.id,
            )
            session.add(sale)
            await session.flush()

            # a payment recorded against the supplier, via the real
            # ledger-append path so its sign convention is authentic
            ledgers = LedgerRepository(session)
            await ledgers.append(
                ORG,
                "cash",
                entry_type=LedgerEntryType.PURCHASE_PAYMENT,
                amount=D("-500.00"),
                source_type="supplier_payment",
                source_id=supplier.id,
                entry_date=today - datetime.timedelta(days=5),
                notes="test payment",
                created_by=staff_user.id,
            )
            # matching payment against the customer's partial payment
            # above, so the ledger statement and the outstanding total
            # (grand_total - amount_paid) agree with each other
            await ledgers.append(
                ORG,
                "cash",
                entry_type=LedgerEntryType.SALE_RECEIPT,
                amount=D("200.00"),
                source_type="customer_payment",
                source_id=customer.id,
                entry_date=today - datetime.timedelta(days=3),
                notes="test receipt",
                created_by=staff_user.id,
            )

            await InventoryService(session).record_purchase_movement(
                ORG,
                product_id=product.id,
                warehouse_id=WAREHOUSE,
                qty=D("50"),
                landed_cost_per_unit=D("100"),
                source_id=old_invoice.id,
                created_by=staff_user.id,
            )

        yield Scenario(product=product, supplier=supplier, customer=customer, today=today)


# --------------------------------------------------------------------
# supplier / customer aging
# --------------------------------------------------------------------


async def test_supplier_command_buckets_by_invoice_age(
    scenario: Scenario, ctx: RequestContext
) -> None:
    result = await handle_supplier(scenario.supplier.name, ctx)
    # old invoice (1000, 95 days) -> 90+; recent invoice (2000-500=1500, 5 days) -> 0-30
    assert "90+d: ₹1,000.00" in result.reply
    assert "0–30d: ₹1,500.00" in result.reply
    assert "Outstanding payable: ₹2,500.00" in result.reply
    assert "NEW-1" in result.reply  # most recent invoice shown as "last purchase"


async def test_customer_command_reports_outstanding(
    scenario: Scenario, ctx: RequestContext
) -> None:
    result = await handle_customer(scenario.customer.name, ctx)
    assert "Outstanding receivable: ₹600.00" in result.reply  # 800 - 200
    assert "0–30d: ₹600.00" in result.reply


async def test_supplier_command_no_match(ctx: RequestContext) -> None:
    result = await handle_supplier("Nobody Textiles Ever", ctx)
    assert "No supplier matching" in result.reply


# --------------------------------------------------------------------
# ledger: party statement running balance + product movement history
# --------------------------------------------------------------------


async def test_ledger_supplier_statement_running_balance(
    scenario: Scenario, ctx: RequestContext
) -> None:
    result = await handle_ledger(f"supplier {scenario.supplier.name}", ctx)
    assert "OLD-1" in result.reply
    assert "NEW-1" in result.reply
    assert "payment" in result.reply
    assert "Current balance: ₹2,500.00" in result.reply


async def test_ledger_customer_statement_running_balance(
    scenario: Scenario, ctx: RequestContext
) -> None:
    result = await handle_ledger(f"customer {scenario.customer.name}", ctx)
    assert "Current balance: ₹600.00" in result.reply


async def test_ledger_product_shows_movement_history(
    scenario: Scenario, ctx: RequestContext
) -> None:
    result = await handle_ledger(scenario.product.code, ctx)
    assert "purchase" in result.reply
    assert "50.0" in result.reply


async def test_ledger_usage_message_on_empty_args(ctx: RequestContext) -> None:
    result = await handle_ledger("", ctx)
    assert "Usage" in result.reply


# --------------------------------------------------------------------
# dashboard / summary -- RBAC and headline numbers
# --------------------------------------------------------------------


async def test_dashboard_hides_partner_capital_for_staff(
    scenario: Scenario, ctx: RequestContext
) -> None:
    result = await handle_dashboard("", ctx)
    assert "Partner capital" not in result.reply


async def test_dashboard_shows_partner_capital_for_owner(
    scenario: Scenario, owner_ctx: RequestContext
) -> None:
    result = await handle_dashboard("", owner_ctx)
    # no partners seeded in this scenario -- the section header still
    # renders (empty list is not None), just with nothing after the dash
    assert "Partner capital" in result.reply


async def test_dashboard_reports_receivables_and_payables(
    scenario: Scenario, ctx: RequestContext
) -> None:
    result = await handle_dashboard("", ctx)
    assert "₹2,500.00 (1 suppliers)" in result.reply
    assert "₹600.00 (1 customers)" in result.reply


async def test_summary_reports_period_totals(scenario: Scenario, ctx: RequestContext) -> None:
    result = await handle_summary("month", ctx)
    assert "📋 Summary — " in result.reply
    assert "Sales:" in result.reply
    assert "Purchases:" in result.reply


async def test_summary_rejects_bad_period(ctx: RequestContext) -> None:
    result = await handle_summary("whenever", ctx)
    assert "Say 'today'" in result.reply


# --------------------------------------------------------------------
# profit -- owner-only via the command registry, and computes correctly
# --------------------------------------------------------------------


async def test_profit_command_is_owner_only_via_registry() -> None:
    from backend.api.whatsapp_commands import COMMAND_REGISTRY
    from backend.models.enums import UserRole

    assert COMMAND_REGISTRY["profit"].min_role == UserRole.OWNER
    assert COMMAND_REGISTRY["dashboard"].min_role == UserRole.STAFF
    assert COMMAND_REGISTRY["summary"].min_role == UserRole.STAFF
    assert COMMAND_REGISTRY["supplier"].min_role == UserRole.STAFF
    assert COMMAND_REGISTRY["customer"].min_role == UserRole.STAFF
    assert COMMAND_REGISTRY["ledger"].min_role == UserRole.STAFF


async def test_profit_command_renders_report(owner_ctx: RequestContext) -> None:
    result = await handle_profit("today", owner_ctx)
    assert "Profit & Loss" in result.reply
    assert "Net profit:" in result.reply


# --------------------------------------------------------------------
# ReportRepository -- receivables/payables aggregate query correctness
# --------------------------------------------------------------------


async def test_receivables_total_matches_customer_outstanding(
    scenario: Scenario, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        total, count = await ReportRepository(session).receivables_total(ORG)
    assert total == D("600.00")
    assert count == 1


async def test_payables_total_matches_supplier_outstanding(
    scenario: Scenario, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        total, count = await ReportRepository(session).payables_total(ORG)
    assert total == D("2500.00")
    assert count == 1
