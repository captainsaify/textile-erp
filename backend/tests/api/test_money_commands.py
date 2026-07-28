"""expense/income/cash/bank -- grammar, postings (ledger + balanced
journal + audit in one transaction), partner-paid edge case
(docs/06_Accounting.md §3, §13)."""

from __future__ import annotations

import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import RequestContext
from backend.api.commands.money_commands import (
    handle_bank,
    handle_cash,
    handle_expense,
    handle_income,
    parse_money_command,
)
from backend.core.exceptions import ValidationError
from backend.models import Partner, User
from backend.tests.conftest import SEEDED_ORG_ID, purge_business_rows


@pytest.fixture
def ctx(staff_user: User, session_factory: async_sessionmaker[AsyncSession]) -> RequestContext:
    return RequestContext(user=staff_user, session_factory=session_factory)


@pytest.fixture(autouse=True)
async def clean_money_tables(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    # no fixture dependencies: instantiated first, so its teardown runs
    # last -- after staff_user/partner teardowns
    yield
    await purge_business_rows(session_factory)


@pytest.fixture
async def partner(
    staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[Partner]:
    async with session_factory() as session:
        row = Partner(
            org_id=uuid.UUID(SEEDED_ORG_ID),
            display_name=f"Rahul{uuid.uuid4().hex[:6]}",
            profit_share_percent=decimal.Decimal("50"),
            created_by=staff_user.id,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    yield row  # cleanup via clean_money_tables purge


def test_parse_money_command() -> None:
    parsed = parse_money_command("transport 1500 cash loading charges", "expense")
    assert parsed.category == "transport"
    assert parsed.amount == decimal.Decimal("1500")
    assert parsed.via == "cash"
    assert parsed.description == "loading charges"
    assert parsed.paid_by is None

    parsed = parse_money_command("transport 1500 cash paid by Rahul", "expense")
    assert parsed.paid_by == "Rahul"
    assert parsed.description is None

    with pytest.raises(ValidationError, match="Usage"):
        parse_money_command("transport 1500", "expense")
    with pytest.raises(ValidationError, match="isn't a number"):
        parse_money_command("transport abc cash", "expense")
    with pytest.raises(ValidationError, match="cash or bank"):
        parse_money_command("transport 1500 upi", "expense")


async def test_expense_posts_ledger_journal_audit(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    result = await handle_expense("transport 1500 cash loading charges", ctx)
    assert "✅ Expense recorded — Transport ₹1,500.00 (cash)." in result.reply
    assert "Cash balance now -₹1,500.00." in result.reply

    async with session_factory() as session:
        expense = (
            await session.execute(
                sa.text("SELECT id, category, amount FROM expenses WHERE org_id = :org"),
                {"org": SEEDED_ORG_ID},
            )
        ).one()
        assert expense.category == "transport"
        assert expense.amount == decimal.Decimal("1500.00")

        ledger = (
            await session.execute(
                sa.text(
                    "SELECT amount, resulting_balance, entry_type::text FROM cash_ledger "
                    "WHERE org_id = :org"
                ),
                {"org": SEEDED_ORG_ID},
            )
        ).one()
        assert ledger.amount == decimal.Decimal("-1500.00")
        assert ledger.resulting_balance == decimal.Decimal("-1500.00")
        assert ledger.entry_type == "expense"

        debits, credits = (
            await session.execute(
                sa.text(
                    "SELECT SUM(jl.debit), SUM(jl.credit) FROM journal_lines jl "
                    "JOIN journal j ON j.id = jl.journal_id WHERE j.org_id = :org"
                ),
                {"org": SEEDED_ORG_ID},
            )
        ).one()
        assert debits == credits == decimal.Decimal("1500.00")

        audit_action = (
            await session.execute(
                sa.text("SELECT action FROM audit_logs WHERE org_id = :org"),
                {"org": SEEDED_ORG_ID},
            )
        ).scalar_one()
        assert audit_action == "expense.created"


async def test_income_increases_balance_after_expense(
    ctx: RequestContext,
) -> None:
    await handle_income("interest 300 bank", ctx)
    result = await handle_bank("", ctx)
    assert "🏦 Bank balance: ₹300.00" in result.reply
    assert "income +₹300.00 — interest" in result.reply

    await handle_expense("rent 100 bank", ctx)
    result = await handle_bank("", ctx)
    assert "🏦 Bank balance: ₹200.00" in result.reply


async def test_partner_paid_expense_skips_business_ledgers(
    ctx: RequestContext,
    partner: Partner,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result = await handle_expense(f"transport 900 cash paid by {partner.display_name}", ctx)
    assert f"(paid by {partner.display_name})" in result.reply
    assert f"{partner.display_name}'s capital balance now ₹900.00." in result.reply

    async with session_factory() as session:
        cash_rows = (
            await session.execute(
                sa.text("SELECT count(*) FROM cash_ledger WHERE org_id = :org"),
                {"org": SEEDED_ORG_ID},
            )
        ).scalar_one()
        assert cash_rows == 0
        capital = (
            await session.execute(
                sa.text("SELECT amount, entry_type::text FROM partner_capital WHERE org_id = :org"),
                {"org": SEEDED_ORG_ID},
            )
        ).one()
        assert capital.amount == decimal.Decimal("900.00")
        assert capital.entry_type == "contribution"


async def test_unknown_partner_rejected(ctx: RequestContext) -> None:
    result = await handle_expense("transport 900 cash paid by Nobody", ctx)
    assert "not found" in result.reply


async def test_cash_empty_state(ctx: RequestContext) -> None:
    result = await handle_cash("", ctx)
    assert "💵 Cash balance: ₹0.00" in result.reply
    assert "No entries yet." in result.reply


async def test_expense_rejects_bad_amounts(ctx: RequestContext) -> None:
    result = await handle_expense("transport -5 cash", ctx)
    assert "greater than zero" in result.reply
    result = await handle_expense("transport 1.999 cash", ctx)
    assert "2 decimal places" in result.reply


# --------------------------------------------------------------------
# amount parsing -- from a real session where eight attempts in a row
# were rejected with an identical, unhelpful usage line
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "supplier: wagdia 4000000 cash ref 001",
        "supplier wagdia 40,00,000 cash",
        "Supplier: wagdia 40,00,000 cash ref 001",
        "wagdia 4000000 cash",
        "Wagdia Textiles 40,92,000 bank against INV-001",
    ],
)
def test_every_real_world_paid_phrasing_parses(text: str) -> None:
    from backend.api.commands.settlement_commands import parse_settlement

    command = parse_settlement(text, "paid")
    assert command.party.lower().startswith("wagdia")
    assert command.via in {"cash", "bank"}
    assert command.amount > 0


def test_indian_digit_grouping_is_accepted_because_we_print_it() -> None:
    """fmt_money renders ₹40,92,000.00; refusing that back as input is
    the system contradicting itself."""
    from backend.api.amounts import parse_amount

    assert parse_amount("40,92,000") == decimal.Decimal("4092000.00")
    assert parse_amount("₹1,23,456.78") == decimal.Decimal("123456.78")


@pytest.mark.parametrize(
    ("text", "amount", "party", "against"),
    [
        # the one that actually posted money: a bare invoice number after
        # the method was read as the amount, so ₹40,00,000 became ₹1.00
        # and "4000000" was absorbed into the supplier name -- reported
        # back as success
        ("create all products 4000000 cash 001", "4000000.00", "create all products", "001"),
        ("Wagdia 40000 cash 001", "40000.00", "Wagdia", "001"),
        # method and amount swapped: still unambiguous, so still accepted
        ("Wagdia cash 40000", "40000.00", "Wagdia", None),
        # a party whose name ends in a number keeps it
        ("Wagdia 2 40000 cash", "40000.00", "Wagdia 2", None),
    ],
)
def test_amount_is_never_taken_from_after_the_payment_method(
    text: str, amount: str, party: str, against: str | None
) -> None:
    from backend.api.commands.settlement_commands import parse_settlement

    command = parse_settlement(text, "paid")
    assert command.amount == decimal.Decimal(amount)
    assert command.party == party
    assert command.against == against


def test_unexplained_trailing_words_are_refused_rather_than_guessed() -> None:
    """Two stray tokens after the method could be anything. Guessing here
    is how ₹1.00 got posted; naming the problem is the alternative."""
    from backend.api.commands.settlement_commands import parse_settlement

    with pytest.raises(ValidationError, match="don't know what '001 002' means"):
        parse_settlement("Wagdia 40000 cash 001 002", "paid")


def test_amount_errors_name_the_problem_not_just_the_usage() -> None:
    from backend.api.commands.settlement_commands import parse_settlement

    with pytest.raises(ValidationError, match="cash or bank"):
        parse_settlement("wagdia 40000", "paid")
    with pytest.raises(ValidationError, match="couldn't find an amount"):
        parse_settlement("wagdia cash", "paid")


def test_expense_and_capital_accept_grouped_amounts() -> None:
    from backend.api.commands.capital_commands import parse_capital_command

    expense = parse_money_command("transport 1,500 cash", "expense")
    assert expense.amount == decimal.Decimal("1500.00")
    capital = parse_capital_command("Rahul 5,00,000 bank", usage="u")
    assert capital.amount == decimal.Decimal("500000.00")
