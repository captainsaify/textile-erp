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


async def _mark_job_failed(factory: Any, job_id: str, exc: Exception) -> None:
    """Record the failure on the row both the follow-up message and the
    API poll read, then tell whoever asked. A silent job is worse than a
    failed one: the user has no way to tell waiting from broken."""
    from backend.models import ReportJob, User

    try:
        async with factory() as session, session.begin():
            job = await session.get(ReportJob, uuid.UUID(job_id))
            if job is None or job.status == "ready":
                return
            job.status = "failed"
            job.error = str(exc)[:500]
            user = await session.get(User, job.created_by)
            number = user.whatsapp_number if user else None
        if number:
            from backend.services.whatsapp_client import get_whatsapp_client

            await get_whatsapp_client().send_text(
                number,
                f"❌ That export (reference {job_id[:8]}) failed and won't arrive. "
                "Try again, and if it keeps failing tell me what you asked for.",
            )
    except Exception as inner:  # noqa: BLE001 -- never mask the original
        logger.error("report_failure_record_failed", job_id=job_id, error=str(inner))


async def _deliver_report(record: Any) -> None:
    """Send the workbook itself, not a note about where it lives.

    A filename inside a container is not something the recipient can
    open. If the upload fails -- or the transport can't carry files at
    all -- say so plainly rather than claiming a delivery that didn't
    happen.
    """
    from backend.services.whatsapp_client import get_whatsapp_client

    client = get_whatsapp_client()
    number, message, path = record.notify_number, record.message, record.file_path
    try:
        send_document = getattr(client, "send_document", None)
        if path is not None and send_document is not None:
            if await send_document(number, path, filename=path.name, caption=message):
                return
            await client.send_text(
                number,
                f"{message}\n\n⚠️ I couldn't attach the file to WhatsApp. "
                "It's saved — ask me to export again, or fetch it from the dashboard.",
            )
            return
        await client.send_text(number, message)
    except Exception as exc:  # noqa: BLE001 -- a send failure must not fail the job
        logger.error("report_notify_failed", error=str(exc))


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
        try:
            async with factory() as session:
                record = await ReportService(session).generate(uuid.UUID(job_id))
        except Exception as exc:  # noqa: BLE001
            # ReportService handles failures *inside* generation. This is
            # for everything before that -- a dead connection, a bad
            # import -- which used to leave the row `queued` forever with
            # the user waiting on a message that was never coming.
            logger.error("report_task_crashed", job_id=job_id, error=str(exc))
            await _mark_job_failed(factory, job_id, exc)
            raise
        if record.notify_number:
            await _deliver_report(record)
        return {"job_id": job_id, "status": record.status}

    return run_async(_run())


@celery_app.task(
    name="group_broadcast_sweep",
    soft_time_limit=60,
    time_limit=90,
    max_retries=0,
)
def group_broadcast_sweep() -> dict[str, Any]:
    """Post recent activity to the partners' group
    (docs/22_GroupBroadcast.md).

    A sweep rather than a hook on each command: it reads only committed
    audit rows, so it can never announce a transaction that rolled back,
    and nobody's WhatsApp reply ever waits on the unofficial relay.

    No retries. A missed sweep is picked up by the next one because the
    watermark only advances over what was actually delivered; retrying
    would risk posting the same activity twice, which is worse than
    posting it a minute late.
    """

    async def _run() -> dict[str, Any]:
        from backend.models import Organization
        from backend.services.broadcast_service import (
            claim_watermark,
            pending_lines,
            write_watermark,
        )
        from backend.services.group_relay import get_group_relay

        relay = get_group_relay()
        if not relay.enabled:
            return {"skipped": "group broadcasting is off"}

        factory = get_session_factory()
        async with factory() as session:
            org_ids = list((await session.execute(select(Organization.id))).scalars())

        sent = 0
        for org_id in org_ids:
            # see partner_notice_sweep: a start point that is recomputed
            # each tick makes the window permanently empty
            async with factory() as session, session.begin():
                since = await claim_watermark(session, org_id)
            async with factory() as session:
                lines, newest = await pending_lines(session, org_id, since)
            if not lines or newest is None:
                continue

            body = "\n".join(lines)
            result = await relay.send_text(body)
            if not result.delivered:
                # the watermark stays put, so the next sweep retries the
                # same activity rather than losing it
                logger.warning("group_broadcast_undelivered", reason=result.reason)
                continue

            async with factory() as session, session.begin():
                await write_watermark(session, org_id, newest)
            sent += len(lines)
        return {"lines": sent}

    return run_async(_run())


