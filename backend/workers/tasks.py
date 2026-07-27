"""Task definitions -- docs/11_BackgroundWorkers.md §2.

Thin by rule: fetch inputs, call one service method, handle retry
semantics (§1). Every task declares its timeouts so a stuck job is
killed rather than holding a worker slot, and every scheduled task
escalates final failure to an owner rather than failing quietly (§4.3)
-- a nightly job that stops running is exactly the failure nobody
notices.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import select

from backend.core.db import get_session_factory
from backend.core.logging import get_logger
from backend.models import Organization, User
from backend.models.enums import UserRole
from backend.services.reconciliation_service import ReconciliationService
from backend.workers.app import celery_app, run_async

logger = get_logger(__name__)


async def _org_ids() -> list[uuid.UUID]:
    factory = get_session_factory()
    async with factory() as session:
        return list((await session.execute(select(Organization.id))).scalars())


async def _owners(org_id: uuid.UUID) -> list[str]:
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            await session.execute(
                select(User.whatsapp_number).where(
                    User.org_id == org_id,
                    User.role == UserRole.OWNER,
                    User.deleted_at.is_(None),
                    User.whatsapp_number.is_not(None),
                )
            )
        ).scalars()
        return [number for number in rows if number]


async def _alert_owners(org_id: uuid.UUID, body: str) -> None:
    """Best effort: a reconciliation result is already durable in
    `reconciliation_runs`, so a failed send must not lose it."""
    from backend.services.whatsapp_client import get_whatsapp_client

    client = get_whatsapp_client()
    for number in await _owners(org_id):
        try:
            await client.send_text(number, body)
        except Exception as exc:  # noqa: BLE001 -- alerting must not raise
            logger.error("owner_alert_failed", to=number, error=str(exc))


async def _reconcile(kind: str) -> dict[str, Any]:
    factory = get_session_factory()
    summary: dict[str, Any] = {"kind": kind, "orgs": 0, "mismatches": 0}
    for org_id in await _org_ids():
        async with factory() as session, session.begin():
            outcome = await ReconciliationService(session).run(org_id, kind)
        summary["orgs"] += 1
        if not outcome.ok:
            summary["mismatches"] += len(outcome.discrepancies)
            await _alert_owners(org_id, outcome.alert_text())
    return summary


@celery_app.task(
    name="inventory_reconciliation",
    soft_time_limit=300,
    time_limit=360,
    autoretry_for=(OSError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=2,
)
def inventory_reconciliation() -> dict[str, Any]:
    """CLAUDE.md's acceptance criterion, enforced nightly against live
    data: qty_on_hand equals the signed sum of movements. Never
    auto-corrects (docs/03_Inventory.md §6)."""
    return run_async(_reconcile("inventory"))


@celery_app.task(
    name="ledger_reconciliation",
    soft_time_limit=300,
    time_limit=360,
    autoretry_for=(OSError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=2,
)
def ledger_reconciliation() -> dict[str, Any]:
    """docs/06_Accounting.md §12: balances re-summed from scratch and
    every journal entry checked for debits == credits."""
    return run_async(_reconcile("ledger"))


async def _low_stock_scan() -> dict[str, Any]:
    from backend.services.stock_service import StockService

    factory = get_session_factory()
    total = 0
    for org_id in await _org_ids():
        async with factory() as session:
            rows = await StockService(session).low_stock(org_id)
            negative = [row for row in rows if row.qty_on_hand < 0]
        if not rows:
            continue
        total += len(rows)
        lines = [f"📉 Nightly stock check — {len(rows)} item(s) at or below reorder level:"]
        lines.extend(f"• {row.code}: {row.qty_on_hand} {row.unit_code}" for row in rows[:10])
        if negative:
            lines.append(f"⚠️ {len(negative)} of them are negative.")
        await _alert_owners(org_id, "\n".join(lines))
    return {"flagged": total}


@celery_app.task(
    name="low_stock_scan",
    soft_time_limit=120,
    time_limit=180,
    autoretry_for=(OSError,),
    retry_backoff=True,
    max_retries=2,
)
def low_stock_scan() -> dict[str, Any]:
    """Nightly sweep catching what the post-sale inline check can't --
    e.g. a reorder_level configured after stock was already low
    (docs/11_BackgroundWorkers.md §7)."""
    return run_async(_low_stock_scan())


async def _session_expiry_sweep() -> dict[str, Any]:
    from backend.models import WhatsappSession

    factory = get_session_factory()
    removed = 0
    async with factory() as session, session.begin():
        stale = (
            await session.execute(
                select(WhatsappSession).where(
                    WhatsappSession.expires_at < datetime.datetime.now(datetime.UTC)
                )
            )
        ).scalars()
        for row in stale:
            await session.delete(row)
            removed += 1
    return {"expired": removed}


@celery_app.task(name="session_expiry_sweep", soft_time_limit=30, time_limit=60, max_retries=1)
def session_expiry_sweep() -> dict[str, Any]:
    """Redis expires its own copy; this clears the durable mirror so an
    abandoned draft doesn't reappear if Redis is later restored from a
    snapshot."""
    return run_async(_session_expiry_sweep())


async def _withdrawal_timeout_sweep() -> dict[str, Any]:
    from backend.models import PartnerCapital
    from backend.repositories.settings_repository import SettingsRepository

    factory = get_session_factory()
    expired = 0
    for org_id in await _org_ids():
        async with factory() as session, session.begin():
            hours = await SettingsRepository(session).withdrawal_approval_timeout_hours(org_id)
            cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=hours)
            rows = (
                await session.execute(
                    select(PartnerCapital).where(
                        PartnerCapital.org_id == org_id,
                        PartnerCapital.status == "pending",
                        PartnerCapital.created_at < cutoff,
                    )
                )
            ).scalars()
            for row in rows:
                # expiring a request moves no money -- a pending row
                # never entered the balance chain (§8 of Accounting)
                row.status = "rejected"
                expired += 1
    return {"expired": expired}


@celery_app.task(
    name="withdrawal_approval_timeout_sweep", soft_time_limit=30, time_limit=60, max_retries=1
)
def withdrawal_approval_timeout_sweep() -> dict[str, Any]:
    """docs/06_Accounting.md §8: an unanswered withdrawal request expires
    rather than waiting indefinitely for a signature."""
    return run_async(_withdrawal_timeout_sweep())


@celery_app.task(
    name="nightly_backup",
    soft_time_limit=600,
    time_limit=900,
    autoretry_for=(OSError,),
    retry_backoff=True,
    max_retries=2,
)
def nightly_backup() -> dict[str, Any]:
    """docs/11_BackgroundWorkers.md §5. Failure is escalated to owners
    every night until resolved -- a failing backup must never become a
    notification people learn to ignore."""
    from backend.services.backup_service import BackupService

    async def _run() -> dict[str, Any]:
        factory = get_session_factory()
        results: dict[str, Any] = {"orgs": 0}
        for org_id in await _org_ids():
            async with factory() as session:
                try:
                    record = await BackupService(session).create_backup(org_id)
                except Exception as exc:  # noqa: BLE001 -- escalate, then re-raise
                    logger.error("nightly_backup_failed", org_id=str(org_id), error=str(exc))
                    await _alert_owners(
                        org_id,
                        f"🚨 Tonight's backup FAILED. Your data is not backed up. Error: {exc}",
                    )
                    raise
            results["orgs"] += 1
            results[str(org_id)] = record.file_path
        return results

    return run_async(_run())


@celery_app.task(
    name="report_generation",
    soft_time_limit=180,
    time_limit=240,
    autoretry_for=(OSError,),
    retry_backoff=True,
    max_retries=2,
)
def report_generation(job_id: str) -> dict[str, Any]:
    """Drives one `report_jobs` row from queued to ready/failed
    (docs/11_BackgroundWorkers.md §8)."""
    from backend.services.report_service import ReportService

    async def _run() -> dict[str, Any]:
        factory = get_session_factory()
        async with factory() as session:
            record = await ReportService(session).generate(uuid.UUID(job_id))
        if record.notify_number:
            from backend.services.whatsapp_client import get_whatsapp_client

            try:
                await get_whatsapp_client().send_text(record.notify_number, record.message)
            except Exception as exc:  # noqa: BLE001
                logger.error("report_notify_failed", error=str(exc))
        return {"job_id": job_id, "status": record.status}

    return run_async(_run())
