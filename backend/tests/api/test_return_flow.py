"""`return` -- docs/08_WhatsApp.md #return, docs/05_Sales.md §6,
docs/03_Inventory.md §2/§4.

The two properties worth guarding, because getting either wrong is
silent:

1. **Cost history.** A sale return adds stock back at the cost it left
   at and must not touch the running average; a purchase return unwinds
   the average using the original landed cost, and when it can't do that
   exactly it says so instead of emitting a wrong number.
2. **No assumed ledger movement.** Reversing a paid cash sale must ask
   before any cash moves.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import RequestContext
from backend.api.commands.return_commands import (
    handle_return,
    handle_return_session_reply,
    parse_return_command,
)
from backend.core.exceptions import ValidationError
from backend.models import (
    Customer,
    Inventory,
    Product,
    PurchaseHeader,
    PurchaseLine,
    SalesHeader,
    SalesLine,
    Supplier,
    User,
)
from backend.models.enums import SalePaymentType
from backend.repositories.accounting_repository import LedgerRepository
from backend.services.session_service import (
    AWAITING_RETURN_REFUND_CHOICE,
    SessionService,
)
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
    return RequestContext(user=staff_user, session_factory=session_factory, message_id="m1")


@pytest.fixture
def owner_ctx(
    owner_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> RequestContext:
    return RequestContext(user=owner_user, session_factory=session_factory, message_id="m2")


@dataclasses.dataclass(frozen=True)
class Scene:
    product_id: uuid.UUID
    code: str
    customer_name: str
    supplier_name: str
    invoice_no: str
    sale_id: uuid.UUID
    purchase_line_id: uuid.UUID


async def _build(
    session: AsyncSession,
    actor: User,
    *,
    qty_on_hand: str,
    avg_cost: str,
    sale_payment: SalePaymentType,
    sale_amount_paid: str,
    sale_qty: str = "10",
    sale_rate: str = "200",
    sale_avg_cost: str = "150",
    purchase_qty: str = "50",
    purchase_landed: str = "160",
) -> Scene:
    suffix = uuid.uuid4().hex[:6]
    product = Product(
        org_id=ORG,
        product_type_id=uuid.UUID(SEEDED_TEXTILE_TYPE_ID),
        code=f"RET{suffix.upper()}",
        description="Return Test Fabric",
        unit_id=uuid.UUID(SEEDED_KG_UNIT_ID),
        created_by=actor.id,
    )
    session.add(product)
    await session.flush()
    session.add(
        Inventory(
            org_id=ORG,
            product_id=product.id,
            warehouse_id=WAREHOUSE,
            qty_on_hand=D(qty_on_hand),
            weighted_avg_cost=D(avg_cost),
        )
    )

    customer = Customer(org_id=ORG, name=f"Cust {suffix}", created_by=actor.id)
    supplier = Supplier(org_id=ORG, name=f"Supp {suffix}", created_by=actor.id)
    session.add_all([customer, supplier])
    await session.flush()

    sale = SalesHeader(
        org_id=ORG,
        customer_id=customer.id,
        warehouse_id=WAREHOUSE,
        sale_date=datetime.date.today(),
        payment_type=sale_payment,
        subtotal=(D(sale_qty) * D(sale_rate)),
        grand_total=(D(sale_qty) * D(sale_rate)),
        amount_paid=D(sale_amount_paid),
        status="confirmed",
        created_by=actor.id,
    )
    session.add(sale)
    await session.flush()
    session.add(
        SalesLine(
            org_id=ORG,
            sales_header_id=sale.id,
            line_no=1,
            product_id=product.id,
            qty=D(sale_qty),
            rate=D(sale_rate),
            line_total=(D(sale_qty) * D(sale_rate)),
            avg_cost_at_sale_time=D(sale_avg_cost),
        )
    )

    invoice_no = f"INV-{suffix}"
    purchase = PurchaseHeader(
        org_id=ORG,
        supplier_id=supplier.id,
        warehouse_id=WAREHOUSE,
        invoice_no=invoice_no,
        invoice_date=datetime.date.today(),
        grand_total=(D(purchase_qty) * D(purchase_landed)),
        amount_paid=D("0"),
        status="confirmed",
        created_by=actor.id,
    )
    session.add(purchase)
    await session.flush()
    purchase_line = PurchaseLine(
        org_id=ORG,
        purchase_header_id=purchase.id,
        line_no=1,
        product_id=product.id,
        qty=D(purchase_qty),
        rate=D(purchase_landed),
        line_total=(D(purchase_qty) * D(purchase_landed)),
        landed_cost_per_unit=D(purchase_landed),
    )
    session.add(purchase_line)
    await session.flush()

    return Scene(
        product_id=product.id,
        code=product.code,
        customer_name=customer.name,
        supplier_name=supplier.name,
        invoice_no=invoice_no,
        sale_id=sale.id,
        purchase_line_id=purchase_line.id,
    )


async def _inventory(
    session_factory: async_sessionmaker[AsyncSession], product_id: uuid.UUID
) -> tuple[decimal.Decimal, decimal.Decimal]:
    async with session_factory() as session:
        row = (
            await session.execute(
                sa.select(Inventory.qty_on_hand, Inventory.weighted_avg_cost).where(
                    Inventory.product_id == product_id
                )
            )
        ).one()
    return row[0], row[1]


# --------------------------------------------------------------------
# grammar
# --------------------------------------------------------------------


def test_parse_return_sale_with_reason() -> None:
    kind, reference, code, qty, reason = parse_return_command(
        "sale ABC TRP 5 reason: wrong color shipped"
    )
    assert (kind, reference, code, qty) == ("sale", "ABC", "TRP", D("5"))
    assert reason == "wrong color shipped"


def test_parse_return_handles_multiword_reference() -> None:
    kind, reference, code, qty, _ = parse_return_command("sale Shree Textiles TRP 2")
    assert reference == "Shree Textiles"
    assert (kind, code, qty) == ("sale", "TRP", D("2"))


def test_parse_return_rejects_garbage() -> None:
    with pytest.raises(ValidationError):
        parse_return_command("everything back please")


# --------------------------------------------------------------------
# sale returns: cost history
# --------------------------------------------------------------------


async def test_sale_return_adds_stock_without_touching_average_cost(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """docs/03_Inventory.md §2: a sale return comes back at the historical
    cost and the running average is unchanged. Recomputing it here would
    let an old sale retroactively distort today's costing."""
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="100",
            avg_cost="180",  # deliberately different from the sale's 150
            sale_payment=SalePaymentType.CREDIT,
            sale_amount_paid="0",
        )

    result = await handle_return(f"sale {scene.customer_name} {scene.code} 4", ctx)
    assert "✅ Return recorded" in result.reply

    qty, avg = await _inventory(session_factory, scene.product_id)
    assert qty == D("104.000")
    assert avg == D("180.0000")  # untouched


