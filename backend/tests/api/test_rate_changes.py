"""Correcting the rate on a confirmed bill -- docs/26_RateChanges.md.

The quantity was right and the price was not. Nothing moves, but the
bill, the payable and what the stock cost all change together -- and
goods already sold keep the cost they were sold at, because reaching
back through every later sale is a different operation with a different
blast radius.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.exceptions import NotFoundError, ValidationError
from backend.models import Inventory, Product, PurchaseHeader, PurchaseLine, Supplier, User
from backend.services.inventory_service import InventoryService
from backend.services.receipt_correction_service import RateChange, RateChangeService
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


async def _bill(
    session_factory: async_sessionmaker[AsyncSession], actor: User
) -> tuple[str, str, str]:
    """Two lines at 150: 35A 800kg and 22D 1000kg."""
    suffix = uuid.uuid4().hex[:5]
    invoice_no = f"INV-{suffix}"
    codes = (f"35A{suffix.upper()}", f"22D{suffix.upper()}")

    async with session_factory() as session, session.begin():
        supplier = Supplier(org_id=ORG, name=f"Wagdia {suffix}", created_by=actor.id)
        session.add(supplier)
        await session.flush()
        header = PurchaseHeader(
            org_id=ORG,
            supplier_id=supplier.id,
            warehouse_id=WAREHOUSE,
            invoice_no=invoice_no,
            invoice_date=datetime.date.today(),
            subtotal=D("270000"),
            grand_total=D("270000"),
            status="confirmed",
            created_by=actor.id,
        )
        session.add(header)
        await session.flush()

        for index, (code, qty) in enumerate(zip(codes, (D("800"), D("1000")), strict=True), 1):
            product = Product(
                org_id=ORG,
                product_type_id=uuid.UUID(SEEDED_TEXTILE_TYPE_ID),
                code=code,
                description=code,
                unit_id=uuid.UUID(SEEDED_KG_UNIT_ID),
                created_by=actor.id,
            )
            session.add(product)
            await session.flush()
            line = PurchaseLine(
                org_id=ORG,
                purchase_header_id=header.id,
                line_no=index,
                product_id=product.id,
                description=code,
                qty=qty,
                weight_kg=D("80"),
                total_weight_kg=qty,
                rate=D("150"),
                line_total=qty * D("150"),
                freight_allocated=D("0"),
                landed_cost_per_unit=D("150"),
            )
            session.add(line)
            await session.flush()
            await InventoryService(session).record_purchase_movement(
                ORG,
                product_id=product.id,
                warehouse_id=WAREHOUSE,
                qty=qty,
                landed_cost_per_unit=D("150"),
                source_id=line.id,
                created_by=actor.id,
            )
    return invoice_no, codes[0], codes[1]


async def _change(
    session_factory: async_sessionmaker[AsyncSession],
    actor: User,
    invoice_no: str,
    rate: str,
    codes: list[str] | None = None,
) -> RateChange:
    async with session_factory() as session, session.begin():
        return await RateChangeService(session).change(
            actor, invoice_no=invoice_no, new_rate=D(rate), codes=codes
        )


async def test_changing_the_rate_for_the_whole_bill(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    invoice_no, code_a, code_b = await _bill(session_factory, staff_user)

    result = await _change(session_factory, staff_user, invoice_no, "145")

    # 1800 kg total at 145
    assert result.new_grand_total == D("261000.00")
    assert result.old_rate == D("150.0000")
    assert result.payable_after == D("261000.00")

    async with session_factory() as session:
        rates = [
            row[0]
            for row in (
                await session.execute(sa.text("SELECT rate FROM purchase_lines ORDER BY line_no"))
            ).all()
        ]
        assert rates == [D("145.0000"), D("145.0000")]


async def test_changing_the_rate_for_specific_codes_only(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    """'apply the changed rate to all or specific items codes'."""
    invoice_no, code_a, code_b = await _bill(session_factory, staff_user)

    result = await _change(session_factory, staff_user, invoice_no, "200", [code_a])

    assert result.codes == [code_a]
    # 800 @ 200 + 1000 @ 150 = 310000
    assert result.new_grand_total == D("310000.00")

    async with session_factory() as session:
        rows = {
            row[0]: row[1]
            for row in (
                await session.execute(
                    sa.text(
                        "SELECT p.code, l.rate FROM purchase_lines l "
                        "JOIN products p ON p.id = l.product_id"
                    )
                )
            ).all()
        }
        assert rows[code_a] == D("200.0000")
        assert rows[code_b] == D("150.0000"), "the code that wasn't named is untouched"


async def test_several_codes_at_once(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    invoice_no, code_a, code_b = await _bill(session_factory, staff_user)

    result = await _change(session_factory, staff_user, invoice_no, "100", [code_a, code_b])

    assert sorted(result.codes) == sorted([code_a, code_b])
    assert result.new_grand_total == D("180000.00")


async def test_the_stock_still_on_hand_is_revalued(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    """The goods didn't move; what they cost did."""
    invoice_no, code_a, _ = await _bill(session_factory, staff_user)

    await _change(session_factory, staff_user, invoice_no, "145", [code_a])

    async with session_factory() as session:
        avg, qty = (
            await session.execute(
                sa.text(
                    "SELECT i.weighted_avg_cost, i.qty_on_hand FROM inventory i "
                    "JOIN products p ON p.id = i.product_id WHERE p.code = :c"
                ),
                {"c": code_a},
            )
        ).one()
        assert avg == D("145.0000")
        assert qty == D("800.000"), "no stock moved"


