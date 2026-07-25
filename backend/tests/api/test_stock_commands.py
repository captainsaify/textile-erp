"""stock / stock CODE / stock low / stock negative / search --
docs/08_WhatsApp.md #stock, #stock-code, #search."""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import RequestContext
from backend.api.commands.stock_commands import handle_search, handle_stock
from backend.models import Inventory, InventoryMovement, Product, Supplier, User
from backend.models.enums import MovementType
from backend.tests.conftest import (
    SEEDED_KG_UNIT_ID,
    SEEDED_MAIN_WAREHOUSE_ID,
    SEEDED_ORG_ID,
    SEEDED_TEXTILE_TYPE_ID,
)

ORG = uuid.UUID(SEEDED_ORG_ID)


@pytest.fixture
def ctx(staff_user: User, session_factory: async_sessionmaker[AsyncSession]) -> RequestContext:
    return RequestContext(user=staff_user, session_factory=session_factory)


@pytest.fixture
async def trp_product(
    staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[Product]:
    async with session_factory() as session:
        product = Product(
            org_id=ORG,
            product_type_id=uuid.UUID(SEEDED_TEXTILE_TYPE_ID),
            code="TRP",
            description="Trouser Poly",
            unit_id=uuid.UUID(SEEDED_KG_UNIT_ID),
            reorder_level=decimal.Decimal("15"),
            created_by=staff_user.id,
        )
        session.add(product)
        await session.flush()
        session.add(
            Inventory(
                org_id=ORG,
                product_id=product.id,
                warehouse_id=uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID),
                qty_on_hand=decimal.Decimal("130.000"),
                weighted_avg_cost=decimal.Decimal("153.2100"),
            )
        )
        session.add(
            InventoryMovement(
                org_id=ORG,
                product_id=product.id,
                warehouse_id=uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID),
                movement_type=MovementType.SALE,
                qty_delta=decimal.Decimal("-20.000"),
                unit_cost=decimal.Decimal("153.2100"),
                resulting_qty_on_hand=decimal.Decimal("130.000"),
                resulting_avg_cost=decimal.Decimal("153.2100"),
                source_type="sales_line",
                source_id=uuid.uuid4(),
                created_at=datetime.datetime(2026, 7, 24, 10, 0, tzinfo=datetime.UTC),
                created_by=staff_user.id,
            )
        )
        await session.commit()
        await session.refresh(product)
    yield product
    async with session_factory() as session:
        await session.execute(
            sa.text("DELETE FROM inventory_movements WHERE product_id = :id"),
            {"id": product.id},
        )
        await session.execute(
            sa.text("DELETE FROM inventory WHERE product_id = :id"), {"id": product.id}
        )
        await session.execute(sa.text("DELETE FROM products WHERE id = :id"), {"id": product.id})
        await session.commit()


@pytest.fixture
async def supplier(
    staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[Supplier]:
    async with session_factory() as session:
        row = Supplier(org_id=ORG, name="Shree Textiles", created_by=staff_user.id)
        session.add(row)
        await session.commit()
        await session.refresh(row)
    yield row
    async with session_factory() as session:
        await session.execute(sa.text("DELETE FROM suppliers WHERE id = :id"), {"id": row.id})
        await session.commit()


async def test_stock_summary(ctx: RequestContext, trp_product: Product) -> None:
    result = await handle_stock("", ctx)
    assert "📦 Stock summary (1 active products)" in result.reply
    # 130.000 * 153.2100 = 19,917.30
    assert "Total value: ₹19,917.30" in result.reply
    assert "Low stock: none" in result.reply


async def test_stock_detail(ctx: RequestContext, trp_product: Product) -> None:
    result = await handle_stock("trp", ctx)
    assert "📦 TRP — Trouser Poly" in result.reply
    assert "On hand: 130.0 KG" in result.reply
    assert "Avg cost: ₹153.21/KG" in result.reply
    assert "Stock value: ₹19,917.30" in result.reply
    assert "Reorder level: 15.0 KG" in result.reply
    assert "Last movement: sale -20.0 KG (24-07-2026)" in result.reply


async def test_stock_unknown_code_suggests(ctx: RequestContext, trp_product: Product) -> None:
    result = await handle_stock("TRQ", ctx)
    assert "Product 'TRQ' not found." in result.reply
    assert "Did you mean TRP" in result.reply


async def test_stock_low_and_negative(
    ctx: RequestContext,
    trp_product: Product,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await session.execute(
            sa.text("UPDATE inventory SET qty_on_hand = '8.000' WHERE product_id = :id"),
            {"id": trp_product.id},
        )
        await session.commit()
    result = await handle_stock("low", ctx)
    assert "📉 Low stock (1 items):" in result.reply
    assert "• TRP — 8.0 KG left (reorder at 15.0 KG)" in result.reply

    result = await handle_stock("negative", ctx)
    assert result.reply == "✅ No negative stock."

    async with session_factory() as session:
        await session.execute(
            sa.text("UPDATE inventory SET qty_on_hand = '-3.000' WHERE product_id = :id"),
            {"id": trp_product.id},
        )
        await session.commit()
    result = await handle_stock("negative", ctx)
    assert "⚠️ Negative stock (1 items):" in result.reply
    assert "• TRP — -3.0 KG (⚠️ negative stock)" in result.reply

    summary = await handle_stock("", ctx)
    assert "Negative stock: 1 item ⚠️" in summary.reply


async def test_search_finds_products_and_suppliers(
    ctx: RequestContext, trp_product: Product, supplier: Supplier
) -> None:
    result = await handle_search("trp", ctx)
    assert "🔎 Results for 'trp':" in result.reply
    assert "• TRP — Trouser Poly (130.0 KG on hand)" in result.reply

    result = await handle_search("shree", ctx)
    assert "Suppliers:" in result.reply
    assert "• Shree Textiles" in result.reply

    result = await handle_search("zzzzz", ctx)
    assert result.reply == "No matches for 'zzzzz'."

    result = await handle_search("", ctx)
    assert result.reply == "Usage: search <text>"