def _notice_client() -> Any:
    """Whichever transport this deployment actually sends on."""
    from backend.core.config import get_settings
    from backend.services.whatsapp_bridge_client import get_bridge_sender
    from backend.services.whatsapp_client import get_whatsapp_client

    if get_settings().whatsapp_transport == "webjs":
        return get_bridge_sender()
    return get_whatsapp_client()


async def _notice_document(org_id: uuid.UUID, reference: Any) -> Any:
    """Build the sheet a notice carries, from the row rather than from
    the message -- so a bill corrected twice arrives as it stands now."""
    from backend.services.document_service import DocumentService

    factory = get_session_factory()
    async with factory() as session:
        service = DocumentService(session)
        if reference.kind == "purchase":
            return await service.purchase(org_id, uuid.UUID(reference.reference))
        if reference.kind == "sale":
            return await service.sale(org_id, uuid.UUID(reference.reference))
        return await service.payment(org_id, reference.reference)


async def _send_notice(client: Any, org_id: uuid.UUID, number: str, notice: Any) -> bool:
    """One partner, one transaction, and its sheet if it has one.

    The text goes first and on its own: a document that fails to build
    or upload must still leave the partner knowing what happened.
    Returning False means nothing reached them at all, which is what
    holds the watermark back.
    """
    from backend.services.partner_notice_service import caption_for

    try:
        if not await client.send_text(number, notice.body):
            return False
    except Exception as exc:  # noqa: BLE001 -- a send failure is never fatal
        logger.error("partner_notice_text_failed", to=number, error=str(exc))
        return False

    send_document = getattr(client, "send_document", None)
    if notice.document is None or send_document is None:
        # no sheet for this kind of change, or a transport with no file
        # channel. Either way the headline already went.
        return True

    try:
        document = await _notice_document(org_id, notice.document)
        await send_document(
            number,
            document.path,
            filename=document.path.name,
            caption=caption_for(notice)[:1024],
        )
    except Exception as exc:  # noqa: BLE001 -- see the docstring
        logger.warning(
            "partner_notice_document_failed",
            kind=notice.document.kind,
            reference=notice.document.reference,
            error=str(exc),
        )
    return True


@celery_app.task(
    name="partner_notice_sweep",
    soft_time_limit=120,
    time_limit=180,
    max_retries=0,
)
def partner_notice_sweep() -> dict[str, Any]:
    """Tell the partners who did not record it what was recorded
    (docs/22_GroupBroadcast.md §7).

    A sweep over committed audit rows, like the group broadcast and for
    the same three reasons — but per person rather than to a chat, and
    carrying the transaction's own sheet, because a partner who is told
    about a bill and cannot see it has been told half of something.

    No retries. The watermark advances only over notices that actually
    went out, so a missed sweep is picked up by the next one; retrying
    would risk telling someone twice, which reads as two transactions.
    """

    async def _run() -> dict[str, Any]:
        from backend.services.broadcast_service import claim_watermark, write_watermark
        from backend.services.partner_notice_service import (
            WATERMARK_KEY,
            pending_notices,
            recipients,
        )

        client = _notice_client()
        factory = get_session_factory()
        sent = 0
        for org_id in await _org_ids():
            # `claim`, not `read`: the start point is written on first
            # sight, or every sweep asks for activity after *this
            # instant* and nothing is ever the first thing sent.
            async with factory() as session, session.begin():
                since = await claim_watermark(session, org_id, WATERMARK_KEY)
            async with factory() as session:
                notices, newest = await pending_notices(session, org_id, since)
            if not notices or newest is None:
                continue

            delivered_through: datetime.datetime | None = None
            for notice in notices:
                async with factory() as session:
                    people = await recipients(session, org_id, exclude_user_id=notice.actor_user_id)
                if not people:
                    # nobody else to tell -- still counts as handled, or
                    # a one-owner org would re-read it every minute
                    delivered_through = notice.at
                    continue
                results = [
                    await _send_notice(client, org_id, person.number, notice) for person in people
                ]
                if not any(results):
                    # nothing reached anyone: stop here so the next sweep
                    # resumes from this notice rather than skipping it
                    break
                delivered_through = notice.at
                sent += sum(results)

            if delivered_through is not None:
                async with factory() as session, session.begin():
                    await write_watermark(session, org_id, delivered_through, WATERMARK_KEY)
        return {"notices": sent}

    return run_async(_run())


