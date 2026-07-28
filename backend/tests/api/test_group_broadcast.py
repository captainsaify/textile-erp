"""Group broadcasting -- docs/22_GroupBroadcast.md.

Meta's Cloud API cannot post to a WhatsApp group at all, so the group is
reached through a second, outbound-only whatsapp-web.js number. These
tests pin the two things that make that safe: the relay never blocks or
breaks the thing that triggered it, and the automatic feed can only
announce facts that were actually committed.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import AuditLog, User
from backend.services import broadcast_service
from backend.tests.conftest import SEEDED_ORG_ID, purge_business_rows

D = decimal.Decimal
ORG = uuid.UUID(SEEDED_ORG_ID)


@pytest.fixture(autouse=True)
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    await purge_business_rows(session_factory)


async def _audit(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    action: str,
    after: dict[str, object] | None = None,
) -> None:
    async with session_factory() as session:
        session.add(
            AuditLog(
                org_id=ORG,
                actor_user_id=user.id,
                action=action,
                entity_type="test",
                entity_id=uuid.uuid4(),
                after_state=after or {},
                channel="whatsapp",
            )
        )
        await session.commit()


def test_a_line_reads_without_knowing_the_schema() -> None:
    entry = AuditLog(
        org_id=ORG,
        actor_user_id=uuid.uuid4(),
        action="payment.paid",
        entity_type="suppliers",
        entity_id=uuid.uuid4(),
        after_state={"amount": "4000000.00", "via": "cash", "name": "Wagdia"},
        channel="whatsapp",
    )

    line = broadcast_service.describe(entry, "Sarfaraz")

    assert "Paid a supplier" in line
    assert "₹40,00,000.00" in line
    assert "Wagdia" in line
    assert "Sarfaraz" in line


def test_bulk_creations_collapse_instead_of_flooding() -> None:
    """One photographed sheet creates 26 products. Twenty-six messages
    would bury the purchase that actually matters and train people to
    ignore the channel."""
    assert "product.created" not in broadcast_service.BROADCAST_ACTIONS
    assert broadcast_service.COLLAPSE_AT < 26


async def test_only_broadcastable_actions_are_picked_up(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)
    await _audit(session_factory, staff_user, "purchase.confirmed", {"invoice_no": "001"})
    await _audit(session_factory, staff_user, "product.created", {"code": "TRP"})

    async with session_factory() as session:
        lines, newest = await broadcast_service.pending_lines(session, ORG, since)

    assert newest is not None
    assert len(lines) == 1
    assert "001" in lines[0]


async def test_a_burst_of_one_action_is_summarised(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)
    for index in range(6):
        await _audit(session_factory, staff_user, "supplier.created", {"name": f"S{index}"})

    async with session_factory() as session:
        lines, _ = await broadcast_service.pending_lines(session, ORG, since)

    assert len(lines) == 1
    assert "6 entries" in lines[0]


async def test_switching_broadcasting_on_does_not_replay_history(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    """A missing watermark means "start from now". Dumping months of
    history into the group the first time it runs would be unrecoverable
    -- you cannot unsend it."""
    await _audit(session_factory, staff_user, "purchase.confirmed", {"invoice_no": "OLD"})

    async with session_factory() as session:
        watermark = await broadcast_service.read_watermark(session, ORG)
        lines, _ = await broadcast_service.pending_lines(session, ORG, watermark)

    assert lines == []


async def test_the_watermark_only_advances_over_what_was_read(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=1)
    await _audit(session_factory, staff_user, "sale.confirmed", {"amount": "100.00"})

    async with session_factory() as session:
        _, newest = await broadcast_service.pending_lines(session, ORG, since)
        assert newest is not None
        # commit rather than begin(): the read above already autobegan a
        # transaction, and entering begin() then raises (HANDOFF.md §5)
        await broadcast_service.write_watermark(session, ORG, newest)
        await session.commit()

    # a second sweep with nothing new says nothing
    async with session_factory() as session:
        watermark = await broadcast_service.read_watermark(session, ORG)
        lines, _ = await broadcast_service.pending_lines(session, ORG, watermark)
    assert lines == []


async def test_the_relay_is_off_when_it_isnt_configured() -> None:
    """An unconfigured relay must not offer a Share button that can only
    fail."""
    from backend.services.group_relay import GroupRelay

    relay = GroupRelay()
    assert relay.enabled is False
    result = await relay.send_text("anything")
    assert result.delivered is False
    assert "isn't switched on" in result.reason


async def test_a_failed_share_says_why(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    """Silence here would let someone believe the other partner has seen
    a number they haven't."""
    from backend.api.command_types import RequestContext
    from backend.api.commands import share_commands
    from backend.services.session_service import SessionService

    ctx = RequestContext(user=staff_user, session_factory=session_factory)
    await SessionService(session_factory).set(
        ORG, staff_user.id, share_commands.SHARE_STATE, {"body": "Cash ₹100", "file": ""}
    )
    state = await SessionService(session_factory).get(ORG, staff_user.id)

    result = await share_commands.handle_share_reply("share group", ctx, state)

    assert "Couldn't share" in result.reply
    assert "isn't switched on" in result.reply


async def test_declining_to_share_says_so_and_clears_the_state(
    session_factory: async_sessionmaker[AsyncSession], staff_user: User
) -> None:
    from backend.api.command_types import RequestContext
    from backend.api.commands import share_commands
    from backend.services.session_service import IDLE, SessionService

    ctx = RequestContext(user=staff_user, session_factory=session_factory)
    sessions = SessionService(session_factory)
    await sessions.set(ORG, staff_user.id, share_commands.SHARE_STATE, {"body": "x", "file": ""})
    state = await sessions.get(ORG, staff_user.id)

    result = await share_commands.handle_share_reply("share no", ctx, state)

    assert "Kept it to yourself" in result.reply
    assert (await sessions.get(ORG, staff_user.id)).state == IDLE


def test_read_commands_are_the_ones_marked_shareable() -> None:
    from backend.api.whatsapp_commands import COMMAND_REGISTRY

    shareable = {name for name, spec in COMMAND_REGISTRY.items() if spec.shareable}
    assert {"summary", "dashboard", "ledger", "supplier", "customer", "stock"} <= shareable
    # nothing that changes data offers to broadcast itself -- the
    # automatic sweep covers those, and it only reports committed facts
    assert not shareable & {"purchase", "sale", "paid", "received", "delete", "edit"}
