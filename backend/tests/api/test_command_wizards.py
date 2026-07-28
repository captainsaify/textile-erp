"""Command wizards -- docs/20_ConversationalIntake.md §7, §12.

A partial command asks for what it needs. The load-bearing test is
equivalence: `expense transport 1500 cash` in one message must reach the
same ledger and journal rows as answering three questions. If those ever
diverge, one of them is wrong.
"""

from __future__ import annotations

import decimal
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import CommandResult, RequestContext
from backend.api.commands import wizards
from backend.api.interactive import Buttons, ListMenu
from backend.models import User
from backend.services.session_service import (
    AWAITING_COMMAND_SLOT,
    IDLE,
    SessionService,
)
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


async def begin(command: str, args: str, ctx: RequestContext) -> CommandResult | None:
    return await wizards.start(wizards.WIZARDS[command], args, ctx)


async def answer(text: str, ctx: RequestContext) -> CommandResult:
    state = await SessionService(ctx.session_factory).get(ctx.user.org_id, ctx.user.id)
    return await wizards.handle_reply(text, ctx, state)


async def state_of(ctx: RequestContext) -> tuple[str, dict[str, Any]]:
    state = await SessionService(ctx.session_factory).get(ctx.user.org_id, ctx.user.id)
    return state.state, dict(state.context)


async def ledger_rows(session_factory: async_sessionmaker[AsyncSession]) -> list[tuple[Any, ...]]:
    async with session_factory() as session:
        rows = (
            await session.execute(
                sa.text(
                    "SELECT entry_type, amount, source_type FROM cash_ledger "
                    "ORDER BY entry_date, amount"
                )
            )
        ).all()
    return [tuple(r) for r in rows]


