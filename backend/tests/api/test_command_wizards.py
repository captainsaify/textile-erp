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
    assert result.reply == "", "the buttons already ask; text as well asks twice"


async def test_a_finished_export_does_not_offer_to_export_again(ctx: RequestContext) -> None:
    """Every completed export used to come back with a period menu whose
    rows re-ran it -- so a finished job looked unfinished, and tapping
    queued a second one."""
    from backend.api.commands.ops_commands import handle_export

    result = await handle_export("purchases year", ctx)

    assert "Building your purchases export" in result.reply
    assert result.interactive is None


async def test_export_offers_reports_then_a_period(ctx: RequestContext) -> None:
    first = await begin("export", "", ctx)
    assert first is not None
    assert isinstance(first.interactive, ListMenu)
    offered = [row.id for section in first.interactive.sections for row in section.rows]
    assert offered == [
        "slot purchases",
        "slot purchases-supplier",
        "slot invoice",
        "slot sales",
        "slot sales-customer",
        "slot statement-supplier",
        "slot statement-customer",
        "slot ledger-supplier",
        "slot ledger-customer",
        "slot stock",
    ]

    second = await answer("slot purchases", ctx)
    assert isinstance(second.interactive, ListMenu)
    # the menu carries the question; sending it as text as well asked
    # twice, which is what arrived on the phone as two "Which period?"
    assert "Which period?" in second.interactive.body
    assert second.reply == ""


async def test_a_report_about_one_party_asks_which_party(ctx: RequestContext) -> None:
    """The supplier question only exists for reports that are about one
    supplier -- a queue fixed at the start couldn't express that."""
    await begin("export", "", ctx)
    await answer("slot statement-supplier", ctx)

    _, context = await state_of(ctx)
    assert context["queue"] == ["supplier", "period"]
    assert "customer" not in context["queue"]
    assert "invoice" not in context["queue"]


async def test_one_invoice_is_never_asked_for_a_period(ctx: RequestContext) -> None:
    """A period could only exclude the very bill that was asked for."""
    await begin("export", "", ctx)
    await answer("slot invoice", ctx)

    _, context = await state_of(ctx)
    assert context["queue"] == ["invoice"]


@pytest.mark.parametrize(
    ("filled", "expected"),
    [
        ({"report": "purchases", "period": "month"}, "purchases month"),
        (
            {"report": "purchases-supplier", "supplier": "Wagdia", "period": "year"},
            "purchases supplier Wagdia year",
        ),
        (
            {"report": "statement-customer", "customer": "Ravi Traders", "period": "month"},
            "statement customer Ravi Traders month",
        ),
        ({"report": "invoice", "invoice": "INV-001"}, "invoice INV-001"),
        ({"report": "stock", "period": "today"}, "stock today"),
    ],
)
def test_the_wizard_assembles_a_command_someone_could_have_typed(
    filled: dict[str, str], expected: str
) -> None:
    """The wizard hands its answers to the real handler, so what it
    builds has to be exactly what the typed form looks like."""
    assert wizards.WIZARDS["export"].assemble(filled) == expected


async def test_supplier_lookup_with_no_name_asks_which_one(ctx: RequestContext) -> None:
    result = await begin("supplier", "", ctx)

    assert result is not None
    assert "Which supplier?" in (result.reply or result.interactive.body)  # type: ignore[union-attr]
    _, context = await state_of(ctx)
    assert context["queue"] == ["name"]


async def test_supplier_lookup_with_a_name_runs_straight_away(ctx: RequestContext) -> None:
    assert await begin("supplier", "Wagdia", ctx) is None


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


# --------------------------------------------------------------------
# capital, withdraw, edit, delete
# --------------------------------------------------------------------


async def test_capital_asks_partner_amount_method_and_direction(ctx: RequestContext) -> None:
    result = await begin("capital", "", ctx)

    assert result is not None
    _, context = await state_of(ctx)
    assert context["queue"] == ["partner", "amount", "method", "kind"]


