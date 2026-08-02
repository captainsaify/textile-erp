"""Telling the other partners what you just did -- docs/22 §7.

Three people run this business from three phones. The one who records a
sale sees it; the other two used to see nothing until somebody opened
the dashboard. These tests pin the two things that makes safe: it
reaches everyone *except* the person who did it, and what it announces
was actually committed.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import AuditLog, User
from backend.models.enums import UserRole
from backend.services import partner_notice_service as notices
from backend.tests.conftest import SEEDED_ORG_ID, purge_business_rows

D = decimal.Decimal
ORG = uuid.UUID(SEEDED_ORG_ID)


@pytest.fixture(autouse=True)
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    await purge_business_rows(session_factory)


async def _owner(
    session_factory: async_sessionmaker[AsyncSession], name: str, *, number: str | None
) -> uuid.UUID:
    async with session_factory() as session:
        user = User(org_id=ORG, full_name=name, whatsapp_number=number, role=UserRole.OWNER)
        session.add(user)
        await session.commit()
        return user.id


async def _audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    actor_id: uuid.UUID | None,
    action: str,
    entity_type: str = "test",
    entity_id: uuid.UUID | None = None,
    after: dict[str, object] | None = None,
) -> uuid.UUID:
    async with session_factory() as session:
        entry = AuditLog(
            org_id=ORG,
            actor_user_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id or uuid.uuid4(),
            after_state=after or {},
            channel="whatsapp",
        )
        session.add(entry)
        await session.commit()
        return entry.id


# --------------------------------------------------------------------
# who hears it
# --------------------------------------------------------------------


async def test_everyone_but_the_person_who_did_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """They already have the confirmation and the sheet in their own
    chat; a copy arriving a minute later reads as a second sale."""
    firoz = await _owner(session_factory, "Firoz", number="+917000000001")
    shoyab = await _owner(session_factory, "Shoyab", number="+917000000002")
    sarfaraz = await _owner(session_factory, "Sarfaraz", number="+917000000003")

    async with session_factory() as session:
        told = await notices.recipients(session, ORG, exclude_user_id=firoz)

    numbers = {person.number for person in told}
    assert numbers == {"+917000000002", "+917000000003"}
    assert shoyab in {person.user_id for person in told}
    assert sarfaraz in {person.user_id for person in told}


async def test_staff_and_the_unreachable_are_not_told(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An owner with no number has no phone to reach; staff are not
    partners in the business and this is a partners' channel."""
    async with session_factory() as session:
        session.add_all(
            [
                User(org_id=ORG, full_name="Helper", whatsapp_number="+917000000004"),
                User(
                    org_id=ORG,
                    full_name="Dashboard Only",
                    email="books@example.test",
                    role=UserRole.OWNER,
                ),
            ]
        )
        await session.commit()
    await _owner(session_factory, "Firoz", number="+917000000005")

    async with session_factory() as session:
        told = await notices.recipients(session, ORG, exclude_user_id=None)

    assert [person.name for person in told] == ["Firoz"]


async def test_an_inactive_owner_stops_hearing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The number Firoz stopped using was retired, not deleted. It must
    not keep receiving the partners' books."""
    retired = await _owner(session_factory, "Old Number", number="+919977250571")
    await _owner(session_factory, "Firoz", number="+917000087329")
    async with session_factory() as session:
        await session.execute(
            sa.text("UPDATE users SET deleted_at = now(), is_active = false WHERE id = :id"),
            {"id": retired},
        )
        await session.commit()

    async with session_factory() as session:
        told = await notices.recipients(session, ORG, exclude_user_id=None)

    assert [person.number for person in told] == ["+917000087329"]


# --------------------------------------------------------------------
# what they hear
# --------------------------------------------------------------------


async def test_only_committed_activity_is_announced(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fed from audit rows, which exist because the transaction
    succeeded — a hook inside the command could announce a purchase that
    then rolled back, and a WhatsApp message cannot be unsent."""
    actor = await _owner(session_factory, "Firoz", number="+917000000006")
    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)
    await _audit(
        session_factory,
        actor_id=actor,
        action="sale.created",
        entity_type="sales_headers",
        after={"grand_total": "165000.00", "name": "Hanif Pune"},
    )
    # not on the list: 26 of these arrive with one photographed sheet
    await _audit(session_factory, actor_id=actor, action="product.created", after={"code": "55D"})

    async with session_factory() as session:
        found, newest = await notices.pending_notices(session, ORG, since)

    assert len(found) == 1
    assert "Sale recorded" in found[0].body
    assert "1,65,000.00" in found[0].body
    assert "Hanif Pune" in found[0].body
    assert "by Firoz" in found[0].body
    assert newest is not None


async def test_the_headline_names_actions_this_system_actually_writes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The group-broadcast map guessed at `sale.confirmed` and
    `expense.recorded`, neither of which has ever been written here — so
    it would have announced almost nothing. This list was read off the
    live table."""
    for action in ("sale.created", "expense.created", "capital.contribution"):
        assert action in notices.NOTIFIABLE, action
    for guessed in ("sale.confirmed", "expense.recorded", "capital.contributed"):
        assert guessed not in notices.NOTIFIABLE, guessed


async def test_a_bill_carries_its_own_sheet(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A partner told about a bill who cannot see it has been told half
    of something."""
    actor = await _owner(session_factory, "Firoz", number="+917000000007")
    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)
    header_id = uuid.uuid4()
    await _audit(
        session_factory,
        actor_id=actor,
        action="purchase.confirmed",
        entity_type="purchase_headers",
        entity_id=header_id,
        after={"invoice_no": "002", "grand_total": "2864400.00"},
    )
    payment_id = await _audit(
        session_factory,
        actor_id=actor,
        action="payment.paid",
        entity_type="suppliers",
        after={"amount": "500000.00", "via": "cash"},
    )

    async with session_factory() as session:
        found, _ = await notices.pending_notices(session, ORG, since)

    by_kind = {n.document.kind: n.document.reference for n in found if n.document is not None}
    assert by_kind["purchase"] == str(header_id)
    # a payment's sheet is keyed by its own audit id, which is what
    # `undo payment` takes and what the confirmation prints
    assert by_kind["payment"] == str(payment_id)[:8]


async def test_a_setting_change_is_announced_without_a_sheet(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Worth telling the partners about; there is no bill to attach."""
    actor = await _owner(session_factory, "Firoz", number="+917000000008")
    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)
    await _audit(session_factory, actor_id=actor, action="settings.updated", entity_type="settings")

    async with session_factory() as session:
        found, _ = await notices.pending_notices(session, ORG, since)

    assert len(found) == 1
    assert found[0].document is None


async def test_the_two_sweeps_keep_separate_places_in_the_log(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One shared watermark would let whichever swept first swallow the
    other's activity."""
    from backend.services.broadcast_service import (
        WATERMARK_KEY,
        read_watermark,
        write_watermark,
    )

    moment = datetime.datetime(2026, 8, 1, 12, 0, tzinfo=datetime.UTC)
    async with session_factory() as session:
        await write_watermark(session, ORG, moment, notices.WATERMARK_KEY)
        await session.commit()

    async with session_factory() as session:
        assert await read_watermark(session, ORG, notices.WATERMARK_KEY) == moment
        # the group's own watermark was not touched, so it still means
        # "start from now"
        group = await read_watermark(session, ORG, WATERMARK_KEY)
    assert group > moment
