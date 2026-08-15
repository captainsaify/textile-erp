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
from backend.api.interactive import ListMenu
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


async def test_tapping_a_brand_on_the_menu_answers_with_that_brand(
    two_brands: tuple[Product, Product], ctx: RequestContext
) -> None:
    """The menu sends its own choice id back as text, so `stock VVP`
    then a tap arrives as `stock VVP Puma`. That used to be looked up as
    a product *coded* "VVP PUMA" and answered "not found" -- making the
    Pick brand menu unusable for every code two brands share."""
    listed = await handle_stock(SHARED_CODE, ctx)
    assert isinstance(listed.interactive, ListMenu)
    # unordered: list_by_code orders by created_at, and rows written in one
    # transaction share a single now() -- so which brand comes first is not
    # decided by anything here
    tapped = {c.id for c in listed.interactive.sections[0].rows}
    assert tapped == {f"stock {SHARED_CODE} Nike", f"stock {SHARED_CODE} Puma"}

    # exactly what the dispatcher hands back after the tap, minus the keyword
    result = await handle_stock(f"{SHARED_CODE} Puma", ctx)
    assert "not found" not in result.reply
    assert "Corduroy Pant" in result.reply  # the Puma one
    assert "Golden Velvet Pant" not in result.reply
    assert "1520" in result.reply
    assert "brands:" not in result.reply  # narrowed, not re-listed


async def test_a_brand_that_does_not_carry_the_code_says_who_does(
    two_brands: tuple[Product, Product], ctx: RequestContext
) -> None:
    """Never fall through to another brand's stock -- a wrong answer,
    not a near miss (ProductRepository.get_by_code's rule)."""
    result = await handle_stock(f"{SHARED_CODE} Adidas", ctx)
    assert "Golden Velvet Pant" not in result.reply
    assert "Corduroy Pant" not in result.reply
    assert "not stocked under 'Adidas'" in result.reply
    assert "Nike" in result.reply and "Puma" in result.reply


async def test_suggestions_do_not_repeat_a_code_once_per_brand(
    two_brands: tuple[Product, Product], ctx: RequestContext
) -> None:
    """'Did you mean 55D, 55D?' -- the same code under two brands is one
    suggestion."""
    result = await handle_stock("VV", ctx)
    assert "not found" in result.reply
    assert result.reply.count(SHARED_CODE) == 1


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


# --------------------------------------------------------------------
# selling a code two brands share
# --------------------------------------------------------------------


async def test_a_sale_asks_which_brand_rather_than_calling_the_code_unknown(
    ctx: RequestContext,
    two_brands: tuple[Product, Product],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """VVP names a product under Nike and a different one under Puma.
    The sale used to report it as an unknown product and suggest adding
    it "via a purchase first" -- sending someone to fix a catalogue that
    was already right."""
    from backend.api.commands.sale_commands import handle_sale, handle_sale_session_reply
    from backend.api.interactive import Buttons
    from backend.models import Customer
    from backend.services.session_service import SessionService

    async with session_factory() as session, session.begin():
        session.add(Customer(org_id=ORG, name="Ravi Traders", created_by=ctx.user.id))

    asked = await handle_sale("Customer: Ravi Traders cash\nVVP 100 150", ctx)

    assert "Unknown products" not in asked.reply
    assert "Nike" in asked.reply and "Puma" in asked.reply
    assert isinstance(asked.interactive, Buttons)
    assert [c.id for c in asked.interactive.choices] == ["brand Nike", "brand Puma"]

    # answering it sells that brand's product, and only that one
    state = await SessionService(session_factory).get(ORG, ctx.user.id)
    answered = await handle_sale_session_reply("brand Puma", ctx, state)
    assert "Sale draft" in answered.reply  # resolved, and now previewable

    state = await SessionService(session_factory).get(ORG, ctx.user.id)
    recorded = await handle_sale_session_reply("confirm", ctx, state)
    assert "Sale recorded" in recorded.reply
    async with session_factory() as session:
        sold = (
            await session.execute(
                sa.select(Inventory.qty_on_hand).where(Inventory.product_id == two_brands[1].id)
            )
        ).scalar_one()
        untouched = (
            await session.execute(
                sa.select(Inventory.qty_on_hand).where(Inventory.product_id == two_brands[0].id)
            )
        ).scalar_one()
    assert sold == decimal.Decimal("1420")  # 1520 - 100, the Puma one
    assert untouched == decimal.Decimal("800")


async def test_a_brand_that_does_not_carry_the_code_is_refused(
    ctx: RequestContext,
    two_brands: tuple[Product, Product],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from backend.api.commands.sale_commands import handle_sale, handle_sale_session_reply
    from backend.models import Customer
    from backend.services.session_service import SessionService

    async with session_factory() as session, session.begin():
        session.add(Customer(org_id=ORG, name="Ravi Traders", created_by=ctx.user.id))
    await handle_sale("Customer: Ravi Traders cash\nVVP 100 150", ctx)

    state = await SessionService(session_factory).get(ORG, ctx.user.id)
    result = await handle_sale_session_reply("Adidas", ctx, state)

    assert "isn't one of VVP's brands" in result.reply
    assert result.interactive is not None  # and the question is asked again
