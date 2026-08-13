"""Reversal manifests and drift classification -- plan.md §10.

The question these answer: merge two parties, wait two months, then try
to restore them. By then rows have been edited, moved again, purged, or
paid against. A reversal that acts without checking would put some rows
back and quietly leave the books wrong; these tests are the checking.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.exceptions import ValidationError
from backend.models import Customer, ReversalManifest, SalesHeader, User
from backend.models.enums import SalePaymentType
from backend.services.reversal_service import ReversalService
from backend.tests.conftest import SEEDED_MAIN_WAREHOUSE_ID, SEEDED_ORG_ID

D = decimal.Decimal
ORG = uuid.UUID(SEEDED_ORG_ID)
WAREHOUSE = uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID)


@pytest.fixture
async def parties(
    staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[tuple[Customer, Customer, SalesHeader]]:
    """Two customers and one sale, as if the second had absorbed the first."""
    suffix = uuid.uuid4().hex[:5]
    async with session_factory() as session:
        loser = Customer(org_id=ORG, name=f"Yakub {suffix}", created_by=staff_user.id)
        winner = Customer(org_id=ORG, name=f"Asif {suffix}", created_by=staff_user.id)
        session.add_all([loser, winner])
        await session.flush()
        sale = SalesHeader(
            org_id=ORG,
            customer_id=winner.id,  # already moved, as a merge would leave it
            warehouse_id=WAREHOUSE,
            sale_date=datetime.date(2026, 6, 1),
            payment_type=SalePaymentType.CREDIT,
            subtotal=D("10000"),
            grand_total=D("10000"),
            created_by=staff_user.id,
        )
        session.add(sale)
        await session.commit()
        for obj in (loser, winner, sale):
            await session.refresh(obj)
        ids = (loser.id, winner.id, sale.id)
    async with session_factory() as session:
        loser = await session.get(Customer, ids[0])  # type: ignore[assignment]
        winner = await session.get(Customer, ids[1])  # type: ignore[assignment]
        sale = await session.get(SalesHeader, ids[2])  # type: ignore[assignment]
        yield loser, winner, sale
    async with session_factory() as session:
        await session.execute(sa.text("DELETE FROM reversal_manifests"))
        await session.execute(sa.text("DELETE FROM sales_headers WHERE id = :i"), {"i": ids[2]})
        await session.execute(
            sa.text("DELETE FROM customers WHERE id = ANY(:i)"), {"i": [ids[0], ids[1]]}
        )
        await session.commit()


async def _manifest(
    session: AsyncSession, actor: User, loser: Customer, winner: Customer, sale: SalesHeader
) -> ReversalManifest:
    return await ReversalService(session).record(
        ORG,
        actor,
        operation="merge_party",
        subject=f"{loser.name} → {winner.name}",
        moved=[
            {
                "table": "sales_headers",
                "id": str(sale.id),
                "column": "customer_id",
                "from": str(loser.id),
                "to": str(winner.id),
            }
        ],
        hidden=[{"table": "customers", "id": str(loser.id)}],
    )


async def test_an_untouched_row_is_intact_and_goes_back(
    parties: tuple[Customer, Customer, SalesHeader],
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    loser, winner, sale = parties
    async with session_factory() as session:
        manifest = await _manifest(session, staff_user, loser, winner, sale)
        plan = await ReversalService(session).plan(manifest)
        assert [r.state for r in plan.rows] == ["intact"]
        assert plan.ok
        moved = await ReversalService(session).apply(plan, staff_user)
        assert moved == 1
        await session.commit()

    async with session_factory() as session:
        restored = await session.get(SalesHeader, sale.id)
        assert restored is not None
        assert restored.customer_id == loser.id, "the sale did not go back"
        revived = await session.get(Customer, loser.id)
        assert revived is not None and revived.deleted_at is None


async def test_a_row_moved_again_blocks_the_whole_reversal(
    parties: tuple[Customer, Customer, SalesHeader],
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A second merge, or a `fix --customer`, moved it somewhere the
    manifest never mentioned. Putting it back where *this* merge found
    it would undo a decision made later."""
    loser, winner, sale = parties
    async with session_factory() as session:
        manifest = await _manifest(session, staff_user, loser, winner, sale)
        await session.commit()
        manifest_id = manifest.id

    async with session_factory() as session:
        third = Customer(org_id=ORG, name=f"Third {uuid.uuid4().hex[:4]}", created_by=staff_user.id)
        session.add(third)
        await session.flush()
        header = await session.get(SalesHeader, sale.id)
        assert header is not None
        header.customer_id = third.id
        await session.commit()

    async with session_factory() as session:
        manifest = await session.get(ReversalManifest, manifest_id)  # type: ignore[assignment]
        plan = await ReversalService(session).plan(manifest)
        assert [r.state for r in plan.rows] == ["re-pointed"]
        assert not plan.ok
        with pytest.raises(ValidationError):
            await ReversalService(session).apply(plan, staff_user)