async def test_sale_return_movement_records_the_historical_cost(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="100",
            avg_cost="180",
            sale_payment=SalePaymentType.CREDIT,
            sale_amount_paid="0",
        )
    await handle_return(f"sale {scene.customer_name} {scene.code} 4", ctx)

    async with session_factory() as session:
        unit_cost = (
            await session.execute(
                sa.text(
                    "SELECT unit_cost FROM inventory_movements "
                    "WHERE movement_type = 'sale_return' AND product_id = :pid"
                ),
                {"pid": scene.product_id},
            )
        ).scalar_one()
    assert unit_cost == D("150.0000")  # avg_cost_at_sale_time, not 180


async def test_sale_return_reduces_a_credit_customers_receivable(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="100",
            avg_cost="180",
            sale_payment=SalePaymentType.CREDIT,
            sale_amount_paid="0",
        )
    result = await handle_return(f"sale {scene.customer_name} {scene.code} 5", ctx)
    assert "outstanding reduced by ₹1,000.00" in result.reply  # 5 x 200


async def test_returning_more_than_was_sold_is_refused(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="100",
            avg_cost="180",
            sale_payment=SalePaymentType.CREDIT,
            sale_amount_paid="0",
            sale_qty="10",
        )
    result = await handle_return(f"sale {scene.customer_name} {scene.code} 11", ctx)
    assert "still returnable" in result.reply
    qty, _ = await _inventory(session_factory, scene.product_id)
    assert qty == D("100.000")  # nothing moved


