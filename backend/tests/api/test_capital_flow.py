"""`capital` / `withdraw` and the dual-approval path --
docs/06_Accounting.md §8, §13; docs/08_WhatsApp.md #capital, #withdraw.

The invariant these guard: a pending withdrawal must move *nothing*.
Not equity, not cash, not the journal. If it did, equity would fall
while assets stayed put and the balance-sheet identity in §6 would be
broken for as long as the request went unanswered.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import RequestContext
from backend.api.commands.capital_commands import (
    handle_approve,
    handle_capital,
    handle_reject,
    handle_withdraw,
    parse_capital_command,
)
from backend.core.exceptions import ValidationError
from backend.models import Partner, PartnerCapital, Setting, User
from backend.models.enums import UserRole
from backend.repositories.accounting_repository import LedgerRepository, PartnerCapitalRepository
from backend.tests.conftest import SEEDED_ORG_ID, purge_business_rows

D = decimal.Decimal
ORG = uuid.UUID(SEEDED_ORG_ID)


@pytest.fixture(autouse=True)
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    await purge_business_rows(session_factory)


@dataclasses.dataclass(frozen=True)
class Pair:
    """Two partners, each linked to an owner-role WhatsApp user -- the
    minimum needed for a second signature to be possible at all."""

    rahul: Partner
    farida: Partner
    rahul_ctx: RequestContext
    farida_ctx: RequestContext


async def _make_partner_user(session: AsyncSession, name: str) -> tuple[User, Partner]:
    user = User(
        org_id=ORG,
        full_name=name,
        whatsapp_number=f"+9198{uuid.uuid4().hex[:8]}",
        role=UserRole.OWNER,
    )
    session.add(user)
    await session.flush()
    partner = Partner(
        org_id=ORG,
        user_id=user.id,
        display_name=name,
        profit_share_percent=D("50"),
        created_by=user.id,
    )
    session.add(partner)
    await session.flush()
    return user, partner


@pytest.fixture
async def pair(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[Pair]:
    async with session_factory() as session, session.begin():
        rahul_user, rahul = await _make_partner_user(session, "Rahul")
        farida_user, farida = await _make_partner_user(session, "Farida")

    yield Pair(
        rahul=rahul,
        farida=farida,
        rahul_ctx=RequestContext(
            user=rahul_user, session_factory=session_factory, message_id="m-rahul"
        ),
        farida_ctx=RequestContext(
            user=farida_user, session_factory=session_factory, message_id="m-farida"
        ),
    )

    async with session_factory() as session:
        for user_id in (rahul_user.id, farida_user.id):
            try:
                await session.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": user_id})
                await session.commit()
            except sa.exc.IntegrityError:
                await session.rollback()


async def _capital_balance(
    session_factory: async_sessionmaker[AsyncSession], partner_id: uuid.UUID
) -> decimal.Decimal:
    async with session_factory() as session:
        return await PartnerCapitalRepository(session).balance(ORG, partner_id)


async def _cash_balance(session_factory: async_sessionmaker[AsyncSession]) -> decimal.Decimal:
    async with session_factory() as session:
        return await LedgerRepository(session).balance(ORG, "cash")


# --------------------------------------------------------------------
# grammar
# --------------------------------------------------------------------


def test_parse_capital_defaults_to_contribution() -> None:
    command = parse_capital_command("Rahul 50000 bank", usage="u")
    assert command.partner_name == "Rahul"
    assert command.amount == D("50000")
    assert command.via == "bank"
    assert command.kind == "contribution"


def test_parse_capital_accepts_multiword_partner_names() -> None:
    """The name is whatever precedes the amount, so a two-word partner
    name doesn't get chopped."""
    command = parse_capital_command("Anita Rao 1200.50 cash withdrawal", usage="u")
    assert command.partner_name == "Anita Rao"
    assert command.amount == D("1200.50")
    assert command.kind == "withdrawal"


