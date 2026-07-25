"""Purchase wave: grammar, allocation math (docs/04_Purchases.md §4),
weighted average (docs/03_Inventory.md §2 worked example), the session
flow (create supplier/product, corrections, CONFIRM), and duplicate
detection layers (docs/04_Purchases.md §6)."""

from __future__ import annotations

import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import CommandResult, RequestContext
from backend.api.commands.purchase_commands import (
    handle_purchase,
    handle_purchase_session_reply,
    parse_purchase_command,
)
from backend.core.exceptions import ValidationError
from backend.models import User
from backend.services.inventory_service import InventoryService
from backend.services.purchase_service import allocate
from backend.services.session_service import SessionService
from backend.tests.conftest import (
    SEEDED_MAIN_WAREHOUSE_ID,
    SEEDED_ORG_ID,
    purge_business_rows,
)

D = decimal.Decimal
ORG = uuid.UUID(SEEDED_ORG_ID)

PURCHASE_TEXT = (
    "Supplier: Shree Textiles Invoice: INV-4521 Date: 24-07-2026\n"
    "TRP 100 150\n"
    "MJP 40 210\n"
    "Freight: 500\n"
    "Other: 100"
)


@pytest.fixture(autouse=True)
async def clean_tables(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    yield
    await purge_business_rows(session_factory)


@pytest.fixture
def ctx(staff_user: User, session_factory: async_sessionmaker[AsyncSession]) -> RequestContext:
    return RequestContext(user=staff_user, session_factory=session_factory)


async def _session_reply(text: str, ctx: RequestContext) -> CommandResult:
    state = await SessionService(ctx.session_factory).get(ctx.user.org_id, ctx.user.id)
    return await handle_purchase_session_reply(text, ctx, state)


def test_allocate_sums_exactly_with_remainder_on_largest_line() -> None:
    shares = allocate(D("500"), [D("100"), D("40")])
    assert shares == [D("357.14"), D("142.86")]
    assert sum(shares) == D("500")

    # a split that doesn't divide evenly: remainder lands on largest weight
    shares = allocate(D("100"), [D("1"), D("1"), D("1")])
    assert sum(shares) == D("100")
    assert shares[2] - shares[0] in {D("0"), D("0.01"), D("0.02")}

    assert allocate(D("0"), [D("5")]) == [D("0")]


def test_parse_purchase_command_full_grammar() -> None:
    draft = parse_purchase_command(PURCHASE_TEXT + "\nTotal: 24000")
    assert draft.supplier_name == "Shree Textiles"
    assert draft.invoice_no == "INV-4521"
    assert draft.invoice_date.isoformat() == "2026-07-24"
    assert [(line.code, line.qty, line.rate) for line in draft.lines] == [
        ("TRP", D("100"), D("150")),
        ("MJP", D("40"), D("210")),
    ]
    assert draft.freight == D("500")
    assert draft.other_charges == D("100")
    assert draft.declared_total == D("24000")
    assert draft.subtotal == D("23400.00")
    assert draft.grand_total == D("24000.00")

    with pytest.raises(ValidationError, match="Couldn't read the first line"):
        parse_purchase_command("Supplier only nonsense")
    with pytest.raises(ValidationError, match="item line"):
        parse_purchase_command("Supplier: X Invoice: 1 Date: 24-07-2026\nTRP one hundred 150")


async def test_weighted_average_worked_example(
    staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """docs/03_Inventory.md §2: opening 100 @ 150, +50 @ 160 -> 153.33;
    sale to 120 (avg unchanged); +20 @ 140 -> 151.43."""
    from backend.models import Inventory, Product, ProductType

    async with session_factory() as session:
        product_type = (
            await session.execute(sa.select(ProductType).where(ProductType.org_id == ORG))
        ).scalar_one()
        product = Product(
            org_id=ORG,
            product_type_id=product_type.id,
            code="WAVG",
            description="Worked Example",
            unit_id=product_type.default_unit_id,
            created_by=staff_user.id,
        )
        session.add(product)
        await session.flush()
        session.add(
            Inventory(
                org_id=ORG,
                product_id=product.id,
                warehouse_id=uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID),
                qty_on_hand=D("100"),
                weighted_avg_cost=D("150"),
            )
        )
        await session.commit()
        product_id = product.id

    async with session_factory() as session:
        async with session.begin():
            movement = await InventoryService(session).record_purchase_movement(
                ORG,
                product_id=product_id,
                warehouse_id=uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID),
                qty=D("50"),
                landed_cost_per_unit=D("160"),
                source_id=uuid.uuid4(),
                created_by=staff_user.id,
            )
        assert movement.resulting_qty_on_hand == D("150.000")
        assert movement.resulting_avg_cost == D("153.3333")

    async with session_factory() as session:  # simulate the sale: qty down, avg untouched
        await session.execute(
            sa.text("UPDATE inventory SET qty_on_hand = 120 WHERE product_id = :id"),
            {"id": product_id},
        )
        await session.commit()

    async with session_factory() as session:
        async with session.begin():
            movement = await InventoryService(session).record_purchase_movement(
                ORG,
                product_id=product_id,
                warehouse_id=uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID),
                qty=D("20"),
                landed_cost_per_unit=D("140"),
                source_id=uuid.uuid4(),
                created_by=staff_user.id,
            )
        assert movement.resulting_qty_on_hand == D("140.000")
        # doc shows 151.43 (2dp); at 4dp storage precision the stored
        # 153.3333 average yields (120*153.3333 + 20*140)/140 = 151.4285
        assert movement.resulting_avg_cost == D("151.4285")


