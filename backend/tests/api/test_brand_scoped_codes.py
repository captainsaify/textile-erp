"""Suppliers reuse short codes across brands, so a product code is only
unique within a brand -- docs/03_Inventory.md (multi-brand via brand_id).
Everything here guards the consequences of that: the DB must allow the
collision, and every lookup must stop assuming a code means one product.
"""

from __future__ import annotations

import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import RequestContext
from backend.api.commands.stock_commands import handle_stock
from backend.models import Brand, Inventory, Product, User
from backend.repositories.product_repository import ProductRepository
from backend.tests.conftest import (
    SEEDED_KG_UNIT_ID,
    SEEDED_MAIN_WAREHOUSE_ID,
    SEEDED_ORG_ID,
    SEEDED_TEXTILE_TYPE_ID,
    purge_business_rows,
)

ORG = uuid.UUID(SEEDED_ORG_ID)
SHARED_CODE = "VVP"


@pytest.fixture(autouse=True)
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    await purge_business_rows(session_factory)


@pytest.fixture
def ctx(staff_user: User, session_factory: async_sessionmaker[AsyncSession]) -> RequestContext:
    return RequestContext(user=staff_user, session_factory=session_factory)


async def _make_brand(session: AsyncSession, actor: User, name: str) -> Brand:
    brand = Brand(org_id=ORG, name=name)
    session.add(brand)
    await session.flush()
    return brand


async def _make_product(
    session: AsyncSession,
    actor: User,
    *,
    code: str,
    description: str,
    brand_id: uuid.UUID | None,
    qty: str = "0",
) -> Product:
    product = Product(
        org_id=ORG,
        product_type_id=uuid.UUID(SEEDED_TEXTILE_TYPE_ID),
        code=code,
        description=description,
        brand_id=brand_id,
        unit_id=uuid.UUID(SEEDED_KG_UNIT_ID),
        created_by=actor.id,
    )
    session.add(product)
    await session.flush()
    session.add(
        Inventory(
            org_id=ORG,
            product_id=product.id,
            warehouse_id=uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID),
            qty_on_hand=decimal.Decimal(qty),
            weighted_avg_cost=decimal.Decimal("100"),
        )
    )
    await session.flush()
    return product


@pytest.fixture
async def two_brands(
    staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[tuple[Product, Product]]:
    """The same code stocked under two brands -- the situation the old
    unique index made impossible."""
    async with session_factory() as session, session.begin():
        nike = await _make_brand(session, staff_user, "Nike")
        puma = await _make_brand(session, staff_user, "Puma")
        first = await _make_product(
            session,
            staff_user,
            code=SHARED_CODE,
            description="Golden Velvet Pant",
            brand_id=nike.id,
            qty="800",
        )
        second = await _make_product(
            session,
            staff_user,
            code=SHARED_CODE,
            description="Corduroy Pant",
            brand_id=puma.id,
            qty="1520",
        )
        await session.refresh(first)
        await session.refresh(second)
        ids = (first.id, second.id)
    async with session_factory() as session:
        loaded = [
            (await session.get(Product, ids[0])),
            (await session.get(Product, ids[1])),
        ]
        assert loaded[0] is not None and loaded[1] is not None
        yield loaded[0], loaded[1]


async def test_same_code_allowed_under_different_brands(
    two_brands: tuple[Product, Product],
) -> None:
    first, second = two_brands
    assert first.code == second.code == SHARED_CODE
    assert first.brand_id != second.brand_id


async def test_same_code_still_rejected_within_one_brand(
    staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        async with session.begin():
            brand = await _make_brand(session, staff_user, "Reebok")
            await _make_product(
                session, staff_user, code="DUP", description="First", brand_id=brand.id
            )
        with pytest.raises(IntegrityError):
            async with session.begin():
                await _make_product(
                    session, staff_user, code="DUP", description="Second", brand_id=brand.id
                )


async def test_brandless_duplicate_still_rejected(
    staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """NULLS NOT DISTINCT: without it Postgres treats each NULL brand as
    unique and the same unbranded code slips in twice."""
    async with session_factory() as session:
        async with session.begin():
            await _make_product(
                session, staff_user, code="NOBRAND", description="First", brand_id=None
            )
        with pytest.raises(IntegrityError):
            async with session.begin():
                await _make_product(
                    session, staff_user, code="NOBRAND", description="Second", brand_id=None
                )


async def test_get_by_code_returns_none_when_ambiguous(
    two_brands: tuple[Product, Product], session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Used to raise MultipleResultsFound and take the request down."""
    async with session_factory() as session:
        repo = ProductRepository(session)
        assert await repo.get_by_code(ORG, SHARED_CODE) is None
        assert len(await repo.list_by_code(ORG, SHARED_CODE)) == 2


async def test_get_by_code_picks_the_brand_when_given_one(
    two_brands: tuple[Product, Product], session_factory: async_sessionmaker[AsyncSession]
) -> None:
    first, second = two_brands
    async with session_factory() as session:
        repo = ProductRepository(session)
        found = await repo.get_by_code(ORG, SHARED_CODE, first.brand_id)
        assert found is not None and found.id == first.id
        other = await repo.get_by_code(ORG, SHARED_CODE, second.brand_id)
        assert other is not None and other.id == second.id


async def test_unknown_brand_does_not_fall_through_to_another_brand(
    two_brands: tuple[Product, Product], session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        repo = ProductRepository(session)
        assert await repo.get_by_code(ORG, SHARED_CODE, uuid.uuid4()) is None


async def test_stock_command_lists_every_brand_carrying_the_code(
    two_brands: tuple[Product, Product], ctx: RequestContext
) -> None:
    """'stock VVP' used to answer 'not found' once two brands had it."""
    result = await handle_stock(SHARED_CODE, ctx)
    assert "not found" not in result.reply
    assert "2 brands" in result.reply
    assert "Nike" in result.reply and "Puma" in result.reply
    assert "Golden Velvet Pant" in result.reply
    assert "Corduroy Pant" in result.reply
    # each brand's own stock, not a merged figure
    assert "800" in result.reply and "1520" in result.reply


async def test_stock_command_unchanged_for_an_unambiguous_code(
    staff_user: User, session_factory: async_sessionmaker[AsyncSession], ctx: RequestContext
) -> None:
    async with session_factory() as session, session.begin():
        brand = await _make_brand(session, staff_user, "Solo")
        await _make_product(
            session,
            staff_user,
            code="ONLYONE",
            description="Single Brand Item",
            brand_id=brand.id,
            qty="42",
        )
    result = await handle_stock("ONLYONE", ctx)
    assert "brands:" not in result.reply
    assert "Single Brand Item" in result.reply
    assert "42" in result.reply


async def test_purchase_line_keeps_the_description_as_printed(
    staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The sheet's wording is the audit trail back to the original
    invoice; products.description is the canonical name and drifts."""
    async with session_factory() as session:
        columns = (
            await session.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'purchase_lines' AND column_name = 'description'"
                )
            )
        ).scalars()
        assert list(columns) == ["description"]
