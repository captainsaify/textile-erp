"""Merging products, re-linking numbers, and the delivery log.

The costing assertion is the one that matters. Merging two products is
the only admin operation that *changes a number people rely on*: the
survivor's weighted average is recomputed over both histories, and a
merge that simply re-pointed the rows would leave `inventory` describing
a history that no longer happened.

So the arithmetic is spelled out rather than asserted against whatever
the code produces:

    A: 10 kg at ₹100   = ₹1,000
    B: 10 kg at ₹200   = ₹2,000
    merged: 20 kg, ₹3,000 → ₹150.0000 average

Nothing about that number can be right by accident.
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

from backend.core.exceptions import ValidationError
from backend.models import (
    BankLedger,
    Inventory,
    InventoryMovement,
    MessageLog,
    Product,
    User,
)
from backend.models.enums import LedgerEntryType, MovementType, UserRole
from backend.services import message_log
from backend.services.admin.contacts import ContactAdminService
from backend.services.admin.diagnostics import DiagnosticsService
from backend.services.admin.products import ProductAdminService, replay_after_reversal
from backend.services.reversal_service import ReversalService
from backend.tests.conftest import (
    SEEDED_KG_UNIT_ID,
    SEEDED_MAIN_WAREHOUSE_ID,
    SEEDED_ORG_ID,
    SEEDED_TEXTILE_TYPE_ID,
)

ORG = uuid.UUID(SEEDED_ORG_ID)
WAREHOUSE = uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID)


async def _make_product(
    session: AsyncSession, actor: User, *, qty: str, cost: str, when: int
) -> Product:
    """A product with one purchase behind it, and stock to match."""
    product = Product(
        org_id=ORG,
        product_type_id=uuid.UUID(SEEDED_TEXTILE_TYPE_ID),
        code=f"MRG{uuid.uuid4().hex[:5].upper()}",
        description="Merge probe",
        unit_id=uuid.UUID(SEEDED_KG_UNIT_ID),
        created_by=actor.id,
    )
    session.add(product)
    await session.flush()
    session.add_all(
        [
            InventoryMovement(
                org_id=ORG,
                product_id=product.id,
                warehouse_id=WAREHOUSE,
                movement_type=MovementType.PURCHASE,
                qty_delta=decimal.Decimal(qty),
                unit_cost=decimal.Decimal(cost),
                resulting_qty_on_hand=decimal.Decimal(qty),
                resulting_avg_cost=decimal.Decimal(cost),
                source_type="test",
                source_id=uuid.uuid4(),
                created_by=actor.id,
                # Distinct timestamps: the replay orders by them, and an
                # average computed in an unstable order is not a value.
                created_at=datetime.datetime(2026, 7, when, tzinfo=datetime.UTC),
            ),
            Inventory(
                org_id=ORG,
                product_id=product.id,
                warehouse_id=WAREHOUSE,
                qty_on_hand=decimal.Decimal(qty),
                weighted_avg_cost=decimal.Decimal(cost),
            ),
        ]
    )
    await session.flush()
    return product


@pytest.fixture
async def pair(
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
    drop_product: list[uuid.UUID],
) -> AsyncIterator[tuple[Product, Product]]:
    async with session_factory() as session:
        first = await _make_product(session, staff_user, qty="10", cost="100", when=1)
        second = await _make_product(session, staff_user, qty="10", cost="200", when=2)
        await session.commit()
        drop_product.extend([first.id, second.id])
        yield first, second


async def test_merging_two_products_replays_the_average_over_both(
    pair: tuple[Product, Product],
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    losing, winning = pair
    async with session_factory() as session:
        service = ProductAdminService(session)
        plan = await service.merge_plan(
            ORG,
            loser_code=losing.code,
            loser_brand=None,
            winner_code=winning.code,
            winner_brand=None,
        )
        assert plan.ok
        assert len(plan.movements) == 1
        await service.merge_apply(ORG, staff_user, plan)

    async with session_factory() as session:
        stock = (
            await session.execute(select(Inventory).where(Inventory.product_id == winning.id))
        ).scalar_one()
        assert stock.qty_on_hand == decimal.Decimal("20.000")
        # 1,000 + 2,000 over 20 kg. Not either input average.
        assert stock.weighted_avg_cost == decimal.Decimal("150.0000")

        gone = await session.get(Product, losing.id)
        assert gone is not None and gone.deleted_at is not None
        emptied = (
            await session.execute(select(Inventory).where(Inventory.product_id == losing.id))
        ).scalar_one()
        assert emptied.qty_on_hand == decimal.Decimal("0.000")


async def test_a_merged_product_can_be_unmerged(
    pair: tuple[Product, Product],
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reversal is the whole promise, and for products it is only half
    done by moving rows: the averages have to be replayed on both sides
    or `inventory` still describes the merged history."""
    losing, winning = pair
    async with session_factory() as session:
        service = ProductAdminService(session)
        plan = await service.merge_plan(
            ORG,
            loser_code=losing.code,
            loser_brand=None,
            winner_code=winning.code,
            winner_brand=None,
        )
        result = await service.merge_apply(ORG, staff_user, plan)

    async with session_factory() as session:
        reversal = ReversalService(session)
        manifest = await reversal.get(ORG, result["reversal"])
        rolled = await reversal.plan(manifest)
        assert rolled.ok, [row.detail for row in rolled.blocked]
        await reversal.apply(rolled, staff_user)
        await replay_after_reversal(session, ORG, manifest)
        await session.commit()

    async with session_factory() as session:
        back = (
            await session.execute(select(Inventory).where(Inventory.product_id == losing.id))
        ).scalar_one()
        assert back.qty_on_hand == decimal.Decimal("10.000")
        assert back.weighted_avg_cost == decimal.Decimal("100.0000")
        survivor = (
            await session.execute(select(Inventory).where(Inventory.product_id == winning.id))
        ).scalar_one()
        assert survivor.qty_on_hand == decimal.Decimal("10.000")
        assert survivor.weighted_avg_cost == decimal.Decimal("200.0000")
        restored = await session.get(Product, losing.id)
        assert restored is not None and restored.deleted_at is None


