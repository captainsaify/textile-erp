"""`edit` / `undo` / `delete` -- docs/08_WhatsApp.md, docs/04_Purchases.md
§8, docs/02_Database.md §4.

The property that makes `undo` safe to have at all: it reverses by
*compensating entry*, never by deleting rows. After an undo the original
header, its lines and its movements are all still there, marked
cancelled, alongside the movements that reversed them. Several tests
below assert exactly that, because an implementation that quietly
deleted instead would pass every "the balance is right" check while
destroying the audit trail.
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
from backend.api.commands.correction_commands import handle_delete, handle_edit, handle_undo
from backend.api.commands.money_commands import handle_expense, handle_income
from backend.models import (
    Brand,
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
from backend.models.enums import PurchaseStatus, SalePaymentType
from backend.repositories.accounting_repository import LedgerRepository
from backend.services.audit_service import AuditService
from backend.services.inventory_service import InventoryService
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
def ctx(owner_user: User, session_factory: async_sessionmaker[AsyncSession]) -> RequestContext:
    return RequestContext(user=owner_user, session_factory=session_factory, message_id="m1")


@pytest.fixture
def staff_ctx(
    staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> RequestContext:
    return RequestContext(user=staff_user, session_factory=session_factory, message_id="m2")


@dataclasses.dataclass(frozen=True)
class Master:
    product_id: uuid.UUID
    code: str
    supplier_name: str
    customer_name: str
    brand_name: str


async def _master(session: AsyncSession, actor: User, *, qty: str = "0") -> Master:
    suffix = uuid.uuid4().hex[:6]
    brand = Brand(org_id=ORG, name=f"Brand {suffix}")
    session.add(brand)
    product = Product(
        org_id=ORG,
        product_type_id=uuid.UUID(SEEDED_TEXTILE_TYPE_ID),
        code=f"COR{suffix.upper()}",
        description="Correction Test Fabric",
        unit_id=uuid.UUID(SEEDED_KG_UNIT_ID),
        created_by=actor.id,
    )
    supplier = Supplier(org_id=ORG, name=f"Supp {suffix}", created_by=actor.id)
    customer = Customer(org_id=ORG, name=f"Cust {suffix}", created_by=actor.id)
    session.add_all([product, supplier, customer])
    await session.flush()
    session.add(
        Inventory(
            org_id=ORG,
            product_id=product.id,
            warehouse_id=WAREHOUSE,
            qty_on_hand=D(qty),
            weighted_avg_cost=D("100"),
        )
    )
    await session.flush()
    return Master(
        product_id=product.id,
        code=product.code,
        supplier_name=supplier.name,
        customer_name=customer.name,
        brand_name=brand.name,
    )


async def _stock(
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


async def _cash(session_factory: async_sessionmaker[AsyncSession]) -> decimal.Decimal:
    async with session_factory() as session:
        return await LedgerRepository(session).balance(ORG, "cash")


# --------------------------------------------------------------------
# edit
# --------------------------------------------------------------------


async def test_edit_changes_an_allowed_field(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        master = await _master(session, ctx.user)
    result = await handle_edit(f"product {master.code} reorder_level 20", ctx)
    assert "reorder_level" in result.reply and "20" in result.reply

    async with session_factory() as session:
        level = (
            await session.execute(
                sa.select(Product.reorder_level).where(Product.id == master.product_id)
            )
        ).scalar_one()
    assert level == D("20")


async def test_edit_refuses_a_field_not_on_the_allow_list(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`code` is deliberately not editable: inventory, movements and the
    OCR learning dictionary all key off it."""
    async with session_factory() as session, session.begin():
        master = await _master(session, ctx.user)
    result = await handle_edit(f"product {master.code} code NEWCODE", ctx)
    # refused, and the refusal names what *is* editable
    assert "Editable on a product" in result.reply
    assert "reorder_level" in result.reply
    async with session_factory() as session:
        code = (
            await session.execute(sa.select(Product.code).where(Product.id == master.product_id))
        ).scalar_one()
    assert code == master.code  # unchanged


