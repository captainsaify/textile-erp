"""Reversing a payment -- docs/25_PaymentReversals.md.

A settlement does two things: it moves money, and it marks bills as
settled. Reversing only the first leaves bills showing paid that nobody
paid -- the payable understated, which is the direction that loses money
without anyone noticing. Both are unwound in one transaction.
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
from backend.models import PurchaseHeader, Supplier, User
from backend.services.settlement_service import PaymentReversalService, SettlementService
from backend.tests.conftest import (
    SEEDED_MAIN_WAREHOUSE_ID,
    SEEDED_ORG_ID,
    purge_business_rows,
)

D = decimal.Decimal
ORG = uuid.UUID(SEEDED_ORG_ID)


@pytest.fixture(autouse=True)
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    await purge_business_rows(session_factory)


async def _bill(
    session_factory: async_sessionmaker[AsyncSession], actor: User, *, total: str = "10000"
) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:5]
    async with session_factory() as session:
        supplier = Supplier(org_id=ORG, name=f"Wagdia {suffix}", created_by=actor.id)
        session.add(supplier)
        await session.flush()
        session.add(
            PurchaseHeader(
                org_id=ORG,
                supplier_id=supplier.id,
                warehouse_id=uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID),
                invoice_no=f"INV-{suffix}",
                invoice_date=datetime.date.today(),
                subtotal=D(total),
                grand_total=D(total),
                status="confirmed",
                created_by=actor.id,
            )
        )
        await session.commit()
    return f"Wagdia {suffix}", f"INV-{suffix}"


async def _pay(
    session_factory: async_sessionmaker[AsyncSession], actor: User, supplier: str, amount: str
) -> str:
    async with session_factory() as session:
        await SettlementService(session).pay_supplier(
            actor, supplier_name=supplier, amount=D(amount), via="cash", against=None
        )
    async with session_factory() as session:
        row = (
            await session.execute(
                sa.text(
                    "SELECT id FROM audit_logs WHERE action = 'payment.paid' "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).first()
        assert row is not None
        return str(row[0])[:8]


async def test_reversing_takes_the_money_back_off_the_bill_too(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    """The part that matters: the bill reopens. Reversing only the ledger
    would leave it marked settled by money that has been taken back."""
    supplier, invoice = await _bill(session_factory, staff_user, total="10000")
    reference = await _pay(session_factory, staff_user, supplier, "4000")

    async with session_factory() as session:
        paid_before = (
            await session.execute(sa.text("SELECT amount_paid FROM purchase_headers"))
        ).scalar_one()
        assert paid_before == D("4000.00")

    async with session_factory() as session:
        result = await PaymentReversalService(session).reverse(staff_user, reference=reference)

    assert result.kind == "paid"
    assert result.amount == D("4000.00")
    assert invoice in " ".join(result.unapplied)

    async with session_factory() as session:
        paid_after, status = (
            await session.execute(
                sa.text("SELECT amount_paid, payment_status FROM purchase_headers")
            )
        ).one()
        assert paid_after == D("0.00")
        assert status == "unpaid"
        # and the payable is whole again
        assert result.outstanding_after == D("10000.00")


async def test_the_cash_comes_back_and_the_books_balance(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    supplier, _ = await _bill(session_factory, staff_user)
    reference = await _pay(session_factory, staff_user, supplier, "4000")

    async with session_factory() as session:
        cash_before = (
            await session.execute(sa.text("SELECT sum(amount) FROM cash_ledger"))
        ).scalar_one()

    async with session_factory() as session:
        await PaymentReversalService(session).reverse(staff_user, reference=reference)

    async with session_factory() as session:
        cash_after = (
            await session.execute(sa.text("SELECT sum(amount) FROM cash_ledger"))
        ).scalar_one()
        assert cash_after == cash_before + D("4000.00")

        debits, credits = (
            await session.execute(sa.text("SELECT sum(debit), sum(credit) FROM journal_lines"))
        ).one()
        assert debits == credits


async def test_a_payment_cannot_be_reversed_twice(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    """Otherwise the money comes back as many times as you ask."""
    supplier, _ = await _bill(session_factory, staff_user)
    reference = await _pay(session_factory, staff_user, supplier, "4000")

    async with session_factory() as session:
        await PaymentReversalService(session).reverse(staff_user, reference=reference)
    async with session_factory() as session:
        with pytest.raises(ValidationError, match="already been reversed"):
            await PaymentReversalService(session).reverse(staff_user, reference=reference)


async def test_a_partial_payment_leaves_the_bill_partial(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    """Two payments, one reversed: the other must still count."""
    supplier, _ = await _bill(session_factory, staff_user, total="10000")
    first = await _pay(session_factory, staff_user, supplier, "3000")
    await _pay(session_factory, staff_user, supplier, "2000")

    async with session_factory() as session:
        await PaymentReversalService(session).reverse(staff_user, reference=first)

    async with session_factory() as session:
        paid, status = (
            await session.execute(
                sa.text("SELECT amount_paid, payment_status FROM purchase_headers")
            )
        ).one()
        assert paid == D("2000.00"), "the payment that wasn't reversed still stands"
        assert status == "partial"


async def test_an_unknown_payment_is_named(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    async with session_factory() as session:
        with pytest.raises(NotFoundError):
            await PaymentReversalService(session).reverse(staff_user, reference="deadbeef")


def test_payment_is_offered_and_routed_like_the_other_reversals() -> None:
    from backend.api.commands import wizards

    assert "payment" in wizards.REVERSIBLE_ENTITIES
    assert wizards.WIZARDS["delete"].reroute({"entity": "payment", "reference": "ab12cd34"}) == (
        "undo",
        "payment ab12cd34",
    )


def test_a_bill_reference_asks_for_a_reference_not_a_product_code() -> None:
    """ "Give its code or name, e.g. TRP" is right for a product and
    wrong for everything else the same slot serves."""
    from backend.api.commands.wizards import _reference_wording

    for entity in ("purchase", "sale", "expense", "payment"):
        question, example = _reference_wording({"entity": entity})
        assert f"Which {entity}?" in question
        assert "TRP" not in example

    question, example = _reference_wording({"entity": "product"})
    assert "code or name" in question
    assert "TRP" in example


# --------------------------------------------------------------------
# undoing a bill that still has money on it
# --------------------------------------------------------------------


async def test_undoing_a_paid_bill_asks_what_to_do_with_the_money(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    """Both silent answers are wrong: reversing the payment takes back
    money that really was sent, and leaving it orphans a receipt against
    a bill that no longer exists."""
    from backend.api.command_types import RequestContext
    from backend.api.commands.correction_commands import handle_undo
    from backend.api.interactive import Buttons
    from backend.services.session_service import (
        AWAITING_UNDO_PAYMENT_CHOICE,
        SessionService,
    )

    supplier, invoice = await _bill(session_factory, staff_user, total="10000")
    await _pay(session_factory, staff_user, supplier, "4000")
    ctx = RequestContext(user=staff_user, session_factory=session_factory)

    result = await handle_undo(f"purchase {invoice}", ctx)

    assert isinstance(result.interactive, Buttons)
    assert "4,000" in result.reply
    assert [c.title for c in result.interactive.choices] == [
        "Reverse both",
        "Keep the money",
        "Cancel",
    ]
    state = await SessionService(session_factory).get(ORG, staff_user.id)
    assert state.state == AWAITING_UNDO_PAYMENT_CHOICE
    assert state.context["reference"] == invoice


async def test_cancelling_that_question_changes_nothing(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    from backend.api.command_types import RequestContext
    from backend.api.commands import undo_payment_choice
    from backend.api.commands.correction_commands import handle_undo
    from backend.services.session_service import IDLE, SessionService

    supplier, invoice = await _bill(session_factory, staff_user, total="10000")
    await _pay(session_factory, staff_user, supplier, "4000")
    ctx = RequestContext(user=staff_user, session_factory=session_factory)
    await handle_undo(f"purchase {invoice}", ctx)

    state = await SessionService(session_factory).get(ORG, staff_user.id)
    result = await undo_payment_choice.handle_choice("undo cancel", ctx, state)

    assert "Left everything as it was" in result.reply
    assert (await SessionService(session_factory).get(ORG, staff_user.id)).state == IDLE
    async with session_factory() as session:
        paid = (
            await session.execute(sa.text("SELECT amount_paid FROM purchase_headers"))
        ).scalar_one()
        assert paid == D("4000.00"), "the payment is untouched"


async def test_an_unpaid_bill_is_undone_without_the_question(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    """The question only exists because money is involved."""
    from backend.api.command_types import RequestContext
    from backend.api.commands.correction_commands import handle_undo

    _, invoice = await _bill(session_factory, staff_user, total="10000")
    ctx = RequestContext(user=staff_user, session_factory=session_factory)

    result = await handle_undo(f"purchase {invoice}", ctx)

    assert result.interactive is None


# --------------------------------------------------------------------
# backdating -- docs/06_Accounting.md, ledgers copied from a paper book
# --------------------------------------------------------------------


async def test_a_payment_can_be_filed_under_the_day_it_actually_moved(
    owner_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A month of payments entered in one sitting all landed on today,
    which put every one of them in the wrong cash-flow period."""
    from backend.api.command_types import RequestContext
    from backend.api.commands.settlement_commands import handle_paid
    from backend.models import CashLedger

    supplier, _ = await _bill(session_factory, owner_user, total="600000")
    ctx = RequestContext(user=owner_user, session_factory=session_factory, message_id="m-date")

    result = await handle_paid(f"{supplier} 600000 cash on 28-07-2026", ctx)

    assert "Payment made" in result.reply
    async with session_factory() as session:
        entry_date = (
            await session.execute(sa.select(CashLedger.entry_date).where(CashLedger.org_id == ORG))
        ).scalar_one()
    assert entry_date == datetime.date(2026, 7, 28)


async def test_a_payment_dated_in_the_future_is_refused(
    owner_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from backend.api.command_types import RequestContext
    from backend.api.commands.settlement_commands import handle_paid

    supplier, _ = await _bill(session_factory, owner_user)
    ctx = RequestContext(user=owner_user, session_factory=session_factory, message_id="m-future")
    tomorrow = (datetime.date.today() + datetime.timedelta(days=400)).strftime("%d-%m-%Y")

    result = await handle_paid(f"{supplier} 5000 cash on {tomorrow}", ctx)

    assert "in the future" in result.reply


async def test_a_supplier_whose_name_holds_on_keeps_it(
    owner_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`on` is only a date clause when a date follows it -- otherwise
    "Delivery on Time" would lose two words out of its name."""
    from backend.api.commands.settlement_commands import parse_settlement

    command = parse_settlement("Delivery on Time Traders 5000 cash", "paid")

    assert command.party == "Delivery on Time Traders"
    assert command.on is None
