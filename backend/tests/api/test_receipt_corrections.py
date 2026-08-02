"""Receipt corrections -- docs/23_ReceiptCorrections.md.

The partners' own example: 35A billed as 10 bales × 80 kg = 800 kg, and
only 9 turn up. Every figure that depends on that weight has to move
together -- the line, the invoice total, the payable, the stock and the
books. A correction that updates four of the five is worse than one that
updates none, because the disagreement is silent.
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
from backend.models import (
    Inventory,
    Product,
    PurchaseHeader,
    PurchaseLine,
    Supplier,
    User,
)
from backend.services.inventory_service import InventoryService
from backend.services.receipt_correction_service import (
    CorrectionResult,
    ReceiptCorrectionService,
)
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


async def _purchase(
    session_factory: async_sessionmaker[AsyncSession],
    actor: User,
    *,
    freight: str = "0",
    rate: str = "150",
) -> tuple[str, str]:
    """35A: 10 bales × 80 kg = 800 kg, exactly as the sheet reads."""
    suffix = uuid.uuid4().hex[:5]
    invoice_no = f"INV-{suffix}"
    code = f"35A{suffix.upper()}"

    async with session_factory() as session, session.begin():
        supplier = Supplier(org_id=ORG, name=f"Wagdia {suffix}", created_by=actor.id)
        product = Product(
            org_id=ORG,
            product_type_id=uuid.UUID(SEEDED_TEXTILE_TYPE_ID),
            code=code,
            description="Men Zipper Jacket",
            unit_id=uuid.UUID(SEEDED_KG_UNIT_ID),
            created_by=actor.id,
        )
        session.add_all([supplier, product])
        await session.flush()

        line_total = D("800") * D(rate)
        header = PurchaseHeader(
            org_id=ORG,
            supplier_id=supplier.id,
            warehouse_id=WAREHOUSE,
            invoice_no=invoice_no,
            invoice_date=datetime.date.today(),
            freight=D(freight),
            other_charges=D("0"),
            subtotal=line_total,
            grand_total=line_total + D(freight),
            status="confirmed",
            created_by=actor.id,
        )
        session.add(header)
        await session.flush()

        landed = ((line_total + D(freight)) / D("800")).quantize(D("0.0001"))
        line = PurchaseLine(
            org_id=ORG,
            purchase_header_id=header.id,
            line_no=1,
            product_id=product.id,
            description="Men Zipper Jacket",
            qty=D("800"),
            weight_kg=D("80"),
            total_weight_kg=D("800"),
            rate=D(rate),
            line_total=line_total,
            freight_allocated=D(freight),
            landed_cost_per_unit=landed,
        )
        session.add(line)
        await session.flush()
        await InventoryService(session).record_purchase_movement(
            ORG,
            product_id=product.id,
            warehouse_id=WAREHOUSE,
            qty=D("800"),
            landed_cost_per_unit=landed,
            source_id=line.id,
            created_by=actor.id,
        )
    return invoice_no, code


async def _second_line(
    session_factory: async_sessionmaker[AsyncSession],
    actor: User,
    invoice_no: str,
    *,
    rate: str = "150",
) -> str:
    """A second item on the same bill: 22D, 5 bales × 80 kg = 400 kg."""
    suffix = uuid.uuid4().hex[:5]
    code = f"22D{suffix.upper()}"
    async with session_factory() as session, session.begin():
        header = (
            await session.execute(
                sa.select(PurchaseHeader).where(PurchaseHeader.invoice_no == invoice_no)
            )
        ).scalar_one()
        product = Product(
            org_id=ORG,
            product_type_id=uuid.UUID(SEEDED_TEXTILE_TYPE_ID),
            code=code,
            description="Men Zipper Jacket B",
            unit_id=uuid.UUID(SEEDED_KG_UNIT_ID),
            created_by=actor.id,
        )
        session.add(product)
        await session.flush()

        line_total = D("400") * D(rate)
        landed = (line_total / D("400")).quantize(D("0.0001"))
        line = PurchaseLine(
            org_id=ORG,
            purchase_header_id=header.id,
            line_no=2,
            product_id=product.id,
            description="Men Zipper Jacket B",
            qty=D("400"),
            weight_kg=D("80"),
            total_weight_kg=D("400"),
            rate=D(rate),
            line_total=line_total,
            freight_allocated=D("0"),
            landed_cost_per_unit=landed,
        )
        session.add(line)
        header.subtotal += line_total
        header.grand_total += line_total
        await session.flush()
        await InventoryService(session).record_purchase_movement(
            ORG,
            product_id=product.id,
            warehouse_id=WAREHOUSE,
            qty=D("400"),
            landed_cost_per_unit=landed,
            source_id=line.id,
            created_by=actor.id,
        )
    return code


async def _correct(
    session_factory: async_sessionmaker[AsyncSession],
    actor: User,
    invoice_no: str,
    code: str,
    pieces: str,
) -> CorrectionResult:
    async with session_factory() as session, session.begin():
        return await ReceiptCorrectionService(session).correct(
            actor, invoice_no=invoice_no, code=code, received_pieces=D(pieces)
        )


async def test_one_bale_short_moves_every_figure_together(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    """The partners' example: 10 × 80 = 800 billed, 9 × 80 = 720 arrived."""
    invoice_no, code = await _purchase(session_factory, staff_user)

    result = await _correct(session_factory, staff_user, invoice_no, code, "9")

    assert result.old_qty == D("800")
    assert result.new_qty == D("720")
    assert result.old_grand_total == D("120000.00")
    assert result.new_grand_total == D("108000.00")  # 720 × 150
    assert result.payable_after == D("108000.00")

    async with session_factory() as session:
        line = (
            await session.execute(
                sa.text("SELECT qty, total_weight_kg, line_total FROM purchase_lines")
            )
        ).one()
        assert line.qty == D("720.000")
        assert line.total_weight_kg == D("720.000")
        assert line.line_total == D("108000.00")

        stock = (
            await session.execute(sa.select(Inventory).where(Inventory.org_id == ORG))
        ).scalar_one()
        assert stock.qty_on_hand == D("720.000")