async def test_stock_still_equals_the_sum_of_its_movements(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    """A revaluation posts a zero-quantity movement, so CLAUDE.md's
    standing criterion is untouched."""
    invoice_no, code_a, _ = await _bill(session_factory, staff_user)
    await _change(session_factory, staff_user, invoice_no, "145", [code_a])

    async with session_factory() as session:
        rows = (
            await session.execute(
                sa.text(
                    "SELECT i.qty_on_hand, "
                    "(SELECT sum(qty_delta) FROM inventory_movements m "
                    " WHERE m.product_id = i.product_id) "
                    "FROM inventory i"
                )
            )
        ).all()
        for on_hand, summed in rows:
            assert on_hand == summed


async def test_the_books_balance_after_a_rate_change(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    invoice_no, _, _ = await _bill(session_factory, staff_user)
    await _change(session_factory, staff_user, invoice_no, "145")

    async with session_factory() as session:
        debits, credits = (
            await session.execute(sa.text("SELECT sum(debit), sum(credit) FROM journal_lines"))
        ).one()
        assert debits == credits


async def test_sold_stock_keeps_the_cost_it_was_sold_at(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    """Reaching back through every later sale to re-derive margin is a
    different operation. It is named in the reply rather than done
    silently."""
    invoice_no, code_a, _ = await _bill(session_factory, staff_user)
    async with session_factory() as session, session.begin():
        stock = (
            await session.execute(
                sa.select(Inventory)
                .join(Product, Product.id == Inventory.product_id)
                .where(Product.code == code_a)
            )
        ).scalar_one()
        stock.qty_on_hand = D("300")  # most of it sold

    result = await _change(session_factory, staff_user, invoice_no, "145", [code_a])

    assert code_a in result.partly_sold


async def test_an_unknown_code_on_that_bill_is_named(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    invoice_no, code_a, _ = await _bill(session_factory, staff_user)

    with pytest.raises(NotFoundError):
        await _change(session_factory, staff_user, invoice_no, "145", ["NOSUCH"])


async def test_a_rate_of_zero_is_refused(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    invoice_no, _, _ = await _bill(session_factory, staff_user)

    with pytest.raises(ValidationError, match="more than zero"):
        await _change(session_factory, staff_user, invoice_no, "0")


def test_the_command_takes_several_codes() -> None:
    from backend.api.commands.rate_commands import parse_rate

    assert parse_rate("001 145") == ("001", D("145"), [])
    assert parse_rate("001 145 35A 22D cpk") == ("001", D("145"), ["35A", "22D", "CPK"])
    assert parse_rate("001 1,450") == ("001", D("1450"), [])

    with pytest.raises(ValidationError, match="isn't a rate"):
        parse_rate("001 abc")
    with pytest.raises(ValidationError):
        parse_rate("001")
