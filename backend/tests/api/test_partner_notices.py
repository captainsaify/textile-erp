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


async def test_the_start_point_is_written_down_not_recomputed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The bug this exists to prevent, in full.

    `read_watermark` answers "now" when no row exists, and the row was
    only written after a successful delivery. So every sweep asked for
    activity after *this instant*, the window `(now, now]` was empty,
    nothing was delivered, no row was written — and a minute later it
    did exactly the same thing. The fan-out ran for hours reporting
    `notices: 0` while purchases were being recorded.

    Claiming pins the origin, so the second sweep can see what happened
    between the two.
    """
    import asyncio

    from backend.services.broadcast_service import claim_watermark

    async with session_factory() as session, session.begin():
        first = await claim_watermark(session, ORG, notices.WATERMARK_KEY)

    await asyncio.sleep(0.05)

    async with session_factory() as session, session.begin():
        second = await claim_watermark(session, ORG, notices.WATERMARK_KEY)

    assert second == first, "the origin moved, so the window is empty again"

    # ...and it is genuinely persisted, not just memoised in a session
    async with session_factory() as session:
        stored = (
            await session.execute(
                sa.text("SELECT value FROM settings WHERE org_id = :org AND key = :key").bindparams(
                    org=ORG, key=notices.WATERMARK_KEY
                )
            )
        ).scalar_one()
    assert first.isoformat() in str(stored)


async def test_claiming_still_never_replays_history(
    session_factory: async_sessionmaker[AsyncSession], owner_user: User
) -> None:
    """Pinning the origin must not turn "start from now" into "send
    everything that ever happened"."""
    from backend.services.broadcast_service import claim_watermark

    async with session_factory() as session, session.begin():
        session.add(
            AuditLog(
                org_id=ORG,
                actor_user_id=owner_user.id,
                action="purchase.confirmed",
                entity_type="purchase_headers",
                entity_id=uuid.uuid4(),
                after_state={"invoice_no": "OLD-1", "grand_total": "1000"},
                channel="whatsapp",
            )
        )

    async with session_factory() as session, session.begin():
        since = await claim_watermark(session, ORG, notices.WATERMARK_KEY)

    async with session_factory() as session:
        pending, _ = await notices.pending_notices(session, ORG, since)
    assert pending == [], "activity from before the first sweep was replayed"


# --------------------------------------------------------------------
# the pull half: `activity`
# --------------------------------------------------------------------


async def test_activity_returns_the_last_ten_newest_first(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A push that depends on WhatsApp reaching a phone sometimes will
    not. The same record has to be available on demand."""
    actor = await _owner(session_factory, "Firoz", number="+919000000001")
    for index in range(12):
        await _audit(
            session_factory,
            actor_id=actor,
            action="payment.paid",
            after={"amount": str(100 + index), "name": f"Supplier {index}"},
        )

    async with session_factory() as session:
        lines = await notices.recent_activity(session, ORG, limit=10)

    assert len(lines) == 10
    assert "Supplier 11" in lines[0].body, "newest first"
    assert lines[0].at >= lines[-1].at


async def test_activity_shows_only_what_the_fan_out_would_have_sent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One list, not two. If `activity` showed rows the notices leave
    out, the two would disagree about what happened."""
    actor = await _owner(session_factory, "Shoyab", number="+919000000002")
    await _audit(session_factory, actor_id=actor, action="sale.created", after={"amount": "500"})
    # 26 of these arrive with one photographed sheet; deliberately not
    # notifiable, so deliberately not here either
    await _audit(session_factory, actor_id=actor, action="product.created", after={"code": "TRP"})

    async with session_factory() as session:
        lines = await notices.recent_activity(session, ORG, limit=10)

    assert len(lines) == 1
    assert "Sale recorded" in lines[0].body


async def test_activity_names_who_did_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Three people record into one set of books; "who" is half the
    information."""
    actor = await _owner(session_factory, "Firoz", number="+919000000003")
    await _audit(session_factory, actor_id=actor, action="expense.created", after={"amount": "250"})

    async with session_factory() as session:
        lines = await notices.recent_activity(session, ORG, limit=10)

    assert "Firoz" in lines[0].body


async def test_the_daily_checkin_asks_for_a_reply_and_says_why(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The reply is the point -- it re-opens WhatsApp's 24-hour window
    so the day's notices can be delivered at all. A message that didn't
    earn a reply would leave the window shut."""
    from backend.workers.tasks import _checkin_body

    actor = await _owner(session_factory, "Firoz", number="+919000000004")
    await _audit(
        session_factory,
        actor_id=actor,
        action="purchase.confirmed",
        after={"invoice_no": "002", "grand_total": "28644"},
    )
    async with session_factory() as session:
        lines = await notices.recent_activity(session, ORG, limit=5)

    body = _checkin_body(datetime.date(2026, 8, 3), lines)

    assert "Reply to this message" in body
    assert "activity" in body
    assert "002" in body, "it carries real figures, or it is not worth replying to"


def test_the_checkin_says_so_when_nothing_happened() -> None:
    """Silence and 'nothing happened' are different, and only one of
    them tells you the bot is still alive."""
    from backend.workers.tasks import _checkin_body

    body = _checkin_body(datetime.date(2026, 8, 3), [])

    assert "Nothing new" in body
    assert "Reply to this message" in body
