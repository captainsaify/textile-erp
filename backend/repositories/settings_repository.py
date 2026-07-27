"""Typed reads of the `settings` table -- docs/08_WhatsApp.md #settings
names the command that writes these; this is the read side, which
several features need before that command exists.

Every getter takes a default and coerces defensively: `settings.value`
is JSONB, so a hand-edited row can hold anything, and a thresholds
lookup that raises would take down an unrelated command.
"""

from __future__ import annotations

import decimal
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Setting

# docs/06_Accounting.md §8
DEFAULT_WITHDRAWAL_DUAL_APPROVAL_THRESHOLD = decimal.Decimal("25000")
DEFAULT_WITHDRAWAL_APPROVAL_TIMEOUT_HOURS = 48
# docs/12_Dashboard.md §2
DEFAULT_SLOW_MOVING_DAYS = 60


class SettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _raw(self, org_id: uuid.UUID, key: str) -> object:
        stmt = select(Setting.value).where(Setting.org_id == org_id, Setting.key == key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_int(self, org_id: uuid.UUID, key: str, default: int) -> int:
        value = await self._raw(org_id, key)
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    async def get_decimal(
        self, org_id: uuid.UUID, key: str, default: decimal.Decimal
    ) -> decimal.Decimal:
        value = await self._raw(org_id, key)
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            return default
        try:
            # str() first: Decimal(float) would inherit binary float error,
            # and this value is compared against money.
            return decimal.Decimal(str(value))
        except decimal.InvalidOperation:
            return default

    async def withdrawal_dual_approval_threshold(self, org_id: uuid.UUID) -> decimal.Decimal:
        return await self.get_decimal(
            org_id,
            "capital_withdrawal_dual_approval_threshold",
            DEFAULT_WITHDRAWAL_DUAL_APPROVAL_THRESHOLD,
        )

    async def withdrawal_approval_timeout_hours(self, org_id: uuid.UUID) -> int:
        return await self.get_int(
            org_id,
            "withdrawal_approval_timeout_hours",
            DEFAULT_WITHDRAWAL_APPROVAL_TIMEOUT_HOURS,
        )

    async def slow_moving_days(self, org_id: uuid.UUID) -> int:
        return await self.get_int(org_id, "slow_moving_days", DEFAULT_SLOW_MOVING_DAYS)