async def test_a_purged_row_blocks_it_too(
    parties: tuple[Customer, Customer, SalesHeader],
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    loser, winner, sale = parties
    async with session_factory() as session:
        manifest = await _manifest(session, staff_user, loser, winner, sale)
        await session.commit()
        manifest_id = manifest.id

    async with session_factory() as session:
        header = await session.get(SalesHeader, sale.id)
        assert header is not None
        header.deleted_at = datetime.datetime.now(datetime.UTC)
        header.purged_at = header.deleted_at
        await session.commit()

    async with session_factory() as session:
        manifest = await session.get(ReversalManifest, manifest_id)  # type: ignore[assignment]
        plan = await ReversalService(session).plan(manifest)
        assert [r.state for r in plan.rows] == ["missing"]
        assert not plan.ok


async def test_money_taken_after_the_merge_makes_it_entangled(
    parties: tuple[Customer, Customer, SalesHeader],
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The case that actually bites. A receipt arrives *after* the merge
    and settles a bill that is about to move back — the payment belongs
    to one party and the bill to another. No arithmetic fixes that, so
    the reversal refuses and names the payment."""
    loser, winner, sale = parties
    async with session_factory() as session:
        manifest = await _manifest(session, staff_user, loser, winner, sale)
        await session.commit()
        manifest_id = manifest.id

    async with session_factory() as session:
        await session.execute(
            sa.text(
                "INSERT INTO audit_logs "
                "(org_id, actor_user_id, action, entity_type, entity_id, after_state, channel) "
                "VALUES (:org, :actor, 'payment.received', 'customers', :entity, "
                "  cast(:state as jsonb), 'whatsapp')"
            ),
            {
                "org": ORG,
                "actor": staff_user.id,
                "entity": winner.id,
                "state": (
                    '{"amount": "10000", "via": "cash", "allocations": '
                    f'[{{"reference": "abc", "applied": "10000", "header_id": "{sale.id}"}}]}}'
                ),
            },
        )
        await session.commit()

    async with session_factory() as session:
        manifest = await session.get(ReversalManifest, manifest_id)  # type: ignore[assignment]
        plan = await ReversalService(session).plan(manifest)
        assert [r.state for r in plan.rows] == ["entangled"]
        assert "payment" in plan.blocked[0].detail
        assert not plan.ok

    async with session_factory() as session:
        await session.execute(sa.text("DELETE FROM audit_logs WHERE action = 'payment.received'"))
        await session.commit()


async def test_a_manifest_is_usable_once(
    parties: tuple[Customer, Customer, SalesHeader],
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    loser, winner, sale = parties
    async with session_factory() as session:
        manifest = await _manifest(session, staff_user, loser, winner, sale)
        plan = await ReversalService(session).plan(manifest)
        await ReversalService(session).apply(plan, staff_user)
        await session.commit()
        manifest_id = manifest.id

    async with session_factory() as session:
        manifest = await session.get(ReversalManifest, manifest_id)  # type: ignore[assignment]
        with pytest.raises(ValidationError, match="already been reversed"):
            await ReversalService(session).plan(manifest)


async def test_rows_created_after_the_merge_are_never_touched(
    parties: tuple[Customer, Customer, SalesHeader],
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The rule that stops a reversal inventing history: a sale made to
    the surviving party after the merge was never the other one's."""
    loser, winner, sale = parties
    async with session_factory() as session:
        manifest = await _manifest(session, staff_user, loser, winner, sale)
        later = SalesHeader(
            org_id=ORG,
            customer_id=winner.id,
            warehouse_id=WAREHOUSE,
            sale_date=datetime.date(2026, 8, 1),
            payment_type=SalePaymentType.CREDIT,
            subtotal=D("500"),
            grand_total=D("500"),
            created_by=staff_user.id,
        )
        session.add(later)
        await session.flush()
        later_id = later.id
        plan = await ReversalService(session).plan(manifest)
        await ReversalService(session).apply(plan, staff_user)
        await session.commit()

    async with session_factory() as session:
        untouched = await session.get(SalesHeader, later_id)
        assert untouched is not None
        assert untouched.customer_id == winner.id, "a later sale was dragged back"
        await session.execute(sa.text("DELETE FROM sales_headers WHERE id = :i"), {"i": later_id})
        await session.commit()


def test_restoring_reads_the_type_off_the_column_not_the_manifest() -> None:
    """The manifest is JSON, so every value comes back as text.

    A merge moves UUIDs *and* renumbers lines. Restoring `line_no` from
    the string "3" would fail in the database rather than in Python,
    which is a worse place to find out — so the type is read off what
    the column holds now.
    """
    import decimal as _decimal

    from backend.services.reversal_service import _coerce

    assert _coerce(uuid.UUID(int=1), str(uuid.UUID(int=2))) == uuid.UUID(int=2)
    assert _coerce(7, "3") == 3
    assert isinstance(_coerce(7, "3"), int)
    assert _coerce(_decimal.Decimal("1.00"), "2.50") == _decimal.Decimal("2.50")
    assert _coerce("anything", "text") == "text"
    assert _coerce(uuid.UUID(int=1), None) is None
