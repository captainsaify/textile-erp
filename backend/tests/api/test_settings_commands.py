"""`settings` -- docs/08_WhatsApp.md #settings.

Beyond the command itself, these check the property that makes the
command meaningful: a value set here actually changes what the system
does. A setting nothing reads is a placeholder (CLAUDE.md rule 4), so
every registered key is asserted to have a consumer.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import RequestContext
from backend.api.commands.settings_commands import handle_settings
from backend.core.settings_registry import REGISTRY, SettingError, spec_for
from backend.models import Setting, User
from backend.models.enums import UserRole
from backend.repositories.settings_repository import SettingsRepository
from backend.tests.conftest import SEEDED_ORG_ID, purge_business_rows

D = decimal.Decimal
ORG = uuid.UUID(SEEDED_ORG_ID)


@pytest.fixture(autouse=True)
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    await purge_business_rows(session_factory)


@pytest.fixture
def ctx(owner_user: User, session_factory: async_sessionmaker[AsyncSession]) -> RequestContext:
    return RequestContext(user=owner_user, session_factory=session_factory, message_id="m1")


# --------------------------------------------------------------------
# the registry itself
# --------------------------------------------------------------------


def test_every_registered_key_is_actually_read_somewhere() -> None:
    """The rule that keeps `settings` honest: a key that can be set but
    that nothing consults would be a placeholder. Each registered key
    must have a named accessor on SettingsRepository, which is how
    services reach it."""
    accessors = {
        "capital_withdrawal_dual_approval_threshold": "withdrawal_dual_approval_threshold",
        "capital_contribution_dual_approval_threshold": "contribution_dual_approval_threshold",
        "withdrawal_approval_timeout_hours": "withdrawal_approval_timeout_hours",
        "slow_moving_days": "slow_moving_days",
        "purchase_total_mismatch_tolerance": "purchase_total_mismatch_tolerance",
        "duplicate_invoice_window_days": "duplicate_invoice_window_days",
        "below_cost_sale_tolerance_percent": "below_cost_tolerance",
        "undo_window_hours": "undo_window_hours",
        "backup_retention_days": "backup_retention_days",
        "sale_dedup_window_minutes": "sale_dedup_window_minutes",
    }
    assert set(REGISTRY) == set(accessors)
    for accessor in accessors.values():
        assert hasattr(SettingsRepository, accessor), accessor


def test_int_keys_reject_decimals_and_words() -> None:
    spec = spec_for("slow_moving_days")
    with pytest.raises(SettingError, match="whole number"):
        spec.parse("60.5")
    with pytest.raises(SettingError, match="whole number"):
        spec.parse("sixty")


def test_money_keys_keep_decimal_precision_through_storage() -> None:
    """Stored as a string, not a float -- money must not pick up binary
    float error on the way through JSONB."""
    spec = spec_for("purchase_total_mismatch_tolerance")
    stored = spec.parse("0.07")
    assert stored == "0.07"
    assert spec.coerce(stored) == D("0.07")


def test_range_bounds_are_enforced() -> None:
    percent = spec_for("below_cost_sale_tolerance_percent")
    with pytest.raises(SettingError, match="at most 100"):
        percent.parse("150")
    with pytest.raises(SettingError, match="at least 0"):
        percent.parse("-1")

    hours = spec_for("withdrawal_approval_timeout_hours")
    with pytest.raises(SettingError, match="at least 1"):
        hours.parse("0")


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(SettingError, match="is not a setting"):
        spec_for("colour_of_the_bikeshed")


def test_unusable_stored_value_falls_back_to_default() -> None:
    """A hand-edited row must not take down an unrelated command."""
    spec = spec_for("slow_moving_days")
    assert spec.coerce("not a number") == spec.default
    assert spec.coerce(None) == spec.default
    assert spec.coerce(True) == spec.default  # bool is an int subclass; not a valid setting


# --------------------------------------------------------------------
# the command
# --------------------------------------------------------------------


async def test_listing_shows_every_key_and_marks_defaults(ctx: RequestContext) -> None:
    result = await handle_settings("", ctx)
    for key in REGISTRY:
        assert key in result.reply
    assert "(default)" in result.reply


async def test_setting_a_value_reports_the_change(ctx: RequestContext) -> None:
    result = await handle_settings("slow_moving_days 90", ctx)
    assert "60" in result.reply and "90" in result.reply
    listing = await handle_settings("", ctx)
    assert "slow_moving_days: 90" in listing.reply
    assert "slow_moving_days: 90 (default)" not in listing.reply


async def test_showing_one_key_reports_its_bounds(ctx: RequestContext) -> None:
    result = await handle_settings("below_cost_sale_tolerance_percent", ctx)
    assert "min 0" in result.reply
    assert "max 100" in result.reply


async def test_bad_value_is_rejected_naming_the_expected_type(ctx: RequestContext) -> None:
    result = await handle_settings("slow_moving_days soon", ctx)
    assert "whole number" in result.reply
    # and nothing was written
    async with ctx.session_factory() as session:
        assert await SettingsRepository(session).get(ORG, "slow_moving_days") == 60


async def test_misspelled_key_suggests_the_right_one(ctx: RequestContext) -> None:
    result = await handle_settings("slow_moving_day 90", ctx)
    assert "Did you mean 'slow_moving_days'" in result.reply


async def test_change_is_audited(ctx: RequestContext) -> None:
    await handle_settings("slow_moving_days 90", ctx)
    async with ctx.session_factory() as session:
        row = (
            await session.execute(
                sa.text(
                    "SELECT before_state, after_state FROM audit_logs "
                    "WHERE action = 'settings.updated' AND org_id = :org"
                ),
                {"org": ORG},
            )
        ).one()
    assert row[0]["value"] == "60"
    assert row[1]["value"] == "90"


async def test_settings_is_owner_only() -> None:
    from backend.api.whatsapp_commands import COMMAND_REGISTRY

    assert COMMAND_REGISTRY["settings"].min_role == UserRole.OWNER


# --------------------------------------------------------------------
# settings actually change behaviour
# --------------------------------------------------------------------


async def test_duplicate_invoice_window_widens_the_scan(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A purchase 5 days from an existing invoice is outside the default
    3-day window and inside a 7-day one."""
    from backend.models import PurchaseHeader, Supplier
    from backend.repositories.purchase_repository import PurchaseRepository
    from backend.tests.conftest import SEEDED_MAIN_WAREHOUSE_ID

    async with session_factory() as session, session.begin():
        supplier = Supplier(org_id=ORG, name="Window Test Co", created_by=ctx.user.id)
        session.add(supplier)
        await session.flush()
        base = datetime.date(2026, 7, 10)
        session.add(
            PurchaseHeader(
                org_id=ORG,
                supplier_id=supplier.id,
                warehouse_id=uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID),
                invoice_no="W-1",
                invoice_date=base,
                grand_total=D("100"),
                status="confirmed",
                created_by=ctx.user.id,
            )
        )
        supplier_id = supplier.id

    probe = datetime.date(2026, 7, 15)  # 5 days later
    async with session_factory() as session:
        repo = PurchaseRepository(session)
        assert await repo.find_potential_duplicates(ORG, supplier_id, probe, window_days=3) == []
        assert (
            len(await repo.find_potential_duplicates(ORG, supplier_id, probe, window_days=7)) == 1
        )