async def test_a_product_with_history_cannot_be_deleted(
    pair: tuple[Product, Product],
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Hiding a product that has been bought would take its purchases out
    of the reports that explain the cost of everything else."""
    losing, _ = pair
    async with session_factory() as session:
        with pytest.raises(ValidationError, match="movement"):
            await ProductAdminService(session).delete(ORG, staff_user, code=losing.code, brand=None)


async def test_describing_a_product_renames_it_and_leaves_the_bills_alone(
    pair: tuple[Product, Product],
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The 55D case: purchase 002 created the row and named it, purchase
    003 later reused the same row keeping its own wording on its lines,
    and a brand correction then moved 002 away -- leaving the row named
    after a bill that no longer belongs to it."""
    target, _ = pair
    async with session_factory() as session:
        result = await ProductAdminService(session).describe(
            ORG, staff_user, code=target.code, brand=None, description="SHORT SLEEVED SWEATER"
        )
    assert result["was"] == "Merge probe"
    assert result["now"] == "SHORT SLEEVED SWEATER"

    async with session_factory() as session:
        renamed = await session.get(Product, target.id)
        assert renamed is not None
        assert renamed.description == "SHORT SLEEVED SWEATER"
        # stock untouched -- this is a label, not a movement
        stock = (
            await session.execute(
                sa.select(Inventory.qty_on_hand).where(Inventory.product_id == target.id)
            )
        ).scalar_one()
        assert stock == decimal.Decimal("10.000")
        logged = (
            await session.execute(
                sa.text(
                    "select before_state, after_state from audit_logs "
                    "where entity_id = :id and action = 'product.described'"
                ),
                {"id": str(target.id)},
            )
        ).all()
    assert len(logged) == 1
    assert logged[0][0] == {"description": "Merge probe"}
    assert logged[0][1] == {"description": "SHORT SLEEVED SWEATER"}


async def test_describing_refuses_a_blank_and_a_no_op(
    pair: tuple[Product, Product],
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    target, _ = pair
    async with session_factory() as session:
        with pytest.raises(ValidationError, match="blank"):
            await ProductAdminService(session).describe(
                ORG, staff_user, code=target.code, brand=None, description="   "
            )
    async with session_factory() as session:
        with pytest.raises(ValidationError, match="already described"):
            await ProductAdminService(session).describe(
                ORG, staff_user, code=target.code, brand=None, description="Merge probe"
            )


async def test_relinking_moves_a_number_and_can_be_undone(
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The Firoz case: a partner changes SIM, and until this runs their
    messages reach an unrecognised number -- which is answered with
    deliberate silence, so the symptom is that nothing happens."""
    number = staff_user.whatsapp_number
    assert number is not None

    async with session_factory() as session:
        newcomer = User(
            org_id=ORG,
            full_name=f"Relink Probe {uuid.uuid4().hex[:6]}",
            email=f"relink-{uuid.uuid4().hex[:8]}@example.test",
            role=UserRole.STAFF,
        )
        session.add(newcomer)
        await session.commit()
        newcomer_id, newcomer_name = newcomer.id, newcomer.full_name

    try:
        async with session_factory() as session:
            # The old holder needs a way back in, or the database's own
            # login_method constraint refuses -- as it should.
            holder = await session.get(User, staff_user.id)
            assert holder is not None
            holder.email = f"holder-{uuid.uuid4().hex[:8]}@example.test"
            await session.commit()

        async with session_factory() as session:
            result = await ContactAdminService(session).relink(
                ORG, staff_user, number=number, to_name=newcomer_name
            )
        assert result["taken_from"] == staff_user.full_name

        async with session_factory() as session:
            moved = await session.get(User, newcomer_id)
            vacated = await session.get(User, staff_user.id)
            assert moved is not None and moved.whatsapp_number == number
            assert vacated is not None and vacated.whatsapp_number is None

            service = ReversalService(session)
            manifest = await service.get(ORG, result["reversal"])
            plan = await service.plan(manifest)
            assert plan.ok, [row.detail for row in plan.blocked]
            await service.apply(plan, staff_user)
            await session.commit()

        async with session_factory() as session:
            restored = await session.get(User, staff_user.id)
            assert restored is not None and restored.whatsapp_number == number
    finally:
        async with session_factory() as session:
            await session.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": newcomer_id})
            await session.commit()


async def test_relinking_refuses_to_strand_someone(
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Taking the number from someone with no email leaves a row that
    cannot sign in by any route. Said in words, not as an IntegrityError
    thrown from three layers down."""
    number = staff_user.whatsapp_number
    assert number is not None
    async with session_factory() as session:
        stranded = User(
            org_id=ORG,
            full_name=f"Stranded Probe {uuid.uuid4().hex[:6]}",
            email=f"stranded-{uuid.uuid4().hex[:8]}@example.test",
            role=UserRole.STAFF,
        )
        session.add(stranded)
        await session.commit()
        stranded_id, stranded_name = stranded.id, stranded.full_name

    try:
        async with session_factory() as session:
            with pytest.raises(ValidationError, match="only way to sign in"):
                await ContactAdminService(session).relink(
                    ORG, staff_user, number=number, to_name=stranded_name
                )
    finally:
        async with session_factory() as session:
            await session.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": stranded_id})
            await session.commit()


async def test_failed_sends_are_grouped_by_cause(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seventeen failures with one cause are one problem. A screen that
    lists them as seventeen rows hides that, which is how they went
    unnoticed for a night."""
    marker = uuid.uuid4().hex[:8]
    async with session_factory() as session:
        for _ in range(3):
            session.add(
                MessageLog(
                    direction="out",
                    transport="cloud",
                    peer=f"+9199{marker}",
                    kind="text",
                    preview="your sheet",
                    ok=False,
                    error_code="131047",
                    error_detail="re-engagement message",
                )
            )
        session.add(
            MessageLog(
                direction="out",
                transport="cloud",
                peer=f"+9199{marker}",
                kind="text",
                preview="delivered fine",
                ok=True,
            )
        )
        await session.commit()

    try:
        async with session_factory() as session:
            summary = await message_log.failure_summary(session, since_hours=1)
            causes = {c["code"]: c for c in summary["causes"]}
            assert causes["131047"]["count"] >= 3
            # The number alone sends the reader to Meta's docs; the
            # sentence tells them whose problem it is.
            assert "24 hours" in causes["131047"]["meaning"]

            failures = await message_log.recent(session, limit=50, failed_only=True)
            assert all(not row.ok for row in failures)
    finally:
        async with session_factory() as session:
            await session.execute(
                sa.text("DELETE FROM message_log WHERE peer = :peer"),
                {"peer": f"+9199{marker}"},
            )
            await session.commit()


async def test_rebuilding_ledgers_repairs_a_drifted_running_balance(
    staff_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`resulting_balance` is a cache of a sum, and caches drift. The
    rebuild walks the chain and rewrites each snapshot from the amounts
    themselves -- which is the same definition reconciliation checks
    against, so the guard around it is the proof."""
    async with session_factory() as session:
        row = BankLedger(
            org_id=ORG,
            entry_type=LedgerEntryType.INCOME,
            entry_date=datetime.date(2026, 7, 1),
            amount=decimal.Decimal("500.00"),
            resulting_balance=decimal.Decimal("999999.00"),  # a lie
            source_type="test",
            source_id=uuid.uuid4(),
            notes="drift probe",
            created_by=staff_user.id,
        )
        session.add(row)
        await session.commit()
        row_id = row.id

    try:
        async with session_factory() as session:
            drift = await DiagnosticsService(session).ledger_drift(ORG)
            assert any(d["ledger"] == "bank" for d in drift)

        async with session_factory() as session:
            result = await DiagnosticsService(session).rebuild_ledgers(ORG, staff_user)
            assert result["corrected"] >= 1

        async with session_factory() as session:
            assert not await DiagnosticsService(session).ledger_drift(ORG)
            # Fetched by id rather than `session.get`: the ledgers are
            # partitioned, so their primary key is (id, created_at).
            repaired = (
                await session.execute(select(BankLedger).where(BankLedger.id == row_id))
            ).scalar_one()
            # The amount itself was never touched: this computes, it does
            # not destroy.
            assert repaired.amount == decimal.Decimal("500.00")
    finally:
        async with session_factory() as session:
            await session.execute(sa.text("DELETE FROM bank_ledger WHERE id = :id"), {"id": row_id})
            await session.commit()


async def test_restore_refuses_while_the_application_is_connected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The regression test for an outage.

    `pg_restore --clean` needs an exclusive lock on every table, and
    cannot have one while the app is connected -- so it *waits*, and
    every query behind it waits too. The site went down and the restore
    never ran. A hang is the worst failure available here, because it is
    indistinguishable from slowness and the instinct is to wait.

    So it must say no, out loud, before it starts. This test holds two
    sessions open, which is exactly the condition that produced the
    outage.
    """
    from backend.services.backup_service import BackupService, _backup_dir_for_tests

    directory = _backup_dir_for_tests()
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / f"backup-refusal-probe-{uuid.uuid4().hex[:8]}.dump"
    probe.write_bytes(b"not a real dump -- never read, the refusal comes first")

    try:
        async with session_factory() as one, session_factory() as two:
            # Both connections are real and open, like the API's are.
            await one.execute(sa.text("SELECT 1"))
            await two.execute(sa.text("SELECT 1"))
            with pytest.raises(ValidationError, match="other connection"):
                await BackupService(one).restore(backup_name=probe.name, confirmation=probe.name)
    finally:
        probe.unlink(missing_ok=True)
