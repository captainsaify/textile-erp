"""Conversational intake -- docs/20_ConversationalIntake.md §12.

Every flow is exercised twice, once tapped and once typed, asserting the
same resulting Draft: that is what stops the two input paths drifting
apart. The wizard is driven directly from a synthetic Draft rather than
through OCR, so these tests say nothing about the reader and everything
about the conversation.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import CommandResult, RequestContext
from backend.api.commands.intake_commands import (
    SLOT_ORDER,
    begin_slots,
    handle_intent_reply,
    handle_slot_reply,
    missing_slots,
)
from backend.models import User
from backend.services.purchase_service import Draft, DraftLine
from backend.services.session_service import (
    AWAITING_INTENT,
    AWAITING_PURCHASE_CONFIRMATION,
    AWAITING_SLOT,
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


def make_draft(*, rate: str = "0", supplier: str = "", invoice: str = "") -> Draft:
    """What the reader hands over: quantities read, everything the sheet
    couldn't state left empty."""
    return Draft(
        supplier_id=None,
        supplier_name=supplier,
        invoice_no=invoice,
        invoice_date=datetime.date(2026, 1, 1),
        brand_id=None,
        brand_name=None,
        lines=[
            DraftLine(
                code="TRP",
                qty=D("100"),
                rate=D(rate),
                product_id=None,
                resolved_code=None,
                unit_code=None,
            )
        ],
        freight=D("0"),
        other_charges=D("0"),
        declared_total=None,
    )


async def state_of(ctx: RequestContext) -> tuple[str, dict[str, Any]]:
    session_state = await SessionService(ctx.session_factory).get(ctx.user.org_id, ctx.user.id)
    return session_state.state, dict(session_state.context)


async def reply(text: str, ctx: RequestContext) -> CommandResult:
    session_state = await SessionService(ctx.session_factory).get(ctx.user.org_id, ctx.user.id)
    return await handle_slot_reply(text, ctx, session_state)


async def start(ctx: RequestContext, draft: Draft | None = None) -> CommandResult:
    draft = draft if draft is not None else make_draft()
    return await begin_slots(draft, missing_slots(draft, date_known=False), ctx)


async def saved_draft(ctx: RequestContext) -> Draft:
    """The draft sitting in the confirmation state, i.e. what the wizard
    actually produced."""
    state, context = await state_of(ctx)
    assert state == AWAITING_PURCHASE_CONFIRMATION, state
    return Draft.from_context(context)


def comparable(draft: Draft) -> tuple[object, ...]:
    return (
        draft.supplier_name,
        draft.invoice_no,
        draft.invoice_date,
        tuple((line.code, line.qty, line.rate) for line in draft.lines),
    )


# --------------------------------------------------------------------
# gap analysis
# --------------------------------------------------------------------


def test_gap_analysis_lists_only_what_is_missing() -> None:
    assert missing_slots(make_draft(), date_known=False) == list(SLOT_ORDER)
    assert missing_slots(make_draft(supplier="Wagdia"), date_known=False) == [
        "invoice_no",
        "invoice_date",
        "purchase_rate",
    ]
    assert missing_slots(make_draft(rate="150", supplier="W", invoice="I-1")) == []


async def test_nothing_missing_skips_straight_to_the_preview(ctx: RequestContext) -> None:
    """The wizard must not invent questions (§12)."""
    draft = make_draft(rate="150", supplier="Wagdia", invoice="INV-1")
    result = await begin_slots(draft, missing_slots(draft), ctx)

    assert "Purchase draft ready" in result.reply
    assert "?" not in result.reply.split("\n")[0]
    state, _ = await state_of(ctx)
    assert state == AWAITING_PURCHASE_CONFIRMATION


async def test_summary_says_how_many_questions_are_coming(ctx: RequestContext) -> None:
    result = await start(ctx)
    assert "4 question(s)" in result.reply
    assert "supplier, invoice number, invoice date, rate" in result.reply
    assert "Which supplier is this from?" in result.reply


# --------------------------------------------------------------------
# tapped vs typed
# --------------------------------------------------------------------


async def test_tapped_and_typed_answers_produce_the_same_draft(ctx: RequestContext) -> None:
    await start(ctx)
    await reply("Wagdia Textiles", ctx)
    await reply("INV-77", ctx)
    await reply("26-07-2026", ctx)
    await reply("150", ctx)
    typed = await saved_draft(ctx)

    await SessionService(ctx.session_factory).set(ctx.user.org_id, ctx.user.id, IDLE, {})
    await start(ctx)
    # what the transport delivers when a row or button is tapped: the
    # choice id, prefixed -- see WhatsAppDispatcher._from_meta
    await reply("slot Wagdia Textiles", ctx)
    await reply("INV-77", ctx)
    await reply("slot other", ctx)
    await reply("26-07-2026", ctx)
    await reply("150", ctx)
    tapped = await saved_draft(ctx)

    assert comparable(typed) == comparable(tapped)
    assert typed.invoice_date == datetime.date(2026, 7, 26)
    assert typed.lines[0].rate == D("150")


async def test_today_button_and_typed_date_agree(ctx: RequestContext) -> None:
    today = datetime.date.today()
    await start(ctx)
    await reply("Wagdia", ctx)
    await reply("INV-1", ctx)
    await reply("slot today", ctx)
    await reply("150", ctx)
    tapped = await saved_draft(ctx)

    await SessionService(ctx.session_factory).set(ctx.user.org_id, ctx.user.id, IDLE, {})
    await start(ctx)
    await reply("Wagdia", ctx)
    await reply("INV-1", ctx)
    await reply(today.strftime("%d-%m-%Y"), ctx)
    await reply("150", ctx)

    assert comparable(tapped) == comparable(await saved_draft(ctx))
    assert tapped.invoice_date == today