async def test_below_cost_tolerance_is_read_as_a_fraction(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Stored as a percent for humans, consumed as a fraction by the
    comparison in docs/05_Sales.md §4 -- an easy place to be off by
    100x."""
    await handle_settings("below_cost_sale_tolerance_percent 5", ctx)
    async with session_factory() as session:
        assert await SettingsRepository(session).below_cost_tolerance(ORG) == D("0.05")


async def test_capital_threshold_change_moves_the_approval_boundary(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        repo = SettingsRepository(session)
        assert await repo.withdrawal_dual_approval_threshold(ORG) == D("25000")
    await handle_settings("capital_withdrawal_dual_approval_threshold 1000", ctx)
    async with session_factory() as session:
        assert await SettingsRepository(session).withdrawal_dual_approval_threshold(ORG) == D(
            "1000"
        )


async def test_defaults_have_exactly_one_home(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With no rows stored, every accessor returns the registry default
    -- so there is no second copy of a default hiding in a service."""
    async with session_factory() as session:
        repo = SettingsRepository(session)
        assert await repo.slow_moving_days(ORG) == REGISTRY["slow_moving_days"].default
        assert (
            await repo.purchase_total_mismatch_tolerance(ORG)
            == REGISTRY["purchase_total_mismatch_tolerance"].default
        )
        assert (
            await repo.sale_dedup_window_minutes(ORG)
            == REGISTRY["sale_dedup_window_minutes"].default
        )


async def test_setting_twice_updates_rather_than_duplicating(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await handle_settings("slow_moving_days 90", ctx)
    await handle_settings("slow_moving_days 120", ctx)
    async with session_factory() as session:
        count = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(Setting)
                .where(Setting.org_id == ORG, Setting.key == "slow_moving_days")
            )
        ).scalar_one()
        assert count == 1
        assert await SettingsRepository(session).slow_moving_days(ORG) == 120