async def test_capital_direction_is_two_buttons_not_a_word_to_remember(
    ctx: RequestContext,
) -> None:
    """'contribution' vs 'withdrawal' is exactly the kind of vocabulary
    a usage line makes you memorise."""
    await begin("capital", "Rahul 50000 cash", ctx)
    _, context = await state_of(ctx)
    assert context["queue"] == ["kind"]

    question = await wizards.ask(wizards.WIZARDS["capital"], "kind", ctx, dict(context["filled"]))
    assert isinstance(question.interactive, Buttons)
    assert [c.title for c in question.interactive.choices] == ["Contribution", "Withdrawal"]


async def test_a_complete_capital_command_still_runs_in_one_shot(ctx: RequestContext) -> None:
    assert await begin("capital", "Rahul 50000 cash contribution", ctx) is None


async def test_withdraw_never_asks_for_a_direction(ctx: RequestContext) -> None:
    """A withdrawal is a withdrawal; asking would be a question with one
    answer."""
    await begin("withdraw", "", ctx)
    _, context = await state_of(ctx)
    assert context["queue"] == ["partner", "amount", "method"]


async def test_edit_offers_the_fields_that_record_actually_has(ctx: RequestContext) -> None:
    """Offering a field the command would reject is worse than offering
    nothing -- and the list depends on the record kind just chosen."""
    await begin("edit", "", ctx)
    await answer("slot product", ctx)
    await answer("TRP", ctx)

    _, context = await state_of(ctx)
    question = await wizards.ask(wizards.WIZARDS["edit"], "field", ctx, dict(context["filled"]))
    assert isinstance(question.interactive, ListMenu)
    offered = [row.title for section in question.interactive.sections for row in section.rows]
    assert offered == list(wizards.EDITABLE_FIELDS["product"])

    customer_question = await wizards.ask(
        wizards.WIZARDS["edit"], "field", ctx, {"entity": "customer"}
    )
    assert isinstance(customer_question.interactive, ListMenu)
    customer_fields = [
        row.title for section in customer_question.interactive.sections for row in section.rows
    ]
    assert "credit_limit" in customer_fields


async def test_delete_always_confirms_and_names_what_it_will_delete(
    ctx: RequestContext,
) -> None:
    """A wizard makes deleting *easier* to reach, so the last tap has to
    say what it is about to do."""
    assert await begin("delete", "product TRP", ctx) is not None, "typed in full, still confirms"

    _, context = await state_of(ctx)
    assert context["queue"] == ["confirm"]

    question = await wizards.ask(wizards.WIZARDS["delete"], "confirm", ctx, dict(context["filled"]))
    assert isinstance(question.interactive, Buttons)
    assert "Delete product TRP?" in question.interactive.body
    assert [c.title for c in question.interactive.choices] == ["Yes, delete", "Keep it"]


async def test_keeping_it_abandons_the_delete(ctx: RequestContext) -> None:
    await begin("delete", "product TRP", ctx)
    result = await answer("slot cancel", ctx)

    assert "Cancelled" in result.reply
    state, _ = await state_of(ctx)
    assert state == IDLE


async def test_an_ambiguous_answer_to_the_delete_confirmation_is_not_a_yes(
    ctx: RequestContext,
) -> None:
    await begin("delete", "product TRP", ctx)
    result = await answer("maybe", ctx)

    assert "Tap 'Yes, delete'" in result.reply
    state, context = await state_of(ctx)
    assert state == AWAITING_COMMAND_SLOT
    assert context["queue"] == ["confirm"]


async def test_the_export_menu_offers_both_ledgers(ctx: RequestContext) -> None:
    result = await begin("export", "", ctx)

    assert result is not None
    assert isinstance(result.interactive, ListMenu)
    offered = [row.id for section in result.interactive.sections for row in section.rows]
    assert "slot ledger-supplier" in offered
    assert "slot ledger-customer" in offered