async def test_full_purchase_session_flow(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    result = await handle_purchase(PURCHASE_TEXT, ctx)
    assert "Purchase draft ready — Shree Textiles, INV-4521" in result.reply
    assert "unknown product" in result.reply
    assert "Supplier 'Shree Textiles' not found" in result.reply

    result = await _session_reply("create supplier", ctx)
    assert "Supplier 'Shree Textiles' not found" not in result.reply

    result = await _session_reply("create product TRP Trouser Poly", ctx)
    assert "TRP  100.0 KG × ₹150.00 = ₹15,000.00" in result.reply
    result = await _session_reply("create product MJP Micro Jogging Pants Fabric", ctx)
    assert "Reply CONFIRM to save" in result.reply

    # correction before confirming
    result = await _session_reply("line 2 qty 45", ctx)
    assert "MJP  45.0 KG × ₹210.00 = ₹9,450.00" in result.reply

    result = await _session_reply("line 2 qty 40", ctx)
    result = await _session_reply("CONFIRM", ctx)
    assert "✅ Purchase confirmed — Shree Textiles, INV-4521" in result.reply
    assert "Grand total: ₹24,000.00" in result.reply
    assert "• TRP now 100.0 KG" in result.reply

    async with session_factory() as session:
        header = (
            await session.execute(
                sa.text("SELECT status::text, subtotal, grand_total, freight FROM purchase_headers")
            )
        ).one()
        assert header.status == "confirmed"
        assert header.subtotal == D("23400.00")
        assert header.grand_total == D("24000.00")

        lines = (
            await session.execute(
                sa.text(
                    "SELECT line_no, freight_allocated, landed_cost_per_unit "
                    "FROM purchase_lines ORDER BY line_no"
                )
            )
        ).all()
        # freight by weight: 500 * 100/140, 500 * 40/140; other by value
        assert lines[0].freight_allocated == D("357.14")
        assert lines[1].freight_allocated == D("142.86")
        # landed = (15000 + 357.14 + 64.10) / 100 and (8400 + 142.86 + 35.90) / 40
        assert lines[0].landed_cost_per_unit == D("154.2124")
        assert lines[1].landed_cost_per_unit == D("214.4690")

        inventory = (
            await session.execute(
                sa.text(
                    "SELECT p.code, i.qty_on_hand, i.weighted_avg_cost FROM inventory i "
                    "JOIN products p ON p.id = i.product_id ORDER BY p.code"
                )
            )
        ).all()
        assert [(r.code, r.qty_on_hand, r.weighted_avg_cost) for r in inventory] == [
            ("MJP", D("40.000"), D("214.4690")),
            ("TRP", D("100.000"), D("154.2124")),
        ]

        debits, credits = (
            await session.execute(sa.text("SELECT SUM(debit), SUM(credit) FROM journal_lines"))
        ).one()
        assert debits == credits == D("24000.00")

        movement_count = (
            await session.execute(sa.text("SELECT count(*) FROM inventory_movements"))
        ).scalar_one()
        assert movement_count == 2

    # session cleared after confirm: next non-command gets unknown-command...
    state = await SessionService(ctx.session_factory).get(ctx.user.org_id, ctx.user.id)
    assert state.is_idle


async def test_exact_duplicate_blocked(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await handle_purchase(PURCHASE_TEXT, ctx)
    await _session_reply("create supplier", ctx)
    await _session_reply("create product TRP Trouser Poly", ctx)
    await _session_reply("create product MJP Micro Jogging Pants Fabric", ctx)
    await _session_reply("confirm", ctx)

    await handle_purchase(PURCHASE_TEXT, ctx)
    result = await _session_reply("confirm", ctx)
    assert "❌ Invoice INV-4521 from Shree Textiles is already recorded" in result.reply
    await _session_reply("discard", ctx)


async def test_fuzzy_duplicate_warns_and_owner_overrides(
    ctx: RequestContext,
    owner_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await handle_purchase(PURCHASE_TEXT, ctx)
    await _session_reply("create supplier", ctx)
    await _session_reply("create product TRP Trouser Poly", ctx)
    await _session_reply("create product MJP Micro Jogging Pants Fabric", ctx)
    await _session_reply("confirm", ctx)

    # same lines/totals, slightly different invoice number -> 2-of-3 signals
    second = PURCHASE_TEXT.replace("INV-4521", "INV-4521A")
    await handle_purchase(second, ctx)
    result = await _session_reply("confirm", ctx)
    assert "looks similar to a purchase already recorded" in result.reply
    assert "Only an owner can confirm" in result.reply  # staff cannot override

    override = await _session_reply("confirm anyway", ctx)
    assert "Only an owner can override" in override.reply
    await _session_reply("discard", ctx)

    owner_ctx = RequestContext(user=owner_user, session_factory=ctx.session_factory)
    await handle_purchase(second, owner_ctx)
    result = await _session_reply("confirm", owner_ctx)
    assert "confirm anyway" in result.reply
    result = await _session_reply("confirm anyway", owner_ctx)
    assert "✅ Purchase confirmed" in result.reply


async def test_total_mismatch_resolution(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await handle_purchase(PURCHASE_TEXT + "\nTotal: 24150", ctx)
    await _session_reply("create supplier", ctx)
    await _session_reply("create product TRP Trouser Poly", ctx)
    await _session_reply("create product MJP Micro Jogging Pants Fabric", ctx)
    result = await _session_reply("confirm", ctx)
    assert "invoice shows a total of" in result.reply

    result = await _session_reply("use invoice total", ctx)
    result = await _session_reply("confirm", ctx)
    assert "✅ Purchase confirmed" in result.reply

    async with session_factory() as session:
        header = (
            await session.execute(
                sa.text("SELECT grand_total, other_charges, notes FROM purchase_headers")
            )
        ).one()
        assert header.grand_total == D("24150.00")
        assert header.other_charges == D("250.00")  # 100 + 150 reconciliation
        assert header.notes == "reconciled against declared invoice total"
