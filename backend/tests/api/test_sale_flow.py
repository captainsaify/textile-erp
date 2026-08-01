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