@celery_app.task(
    name="daily_checkin",
    soft_time_limit=120,
    time_limit=180,
    max_retries=0,
)
def daily_checkin() -> dict[str, Any]:
    """One message a day to each owner, at a fixed hour.

    WhatsApp only lets a business send a free-form message to someone
    who messaged it in the last 24 hours. Partners who spend a day not
    typing anything fall outside that window, and every notification
    aimed at them is refused -- silently, until delivery receipts were
    being read.

    So the day opens with one predictable message carrying the last few
    updates. Replying to it re-opens the window for the next 24 hours,
    which is what makes the rest of the day's notices deliverable. It is
    fine to miss one: the same list is always available on demand with
    `activity`, and missing it costs delivery, never the record.

    Fires hourly and sends only to orgs whose configured hour it is, so
    the time is a setting rather than a redeploy.
    """

    async def _run() -> dict[str, Any]:
        from backend.repositories.accounting_repository import business_now
        from backend.repositories.settings_repository import SettingsRepository
        from backend.services.partner_notice_service import recent_activity, recipients

        client = _notice_client()
        factory = get_session_factory()
        sent = 0

        for org_id in await _org_ids():
            async with factory() as session:
                # the org's own clock, not the server's: "9 in the
                # morning" is a fact about the business, not about UTC
                now = await business_now(session, org_id)
                if await SettingsRepository(session).daily_checkin_hour(org_id) != now.hour:
                    continue
                lines = await recent_activity(session, org_id, limit=DAILY_CHECKIN_LINES)
                people = await recipients(session, org_id, exclude_user_id=None)

            body = _checkin_body(now.date(), lines)
            for person in people:
                try:
                    if await client.send_text(person.number, body):
                        sent += 1
                except Exception as exc:  # noqa: BLE001 -- never fatal
                    logger.error("daily_checkin_failed", to=person.number, error=str(exc))
        return {"sent": sent}

    return run_async(_run())


#: Enough to show the day was covered without becoming a report nobody
#: reads. `activity` is there for the full list.
DAILY_CHECKIN_LINES = 5


def _checkin_body(today: datetime.date, lines: list[Any]) -> str:
    """Worth replying to, or it will be ignored and the window stays
    shut. So it carries yesterday's actual figures rather than a bare
    'please reply' -- the reply is a side effect of it being useful."""
    from backend.api.formatting import fmt_date

    rendered = [f"☀️ Good morning — {fmt_date(today)}"]
    if lines:
        rendered.append("")
        rendered.append("Since you last looked:")
        rendered.extend(f"• {line.body}" for line in lines)
    else:
        rendered.append("")
        rendered.append("Nothing new was recorded yesterday.")
    rendered.append("")
    rendered.append(
        "Reply to this message — even just 'ok' — so the day's updates can reach you. "
        "Send 'activity' any time for the last 10."
    )
    return "\n".join(rendered)
