"""The same code under two brands.

VVP under TOP and VVP under MKD are two different products -- that is
the whole point of scoping codes by brand. But the likeliest reason a
code shows up under a second brand is that the brand was answered wrong,
and confirming quietly creates a duplicate that then diverges from the
original. So it is surfaced before CONFIRM.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import RequestContext
from backend.api.commands.ocr_commands import resolve_after_details
from backend.api.commands.purchase_commands import preview_result
from backend.api.interactive import Buttons
from backend.models import Brand, Product, Supplier, User
from backend.services.purchase_service import Draft, DraftLine
from backend.tests.conftest import (
    SEEDED_KG_UNIT_ID,
    SEEDED_ORG_ID,
    SEEDED_TEXTILE_TYPE_ID,
    purge_business_rows,
)

D = decimal.Decimal
ORG = uuid.UUID(SEEDED_ORG_ID)


@pytest.fixture(autouse=True)
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    await purge_business_rows(session_factory)


@pytest.fixture
def ctx(staff_user: User, session_factory: async_sessionmaker[AsyncSession]) -> RequestContext:
    return RequestContext(user=staff_user, session_factory=session_factory, message_id="m1")


async def _existing_under_top(
    session_factory: async_sessionmaker[AsyncSession], actor: User, codes: list[str]
) -> None:
    async with session_factory() as session:
        brand = Brand(org_id=ORG, name="TOP")
        supplier = Supplier(org_id=ORG, name="Wagdia", created_by=actor.id)
        session.add_all([brand, supplier])
        await session.flush()
        for code in codes:
            session.add(
                Product(
                    org_id=ORG,
                    product_type_id=uuid.UUID(SEEDED_TEXTILE_TYPE_ID),
                    code=code,
                    brand_id=brand.id,
                    description=f"TOP {code}",
                    unit_id=uuid.UUID(SEEDED_KG_UNIT_ID),
                    created_by=actor.id,
                )
            )
        await session.commit()


def _mkd_draft(codes: list[str]) -> Draft:
    return Draft(
        supplier_id=None,
        supplier_name="Wagdia",
        invoice_no="227",
        invoice_date=datetime.date.today(),
        brand_id=None,
        brand_name="MKD",
        lines=[
            DraftLine(
                code=code,
                qty=D("800"),
                rate=D("75"),
                product_id=None,
                resolved_code=None,
                unit_code="KG",
            )
            for code in codes
        ],
        freight=D("0"),
        other_charges=D("0"),
        declared_total=None,
    )


async def test_codes_already_under_another_brand_are_flagged(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The partners' real case: an MKD bill carrying VVP, 22D, PP, MMS,
    MSW and CPK, all of which already exist under TOP."""
    shared = ["VVP", "22D", "PP", "MMS", "MSW", "CPK"]
    await _existing_under_top(session_factory, ctx.user, shared)

    draft = await resolve_after_details(_mkd_draft(shared), ctx)

    assert len(draft.brand_collisions) == 6
    assert all("already under TOP" in entry for entry in draft.brand_collisions)
    # they are *not* matched to the TOP products
    assert all(line.product_id is None for line in draft.lines)


