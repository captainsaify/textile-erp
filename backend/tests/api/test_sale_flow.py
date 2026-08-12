"""Sale wave: grammar and defaults, stock reduction without touching
average cost, below-cost and credit-limit warnings with RBAC, negative
stock override, idempotent resends, and the revenue/COGS journal
(docs/05_Sales.md §2-§8, docs/06_Accounting.md §3)."""

from __future__ import annotations

import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import CommandResult, RequestContext
from backend.api.commands.sale_commands import (
    handle_sale,
    handle_sale_session_reply,
    parse_sale_command,
)
from backend.core.exceptions import ValidationError
from backend.models import Customer, Inventory, Product, ProductType, User
from backend.models.enums import SalePaymentType
from backend.services.sales_service import idempotency_key
from backend.services.session_service import SessionService
from backend.tests.conftest import (
    SEEDED_MAIN_WAREHOUSE_ID,
    SEEDED_ORG_ID,
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
    return RequestContext(user=staff_user, session_factory=session_factory, message_id="m1")


@pytest.fixture
def owner_ctx(
    owner_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> RequestContext:
    return RequestContext(user=owner_user, session_factory=session_factory, message_id="m2")


@pytest.fixture
async def stocked(
    staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[dict[str, uuid.UUID]]:
    """TRP: 150 KG @ 153.21 avg; MJP: 40 KG @ 214.49 avg."""
    ids: dict[str, uuid.UUID] = {}
    async with session_factory() as session:
        product_type = (
            await session.execute(sa.select(ProductType).where(ProductType.org_id == ORG))
        ).scalar_one()
        for code, description, qty, cost in (
            ("TRP", "Trouser Poly", D("150"), D("153.2100")),
            ("MJP", "Jogging Fabric", D("40"), D("214.4900")),
        ):
            product = Product(
                org_id=ORG,
                product_type_id=product_type.id,
                code=code,
                description=description,
                unit_id=product_type.default_unit_id,
                created_by=staff_user.id,
            )
            session.add(product)
            await session.flush()
            ids[code] = product.id
            session.add(
                Inventory(
                    org_id=ORG,
                    product_id=product.id,
                    warehouse_id=WAREHOUSE,
                    qty_on_hand=qty,
                    weighted_avg_cost=cost,
                )
            )
        customer = Customer(org_id=ORG, name="ABC", created_by=staff_user.id)
        session.add(customer)
        await session.flush()
        ids["customer"] = customer.id
        await session.commit()
    yield ids


async def _reply(text: str, ctx: RequestContext) -> CommandResult:
    state = await SessionService(ctx.session_factory).get(ctx.user.org_id, ctx.user.id)
    return await handle_sale_session_reply(text, ctx, state)


def test_parse_sale_grammar_and_credit_default() -> None:
    draft = parse_sale_command("Customer: ABC\nTRP 20 165\nMJP 5 220")
    assert draft.customer_name == "ABC"
    assert draft.payment_type is SalePaymentType.CREDIT  # §2 default
    assert [(line.code, line.qty, line.rate) for line in draft.lines] == [
        ("TRP", D("20"), D("165")),
        ("MJP", D("5"), D("220")),
    ]
    assert parse_sale_command("Customer: ABC cash\nTRP 1 1").payment_type is SalePaymentType.CASH
    assert parse_sale_command("Customer: ABC bank\nTRP 1 1").payment_type is SalePaymentType.BANK

    with pytest.raises(ValidationError):
        parse_sale_command("no header here")
    with pytest.raises(ValidationError, match="item line"):
        parse_sale_command("Customer: ABC\nTRP twenty 165")


def test_idempotency_key_ignores_whitespace_and_case() -> None:
    a = idempotency_key("+9199", "sale Customer: ABC\nTRP 20 165")
    b = idempotency_key("+9199", "sale customer: abc\n  TRP   20 165 ")
    assert a == b
    assert a != idempotency_key("+9188", "sale Customer: ABC\nTRP 20 165")


async def test_credit_sale_reduces_stock_and_creates_receivable(
    ctx: RequestContext,
    stocked: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A clean sale previews and waits, like a purchase does -- one
    # CONFIRM between a typed rate and stock leaving the building.
    preview = await handle_sale("Customer: ABC\nTRP 20 165\nMJP 5 220", ctx)
    assert "🧾 Sale draft — ABC (credit)" in preview.reply
    result = await _reply("confirm", ctx)
    assert "✅ Sale recorded — ABC (credit)" in result.reply
    assert "TRP  20.0 KG × ₹165.00 = ₹3,300.00" in result.reply
    assert "Total: ₹4,400.00" in result.reply
    assert "ABC now owes: ₹4,400.00 (was ₹0.00)" in result.reply
    assert "Stock after: TRP 130.0 KG · MJP 35.0 KG" in result.reply

    async with session_factory() as session:
        # sales never change average cost (docs/03_Inventory.md §2)
        inventory = (
            await session.execute(
                sa.text(
                    "SELECT p.code, i.qty_on_hand, i.weighted_avg_cost FROM inventory i "
                    "JOIN products p ON p.id = i.product_id ORDER BY p.code"
                )
            )
        ).all()
        assert [(r.code, r.qty_on_hand, r.weighted_avg_cost) for r in inventory] == [
            ("MJP", D("35.000"), D("214.4900")),
            ("TRP", D("130.000"), D("153.2100")),
        ]
        header = (
            await session.execute(
                sa.text(
                    "SELECT payment_type::text, payment_status, amount_paid, grand_total "
                    "FROM sales_headers"
                )
            )
        ).one()
        assert header.payment_type == "credit"
        assert header.payment_status == "unpaid"
        assert header.amount_paid == D("0.00")

        # avg cost snapshot for margin reporting (§3)
        snapshots = (
            (
                await session.execute(
                    sa.text("SELECT avg_cost_at_sale_time FROM sales_lines ORDER BY line_no")
                )
            )
            .scalars()
            .all()
        )
        assert snapshots == [D("153.2100"), D("214.4900")]

        # revenue + COGS balanced (docs/06_Accounting.md §3)
        rows = (
            await session.execute(
                sa.text(
                    "SELECT jl.account_code, jl.debit, jl.credit FROM journal_lines jl "
                    "JOIN journal j ON j.id = jl.journal_id ORDER BY jl.account_code"
                )
            )
        ).all()
        by_account = {r.account_code: (r.debit, r.credit) for r in rows}
        assert by_account["accounts_receivable"] == (D("4400.00"), D("0.00"))
        assert by_account["sales_revenue"] == (D("0.00"), D("4400.00"))
        cogs = D("20") * D("153.21") + D("5") * D("214.49")
        assert by_account["cogs"] == (cogs.quantize(D("0.01")), D("0.00"))
        assert by_account["inventory"] == (D("0.00"), cogs.quantize(D("0.01")))
        assert sum(r.debit for r in rows) == sum(r.credit for r in rows)

        # no cash movement on a credit sale (§3)
        assert (
            await session.execute(sa.text("SELECT count(*) FROM cash_ledger"))
        ).scalar_one() == 0


async def test_cash_sale_posts_ledger_inflow(
    ctx: RequestContext,
    stocked: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await handle_sale("Customer: ABC cash\nTRP 10 200", ctx)
    result = await _reply("confirm", ctx)
    assert "✅ Sale recorded — ABC (cash)" in result.reply
    assert "Cash balance now ₹2,000.00" in result.reply

    async with session_factory() as session:
        entry = (
            await session.execute(sa.text("SELECT amount, entry_type::text FROM cash_ledger"))
        ).one()
        assert entry.amount == D("2000.00")
        assert entry.entry_type == "sale_receipt"
        status = (
            await session.execute(sa.text("SELECT payment_status FROM sales_headers"))
        ).scalar_one()
        assert status == "paid"


async def test_a_near_matching_customer_is_offered_never_taken(
    ctx: RequestContext,
    stocked: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two traders in one town must not collapse into one ledger.

    Names are recorded name-then-town, so two customers in the same city
    share half the string and score well above the 80 threshold:

        Sohail Bhai Lucknow  vs  Rais bhai Lucknow    83
        Zahid Bhai Dimapur   vs  Shahid Bhai Dimnapur 90

    A single candidate over the threshold used to be taken outright --
    only an exact tie between two counted as ambiguous. Both of those
    pairs were silently merged in production, putting sales on a
    stranger's ledger, which means a debt chased from the wrong man.
    """
    from backend.services.sales_service import SalesService

    async with session_factory() as session, session.begin():
        await SalesService(session).create_customer(ctx.user, "Rais bhai Lucknow")

    result = await handle_sale("Customer: Sohail Bhai Lucknow\nTRP 10 165", ctx)
    # Offered, not taken -- and creating the new one stays available.
    assert "Rais bhai Lucknow" in result.reply
    assert "Sale recorded" not in result.reply

    async with session_factory() as session:
        service = SalesService(session)
        match = await service.resolve_customer(ctx.user.org_id, "Sohail Bhai Lucknow")
        assert match.exact is None
        assert [c.name for c in match.near] == ["Rais bhai Lucknow"]

        # An exact name still resolves without a question, whatever the
        # spacing -- asking about a name typed perfectly would be noise.
        match = await service.resolve_customer(ctx.user.org_id, "  rais   bhai  lucknow ")
        assert match.exact is not None and match.exact.name == "Rais bhai Lucknow"

    await _reply("discard", ctx)


async def test_sale_charges_bill_the_customer_without_inflating_revenue(
    ctx: RequestContext,
    stocked: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GST and packing belong on the bill, not in revenue.

    Sales had no charge columns at all, so anything recovered from a
    customer on top of the goods was being written down as a separate
    operating *expense* -- money coming in, recorded on the side of the
    books where money goes out.

    They are credited to OTHER_INCOME rather than SALES_REVENUE. Folding
    a tax into revenue would inflate the revenue line and the gross
    margin with money never earned on the goods, and gross margin is the
    figure these partners actually steer by.
    """
    await handle_sale("Customer: ABC\nTRP 20 165", ctx)

    result = await _reply("GST 594", ctx)
    assert "Goods: ₹3,300.00" in result.reply
    assert "Total: ₹3,894.00" in result.reply

    # A second charge joins it, itemised; re-sending one re-states it.
    result = await _reply("packing 100", ctx)
    assert "Other charges: ₹694.00  (GST ₹594.00 + PACKING ₹100.00)" in result.reply
    result = await _reply("GST 500", ctx)
    assert "Other charges: ₹600.00" in result.reply
    assert "Total: ₹3,900.00" in result.reply

    # An item line is not a charge, or the bill grows silently.
    result = await _reply("TRP 800", ctx)
    assert "Total: ₹3,900.00" not in result.reply or "Sale draft" not in result.reply

    result = await _reply("confirm", ctx)
    assert "✅ Sale recorded" in result.reply

    async with session_factory() as session:
        header = (
            await session.execute(
                sa.text("SELECT subtotal, other_charges, grand_total FROM sales_headers")
            )
        ).one()
        assert (header.subtotal, header.other_charges, header.grand_total) == (
            D("3300.00"),
            D("600.00"),
            D("3900.00"),
        )
        # Revenue is the goods alone; the charges sit in other_income.
        posted = (
            await session.execute(
                sa.text(
                    "SELECT account_code, sum(credit) AS credit FROM journal_lines "
                    "WHERE credit > 0 GROUP BY account_code ORDER BY account_code"
                )
            )
        ).all()
        by_code = {row.account_code: row.credit for row in posted}
        assert by_code["sales_revenue"] == D("3300.00")
        assert by_code["other_income"] == D("600.00")


async def test_below_cost_warning_blocks_staff_and_owner_confirms(
    ctx: RequestContext, owner_ctx: RequestContext, stocked: dict[str, uuid.UUID]
) -> None:
    result = await handle_sale("Customer: ABC\nTRP 10 140", ctx)
    assert "average cost is ₹153.21/KG" in result.reply
    assert "Only an owner can confirm" in result.reply

    result = await _reply("confirm", ctx)
    assert "Only an owner can confirm" in result.reply

    # owner has their own session; drive the same sale through it
    result = await handle_sale("Customer: ABC\nTRP 10 140", owner_ctx)
    assert 'Reply "confirm" to proceed anyway' in result.reply
    result = await _reply("confirm", owner_ctx)
    assert "✅ Sale recorded" in result.reply


async def test_insufficient_stock_blocks_then_override(
    ctx: RequestContext,
    stocked: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result = await handle_sale("Customer: ABC\nMJP 60 300", ctx)
    assert "MJP has 40.0 KG in stock, this sale needs 60.0 KG" in result.reply
    assert 'Reply "override"' in result.reply

    result = await _reply("line 1 qty 30", ctx)
    assert "🧾 Sale draft" in result.reply  # corrected, and now clean
    result = await _reply("confirm", ctx)
    assert "✅ Sale recorded" in result.reply

    async with session_factory() as session:
        qty = (
            await session.execute(
                sa.text(
                    "SELECT i.qty_on_hand FROM inventory i JOIN products p "
                    "ON p.id = i.product_id WHERE p.code = 'MJP'"
                )
            )
        ).scalar_one()
        assert qty == D("10.000")


async def test_override_allows_negative_stock(
    ctx: RequestContext,
    stocked: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await handle_sale("Customer: ABC\nMJP 50 300", ctx)
    result = await _reply("override", ctx)
    assert "✅ Sale recorded" in result.reply
    async with session_factory() as session:
        qty = (
            await session.execute(
                sa.text(
                    "SELECT i.qty_on_hand FROM inventory i JOIN products p "
                    "ON p.id = i.product_id WHERE p.code = 'MJP'"
                )
            )
        ).scalar_one()
        assert qty == D("-10.000")


async def test_identical_resend_is_not_double_counted(
    ctx: RequestContext,
    stocked: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    text = "Customer: ABC\nTRP 20 165"
    await handle_sale(text, ctx)
    first = await _reply("confirm", ctx)
    assert "✅ Sale recorded" in first.reply

    await handle_sale(text, ctx)
    second = await _reply("confirm", ctx)
    assert "↩️ This looks identical" in second.reply

    async with session_factory() as session:
        count = (await session.execute(sa.text("SELECT count(*) FROM sales_headers"))).scalar_one()
        assert count == 1


async def test_unknown_customer_offers_creation(
    ctx: RequestContext, stocked: dict[str, uuid.UUID]
) -> None:
    result = await handle_sale("Customer: Brand New Buyer\nTRP 5 200", ctx)
    assert "not found" in result.reply
    assert "create customer" in result.reply

    result = await _reply("create customer", ctx)
    assert "🧾 Sale draft — Brand New Buyer (credit)" in result.reply
    result = await _reply("confirm", ctx)
    assert "✅ Sale recorded — Brand New Buyer (credit)" in result.reply


async def test_credit_limit_warning(
    owner_ctx: RequestContext,
    stocked: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await session.execute(
            sa.text("UPDATE customers SET credit_limit = 1000 WHERE name = 'ABC'")
        )
        await session.commit()

    result = await handle_sale("Customer: ABC\nTRP 20 165", owner_ctx)
    assert "credit limit is ₹1,000.00" in result.reply
    assert "outstanding to ₹3,300.00" in result.reply  # 20 x 165
    result = await _reply("confirm", owner_ctx)
    assert "✅ Sale recorded" in result.reply


# --------------------------------------------------------------------
# the duplicate guard is a window, not a life sentence
# --------------------------------------------------------------------


def test_the_same_sale_text_keys_differently_once_the_window_passes() -> None:
    """`sales_headers_org_idempotency_active_uq` is absolute, so a key
    derived from the text alone meant a customer could never buy the
    same things for the same money twice. It cost a real ₹1,65,000 sale
    two days after an identical one, with the refusal claiming it had
    "just" been sent."""
    import datetime as dt

    from backend.services.sales_service import DEDUP_BUCKET_MINUTES, idempotency_key

    text = "Customer: Hanif Pune credit\n55D 1200 137.50"
    monday = dt.datetime(2026, 7, 30, 12, 0, tzinfo=dt.UTC)

    # an accidental re-send moments later still collides
    assert idempotency_key("+91", text, now=monday) == idempotency_key(
        "+91", text, now=monday + dt.timedelta(seconds=30)
    )
    # two days later is a different sale, and is allowed to be one
    assert idempotency_key("+91", text, now=monday) != idempotency_key(
        "+91", text, now=monday + dt.timedelta(days=2)
    )
    # the window is the bucket, not forever
    assert idempotency_key("+91", text, now=monday) != idempotency_key(
        "+91", text, now=monday + dt.timedelta(minutes=DEDUP_BUCKET_MINUTES * 2)
    )


async def test_a_sale_can_be_dated(
    ctx: RequestContext,
    stocked: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Goods leave on one day and get entered on another; filing the
    sale under the day it was typed puts it in the wrong month."""
    import datetime as dt

    from backend.models import CashLedger, SalesHeader

    preview = await handle_sale("Customer: ABC cash on 28-07-2026\nTRP 10 200", ctx)
    assert "28-07-2026" in preview.reply

    recorded = await _reply("confirm", ctx)
    assert "Sale recorded" in recorded.reply

    async with session_factory() as session:
        row = (
            await session.execute(
                sa.select(SalesHeader.sale_date, CashLedger.entry_date)
                .join(CashLedger, CashLedger.source_id == SalesHeader.id, isouter=True)
                .where(SalesHeader.org_id == ORG)
                .order_by(SalesHeader.created_at.desc())
                .limit(1)
            )
        ).one()
    # the header and the money it moved land on the same day
    assert row[0] == dt.date(2026, 7, 28)
    assert row[1] in (None, dt.date(2026, 7, 28))


def test_a_customer_named_with_on_keeps_it() -> None:
    """`on` is a date clause only when a date follows it."""
    from backend.api.commands.sale_commands import parse_sale_command

    draft = parse_sale_command("Customer: Hands on Traders credit\nTRP 10 100")

    assert draft.customer_name == "Hands on Traders"
    assert draft.on is None


async def test_charge_can_be_added_after_a_sale_is_confirmed(
    ctx: RequestContext,
    stocked: dict[str, uuid.UUID],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GST that turns up an hour after the goods did.

    Charges could only be typed while a draft was open, and a sale's
    draft closes the instant it confirms -- which is immediately unless
    something looks wrong. So the words were reachable exactly when a
    sale was *problematic* and unreachable when it was clean, and a
    charge arriving later was being booked as an operating expense:
    money the customer owes, filed where money leaves.
    """
    from backend.api.commands.correction_commands import handle_charge

    await handle_sale("Customer: ABC\nTRP 20 165", ctx)
    await _reply("confirm", ctx)

    async with session_factory() as session:
        sale_id = (
            await session.execute(sa.text("SELECT id FROM sales_headers LIMIT 1"))
        ).scalar_one()

    result = await handle_charge(
        f"{str(sale_id)[:8]} GST 594 note: shared with the Lucknow bill", ctx
    )
    assert "GST ₹594.00 added to sale" in result.reply
    assert "₹3,300.00 → ₹3,894.00" in result.reply
    assert "Now owes: ₹3,894.00" in result.reply

    async with session_factory() as session:
        row = (
            await session.execute(
                sa.text(
                    "SELECT subtotal, other_charges, grand_total, payment_status, notes "
                    "FROM sales_headers"
                )
            )
        ).one()
        assert (row.subtotal, row.other_charges, row.grand_total) == (
            D("3300.00"),
            D("594.00"),
            D("3894.00"),
        )
        assert row.notes == "shared with the Lucknow bill"
        # Not revenue: the goods are what was sold.
        rows = (
            await session.execute(
                sa.text(
                    "SELECT account_code, sum(credit) AS total FROM journal_lines "
                    "WHERE credit > 0 GROUP BY account_code"
                )
            )
        ).all()
        credits: dict[str, decimal.Decimal] = {row.account_code: row.total for row in rows}
        assert credits["sales_revenue"] == D("3300.00")
        assert credits["other_income"] == D("594.00")

    # Unknown bill says so rather than failing quietly.
    assert "bill" in (await handle_charge("nosuchbill GST 100", ctx)).reply.lower()
