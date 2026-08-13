"""`guarded()` end to end, against a real session.

These exist because the pure-function tests over `_regressions` all
passed while `guarded` itself was broken: the baseline snapshot's
SELECTs autobegin a transaction, so `session.begin()` raised "a
transaction is already begun" and every mutating command died before
touching anything. It shipped, and failed on the first real command.

A safety net nobody has fallen into is not known to hold.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.admin.harness import AdminContext, ReconciliationRegressed, guarded
from backend.models import Inventory, InventoryMovement, Organization, Product, User
from backend.models.enums import MovementType
from backend.tests.conftest import (
    SEEDED_KG_UNIT_ID,
    SEEDED_MAIN_WAREHOUSE_ID,
    SEEDED_ORG_ID,
    SEEDED_TEXTILE_TYPE_ID,
)

ORG = uuid.UUID(SEEDED_ORG_ID)
WAREHOUSE = uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID)


@pytest.fixture
async def ctx(
    staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[AdminContext]:
    async with session_factory() as session:
        org = (
            await session.execute(select(Organization).where(Organization.id == ORG))
        ).scalar_one()
        yield AdminContext(session=session, org=org, actor=staff_user)


@pytest.fixture
async def widget(
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
    drop_product: list[uuid.UUID],
) -> AsyncIterator[Product]:
    async with session_factory() as session:
        product = Product(
            org_id=ORG,
            product_type_id=uuid.UUID(SEEDED_TEXTILE_TYPE_ID),
            code=f"GRD{uuid.uuid4().hex[:4].upper()}",
            description="Guard probe",
            unit_id=uuid.UUID(SEEDED_KG_UNIT_ID),
            created_by=staff_user.id,
        )
        session.add(product)
        await session.flush()
        session.add(
            Inventory(
                org_id=ORG,
                product_id=product.id,
                warehouse_id=WAREHOUSE,
                qty_on_hand=decimal.Decimal("0.000"),
                weighted_avg_cost=decimal.Decimal("0.0000"),
            )
        )
        await session.commit()
        drop_product.append(product.id)
        yield product


def _movement(product: Product, user: User, qty: str) -> InventoryMovement:
    return InventoryMovement(
        org_id=ORG,
        product_id=product.id,
        warehouse_id=WAREHOUSE,
        movement_type=MovementType.PURCHASE,
        qty_delta=decimal.Decimal(qty),
        unit_cost=decimal.Decimal("100"),
        resulting_qty_on_hand=decimal.Decimal(qty),
        resulting_avg_cost=decimal.Decimal("100"),
        source_type="test",
        source_id=uuid.uuid4(),
        created_by=user.id,
        created_at=datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC),
    )


async def test_guarded_commits_a_balanced_change(
    ctx: AdminContext,
    widget: Product,
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The regression test for the autobegin bug: this failed with
    'a transaction is already begun' before the snapshot released its
    read transaction."""
    async with guarded(ctx, what="balanced probe", backup=False):
        ctx.session.add(_movement(widget, staff_user, "10"))
        inventory = (
            await ctx.session.execute(select(Inventory).where(Inventory.product_id == widget.id))
        ).scalar_one()
        inventory.qty_on_hand = decimal.Decimal("10.000")
        inventory.weighted_avg_cost = decimal.Decimal("100.0000")

    async with session_factory() as session:
        row = (
            await session.execute(select(Inventory).where(Inventory.product_id == widget.id))
        ).scalar_one()
        assert row.qty_on_hand == decimal.Decimal("10.000")


async def test_guarded_rolls_back_when_the_books_stop_balancing(
    ctx: AdminContext,
    widget: Product,
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A movement with no matching stock change is exactly the shape of
    every costing bug this CLI exists to avoid. It must not commit."""
    with pytest.raises(ReconciliationRegressed):
        async with guarded(ctx, what="unbalanced probe", backup=False):
            ctx.session.add(_movement(widget, staff_user, "7"))
            # deliberately not touching Inventory

    async with session_factory() as session:
        row = (
            await session.execute(select(Inventory).where(Inventory.product_id == widget.id))
        ).scalar_one()
        assert row.qty_on_hand == decimal.Decimal("0.000"), "the rollback did not happen"
        count = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(InventoryMovement)
                .where(InventoryMovement.product_id == widget.id)
            )
        ).scalar_one()
        assert count == 0, "the movement survived a rollback"


async def test_dry_run_changes_nothing(
    ctx: AdminContext,
    widget: Product,
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx.dry_run = True
    async with guarded(ctx, what="dry run probe", backup=False):
        ctx.session.add(_movement(widget, staff_user, "5"))
        inventory = (
            await ctx.session.execute(select(Inventory).where(Inventory.product_id == widget.id))
        ).scalar_one()
        inventory.qty_on_hand = decimal.Decimal("5.000")

    async with session_factory() as session:
        row = (
            await session.execute(select(Inventory).where(Inventory.product_id == widget.id))
        ).scalar_one()
        assert row.qty_on_hand == decimal.Decimal("0.000"), "--dry-run committed"
