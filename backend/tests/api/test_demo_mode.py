"""Demonstrating the system without writing to the partners' books --
docs/29_DemoMode.md.

The property under test is not "demo mode works". It is that **a demo
message cannot reach the real business**, which is a claim about
`org_id` scoping rather than about any flag. So the tests below record
real transactions in demo mode and then assert the real org is
byte-for-byte unchanged.
"""

from __future__ import annotations

import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import RequestContext
from backend.api.commands.demo_commands import (
    handle_demo,
    handle_login,
    handle_reset_demo,
    is_demo,
    leave,
)
from backend.models import Customer, ProductType, Supplier, Unit, User, Warehouse
from backend.services.demo_service import DEMO_ORG_ID, DemoService, as_demo
from backend.tests.conftest import SEEDED_ORG_ID, purge_business_rows

D = decimal.Decimal
ORG = uuid.UUID(SEEDED_ORG_ID)


@pytest.fixture(autouse=True)
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    async with session_factory() as session:
        await DemoService(session).reset()
        await session.execute(
            sa.text("DELETE FROM ocr_templates WHERE org_id = :org").bindparams(org=DEMO_ORG_ID)
        )
        for table in ("warehouses", "product_types", "units", "organizations"):
            await session.execute(
                sa.text(
                    f"DELETE FROM {table} WHERE {'id' if table == 'organizations' else 'org_id'}"
                    " = :org"
                ).bindparams(org=DEMO_ORG_ID)
            )
        await session.commit()
    await purge_business_rows(session_factory)


@pytest.fixture
def ctx(owner_user: User, session_factory: async_sessionmaker[AsyncSession]) -> RequestContext:
    return RequestContext(user=owner_user, session_factory=session_factory, message_id="m1")


@pytest.fixture(autouse=True)
async def not_in_demo(owner_user: User) -> AsyncIterator[None]:
    yield
    await leave(owner_user.whatsapp_number)


async def _counts(
    session_factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID
) -> dict[str, int]:
    async with session_factory() as session:
        return {
            table: (
                await session.execute(
                    sa.text(f"SELECT count(*) FROM {table} WHERE org_id = :org").bindparams(
                        org=org_id
                    )
                )
            ).scalar_one()
            for table in ("suppliers", "customers", "products", "purchase_headers", "expenses")
        }


# --------------------------------------------------------------------
# switching
# --------------------------------------------------------------------


async def test_login_as_test_creates_a_business_seeded_like_the_real_one(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A demo that could not read the same sheets or speak the same
    units would demonstrate a different system."""
    result = await handle_login("as test", ctx)

    assert "demo" in result.reply.lower()
    assert await is_demo(ctx.user.whatsapp_number)

    async with session_factory() as session:
        for model in (Unit, ProductType, Warehouse):
            real = (
                await session.execute(
                    sa.select(sa.func.count()).select_from(model).where(model.org_id == ORG)
                )
            ).scalar_one()
            demo = (
                await session.execute(
                    sa.select(sa.func.count()).select_from(model).where(model.org_id == DEMO_ORG_ID)
                )
            ).scalar_one()
            assert demo == real, model.__name__

        # the copied product type points at the *demo's* KG, not the
        # real org's -- otherwise the two businesses share a row
        product_type = (
            (await session.execute(sa.select(ProductType).where(ProductType.org_id == DEMO_ORG_ID)))
            .scalars()
            .first()
        )
        assert product_type is not None
        unit = await session.get(Unit, product_type.default_unit_id)
        assert unit is not None and unit.org_id == DEMO_ORG_ID


async def test_login_as_real_switches_back(ctx: RequestContext) -> None:
    await handle_login("as test", ctx)
    result = await handle_login("as real", ctx)

    assert not await is_demo(ctx.user.whatsapp_number)
    assert "real business" in result.reply


async def test_an_unrecognised_target_asks_rather_than_guessing(ctx: RequestContext) -> None:
    """Switching the wrong way is the one mistake this command can make."""
    result = await handle_login("as sandbox-2", ctx)

    assert "login as test" in result.reply
    assert not await is_demo(ctx.user.whatsapp_number)


# --------------------------------------------------------------------
# isolation -- the point of the whole feature
# --------------------------------------------------------------------


async def test_work_done_in_the_demo_never_reaches_the_real_business(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Two test sales and a ₹15,000 phantom receivable have already had
    to be cleaned out of the partners' real books. This is the fix, and
    this test is the claim."""
    from backend.api.commands.money_commands import handle_expense

    async with session_factory() as session:
        session.add_all(
            [
                Supplier(org_id=ORG, name="Real Supplier", created_by=ctx.user.id),
                Customer(org_id=ORG, name="Real Customer", created_by=ctx.user.id),
            ]
        )
        await session.commit()
    before = await _counts(session_factory, ORG)

    await handle_login("as test", ctx)
    demo_ctx = RequestContext(
        user=as_demo(ctx.user), session_factory=session_factory, message_id="m-demo"
    )
    recorded = await handle_expense("demo rent 5000 cash", demo_ctx)
    assert "Expense recorded" in recorded.reply

    # the real org is untouched...
    assert await _counts(session_factory, ORG) == before
    # ...and the expense is in the demo's books
    assert (await _counts(session_factory, DEMO_ORG_ID))["expenses"] == 1


async def test_the_demo_cannot_see_the_real_businesss_parties(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Isolation has to run both ways: a demo that listed the partners'
    real suppliers would leak their book to whoever is watching."""
    from backend.repositories.party_repository import SupplierRepository

    async with session_factory() as session:
        session.add(Supplier(org_id=ORG, name="Confidential Traders", created_by=ctx.user.id))
        await session.commit()

    await handle_login("as test", ctx)
    async with session_factory() as session:
        found = await SupplierRepository(session).search(DEMO_ORG_ID, "Confidential", limit=5)
    assert found == []


# --------------------------------------------------------------------
# reset
# --------------------------------------------------------------------


async def test_reset_empties_the_demo_and_keeps_its_seed(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from backend.api.commands.money_commands import handle_expense

    await handle_login("as test", ctx)
    demo_ctx = RequestContext(
        user=as_demo(ctx.user), session_factory=session_factory, message_id="m-demo"
    )
    await handle_expense("demo tea 100 cash", demo_ctx)
    assert (await _counts(session_factory, DEMO_ORG_ID))["expenses"] == 1

    result = await handle_reset_demo("", demo_ctx)

    assert "wiped" in result.reply
    assert (await _counts(session_factory, DEMO_ORG_ID))["expenses"] == 0
    # the seed survives, so the next demonstration can start immediately
    async with session_factory() as session:
        units = (
            await session.execute(
                sa.select(sa.func.count()).select_from(Unit).where(Unit.org_id == DEMO_ORG_ID)
            )
        ).scalar_one()
    assert units > 0


async def test_reset_refuses_outside_the_demo(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """It deletes without confirmation, which is only safe because what
    it deletes was never real."""
    async with session_factory() as session:
        session.add(Supplier(org_id=ORG, name="Do Not Delete", created_by=ctx.user.id))
        await session.commit()

    result = await handle_reset_demo("", ctx)

    assert "real books" in result.reply
    assert (await _counts(session_factory, ORG))["suppliers"] == 1


async def test_demo_reports_which_books_you_are_on(ctx: RequestContext) -> None:
    off = await handle_demo("", ctx)
    assert "real" in off.reply.lower()

    await handle_login("as test", ctx)
    on = await handle_demo("", ctx)
    assert "demo" in on.reply.lower()
    assert "untouched" in on.reply