async def test_edit_handles_a_multiword_reference(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Customer and supplier names have spaces; a positional split would
    chop "Acme Traders" in half and look up the wrong party."""
    async with session_factory() as session, session.begin():
        session.add(Customer(org_id=ORG, name="Acme Traders", created_by=ctx.user.id))

    result = await handle_edit("customer Acme Traders credit_limit 5000", ctx)
    assert "credit_limit" in result.reply
    async with session_factory() as session:
        limit = (
            await session.execute(
                sa.select(Customer.credit_limit).where(Customer.name == "Acme Traders")
            )
        ).scalar_one()
    assert limit == D("5000")


async def test_edit_records_before_and_after(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        master = await _master(session, ctx.user)
    await handle_edit(f"customer {master.customer_name} credit_limit 5000", ctx)
    async with session_factory() as session:
        row = (
            await session.execute(
                sa.text(
                    "SELECT before_state, after_state FROM audit_logs "
                    "WHERE action = 'customer.edited' AND org_id = :org"
                ),
                {"org": ORG},
            )
        ).one()
    assert row[1]["credit_limit"] == "5000"


async def test_editing_a_confirmed_transaction_points_at_undo(ctx: RequestContext) -> None:
    """docs/04_Purchases.md §8: a confirmed purchase is immutable
    because stock and books were already derived from it."""
    result = await handle_edit("purchase INV-1 rate 100", ctx)
    assert "never changed in place" in result.reply
    assert "undo purchase" in result.reply


async def test_edit_rejects_a_negative_number(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        master = await _master(session, ctx.user)
    result = await handle_edit(f"product {master.code} reorder_level -5", ctx)
    assert "can't be negative" in result.reply


# --------------------------------------------------------------------
# delete
# --------------------------------------------------------------------


async def test_delete_soft_deletes_master_data(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        master = await _master(session, ctx.user)
    result = await handle_delete(f"brand {master.brand_name}", ctx)
    assert "deleted" in result.reply

    async with session_factory() as session:
        deleted_at = (
            await session.execute(
                sa.select(Brand.deleted_at).where(Brand.name == master.brand_name)
            )
        ).scalar_one()
    assert deleted_at is not None  # the row is still there


async def test_delete_refuses_a_product_that_still_has_stock(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        master = await _master(session, ctx.user, qty="40")
    result = await handle_delete(f"product {master.code}", ctx)
    assert "still has" in result.reply


async def test_delete_refuses_a_customer_who_still_owes(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Filing away a party with an open balance would make a real
    receivable vanish from the dashboard."""
    async with session_factory() as session, session.begin():
        master = await _master(session, ctx.user)
        customer = (
            await session.execute(sa.select(Customer).where(Customer.name == master.customer_name))
        ).scalar_one()
        session.add(
            SalesHeader(
                org_id=ORG,
                customer_id=customer.id,
                warehouse_id=WAREHOUSE,
                sale_date=datetime.date.today(),
                payment_type=SalePaymentType.CREDIT,
                grand_total=D("500"),
                amount_paid=D("0"),
                status="confirmed",
                created_by=ctx.user.id,
            )
        )
    result = await handle_delete(f"customer {master.customer_name}", ctx)
    assert "still has" in result.reply and "outstanding" in result.reply


async def test_deleting_a_transaction_routes_to_undo(ctx: RequestContext) -> None:
    result = await handle_delete("purchase INV-1", ctx)
    assert "🚫" in result.reply
    assert "undo purchase" in result.reply


# --------------------------------------------------------------------
# undo: purchases
# --------------------------------------------------------------------


async def _make_confirmed_purchase(
    session_factory: async_sessionmaker[AsyncSession],
    actor: User,
    master: Master,
    *,
    qty: str = "50",
    landed: str = "160",
    opening_qty: str = "100",
    opening_avg: str = "150",
) -> str:
    """A purchase as `confirm` would leave it: movement applied, average
    updated, audit row written."""
    invoice_no = f"INV-{uuid.uuid4().hex[:6]}"
    async with session_factory() as session, session.begin():
        inventory = (
            await session.execute(
                sa.select(Inventory).where(Inventory.product_id == master.product_id)
            )
        ).scalar_one()
        inventory.qty_on_hand = D(opening_qty)
        inventory.weighted_avg_cost = D(opening_avg)
        await session.flush()

        supplier = (
            await session.execute(sa.select(Supplier).where(Supplier.name == master.supplier_name))
        ).scalar_one()
        header = PurchaseHeader(
            org_id=ORG,
            supplier_id=supplier.id,
            warehouse_id=WAREHOUSE,
            invoice_no=invoice_no,
            invoice_date=datetime.date.today(),
            grand_total=(D(qty) * D(landed)),
            amount_paid=D("0"),
            status=PurchaseStatus.CONFIRMED,
            created_by=actor.id,
        )
        session.add(header)
        await session.flush()
        line = PurchaseLine(
            org_id=ORG,
            purchase_header_id=header.id,
            line_no=1,
            product_id=master.product_id,
            qty=D(qty),
            rate=D(landed),
            line_total=(D(qty) * D(landed)),
            landed_cost_per_unit=D(landed),
        )
        session.add(line)
        await session.flush()
        await InventoryService(session).record_purchase_movement(
            ORG,
            product_id=master.product_id,
            warehouse_id=WAREHOUSE,
            qty=D(qty),
            landed_cost_per_unit=D(landed),
            source_id=line.id,
            created_by=actor.id,
        )
        await AuditService(session).record(
            ORG,
            actor.id,
            action="purchase.confirmed",
            entity_type="purchase_headers",
            entity_id=header.id,
            after_state={"invoice_no": invoice_no},
        )
    return invoice_no


async def test_undo_purchase_reverses_stock_and_average(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """100 KG @ 150 plus 50 KG @ 160 gives 150 @ 153.3333; undoing the
    purchase must return both figures to where they started."""
    async with session_factory() as session, session.begin():
        master = await _master(session, ctx.user)
    invoice = await _make_confirmed_purchase(session_factory, ctx.user, master)

    qty, avg = await _stock(session_factory, master.product_id)
    assert qty == D("150.000")

    result = await handle_undo(f"purchase {invoice}", ctx)
    assert "↩️ Undone" in result.reply

    qty, avg = await _stock(session_factory, master.product_id)
    assert qty == D("100.000")
    assert avg == D("150.0000")


async def test_undo_purchase_keeps_the_record_and_marks_it_cancelled(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The whole point of compensating entries: nothing is erased."""
    async with session_factory() as session, session.begin():
        master = await _master(session, ctx.user)
    invoice = await _make_confirmed_purchase(session_factory, ctx.user, master)
    await handle_undo(f"purchase {invoice}", ctx)

    async with session_factory() as session:
        header = (
            await session.execute(
                sa.select(PurchaseHeader).where(PurchaseHeader.invoice_no == invoice)
            )
        ).scalar_one()
        assert header.status == PurchaseStatus.CANCELLED
        assert header.deleted_at is None  # cancelled, not soft-deleted

        lines = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(PurchaseLine)
                .where(PurchaseLine.purchase_header_id == header.id)
            )
        ).scalar_one()
        assert lines == 1  # original line still present

        movements = (
            (
                await session.execute(
                    sa.text(
                        "SELECT movement_type FROM inventory_movements "
                        "WHERE product_id = :pid ORDER BY created_at"
                    ),
                    {"pid": master.product_id},
                )
            )
            .scalars()
            .all()
        )
    # the original purchase AND its reversal both remain visible
    assert list(movements) == ["purchase", "purchase_return"]


async def test_undo_is_refused_twice(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        master = await _master(session, ctx.user)
    invoice = await _make_confirmed_purchase(session_factory, ctx.user, master)
    await handle_undo(f"purchase {invoice}", ctx)
    second = await handle_undo(f"purchase {invoice}", ctx)

    assert "already been undone" in second.reply
    qty, _ = await _stock(session_factory, master.product_id)
    assert qty == D("100.000")  # not double-reversed


async def test_undo_purchase_refused_when_partly_paid(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Undoing would leave the payment pointing at nothing."""
    async with session_factory() as session, session.begin():
        master = await _master(session, ctx.user)
    invoice = await _make_confirmed_purchase(session_factory, ctx.user, master)
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text("UPDATE purchase_headers SET amount_paid = 100 WHERE invoice_no = :inv"),
            {"inv": invoice},
        )

    result = await handle_undo(f"purchase {invoice}", ctx)
    assert "already paid" in result.reply
    qty, _ = await _stock(session_factory, master.product_id)
    assert qty == D("150.000")  # untouched


async def test_undo_outside_the_window_is_refused(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        master = await _master(session, ctx.user)
    invoice = await _make_confirmed_purchase(session_factory, ctx.user, master)
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text(
                "UPDATE audit_logs SET created_at = now() - interval '48 hours' "
                "WHERE action = 'purchase.confirmed'"
            )
        )

    result = await handle_undo(f"purchase {invoice}", ctx)
    assert "can no longer be undone" in result.reply


async def test_undo_window_follows_settings(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from backend.api.commands.settings_commands import handle_settings

    await handle_settings("undo_window_hours 72", ctx)
    async with session_factory() as session, session.begin():
        master = await _master(session, ctx.user)
    invoice = await _make_confirmed_purchase(session_factory, ctx.user, master)
    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text(
                "UPDATE audit_logs SET created_at = now() - interval '48 hours' "
                "WHERE action = 'purchase.confirmed'"
            )
        )

    result = await handle_undo(f"purchase {invoice}", ctx)
    assert "↩️ Undone" in result.reply  # 48h is inside a 72h window


# --------------------------------------------------------------------
# undo: money entries and the bare form
# --------------------------------------------------------------------


async def test_bare_undo_reverses_the_most_recent_entry(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await handle_expense("transport 1500 cash loading charges", ctx)
    assert await _cash(session_factory) == D("-1500.00")

    result = await handle_undo("", ctx)
    assert "↩️ Undone" in result.reply
    assert await _cash(session_factory) == D("0.00")


async def test_undo_soft_deletes_the_expense_rather_than_erasing_it(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await handle_expense("transport 1500 cash", ctx)
    await handle_undo("", ctx)
    async with session_factory() as session:
        row = (
            await session.execute(
                sa.text("SELECT amount, deleted_at FROM expenses WHERE org_id = :org"),
                {"org": ORG},
            )
        ).one()
    assert row[0] == D("1500.00")  # still there
    assert row[1] is not None  # marked deleted


async def test_bare_undo_reverses_income_too(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await handle_income("commission 800 bank", ctx)
    result = await handle_undo("", ctx)
    assert "↩️ Undone" in result.reply
    async with session_factory() as session:
        bank = await LedgerRepository(session).balance(ORG, "bank")
    assert bank == D("0.00")


async def test_bare_undo_takes_the_latest_of_several(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await handle_expense("transport 500 cash", ctx)
    await handle_expense("packing 300 cash", ctx)
    assert await _cash(session_factory) == D("-800.00")

    await handle_undo("", ctx)
    # the 300 goes back, the 500 stays
    assert await _cash(session_factory) == D("-500.00")

    await handle_undo("", ctx)
    assert await _cash(session_factory) == D("0.00")


async def test_bare_undo_with_nothing_to_undo(ctx: RequestContext) -> None:
    result = await handle_undo("", ctx)
    assert "not found" in result.reply


# --------------------------------------------------------------------
# permissions
# --------------------------------------------------------------------


async def test_staff_cannot_undo_someone_elses_entry(
    ctx: RequestContext,
    staff_ctx: RequestContext,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        master = await _master(session, ctx.user)
    invoice = await _make_confirmed_purchase(session_factory, ctx.user, master)

    result = await handle_undo(f"purchase {invoice}", staff_ctx)
    assert "someone else" in result.reply
    qty, _ = await _stock(session_factory, master.product_id)
    assert qty == D("150.000")


async def test_staff_can_undo_their_own_entry(
    staff_ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await handle_expense("packing 200 cash", staff_ctx)
    result = await handle_undo("", staff_ctx)
    assert "↩️ Undone" in result.reply


async def test_edit_and_delete_are_owner_only_undo_is_staff() -> None:
    from backend.api.whatsapp_commands import COMMAND_REGISTRY
    from backend.models.enums import UserRole

    assert COMMAND_REGISTRY["edit"].min_role == UserRole.OWNER
    assert COMMAND_REGISTRY["delete"].min_role == UserRole.OWNER
    assert COMMAND_REGISTRY["undo"].min_role == UserRole.STAFF


# --------------------------------------------------------------------
# the books stay balanced
# --------------------------------------------------------------------


async def test_undo_posts_balanced_compensating_journals(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        master = await _master(session, ctx.user)
    invoice = await _make_confirmed_purchase(session_factory, ctx.user, master)
    await handle_undo(f"purchase {invoice}", ctx)
    await handle_expense("transport 400 cash", ctx)
    await handle_undo("", ctx)

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
        undo_journals = (
            await session.execute(
                sa.text(
                    "SELECT count(*) FROM journal WHERE org_id = :org AND source_type LIKE '%_undo'"
                ),
                {"org": ORG},
            )
        ).scalar_one()
    assert unbalanced == 0
    assert undo_journals == 2


async def test_undo_is_itself_audited_separately(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """docs/04_Purchases.md §8: the undo gets its own audit row, distinct
    from the original confirm."""
    async with session_factory() as session, session.begin():
        master = await _master(session, ctx.user)
    invoice = await _make_confirmed_purchase(session_factory, ctx.user, master)
    await handle_undo(f"purchase {invoice}", ctx)

    async with session_factory() as session:
        actions = (
            (
                await session.execute(
                    sa.text(
                        "SELECT action FROM audit_logs WHERE org_id = :org "
                        "AND entity_type = 'purchase_headers' ORDER BY created_at"
                    ),
                    {"org": ORG},
                )
            )
            .scalars()
            .all()
        )
    assert list(actions) == ["purchase.confirmed", "purchase.confirmed.undone"]


async def test_movements_still_reconcile_after_undo(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """CLAUDE.md's standing invariant: qty_on_hand equals the signed sum
    of movements, undo included."""
    async with session_factory() as session, session.begin():
        master = await _master(session, ctx.user)
    invoice = await _make_confirmed_purchase(
        session_factory, ctx.user, master, opening_qty="0", opening_avg="0"
    )
    await handle_undo(f"purchase {invoice}", ctx)

    async with session_factory() as session:
        movement_sum = (
            await session.execute(
                sa.text(
                    "SELECT coalesce(sum(qty_delta), 0) FROM inventory_movements "
                    "WHERE product_id = :pid"
                ),
                {"pid": master.product_id},
            )
        ).scalar_one()
    qty, _ = await _stock(session_factory, master.product_id)
    assert qty == movement_sum


async def test_undo_sale_returns_stock_and_cancels_the_sale(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        master = await _master(session, ctx.user, qty="100")
        customer = (
            await session.execute(sa.select(Customer).where(Customer.name == master.customer_name))
        ).scalar_one()
        sale = SalesHeader(
            org_id=ORG,
            customer_id=customer.id,
            warehouse_id=WAREHOUSE,
            sale_date=datetime.date.today(),
            payment_type=SalePaymentType.CREDIT,
            subtotal=D("2000"),
            grand_total=D("2000"),
            amount_paid=D("0"),
            status="confirmed",
            created_by=ctx.user.id,
        )
        session.add(sale)
        await session.flush()
        line = SalesLine(
            org_id=ORG,
            sales_header_id=sale.id,
            line_no=1,
            product_id=master.product_id,
            qty=D("10"),
            rate=D("200"),
            line_total=D("2000"),
            avg_cost_at_sale_time=D("100"),
        )
        session.add(line)
        await session.flush()
        await InventoryService(session).record_sale_movement(
            ORG,
            product_id=master.product_id,
            product_code=master.code,
            warehouse_id=WAREHOUSE,
            qty=D("10"),
            source_id=line.id,
            created_by=ctx.user.id,
        )
        await AuditService(session).record(
            ORG,
            ctx.user.id,
            # what sales_service actually writes. This fixture said
            # "sale.confirmed" and so did the undo registry, so the test
            # passed while every real sale -- recorded as "sale.created"
            # -- was unundoable.
            action="sale.created",
            entity_type="sales_headers",
            entity_id=sale.id,
            after_state={"grand_total": "2000"},
        )
        sale_id = sale.id

    qty, _ = await _stock(session_factory, master.product_id)
    assert qty == D("90.000")

    result = await handle_undo(f"sale {master.customer_name}", ctx)
    assert "↩️ Undone" in result.reply

    qty, avg = await _stock(session_factory, master.product_id)
    assert qty == D("100.000")
    assert avg == D("100.0000")  # sales never moved it, nor does undoing one

    async with session_factory() as session:
        status = (
            await session.execute(sa.select(SalesHeader.status).where(SalesHeader.id == sale_id))
        ).scalar_one()
    assert status == "cancelled"