async def test_repeated_returns_accumulate_against_the_original_quantity(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="100",
            avg_cost="180",
            sale_payment=SalePaymentType.CREDIT,
            sale_amount_paid="0",
            sale_qty="10",
        )
    await handle_return(f"sale {scene.customer_name} {scene.code} 6", ctx)
    second = await handle_return(f"sale {scene.customer_name} {scene.code} 6", ctx)
    assert "still returnable" in second.reply  # only 4 left, not 10 again


async def test_full_return_marks_the_sale_returned(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="100",
            avg_cost="180",
            sale_payment=SalePaymentType.CREDIT,
            sale_amount_paid="0",
            sale_qty="10",
        )
    await handle_return(f"sale {scene.customer_name} {scene.code} 4", ctx)
    async with session_factory() as session:
        status = (
            await session.execute(
                sa.select(SalesHeader.status).where(SalesHeader.id == scene.sale_id)
            )
        ).scalar_one()
    assert status == "partially_returned"

    await handle_return(f"sale {scene.customer_name} {scene.code} 6", ctx)
    async with session_factory() as session:
        status = (
            await session.execute(
                sa.select(SalesHeader.status).where(SalesHeader.id == scene.sale_id)
            )
        ).scalar_one()
    assert status == "returned"


# --------------------------------------------------------------------
# sale returns: the refund question
# --------------------------------------------------------------------


async def test_paid_cash_sale_asks_before_moving_any_money(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """docs/05_Sales.md §6: whether cash actually left the drawer is a
    fact only the partner knows, so nothing posts until they say."""
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="100",
            avg_cost="180",
            sale_payment=SalePaymentType.CASH,
            sale_amount_paid="2000",  # 10 x 200, paid in full
        )
    cash_before = await _cash(session_factory)

    result = await handle_return(f"sale {scene.customer_name} {scene.code} 5", ctx)
    assert "already paid" in result.reply
    assert "refund cash" in result.reply and "credit" in result.reply

    # nothing has moved yet -- not stock, not cash
    qty, _ = await _inventory(session_factory, scene.product_id)
    assert qty == D("100.000")
    assert await _cash(session_factory) == cash_before

    state = await SessionService(session_factory).get(ORG, ctx.user.id)
    assert state.state == AWAITING_RETURN_REFUND_CHOICE


async def _cash(session_factory: async_sessionmaker[AsyncSession]) -> decimal.Decimal:
    async with session_factory() as session:
        return await LedgerRepository(session).balance(ORG, "cash")


async def test_refund_cash_posts_the_ledger_movement(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="100",
            avg_cost="180",
            sale_payment=SalePaymentType.CASH,
            sale_amount_paid="2000",
        )
    await handle_return(f"sale {scene.customer_name} {scene.code} 5", ctx)
    state = await SessionService(session_factory).get(ORG, ctx.user.id)
    result = await handle_return_session_reply("refund cash", ctx, state)

    assert "Refunded ₹1,000.00 from cash" in result.reply
    assert await _cash(session_factory) == D("-1000.00")
    qty, _ = await _inventory(session_factory, scene.product_id)
    assert qty == D("105.000")


async def test_credit_choice_moves_stock_but_no_cash(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="100",
            avg_cost="180",
            sale_payment=SalePaymentType.CASH,
            sale_amount_paid="2000",
        )
    await handle_return(f"sale {scene.customer_name} {scene.code} 5", ctx)
    state = await SessionService(session_factory).get(ORG, ctx.user.id)
    result = await handle_return_session_reply("credit", ctx, state)

    assert "held as credit" in result.reply
    assert await _cash(session_factory) == D("0")
    qty, _ = await _inventory(session_factory, scene.product_id)
    assert qty == D("105.000")


async def test_unrecognised_refund_reply_repeats_the_question(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="100",
            avg_cost="180",
            sale_payment=SalePaymentType.CASH,
            sale_amount_paid="2000",
        )
    await handle_return(f"sale {scene.customer_name} {scene.code} 5", ctx)
    state = await SessionService(session_factory).get(ORG, ctx.user.id)
    result = await handle_return_session_reply("maybe later", ctx, state)
    assert "refund cash" in result.reply
    qty, _ = await _inventory(session_factory, scene.product_id)
    assert qty == D("100.000")  # still nothing moved