async def test_stock_still_equals_the_sum_of_its_movements(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    """CLAUDE.md's standing acceptance criterion. A correction that
    edited qty_on_hand directly would break the nightly reconciliation
    silently."""
    invoice_no, code = await _purchase(session_factory, staff_user)
    await _correct(session_factory, staff_user, invoice_no, code, "9")

    async with session_factory() as session:
        on_hand = (await session.execute(sa.text("SELECT qty_on_hand FROM inventory"))).scalar_one()
        summed = (
            await session.execute(sa.text("SELECT sum(qty_delta) FROM inventory_movements"))
        ).scalar_one()
        assert on_hand == summed

        kinds = [
            row[0]
            for row in (
                await session.execute(
                    sa.text("SELECT movement_type FROM inventory_movements ORDER BY created_at")
                )
            ).all()
        ]
        # the correction reads as a correction, not as goods physically
        # going back to the supplier
        assert kinds == ["purchase", "adjustment_decrease"]


async def test_the_books_balance_after_a_correction(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    invoice_no, code = await _purchase(session_factory, staff_user)
    await _correct(session_factory, staff_user, invoice_no, code, "9")

    async with session_factory() as session:
        debits, credits = (
            await session.execute(sa.text("SELECT sum(debit), sum(credit) FROM journal_lines"))
        ).one()
        assert debits == credits, "double entry must still balance"

        # the fixture builds the purchase rows directly, so the only
        # journal entry here is the correction itself -- what it must
        # show is the 12,000 coming *off* the payable
        payable_delta = (
            await session.execute(
                sa.text(
                    "SELECT sum(credit) - sum(debit) FROM journal_lines "
                    "WHERE account_code = 'accounts_payable'"
                )
            )
        ).scalar_one()
        assert payable_delta == D("-12000.00")


async def test_extra_bales_are_added_by_the_same_logic(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    """'if stock comes more, it will be added like the same logic'."""
    invoice_no, code = await _purchase(session_factory, staff_user)

    result = await _correct(session_factory, staff_user, invoice_no, code, "12")

    assert result.new_qty == D("960")  # 12 × 80
    assert result.new_grand_total == D("144000.00")  # 960 × 150
    async with session_factory() as session:
        stock = (await session.execute(sa.text("SELECT qty_on_hand FROM inventory"))).scalar_one()
        assert stock == D("960.000")


async def test_freight_is_re_split_but_the_charge_itself_does_not_change(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    """The transporter charged what they charged; a missing bale doesn't
    refund freight. Only its share across the invoice moves."""
    invoice_no, code = await _purchase(session_factory, staff_user, freight="1000")

    result = await _correct(session_factory, staff_user, invoice_no, code, "9")

    # 720 × 150 + 1000 freight
    assert result.new_grand_total == D("109000.00")
    async with session_factory() as session:
        freight = (
            await session.execute(sa.text("SELECT freight FROM purchase_headers"))
        ).scalar_one()
        assert freight == D("1000.00")


async def test_correcting_to_what_it_already_says_is_refused(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    invoice_no, code = await _purchase(session_factory, staff_user)

    with pytest.raises(ValidationError, match="Nothing to change"):
        await _correct(session_factory, staff_user, invoice_no, code, "10")


async def test_an_unknown_invoice_or_code_is_named(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    from backend.core.exceptions import NotFoundError

    invoice_no, code = await _purchase(session_factory, staff_user)

    with pytest.raises(NotFoundError):
        await _correct(session_factory, staff_user, "NOPE-1", code, "9")
    with pytest.raises(NotFoundError):
        await _correct(session_factory, staff_user, invoice_no, "NOSUCH", "9")


async def test_overpayment_is_flagged_rather_than_hidden(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    """Paying the full 1,20,000 and then correcting to 1,08,000 leaves
    12,000 sitting with the supplier. Saying nothing would let someone
    pay it twice."""
    invoice_no, code = await _purchase(session_factory, staff_user)
    async with session_factory() as session, session.begin():
        await session.execute(sa.text("UPDATE purchase_headers SET amount_paid = 120000.00"))

    result = await _correct(session_factory, staff_user, invoice_no, code, "9")

    assert result.now_overpaid is True
    assert result.payable_after < D("0")


def test_the_command_speaks_in_bales() -> None:
    from backend.api.commands.receipt_commands import parse_receive

    invoice_no, corrections = parse_receive("001 35A 9")
    assert (invoice_no, corrections) == ("001", [("35A", D("9"))])

    with pytest.raises(ValidationError, match="isn't a number of bales"):
        parse_receive("001 35A nine")
    with pytest.raises(ValidationError):
        parse_receive("001 35A")


async def test_two_short_lines_go_in_together_and_the_bill_is_stated_once(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    """Both corrections land, and the reply quotes one invoice total --
    the one true after both, not the two intermediate figures."""
    from backend.api.command_types import RequestContext
    from backend.api.commands.receipt_commands import handle_receive

    invoice_no, code = await _purchase(session_factory, staff_user)
    second = await _second_line(session_factory, staff_user, invoice_no)
    ctx = RequestContext(user=staff_user, session_factory=session_factory)

    result = await handle_receive(f"{invoice_no} {code} 9 {second} 4", ctx)

    assert "2 lines corrected" in result.reply
    assert code in result.reply and second in result.reply
    # 9×80×150 + 4×80×150 = 108000 + 48000
    assert "1,56,000.00" in result.reply
    assert result.reply.count("Invoice total:") == 1

    async with session_factory() as session:
        rows = (
            await session.execute(
                sa.select(Product.code, PurchaseLine.qty).join(
                    Product, Product.id == PurchaseLine.product_id
                )
            )
        ).all()
    quantities: dict[str, decimal.Decimal] = {row[0]: row[1] for row in rows}
    assert quantities[code] == D("720.000")
    assert quantities[second] == D("320.000")


async def test_a_failure_on_the_second_line_leaves_the_first_alone(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    """One truck is one transaction. Half-applied would leave the bill
    disagreeing with the stock behind it."""
    from backend.api.command_types import RequestContext
    from backend.api.commands.receipt_commands import handle_receive

    invoice_no, code = await _purchase(session_factory, staff_user)
    ctx = RequestContext(user=staff_user, session_factory=session_factory)

    result = await handle_receive(f"{invoice_no} {code} 9 NOSUCH 4", ctx)

    assert "NOSUCH" in result.reply
    async with session_factory() as session:
        qty = (await session.execute(sa.select(PurchaseLine.qty))).scalar_one()
    assert qty == D("800.000"), "the first line was rolled back with the second"


def test_one_truck_is_one_command() -> None:
    """Several lines short off the same delivery is one event, and used
    to be one command each -- with the invoice number retyped every
    time, which is how the second one gets skipped."""
    from backend.api.commands.receipt_commands import parse_receive

    invoice_no, corrections = parse_receive("001 35A 9 22D 4 CPK 0")
    assert invoice_no == "001"
    assert corrections == [("35A", D("9")), ("22D", D("4")), ("CPK", D("0"))]

    # a code with nothing after it is a half-given answer, not a zero
    with pytest.raises(ValidationError, match="no count after it"):
        parse_receive("001 35A 9 22D")
    # two counts for one line are two different claims about what arrived
    with pytest.raises(ValidationError, match="listed twice"):
        parse_receive("001 35A 9 35a 4")
