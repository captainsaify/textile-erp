"""Replaying weighted-average cost from movement history.

The first test here is not hypothetical. A repair script skipped
zero-quantity movements as an obvious optimisation, which discarded
every rate correction ever made across 28 products and overstated stock
by roughly 1.3 lakh on the live books. It was caught by a person who
knew what one product had cost, not by anything automated. This is that
automation.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import Inventory, InventoryMovement, Product, User
from backend.models.enums import MovementType
from backend.services.cost_replay_service import CostReplayService
from backend.tests.conftest import (
    SEEDED_KG_UNIT_ID,
    SEEDED_MAIN_WAREHOUSE_ID,
    SEEDED_ORG_ID,
    SEEDED_TEXTILE_TYPE_ID,
)

ORG = uuid.UUID(SEEDED_ORG_ID)
WAREHOUSE = uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID)
BASE = datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.UTC)


@pytest.fixture
async def cww(
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
    drop_product: list[uuid.UUID],
) -> AsyncIterator[Product]:
    async with session_factory() as session:
        product = Product(
            org_id=ORG,
            product_type_id=uuid.UUID(SEEDED_TEXTILE_TYPE_ID),
            code="CWW",
            description="Cotton Winter Wear",
            unit_id=uuid.UUID(SEEDED_KG_UNIT_ID),
            created_by=staff_user.id,
        )
        session.add(product)
        await session.flush()
        await session.commit()
        drop_product.append(product.id)
        yield product


async def _movement(
    session: AsyncSession,
    product: Product,
    user: User,
    *,
    minutes: int,
    kind: MovementType,
    qty: str,
    unit_cost: str,
) -> None:
    session.add(
        InventoryMovement(
            org_id=ORG,
            product_id=product.id,
            warehouse_id=WAREHOUSE,
            movement_type=kind,
            qty_delta=decimal.Decimal(qty),
            unit_cost=decimal.Decimal(unit_cost),
            # Deliberately wrong: replay must derive these, not trust them.
            resulting_qty_on_hand=decimal.Decimal("-999"),
            resulting_avg_cost=decimal.Decimal("-999"),
            source_type="test",
            source_id=uuid.uuid4(),
            created_by=user.id,
            created_at=BASE + datetime.timedelta(minutes=minutes),
        )
    )


async def _inventory(session: AsyncSession, product: Product) -> Inventory | None:
    return (
        await session.execute(
            select(Inventory).where(
                Inventory.org_id == ORG,
                Inventory.product_id == product.id,
                Inventory.warehouse_id == WAREHOUSE,
            )
        )
    ).scalar_one_or_none()


async def test_a_zero_quantity_movement_restates_the_average(
    cww: Product, staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The bug that cost 1.3 lakh, in one test.

    Bought 100 at 150, corrected the bill to 107, then bought 50 at 106.
    The correction is a qty_delta=0 movement whose unit_cost carries the
    new average. Skipping it gives 135.33; honouring it gives 106.67.
    """
    async with session_factory() as session:
        await _movement(
            session,
            cww,
            staff_user,
            minutes=0,
            kind=MovementType.PURCHASE,
            qty="100",
            unit_cost="150",
        )
        await _movement(
            session,
            cww,
            staff_user,
            minutes=10,
            kind=MovementType.ADJUSTMENT_INCREASE,
            qty="0",
            unit_cost="107",
        )
        await _movement(
            session,
            cww,
            staff_user,
            minutes=20,
            kind=MovementType.PURCHASE,
            qty="50",
            unit_cost="106",
        )
        await session.commit()

    async with session_factory() as session:
        result = await CostReplayService(session).replay(ORG, cww.id, WAREHOUSE)
        await session.commit()

    assert result.movements == 3
    assert result.qty_after == decimal.Decimal("150.000")
    # (100*107 + 50*106) / 150 -- not (100*150 + 50*106) / 150 = 135.3333
    assert result.avg_after == decimal.Decimal("106.6667")
    assert result.avg_after < decimal.Decimal("110"), (
        "the restatement was skipped: this is the 1.3 lakh overstatement, exactly"
    )


async def test_selling_does_not_move_the_average(
    cww: Product, staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Cost leaves at the current average by definition. Recomputing on
    the way out would drift the books on every sale."""
    async with session_factory() as session:
        await _movement(
            session,
            cww,
            staff_user,
            minutes=0,
            kind=MovementType.PURCHASE,
            qty="100",
            unit_cost="120",
        )
        await _movement(
            session, cww, staff_user, minutes=10, kind=MovementType.SALE, qty="-40", unit_cost="70"
        )
        await session.commit()

    async with session_factory() as session:
        result = await CostReplayService(session).replay(ORG, cww.id, WAREHOUSE)
        await session.commit()

    assert result.qty_after == decimal.Decimal("60.000")
    assert result.avg_after == decimal.Decimal("120.0000")


async def test_replay_rewrites_the_resulting_columns(
    cww: Product, staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`resulting_*` is how the ledger explains itself later. Left
    describing a history that no longer happened, every subsequent
    investigation reads a lie."""
    async with session_factory() as session:
        await _movement(
            session,
            cww,
            staff_user,
            minutes=0,
            kind=MovementType.PURCHASE,
            qty="10",
            unit_cost="100",
        )
        await session.commit()

    async with session_factory() as session:
        await CostReplayService(session).replay(ORG, cww.id, WAREHOUSE)
        await session.commit()

    async with session_factory() as session:
        movement = (
            await session.execute(
                select(InventoryMovement).where(InventoryMovement.product_id == cww.id)
            )
        ).scalar_one()
        assert movement.resulting_qty_on_hand == decimal.Decimal("10.000")
        assert movement.resulting_avg_cost == decimal.Decimal("100.0000")


async def test_replay_is_idempotent(
    cww: Product, staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Running it twice must not drift. A replay that is not idempotent
    cannot be safely offered as a repair."""
    async with session_factory() as session:
        for i, (qty, cost) in enumerate([("80", "90"), ("0", "95"), ("-20", "0"), ("40", "110")]):
            await _movement(
                session,
                cww,
                staff_user,
                minutes=i * 10,
                kind=MovementType.PURCHASE if qty not in {"-20"} else MovementType.SALE,
                qty=qty,
                unit_cost=cost,
            )
        await session.commit()

    async with session_factory() as session:
        first = await CostReplayService(session).replay(ORG, cww.id, WAREHOUSE)
        await session.commit()
    async with session_factory() as session:
        second = await CostReplayService(session).replay(ORG, cww.id, WAREHOUSE)
        await session.commit()

    assert (first.qty_after, first.avg_after) == (second.qty_after, second.avg_after)
    assert not second.changed


async def test_replay_creates_the_inventory_row_when_a_movement_arrives_first(
    cww: Product, staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Re-pointing a line can give a product its first movement in a
    warehouse it has no inventory row for yet."""
    async with session_factory() as session:
        assert await _inventory(session, cww) is None
        await _movement(
            session,
            cww,
            staff_user,
            minutes=0,
            kind=MovementType.PURCHASE,
            qty="5",
            unit_cost="200",
        )
        await session.commit()

    async with session_factory() as session:
        await CostReplayService(session).replay(ORG, cww.id, WAREHOUSE)
        await session.commit()

    async with session_factory() as session:
        inventory = await _inventory(session, cww)
        assert inventory is not None
        assert inventory.qty_on_hand == decimal.Decimal("5.000")
        assert inventory.weighted_avg_cost == decimal.Decimal("200.0000")