async def test_unpaid_credit_sale_does_not_ask(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Only an already-settled sale raises the refund question; a credit
    sale is pure bookkeeping."""
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="100",
            avg_cost="180",
            sale_payment=SalePaymentType.CREDIT,
            sale_amount_paid="0",
        )
    result = await handle_return(f"sale {scene.customer_name} {scene.code} 5", ctx)
    assert "✅ Return recorded" in result.reply


# --------------------------------------------------------------------
# purchase returns: unwinding the weighted average
# --------------------------------------------------------------------


async def test_purchase_return_unwinds_the_average_exactly(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """150 KG @ 153.3333 contains 50 KG @ 160 from this invoice.
    Returning 50 leaves (150*153.3333 - 50*160) / 100 = 150.00 --
    the pre-purchase average, because the purchase is fully unwound."""
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="150",
            avg_cost="153.3333",
            sale_payment=SalePaymentType.CREDIT,
            sale_amount_paid="0",
            purchase_qty="50",
            purchase_landed="160",
        )

    result = await handle_return(f"purchase {scene.invoice_no} {scene.code} 50", ctx)
    assert "✅ Return recorded" in result.reply
    assert "⚠️" not in result.reply  # exact, no approximation warning

    qty, avg = await _inventory(session_factory, scene.product_id)
    assert qty == D("100.000")
    assert avg == D("150.0000")


async def test_partial_purchase_return_unwinds_proportionally(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="150",
            avg_cost="153.3333",
            sale_payment=SalePaymentType.CREDIT,
            sale_amount_paid="0",
            purchase_qty="50",
            purchase_landed="160",
        )
    await handle_return(f"purchase {scene.invoice_no} {scene.code} 20", ctx)

    qty, avg = await _inventory(session_factory, scene.product_id)
    assert qty == D("130.000")
    # (150*153.3333 - 20*160) / 130
    expected = ((D("150") * D("153.3333")) - (D("20") * D("160"))) / D("130")
    assert avg == expected.quantize(D("0.0001"))


async def test_purchase_return_approximates_and_says_so_when_batch_is_gone(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """docs/03_Inventory.md §4: once most of the batch has been sold and
    mixed with later purchases, exact reversal is impossible. The system
    must not emit a wrong average -- it holds the average, reduces value
    proportionally, and flags for review."""
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            # only 10 KG left, but the invoice was for 50 KG @ 160:
            # 10*100 - 10*160 is negative, so no exact answer exists
            qty_on_hand="10",
            avg_cost="100",
            sale_payment=SalePaymentType.CREDIT,
            sale_amount_paid="0",
            purchase_qty="50",
            purchase_landed="160",
        )
    result = await handle_return(f"purchase {scene.invoice_no} {scene.code} 10", ctx)

    assert "✅ Return recorded" in result.reply
    assert "couldn't be unwound exactly" in result.reply
    qty, avg = await _inventory(session_factory, scene.product_id)
    assert qty == D("0.000")
    assert avg == D("100.0000")  # held, not driven negative


async def test_approximated_return_never_produces_a_negative_average(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The failure this whole branch exists to prevent: a negative or
    absurd average silently corrupting the cost basis of every later
    sale, with nothing raising."""
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="12",
            avg_cost="50",
            sale_payment=SalePaymentType.CREDIT,
            sale_amount_paid="0",
            purchase_qty="50",
            purchase_landed="160",
        )
    await handle_return(f"purchase {scene.invoice_no} {scene.code} 11", ctx)
    _, avg = await _inventory(session_factory, scene.product_id)
    assert avg > D("0")


async def test_purchase_return_reduces_what_is_owed(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="150",
            avg_cost="153.3333",
            sale_payment=SalePaymentType.CREDIT,
            sale_amount_paid="0",
            purchase_qty="50",
            purchase_landed="160",
        )
    result = await handle_return(f"purchase {scene.invoice_no} {scene.code} 10", ctx)
    assert "Owed to" in result.reply
    assert "₹1,600.00" in result.reply  # 10 x 160