def test_parse_capital_rejects_missing_payment_method() -> None:
    with pytest.raises(ValidationError, match="cash or bank"):
        parse_capital_command("Rahul 500 wallet", usage="u")


def test_parse_capital_rejects_non_numeric_amount() -> None:
    with pytest.raises(ValidationError, match="not a number"):
        parse_capital_command("Rahul lots cash", usage="u")


# --------------------------------------------------------------------
# contributions and small withdrawals post immediately
# --------------------------------------------------------------------


async def test_contribution_moves_capital_and_cash(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    result = await handle_capital("Rahul 50000 bank", pair.rahul_ctx)
    assert "✅ Capital contribution recorded" in result.reply
    assert "₹50,000.00" in result.reply
    assert await _capital_balance(session_factory, pair.rahul.id) == D("50000.00")


async def test_small_withdrawal_posts_without_approval(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await handle_capital("Rahul 50000 cash", pair.rahul_ctx)
    result = await handle_withdraw("Rahul 5000 cash", pair.rahul_ctx)
    assert "✅ Capital withdrawal recorded" in result.reply
    assert result.notifications == ()
    assert await _capital_balance(session_factory, pair.rahul.id) == D("45000.00")
    assert await _cash_balance(session_factory) == D("45000.00")


async def test_withdrawal_into_deficit_is_allowed_but_flagged(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """docs/06_Accounting.md §13: a negative capital balance is allowed
    -- it means the partner owes the business -- but never silent."""
    result = await handle_withdraw("Rahul 1000 cash", pair.rahul_ctx)
    assert "✅ Capital withdrawal recorded" in result.reply
    assert "negative" in result.reply
    assert await _capital_balance(session_factory, pair.rahul.id) == D("-1000.00")


# --------------------------------------------------------------------
# the dual-approval path
# --------------------------------------------------------------------


async def test_large_withdrawal_moves_nothing_until_approved(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The core invariant: a pending request touches no balance."""
    await handle_capital("Rahul 100000 cash", pair.rahul_ctx)
    capital_before = await _capital_balance(session_factory, pair.rahul.id)
    cash_before = await _cash_balance(session_factory)

    result = await handle_withdraw("Rahul 30000 cash", pair.rahul_ctx)
    assert "🔒" in result.reply
    assert "Waiting on: Farida" in result.reply

    assert await _capital_balance(session_factory, pair.rahul.id) == capital_before
    assert await _cash_balance(session_factory) == cash_before

    async with session_factory() as session:
        journal_rows = (
            await session.execute(
                sa.text(
                    "SELECT count(*) FROM journal WHERE org_id = :org "
                    "AND description LIKE '%withdrawal%'"
                ),
                {"org": ORG},
            )
        ).scalar_one()
    assert journal_rows == 0  # nothing posted to the books either


async def test_large_withdrawal_notifies_the_other_partner(pair: Pair) -> None:
    result = await handle_withdraw("Rahul 30000 bank", pair.rahul_ctx)
    assert len(result.notifications) == 1
    number, body = result.notifications[0]
    assert number == pair.farida_ctx.user.whatsapp_number
    assert "Rahul requested a capital withdrawal" in body
    assert "approve withdraw" in body


async def test_requester_cannot_approve_their_own_withdrawal(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """§8's entire purpose -- checked server-side, not just assumed
    because it arrives from a different phone."""
    request = await handle_withdraw("Rahul 30000 cash", pair.rahul_ctx)
    reference = request.notifications[0][1].split("approve withdraw ")[1].split('"')[0]

    result = await handle_approve(f"withdraw {reference}", pair.rahul_ctx)
    assert "can't approve your own" in result.reply
    assert await _capital_balance(session_factory, pair.rahul.id) == D("0")


async def test_second_partner_approval_posts_the_withdrawal(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await handle_capital("Rahul 100000 cash", pair.rahul_ctx)
    request = await handle_withdraw("Rahul 30000 cash", pair.rahul_ctx)
    reference = request.notifications[0][1].split("approve withdraw ")[1].split('"')[0]

    result = await handle_approve(f"withdraw {reference}", pair.farida_ctx)
    assert "✅ Approved." in result.reply
    assert await _capital_balance(session_factory, pair.rahul.id) == D("70000.00")
    assert await _cash_balance(session_factory) == D("70000.00")


async def test_approval_recomputes_against_the_balance_at_approval_time(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A contribution landing while the request waits must not be
    overwritten: the withdrawal joins the chain at approval, using the
    balance as it stands then, not as it stood at request time."""
    await handle_capital("Rahul 100000 cash", pair.rahul_ctx)
    request = await handle_withdraw("Rahul 30000 cash", pair.rahul_ctx)
    reference = request.notifications[0][1].split("approve withdraw ")[1].split('"')[0]

    # someone tops Rahul up while the withdrawal sits pending
    await handle_capital("Rahul 20000 cash", pair.rahul_ctx)
    assert await _capital_balance(session_factory, pair.rahul.id) == D("120000.00")

    await handle_approve(f"withdraw {reference}", pair.farida_ctx)
    assert await _capital_balance(session_factory, pair.rahul.id) == D("90000.00")


async def test_rejection_leaves_balances_untouched(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await handle_capital("Rahul 100000 cash", pair.rahul_ctx)
    request = await handle_withdraw("Rahul 30000 cash", pair.rahul_ctx)
    reference = request.notifications[0][1].split("approve withdraw ")[1].split('"')[0]

    result = await handle_reject(f"withdraw {reference}", pair.farida_ctx)
    assert "🚫 Rejected" in result.reply
    assert await _capital_balance(session_factory, pair.rahul.id) == D("100000.00")
    assert await _cash_balance(session_factory) == D("100000.00")


async def test_a_rejected_request_cannot_then_be_approved(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    request = await handle_withdraw("Rahul 30000 cash", pair.rahul_ctx)
    reference = request.notifications[0][1].split("approve withdraw ")[1].split('"')[0]
    await handle_reject(f"withdraw {reference}", pair.farida_ctx)

    result = await handle_approve(f"withdraw {reference}", pair.farida_ctx)
    assert "not found" in result.reply
    assert await _capital_balance(session_factory, pair.rahul.id) == D("0")


async def test_approving_twice_posts_only_once(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await handle_capital("Rahul 100000 cash", pair.rahul_ctx)
    request = await handle_withdraw("Rahul 30000 cash", pair.rahul_ctx)
    reference = request.notifications[0][1].split("approve withdraw ")[1].split('"')[0]

    await handle_approve(f"withdraw {reference}", pair.farida_ctx)
    second = await handle_approve(f"withdraw {reference}", pair.farida_ctx)

    assert "not found" in second.reply  # no longer pending
    assert await _capital_balance(session_factory, pair.rahul.id) == D("70000.00")


async def test_expired_request_is_cancelled_rather_than_approved(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    request = await handle_withdraw("Rahul 30000 cash", pair.rahul_ctx)
    reference = request.notifications[0][1].split("approve withdraw ")[1].split('"')[0]

    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text(
                "UPDATE partner_capital SET created_at = now() - interval '72 hours' "
                "WHERE status = 'pending'"
            )
        )

    result = await handle_approve(f"withdraw {reference}", pair.farida_ctx)
    assert "expired" in result.reply
    assert await _capital_balance(session_factory, pair.rahul.id) == D("0")


async def test_threshold_is_configurable_via_settings(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session, session.begin():
        session.add(
            Setting(
                org_id=ORG,
                key="capital_withdrawal_dual_approval_threshold",
                value=1000,
            )
        )

    # 5000 would post immediately at the 25k default; at a 1k threshold
    # it has to wait
    result = await handle_withdraw("Rahul 5000 cash", pair.rahul_ctx)
    assert "🔒" in result.reply
    assert await _capital_balance(session_factory, pair.rahul.id) == D("0")


async def test_capital_command_redirects_large_withdrawals_to_the_approval_flow(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """docs/08_WhatsApp.md #capital: `capital ... withdrawal` is shorthand
    below the threshold, but above it must not become a second way to
    move large sums without a signature."""
    result = await handle_capital("Rahul 30000 cash withdrawal", pair.rahul_ctx)
    assert "dual-approval threshold" in result.reply
    assert "🔒" in result.reply
    assert await _capital_balance(session_factory, pair.rahul.id) == D("0")


async def test_withdrawal_blocked_when_no_other_partner_can_approve(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A lone partner must not be able to self-serve a large withdrawal
    just because there is nobody to ask."""
    async with session_factory() as session, session.begin():
        solo_user, _ = await _make_partner_user(session, "Solo")
    ctx = RequestContext(user=solo_user, session_factory=session_factory)

    result = await handle_withdraw("Solo 30000 cash", ctx)
    assert "no other partner" in result.reply

    async with session_factory() as session:
        pending = (
            await session.execute(sa.select(sa.func.count()).select_from(PartnerCapital))
        ).scalar_one()
    assert pending == 0


async def test_unknown_partner_is_reported(pair: Pair) -> None:
    result = await handle_capital("Nobody 500 cash", pair.rahul_ctx)
    assert "not found" in result.reply


async def test_capital_commands_are_owner_only() -> None:
    from backend.api.whatsapp_commands import COMMAND_REGISTRY

    for name in ("capital", "withdraw", "approve", "reject"):
        assert COMMAND_REGISTRY[name].min_role == UserRole.OWNER


async def test_dashboard_shows_partner_capital_balances(pair: Pair) -> None:
    """The dashboard's partner-capital section had nothing to show until
    this wave -- docs/12_Dashboard.md §2."""
    from backend.api.commands.report_commands import handle_dashboard

    await handle_capital("Rahul 50000 bank", pair.rahul_ctx)
    result = await handle_dashboard("", pair.rahul_ctx)
    assert "Partner capital" in result.reply
    assert "Rahul ₹50,000.00" in result.reply


@pytest.mark.parametrize("hours_setting", [1, 96])
async def test_expiry_window_follows_settings(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession], hours_setting: int
) -> None:
    async with session_factory() as session, session.begin():
        session.add(
            Setting(org_id=ORG, key="withdrawal_approval_timeout_hours", value=hours_setting)
        )
    request = await handle_withdraw("Rahul 30000 cash", pair.rahul_ctx)
    reference = request.notifications[0][1].split("approve withdraw ")[1].split('"')[0]

    async with session_factory() as session, session.begin():
        await session.execute(
            sa.text(
                "UPDATE partner_capital SET created_at = now() - interval '48 hours' "
                "WHERE status = 'pending'"
            )
        )

    result = await handle_approve(f"withdraw {reference}", pair.farida_ctx)
    if hours_setting == 1:
        assert "expired" in result.reply
    else:
        assert "✅ Approved." in result.reply


async def test_pending_row_carries_no_posted_at(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The DB constraint that keeps 'pending' and 'in the balance chain'
    from ever drifting apart."""
    await handle_withdraw("Rahul 30000 cash", pair.rahul_ctx)
    async with session_factory() as session:
        rows = (
            await session.execute(
                sa.select(PartnerCapital.status, PartnerCapital.posted_at).where(
                    PartnerCapital.org_id == ORG
                )
            )
        ).all()
    assert [tuple(row) for row in rows] == [("pending", None)]


async def test_posted_at_orders_the_chain_not_created_at(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A withdrawal requested before a contribution but approved after it
    must land last in the chain -- the reason posted_at exists."""
    request = await handle_withdraw("Rahul 30000 cash", pair.rahul_ctx)
    reference = request.notifications[0][1].split("approve withdraw ")[1].split('"')[0]
    await handle_capital("Rahul 100000 cash", pair.rahul_ctx)
    await handle_approve(f"withdraw {reference}", pair.farida_ctx)

    async with session_factory() as session:
        ordered = (
            (
                await session.execute(
                    sa.select(PartnerCapital.amount)
                    .where(PartnerCapital.org_id == ORG, PartnerCapital.status == "posted")
                    .order_by(PartnerCapital.posted_at)
                )
            )
            .scalars()
            .all()
        )
    assert list(ordered) == [D("100000.00"), D("-30000.00")]
    assert await _capital_balance(session_factory, pair.rahul.id) == D("70000.00")


async def test_created_at_ordering_would_have_been_wrong(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Companion to the test above, stated as the bug it prevents: by
    created_at the withdrawal comes first, which would make the running
    balance read 70000 -> ... in the wrong order."""
    request = await handle_withdraw("Rahul 30000 cash", pair.rahul_ctx)
    reference = request.notifications[0][1].split("approve withdraw ")[1].split('"')[0]
    await handle_capital("Rahul 100000 cash", pair.rahul_ctx)
    await handle_approve(f"withdraw {reference}", pair.farida_ctx)

    async with session_factory() as session:
        by_created = (
            (
                await session.execute(
                    sa.select(PartnerCapital.amount)
                    .where(PartnerCapital.org_id == ORG, PartnerCapital.status == "posted")
                    .order_by(PartnerCapital.created_at)
                )
            )
            .scalars()
            .all()
        )
    # request predates the contribution, so created_at disagrees with the
    # order money actually moved
    assert list(by_created) == [D("-30000.00"), D("100000.00")]


async def test_journal_balances_for_every_capital_posting(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """JournalService refuses unbalanced entries, so this is really a
    check that every path posts one at all -- and the nightly check in
    docs/06_Accounting.md §12.2 expects it to hold table-wide."""
    await handle_capital("Rahul 100000 cash", pair.rahul_ctx)
    await handle_withdraw("Rahul 5000 cash", pair.rahul_ctx)
    request = await handle_withdraw("Rahul 30000 bank", pair.rahul_ctx)
    reference = request.notifications[0][1].split("approve withdraw ")[1].split('"')[0]
    await handle_approve(f"withdraw {reference}", pair.farida_ctx)

    async with session_factory() as session:
        unbalanced = (
            await session.execute(
                sa.text(
                    "SELECT count(*) FROM ("
                    "  SELECT journal_id FROM journal_lines"
                    "  GROUP BY journal_id HAVING sum(debit) <> sum(credit)"
                    ") bad"
                )
            )
        ).scalar_one()
        capital_journals = (
            await session.execute(
                sa.text(
                    "SELECT count(*) FROM journal WHERE org_id = :org "
                    "AND source_type = 'partner_capital'"
                ),
                {"org": ORG},
            )
        ).scalar_one()
    assert unbalanced == 0
    assert capital_journals == 3  # contribution, small withdrawal, approved withdrawal


async def test_capital_entries_appear_in_the_cash_ledger(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from backend.api.commands.money_commands import handle_cash

    await handle_capital("Rahul 50000 cash", pair.rahul_ctx)
    result = await handle_cash("", pair.rahul_ctx)
    assert "capital_in" in result.reply
    assert "₹50,000.00" in result.reply


async def test_business_date_is_used_for_entry_date(
    pair: Pair, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """docs/02_Database.md §8: DATE columns hold the org's local date."""
    await handle_capital("Rahul 1000 cash", pair.rahul_ctx)
    async with session_factory() as session:
        entry_date = (
            await session.execute(
                sa.select(PartnerCapital.entry_date).where(PartnerCapital.org_id == ORG)
            )
        ).scalar_one()
        tz_today = (
            await session.execute(
                sa.text(
                    "SELECT (now() AT TIME ZONE (SELECT timezone FROM organizations "
                    "WHERE id = :org))::date"
                ),
                {"org": ORG},
            )
        ).scalar_one()
    assert entry_date == tz_today
    assert isinstance(entry_date, datetime.date)