async def test_one_shot_details_equals_answering_every_slot(ctx: RequestContext) -> None:
    """`details ...` in one message must reach the same draft as four
    answers -- if those diverge, one of them is wrong (§12)."""
    await start(ctx)
    await reply("Shree Textiles", ctx)
    await reply("INV-9", ctx)
    await reply("24-07-2026", ctx)
    await reply("150", ctx)
    stepwise = await saved_draft(ctx)

    await SessionService(ctx.session_factory).set(ctx.user.org_id, ctx.user.id, IDLE, {})
    await start(ctx)
    result = await reply(
        "details Supplier: Shree Textiles Invoice: INV-9 Date: 24-07-2026 Rate: 150", ctx
    )

    assert "Purchase draft ready" in result.reply
    assert comparable(stepwise) == comparable(await saved_draft(ctx))


# --------------------------------------------------------------------
# validation
# --------------------------------------------------------------------


async def test_wrong_type_of_answer_re_asks_naming_the_expectation(ctx: RequestContext) -> None:
    """The realistic mistake is not malformed input, it is the wrong
    *kind* of input -- 'cash' where a rate belongs (§12)."""
    await start(ctx)
    await reply("Wagdia", ctx)
    await reply("INV-1", ctx)
    await reply("26-07-2026", ctx)
    result = await reply("cash", ctx)

    assert "'cash' isn't a number I can read." in result.reply
    assert "What rate per unit did you pay?" in result.reply
    state, context = await state_of(ctx)
    assert state == AWAITING_SLOT
    assert context["queue"] == ["purchase_rate"]


async def test_unreadable_date_re_asks_with_the_format(ctx: RequestContext) -> None:
    await start(ctx)
    await reply("Wagdia", ctx)
    await reply("INV-1", ctx)
    result = await reply("last tuesday", ctx)

    assert "DD-MM-YYYY" in result.reply
    state, context = await state_of(ctx)
    assert state == AWAITING_SLOT
    assert context["queue"][0] == "invoice_date"


async def test_amount_with_indian_grouping_is_accepted(ctx: RequestContext) -> None:
    """The system prints '1,50,000'; refusing to read it back was a real
    failure (docs/20 §1)."""
    await start(ctx)
    await reply("Wagdia", ctx)
    await reply("INV-1", ctx)
    await reply("26-07-2026", ctx)
    await reply("₹1,50,000", ctx)

    assert (await saved_draft(ctx)).lines[0].rate == D("150000")


# --------------------------------------------------------------------
# escape hatches
# --------------------------------------------------------------------


async def test_back_clears_the_previous_answer_and_re_asks_it(ctx: RequestContext) -> None:
    await start(ctx)
    await reply("Wagdia", ctx)
    result = await reply("back", ctx)

    assert "Going back." in result.reply
    assert "Which supplier is this from?" in result.reply
    state, context = await state_of(ctx)
    assert context["queue"] == list(SLOT_ORDER)
    assert context["filled"] == {}

    await reply("Shree Textiles", ctx)
    await reply("INV-1", ctx)
    await reply("26-07-2026", ctx)
    await reply("150", ctx)
    assert (await saved_draft(ctx)).supplier_name == "Shree Textiles"


async def test_back_on_the_first_question_says_so_rather_than_failing(ctx: RequestContext) -> None:
    await start(ctx)
    result = await reply("back", ctx)

    assert "That's the first question." in result.reply
    state, _ = await state_of(ctx)
    assert state == AWAITING_SLOT


async def test_cancel_abandons_the_draft_explicitly(ctx: RequestContext) -> None:
    await start(ctx)
    result = await reply("cancel", ctx)

    assert "nothing was saved" in result.reply
    state, _ = await state_of(ctx)
    assert state == IDLE


async def test_skip_is_refused_because_no_slot_here_is_optional(ctx: RequestContext) -> None:
    await start(ctx)
    result = await reply("skip", ctx)

    assert "can't skip" in result.reply
    state, context = await state_of(ctx)
    assert state == AWAITING_SLOT
    assert context["filled"] == {}


# --------------------------------------------------------------------
# intent
# --------------------------------------------------------------------


async def test_intent_neither_leaves_the_photo_alone(ctx: RequestContext) -> None:
    sessions = SessionService(ctx.session_factory)
    await sessions.set(ctx.user.org_id, ctx.user.id, AWAITING_INTENT, {"attachment_id": "x"})
    session_state = await sessions.get(ctx.user.org_id, ctx.user.id)
    result = await handle_intent_reply("intake cancel", ctx, session_state)

    assert "left that photo alone" in result.reply
    state, _ = await state_of(ctx)
    assert state == IDLE


async def test_intent_sale_now_reads_the_note_instead_of_refusing(
    ctx: RequestContext,
) -> None:
    """Tapping "A sale" used to answer "I can't read a sales sheet yet".
    That was a missing feature dressed as a capability limit -- the same
    vision model reads a handwritten note fine. It now goes to the sales
    reader, which is why an unknown attachment reports an expired photo
    rather than a refusal to try.
    """
    sessions = SessionService(ctx.session_factory)
    await sessions.set(ctx.user.org_id, ctx.user.id, AWAITING_INTENT, {"attachment_id": "x"})
    session_state = await sessions.get(ctx.user.org_id, ctx.user.id)

    result = await handle_intent_reply("intake sale", ctx, session_state)

    assert "can't read a sales sheet" not in result.reply
    assert "expired" in result.reply
