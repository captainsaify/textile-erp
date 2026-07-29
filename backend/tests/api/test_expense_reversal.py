"""Reversing an expense, and reversing a sale by its own reference.

Money that already moved is put back with compensating entries, never
removed by deleting a row. The partner-paid case matters most: the
business cash was never touched, so reversing it as a cash refund would
invent money the business never held.
"""

from __future__ import annotations

import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import RequestContext
from backend.core.exceptions import NotFoundError, ValidationError
from backend.models import Partner, User
from backend.services.money_service import MoneyService
from backend.tests.conftest import SEEDED_ORG_ID, purge_business_rows

D = decimal.Decimal
ORG = uuid.UUID(SEEDED_ORG_ID)


@pytest.fixture(autouse=True)
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    await purge_business_rows(session_factory)


@pytest.fixture
def ctx(staff_user: User, session_factory: async_sessionmaker[AsyncSession]) -> RequestContext:
    return RequestContext(user=staff_user, session_factory=session_factory, message_id="m1")


async def _record(
    session_factory: async_sessionmaker[AsyncSession],
    actor: User,
    *,
    category: str = "transport",
    amount: str = "1500",
    partner_name: str | None = None,
) -> str:
    async with session_factory() as session:
        await MoneyService(session).record_expense(
            actor,
            category=category,
            amount=D(amount),
            via="cash",
            description=None,
            paid_by_partner_name=partner_name,
        )
    async with session_factory() as session:
        row = (
            await session.execute(
                sa.text("SELECT id FROM expenses WHERE category = :c ORDER BY created_at DESC"),
                {"c": category},
            )
        ).first()
        assert row is not None
        return str(row[0])[:8]


async def test_reversing_puts_the_cash_back_and_keeps_the_history(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    reference = await _record(session_factory, ctx.user)

    async with session_factory() as session:
        before = (
            await session.execute(sa.text("SELECT sum(amount) FROM cash_ledger"))
        ).scalar_one()

    async with session_factory() as session:
        result = await MoneyService(session).reverse_expense(ctx.user, reference=reference)

    assert result.amount == D("1500.00")
    assert result.paid_by_partner is False

    async with session_factory() as session:
        after = (await session.execute(sa.text("SELECT sum(amount) FROM cash_ledger"))).scalar_one()
        assert after == before + D("1500.00"), "the money went back"

        # the row is soft-deleted, not gone
        alive, total = (
            await session.execute(
                sa.text("SELECT count(*) FILTER (WHERE deleted_at IS NULL), count(*) FROM expenses")
            )
        ).one()
        assert (alive, total) == (0, 1)

        debits, credits = (
            await session.execute(sa.text("SELECT sum(debit), sum(credit) FROM journal_lines"))
        ).one()
        assert debits == credits, "double entry still balances"


async def test_a_partner_paid_expense_reverses_against_capital_not_the_till(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The business cash was never touched -- it was an implicit capital
    contribution. Refunding it as cash would invent money the business
    never held."""
    async with session_factory() as session:
        partner = Partner(
            org_id=ORG,
            display_name=f"Firoz{uuid.uuid4().hex[:4]}",
            profit_share_percent=D("50"),
            created_by=ctx.user.id,
        )
        session.add(partner)
        await session.commit()
        partner_name = partner.display_name

    reference = await _record(session_factory, ctx.user, category="rent", partner_name=partner_name)

    async with session_factory() as session:
        cash_before = (
            await session.execute(sa.text("SELECT count(*) FROM cash_ledger"))
        ).scalar_one()

    async with session_factory() as session:
        result = await MoneyService(session).reverse_expense(ctx.user, reference=reference)

    assert result.paid_by_partner is True
    async with session_factory() as session:
        cash_after = (
            await session.execute(sa.text("SELECT count(*) FROM cash_ledger"))
        ).scalar_one()
        assert cash_after == cash_before, "the till was never involved either way"

        entries = [
            row[0]
            for row in (
                await session.execute(
                    sa.text("SELECT entry_type FROM partner_capital ORDER BY created_at")
                )
            ).all()
        ]
        assert entries == ["contribution", "withdrawal"]


async def test_an_ambiguous_category_is_refused_rather_than_guessed(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Two 'transport' expenses and no reference: resolving that to "the
    latest" is how the wrong one gets reversed."""
    await _record(session_factory, ctx.user, amount="100")
    await _record(session_factory, ctx.user, amount="200")

    async with session_factory() as session:
        with pytest.raises(ValidationError, match="2 'transport' expenses"):
            await MoneyService(session).reverse_expense(ctx.user, reference="transport")


async def test_a_single_category_match_is_enough(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _record(session_factory, ctx.user, category="tea", amount="50")

    async with session_factory() as session:
        result = await MoneyService(session).reverse_expense(ctx.user, reference="tea")
    assert result.amount == D("50.00")


async def test_an_unknown_expense_is_named(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        with pytest.raises(NotFoundError):
            await MoneyService(session).reverse_expense(ctx.user, reference="nosuch")


async def test_reversing_twice_is_refused(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The first reversal soft-deletes it, so the second finds nothing --
    which is what stops the money going back twice."""
    reference = await _record(session_factory, ctx.user)

    async with session_factory() as session:
        await MoneyService(session).reverse_expense(ctx.user, reference=reference)
    async with session_factory() as session:
        with pytest.raises(NotFoundError):
            await MoneyService(session).reverse_expense(ctx.user, reference=reference)


def test_expense_is_offered_and_routed_like_the_other_reversals() -> None:
    from backend.api.commands import wizards

    assert "expense" in wizards.REVERSIBLE_ENTITIES
    assert wizards.WIZARDS["delete"].reroute({"entity": "expense", "reference": "ab12cd34"}) == (
        "undo",
        "expense ab12cd34",
    )


async def test_a_sale_is_undone_by_its_own_ref_not_the_customer_name(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The picker hands `undo` a short ref. It only understood customer
    names, so it answered "customer ec196ee8 not found" -- and a name
    resolves to "their latest", which is a guess at which sale you
    meant."""
    import datetime

    from backend.models import Customer, SalesHeader
    from backend.models.enums import SalePaymentType
    from backend.services.undo_service import UndoService
    from backend.tests.conftest import SEEDED_MAIN_WAREHOUSE_ID

    suffix = uuid.uuid4().hex[:5]
    async with session_factory() as session:
        customer = Customer(org_id=ORG, name=f"xyz {suffix}", created_by=ctx.user.id)
        session.add(customer)
        await session.flush()
        sale = SalesHeader(
            org_id=ORG,
            customer_id=customer.id,
            warehouse_id=uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID),
            sale_date=datetime.date.today(),
            payment_type=SalePaymentType.CREDIT,
            grand_total=D("15000.00"),
            status="confirmed",
            created_by=ctx.user.id,
        )
        session.add(sale)
        await session.commit()
        reference = str(sale.id)[:8]
        sale_id = sale.id

    async with session_factory() as session:
        resolved = await UndoService(session)._lookup_entity(ORG, "sale", reference)

    assert resolved == sale_id