async def reset_postings(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Clear what an expense writes, and nothing else.

    Not `purge_business_rows`: that removes users too, and the wizard
    under test needs the session's owner to still exist.
    """
    async with session_factory() as session:
        for table in ("journal_lines", "journal", "cash_ledger", "bank_ledger", "expenses"):
            await session.execute(sa.text(f"DELETE FROM {table}"))  # noqa: S608 -- fixed literals
        await session.commit()


async def journal_rows(session_factory: async_sessionmaker[AsyncSession]) -> list[tuple[Any, ...]]:
    async with session_factory() as session:
        rows = (
            await session.execute(
                sa.text(
                    "SELECT l.account_code, l.debit, l.credit FROM journal_lines l "
                    "JOIN journal j ON j.id = l.journal_id "
                    "ORDER BY l.account_code, l.debit"
                )
            )
        ).all()
    return [tuple(r) for r in rows]


# --------------------------------------------------------------------
# asking instead of printing usage
# --------------------------------------------------------------------


async def test_bare_command_asks_the_first_question(ctx: RequestContext) -> None:
    """`paid` used to print a usage line. Eight consecutive real attempts
    were rejected by one (docs/20 §1)."""
    result = await begin("paid", "", ctx)

    assert result is not None
    assert "Which supplier?" in result.reply
    assert "Usage:" not in result.reply
    state, context = await state_of(ctx)
    assert state == AWAITING_COMMAND_SLOT
    assert context["queue"] == ["party", "amount", "method"]


async def test_partial_command_asks_only_for_what_is_missing(ctx: RequestContext) -> None:
    result = await begin("paid", "wagdia", ctx)

    assert result is not None
    assert "How much?" in result.reply
    _, context = await state_of(ctx)
    assert context["filled"] == {"party": "wagdia"}
    assert context["queue"] == ["amount", "method"]


async def test_complete_command_does_not_start_a_wizard(ctx: RequestContext) -> None:
    """A fluent user typing it fully gets one round trip, not four."""
    assert await begin("paid", "wagdia 40000 cash", ctx) is None
    assert await begin("expense", "transport 1500 cash", ctx) is None

    state, _ = await state_of(ctx)
    assert state == IDLE


async def test_the_payment_method_is_offered_as_buttons(ctx: RequestContext) -> None:
    result = await begin("paid", "wagdia 40000", ctx)

    assert result is not None
    assert isinstance(result.interactive, Buttons)
    assert [c.title for c in result.interactive.choices] == ["Cash", "Bank"]


async def test_export_offers_reports_then_a_period(ctx: RequestContext) -> None:
    first = await begin("export", "", ctx)
    assert first is not None
    assert isinstance(first.interactive, Buttons)
    assert [c.title for c in first.interactive.choices] == ["Purchases", "Sales", "Stock"]

    second = await answer("slot purchases", ctx)
    assert isinstance(second.interactive, ListMenu)
    assert "Which period?" in second.reply


# --------------------------------------------------------------------
# equivalence -- the point of the whole design
# --------------------------------------------------------------------


async def test_wizard_and_one_shot_produce_identical_postings(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """docs/20 §12, named explicitly: one message and three answers must
    reach the same ledger and journal rows."""
    from backend.api.commands.money_commands import handle_expense

    await handle_expense("transport 1500 cash", ctx)
    one_shot = (await ledger_rows(session_factory), await journal_rows(session_factory))
    await reset_postings(session_factory)

    await begin("expense", "", ctx)
    await answer("transport", ctx)
    await answer("1500", ctx)
    await answer("slot cash", ctx)
    wizard = (await ledger_rows(session_factory), await journal_rows(session_factory))

    assert wizard == one_shot
    assert wizard[0], "the expense should actually have posted"


async def test_tapped_and_typed_answers_agree(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await begin("expense", "", ctx)
    await answer("transport", ctx)
    await answer("1500", ctx)
    await answer("cash", ctx)  # typed
    typed = await journal_rows(session_factory)
    await reset_postings(session_factory)

    await begin("expense", "", ctx)
    await answer("slot transport", ctx)
    await answer("1500", ctx)
    await answer("slot cash", ctx)  # tapped
    assert await journal_rows(session_factory) == typed


async def test_the_finished_wizard_runs_the_real_command(ctx: RequestContext) -> None:
    await begin("expense", "", ctx)
    await answer("transport", ctx)
    await answer("1500", ctx)
    result = await answer("slot cash", ctx)

    # the reply is the command's own, not something the wizard invented
    assert "transport" in result.reply.lower()
    state, _ = await state_of(ctx)
    assert state == IDLE


# --------------------------------------------------------------------
# validation and escape hatches
# --------------------------------------------------------------------


async def test_wrong_type_of_answer_re_asks_naming_the_expectation(ctx: RequestContext) -> None:
    """'cash' where an amount belongs is the realistic mistake."""
    await begin("paid", "wagdia", ctx)
    result = await answer("cash", ctx)

    assert "isn't a number I can read" in result.reply
    assert "How much?" in result.reply
    state, context = await state_of(ctx)
    assert state == AWAITING_COMMAND_SLOT
    assert context["queue"] == ["amount", "method"]


async def test_a_non_method_where_the_method_belongs_is_refused(ctx: RequestContext) -> None:
    await begin("paid", "wagdia 40000", ctx)
    result = await answer("upi", ctx)

    assert "isn't a payment method" in result.reply
    _, context = await state_of(ctx)
    assert context["queue"] == ["method"]


async def test_back_clears_the_previous_answer(ctx: RequestContext) -> None:
    await begin("paid", "", ctx)
    await answer("wagdia", ctx)
    result = await answer("back", ctx)

    assert "Going back." in result.reply
    assert "Which supplier?" in result.reply
    _, context = await state_of(ctx)
    assert context["filled"] == {}
    assert context["queue"] == ["party", "amount", "method"]


async def test_cancel_records_nothing(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await begin("expense", "", ctx)
    await answer("transport", ctx)
    result = await answer("cancel", ctx)

    assert "no expense was recorded" in result.reply
    state, _ = await state_of(ctx)
    assert state == IDLE
    assert await journal_rows(session_factory) == []


async def test_someone_else_prompts_for_typing_rather_than_answering_itself(
    ctx: RequestContext,
) -> None:
    """The 'Someone else' row can't be its own answer -- a new party's
    name is free text (docs/20 §9)."""
    await begin("paid", "", ctx)
    result = await answer("slot new", ctx)

    assert "type it" in result.reply.lower()
    _, context = await state_of(ctx)
    assert context["filled"] == {}
    assert context["queue"][0] == "party"


# --------------------------------------------------------------------
# prefill must never guess wrong -- a wrong guess here is a wrong payment
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("", {}),
        ("wagdia", {"party": "wagdia"}),
        ("wagdia 40000", {"party": "wagdia", "amount": "40000.00"}),
        ("Supplier: wagdia 40000", {"party": "wagdia", "amount": "40000.00"}),
        ("wagdia cash", {"party": "wagdia", "method": "cash"}),
        (
            "wagdia textiles 40,00,000 bank",
            {"party": "wagdia textiles", "amount": "4000000.00", "method": "bank"},
        ),
    ],
)
def test_settlement_prefill_places_only_what_it_is_sure_of(
    args: str, expected: dict[str, str]
) -> None:
    assert wizards.WIZARDS["paid"].prefill(args) == expected
