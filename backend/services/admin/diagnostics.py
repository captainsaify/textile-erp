"""Is the machine healthy, and are the running balances honest?

Two things that sound like one. `erp check` already answers whether the
books balance; this answers the questions underneath it — is there disk
left, when did the nightly job last run, how big has the database got,
and are the *snapshot* balances on the ledgers still equal to the sums
they claim to snapshot.

That last one has a repair, and it is the only repair in this file.
`cash_ledger` and `bank_ledger` each carry `resulting_balance`: the
running total as it stood after that row. It is a cache, and like every
cache it can drift from the thing it caches — a row inserted with an
earlier timestamp than the rows after it is enough. Rebuilding walks the
chain in order and rewrites each snapshot from the sum of everything
before it, which is the definition the reconciliation check uses. So the
guard's own ledger check is the proof this worked.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_settings
from backend.models import (
    AuditLog,
    BankLedger,
    CashLedger,
    Customer,
    InventoryMovement,
    PartnerCapital,
    Product,
    PurchaseHeader,
    ReconciliationRun,
    SalesHeader,
    Supplier,
    User,
)
from backend.services.admin.guard import guarded
from backend.services.audit_service import AuditService
from backend.services.backup_service import BackupService, disk_free_bytes

ZERO = decimal.Decimal("0")

#: What a person would want counted. Not every table — a list of 33
#: numbers is not a health check, it is a wall.
COUNTED: dict[str, Any] = {
    "purchases": PurchaseHeader,
    "sales": SalesHeader,
    "products": Product,
    "suppliers": Supplier,
    "customers": Customer,
    "stock movements": InventoryMovement,
    "audit entries": AuditLog,
}


class DiagnosticsService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def report(self, org_id: uuid.UUID) -> dict[str, Any]:
        counts = []
        for label, model in COUNTED.items():
            stmt = select(func.count()).select_from(model).where(model.org_id == org_id)
            if hasattr(model, "deleted_at"):
                stmt = stmt.where(model.deleted_at.is_(None))
            total = (await self._session.execute(stmt)).scalar_one()
            counts.append({"label": label, "count": int(total)})

        size = (
            await self._session.execute(text("SELECT pg_database_size(current_database())"))
        ).scalar_one()

        last_runs = (
            await self._session.execute(
                select(
                    ReconciliationRun.kind,
                    func.max(ReconciliationRun.started_at),
                    func.count(),
                )
                .where(ReconciliationRun.org_id == org_id)
                .group_by(ReconciliationRun.kind)
            )
        ).all()

        stored = BackupService(self._session).list_backups()
        free = disk_free_bytes(_backup_dir())

        return {
            "counts": counts,
            "database_mb": round(int(size) / 1_000_000, 1),
            "disk_free_gb": round(free / 1_000_000_000, 1),
            "backups": len(stored),
            "newest_backup": Path(stored[0].file_path).name if stored else None,
            "nightly": [
                {
                    "kind": kind,
                    "last_run": when.isoformat(),
                    "runs": int(runs),
                    "stale": _stale(when),
                }
                for kind, when, runs in sorted(last_runs)
            ],
            "ledger_drift": await self.ledger_drift(org_id),
        }

    async def ledger_drift(self, org_id: uuid.UUID) -> list[dict[str, str]]:
        """Where a running balance disagrees with the sum it summarises."""
        drift = []
        for name, model in (("cash", CashLedger), ("bank", BankLedger)):
            total = decimal.Decimal(
                (
                    await self._session.execute(
                        select(func.coalesce(func.sum(model.amount), ZERO)).where(
                            model.org_id == org_id
                        )
                    )
                ).scalar_one()
            )
            snapshot = decimal.Decimal(
                (
                    await self._session.execute(
                        select(model.resulting_balance)
                        .where(model.org_id == org_id)
                        .order_by(model.created_at.desc(), model.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                or ZERO
            )
            if snapshot != total:
                drift.append({"ledger": name, "says": str(snapshot), "should_be": str(total)})
        return drift

    async def rebuild_ledgers(self, org_id: uuid.UUID, actor: User) -> dict[str, Any]:
        """Rewrite every running balance from the rows themselves.

        Computes rather than destroys, like `recost`: the amounts are
        untouched and only the derived snapshot beside each one is
        rewritten. Under the guard, so if the result does not balance it
        never happened.
        """
        async with guarded(self._session, org_id) as report:
            fixed = 0
            for name, model in (("cash", CashLedger), ("bank", BankLedger)):
                running = ZERO
                changed = 0
                rows = list(
                    (
                        await self._session.execute(
                            select(model)
                            .where(model.org_id == org_id)
                            # Same ordering the reconciliation check reads
                            # the last row by, so "rebuilt" and "checked"
                            # cannot mean two different chains.
                            .order_by(model.created_at, model.id)
                        )
                    ).scalars()
                )
                for row in rows:
                    running += decimal.Decimal(row.amount)
                    if row.resulting_balance != running:
                        row.resulting_balance = running
                        changed += 1
                fixed += changed
                report.note(f"{name}: {len(rows)} row(s) walked, {changed} corrected")

            partners = (
                await self._session.execute(
                    select(PartnerCapital.partner_id)
                    .where(PartnerCapital.org_id == org_id, PartnerCapital.status == "posted")
                    .distinct()
                )
            ).scalars()
            for partner_id in list(partners):
                running = ZERO
                capital = list(
                    (
                        await self._session.execute(
                            select(PartnerCapital)
                            .where(
                                PartnerCapital.org_id == org_id,
                                PartnerCapital.partner_id == partner_id,
                                PartnerCapital.status == "posted",
                            )
                            .order_by(PartnerCapital.posted_at, PartnerCapital.id)
                        )
                    ).scalars()
                )
                for entry in capital:
                    running += decimal.Decimal(entry.amount)
                    if entry.resulting_balance != running:
                        entry.resulting_balance = running
                        fixed += 1
            await self._session.flush()
            if not fixed:
                report.note("nothing moved — every running balance already agreed")

            await AuditService(self._session).record(
                org_id,
                actor.id,
                action="ledger.rebuilt",
                entity_type="cash_ledger",
                entity_id=org_id,
                after_state={"corrected": fixed},
                channel="cli",
            )

        return {"corrected": fixed, "committed": report.committed, "notes": report.notes}


def _backup_dir() -> Path:
    """Where backups live, without opening a `BackupService` to ask.

    Same expression the service uses. Duplicated deliberately rather than
    reaching into a private method from outside its class -- one line of
    repetition against a name that could be renamed underneath us.
    """
    return Path(get_settings().attachments_dir).parent / "backups"


def _stale(when: datetime.datetime) -> bool:
    """A nightly job that has not run for two days has stopped running.

    Two, not one: a run that fires at 02:00 is a day and a bit old for
    most of the working day, and a health screen that cries every
    afternoon is one nobody reads.
    """
    return when < datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=2)