async def test_returning_more_than_was_purchased_is_refused(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="150",
            avg_cost="153.3333",
            sale_payment=SalePaymentType.CREDIT,
            sale_amount_paid="0",
            purchase_qty="50",
            purchase_landed="160",
        )
    result = await handle_return(f"purchase {scene.invoice_no} {scene.code} 60", ctx)
    assert "still returnable" in result.reply
    qty, _ = await _inventory(session_factory, scene.product_id)
    assert qty == D("150.000")


# --------------------------------------------------------------------
# books stay balanced
# --------------------------------------------------------------------


async def test_every_return_posts_a_balanced_journal(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="150",
            avg_cost="153.3333",
            sale_payment=SalePaymentType.CREDIT,
            sale_amount_paid="0",
            purchase_qty="50",
            purchase_landed="160",
        )
    await handle_return(f"sale {scene.customer_name} {scene.code} 3", ctx)
    await handle_return(f"purchase {scene.invoice_no} {scene.code} 5", ctx)

    async with session_factory() as session:
        unbalanced = (
            await session.execute(
                sa.text(
                    "SELECT count(*) FROM ("
                    "  SELECT journal_id FROM journal_lines"
                    "  GROUP BY journal_id HAVING sum(debit) <> sum(credit)"
                    ") bad"
                )
            )
        ).scalar_one()
        return_journals = (
            await session.execute(
                sa.text(
                    "SELECT count(*) FROM journal WHERE org_id = :org "
                    "AND source_type IN ('sale_return', 'purchase_return')"
                ),
                {"org": ORG},
            )
        ).scalar_one()
    assert unbalanced == 0
    assert return_journals == 2


async def test_movements_reconcile_with_inventory_after_returns(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """CLAUDE.md's acceptance criterion, applied to this wave: qty_on_hand
    equals the signed sum of movements."""
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="150",
            avg_cost="153.3333",
            sale_payment=SalePaymentType.CREDIT,
            sale_amount_paid="0",
            purchase_qty="50",
            purchase_landed="160",
        )
    await handle_return(f"sale {scene.customer_name} {scene.code} 3", ctx)
    await handle_return(f"purchase {scene.invoice_no} {scene.code} 5", ctx)

    async with session_factory() as session:
        movement_sum = (
            await session.execute(
                sa.text(
                    "SELECT coalesce(sum(qty_delta), 0) FROM inventory_movements "
                    "WHERE product_id = :pid"
                ),
                {"pid": scene.product_id},
            )
        ).scalar_one()
    qty, _ = await _inventory(session_factory, scene.product_id)
    # opening 150 was seeded directly, so movements account for the delta
    assert qty == D("150") + movement_sum


# --------------------------------------------------------------------
# permissions
# --------------------------------------------------------------------


async def test_staff_cannot_return_against_an_old_transaction(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """docs/08_WhatsApp.md #return: staff may reverse their own recent
    entries; older ones need an owner."""
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            ctx.user,
            qty_on_hand="100",
            avg_cost="180",
            sale_payment=SalePaymentType.CREDIT,
            sale_amount_paid="0",
        )
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text(
                "UPDATE sales_headers SET created_at = now() - interval '48 hours' WHERE id = :id"
            ),
            {"id": scene.sale_id},
        )

    result = await handle_return(f"sale {scene.customer_name} {scene.code} 2", ctx)
    assert "owner needs to record this return" in result.reply


async def test_owner_can_return_against_an_old_transaction(
    owner_ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        scene = await _build(
            session,
            owner_ctx.user,
            qty_on_hand="100",
            avg_cost="180",
            sale_payment=SalePaymentType.CREDIT,
            sale_amount_paid="0",
        )
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text(
                "UPDATE sales_headers SET created_at = now() - interval '48 hours' WHERE id = :id"
            ),
            {"id": scene.sale_id},
        )

    result = await handle_return(f"sale {scene.customer_name} {scene.code} 2", owner_ctx)
    assert "✅ Return recorded" in result.reply


async def test_return_is_registered_for_staff() -> None:
    from backend.api.whatsapp_commands import COMMAND_REGISTRY
    from backend.models.enums import UserRole

    assert COMMAND_REGISTRY["return"].min_role == UserRole.STAFF
