"""Reads and writes of the `settings` table, typed through
backend/core/settings_registry.py -- docs/08_WhatsApp.md #settings.

Every getter goes through the registry, so the default a service gets
here is the same value the `settings` command lists and validates
against. There is deliberately no second copy of any default in a
service module.
"""

from __future__ import annotations

import datetime
import decimal
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.settings_registry import REGISTRY, SettingSpec, spec_for
from backend.models import Setting


class SettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _stored(self, org_id: uuid.UUID, key: str) -> object:
        stmt = select(Setting.value).where(Setting.org_id == org_id, Setting.key == key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get(self, org_id: uuid.UUID, key: str) -> decimal.Decimal | int:
        spec = spec_for(key)
        stored = await self._stored(org_id, key)
        return spec.default if stored is None else spec.coerce(stored)

    async def get_int(self, org_id: uuid.UUID, key: str) -> int:
        return int(await self.get(org_id, key))

    async def get_decimal(self, org_id: uuid.UUID, key: str) -> decimal.Decimal:
        return decimal.Decimal(await self.get(org_id, key))

    async def all_values(
        self, org_id: uuid.UUID
    ) -> list[tuple[SettingSpec, decimal.Decimal | int, bool]]:
        """(spec, effective value, is_customised) for every known key, in
        registry order. `is_customised` drives the "(default)" marker in
        the `settings` listing."""
        rows: dict[str, object] = {
            key: value
            for key, value in (
                await self._session.execute(
                    select(Setting.key, Setting.value).where(Setting.org_id == org_id)
                )
            ).all()
        }
        out: list[tuple[SettingSpec, decimal.Decimal | int, bool]] = []
        for key, spec in REGISTRY.items():
            if key in rows:
                out.append((spec, spec.coerce(rows[key]), True))
            else:
                out.append((spec, spec.default, False))
        return out

    async def set(
        self, org_id: uuid.UUID, key: str, value: object, actor_id: uuid.UUID
    ) -> uuid.UUID:
        """Upsert, returning the row id so the caller can audit against
        it. Validation happens in `SettingSpec.parse` before this point --
        this stores what it is given."""
        spec = spec_for(key)
        now = datetime.datetime.now(datetime.UTC)
        stmt = (
            pg_insert(Setting)
            .values(
                org_id=org_id,
                key=spec.key,
                value=value,
                updated_by=actor_id,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[Setting.org_id, Setting.key],
                set_={"value": value, "updated_by": actor_id, "updated_at": now},
            )
            .returning(Setting.id)
        )
        return (await self._session.execute(stmt)).scalar_one()

    # --- named accessors, so call sites read as intent not string keys ---

    async def withdrawal_dual_approval_threshold(self, org_id: uuid.UUID) -> decimal.Decimal:
        return await self.get_decimal(org_id, "capital_withdrawal_dual_approval_threshold")

    async def contribution_dual_approval_threshold(self, org_id: uuid.UUID) -> decimal.Decimal:
        return await self.get_decimal(org_id, "capital_contribution_dual_approval_threshold")

    async def daily_checkin_hour(self, org_id: uuid.UUID) -> int:
        return await self.get_int(org_id, "daily_checkin_hour")

    async def withdrawal_approval_timeout_hours(self, org_id: uuid.UUID) -> int:
        return await self.get_int(org_id, "withdrawal_approval_timeout_hours")

    async def slow_moving_days(self, org_id: uuid.UUID) -> int:
        return await self.get_int(org_id, "slow_moving_days")

    async def purchase_total_mismatch_tolerance(self, org_id: uuid.UUID) -> decimal.Decimal:
        return await self.get_decimal(org_id, "purchase_total_mismatch_tolerance")

    async def duplicate_invoice_window_days(self, org_id: uuid.UUID) -> int:
        return await self.get_int(org_id, "duplicate_invoice_window_days")

    async def below_cost_tolerance(self, org_id: uuid.UUID) -> decimal.Decimal:
        """Stored as a percent (0-100); returned as the fraction the
        below-cost comparison in docs/05_Sales.md §4 actually uses."""
        return await self.get_decimal(org_id, "below_cost_sale_tolerance_percent") / 100

    async def backup_retention_days(self, org_id: uuid.UUID) -> int:
        return await self.get_int(org_id, "backup_retention_days")

    async def undo_window_hours(self, org_id: uuid.UUID) -> int:
        return await self.get_int(org_id, "undo_window_hours")

    async def sale_dedup_window_minutes(self, org_id: uuid.UUID) -> int:
        return await self.get_int(org_id, "sale_dedup_window_minutes")