async def test_the_preview_asks_before_creating_duplicates(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _existing_under_top(session_factory, ctx.user, ["VVP"])
    draft = await resolve_after_details(_mkd_draft(["VVP"]), ctx)

    result = preview_result(draft)

    assert "already exist under another brand" in result.reply
    assert "VVP" in result.reply
    assert isinstance(result.interactive, Buttons)
    assert [c.title for c in result.interactive.choices] == [
        "Yes, separate",
        "Fix the brand",
        "Discard",
    ]
    # "Yes, separate" is the bulk create -- the collision question
    # replaces the catalogue prompt rather than arriving after it, so
    # nobody creates six duplicates in one tap before being warned
    assert result.interactive.choices[0].id == "create all products"


async def test_a_code_unique_to_this_brand_raises_nothing(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The warning has to stay rare, or it becomes noise people tap
    through."""
    await _existing_under_top(session_factory, ctx.user, ["VVP"])

    draft = await resolve_after_details(_mkd_draft(["BRANDNEW"]), ctx)

    assert draft.brand_collisions == []
    assert "already exist under another brand" not in preview_result(draft).reply


async def test_fixing_the_brand_returns_to_the_brand_question(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Re-answering re-resolves every code against the corrected brand,
    which is the point -- a wrong brand silently duplicates products."""
    from backend.api.commands.purchase_commands import handle_purchase_session_reply
    from backend.services.session_service import (
        AWAITING_PURCHASE_CONFIRMATION,
        AWAITING_SLOT,
        SessionService,
    )

    await _existing_under_top(session_factory, ctx.user, ["VVP"])
    draft = await resolve_after_details(_mkd_draft(["VVP"]), ctx)
    sessions = SessionService(session_factory)
    await sessions.set(ORG, ctx.user.id, AWAITING_PURCHASE_CONFIRMATION, draft.to_context())
    state = await sessions.get(ORG, ctx.user.id)

    result = await handle_purchase_session_reply("fix brand", ctx, state)

    assert "brand" in result.reply.lower() or result.interactive is not None
    after = await sessions.get(ORG, ctx.user.id)
    assert after.state == AWAITING_SLOT
    assert after.context["queue"] == ["brand"]


async def test_collisions_survive_the_session_round_trip(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The draft is parked in the session between messages; a warning
    that vanished on the round trip would let CONFIRM through clean."""
    await _existing_under_top(session_factory, ctx.user, ["VVP"])
    draft = await resolve_after_details(_mkd_draft(["VVP"]), ctx)

    restored = Draft.from_context(draft.to_context())

    assert restored.brand_collisions == draft.brand_collisions


async def _existing_under(
    session_factory: async_sessionmaker[AsyncSession],
    actor: User,
    brand_name: str,
    codes: list[str],
) -> None:
    async with session_factory() as session:
        brand = Brand(org_id=ORG, name=brand_name)
        session.add(brand)
        await session.flush()
        for code in codes:
            session.add(
                Product(
                    org_id=ORG,
                    product_type_id=uuid.UUID(SEEDED_TEXTILE_TYPE_ID),
                    code=code,
                    brand_id=brand.id,
                    description=f"{brand_name} {code}",
                    unit_id=uuid.UUID(SEEDED_KG_UNIT_ID),
                    created_by=actor.id,
                )
            )
        await session.commit()


async def test_a_code_shared_with_another_brand_is_named_before_confirm(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Noor Traders' bill carried VVP and VVP-1 and the brand was answered
    TOP, so both resolved to TOP's products -- correctly. But VVP also
    names an MKD product, so which one this bought was a real choice,
    and the preview says so instead of making it silently."""
    await _existing_under_top(session_factory, ctx.user, ["VVP", "VVP-1"])
    await _existing_under(session_factory, ctx.user, "MKD", ["VVP"])

    draft = _mkd_draft(["VVP", "VVP-1"])
    draft.brand_name = "TOP"
    draft.supplier_id = uuid.uuid4()
    draft = await resolve_after_details(draft, ctx)

    # resolved, under TOP -- nothing is being created
    assert all(line.product_id is not None for line in draft.lines)
    assert draft.brand_collisions == []
    assert draft.shared_codes == ["VVP (also under MKD)"]
    # VVP-1 exists under one brand only, so it stays quiet
    assert not any("VVP-1" in entry for entry in draft.shared_codes)

    result = preview_result(draft)
    assert "exist under more than one brand" in result.reply
    assert isinstance(result.interactive, Buttons)
    assert [c.id for c in result.interactive.choices] == ["confirm", "fix brand", "discard"]


async def test_shared_codes_survive_the_session_round_trip(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _existing_under_top(session_factory, ctx.user, ["VVP"])
    await _existing_under(session_factory, ctx.user, "MKD", ["VVP"])
    draft = _mkd_draft(["VVP"])
    draft.brand_name = "TOP"
    draft = await resolve_after_details(draft, ctx)

    assert Draft.from_context(draft.to_context()).shared_codes == draft.shared_codes