async def test_a_ledger_is_not_asked_for_a_period(ctx: RequestContext) -> None:
    """A ledger is a position as of today, not a range."""
    await begin("export", "", ctx)
    await answer("slot ledger-customer", ctx)

    state, _ = await state_of(ctx)
    assert state == IDLE, "nothing left to ask, so it ran"


def test_the_ledger_assembles_to_a_role_not_a_party() -> None:
    assert wizards.WIZARDS["export"].assemble({"report": "ledger-supplier"}) == "ledger supplier"
    assert wizards.WIZARDS["export"].assemble({"report": "ledger-customer"}) == "ledger customer"


@pytest.mark.parametrize("word", ["discard", "cancel", "stop", "quit", "never mind"])
async def test_every_way_of_saying_stop_stops(word: str, ctx: RequestContext) -> None:
    """The bot prints "discard" on a purchase preview and "cancel" in a
    wizard. Accepting only one made the system contradict its own
    instructions -- "discard" mid-expense was read as an amount."""
    await begin("expense", "", ctx)
    result = await answer(word, ctx)

    assert "Cancelled" in result.reply
    state, _ = await state_of(ctx)
    assert state == IDLE


async def test_stopping_works_at_any_point_in_the_wizard(ctx: RequestContext) -> None:
    await begin("expense", "", ctx)
    await answer("transport", ctx)
    result = await answer("discard", ctx)

    assert "no expense was recorded" in result.reply
    state, _ = await state_of(ctx)
    assert state == IDLE


# --------------------------------------------------------------------
# deleting a bill: routed, not refused
# --------------------------------------------------------------------


async def test_the_delete_menu_admits_that_bills_exist(ctx: RequestContext) -> None:
    """Offering only the four master records left someone wanting to
    remove a wrong bill staring at a menu that didn't mention bills,
    with nothing saying why."""
    await begin("delete", "", ctx)
    question = await wizards.ask(wizards.WIZARDS["delete"], "entity", ctx, {})

    assert isinstance(question.interactive, ListMenu)
    offered = [row.id for section in question.interactive.sections for row in section.rows]
    assert "slot purchase" in offered
    assert "slot sale" in offered
    # and it says what will actually happen to them
    titles = [section.title for section in question.interactive.sections]
    assert any("reversed" in title.lower() for title in titles)


async def test_deleting_a_bill_says_reverse_not_delete(ctx: RequestContext) -> None:
    await begin("delete", "purchase INV-001", ctx)
    _, context = await state_of(ctx)
    question = await wizards.ask(wizards.WIZARDS["delete"], "confirm", ctx, dict(context["filled"]))

    assert isinstance(question.interactive, Buttons)
    assert "Reverse purchase INV-001?" in question.interactive.body
    assert [c.title for c in question.interactive.choices] == ["Yes, reverse", "Keep it"]
    assert "compensating entry" in question.interactive.footer


def test_a_bill_reroutes_to_undo_rather_than_dead_ending() -> None:
    """`delete purchase X` used to explain that it couldn't, and stop.
    Now it does the thing the person wanted."""
    delete = wizards.WIZARDS["delete"]

    assert delete.reroute({"entity": "purchase", "reference": "INV-1"}) == (
        "undo",
        "purchase INV-1",
    )
    assert delete.reroute({"entity": "sale", "reference": "abc123"}) == ("undo", "sale abc123")
    # a master record is still a real delete
    assert delete.reroute({"entity": "product", "reference": "TRP"}) is None


async def test_editing_a_bill_never_asks_which_field(ctx: RequestContext) -> None:
    """A bill isn't edited field-by-field -- stock and the books were
    derived from it, so it is reversed and re-entered. With nothing left
    to ask, the wizard doesn't start at all."""
    started = await begin("edit", "purchase INV-001", ctx)

    assert started is None, "no field/value questions remain, so it ran"
    assert wizards.WIZARDS["edit"].reroute({"entity": "purchase", "reference": "INV-001"}) == (
        "undo",
        "purchase INV-001",
    )
